# Tortoise

A slow, steady weekly agent that crawls a UK council's planning website, reads
every new application's PDFs, and emails you a one-page brief: what is being
built near your street, and the date by which you can object. You never open a
60-page planning document again.

Built for Hacktoberfest Week 3 — "Slow and Steady: build something that rewards
laziness". Tortoise crawls politely, one page at a time, overnight, so you can
be lazy.

## How it works

Every Monday morning (UK time), one Temporal workflow per council:

1. **Crawl** — Stagehand drives the council's weekly planning list in plain
   English ("choose Weekly List, last week, validated applications, Search"),
   one page at a time, heartbeating each page so a crash resumes instead of
   restarting.
2. **Filter by place** — plain Python: street names and postcode sectors from
   your watch zone. This runs *before* any browser minute or model token is
   spent on an application.
3. **Fetch** — for each in-zone application: the comment deadline, plus up to
   three text documents (application form, design and access statement,
   planning statement), downloaded over plain HTTP. Drawings and scans are
   skipped. A durable 8-second pause separates applications.
4. **Filter by relevance** — Gemma runs locally via Ollama and labels every
   application for free. If Ollama is down, everything is kept and marked
   "unfiltered" — Tortoise never silently drops applications.
5. **Brief** — Ultramarine 1 Flash reads only the kept documents and writes
   the brief. The one paid step, a few cents, counted to the last token.
6. **Email** — Brevo sends the brief with the run's numbers and exact cost in
   the footer. If the brief was skipped (bad key, no credit), the email says
   so instead of going silent.

```
Temporal workflow (WeeklyWatch, one per council per week)
  └─ crawl_list ──▶ zone filter ──▶ fetch_application (+8s) per app
        └─▶ gemma_filter (local, free) ──▶ write_brief (paid, counted)
              └─▶ send_email ──▶ record_run (the Lazy Payoff numbers)
```

## The politeness rules

Tortoise is a guest on council websites:

- One page at a time, with a 5–10 second pause between requests (the pause
  between applications is a durable `workflow.sleep`).
- Only last week's list, only applications in your watch zone, at most three
  documents each.
- `robots.txt` and the site terms are checked before a council is enabled —
  if crawling is not allowed, that council stays disabled.
- Never logs in, never comments, never submits anything.
- Does not store or publish applicants' names. The `data/` directory (database
  and downloaded PDFs) is gitignored for the same reason.

## Setup

Python 3.11+.

```bash
python -m pip install -e ".[dev]"
cp .env.example .env        # fill in what you have; see the table below
```

| Variable | What for | Where to get it |
|---|---|---|
| `BROWSERBASE_API_KEY` / `BROWSERBASE_PROJECT_ID` | real browser runs (free plan: 1 browser hour/month) | [browserbase.com](https://www.browserbase.com) |
| `STAGEHAND_BASE_URL` / `STAGEHAND_MODEL` | the model driving Stagehand (defaults to local Ollama, free) | — |
| `OLLAMA_BASE_URL` / `GEMMA_MODEL` | the free local filter | [ollama.com](https://ollama.com); `ollama pull gemma4:e4b` |
| `ULTRAMARINE_BASE_URL` / `ULTRAMARINE_API_KEY` / `ULTRAMARINE_MODEL` | writes the brief ($0.15/M in, $0.03/M cached, $0.50/M out) | [Paper Instruments docs](https://docs.paperinstruments.com/docs); Hacktoberfest gives $5 free credit |
| `BREVO_API_KEY`, `BREVO_FROM_EMAIL`, `TORTOISE_TO_EMAIL` | sends the email (free plan) | [brevo.com](https://www.brevo.com) |

Then:

```bash
# 1. Edit config/councils.toml: your councils and your watch zone.
#    Two councils ship as examples (West Suffolk = Idox Public Access,
#    Sevenoaks = published weekly list page). Read each site's robots.txt first.
tortoise config-check      # shows councils, zones, and any missing env vars

# 2. Start the Temporal dev server (durable state in a local file)
temporal server start-dev --db-filename data/temporal.db

# 3. In another terminal: the worker
tortoise init-db
tortoise worker

# 4. Try one run by hand on local Chrome (costs no Browserbase quota)
tortoise run-once --council west-suffolk

# 5. When it works: the Monday schedule (07:00 UK time)
tortoise schedule add --council west-suffolk
tortoise schedule add --council sevenoaks

# 6. The Lazy Payoff log
tortoise runs
```

## Choosing councils

Two crawl modes are supported:

- `idox_weekly_list` — councils running Idox Public Access (the majority:
  more than 150 UK councils share this one system, so one agent covers them
  all). Set `search_url` to the Weekly/Monthly Lists search page.
- `weekly_list_page` — councils that publish a plain weekly list page. Set
  `weekly_list_url`.

Each council has a watch zone: street names and postcode sectors (a sector is
the outward code plus the first inward digit, e.g. `IP33 1`). Pick a real area
— a friend's street, a busy high street — and put it in the config.

## Cost

About zero. Stagehand's driving model and Gemma run locally via Ollama. The
only paid call is the brief, computed exactly per the plan:

```
cost = (0.15 * input + 0.03 * cached + 0.50 * output) / 1,000,000   (USD)
```

Every call's tokens and dollars are logged in the `costs` table, and each
email's footer carries the run's totals: applications listed and kept, PDFs
and pages you did not have to read, browser minutes, and the cost.

## Testing

The logic is tested offline so real browser minutes go only to real runs:

```bash
python -m pytest      # 85 offline tests; no network, no Ollama, no browser
```

The crawl flows run against a scripted fake browser (see
`tests/test_crawl.py`), the cost formula is pinned to a hand-worked example,
and Gemma's JSON parsing is tested against broken replies. PDF fixtures are
hand-built minimal PDFs — regenerate with `python tests/fixtures/make_fixtures.py`.

The six failure tests (kill the worker mid-crawl, kill the browser session,
force Ultramarine to fail, stop Ollama, run the same week twice, slow site)
are documented with their expected results and evidence export in
[docs/failure-tests.md](docs/failure-tests.md). Histories land in
`docs/evidence/`.

## Repository layout

```
src/tortoise/
  models.py        domain types: applications, zones, filter results, run numbers
  config.py        councils.toml loading + env access with clear errors
  zone.py          street + postcode-sector matching (pure, offline)
  storage.py       SQLite: applications, documents, filter results, runs, costs
  cost.py          the exact dollar-cost formula (Decimal)
  extract.py       PDF text extraction, scan/drawing detection
  gemma.py         the free local filter (Ollama) with strict JSON parsing
  brief.py         Ultramarine brief writer with token budgeting
  mailer.py        Brevo email assembly and delivery
  crawl/           Stagehand drivers, schemas, document selection, crawl flows
  workflow/        the Temporal workflow, activities, schedule, worker, CLI starter
config/councils.toml   the one file you edit: councils + watch zones
docs/failure-tests.md  the six failure tests and how to prove them
docs/build-plan.pdf    the original build plan
```

## Status and honest notes

- All offline logic is implemented and tested (85 tests).
- The Stagehand integration is written against the v4 Python SDK
  (`stagehand 4.3.0`) and isolated in `src/tortoise/crawl/driver.py` — the
  plain-English instructions are tuned for Idox Public Access and will likely
  need small wording fixes on the first real run against a live council site.
  That is the one place the SDK cannot be exercised offline.
- The Browserbase session factory (`StagehandDriver.create_browserbase`) uses
  the documented CDP-connect pattern and needs its first real-run verification.
- Failure-test evidence is collected in `docs/evidence/` as the tests run.

## License

MIT — see [LICENSE](LICENSE).
