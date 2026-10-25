# Failure tests

Six things you break on purpose, and what must happen. Every test has its
evidence in `docs/evidence/`: an exported Temporal workflow history (test 5
is a client-side rejection, so its evidence is the CLI output instead).

Export a history after each test:

```bash
temporal workflow show --workflow-id <id> --output json \
  > docs/evidence/failure-test-<n>-<name>.json
```

Start the dev server and the worker first:

```bash
temporal server start-dev --db-filename data/temporal.db
tortoise worker            # in a second terminal
```

Run these only against a council you are allowed to crawl (see the
politeness rules in the README). Each test gets its own week label so the
duplicate guard does not reject it, e.g. `--week 2026-W44-t1`.

| # | What breaks | Evidence | Result (UTC) |
|---|---|---|---|
| 1 | Worker killed mid-crawl | `failure-test-1-worker-killed.json` | passed, 25 Oct 2026 |
| 2 | Browser session ended mid-crawl | `failure-test-2-session-ended.json` | passed, 25 Oct 2026 |
| 3 | Ultramarine fails | `failure-test-3-brief-retries.json` | passed, 25 Oct 2026 |
| 4 | Ollama is down | `failure-test-4-ollama-down.json` | passed, 25 Oct 2026 |
| 5 | Same week run twice | `failure-test-5-duplicate.txt` | passed, 25 Oct 2026 |
| 6 | Slow council site | `failure-test-6-slow-site.json` | passed, 25 Oct 2026 |

## 1. Kill the worker during the crawl

```bash
tortoise run-once --council <slug> --browserbase --week 2026-W44-t1
# ...after "crawled results page 2" appears in the worker log:
#    kill the worker process (hard kill, not Ctrl+C)
tortoise worker            # start a new one
```

**Expected:** the crawl's heartbeat stops, so after its 2-minute heartbeat
timeout Temporal retries `crawl_list` on the new worker, which reads the
last heartbeated page and resumes there (Idox page links carry the page
number, so it jumps straight to it). Each page's rows are stored *before*
the page is heartbeated, and rows are upserted by `(council, reference)`,
so nothing is lost and nothing duplicates.

**Seen (25 Oct 2026):** worker killed after page 2; the new worker logged
`resuming crawl from page 2`, crawled pages 2 and 3, and the run completed
with 23 application rows, 23 distinct. The history shows `crawl_list`
attempt 2 with last failure `activity Heartbeat timeout`.

## 2. End the Browserbase session mid-crawl

Run with `--browserbase`, then end the session while the crawl is running:
from the Browserbase dashboard, or through the API
(`sessions.update(id, status="REQUEST_RELEASE")`; the worker logs each
session id).

**Expected:** the next browser call fails, the activity is retried in a
new session, and the crawl resumes from the last heartbeated page.

**Seen (25 Oct 2026):** session ended after page 2 → `CDP connection
closed` → attempt 2 opened a new session, logged `resuming crawl from
page 2`, and the run completed.

## 3. Make Ultramarine fail

```bash
TORTOISE_TEST_BRIEF_FAIL=1 tortoise worker
tortoise run-once --council <slug> --browserbase --week 2026-W44-t3
```

**Expected:** `write_brief` is tried three times with backoff (the flag
raises a retryable error on purpose), then the email says the brief was
skipped, with the reason and the run's numbers. Bad-key and no-credit
errors (`BriefAuthError`) skip the retries and go straight to that email.

**Seen (25 Oct 2026):** the exported history is a real failure, not the
flag: Ultramarine returned an empty brief (its thinking used up a small
output cap). Three attempts, then the "brief was skipped" email. The flag's
path is pinned offline in `tests/test_workflow_activities.py`.

## 4. Stop Ollama

Quit Ollama, or start the worker with `TORTOISE_TEST_OLLAMA_DOWN=1` (which
points the filter at a dead port), then run once.

**Expected:** three attempts, then every in-zone application is kept and
the run is marked `unfiltered` (`tortoise runs` shows a `*`). Tortoise
never drops applications because its free filter was down.

**Seen (25 Oct 2026):** `gemma_filter` failed three times, both in-zone
applications were kept, and the run was recorded as unfiltered. (This run
predates the document-download fix, so its brief had no PDF text.)

## 5. Run the same week twice

```bash
tortoise run-once --council <slug> --browserbase --week 2026-W44   # completes
tortoise run-once --council <slug> --browserbase --week 2026-W44   # again
```

**Expected:** the second start is rejected: the workflow id
`watch-<council>-<week>` uses a reject-duplicate policy. Scheduled runs,
whose week is computed inside the workflow, are caught by a storage guard
(`WeekAlreadyRunError`). One email, not two.

**Seen (25 Oct 2026):** `error: Workflow execution already started`, and
one completed execution for the week.

## 6. A slow council site

```bash
TORTOISE_TEST_CRAWL_DELAY_MS=150000 tortoise worker
tortoise run-once --council <slug> --browserbase --week 2026-W44-slow
```

The flag stalls the crawl's *first* attempt for 150 seconds, before any
browser opens (so the stall costs no browser minutes): longer than the
crawl's 2-minute heartbeat timeout. It models a site that is slow, then
recovers.

**Expected:** Temporal times the first attempt out and cancels it; the
retry crawls at normal speed and the run finishes.

**Seen (25 Oct 2026):** the first attempt was cancelled at 2:00 with
`activity Heartbeat timeout`; attempt 2 crawled 3 pages and the run
completed.
