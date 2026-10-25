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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from ..config import env_str
from ..extract import extract_text
from ..models import Application, CrawlMode, Council, ExtractedDocument, parse_uk_date
from .documents import download_pdf_async, safe_filename, select_documents, url_filename
from .driver import BrowserDriver, DriverError, absolutise, act_optional, click_with_fallback
from .schemas import ApplicationSummary, DocumentsList, WeeklyListPage

logger = logging.getLogger(__name__)

# A runaway pagination guard: a weekly list is never hundreds of pages long.
MAX_LIST_PAGES = 40

# Failure-test flag: replaces the politeness pause with a long delay.
TEST_CRAWL_DELAY_MS = "TORTOISE_TEST_CRAWL_DELAY_MS"

T = TypeVar("T", bound=BaseModel)

PageCallback = Callable[[int], Awaitable[None]]


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

    async def _pause(self) -> None:
        # Test hook (failure test 6): TORTOISE_TEST_CRAWL_DELAY_MS replaces
        # the politeness pause with a long, artificial delay so timeouts and
        # resume behaviour can be exercised offline.
        delay_ms = int(env_str(TEST_CRAWL_DELAY_MS, "0") or 0)
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)
            return
        if self._pause_range[1] > 0:
            await asyncio.sleep(random.uniform(*self._pause_range))

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

        outcome = CrawlListOutcome()
        seen: set[str] = set()
        page_number = 1
        while page_number <= MAX_LIST_PAGES:
            if page_number > 1:
                await self._pause()
            data = await self._driver.extract_idox_rows()
            rows = data.get("rows", []) if isinstance(data, dict) else []
            for row_ in rows:
                application = self._row_to_application(row_)
                if application and application.reference not in seen:
                    seen.add(application.reference)
                    outcome.applications.append(application)
            outcome.pages_read += 1
            if self._on_page is not None:
                await self._on_page(page_number)
            next_href = data.get("next_href") if isinstance(data, dict) else None
            if not next_href:
                break
            await self._driver.goto(absolutise(next_href, council.search_url))
            page_number += 1

        if not outcome.applications:
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
        outcome = CrawlListOutcome()
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
        """Open one application, note its deadline, download up to 3 text documents."""
        outcome = FetchOutcome()
        await self._driver.goto(application.url)
        outcome.pages_read += 1

        # Idox detail pages take the tab in the URL: summary first (dates),
        # then the documents list, without any tab-clicking.
        documents_url = application.url
        if "activeTab=summary" in application.url:
            documents_url = application.url.replace("activeTab=summary", "activeTab=documents")

        summary = await self._driver.extract(
            "Extract the date by which comments must be made about this planning application "
            "(often labelled 'comments by' or under important dates). Leave it empty if the "
            "page does not show one.",
            ApplicationSummary,
        )
        outcome.comment_deadline = parse_uk_date(summary.comment_deadline)

        await self._driver.goto(documents_url)
        outcome.pages_read += 1
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
        except DriverError:
            logger.warning("deterministic documents parse failed; trying the LLM")
        if not docs.documents:
            docs = await self._driver.extract(
                "Extract every document in this application's documents list: its name and its "
                "link. Do not follow the links.",
                DocumentsList,
            )

        chosen = select_documents(docs.documents)
        for link in chosen:
            filename = safe_filename(link.name or url_filename(link.url))
            destination = dest_dir / filename
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
            extracted = extract_text(destination)
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

    async def _fetch_document_bytes(self, url: str) -> bytes:
        """Fetch a document through the session when possible (geo-blocked
        portals reject direct local HTTP), falling back to direct download."""
        fetcher = getattr(self._driver, "fetch_pdf", None)
        if fetcher is not None:
            try:
                return await fetcher(url)
            except DriverError:
                logger.warning("session fetch failed; trying direct download")
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
