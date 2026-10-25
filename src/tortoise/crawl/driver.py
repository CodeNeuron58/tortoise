"""A narrow browser-driver protocol plus the real Stagehand implementation.

Tortoise's crawl logic (page flows, pagination, politeness) is written against
:class:`BrowserDriver` — four awaited calls that mirror Stagehand's act /
extract / observe vocabulary. Tests drive the logic with a fake; production
uses :class:`StagehandDriver`, so any Stagehand API drift is fixed in this one
file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel
from stagehand import Page, Stagehand
from stagehand import local_browser

from .llm_adapter import OpenAICompatLLM

T = TypeVar("T", bound=BaseModel)

logger = logging.getLogger(__name__)


class DriverError(Exception):
    """A browser action failed; the activity layer decides whether to retry."""


@runtime_checkable
class BrowserDriver(Protocol):
    """What crawl flows need from a browser. Nothing more."""

    async def goto(self, url: str) -> None: ...

    async def act(self, instruction: str) -> str:
        """Perform a click/choice; raise DriverError when it fails."""
        ...

    async def extract(self, instruction: str, schema: type[T]) -> T: ...

    async def observe(self, instruction: str) -> list[str]: ...

    async def click_direct(self, purpose: str) -> bool:
        """Direct-selector click for a known stable control; True on success."""
        ...

    async def submit_idox_weekly_form(self) -> None: ...

    async def extract_idox_rows(self) -> dict: ...

    async def extract_idox_documents(self) -> dict: ...

    async def extract_idox_dates(self) -> dict: ...

    async def fetch_pdf(self, url: str) -> bytes: ...

    async def close(self) -> None: ...


# Set this to a directory to save a screenshot after every page Tortoise
# opens: evidence of a real run, and the raw footage for the demo video.
CAPTURE_DIR_ENV = "TORTOISE_CAPTURE_DIR"

# Browserbase ends a session on its own after this many seconds — the crawl
# activity's own 12-minute budget — so a session orphaned by a killed worker
# cannot run on. Every normal run releases its sessions explicitly long before.
_SESSION_TIMEOUT_SECONDS = 720

# Largest document fetched through the session (base64 crosses CDP).
_MAX_SESSION_FETCH_BYTES = 30 * 1024 * 1024


class StagehandDriver:
    """BrowserDriver backed by Stagehand v4 against one page."""

    def __init__(
        self,
        stagehand: Stagehand,
        page: Page,
        release: "Callable[[], Awaitable[None]] | None" = None,
    ) -> None:
        self._stagehand = stagehand
        self._page = page
        self._release = release
        self._started = time.monotonic()
        self._capture_dir = _capture_dir()
        self.session_id = ""  # the Browserbase session id, for its recording

    @classmethod
    async def create_local(
        cls,
        llm: OpenAICompatLLM,
        headless: bool = True,
        executable_path: str | None = None,
    ) -> "StagehandDriver":
        """Launch local Chrome — how Tortoise develops and tests, costing no quota."""
        browser = await local_browser.launch(headless=headless, executable_path=executable_path)
        # The callback is passed bare: Stagehand wraps it in its ClientLLM model itself.
        stagehand = await Stagehand.create(browser=browser, model=llm.generate)
        page = await cls._first_page(stagehand)
        return cls(stagehand, page)

    @classmethod
    async def create_browserbase(
        cls,
        llm: OpenAICompatLLM,
        api_key: str,
        project_id: str,
        region: str | None = None,
        extension_cache: Path | None = None,
    ) -> "StagehandDriver":
        """Create one Browserbase session and connect Stagehand to it.

        SEAM: this is the one integration that cannot be exercised offline.

        Browserbase rejects CDP ``Extensions.loadUnpacked``, so Stagehand's
        bundled extension is pre-installed the Browserbase way: build its
        archive, upload it once per project (the id is cached), and pass it
        at session creation. The connect URL comes from the session itself
        (a region-specific ``wss://connect.<region>.browserbase.com/`` URL
        with an embedded signing key — not a fixed hostname).

        UK council portals commonly block non-EU traffic, so real runs pin
        ``region`` (``eu-central-1``) unless testing says otherwise.

        Closing the driver *releases* the session through the API: a
        long-lived worker otherwise keeps the CDP socket open and the
        session runs on, billed, until Browserbase's own timeout.
        """
        from browserbase import AsyncBrowserbase
        from stagehand.browserbase_session import BrowserbaseRegion

        create_kwargs: dict = {"project_id": project_id, "api_timeout": _SESSION_TIMEOUT_SECONDS}
        if region:
            create_kwargs["region"] = BrowserbaseRegion(region)

        async with AsyncBrowserbase(api_key=api_key) as client:
            if extension_cache is not None:
                create_kwargs["extension_id"] = await _ensure_extension_id(
                    client, api_key, extension_cache
                )
            session = await client.sessions.create(**create_kwargs)
            connect_url = getattr(session, "connect_url", None)
            if not connect_url:
                # The SDK's session model omits connect_url; the raw API has it.
                connect_url = await asyncio.to_thread(
                    _fetch_connect_url, api_key, session.id
                )
        logger.info("browserbase session %s (%s)", session.id, region or "default region")

        async def release() -> None:
            async with AsyncBrowserbase(api_key=api_key) as client:
                await client.sessions.update(
                    session.id, status="REQUEST_RELEASE", project_id=project_id
                )

        try:
            browser = await local_browser.connect(cdp_url=connect_url)
            # The callback is passed bare: Stagehand wraps it in its ClientLLM model itself.
            stagehand = await Stagehand.create(browser=browser, model=llm.generate)
            page = await cls._first_page(stagehand)
        except Exception:
            await _quietly(release)
            raise
        driver = cls(stagehand, page, release=release)
        driver.session_id = session.id
        return driver

    @staticmethod
    async def _first_page(stagehand: Stagehand) -> Page:
        context = getattr(stagehand.browser, "context", None)
        if context is not None:
            page = await context.active_page()
            if page is not None:
                return page
        raise DriverError("Stagehand did not expose an active page to drive")

    @property
    def seconds_in_browser(self) -> float:
        """Wall-clock seconds this session has been alive (for the Lazy Payoff)."""
        return time.monotonic() - self._started

    async def goto(self, url: str) -> None:
        try:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            raise DriverError(f"could not open {url}: {exc}") from exc
        await self.capture(_page_label(url))

    async def capture(self, label: str) -> None:
        """Save a screenshot when TORTOISE_CAPTURE_DIR is set; never fails a run."""
        if self._capture_dir is None:
            return
        try:
            self._capture_dir.mkdir(parents=True, exist_ok=True)
            index = len(list(self._capture_dir.glob("*.png"))) + 1
            path = self._capture_dir / f"{time.strftime('%H%M%S')}-{index:03d}-{label}.png"
            await self._page.screenshot(path=str(path))
        except Exception as exc:  # noqa: BLE001 — footage is optional
            logger.warning("screenshot failed: %s", exc)

    async def act(self, instruction: str) -> str:
        try:
            result = await self._stagehand.act(instruction, page=self._page)
        except Exception as exc:
            raise DriverError(f"action failed ({instruction!r}): {exc}") from exc
        data = getattr(result, "data", None)
        if data is None or not getattr(data, "success", False):
            message = getattr(data, "message", "unknown reason")
            raise DriverError(f"action did not succeed ({instruction!r}): {message}")
        return getattr(data, "message", "")

    async def extract(self, instruction: str, schema: type[T]) -> T:
        try:
            result = await self._stagehand.extract(instruction, schema=schema, page=self._page)
        except Exception as exc:
            raise DriverError(f"extraction failed ({instruction!r}): {exc}") from exc
        data = getattr(result, "data", None)
        if data is None:
            raise DriverError(f"extraction returned nothing ({instruction!r})")
        return data

    async def observe(self, instruction: str) -> list[str]:
        try:
            results = await self._stagehand.observe(instruction, page=self._page)
        except Exception as exc:
            raise DriverError(f"observation failed ({instruction!r}): {exc}") from exc
        return [
            getattr(item, "description", None) or str(item)
            for item in (results or [])
        ]

    async def click_direct(self, purpose: str) -> bool:
        """Click a stable control by CSS selector or visible text, via JS."""
        import json as _json

        selectors = list(_DIRECT_SELECTORS.get(purpose, ()))
        texts = list(_DIRECT_TEXTS.get(purpose, ()))
        if not selectors and not texts:
            return False
        js = (
            _CLICK_JS_TEMPLATE.replace("__SELECTORS__", _json.dumps(selectors))
            .replace("__TEXTS__", _json.dumps(texts))
        )
        try:
            result = await self._page.evaluate(js)
        except Exception:
            return False
        return bool(result)

    async def submit_idox_weekly_form(self) -> None:
        """Submit Idox's weekly-list form deterministically (no LLM).

        The form already defaults to last week; this just ensures the
        'validated' radio is chosen and submits, CSRF token included.
        """
        try:
            await self._page.evaluate(
                '(() => { const r = document.querySelector(\'input[value="DC_Validated"]\');'
                " if (r && !r.checked) r.click();"
                " document.querySelector('form').submit(); })()"
            )
            await self._page.wait_for_timeout(5000)
        except Exception as exc:
            raise DriverError(f"could not submit the weekly list form: {exc}") from exc
        await self.capture("results")

    async def extract_idox_rows(self) -> dict:
        """Parse Idox's search results straight from the DOM (no LLM).

        Returns ``{"rows": [...], "next_href": str|null}`` where each row has
        reference, address, proposal, url and validated_on. Deterministic and
        free — the plan's 'direct selectors for the stable parts'.
        """
        try:
            return await self._page.evaluate(_IDOX_ROWS_JS)
        except Exception as exc:
            raise DriverError(f"could not parse the weekly list results: {exc}") from exc

    async def extract_idox_documents(self) -> dict:
        """Parse the documents table deterministically (no LLM).

        The Idox documents table (``#Documents``) populates asynchronously,
        so this waits for rows before parsing. Returns
        ``{"documents": [{"name": ..., "url": ...}]}``.
        """
        try:
            await self._page.wait_for_timeout(4000)
            await self.capture("documents-loaded")
            return await self._page.evaluate(_IDOX_DOCUMENTS_JS)
        except Exception as exc:
            raise DriverError(f"could not parse the documents table: {exc}") from exc

    async def extract_idox_dates(self) -> dict:
        """Parse the Idox 'Important Dates' tab deterministically (no LLM).

        Returns ``{"found": bool, "dates": {label: value}}`` straight from
        ``#simpleDetailsTable`` — the consultation expiry dates live here,
        not on the summary tab.
        """
        try:
            return await self._page.evaluate(_IDOX_DATES_JS)
        except Exception as exc:
            raise DriverError(f"could not parse the important dates: {exc}") from exc

    async def fetch_pdf(self, url: str) -> bytes:
        """Download bytes from inside the browser session.

        Council portals geo-block non-EU traffic, so documents must be
        fetched through the same EU session that crawled them — a plain
        local HTTP GET would time out. The script is a self-invoking async
        function with the URL embedded as a JSON literal.
        """
        import base64

        js = _FETCH_JS_TEMPLATE.replace("__URL__", json.dumps(url)).replace(
            "__MAX_BYTES__", str(_MAX_SESSION_FETCH_BYTES)
        )
        try:
            result = await self._page.evaluate(js)
        except Exception as exc:
            raise DriverError(f"session fetch failed for {url}: {exc}") from exc
        if not isinstance(result, dict) or "b64" not in result:
            detail = result.get("error") if isinstance(result, dict) else result
            raise DriverError(f"session fetch rejected {url}: {detail}")
        return base64.b64decode(result["b64"])

    async def close(self) -> None:
        try:
            await self._stagehand.close()
        except Exception:
            # Closing must never mask the real outcome of a run.
            pass
        if self._release is not None:
            await _quietly(self._release)
            self._release = None


async def _ensure_extension_id(client, api_key: str, cache_dir: Path) -> str:
    """Stagehand's extension id on Browserbase, uploading it once per project.

    Browserbase only allows extensions installed through its own API, so the
    archive Stagehand ships is uploaded and the resulting id cached in the
    data directory; later sessions just reference it.
    """
    from stagehand.browserbase_session import build_extension_archive

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / "browserbase_extension_id"
    if cache_file.exists():
        cached = cache_file.read_text(encoding="utf-8").strip()
        if cached:
            return cached
    archive = build_extension_archive()
    extension = await client.extensions.create(
        file=("stagehand-extension.zip", archive, "application/zip")
    )
    cache_file.write_text(extension.id, encoding="utf-8")
    return extension.id


def _fetch_connect_url(api_key: str, session_id: str) -> str:
    """The session's real CDP websocket URL, from the raw Browserbase API.

    The API returns ``connectUrl`` — a ``wss://connect.<region>.browserbase.com``
    URL carrying an embedded signing key, valid for that session only.
    """
    import json
    import urllib.request

    request = urllib.request.Request(
        f"https://api.browserbase.com/v1/sessions/{session_id}",
        headers={"x-bb-api-key": api_key},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.load(response)
    connect_url = data.get("connectUrl")
    if not connect_url:
        raise DriverError("Browserbase session has no connectUrl (already expired?)")
    return connect_url


async def _quietly(release: Callable[[], Awaitable[None]]) -> None:
    """Release a session, logging instead of raising (cleanup must not mask)."""
    try:
        await release()
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not release the browser session: %s", exc)


def _capture_dir() -> Path | None:
    raw = os.environ.get(CAPTURE_DIR_ENV, "").strip()
    return Path(raw) if raw else None


def _page_label(url: str) -> str:
    """A short filename-safe label for a screenshot of ``url``."""
    tab = re.search(r"activeTab=([A-Za-z]+)", url)
    if tab:
        return tab.group(1)
    page = re.search(r"searchCriteria\.page=(\d+)", url)
    if page:
        return f"results-page-{page.group(1)}"
    if "weeklyList" in url:
        return "weekly-list-form"
    return "page"


# Direct-click fallbacks for the stable parts of Idox Public Access pages,
# per the plan's advice ("use direct selectors for the stable parts"). The
# protocol-native act() is always tried first; these are the safety net.
_DIRECT_SELECTORS: dict[str, tuple[str, ...]] = {
    "search": (
        "input[name='search']",
        "button[type='submit']",
        "form input[type='submit']",
        "#search",
    ),
    "next": (
        "a.next",
        "li.next a",
        "a[rel='next']",
        ".pagination a:last-child",
    ),
    "documents": (
        "#tab-documents a",
        "li#tab-documents a",
        "a[href*='activeTab=documents']",
    ),
}
_DIRECT_TEXTS: dict[str, tuple[str, ...]] = {
    "search": ("search",),
    "next": ("next", "next page", "»", "›"),
    "documents": ("documents",),
}

_CLICK_JS_TEMPLATE = """\
(() => {
  const selectors = __SELECTORS__;
  const texts = __TEXTS__;
  for (const s of selectors) {
    const el = document.querySelector(s);
    if (el) { el.click(); return s; }
  }
  const candidates = document.querySelectorAll('a, button, input[type=submit]');
  for (const el of candidates) {
    const label = (el.textContent || el.value || '').trim().toLowerCase();
    if (texts.some((t) => label === t)) { el.click(); return 'text:' + label; }
  }
  return null;
})()
"""

# Deterministic Idox Public Access results parser — the DOM every Idox council
# shares: .searchresult blocks with a summaryLink (proposal + details URL),
# p.address, and p.metaInfo carrying the reference and dates.
_IDOX_ROWS_JS = """\
(() => {
  const rows = Array.from(document.querySelectorAll('.searchresult')).map((el) => {
    const link = el.querySelector('a.summaryLink');
    const proposal = (el.querySelector('.summaryLinkTextClamp') || {}).textContent || '';
    const address = ((el.querySelector('p.address') || {}).textContent || '').replace(/\\s+/g, ' ').trim();
    const meta = ((el.querySelector('p.metaInfo') || {}).textContent || '').replace(/\\s+/g, ' ');
    const ref = (meta.match(/Ref\\.?\\s*No:?\\s*([^\\s\u00b7]+)/) || [])[1] || '';
    let validated = (meta.match(/Validated:\\s*([A-Za-z0-9 ]+)/) || [])[1] || '';
    if (!validated) validated = (meta.match(/Received:\\s*([A-Za-z0-9 ]+)/) || [])[1] || '';
    validated = validated.trim().replace(/^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\\s+/i, '');
    return {
      reference: ref.trim(),
      address: address,
      proposal: proposal.replace(/\\s+/g, ' ').trim(),
      url: link ? (link.getAttribute('href') || '') : '',
      validated_on: validated,
    };
  });
  const next = Array.from(document.querySelectorAll('a'))
    .find((a) => /^next$/i.test((a.textContent || '').trim()));
  return { rows: rows, next_href: next ? next.getAttribute('href') : null };
})()
"""

# Deterministic Idox documents-table parser. Columns vary slightly between
# councils; the name is the joined cell text (type + description feed the
# priority selector) and the URL is the row's non-sort anchor.
_IDOX_DOCUMENTS_JS = """\
(() => {
  const table = document.querySelector('#Documents') || document.querySelector('table#Documents');
  if (!table) { return { documents: [] }; }
  const docs = Array.from(table.rows).slice(1).map((tr) => {
    const cells = Array.from(tr.cells || [])
      .map((c) => (c.textContent || '').replace(/[\\s]+/g, ' ').trim())
      .filter((t) => t.length > 0 && t !== 'View' && !/^select this document$/i.test(t)
              && !/^\\d{1,2} [A-Za-z]{3,9} \\d{4}$/.test(t));
    const links = Array.from(tr.querySelectorAll('a'))
      .map((a) => a.getAttribute('href') || '')
      .filter((h) => h.length > 1 && h.charAt(0) !== '#'
              && h.indexOf('javascript') === -1 && h.indexOf('orderBy') === -1);
    // Prefer the real download link: .pdf, documentDownload or files path.
    const best = links.find((h) => h.toLowerCase().indexOf('.pdf') !== -1)
      || links.find((h) => h.indexOf('documentDownload') !== -1)
      || links.find((h) => h.indexOf('files') !== -1)
      || links[0] || '';
    let absolute = '';
    if (best) { try { absolute = new URL(best, location.href).href; } catch (e) { absolute = best; } }
    return { name: cells.join(' '), url: absolute };
  }).filter((d) => d.url);
  return { documents: docs };
})()
"""

# Deterministic Idox "Important Dates" parser: label/value pairs from the
# shared #simpleDetailsTable (th = label, td = value).
_IDOX_DATES_JS = """\
(() => {
  const table = document.querySelector('#simpleDetailsTable');
  if (!table) { return { found: false, dates: {} }; }
  const dates = {};
  for (const tr of Array.from(table.rows)) {
    const th = tr.querySelector('th');
    const td = tr.querySelector('td');
    if (!th || !td) continue;
    const label = (th.textContent || '').replace(/\\s+/g, ' ').trim();
    const value = (td.textContent || '').replace(/\\s+/g, ' ').trim();
    if (label) dates[label] = value;
  }
  return { found: true, dates: dates };
})()
"""

# Fetch one document from inside the session, same-origin with the portal.
_FETCH_JS_TEMPLATE = """\
(async () => {
  const target = new URL(__URL__, location.href).href;
  let resp;
  try {
    resp = await fetch(target, { credentials: 'include' });
  } catch (e) {
    return { error: 'network: ' + String(e) };
  }
  if (!resp.ok) { return { error: 'HTTP ' + resp.status }; }
  const buf = new Uint8Array(await resp.arrayBuffer());
  if (buf.length > __MAX_BYTES__) { return { error: 'too large: ' + buf.length + ' bytes' }; }
  let binary = '';
  for (let i = 0; i < buf.length; i += 0x8000) {
    binary += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
  }
  return { b64: btoa(binary) };
})()
"""


async def act_optional(driver: BrowserDriver, instruction: str) -> bool:
    """Act on a step that may not exist on every council's page.

    Returns True when the action succeeded; a failure is swallowed because
    'choose Planning if a category is shown' is meant to be conditional.
    """
    try:
        await driver.act(instruction)
        return True
    except DriverError:
        return False


async def click_with_fallback(
    driver: BrowserDriver, instruction: str, purpose: str
) -> None:
    """Click a required control: act() first, direct selectors as the net.

    Raises DriverError when both fail — the activity layer decides whether
    that is worth a retry.
    """
    try:
        await driver.act(instruction)
        return
    except DriverError:
        pass
    if not await driver.click_direct(purpose):
        raise DriverError(
            f"could not click the {purpose} control: act failed and no "
            f"known selector matched"
        )


def absolutise(url: str, base_url: str) -> str:
    """Resolve a possibly-relative link against the page it came from."""
    if not url:
        return ""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    from urllib.parse import urljoin

    return urljoin(base_url, url)


__all__: list[Any] = [
    "BrowserDriver",
    "DriverError",
    "StagehandDriver",
    "act_optional",
    "absolutise",
]
