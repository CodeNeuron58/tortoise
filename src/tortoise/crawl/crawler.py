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

from ..extract import extract_text
from ..models import Application, CrawlMode, Council, ExtractedDocument, parse_uk_date
from .documents import download_pdf_async, safe_filename, select_documents, url_filename
from .driver import BrowserDriver, DriverError, absolutise, act_optional
from .schemas import ApplicationSummary, DocumentsList, WeeklyListPage

logger = logging.getLogger(__name__)

# A runaway pagination guard: a weekly list is never hundreds of pages long.
MAX_LIST_PAGES = 40

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
        if self._pause_range[1] > 0:
            await asyncio.sleep(random.uniform(*self._pause_range))

    # --- weekly list ------------------------------------------------------

    async def crawl_list(self) -> CrawlListOutcome:
        if self._council.mode is CrawlMode.IDOX_WEEKLY_LIST:
            return await self._crawl_idox()
        return await self._crawl_list_page()

    async def _crawl_idox(self) -> CrawlListOutcome:
        council = self._council
        await self._driver.goto(council.search_url)

        # Conditional steps: council pages differ, so failures are fine here.
        await act_optional(self._driver, "if a search-type choice is shown, choose 'Planning'")
        await act_optional(
            self._driver, "in the Weekly or Monthly Lists form, make sure 'Weekly' is selected"
        )
        await act_optional(self._driver, "set the list period to last week")
        await act_optional(
            self._driver, "if asked which applications to show, choose the validated applications"
        )
        await self._driver.act("click the Search button")

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
            await self._driver.act("click the Next page button")

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
            await self._driver.act("click the Next page button")
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
        reference = (row.reference or "").strip()
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
            address=(row.address or "").strip(),
            proposal=(row.proposal or "").strip(),
            url=absolutise((row.url or "").strip(), base),
            validated_on=parse_uk_date(row.validated_on),
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

        summary = await self._driver.extract(
            "Extract the date by which comments must be made about this planning application "
            "(often labelled 'comments by' or under important dates). Leave it empty if the "
            "page does not show one.",
            ApplicationSummary,
        )
        outcome.comment_deadline = parse_uk_date(summary.comment_deadline)

        # The documents live on their own tab on Idox sites; on other layouts
        # they are already on the page, and a failed tab click is fine.
        await act_optional(self._driver, "click the Documents tab if the page has one")
        docs = await self._driver.extract(
            "Extract every document in this application's documents list: its name and its "
            "link. Do not follow the links.",
            DocumentsList,
        )
        outcome.pages_read += 1

        chosen = select_documents(docs.documents)
        for link in chosen:
            filename = safe_filename(link.name or url_filename(link.url))
            try:
                path = await download_pdf_async(link.url, dest_dir, filename)
            except Exception as exc:  # noqa: BLE001 — one bad document must not sink the run
                logger.warning("%s: skipping document %s: %s", application.reference, filename, exc)
                continue
            extracted = extract_text(path)
            outcome.documents.append(
                ExtractedDocument(
                    council=application.council,
                    reference=application.reference,
                    filename=filename,
                    url=link.url,
                    path=str(path),
                    pages=extracted.pages,
                    text=extracted.text,
                )
            )
        outcome.browser_seconds = self._driver.seconds_in_browser
        return outcome


__all__ = [
    "CrawlListOutcome",
    "FetchOutcome",
    "MAX_LIST_PAGES",
    "WeeklyListCrawler",
]
