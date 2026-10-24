"""The Temporal activities: all the real work, one small step each.

Every activity is a chain link: a crash costs at most one step. Retry policies
are declared in :mod:`tortoise.workflow.weekly_watch` so they live next to the
workflow; error *types* here say what is retryable (``BriefError`` — transient)
and what is not (``BriefAuthError`` — a bad key will not fix itself).

Heartbeats: the crawl heartbeats the page number it finished; on a retry the
activity resumes from the next page (rows are already upserted, so nothing
duplicates — failure test 1). Gemma heartbeats after each application.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date

from temporalio import activity

from ..brief import BriefItem, BriefWriter, budget_document_texts
from ..config import AppConfig, env_required, env_str, load_config
from ..crawl.crawler import WeeklyListCrawler
from ..crawl.driver import StagehandDriver
from ..crawl.llm_adapter import OpenAICompatLLM
from ..gemma import RELEVANCE_KEEP_AT_OR_ABOVE, FilterParseError, GemmaFilter
from ..mailer import BrevoSender, build_email
from ..models import UNFILTERED_TYPE, Application, FilterResult, RunNumbers
from ..storage import Database, pdf_dir
from .messages import (
    ApplicationRow,
    BriefOutcome,
    BriefParams,
    CrawlListParams,
    CrawlListResult,
    CouncilSettings,
    EmailParams,
    EmailResult,
    FetchParams,
    FetchResult,
    FilterParams,
    FilterSummary,
)

logger = logging.getLogger(__name__)

TASK_QUEUE = "tortoise"

# Failure-test flags (see .env.example); never set in real runs.
TEST_BRIEF_FAIL = "TORTOISE_TEST_BRIEF_FAIL"
TEST_CRAWL_DELAY_MS = "TORTOISE_TEST_CRAWL_DELAY_MS"
TEST_OLLAMA_DOWN = "TORTOISE_TEST_OLLAMA_DOWN"

# Where the filter endpoint points when the Ollama-down flag is set: a port
# nothing listens on, so connection attempts fail fast.
_DEAD_PORT_BASE_URL = "http://127.0.0.1:9/v1"


class _Runtime:
    """Per-process cache of config and database (activities run in one worker)."""

    def __init__(self) -> None:
        self.config: AppConfig | None = None
        self.db: Database | None = None

    def get(self) -> tuple[AppConfig, Database]:
        if self.config is None:
            self.config = load_config()
        if self.db is None:
            assert self.config is not None
            self.db = Database(self.config.data_dir / "tortoise.db")
            self.db.init_schema()
        return self.config, self.db


_RUNTIME = _Runtime()


async def _apply_test_delay() -> None:
    """Failure test 6: make the council site appear slow."""
    delay_ms = int(env_str(TEST_CRAWL_DELAY_MS, "0") or 0)
    if delay_ms > 0:
        activity.logger.info("test flag %s set: delaying %d ms", TEST_CRAWL_DELAY_MS, delay_ms)
        await asyncio.sleep(delay_ms / 1000.0)


def _to_row(application: Application) -> ApplicationRow:
    return ApplicationRow(
        reference=application.reference,
        council=application.council,
        address=application.address,
        proposal=application.proposal,
        url=application.url,
        validated_on=application.validated_on.isoformat() if application.validated_on else "",
        comment_deadline=(
            application.comment_deadline.isoformat() if application.comment_deadline else ""
        ),
        postcode=application.postcode,
    )


def _to_application(row: ApplicationRow) -> Application:
    return Application(
        reference=row.reference,
        council=row.council,
        address=row.address,
        proposal=row.proposal,
        url=row.url,
        validated_on=date.fromisoformat(row.validated_on) if row.validated_on else None,
        comment_deadline=date.fromisoformat(row.comment_deadline) if row.comment_deadline else None,
        postcode=row.postcode,
    )


def _browser_choice(local_browser: bool) -> bool:
    """True when Tortoise should use local Chrome instead of Browserbase."""
    if local_browser:
        return True
    # No Browserbase credentials configured means local is the only option.
    return not (env_str("BROWSERBASE_API_KEY") and env_str("BROWSERBASE_PROJECT_ID"))


def _stagehand_llm() -> OpenAICompatLLM:
    return OpenAICompatLLM(
        base_url=env_required("STAGEHAND_BASE_URL", "driving Stagehand's browser actions"),
        api_key=env_str("STAGEHAND_API_KEY", "ollama") or "ollama",
        model=env_required("STAGEHAND_MODEL", "driving Stagehand's browser actions"),
    )


async def _open_driver(local_browser: bool) -> StagehandDriver:
    llm = _stagehand_llm()
    if _browser_choice(local_browser):
        return await StagehandDriver.create_local(llm)
    return await StagehandDriver.create_browserbase(
        llm,
        api_key=env_required("BROWSERBASE_API_KEY", "real browser runs"),
        project_id=env_required("BROWSERBASE_PROJECT_ID", "real browser runs"),
    )


# --- activities --------------------------------------------------------------


@activity.defn
async def get_council_settings(council_slug: str) -> CouncilSettings:
    """Fresh config values for the workflow (so config edits need no reinstall)."""
    config, _ = _RUNTIME.get()
    council = config.council(council_slug)
    return CouncilSettings(
        slug=council.slug,
        name=council.name,
        zone_label=council.zone.label,
        streets=list(council.zone.streets),
        postcode_sectors=list(council.zone.postcode_sectors),
    )


@activity.defn
async def check_not_already_run(params: tuple[str, str]) -> bool:
    """Dedup guard: True when this council+week already completed."""
    _, db = _RUNTIME.get()
    council, week = params
    return db.has_completed_run(council, week)


@activity.defn
async def crawl_list(params: CrawlListParams) -> CrawlListResult:
    """Crawl one council's weekly list, heartbeating every results page."""
    config, db = _RUNTIME.get()
    council = config.council(params.council)
    await _apply_test_delay()

    resume_page = 1
    info = activity.info()
    if info.heartbeat_details:
        resume_page = max(1, int(info.heartbeat_details[0]))
    if resume_page > 1:
        activity.logger.info("resuming crawl from page %d", resume_page)

    async def on_page(page_number: int) -> None:
        activity.heartbeat(page_number)

    driver = await _open_driver(params.local_browser)
    try:
        crawler = WeeklyListCrawler(driver, council, on_page=on_page, start_page=resume_page)
        outcome = await crawler.crawl_list()
    finally:
        await driver.close()

    for application in outcome.applications:
        db.upsert_application(application, params.week)
    stored = db.applications_for_week(council.slug, params.week)
    return CrawlListResult(
        applications=[_to_row(app) for app in stored],
        pages_read=outcome.pages_read,
        browser_seconds=outcome.browser_seconds,
    )


@activity.defn
async def fetch_application(params: FetchParams) -> FetchResult:
    """Open one application, record its deadline, download up to 3 documents."""
    config, db = _RUNTIME.get()
    council = config.council(params.council)
    stored = {app.reference: app for app in db.applications_for_week(council.slug, params.week)}
    if params.reference not in stored:
        raise KeyError(f"{params.reference} is not stored for {council.slug} {params.week}")
    application = stored[params.reference]

    driver = await _open_driver(params.local_browser)
    try:
        crawler = WeeklyListCrawler(driver, council)
        outcome = await crawler.fetch_application(
            application, pdf_dir(config.data_dir, council.slug, application.reference)
        )
    finally:
        await driver.close()

    if outcome.comment_deadline:
        db.set_comment_deadline(council.slug, application.reference, outcome.comment_deadline)
    for document in outcome.documents:
        db.save_document(document)
    return FetchResult(
        reference=application.reference,
        comment_deadline=(
            outcome.comment_deadline.isoformat() if outcome.comment_deadline else ""
        ),
        documents=len(outcome.documents),
        pages_read=outcome.pages_read,
        browser_seconds=outcome.browser_seconds,
    )


@activity.defn
async def gemma_filter(params: FilterParams) -> FilterSummary:
    """Gemma labels every in-zone application; kept results are stored."""
    config, db = _RUNTIME.get()
    council = config.council(params.council)
    model = env_required("GEMMA_MODEL", "the local Gemma filter")
    base_url = _DEAD_PORT_BASE_URL if env_str(TEST_OLLAMA_DOWN) else env_required(
        "OLLAMA_BASE_URL", "the local Gemma filter"
    )
    gemma = GemmaFilter(base_url=base_url, model=model)

    started = time.monotonic()
    kept: list[str] = []
    applications = db.applications_for_week(council.slug, params.week)
    for index, application in enumerate(applications, start=1):
        try:
            result = await gemma.filter_application(application, council.zone)
        except FilterParseError as exc:
            # The model replied garbage: fail open, keep the application.
            activity.logger.warning(
                "unparseable filter reply for %s: %s", application.reference, exc
            )
            result = FilterResult(
                reference=application.reference,
                type=UNFILTERED_TYPE,
                street_impact=(),
                relevance=RELEVANCE_KEEP_AT_OR_ABOVE,
                one_line="",
                model=model,
                raw=str(exc),
            )
        is_kept = result.relevance >= RELEVANCE_KEEP_AT_OR_ABOVE
        db.save_filter_result(result, council.slug, kept=is_kept)
        if is_kept:
            kept.append(application.reference)
        activity.heartbeat(index)

    return FilterSummary(
        kept_references=kept,
        unfiltered=False,
        gemma_seconds=time.monotonic() - started,
    )


@activity.defn
async def write_brief(params: BriefParams) -> BriefOutcome:
    """Ultramarine reads the kept applications' documents and writes the brief."""
    if env_str(TEST_BRIEF_FAIL):
        # Retryable on purpose: failure test 3 expects retries, then a skip.
        raise BriefTestFailure("test flag set: forced Ultramarine failure")

    config, db = _RUNTIME.get()
    council = config.council(params.council)
    kept_refs = set(params.kept_references)
    applications = [
        app
        for app in db.applications_for_week(council.slug, params.week)
        if app.reference in kept_refs
    ]

    model = env_required("ULTRAMARINE_MODEL", "writing the brief")
    writer = BriefWriter(
        base_url=env_required("ULTRAMARINE_BASE_URL", "writing the brief"),
        api_key=env_required("ULTRAMARINE_API_KEY", "writing the brief"),
        model=model,
    )

    items: list[BriefItem] = []
    for application in applications:
        documents = db.documents_for(council.slug, application.reference)
        items.append(BriefItem(application=application, documents=documents))

    # Trim document texts so the whole request stays inside the token budget.
    all_texts = [document.text for item in items for document in item.readable_documents]
    budgeted = budget_document_texts(all_texts)
    cursor = 0
    for item in items:
        for document in item.readable_documents:
            document.text = budgeted[cursor]
            cursor += 1

    result = await writer.write_brief(council.zone.label, items)
    db.log_cost(
        council.slug,
        params.week,
        call="brief",
        model=model,
        usage=result.usage,
        cost_usd=result.cost_usd_float,
    )
    return BriefOutcome(
        text=result.text,
        model=model,
        input_tokens=result.usage.input_tokens,
        cached_tokens=result.usage.cached_tokens,
        output_tokens=result.usage.output_tokens,
        cost_usd=result.cost_usd_float,
    )


class BriefTestFailure(Exception):
    """Retryable forced failure used by failure test 3."""


@activity.defn
async def send_email(params: EmailParams) -> EmailResult:
    """Send the weekly email; the brief may legitimately be None (skipped)."""
    content = build_email(
        zone_label=params.zone_label,
        numbers=params.numbers,
        brief_text=params.brief_text,
        earliest_deadline=(
            date.fromisoformat(params.earliest_deadline) if params.earliest_deadline else None
        ),
    )
    sender = BrevoSender(
        api_key=env_required("BREVO_API_KEY", "sending the email"),
        from_email=env_required("BREVO_FROM_EMAIL", "sending the email"),
        to_email=env_required("TORTOISE_TO_EMAIL", "sending the email"),
        from_name=env_str("BREVO_FROM_NAME", "Tortoise") or "Tortoise",
    )
    await sender.send(content)
    return EmailResult(subject=content.subject)


@activity.defn
async def record_run(numbers: RunNumbers) -> None:
    """Persist the run's Lazy Payoff numbers."""
    _, db = _RUNTIME.get()
    db.upsert_run(numbers)
