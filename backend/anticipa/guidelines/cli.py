"""Operator CLI for the guideline engine (also `python -m anticipa.guidelines ...`).

import-drafts [PATH] [--dry-run]   import guideline drafts (default: config/guidelines/rules_draft.json).
                                   Imports DRAFTS ONLY; approval stays a human decision (guidelines.approve).
submit [--dataset V] [--mode M]    queue the population evaluation and care-gap case creation for a worker
                                   (approved versions only). Checkpointed, resumable. Submitting again in the
                                   default incremental mode re-evaluates only changed residents.
reconcile                          align unplanned cases with their newest evaluation.
evaluate [--dataset V] [--date D]  run the population evaluation inline (no worker) with approved rules; print
                                   measured throughput plus a linear extrapolation to 100k / 1M residents.

`submit` and `evaluate` accept --guideline-keys A,B (only those approved guidelines) and --storage-profile
full|rules (only residents stored with that profile; the lean `rules` profile lacks records that later
guidelines may need). `evaluate --create-cases` then creates care-gap cases and reconciles open ones for the
same versions, resumable because every step is idempotent.
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
from anticipa.guidelines.population import EvalScope, evaluate_population, extrapolate, resolve_guidelines

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


def _submit(args: argparse.Namespace) -> int:
    from anticipa.core.errors import ApiError
    from anticipa.guidelines.handlers import submit_evaluation_job

    on = date.fromisoformat(args.date) if args.date else None
    try:
        with get_sessionmaker()() as session:
            result = submit_evaluation_job(
                session,
                evaluation_date=on,
                dataset_version=args.dataset,
                mode=args.mode,
                chunk_size=args.chunk_size,
                create_cases=not args.no_cases,
                idempotency_key=args.idempotency_key,
                guideline_keys=_keys(args.guideline_keys),
                storage_profile=args.storage_profile,
            )
            session.commit()
            job = result.job
            print(
                json.dumps(
                    {
                        "job_id": str(job.id),
                        "created": result.created,
                        "status": job.status,
                        "scope": job.input_scope,
                    },
                    indent=2,
                )
            )
    except ApiError as exc:
        print(json.dumps({"error": exc.message}), file=sys.stderr)
        return 2
    return 0


def _reconcile(_args: argparse.Namespace) -> int:
    from anticipa.caregaps.service import reconcile_open_cases

    print(json.dumps(reconcile_open_cases(get_sessionmaker(), actor=Actor.system())))
    return 0


def _keys(raw: str | None) -> list[str] | None:
    return [k.strip() for k in raw.split(",") if k.strip()] if raw else None


def _evaluate(args: argparse.Namespace) -> int:
    from anticipa.core.errors import ApiError

    sessions = get_sessionmaker()
    on = date.fromisoformat(args.date) if args.date else datetime.now(UTC).date()
    try:
        with sessions() as s:
            rules = resolve_guidelines(s, on, guideline_keys=_keys(args.guideline_keys))
        scope = EvalScope(
            on,
            [r.guideline_version_id for r in rules],
            dataset_version=args.dataset,
            mode=args.mode,
            chunk_size=args.chunk_size,
            storage_profile=args.storage_profile,
        )
        stats = evaluate_population(sessions, scope)
    except ApiError as exc:
        print(json.dumps({"error": exc.message}), file=sys.stderr)
        return 2
    with sessions() as s:
        audit.record(
            s,
            Actor.system(),
            "eligibility.evaluate_population",
            "cli",
            None,
            "success",
            {"evaluation_date": on.isoformat(), "guideline_versions": len(rules), **stats.as_dict()},
        )
        s.commit()
    rate = stats.residents / stats.seconds if stats.seconds else 0
    report = {
        **stats.as_dict(),
        "guideline_versions": len(rules),
        "guideline_version_ids": [str(r.guideline_version_id) for r in rules],
        "storage_profile": args.storage_profile,
        "extrapolation": extrapolate(rate, len(rules)),
    }
    if args.create_cases:
        from anticipa.caregaps.service import create_cases_scan, reconcile_open_cases

        ids = [r.guideline_version_id for r in rules]
        report["cases"] = create_cases_scan(sessions, ids, actor=Actor.system())
        report["reconciled"] = reconcile_open_cases(sessions, ids, actor=Actor.system())
    print(json.dumps(report, indent=2))
    return 0


def _scope_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--guideline-keys", help="comma separated guideline keys (default: every approved guideline)"
    )
    parser.add_argument(
        "--storage-profile",
        choices=("full", "rules"),
        help="only residents stored with this profile (default: all)",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="anticipa-guidelines", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import-drafts", help="import guideline drafts (never approves)")
    imp.add_argument("path", nargs="?", default=str(DEFAULT_DRAFTS))
    imp.add_argument("--dry-run", action="store_true", help="validate and report without writing")
    imp.set_defaults(func=_import_drafts)
    sb = sub.add_parser(
        "submit", help="queue the population evaluation for a worker (approved versions only)"
    )
    sb.add_argument("--dataset", help="dataset_version to restrict to")
    sb.add_argument("--date", help="evaluation date (ISO); default today UTC")
    sb.add_argument("--mode", choices=("incremental", "full"), default="incremental")
    sb.add_argument("--chunk-size", type=int, default=1000)
    sb.add_argument("--no-cases", action="store_true", help="do not create care-gap cases afterwards")
    sb.add_argument("--idempotency-key", help="re-submitting the same key returns the existing job")
    _scope_options(sb)
    sb.set_defaults(func=_submit)
    sub.add_parser("reconcile", help="align unplanned cases with the newest evaluation").set_defaults(
        func=_reconcile
    )
    ev = sub.add_parser("evaluate", help="evaluate the population inline and report throughput")
    ev.add_argument("--dataset", help="dataset_version to restrict to")
    ev.add_argument("--date", help="evaluation date (ISO); default today UTC")
    ev.add_argument("--mode", choices=("incremental", "full"), default="incremental")
    ev.add_argument("--chunk-size", type=int, default=1000)
    ev.add_argument("--create-cases", action="store_true", help="then create care-gap cases and reconcile")
    _scope_options(ev)
    ev.set_defaults(func=_evaluate)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
