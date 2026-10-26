"""The ``tortoise`` command line: init, check, worker, run-once, schedule, runs.

Everything the plan needs on the command line:
* ``tortoise init-db`` — create the SQLite database.
* ``tortoise config-check`` — validate the council config and report which
  environment variables are still missing, before anything costs money.
* ``tortoise worker`` — run the Temporal worker (leave it running).
* ``tortoise run-once --council west-suffolk`` — start one run by hand and
  wait for it (the demo path; uses local Chrome unless ``--browserbase``).
* ``tortoise schedule add|remove|list`` — manage the Monday 07:00 crons.
* ``tortoise runs`` — the Lazy Payoff log from recent runs.
* ``tortoise email-preview --council west-suffolk --week 2026-W44`` — render
  a past run's email to an HTML file (no crawl, no model call); ``--send``
  mails it too.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from dotenv import load_dotenv

from .config import env_str, load_config
from .storage import Database
from .workflow import activities as _activities  # noqa: F401  (ensures flags documented)
from .workflow import schedule as schedule_module
from .workflow.connection import TASK_QUEUE
from .workflow.starter import start_and_wait


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nstopped")
        return 130
    except Exception as exc:  # one clear line instead of a traceback wall
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tortoise",
        description="A slow, steady weekly planning-application watcher.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init_db = sub.add_parser("init-db", help="create the SQLite database and schema")
    init_db.set_defaults(func=_cmd_init_db)

    check = sub.add_parser("config-check", help="validate config and report missing env vars")
    check.set_defaults(func=_cmd_config_check)

    worker = sub.add_parser("worker", help="run the Temporal worker (Ctrl+C to stop)")
    worker.set_defaults(func=_cmd_worker)

    run_once = sub.add_parser("run-once", help="start one WeeklyWatch run and wait for it")
    run_once.add_argument("--council", required=True, help="council slug from councils.toml")
    run_once.add_argument("--week", default=None, help="ISO week like 2026-W43 (default: this week)")
    browser = run_once.add_mutually_exclusive_group()
    browser.add_argument(
        "--local", dest="local_browser", action="store_true", default=True,
        help="use local Chrome (default; costs no Browserbase quota)",
    )
    browser.add_argument(
        "--browserbase", dest="local_browser", action="store_false",
        help="use Browserbase (real runs; spends browser minutes)",
    )
    run_once.set_defaults(func=_cmd_run_once)

    schedule = sub.add_parser("schedule", help="manage the Monday 07:00 UK schedules")
    schedule_sub = schedule.add_subparsers(dest="schedule_command", required=True)
    add = schedule_sub.add_parser("add", help="add the weekly schedule for a council")
    add.add_argument("--council", required=True)
    add.set_defaults(func=_cmd_schedule_add)
    remove = schedule_sub.add_parser("remove", help="remove the schedule for a council")
    remove.add_argument("--council", required=True)
    remove.set_defaults(func=_cmd_schedule_remove)
    listing = schedule_sub.add_parser("list", help="list schedules")
    listing.set_defaults(func=_cmd_schedule_list)

    runs = sub.add_parser("runs", help="show recent runs (the Lazy Payoff log)")
    runs.add_argument("--limit", type=int, default=10)
    runs.set_defaults(func=_cmd_runs)

    preview = sub.add_parser(
        "email-preview",
        help="render a past run's email to an HTML file (no crawl, no model call)",
    )
    preview.add_argument("--council", required=True)
    preview.add_argument("--week", required=True, help="the week label the run used, e.g. 2026-W44")
    preview.add_argument("--out", default=None, help="HTML file to write (default: in the data dir)")
    preview.add_argument("--send", action="store_true", help="also send it to TORTOISE_TO_EMAIL")
    preview.set_defaults(func=_cmd_email_preview)

    return parser


def _cmd_init_db(args: argparse.Namespace) -> int:
    config = load_config()
    database = Database(config.data_dir / "tortoise.db")
    database.init_schema()
    print(f"database ready at {database.path}")
    return 0


def _cmd_config_check(args: argparse.Namespace) -> int:
    config = load_config()
    print(f"data dir: {config.data_dir}")
    for council in config.councils:
        state = "enabled" if council.enabled else "disabled"
        print(f"\n{council.slug} ({state}) — {council.name}")
        print(f"  mode: {council.mode.value}")
        url = council.search_url or council.weekly_list_url
        print(f"  url: {url}")
        print(f"  zone: {council.zone.label}")
        print(f"  streets: {', '.join(council.zone.streets) or '(none)'}")
        print(f"  sectors: {', '.join(council.zone.postcode_sectors) or '(none)'}")
        if "PLACEHOLDER" in council.zone.label or "placeholder" in council.zone.label:
            print("  !! placeholder zone — edit config/councils.toml before a real run")

    missing = _missing_env()
    print("\nenvironment:")
    if missing:
        for name, what in missing:
            print(f"  missing {name} ({what})")
        print("  copy .env.example to .env and fill in what you need")
    else:
        print("  all required variables are set")
    return 0


# Variables that genuinely have no sensible default. The Stagehand/Ollama
# settings default to local Ollama in code, so they are not listed here;
# BROWSERBASE_* is only needed for real (non-local) runs.
_REQUIRED_BY_STEP = {
    "writing the brief (Ultramarine)": [
        ("ULTRAMARINE_API_KEY", "writing the brief"),
    ],
    "sending email (Brevo)": [
        ("BREVO_API_KEY", "sending the email"),
        ("BREVO_FROM_EMAIL", "sending the email"),
        ("TORTOISE_TO_EMAIL", "sending the email"),
    ],
    "real browser runs (optional — local Chrome works without)": [
        ("BROWSERBASE_API_KEY", "real browser runs"),
        ("BROWSERBASE_PROJECT_ID", "real browser runs"),
    ],
}


def _missing_env() -> list[tuple[str, str]]:
    missing: list[tuple[str, str]] = []
    for step, variables in _REQUIRED_BY_STEP.items():
        for name, what in variables:
            if not env_str(name):
                missing.append((f"{name} [{step}]", what))
    return missing


def _cmd_worker(args: argparse.Namespace) -> int:
    from .workflow.worker import run_worker

    print(f"starting worker on task queue {TASK_QUEUE!r} (Ctrl+C to stop)")
    asyncio.run(run_worker())
    return 0


def _cmd_run_once(args: argparse.Namespace) -> int:
    load_config().enabled_council(args.council)  # fail fast, before any connect
    print(f"starting a run for {args.council} (week {args.week or '(this week)'})")
    numbers = asyncio.run(
        start_and_wait(
            council=args.council,
            week=args.week,
            local_browser=args.local_browser,
        )
    )
    print(f"\nstatus: {numbers.status}")
    print(numbers.footer())
    if numbers.brief_skipped_reason:
        print(f"brief skipped: {numbers.brief_skipped_reason}")
    return 0


def _cmd_schedule_add(args: argparse.Namespace) -> int:
    load_config().enabled_council(args.council)  # fail fast on unknown or disabled
    sid = asyncio.run(schedule_module.install(args.council))
    print(f"schedule {sid} installed ({schedule_module.CRON} {schedule_module.TIME_ZONE})")
    return 0


def _cmd_schedule_remove(args: argparse.Namespace) -> int:
    asyncio.run(schedule_module.remove(args.council))
    print(f"schedule removed for {args.council}")
    return 0


def _cmd_schedule_list(args: argparse.Namespace) -> int:
    infos = asyncio.run(schedule_module.list_schedules())
    if not infos:
        print("no schedules installed")
    for info in infos:
        print(f"{info.id}: cron {info.cron!r} in {info.time_zone}")
    return 0


def _cmd_runs(args: argparse.Namespace) -> int:
    config = load_config()
    database = Database(config.data_dir / "tortoise.db")
    database.init_schema()
    runs = database.recent_runs(limit=args.limit)
    if not runs:
        print("no runs recorded yet")
        return 0
    for run in runs:
        prefix = "*" if run.unfiltered else " "
        print(f"{prefix} {run.council} {run.week} [{run.status}] {run.footer()}")
        if run.brief_skipped_reason:
            print(f"    brief skipped: {run.brief_skipped_reason}")
    print("\n(* = unfiltered: Gemma was down, everything was kept)")
    return 0


def _cmd_email_preview(args: argparse.Namespace) -> int:
    """Rebuild a recorded run's email from storage: nothing is crawled or paid for."""
    import base64
    from pathlib import Path

    from .brief import parse_brief
    from .workflow.activities import compose_email

    config = load_config()
    council = config.council(args.council)  # a disabled council is fine: nothing is crawled
    database = Database(config.data_dir / "tortoise.db")
    database.init_schema()
    numbers = database.run_numbers(council.slug, args.week)
    if numbers is None:
        print(f"error: no run recorded for {council.slug} week {args.week} (see `tortoise runs`)",
              file=sys.stderr)
        return 1
    brief_text = database.brief_for(council.slug, args.week)
    references = [entry.reference for entry in parse_brief(brief_text).entries] if brief_text else []

    # The file preview shows the logo even before it is hosted anywhere;
    # a sent email needs TORTOISE_LOGO_URL (mail clients block embedded images).
    logo_file = Path("assets/logo-email.png")
    local_logo = env_str("TORTOISE_LOGO_URL")
    if not local_logo and logo_file.is_file():
        local_logo = "data:image/png;base64," + base64.b64encode(logo_file.read_bytes()).decode()
    content = compose_email(database, council, numbers, brief_text, references, logo_url=local_logo)

    out = Path(args.out) if args.out else config.data_dir / f"email-preview-{council.slug}-{args.week}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content.html, encoding="utf-8")
    print(f"subject: {content.subject}")
    print(f"wrote {out.resolve()}  (open it in a browser)")

    if args.send:
        from .config import env_required
        from .mailer import BrevoSender

        sendable = compose_email(database, council, numbers, brief_text, references)
        sender = BrevoSender(
            api_key=env_required("BREVO_API_KEY", "sending the email"),
            from_email=env_required("BREVO_FROM_EMAIL", "sending the email"),
            to_email=env_required("TORTOISE_TO_EMAIL", "sending the email"),
            from_name=env_str("BREVO_FROM_NAME", "Tortoise") or "Tortoise",
        )
        asyncio.run(sender.send(sendable))
        print(f"sent to {env_str('TORTOISE_TO_EMAIL')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
