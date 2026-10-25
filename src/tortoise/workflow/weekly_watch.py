"""WeeklyWatch: the one Temporal workflow that runs Tortoise each week.

A chain of small activities (a crash costs at most one page of work), with the
zone filter and the bookkeeping done inline as plain deterministic code:

1. ``crawl_list`` — one browser session, heartbeats per results page, up to 3
   retries, each resuming from the last heartbeated page.
2. Zone filter in the workflow itself — plain code, before any browser minute
   or token is spent on an application.
3. ``fetch_application`` per kept application, followed by a durable
   ``workflow.sleep(8)`` — the politeness pause that survives crashes.
4. ``gemma_filter`` — if Ollama stays down after 3 attempts, everything is
   kept and the run is marked "unfiltered" (never silently drop).
5. ``write_brief`` — retried with backoff; a missing-credit or bad-key error
   is not retried, and the email says the brief was skipped.
6. ``send_email``, then the run's numbers are recorded.

Duplicate runs of the same week are rejected: manual runs use a workflow id of
``watch-<council>-<week>`` with a reject-duplicate policy, and a storage guard
catches scheduled runs whose week is computed inside the workflow.

This module imports only pure code (messages, models, zone): the workflow
sandbox must never see an SDK.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

from ..models import RunNumbers, WatchZone
from ..zone import in_zone
from .messages import (
    ApplicationRow,
    BriefOutcome,
    CouncilSettings,
    CrawlListParams,
    CrawlListResult,
    EmailParams,
    FetchParams,
    FetchResult,
    FilterParams,
    FilterSummary,
    BriefParams,
    WeeklyWatchInput,
    rebuild,
)

# Activity names, as registered by the worker (the activities module is never
# imported here — the sandbox must not see it).
GET_COUNCIL_SETTINGS = "get_council_settings"
CHECK_NOT_ALREADY_RUN = "check_not_already_run"
CRAWL_LIST = "crawl_list"
FETCH_APPLICATION = "fetch_application"
GEMMA_FILTER = "gemma_filter"
WRITE_BRIEF = "write_brief"
SEND_EMAIL = "send_email"
RECORD_RUN = "record_run"

# The plan's politeness pause between applications, durable across crashes.
INTER_APPLICATION_PAUSE = timedelta(seconds=8)


def _default_retry(maximum_attempts: int = 3) -> RetryPolicy:
    return RetryPolicy(
        maximum_attempts=maximum_attempts,
        initial_interval=timedelta(seconds=15),
        backoff_coefficient=2.0,
        maximum_interval=timedelta(minutes=2),
    )


class WeekAlreadyRunError(Exception):
    """A completed run for this council and week already exists."""


@workflow.defn
class WeeklyWatch:
    """One council, one week, one brief, one email."""

    @workflow.run
    async def run(self, params: WeeklyWatchInput) -> RunNumbers:
        workflow.logger.info("weekly watch starting for %s", params.council)
        week = params.week or _current_week()
        numbers = RunNumbers(council=params.council, week=week)

        settings = rebuild(
            CouncilSettings,
            await workflow.execute_activity(
                GET_COUNCIL_SETTINGS,
                params.council,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_default_retry(),
            ),
        )
        already = await workflow.execute_activity(
            CHECK_NOT_ALREADY_RUN,
            (params.council, week),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_default_retry(),
        )
        if already:
            raise WeekAlreadyRunError(
                f"{params.council} week {week} already ran to completion; "
                "starting it again would send a duplicate email"
            )

        # Record the run as started, for observability, then crawl.
        await _record(numbers)

        # 1. Crawl the weekly list.
        crawl = rebuild(
            CrawlListResult,
            await workflow.execute_activity(
                CRAWL_LIST,
                CrawlListParams(council=params.council, week=week, local_browser=params.local_browser),
                start_to_close_timeout=timedelta(minutes=12),  # under Browserbase's 15-minute session cap
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=_default_retry(),
            ),
        )
        numbers.listed = len(crawl.applications)
        numbers.pages_read += crawl.pages_read
        numbers.browser_seconds += crawl.browser_seconds

        # 2. Zone filter: plain code.
        zone = WatchZone(
            label=settings.zone_label,
            streets=tuple(settings.streets),
            postcode_sectors=tuple(settings.postcode_sectors),
        )
        in_zone_rows = [
            row
            for row in crawl.applications
            if in_zone(row.address, zone, row.postcode)
        ]

        # 3. Fetch each kept application, politely.
        deadlines: list[str] = []
        for row in in_zone_rows:
            fetch = rebuild(
                FetchResult,
                await workflow.execute_activity(
                    FETCH_APPLICATION,
                    FetchParams(
                        council=params.council,
                        week=week,
                        reference=row.reference,
                        local_browser=params.local_browser,
                    ),
                    start_to_close_timeout=timedelta(minutes=6),
                    retry_policy=_default_retry(),
                ),
            )
            numbers.pdfs_read += fetch.documents
            numbers.pages_read += fetch.pages_read
            numbers.browser_seconds += fetch.browser_seconds
            if fetch.comment_deadline:
                deadlines.append(fetch.comment_deadline)
            await workflow.sleep(INTER_APPLICATION_PAUSE)
        numbers.kept = len(in_zone_rows)

        if not in_zone_rows:
            numbers.status = "completed"
            await _record(numbers)
            return numbers

        # 4. Gemma's free filter. If Ollama stays down after 3 attempts, keep
        # everything and mark the run "unfiltered" instead of dropping apps.
        kept_references = [row.reference for row in in_zone_rows]
        try:
            filter_summary = rebuild(
                FilterSummary,
                await workflow.execute_activity(
                    GEMMA_FILTER,
                    FilterParams(council=params.council, week=week),
                    start_to_close_timeout=timedelta(minutes=45),
                    heartbeat_timeout=timedelta(minutes=5),
                    retry_policy=_default_retry(),
                ),
            )
            numbers.gemma_seconds = filter_summary.gemma_seconds
            kept_references = filter_summary.kept_references
        except ActivityError:
            workflow.logger.warning("Gemma stayed down; keeping everything as 'unfiltered'")
            numbers.unfiltered = True
        numbers.kept = len(kept_references)

        # 5. The one paid step. Auth failures are not retried; anything else
        # retries, and after the limit the email says the brief was skipped.
        brief_text: str | None = None
        try:
            if kept_references:
                outcome = rebuild(
                    BriefOutcome,
                    await workflow.execute_activity(
                        WRITE_BRIEF,
                        BriefParams(
                            council=params.council, week=week, kept_references=kept_references
                        ),
                        start_to_close_timeout=timedelta(minutes=10),
                        retry_policy=RetryPolicy(
                            maximum_attempts=3,
                            initial_interval=timedelta(seconds=20),
                            backoff_coefficient=2.0,
                            maximum_interval=timedelta(minutes=5),
                            non_retryable_error_types=["BriefAuthError"],
                        ),
                    ),
                )
                brief_text = outcome.text
                numbers.input_tokens = outcome.input_tokens
                numbers.cached_tokens = outcome.cached_tokens
                numbers.output_tokens = outcome.output_tokens
                numbers.cost_usd = outcome.cost_usd
            else:
                workflow.logger.info("the filter kept nothing this week")
        except ActivityError as err:
            numbers.brief_skipped_reason = _error_message(err)
            workflow.logger.warning("brief skipped: %s", numbers.brief_skipped_reason)

        # 6. The email, then the numbers.
        earliest_deadline = min(deadlines) if deadlines else None
        await workflow.execute_activity(
            SEND_EMAIL,
            EmailParams(
                council=params.council,
                week=week,
                zone_label=settings.zone_label,
                numbers=numbers,
                brief_text=brief_text,
                earliest_deadline=earliest_deadline,
            ),
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(
                maximum_attempts=3,
                non_retryable_error_types=["MailAuthError"],
            ),
        )

        numbers.status = "completed"
        await _record(numbers)
        return numbers


async def _record(numbers: RunNumbers) -> None:
    await workflow.execute_activity(
        RECORD_RUN,
        numbers,
        start_to_close_timeout=timedelta(seconds=30),
        retry_policy=_default_retry(),
    )


def _error_message(err: ActivityError) -> str:
    """A one-line reason for the email, from the deepest error we can reach."""
    cause = err.cause
    depth = 0
    while cause is not None and getattr(cause, "cause", None) is not None and depth < 5:
        cause = cause.cause
        depth += 1
    message = str(cause or err)
    return message.split("\n")[0][:300]


def _current_week() -> str:
    """The ISO week of the workflow's (deterministic) current time."""
    iso = workflow.utcnow().isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"
