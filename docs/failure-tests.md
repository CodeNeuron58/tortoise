# Failure tests

Six things you break on purpose, and what must happen. Every test needs its
workflow history saved in `docs/evidence/` — that is the proof for the post.

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

All tests can run on **local Chrome** (`run-once` defaults to `--local`) —
they cost no Browserbase minutes, except test 2 which needs a real session.

## 1. Kill the worker during the crawl

```bash
tortoise run-once --council west-suffolk
# ...while the crawl is running: kill the worker process (Ctrl+C counts)
tortoise worker            # restart
```

**Expected:** the workflow resumes from the last heartbeated page; no
duplicate rows. Rows are upserted by `(council, reference)`, so re-crawling
pages cannot double-count them.
**Evidence:** `failure-test-1-crawl-resume.json`

## 2. End the Browserbase session mid-crawl

Run once with `--browserbase`, then end the session from the Browserbase
dashboard while the crawl is running.

**Expected:** the activity retries in a new session and resumes from the last
heartbeated page.
**Evidence:** `failure-test-2-session-death.json`

## 3. Make Ultramarine fail

```bash
TORTOISE_TEST_BRIEF_FAIL=1 tortoise run-once --council west-suffolk
```

**Expected:** the brief activity is retried three times with backoff
(`BriefTestFailure` is retryable on purpose), then the email says the brief
was skipped — with the reason and the run numbers.
**Evidence:** `failure-test-3-brief-retries.json`

## 4. Stop Ollama

Quit Ollama (or set `TORTOISE_TEST_OLLAMA_DOWN=1`, which points the filter at
a dead port), then:

```bash
tortoise run-once --council west-suffolk
```

**Expected:** three retries, then everything is kept and the run is marked
`unfiltered` (the `tortoise runs` output shows a `*`). Tortoise never drops
applications because its free filter was down — it says so instead.
**Evidence:** `failure-test-4-ollama-down.json`

## 5. Run the same week twice

```bash
tortoise run-once --council west-suffolk   # completes
tortoise run-once --council west-suffolk   # again
```

**Expected:** the second start is rejected — the workflow id
`watch-<council>-<week>` uses a reject-duplicate policy, and the storage
guard raises `WeekAlreadyRunError`. One email, not two.
**Evidence:** `failure-test-5-duplicate-rejected.json`

## 6. A slow council site

```bash
TORTOISE_TEST_CRAWL_DELAY_MS=180000 tortoise run-once --council west-suffolk
```

The flag replaces the 5–10 second politeness pause with a three-minute delay
before every page, so the first attempt blows its 12-minute budget while
heartbeating, and the retry resumes from the heartbeated page with less work
left and finishes.

**Expected:** timeouts and retries; the run still completes.
**Evidence:** `failure-test-6-slow-site.json`
