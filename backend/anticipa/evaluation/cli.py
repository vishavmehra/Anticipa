"""Operator CLI: `python -m anticipa.evaluation run --dataset-version V [--cohort-manifest PATH] [--split dev|test|all --final]`.

Exit codes (09 §10.1): 0 all evaluated gates passed; 1 a blocking gate failed; 2 integrity/usage error (no metrics
trusted); 3 partial run. The evaluator reads stored results and the planted ground truth; it makes no model calls.
Instruction: deliver a working, tested, demonstrable Anticipa MVP including the evaluation report.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from anticipa.db import get_sessionmaker
from anticipa.evaluation import error_analysis, gt, readers, runner


def _run(args: argparse.Namespace) -> int:
    cfg = runner.RunConfig(
        dataset_version=args.dataset_version, split=args.split, cohort_manifest=args.cohort_manifest, final=args.final,
        gt_root=args.gt_root, verify_checksums=not args.no_checksum, bootstrap_resamples=args.bootstrap, seed=args.seed,
        probe_closure=args.probe, profile=args.profile, out_dir=args.out_dir, docs_dir=args.docs_dir if args.docs_dir else None,
        persist=not args.no_persist,
    )  # fmt: skip
    if args.dry_check:
        try:
            out = runner.dry_check(get_sessionmaker(), cfg)
        except (gt.GtError, ValueError, FileNotFoundError) as exc:
            print(json.dumps({"error": getattr(exc, "code", type(exc).__name__), "message": str(exc)}), file=sys.stderr)
            return 2
        print(json.dumps(out, indent=2, default=str))
        return 0 if out["ready"] else 1
    try:
        rep = runner.run_evaluation(get_sessionmaker(), cfg, progress=lambda m: print(f"[evaluation] {m}", file=sys.stderr))
    except (gt.GtError, ValueError, FileNotFoundError) as exc:
        print(json.dumps({"error": getattr(exc, "code", type(exc).__name__), "message": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps({"run_id": rep["run_id"], "status": rep["status"], "gate_summary": rep["gate_summary"], "metrics_digest": rep["metrics_digest"],
                      "files": rep.get("files", {}), "exit_code": rep["exit_code"]}, indent=2))  # fmt: skip
    return rep["exit_code"]


def _errors(args: argparse.Namespace) -> int:
    """Dev split only error analysis for the AI workstream (never reads the test split)."""
    sessions = get_sessionmaker()
    try:
        ids = readers.load_cohort_manifest(args.cohort_manifest)["resident_ids"]
        report = error_analysis.analyse(sessions, args.dataset_version, ids, examples=args.examples, verify=not args.no_checksum,
                                        text_of=error_analysis.document_text_reader(sessions))
    except (gt.GtError, ValueError, FileNotFoundError) as exc:
        print(json.dumps({"error": getattr(exc, "code", type(exc).__name__), "message": str(exc)}), file=sys.stderr)
        return 2
    path = error_analysis.write_json(report, Path(args.out))
    print(json.dumps({"written": str(path), "split": report["split"], "totals": report["totals"], "strict_misses_by_type": report["strict_misses_by_type"]}, indent=2))
    return 0


def _impact(args: argparse.Namespace) -> int:
    """Proven impact baselines (rules only, first come planner): dev split truth only, read only, no model calls."""
    from anticipa.evaluation import impact  # local import: it loads the guideline engine and the planner

    try:
        return impact.cli(args, get_sessionmaker())
    except (gt.GtError, ValueError, FileNotFoundError) as exc:
        print(json.dumps({"error": getattr(exc, "code", type(exc).__name__), "message": str(exc)}), file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="anticipa-evaluation", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="evaluate the stored pipeline results against the planted ground truth")
    r.add_argument("--dataset-version", required=True)
    r.add_argument("--cohort-manifest", help="AI-cohort manifest JSON (cohorts/<id>.json of the document store); default: every resident with labels")
    r.add_argument("--split", choices=gt.SPLITS[:3], default="dev", help="dev (default), or test/all which require --final")
    r.add_argument("--final", action="store_true", help="deliberate, logged evaluation of the test split (09 §5.6)")
    r.add_argument("--profile", choices=("dev", "ci", "release"), default="dev")
    r.add_argument("--gt-root", help="ground-truth root (default data/ground_truth)")
    r.add_argument("--no-checksum", action="store_true", help="skip ground-truth sha256 verification (lower assurance; recorded)")
    r.add_argument("--bootstrap", type=int, default=200, help="cluster-bootstrap resamples for F1 intervals (0 = none)")
    r.add_argument("--seed", type=int, default=42)
    r.add_argument("--probe", type=int, default=20, help="open cases to probe for invalid closure (rolled back; 0 = skip)")
    r.add_argument("--out-dir", help="default data/reports/<run_id>")
    r.add_argument("--docs-dir", default=str(Path(runner.DOCS_DIR)), help="where the human-readable report is also written (default Docs/evaluation); '' to skip")
    r.add_argument("--no-persist", action="store_true", help="do not write the evaluation_runs row")
    r.add_argument("--dry-check", action="store_true", help="preflight only: checksums, document sync, readiness; reads no label and logs no test access")
    r.set_defaults(func=_run)
    e = sub.add_parser("errors", help="dev split ONLY: extraction error analysis (JSON) for the AI workstream; the test split is never read")
    e.add_argument("--dataset-version", required=True)
    e.add_argument("--cohort-manifest", required=True)
    e.add_argument("--out", default=str(error_analysis.DEFAULT_OUT))
    e.add_argument("--examples", type=int, default=40)
    e.add_argument("--no-checksum", action="store_true", help="skip ground truth file sha256 verification (recorded in the output)")
    e.set_defaults(func=_errors)
    i = sub.add_parser(
        "impact",
        help="proven impact baselines (rules only, first come planner); writes data/reports/impact-baselines-<date>.json",
    )
    i.add_argument("--out", help="default data/reports/impact-baselines-<YYYYMMDD>.json")
    i.add_argument("--store", action="store_true", help="also keep a copy in analytics_snapshots for deployments without data/")
    i.set_defaults(func=_impact)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
