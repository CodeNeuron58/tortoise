<p align="center"><img src="assets/logo.png" alt="Tortoise logo: a tortoise whose shell is a folded street map" width="180"></p>

<h1 align="center">Tortoise</h1>

A slow, steady weekly agent that crawls a UK council's planning website, reads
every new application's PDFs, and emails you a one-page brief: what is being
built near your street, and the date by which you can object. You never open a
60-page planning document again.

Built for Hacktoberfest Week 3 — "Slow and Steady: build something that rewards
laziness". Tortoise crawls politely, one page at a time, overnight, so you can
be lazy.

## How it works

Every Monday morning (UK time), one Temporal workflow per council:

1. **Crawl** — Stagehand opens the council's weekly planning list in a
   Browserbase session pinned to the EU, submits the weekly-list form and
   reads every results page, one page at a time, heartbeating each page so a
   crash resumes instead of restarting. Idox portals share one DOM, so the
   stable parts are read with direct selectors; Stagehand's plain-English
   `act`/`extract` is the fallback for layouts that differ.
2. **Filter by place** — plain Python: street names and postcode sectors from
   your watch zone. This runs *before* any browser minute or model token is
   spent on an application.
3. **Fetch** — for each in-zone application: the comment deadline from its
   Important Dates tab, plus up to three text documents (application form,
   design and access statement, planning statement), downloaded through the
   same EU browser session. Drawings, plans and photos are skipped. A durable
   8-second pause separates applications.
4. **Filter by relevance** — Gemma runs locally via Ollama and labels every
   in-zone application for free. If Ollama is down, everything is kept and
   marked "unfiltered" — Tortoise never silently drops applications.
5. **Brief** — Ultramarine 1 Flash reads the council's listing and the kept
   documents and writes the brief. The one paid step, a fraction of a cent,
   counted to the last token.
6. **Email** — Brevo sends a designed email: one card per application with
   what is proposed, why a neighbour might care, the comment deadline (with a
   countdown) and a link to the council's page, then the run's numbers and
   exact cost. Deadlines and links come from the council's pages, never from
   the model. If the brief was skipped (bad key, no credit), the email says so
   instead of going silent.

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
  documents each. Every browser session is released the moment its work is
  done.
- Never logs in, never comments, never submits anything.
- Never opens an application's details or contacts tabs (they carry
  applicants' names), and the brief is told never to name people. The
  `data/` directory (database and downloaded PDFs) is gitignored.
- `robots.txt` and the site terms decide whether a council may be switched
  on, and nothing can start a run for a council with `enabled = false`.

**What robots.txt actually says.** On 25 Oct 2026 (UTC) every reachable Idox
Public Access portal checked — West Suffolk, Sevenoaks and twelve more —
served `User-agent: *` / `Disallow: /`; the others block automated traffic
outright. The West Suffolk runs behind this project's demo and evidence were
made before that was checked (the portal is geo-blocked from where Tortoise
was built). So both example councils ship **disabled**: switch one on only
with that council's permission.

## Setup

Python 3.11 (pinned in `.python-version`), managed with [uv](https://docs.astral.sh/uv/):

```bash
# once: install uv
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"   # Windows
# curl -LsSf https://astral.sh/uv/install.sh | sh                                     # macOS/Linux

uv sync                     # creates .venv and installs the locked dependencies (uv.lock)
cp .env.example .env        # fill in what you have; see the table below
```

| Variable | What for | Where to get it |
|---|---|---|
| `BROWSERBASE_API_KEY` / `BROWSERBASE_PROJECT_ID` | real browser runs, in `eu-central-1` (UK portals block other regions) | [browserbase.com](https://www.browserbase.com) |
| `STAGEHAND_BASE_URL` / `STAGEHAND_MODEL` | the model driving Stagehand's fallback calls (local Ollama, or Ultramarine once its key is set) | — |
| `OLLAMA_BASE_URL` / `GEMMA_MODEL` | the free local filter | [ollama.com](https://ollama.com); `ollama pull gemma4:e4b` |
| `ULTRAMARINE_BASE_URL` / `ULTRAMARINE_API_KEY` / `ULTRAMARINE_MODEL` | writes the brief ($0.15/M in, $0.03/M cached, $0.50/M out) | [Paper Instruments docs](https://docs.paperinstruments.com/docs); Hacktoberfest gives $5 free credit |
| `BREVO_API_KEY`, `BREVO_FROM_EMAIL`, `TORTOISE_TO_EMAIL` | sends the email (free plan) | [brevo.com](https://www.brevo.com) |
| `TORTOISE_CAPTURE_DIR` | optional: a screenshot of every page a run opens | — |
| `TORTOISE_LOGO_URL` | optional: the logo in the email header (a public URL, e.g. the raw GitHub URL of `assets/logo-email.png`) | — |

Then:

```bash
# 1. Edit config/councils.toml: your council and your watch zone.
#    Read the council's robots.txt and terms; set enabled = true only if they
#    (or the council, when you ask) allow it.
uv run tortoise config-check      # councils, zones, and any missing env vars

# 2. Start the Temporal dev server (durable state in a local file)
temporal server start-dev --db-filename data/temporal.db

# 3. In another terminal: the worker
uv run tortoise init-db
uv run tortoise worker

# 4. One run by hand (UK portals need --browserbase; local Chrome is for development)
uv run tortoise run-once --council <slug> --browserbase

# 5. When it works: the Monday schedule (07:00 UK time)
uv run tortoise schedule add --council <slug>

# 6. The Lazy Payoff log
uv run tortoise runs

# 7. See any past run's email again — no crawl, no model call
uv run tortoise email-preview --council <slug> --week <week label>   # add --send to mail it
```

## Choosing councils

Two crawl modes are supported:

- `idox_weekly_list` — councils running Idox Public Access (more than 150 UK
  councils share this one system, so one agent covers them all). Set
  `search_url` to the Weekly/Monthly Lists search page.
- `weekly_list_page` — councils that publish a plain weekly list page. Set
  `weekly_list_url`. (Shipped as an example; not yet run live.)

Each council has a watch zone: street names and postcode sectors (a sector is
the outward code plus the first inward digit, e.g. `IP33 3`). Pick a real area
— a friend's street, a busy high street — and put it in the config.

## A real week

West Suffolk, the weekly list for 19–23 Oct 2026, run at 23:11 UTC on 25 Oct
(`2026-W44`):

- **23** applications listed over 3 results pages; **2** inside the watch zone
  (York Road and Springfield Road, Bury St Edmunds); Gemma kept both.
- **2** application-form PDFs read (8 and 5 pages); the drawings were skipped.
- Comment deadlines found: **14 Nov 2026** and **10 Nov 2026**.
- **7** pages read, **1.71** browser minutes over 3 sessions, all released.
- Ultramarine: 4,187 tokens in, 2,852 out — **$0.0021** for the week.

The email that run sent is in
[docs/evidence/email-2026-W44.txt](docs/evidence/email-2026-W44.txt), and its
workflow history in `docs/evidence/successful-full-run.json`.

## Cost

About zero. Gemma runs locally, and the crawl uses direct selectors, so the
browser-driving model is normally never called. The paid calls are the brief
and, only when a page needs Stagehand's fallback, the driving model; both are
counted:

```
cost = (0.15 * uncached_input + 0.03 * cached_input + 0.50 * output) / 1,000,000   (USD)
```

Every call's tokens and dollars are logged in the `costs` table (exported to
[docs/evidence/cost-log.csv](docs/evidence/cost-log.csv)), and each email's
footer carries the run's totals: applications listed and kept, PDFs and
pages read, browser minutes, Gemma seconds, tokens and dollars.

## Testing

The logic is tested offline so real browser minutes go only to real runs:

```bash
uv run pytest         # 109 offline tests; no network, no Ollama, no browser
```

The crawl flows run against a scripted fake browser (see
`tests/test_crawl.py`), the cost formula is pinned to hand-worked examples,
and Gemma's JSON parsing is tested against broken replies. PDF fixtures are
hand-built minimal PDFs — regenerate with `python tests/fixtures/make_fixtures.py`.

The six failure tests — kill the worker mid-crawl, end the browser session,
make Ultramarine fail, stop Ollama, run the same week twice, a slow site —
all passed against the live stack. Procedures, results and the exported
workflow histories: [docs/failure-tests.md](docs/failure-tests.md) and
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
  crawl/           Stagehand driver, page parsers, document selection, crawl flows
  workflow/        the Temporal workflow, activities, schedule, worker, CLI starter
config/councils.toml   the one file you edit: councils + watch zones
docs/failure-tests.md  the six failure tests and their results
docs/evidence/         workflow histories, the real email, the cost log
assets/                the logo (README and email header)
```

## Status and honest notes

- The whole pipeline ran live, end to end, on 25 Oct 2026 (UTC): EU Browserbase
  session → 23 applications → zone filter → local Gemma → two PDFs read →
  Ultramarine brief → email with the exact cost in the footer. All six
  failure tests have evidence in `docs/evidence/`.
- Only West Suffolk has been run live, and its `robots.txt` disallows
  automated access (see the politeness rules). Both example councils ship
  disabled.
- Fixed just before the run above: document downloads (the session
  fetch built invalid JavaScript, so earlier runs read 0 PDFs), comment
  deadlines (read from the Important Dates tab, not the summary), the brief
  prompt (it never included the proposal), Idox resume-from-heartbeat,
  sessions left running until Browserbase's timeout, and cached tokens
  billed twice. Histories exported before the fixes (failure tests 3 and 4)
  show the old behaviour in their briefs.
- Gemma's relevance score is from a 4B local model; the plain-Python zone
  filter does the heavy lifting, and Gemma only ever sees in-zone
  applications.

## License

MIT — see [LICENSE](LICENSE).
