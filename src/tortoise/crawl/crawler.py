"""The crawl flows: weekly lists and application details, per council mode.

Written against the :class:`~tortoise.crawl.driver.BrowserDriver` protocol so
the logic is unit-testable offline with a fake driver; the real driver is
:class:`~tortoise.crawl.driver.StagehandDriver`. Plain-English actions make one
flow survive small layout differences between councils, per the plan.

Politeness lives here too: one page at a time with a 5–10 second pause between
requests (configurable down to zero in tests). The durable 8-second pause
*between applications* is the workflow's job — it must survive crashes.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from ..extract import ExtractionError, extract_text
from ..models import Application, CrawlMode, Council, ExtractedDocument, parse_uk_date
from .documents import download_pdf_async, safe_filename, select_documents, url_filename
from .driver import BrowserDriver, DriverError, absolutise, act_optional, click_with_fallback
from .schemas import ApplicationSummary, DocumentsList, WeeklyListPage

logger = logging.getLogger(__name__)

# A runaway pagination guard: a weekly list is never hundreds of pages long.
MAX_LIST_PAGES = 40

T = TypeVar("T", bound=BaseModel)

PageCallback = Callable[[int], Awaitable[None]]

# Which Important Dates row is "the comment deadline", in order. The
# neighbour consultation expiry is the date residents' comments are due;
# without one (tree works, adverts) a site notice or press advert expiry
# stands in, then the general consultation expiry.
_DEADLINE_LABELS: tuple[str, ...] = (
    "neighbour consultation expiry",
    "site notice expiry",
    "advertisement expiry",
    "consultation expiry",
    "comments by",
    "comment by",
)


def comment_deadline_from_dates(dates: dict[str, str]) -> date | None:
    """Pick the comment deadline out of an Idox Important Dates table.

    Labels are matched case-insensitively by containment, first rule wins;
    "Not Available" and other unparseable values are skipped.
    """
    for wanted in _DEADLINE_LABELS:
        for label, value in dates.items():
            if wanted in label.lower():
                parsed = parse_uk_date(value)
                if parsed is not None:
                    return parsed
    return None


def idox_tab_urls(url: str) -> dict[str, str] | None:
    """The dates and documents tab URLs of an Idox application, or None.

    Idox detail pages are ``applicationDetails.do?keyVal=...&activeTab=...``;
    any other URL is not Idox and gets the single-page treatment.
    """
    if "applicationDetails.do" not in url:
        return None

    def tab(name: str) -> str:
        if "activeTab=" in url:
            return re.sub(r"activeTab=[A-Za-z]+", f"activeTab={name}", url)
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}activeTab={name}"

    return {"dates": tab("dates"), "documents": tab("documents")}


def _idox_page_href(next_href: str, page: int) -> str | None:
    """An Idox results link rewritten to jump to ``page``, if it carries one."""
    if not re.search(r"searchCriteria\.page=\d+", next_href):
        return None
    return re.sub(r"searchCriteria\.page=\d+", f"searchCriteria.page={page}", next_href)


@dataclass(slots=True)
class CrawlListOutcome:
    """Everything one weekly-list crawl produced."""

    applications: list[Application] = field(default_factory=list)
    pages_read: int = 0
    browser_seconds: float = 0.0


@dataclass(slots=True)
class FetchOutcome:
    """Everything one application-detail fetch produced."""

    comment_deadline: date | None = None
    documents: list[ExtractedDocument] = field(default_factory=list)
    pages_read: int = 0
    browser_seconds: float = 0.0


class WeeklyListCrawler:
    """Crawls one council: the weekly list, then individual applications."""

    def __init__(
        self,
        driver: BrowserDriver,
        council: Council,
        on_page: PageCallback | None = None,
        pause_range: tuple[float, float] = (5.0, 10.0),
        start_page: int = 1,
    ) -> None:
        self._driver = driver
        self._council = council
        self._on_page = on_page
        self._pause_range = pause_range
        self._start_page = max(1, start_page)
        self._outcome = CrawlListOutcome()

    async def _pause(self) -> None:
        # The slow-site failure test (6) stalls the crawl activity itself,
        # before a browser opens; see tortoise.workflow.activities.
        if self._pause_range[1] > 0:
            await asyncio.sleep(random.uniform(*self._pause_range))

    def applications_so_far(self) -> list[Application]:
        """Rows parsed so far in this crawl (stored before each heartbeat)."""
        return list(self._outcome.applications)

    # --- weekly list ------------------------------------------------------

    async def crawl_list(self) -> CrawlListOutcome:
        if self._council.mode is CrawlMode.IDOX_WEEKLY_LIST:
            return await self._crawl_idox()
        return await self._crawl_list_page()

    async def _crawl_idox(self) -> CrawlListOutcome:
        """Crawl an Idox weekly list: submit the form, parse the DOM, page Next.

        The form drive and row parsing are deterministic (direct selectors —
        every Idox council shares this DOM); the LLM is not involved, so this
        step is free and exact. Falls back to the LLM flow only if the
        deterministic parse finds nothing at all (a council with a divergent
        layout).
        """
        council = self._council
        await self._driver.goto(council.search_url)
        await self._driver.submit_idox_weekly_form()

        outcome = self._outcome = CrawlListOutcome()
        seen: set[str] = set()
        page_number = 1
        while page_number <= MAX_LIST_PAGES:
            if page_number > 1:
                await self._pause()
            data = await self._driver.extract_idox_rows()
            next_href = data.get("next_href") if isinstance(data, dict) else None
            if page_number < self._start_page and next_href:
                # Resuming a retry: the earlier pages' rows were stored by the
                # attempt that heartbeated them, so go straight to the resume
                # page (Idox page links carry the page number) — or walk one
                # page at a time when the link has no number to jump with.
                outcome.pages_read += 1
                jump = _idox_page_href(next_href, self._start_page)
                await self._driver.goto(absolutise(jump or next_href, council.search_url))
                page_number = self._start_page if jump else page_number + 1
                continue
            rows = data.get("rows", []) if isinstance(data, dict) else []
            for row_ in rows:
                application = self._row_to_application(row_)
                if application and application.reference not in seen:
                    seen.add(application.reference)
                    outcome.applications.append(application)
            outcome.pages_read += 1
            if self._on_page is not None:
                await self._on_page(page_number)
            if not next_href:
                break
            await self._driver.goto(absolutise(next_href, council.search_url))
            page_number += 1

        if not outcome.applications and self._start_page == 1:
            workflow_note = "no rows parsed; falling back to the LLM flow"
            logger.warning("%s: %s", council.slug, workflow_note)
            return await self._crawl_idox_llm()

        outcome.browser_seconds = self._driver.seconds_in_browser
        logger.info(
            "%s: %d applications over %d pages (deterministic)",
            council.slug,
            len(outcome.applications),
            outcome.pages_read,
        )
        return outcome

    async def _crawl_idox_llm(self) -> CrawlListOutcome:
        """The LLM-driven Idox flow — the fallback for divergent layouts."""
        await act_optional(
            self._driver, "if a search-type choice is shown, choose 'Planning'"
        )
        await act_optional(
            self._driver, "in the Weekly or Monthly Lists form, make sure 'Weekly' is selected"
        )
        await act_optional(self._driver, "set the list period to last week")
        await act_optional(
            self._driver, "if asked which applications to show, choose the validated applications"
        )
        await click_with_fallback(self._driver, "click the Search button", "search")
        return await self._paginate()

    async def _crawl_list_page(self) -> CrawlListOutcome:
        await self._driver.goto(self._council.weekly_list_url)
        return await self._paginate()

    async def _paginate(self) -> CrawlListOutcome:
        outcome = self._outcome = CrawlListOutcome()
        seen: set[str] = set()

        # Resuming a retry: skip forward past pages already heartbeated by
        # clicking Next without extracting (their rows are already stored).
        for _ in range(1, self._start_page):
            await self._pause()
            await click_with_fallback(self._driver, "click the Next page button", "next")

        for page_number in range(self._start_page, MAX_LIST_PAGES + 1):
            if page_number > self._start_page:
                await self._pause()
            page = await self._driver.extract(
                "Extract every planning application row in the results list: its reference, "
                "address, proposal text, validated date exactly as shown, and the link to the "
                "application's details page. Also say whether there is a next page.",
                WeeklyListPage,
            )
            outcome.pages_read += 1
            for row_ in page.rows:
                application = self._row_to_application(row_)
                if application and application.reference not in seen:
                    seen.add(application.reference)
                    outcome.applications.append(application)
            if self._on_page is not None:
                await self._on_page(page_number)
            if not page.has_next_page:
                break
            await click_with_fallback(self._driver, "click the Next page button", "next")
        outcome.browser_seconds = self._driver.seconds_in_browser
        logger.info(
            "%s: %d applications over %d pages (from page %d)",
            self._council.slug,
            len(outcome.applications),
            outcome.pages_read,
            self._start_page,
        )
        return outcome

    def _row_to_application(self, row) -> Application | None:
        """Build an Application from a parsed row — dict or object, either way."""
        if isinstance(row, dict):
            reference = str(row.get("reference") or "").strip()
            address = str(row.get("address") or "").strip()
            proposal = str(row.get("proposal") or "").strip()
            url = str(row.get("url") or "").strip()
            validated_raw = str(row.get("validated_on") or "").strip()
        else:
            reference = (row.reference or "").strip()
            address = (row.address or "").strip()
            proposal = (row.proposal or "").strip()
            url = (row.url or "").strip()
            validated_raw = (row.validated_on or "").strip()
        if not reference:
            return None
        base = (
            self._council.search_url
            if self._council.mode is CrawlMode.IDOX_WEEKLY_LIST
            else self._council.weekly_list_url
        )
        return Application(
            reference=reference,
            council=self._council.slug,
            address=address,
            proposal=proposal,
            url=absolutise(url, base),
            validated_on=parse_uk_date(validated_raw),
            postcode="",
        )

    # --- one application --------------------------------------------------

    async def fetch_application(
        self, application: Application, dest_dir: Path
    ) -> FetchOutcome:
        """Open one application, note its deadline, download up to 3 text documents.

        Idox pages take the tab in the URL, so this reads the Important Dates
        tab (where the consultation expiry dates live) and then the documents
        tab, one polite request at a time. The details and contacts tabs —
        which carry applicants' names — are never opened.
        """
        outcome = FetchOutcome()
        tabs = idox_tab_urls(application.url)

        if tabs is not None:
            await self._driver.goto(tabs["dates"])
            outcome.pages_read += 1
            outcome.comment_deadline = await self._read_idox_deadline()
            await self._pause()
            documents_url = tabs["documents"]
            await self._driver.goto(documents_url)
            outcome.pages_read += 1
        else:
            # A council outside Idox: one page carries both.
            documents_url = application.url
            await self._driver.goto(documents_url)
            outcome.pages_read += 1
            outcome.comment_deadline = await self._extract_deadline_with_llm()

        # Deterministic parse first; the async-populating table defeated the
        # LLM extractor more often than not. LLM only as fallback.
        docs = DocumentsList(documents=[])
        try:
            parsed = await self._driver.extract_idox_documents()
            from .schemas import DocumentLink as _Link

            docs = DocumentsList(
                documents=[
                    _Link(name=d.get("name", ""), url=absolutise(d.get("url", ""), documents_url))
                    for d in (parsed.get("documents", []) if isinstance(parsed, dict) else [])
                    if d.get("url")
                ]
            )
        except DriverError as exc:
            logger.warning("deterministic documents parse failed (%s); trying the LLM", exc)
        if not docs.documents:
            docs = await self._driver.extract(
                "Extract every document in this application's documents list: its name and its "
                "link. Do not follow the links.",
                DocumentsList,
            )

        chosen = select_documents(docs.documents)
        for link in chosen:
            filename = safe_filename(link.name or url_filename(link.url))
            if not filename.lower().endswith(".pdf"):
                filename += ".pdf"
            destination = dest_dir / filename
            await self._pause()
            try:
                data = await self._fetch_document_bytes(link.url)
            except Exception as exc:  # noqa: BLE001 — one bad document must not sink the run
                logger.warning("%s: skipping document %s: %s", application.reference, filename, exc)
                continue
            if not data.startswith(b"%PDF"):
                logger.warning("%s: %s is not a PDF; skipping", application.reference, filename)
                continue
            dest_dir.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            try:
                extracted = extract_text(destination)
            except ExtractionError as exc:
                logger.warning("%s: %s", application.reference, exc)
                continue
            logger.info(
                "%s: read %s (%d pages, %d characters)",
                application.reference,
                filename,
                extracted.pages,
                len(extracted.text),
            )
            outcome.documents.append(
                ExtractedDocument(
                    council=application.council,
                    reference=application.reference,
                    filename=filename,
                    url=link.url,
                    path=str(destination),
                    pages=extracted.pages,
                    text=extracted.text,
                )
            )
        outcome.browser_seconds = self._driver.seconds_in_browser
        return outcome

    async def _read_idox_deadline(self) -> date | None:
        """The comment deadline from the Important Dates tab, or None."""
        try:
            parsed = await self._driver.extract_idox_dates()
        except DriverError as exc:
            logger.warning("important dates parse failed (%s); trying the LLM", exc)
            parsed = None
        if isinstance(parsed, dict) and parsed.get("found"):
            # The table is there: an empty deadline is the honest answer
            # (dates not set yet), not a reason to spend LLM tokens.
            return comment_deadline_from_dates(parsed.get("dates") or {})
        return await self._extract_deadline_with_llm()

    async def _extract_deadline_with_llm(self) -> date | None:
        summary = await self._driver.extract(
            "Extract the date by which comments must be made about this planning application "
            "(often labelled 'comments by', or a consultation expiry date under important "
            "dates). Leave it empty if the page does not show one.",
            ApplicationSummary,
        )
        return parse_uk_date(summary.comment_deadline)

    async def _fetch_document_bytes(self, url: str) -> bytes:
        """Fetch a document through the session when possible (geo-blocked
        portals reject direct local HTTP), falling back to direct download."""
        fetcher = getattr(self._driver, "fetch_pdf", None)
        if fetcher is not None:
            try:
                return await fetcher(url)
            except DriverError as exc:
                logger.warning("session fetch failed (%s); trying direct download", exc)
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = await download_pdf_async(url, Path(tmp), "doc.pdf")
            return Path(path).read_bytes()


__all__ = [
    "CrawlListOutcome",
    "FetchOutcome",
    "MAX_LIST_PAGES",
    "WeeklyListCrawler",
]
