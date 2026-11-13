"""Bulk loader: normalized Synthea batches -> `anticipa.residents` / `anticipa.clinical_records` (ADR-048).

Per batch, in ONE transaction: COPY the normalizer's rows into TEMP staging tables (typed, no indexes), then
`INSERT ... SELECT ... ON CONFLICT DO NOTHING` into the real tables. Re-running a batch is therefore a no-op, and a
batch that already completed (same generation sha recorded in `datasets.manifest`) is skipped outright.
Memory stays bounded: rows are streamed from the gzip NDJSON through the normalizer into the COPY stream.

    uv run --directory backend python -m anticipa.datafabric.loader ingest --dataset-version V \
        [--batch 0 --batch 1] [--database-url postgresql://...] [--force]
"""
# ruff: noqa: E501, S608

from __future__ import annotations

import argparse
import hashlib
import json
import time
import uuid
from datetime import UTC, datetime
from math import ceil
from pathlib import Path

import orjson
import psutil
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from anticipa.config import DB_SCHEMA, get_settings, normalize_db_url
from anticipa.datafabric.models import Dataset
from anticipa.datafabric.normalize import ALL_TYPES, CLINICAL_COLUMNS, RESIDENT_COLUMNS, BatchNormalizer
from anticipa.datafabric.profiles import FULL, PROFILES, RuleCodes, load_rule_codes
from anticipa.datafabric.synthea import SYNTHEA_DIR, load_manifest, sorted_batches
from anticipa.db import make_engine

S = DB_SCHEMA
_STG_RESIDENTS = """CREATE TEMP TABLE stg_residents (
    id uuid, source_system text, source_patient_id text, birth_date date, deceased_date date,
    administrative_sex text, preferred_language text, district_code text, source_version text
) ON COMMIT DROP"""
_STG_CLINICAL = """CREATE TEMP TABLE stg_clinical (
    resident_id uuid, resource_type text, source_resource_id text, source_system text, category text,
    code_system text, code text, display text, effective_start timestamptz, effective_end timestamptz,
    status text, value_json jsonb, source_reference text
) ON COMMIT DROP"""
_INS_RESIDENTS = f"""INSERT INTO {S}.residents (id, dataset_id, source_system, source_patient_id, birth_date, deceased_date,
    administrative_sex, preferred_language, district_code, source_version, synthetic_status)
SELECT id, %s, source_system, source_patient_id, birth_date, deceased_date, administrative_sex, preferred_language,
    district_code, source_version, 'synthetic' FROM stg_residents
ON CONFLICT (source_system, source_patient_id) DO NOTHING"""
# Row ids are time-prefixed (12 hex chars of epoch-ms + the rest of a random v4 uuid) so the primary-key index is
# append-mostly instead of random-insert; ORDER BY keeps each resident's rows adjacent in the two resident_id-led
# indexes. Both matter once the table holds tens of millions of rows. Ids stay valid uuids.
_REPROFILE_RESIDENTS = f"""UPDATE {S}.residents r SET source_version = s.source_version
FROM stg_residents s WHERE r.source_system = s.source_system AND r.source_patient_id = s.source_patient_id
  AND r.source_version IS DISTINCT FROM s.source_version"""
_INS_CLINICAL = f"""INSERT INTO {S}.clinical_records (id, {", ".join(CLINICAL_COLUMNS)}, synthetic_status)
SELECT (lpad(to_hex((extract(epoch FROM clock_timestamp()) * 1000)::bigint), 12, '0')
        || substr(replace(gen_random_uuid()::text, '-', ''), 13))::uuid,
    {", ".join(CLINICAL_COLUMNS)}, 'synthetic'
FROM stg_clinical ORDER BY resident_id, category, code
ON CONFLICT (resident_id, resource_type, source_resource_id) DO NOTHING"""


def generation_sha(batch: dict) -> str:
    """Fingerprint of a batch's generated files (from the generation manifest); changes iff the output changes."""
    blob = orjson.dumps({n: f["sha256"] for n, f in sorted(batch["files"].items())})
    return hashlib.sha256(blob).hexdigest()


def _generation_summary(manifest: dict) -> dict:
    keys = (
        "dataset_version",
        "generator",
        "generator_version",
        "jar_sha256",
        "jre_version",
        "script_version",
        "base_seed",
        "batch_size",
        "reference_date",
        "years_of_history",
        "config_hash",
        "assignment_config",
        "properties_file",
        "created_at",
    )
    out = {k: manifest.get(k) for k in keys}
    out["batches_planned"] = ceil(manifest["resident_target"] / manifest["batch_size"])
    out["resident_target"] = manifest["resident_target"]
    return out


def upsert_dataset(session: Session, manifest: dict, *, status: str = "loading") -> uuid.UUID:
    """Create or refresh the `datasets` row for a generation manifest; keeps any recorded `ingest` progress."""
    gen = _generation_summary(manifest)
    stmt = pg_insert(Dataset).values(
        dataset_version=manifest["dataset_version"],
        generator=manifest.get("generator", "synthea"),
        generator_version=manifest.get("generator_version"),
        base_seed=manifest.get("base_seed"),
        config_hash=manifest.get("config_hash"),
        resident_target=manifest.get("resident_target"),
        status=status,
        manifest={"generation": gen, "ingest": {"batches": {}}},
    )
    excluded = stmt.excluded
    stmt = stmt.on_conflict_do_update(
        index_elements=["dataset_version"],
        set_={
            "generator_version": excluded.generator_version,
            "config_hash": excluded.config_hash,
            "resident_target": excluded.resident_target,
            "manifest": text(
                f"jsonb_set({S}.datasets.manifest, '{{generation}}', excluded.manifest->'generation')"
            ),
        },
    ).returning(Dataset.id)
    dataset_id = session.execute(stmt).scalar_one()
    session.commit()
    return dataset_id


def _record_batch(session: Session, dataset_id: uuid.UUID, index: int, entry: dict) -> str:
    """Read-modify-write of datasets.manifest under a row lock; returns the new dataset status."""
    row = session.execute(
        text(f"SELECT manifest FROM {S}.datasets WHERE id = :id FOR UPDATE"), {"id": dataset_id}
    ).one()
    manifest = dict(row[0])
    batches = manifest.setdefault("ingest", {}).setdefault("batches", {})
    batches[str(index)] = entry
    planned = (manifest.get("generation") or {}).get("batches_planned")
    status = "loaded" if planned and len(batches) >= planned else "loading"
    manifest["ingest"]["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    session.execute(
        text(f"UPDATE {S}.datasets SET manifest = CAST(:m AS jsonb), status = :s WHERE id = :id"),
        {"m": orjson.dumps(manifest).decode(), "s": status, "id": dataset_id},
    )
    return status


def set_storage_profile(session: Session, dataset_version: str, info: dict) -> None:
    """Record the dataset's storage-profile policy in datasets.manifest.storage_profile (read-modify-write, row lock)."""
    row = session.execute(
        text(f"SELECT id, manifest FROM {S}.datasets WHERE dataset_version = :v FOR UPDATE"),
        {"v": dataset_version},
    ).first()
    if row is None:
        raise ValueError(f"unknown dataset {dataset_version!r}")
    manifest = dict(row[1])
    manifest["storage_profile"] = info
    session.execute(
        text(f"UPDATE {S}.datasets SET manifest = CAST(:m AS jsonb) WHERE id = :id"),
        {"m": orjson.dumps(manifest).decode(), "id": row[0]},
    )
    session.commit()


def ingested_batches(session: Session, dataset_version: str) -> dict[str, dict]:
    row = session.execute(
        text(f"SELECT manifest->'ingest'->'batches' FROM {S}.datasets WHERE dataset_version = :v"),
        {"v": dataset_version},
    ).first()
    return dict(row[0]) if row and row[0] else {}


def ingest_batch(
    session: Session,
    *,
    dataset_version: str,
    batch_index: int,
    manifest: dict | None = None,
    data_dir: Path = SYNTHEA_DIR,
    force: bool = False,
    types: tuple[str, ...] | None = None,
    profile: str = FULL,
    rule_codes: RuleCodes | None = None,
) -> dict:
    """Load one generated batch under a storage `profile` (see profiles.py). Returns a stats dict, also stored in
    datasets.manifest.ingest.batches[index]. A batch is skipped only if it was loaded from the same generated files
    under the same profile and the same guideline code set (so legacy entries without a profile are reloaded once)."""
    rule_codes = rule_codes or load_rule_codes()
    ds_dir = Path(data_dir) / dataset_version
    manifest = manifest or load_manifest(ds_dir)
    if manifest is None:
        raise FileNotFoundError(f"no manifest.json in {ds_dir}")
    batch = manifest["batches"].get(str(batch_index))
    if not batch or batch["status"] != "completed":
        raise ValueError(f"{dataset_version} batch {batch_index} is not a completed batch")
    sha = generation_sha(batch)

    dataset_id = upsert_dataset(session, manifest)
    if not force:
        done = ingested_batches(session, dataset_version).get(str(batch_index))
        if (
            done
            and done.get("generation_sha") == sha
            and done.get("profile") == profile
            and done.get("rules_digest") == rule_codes.digest
        ):
            return {
                "batch": batch_index,
                "skipped": True,
                "reason": "already ingested",
                **{k: done[k] for k in ("residents_inserted", "clinical_inserted") if k in done},
            }

    t0 = time.perf_counter()
    norm = BatchNormalizer(
        dataset_version,
        ds_dir / f"batch-{batch_index}",
        source_version=manifest.get("generator_version"),
        profile=profile,
        rule_codes=rule_codes,
    )
    conn = session.connection()
    raw = conn.connection.driver_connection  # psycopg3 connection inside the SQLAlchemy transaction
    try:
        with raw.cursor() as cur:
            cur.execute("SET LOCAL synchronous_commit = off")  # bulk load; a crash simply re-runs the batch
            cur.execute(
                "DROP TABLE IF EXISTS pg_temp.stg_residents, pg_temp.stg_clinical"
            )  # savepoint-wrapped callers
            cur.execute(_STG_RESIDENTS)
            cur.execute(_STG_CLINICAL)
            with cur.copy(f"COPY stg_residents ({', '.join(RESIDENT_COLUMNS)}) FROM STDIN") as cp:
                for row in norm.residents():
                    cp.write_row(row)
            n_res_staged = cur.rowcount
            with cur.copy(f"COPY stg_clinical ({', '.join(CLINICAL_COLUMNS)}) FROM STDIN") as cp:
                for row in norm.clinical_records(types):
                    cp.write_row(row)
            n_clin_staged = cur.rowcount
            t1 = time.perf_counter()
            cur.execute(_INS_RESIDENTS, (dataset_id,))
            n_res = cur.rowcount
            cur.execute(
                _REPROFILE_RESIDENTS
            )  # a batch re-loaded under another profile updates the resident marker
            n_reprofiled = cur.rowcount
            cur.execute(_INS_CLINICAL)
            n_clin = cur.rowcount
            t2 = time.perf_counter()
    except BaseException:
        session.rollback()
        raise
    finally:
        norm.close()

    stats = norm.stats.to_dict()
    entry = {
        "generation_sha": sha,
        "residents_staged": n_res_staged,
        "residents_inserted": n_res,
        "residents_reprofiled": n_reprofiled,
        "profile": profile,
        "rules_digest": rule_codes.digest,
        "clinical_staged": n_clin_staged,
        "clinical_inserted": n_clin,
        "emitted": stats["emitted"],
        "filtered_out": stats["skipped"],
        "quarantined": stats["quarantined"],
        "stage_s": round(t1 - t0, 2),
        "insert_s": round(t2 - t1, 2),
        "loaded_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    _record_batch(session, dataset_id, batch_index, entry)
    session.commit()
    return {"batch": batch_index, "skipped": False, **entry, "lines_read": sum(stats["read"].values())}


def ingest_dataset(
    session: Session,
    dataset_version: str,
    *,
    batches: list[int] | None = None,
    data_dir: Path = SYNTHEA_DIR,
    force: bool = False,
    types: tuple[str, ...] | None = None,
    profile: str = FULL,
    log=lambda m: None,
) -> list[dict]:
    manifest = load_manifest(Path(data_dir) / dataset_version)
    if manifest is None:
        raise FileNotFoundError(f"no manifest for {dataset_version}")
    indices = (
        batches
        if batches is not None
        else [i for i, b in sorted_batches(manifest) if b["status"] == "completed"]
    )
    out = []
    for i in indices:
        r = ingest_batch(
            session,
            dataset_version=dataset_version,
            batch_index=i,
            manifest=manifest,
            data_dir=data_dir,
            force=force,
            types=types,
            profile=profile,
        )
        log(
            f"ingest batch {i}: "
            + (
                "skipped"
                if r["skipped"]
                else f"{r['residents_inserted']} residents, {r['clinical_inserted']} records in {r['stage_s'] + r['insert_s']:.1f}s"
            )
        )
        out.append(r)
    return out


def engine_for(database_url: str | None) -> Engine:
    url = normalize_db_url(database_url) if database_url else get_settings().sqlalchemy_url()
    return make_engine(url, pool_size=2, max_overflow=0)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m anticipa.datafabric.loader")
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("ingest", help="load completed batches of a generated dataset")
    g.add_argument("--dataset-version", required=True)
    g.add_argument(
        "--batch", type=int, action="append", help="batch index (repeatable); default all completed"
    )
    g.add_argument("--database-url", default=None, help="override the configured database (never printed)")
    g.add_argument("--data-dir", type=Path, default=SYNTHEA_DIR)
    g.add_argument("--force", action="store_true", help="re-run batches already recorded as ingested")
    g.add_argument(
        "--include-document-references", action="store_true", help="also load Synthea DocumentReference stubs"
    )
    g.add_argument("--profile", choices=PROFILES, default=FULL, help="storage profile (see profiles.py)")
    a = p.parse_args(argv)
    engine = engine_for(a.database_url)
    t0 = time.monotonic()
    with Session(engine) as session:
        results = ingest_dataset(
            session,
            a.dataset_version,
            batches=a.batch,
            data_dir=a.data_dir,
            force=a.force,
            types=ALL_TYPES if a.include_document_references else None,
            profile=a.profile,
            log=lambda m: print(m, flush=True),
        )
    mi = psutil.Process().memory_info()
    done = [r for r in results if not r["skipped"]]
    print(
        json.dumps(
            {
                "dataset_version": a.dataset_version,
                "batches": len(results),
                "skipped": len(results) - len(done),
                "residents_inserted": sum(r["residents_inserted"] for r in done),
                "clinical_inserted": sum(r["clinical_inserted"] for r in done),
                "wall_s": round(time.monotonic() - t0, 1),
                "peak_rss_mb": round(getattr(mi, "peak_wset", mi.rss) / 2**20, 1),
            }
        )
    )


if __name__ == "__main__":
    main()
