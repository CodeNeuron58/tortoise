"""A narrow browser-driver protocol plus the real Stagehand implementation.

Tortoise's crawl logic (page flows, pagination, politeness) is written against
:class:`BrowserDriver` — four awaited calls that mirror Stagehand's act /
extract / observe vocabulary. Tests drive the logic with a fake; production
uses :class:`StagehandDriver`, so any Stagehand API drift is fixed in this one
file.
"""

from __future__ import annotations

import time
from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel
from stagehand import Page, Stagehand
from stagehand import local_browser

from .llm_adapter import OpenAICompatLLM

T = TypeVar("T", bound=BaseModel)


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

    async def close(self) -> None: ...


class StagehandDriver:
    """BrowserDriver backed by Stagehand v4 against one page."""

    def __init__(self, stagehand: Stagehand, page: Page) -> None:
        self._stagehand = stagehand
        self._page = page
        self._started = time.monotonic()

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
    ) -> "StagehandDriver":
        """Create one Browserbase session and connect Stagehand to it.

        SEAM: this is the one integration that cannot be exercised offline.
        It uses the documented Browserbase CDP pattern (create session, connect
        by websocket with the API key header); if Stagehand's Browserbase
        integration changes, fix it here only.
        """
        from browserbase import AsyncBrowserbase

        async with AsyncBrowserbase(api_key=api_key) as client:
            session = await client.sessions.create(project_id=project_id)
        browser = await local_browser.connect(
            cdp_url=f"wss://cdn.browserbase.com?sessionId={session.id}",
            headers={"x-bb-api-key": api_key},
        )
        # The callback is passed bare: Stagehand wraps it in its ClientLLM model itself.
        stagehand = await Stagehand.create(browser=browser, model=llm.generate)
        page = await cls._first_page(stagehand)
        return cls(stagehand, page)

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

    async def close(self) -> None:
        try:
            await self._stagehand.close()
        except Exception:
            # Closing must never mask the real outcome of a run.
            pass


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
