# ruff: noqa: E501
"""CLI: `python -m anticipa.datafabric.docgen {generate,verify} ...` (prints one JSON summary on stdout)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date


def _residents(value: str) -> int | None:
    return None if value == "all" else int(value)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m anticipa.datafabric.docgen", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    g = sub.add_parser(
        "generate", help="plant facts and write documents + ground truth (idempotent, resumable)"
    )
    g.add_argument("--dataset-version", required=True)
    g.add_argument(
        "--residents",
        type=_residents,
        default=None,
        metavar="N|all",
        help="residents to consider (default all)",
    )
    g.add_argument("--seed", type=int, required=True)
    g.add_argument(
        "--source",
        choices=("db", "synthea"),
        default="db",
        help="db: residents/clinical_records in PostgreSQL; synthea: data/synthea/<V> NDJSON (no DB)",
    )
    g.add_argument("--chunk-size", type=int, default=500)
    g.add_argument(
        "--register",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="insert `documents` rows (default: on for --source db, off for synthea)",
    )
    g.add_argument(
        "--enrich",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="plant seeded screening completions/exclusions (structured rows with --register, or note-only); default on",
    )
    g.add_argument(
        "--family",
        choices=("linked", "all", "none"),
        default=None,
        help="who gets family-dependent facts: residents of family-linked blocks only (default for db), all, none",
    )
    g.add_argument(
        "--only-linked",
        action="store_true",
        help="skip residents outside family-linked blocks (generate them after their block is linked)",
    )
    g.add_argument(
        "--refresh-linked",
        action="store_true",
        help="first purge and regenerate residents generated before their block was family-linked",
    )
    g.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=None,
        help="reference date (default: dataset reference date)",
    )
    r = sub.add_parser(
        "reset", help="delete docgen output for a dataset (dry run unless --yes), e.g. to regenerate"
    )
    r.add_argument("--dataset-version", required=True)
    r.add_argument(
        "--yes", action="store_true", help="actually delete (default: only report what would be deleted)"
    )
    r.add_argument(
        "--keep-enrichment", action="store_true", help="keep the anticipa_enrichment clinical_records rows"
    )
    r.add_argument("--files-only", action="store_true", help="do not touch the database")
    v = sub.add_parser("verify", help="check labels against stored documents (hashes, quotes, spans)")
    v.add_argument("--dataset-version", required=True)
    v.add_argument("--max-docs", type=int, default=None)
    args = p.parse_args(argv)

    if args.command == "generate":
        from anticipa.datafabric.docgen.generate import generate

        report = generate(
            args.dataset_version,
            seed=args.seed,
            residents=args.residents,
            source=args.source,
            chunk_size=args.chunk_size,
            register=args.register,
            as_of=args.as_of,
            enrich=args.enrich,
            family=args.family,
            only_linked=args.only_linked,
            refresh_linked=args.refresh_linked,
        )
        json.dump(report.summary(), sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0
    if args.command == "reset":
        from anticipa.datafabric.docgen.generate import reset_generated
        from anticipa.datafabric.docstore import DocStoreSettings

        store_root = DocStoreSettings().local_dir
        if args.files_only:
            out = reset_generated(args.dataset_version, store_root=store_root, apply=args.yes)
        else:
            from anticipa.db import get_sessionmaker

            with get_sessionmaker()() as session:
                out = reset_generated(
                    args.dataset_version,
                    session=session,
                    store_root=store_root,
                    include_enrichment=not args.keep_enrichment,
                    apply=args.yes,
                )
        json.dump(out, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    from anticipa.datafabric.docgen.groundtruth import verify_ground_truth
    from anticipa.datafabric.docstore import get_document_store

    result = verify_ground_truth(args.dataset_version, get_document_store(), max_docs=args.max_docs)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return (
        1
        if any(
            result.get(k)
            for k in (
                "missing_documents",
                "hash_mismatches",
                "quote_mismatches",
                "span_errors",
                "duplicate_label_ids",
            )
        )
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
