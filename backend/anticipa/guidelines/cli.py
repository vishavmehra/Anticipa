"""Operator CLI for the guideline engine (also `python -m anticipa.guidelines ...`).

  import-drafts [PATH] [--dry-run]   import guideline drafts (default: config/guidelines/rules_draft.json). Imports
                                     DRAFTS ONLY; approval stays a human decision (guidelines.approve).
  evaluate [--dataset V] [--date D]  run the population evaluation inline (no worker) with the approved rules and print
                                     measured throughput plus a linear extrapolation to 100k / 1M residents.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from anticipa.config import ROOT_DIR
from anticipa.core import audit
from anticipa.core.audit import Actor
from anticipa.db import get_sessionmaker
from anticipa.guidelines import service
from anticipa.guidelines.population import EvalScope, evaluate_population, extrapolate, resolve_rules

DEFAULT_DRAFTS = ROOT_DIR / "config" / "guidelines" / "rules_draft.json"


def _import_drafts(args: argparse.Namespace) -> int:
    path = Path(args.path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": f"cannot read {path}: {exc}"}), file=sys.stderr)
        return 2
    with get_sessionmaker()() as session:
        report = service.import_drafts(session, payload, Actor.system(), dry_run=args.dry_run)
        if args.dry_run:
            session.rollback()
        else:
            session.commit()
    print(json.dumps({"file": str(path), "dry_run": args.dry_run, **report.as_dict()}, indent=2))
    return 1 if report.errors else 0


def _evaluate(args: argparse.Namespace) -> int:
    sessions = get_sessionmaker()
    on = date.fromisoformat(args.date) if args.date else datetime.now(UTC).date()
    with sessions() as s:
        rules = resolve_rules(s, on)
    scope = EvalScope(on, [r.rule_version_id for r in rules], dataset_version=args.dataset, mode=args.mode, chunk_size=args.chunk_size)
    stats = evaluate_population(sessions, scope)
    with sessions() as s:
        audit.record(s, Actor.system(), "eligibility.evaluate_population", "cli", None, "success",
                     {"evaluation_date": on.isoformat(), "rule_versions": len(rules), **stats.as_dict()})  # fmt: skip
        s.commit()
    rate = stats.residents / stats.seconds if stats.seconds else 0
    print(json.dumps({**stats.as_dict(), "rules": len(rules), "extrapolation": extrapolate(rate, len(rules))}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="anticipa-guidelines", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import-drafts", help="import guideline drafts (never approves)")
    imp.add_argument("path", nargs="?", default=str(DEFAULT_DRAFTS))
    imp.add_argument("--dry-run", action="store_true", help="validate and report without writing")
    imp.set_defaults(func=_import_drafts)
    ev = sub.add_parser("evaluate", help="evaluate the population inline and report throughput")
    ev.add_argument("--dataset", help="dataset_version to restrict to")
    ev.add_argument("--date", help="evaluation date (ISO); default today UTC")
    ev.add_argument("--mode", choices=("incremental", "full"), default="incremental")
    ev.add_argument("--chunk-size", type=int, default=1000)
    ev.set_defaults(func=_evaluate)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
