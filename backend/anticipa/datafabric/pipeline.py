"""End-to-end dataset run: generate with Synthea, load every batch as soon as it completes, then link families.

    python -m anticipa.datafabric.pipeline run --residents 100000 --batch-size 500 --base-seed 42 --workers 6 \
        --dataset-version syn-20261108-s42
    python -m anticipa.datafabric.pipeline status --dataset-version syn-20261108-s42

Resumable: re-running the identical command skips generated batches (manifest.json) and loaded batches (datasets.manifest in
the database), regenerates only failed/interrupted batches and re-links families at the end (idempotent).
Run detached with Start-Process (see Docs/benchmarks/); logs go wherever the launcher redirects stdout/stderr.
"""
# ruff: noqa: E501, S608

from __future__ import annotations

import argparse
import json
import queue
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from anticipa.config import DB_SCHEMA
from anticipa.datafabric import synthea
from anticipa.datafabric.family import BLOCK_BATCHES, GT_DIR, link_ready_blocks
from anticipa.datafabric.loader import engine_for, ingest_batch, set_storage_profile
from anticipa.datafabric.normalize import ALL_TYPES
from anticipa.datafabric.profiles import (
    FULL,
    PROFILE_DEFINITIONS,
    RULES,
    load_rule_codes,
    profile_for_batch,
)
from anticipa.datafabric.synthea import DEFAULT_YEARS_OF_HISTORY, SYNTHEA_DIR, load_manifest

INGEST_ATTEMPTS = 6  # backoff 30, 60, 120, 240, 300 s: rides out a ~12 min database outage per batch


def log(msg: str) -> None:
    print(f"{datetime.now(UTC).isoformat(timespec='seconds')} {msg}", flush=True)


def _link_ready(session: Session, version: str, args: argparse.Namespace) -> None:
    """Link every block whose batches are all loaded (cheap no-op otherwise); never raises into the ingest loop."""
    if args.no_link_families:
        return
    try:
        for r in link_ready_blocks(
            session,
            version,
            block_batches=args.block_batches,
            data_dir=args.data_dir,
            gt_root=args.gt_dir,
            log=log,
        ):
            log(
                f"BLOCK {r.block} LINKED: {r.residents} residents, {r.units} families, {r.edges} edges, "
                f"{r.residents_in_families} residents in families ({r.seconds}s)"
            )
    except Exception as exc:  # noqa: BLE001 - re-attempted after the next batch and at the end of the run
        session.rollback()
        log(f"family linking failed (will retry): {type(exc).__name__}: {str(exc)[:300]}")


def _storage_profile_info(args: argparse.Namespace) -> dict:
    rules = load_rule_codes()
    planned = -(-args.residents // args.batch_size)
    return {
        "policy": "lean storage after the first full_residents residents (owner decision 2026-11-11)",
        "full_residents": args.full_residents,
        "full_batches": args.full_batches,
        "batch_size": args.batch_size,
        "profiles": {
            FULL: {"definition": PROFILE_DEFINITIONS[FULL], "batches": [0, min(args.full_batches, planned)]},
            RULES: {"definition": PROFILE_DEFINITIONS[RULES], "batches": [args.full_batches, planned]},
        },
        "rules_file": "config/guidelines/rules_draft.json",
        "rules_digest": rules.digest,
        "rules_codes": len(rules.codes),
        "rules_selectors": rules.selectors,
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def _ingest_worker(engine, version: str, args: argparse.Namespace, q: queue.Queue, failed: list[int]) -> None:
    recorded = False
    with Session(engine) as session:
        while (i := q.get()) is not None:
            profile = profile_for_batch(i, args.full_batches)
            types = ALL_TYPES if args.include_document_references and profile == FULL else None
            for attempt in range(1, INGEST_ATTEMPTS + 1):
                try:
                    r = ingest_batch(
                        session,
                        dataset_version=version,
                        batch_index=i,
                        data_dir=args.data_dir,
                        types=types,
                        profile=profile,
                    )
                    if not recorded:
                        set_storage_profile(session, version, _storage_profile_info(args))
                        recorded = True
                except Exception as exc:  # noqa: BLE001 - a DB blip must not stop generation; the batch is retried/resumed
                    session.rollback()
                    log(
                        f"ingest batch {i} attempt {attempt}/{INGEST_ATTEMPTS} failed: {type(exc).__name__}: {str(exc)[:300]}"
                    )
                    if attempt == INGEST_ATTEMPTS:
                        failed.append(i)
                    else:
                        time.sleep(min(30 * 2 ** (attempt - 1), 300))
                else:
                    if r["skipped"]:
                        log(f"ingest batch {i}: already loaded")
                    else:
                        log(
                            f"ingest batch {i} [{profile}]: {r['residents_inserted']} residents, "
                            f"{r['clinical_inserted']} records (stage {r['stage_s']}s, insert {r['insert_s']}s)"
                        )
                        _link_ready(session, version, args)
                    break


def run(args: argparse.Namespace) -> int:
    engine = engine_for(args.database_url)
    with engine.connect() as c:  # fail fast, before hours of generation, if the DB or schema is not ready
        c.execute(text(f"SELECT 1 FROM {DB_SCHEMA}.datasets LIMIT 1"))
    version = args.dataset_version
    ds_dir = args.data_dir / version
    args.full_batches = -(-args.full_residents // args.batch_size)
    if args.full_batches % args.block_batches:
        raise SystemExit(
            f"--full-residents ({args.full_batches} batches) must be a whole number of family blocks "
            f"({args.block_batches} batches) so a block has a single storage profile"
        )
    q: queue.Queue[int | None] = queue.Queue()
    failed_ingest: list[int] = []
    worker = threading.Thread(
        target=_ingest_worker, args=(engine, version, args, q, failed_ingest), daemon=True
    )
    worker.start()
    existing = load_manifest(ds_dir)
    if existing:
        for i, b in sorted((int(k), v) for k, v in existing["batches"].items()):
            if b["status"] == "completed":
                q.put(i)
    gen_ok = True
    try:
        synthea.generate(
            residents=args.residents,
            batch_size=args.batch_size,
            base_seed=args.base_seed,
            workers=args.workers,
            dataset_version=version,
            years_of_history=args.years_of_history,
            heap_mb=args.heap_mb,
            max_attempts=args.max_attempts,
            batch_timeout_s=args.batch_timeout_s,
            data_dir=args.data_dir,
            log=log,
            on_batch=lambda i, _entry: q.put(i),
        )
    except SystemExit as exc:
        gen_ok = exc.code in (0, None)
        log(
            f"generation finished with failures (exit {exc.code}); loaded batches stay loaded, re-run to retry the rest"
        )
    finally:
        q.put(None)
        worker.join()
    if failed_ingest:
        log(f"ingest failed for batches {sorted(failed_ingest)}; re-run to retry")
    if gen_ok and not failed_ingest:
        with Session(engine) as session:
            _link_ready(session, version, args)
    done = gen_ok and not failed_ingest
    log("PIPELINE COMPLETE" if done else "PIPELINE INCOMPLETE - re-run the same command to resume")
    return 0 if done else 1


def status(args: argparse.Namespace) -> int:
    version = args.dataset_version
    manifest = load_manifest(args.data_dir / version)
    out: dict = {"dataset_version": version}
    if manifest:
        batches = manifest["batches"]
        done = [b for b in batches.values() if b["status"] == "completed"]
        planned = -(-manifest["resident_target"] // manifest["batch_size"])
        out["generation"] = {
            "status": manifest.get("status"),
            "batches_planned": planned,
            "batches_completed": len(done),
            "batches_running": sum(b["status"] == "running" for b in batches.values()),
            "batches_failed": sum(b["status"] == "failed" for b in batches.values()),
            "residents_completed": sum(b["residents"] for b in done),
            "residents_target": manifest["resident_target"],
        }
        if done:
            t0 = min(datetime.fromisoformat(b["started_at"]) for b in done)
            t1 = max(datetime.fromisoformat(b["finished_at"]) for b in done)
            rate = out["generation"]["residents_completed"] / max((t1 - t0).total_seconds(), 1)
            remaining = manifest["resident_target"] - out["generation"]["residents_completed"]
            out["generation"]["residents_per_s"] = round(rate, 2)
            out["generation"]["eta_hours"] = round(remaining / rate / 3600, 1) if rate else None
    try:
        with engine_for(args.database_url).connect() as c:
            row = c.execute(
                text(
                    "SELECT d.status, (SELECT count(*) FROM jsonb_object_keys("
                    "COALESCE(d.manifest->'ingest'->'batches', '{}'::jsonb))) "
                    f"FROM {DB_SCHEMA}.datasets d WHERE d.dataset_version = :v"
                ),
                {"v": version},
            ).first()
            out["database"] = {
                "dataset_status": row[0] if row else None,
                "batches_loaded": row[1] if row else 0,
            }
            if row:
                out["database"]["residents"] = c.execute(
                    text(
                        f"SELECT count(*) FROM {DB_SCHEMA}.residents r JOIN {DB_SCHEMA}.datasets d ON d.id = r.dataset_id "
                        "WHERE d.dataset_version = :v"
                    ),
                    {"v": version},
                ).scalar_one()
                out["database"]["family_edges"] = c.execute(
                    text(
                        f"SELECT count(*) FROM {DB_SCHEMA}.family_edges e JOIN {DB_SCHEMA}.residents r ON r.id = e.resident_id "
                        f"JOIN {DB_SCHEMA}.datasets d ON d.id = r.dataset_id WHERE d.dataset_version = :v"
                    ),
                    {"v": version},
                ).scalar_one()
    except Exception as exc:  # noqa: BLE001 - status must work while the DB is down
        out["database"] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    print(json.dumps(out, indent=2))
    return 0


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m anticipa.datafabric.pipeline")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="generate + load + link (resumable)")
    r.add_argument("--residents", type=int, required=True)
    r.add_argument("--batch-size", type=int, default=500)
    r.add_argument("--base-seed", type=int, default=42)
    r.add_argument("--workers", type=int, default=6)
    r.add_argument("--dataset-version", required=True)
    r.add_argument("--years-of-history", type=int, default=DEFAULT_YEARS_OF_HISTORY)
    r.add_argument("--heap-mb", type=int, default=2048)
    r.add_argument("--max-attempts", type=int, default=3)
    r.add_argument("--batch-timeout-s", type=float, default=14400)
    r.add_argument("--no-link-families", action="store_true")
    r.add_argument(
        "--full-residents",
        type=int,
        default=100_000,
        help="residents (from the start) that keep full detail; later residents load the rules profile",
    )
    r.add_argument(
        "--block-batches",
        type=int,
        default=BLOCK_BATCHES,
        help="batches per family block (default 20 = 10k residents)",
    )
    r.add_argument(
        "--include-document-references", action="store_true", help="also load Synthea DocumentReference stubs"
    )
    r.add_argument("--gt-dir", type=Path, default=GT_DIR)
    r.add_argument("--data-dir", type=Path, default=SYNTHEA_DIR)
    s = sub.add_parser("status", help="progress of a dataset run")
    s.add_argument("--dataset-version", required=True)
    s.add_argument("--data-dir", type=Path, default=SYNTHEA_DIR)
    for sp in (r, s):
        sp.add_argument("--database-url", default=None)
    a = p.parse_args(argv)
    raise SystemExit(run(a) if a.cmd == "run" else status(a))


if __name__ == "__main__":
    main()
