"""`anticipa` command line: `worker`, `migrate`, `seed-users`. Run with `uv run --directory backend anticipa
<command>`."""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import logging
import signal
import sys
import threading
from pathlib import Path

from anticipa.config import ConfigError, get_settings
from anticipa.core.logging import configure_logging
from anticipa.core.modules import MODULES

log = logging.getLogger("anticipa.cli")
ALEMBIC_DIR = (
    Path(__file__).resolve().parents[1] / "alembic"
)  # backend/alembic (/app/backend/alembic in the image)


def cmd_worker(args: argparse.Namespace) -> int:
    from anticipa.ai import DbSink, configure_router
    from anticipa.db import get_sessionmaker
    from anticipa.jobs.worker import WorkerConfig, run_worker

    configure_router(sink=DbSink(get_sessionmaker()))  # model calls made by job handlers are logged too
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    done = run_worker(
        get_sessionmaker(), WorkerConfig.from_settings(get_settings()), stop, exit_when_idle=args.once
    )
    print(f"worker stopped; jobs processed: {done}")
    return 0


def cmd_migrate(_args: argparse.Namespace) -> int:
    """`alembic upgrade head` against MIGRATION_DATABASE_URL (or the runtime database). For hosts without a
    shell."""
    from alembic.config import Config

    from alembic import command

    cfg = Config(str(ALEMBIC_DIR.parent / "alembic.ini"))
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    command.upgrade(cfg, "head")
    return 0


def cmd_seed_users(_args: argparse.Namespace) -> int:
    from anticipa.core.supabase_admin import SeedError, record_seed_audit, seed_users
    from anticipa.db import get_sessionmaker

    settings = get_settings()
    try:
        results = seed_users(
            settings,
            settings.demo_password.get_secret_value() if settings.demo_password else None,
            judge_password=settings.judge_password.get_secret_value() if settings.judge_password else None,
        )
    except SeedError as exc:
        print(f"seed-users: {exc}", file=sys.stderr)
        return 2
    for r in results:
        print(f"{r.action:8} {r.role:18} {r.email}")
    try:  # role assignment is sensitive: leave an audit trail when a database is configured and reachable
        with get_sessionmaker()() as session:
            record_seed_audit(session, results)
            session.commit()
    except Exception as exc:
        print(
            "seed-users: users were provisioned, but the audit events could not be stored "
            f"({type(exc).__name__}).",
            file=sys.stderr,
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="anticipa", description="Anticipa operator commands")
    sub = parser.add_subparsers(dest="command", required=True)
    worker = sub.add_parser("worker", help="run the background job worker (claims jobs with SKIP LOCKED)")
    worker.add_argument("--once", action="store_true", help="exit when no job is runnable")
    worker.set_defaults(func=cmd_worker)
    sub.add_parser("migrate", help="apply database migrations (alembic upgrade head)").set_defaults(
        func=cmd_migrate
    )
    sub.add_parser(
        "seed-users",
        help="create/update demo users in Supabase Auth (needs SUPABASE_SECRET_KEY, ANTICIPA_DEMO_PASSWORD)",
    ).set_defaults(func=cmd_seed_users)
    return parser


def module_clis() -> dict[str, str]:
    """`anticipa/<module>/cli.py` files exposing `main(argv)`, mounted as `anticipa <module> ...`
    (`datafabric.docgen` is mounted as `docgen`)."""
    found = {}
    for module in MODULES:
        path = f"anticipa.{module}.cli"
        if importlib.util.find_spec(path) is not None:
            found[module.rsplit(".", 1)[-1]] = path
    return found


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    mounted = module_clis()
    try:
        if argv and argv[0] in mounted:  # delegate: the module owns its sub-commands and flags
            return importlib.import_module(mounted[argv[0]]).main(argv[1:]) or 0
        parser = build_parser()
        parser.epilog = "module commands: " + ", ".join(f"{n} (anticipa {n} --help)" for n in sorted(mounted))
        args = parser.parse_args(argv)
        configure_logging(get_settings().log_level)
        return args.func(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
