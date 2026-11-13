# ruff: noqa: S608, E501  (identifiers come from constants and the catalogue; every value is a bound parameter)
"""Cloud judging subset (ADR-059/060/064): copy ~1,100 whole family blocks from the local dev database to Supabase.

    uv run --directory backend anticipa datafabric judge-subset [--dry-run]

Re-runnable and replace-on-rerun: the judge dataset (`<source>-judge`) is truncated and re-copied inside ONE target
transaction (a failed run leaves the previous cloud state untouched; the old and new copy coexist until commit, so keep
the dataset under about half of the 500 MB plan). The target is reserved for this dataset: it refuses any other. Re-run it at code freeze, after the rule
versions and the AI extraction outputs exist.

* Selection is deterministic (seeded md5 order). Seeds are the AI evidence cohort residents that have documents;
  each seed pulls its whole family block (connected component of `family_edges`), so pedigrees stay intact.
* Copied: residents, clinical_records, family_edges, documents (+ their text, into the Supabase Storage bucket),
  evidence_facts/evidence_sources (+ the referenced model_invocations), guidelines/guideline_versions/rule_versions,
  eligibility_evaluations, care_gap_cases + case_status_history, and whatever planning/engagement/closure rows exist
  (clinics, services, slots, planning runs, allocations, communications, responses, appointments, completion
  evidence), consent records and the simulated resident conversation threads, and the analytics snapshot rows other
  than `core` (for example the stored impact report; `core` is recomputed on the target so it describes the target's
  own residents: `python -m anticipa.analytics refresh`). `evaluation_runs` only with --with-evaluation-runs.
* Never copied: ground truth (it lives in files, never in a table), audit_events, user_profiles, processing_jobs/items,
  document_chunks, alembic_version. `job_id` columns are nulled (jobs are not copied).
* The source is read in one READ ONLY REPEATABLE READ transaction (a consistent snapshot; no write is possible).
* The estimated size is checked against --max-mb before anything is written (ADR-059).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg import IsolationLevel, sql
from psycopg.types.json import Jsonb
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url

from anticipa.config import DB_SCHEMA, ENV_FILE, get_settings
from anticipa.datafabric.docstore import (
    DocStoreSettings,
    DocumentStore,
    LocalDocumentStore,
    SupabaseStorageDocumentStore,
    content_hash,
)

log = logging.getLogger("anticipa.judge_subset")

DEFAULT_SOURCE_VERSION = "syn-20261108-s42"
TOOL_VERSION = "judge-subset/1"
SIZE_OVERHEAD = 1.6  # heap + index bytes per raw column byte (measured 1.58 on 2026-11-12; the report shows reality)
PLAN_LIMIT_MB = 500  # Supabase Free database size
PEAK_LIMIT_MB = 450  # old + new copy coexist until commit; above this a replace needs --truncate-first
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "host.docker.internal"}
GUARDED_TABLES = (  # immutability triggers (migrations 0002/0006) that would block a replace of approved rows
    ("guideline_versions", "guideline_versions_guard"),
    ("rule_versions", "rule_versions_guard"),
)

_S = DB_SCHEMA
_RES = "resident_id = ANY(%(r)s::uuid[])"
_FACT_IDS = f"SELECT id FROM {_S}.evidence_facts WHERE resident_id = ANY(%(r)s::uuid[])"
_CASE_IDS = f"SELECT id FROM {_S}.care_gap_cases WHERE resident_id = ANY(%(r)s::uuid[])"


class JudgeSubsetError(RuntimeError):
    """A refusal or a failed safety check; the target was not (or is no longer) modified."""


@dataclass(frozen=True)
class Spec:
    table: str
    where: str | None = None  # None: global table, copied whole
    optional: bool = False


# Parents before children (insert order); deletion walks the global tables in reverse.
GLOBAL_TABLES = (
    "guidelines", "guideline_versions", "rule_versions", "clinics", "clinic_services", "capacity_slots",
    "planning_runs", "communication_templates",
)  # fmt: skip
SPECS = (
    *(Spec(t) for t in GLOBAL_TABLES),
    Spec("analytics_snapshots", "key <> 'core'"),  # aggregates only; `core` describes the local population, so it is recomputed
    Spec("residents", "id = ANY(%(r)s::uuid[])"),
    Spec("consent_records", _RES),
    Spec("clinical_records", _RES),
    Spec("family_edges", _RES),
    Spec("documents", _RES),
    Spec("model_invocations", f"id IN (SELECT invocation_id FROM {_S}.evidence_facts WHERE {_RES})"),
    Spec("evidence_facts", _RES),
    Spec("evidence_sources", f"evidence_fact_id IN ({_FACT_IDS})"),
    Spec("eligibility_evaluations", _RES),
    Spec("care_gap_cases", _RES),
    Spec("case_status_history", f"case_id IN ({_CASE_IDS})"),
    Spec("conversation_messages", _RES),
    Spec("planned_allocations", _RES),
    Spec("unallocated_cases", _RES),
    Spec("communication_events", _RES),
    Spec("resident_responses", _RES),
    Spec("appointments", _RES),
    Spec("completion_evidence", f"care_gap_case_id IN ({_CASE_IDS})"),
    Spec("evaluation_runs", optional=True),
)

# Column rewrites applied while reading the source (everything else is copied verbatim).
OVERRIDES = {
    ("residents", "dataset_id"): "%(dataset)s::uuid",
    # `<dataset_version>/<resident>/<document>.txt` -> `<judge_version>/<resident>/<document>.txt`
    ("documents", "storage_key"): "%(judge)s || substr(storage_key, strpos(storage_key, '/'))",
    ("evidence_facts", "subject_person_id"): (
        "CASE WHEN subject_person_id = ANY(%(r)s::uuid[]) THEN subject_person_id END"
    ),
}


class CloudEnv(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    supabase_db_url: SecretStr | None = Field(None, validation_alias="SUPABASE_DB_URL")
    supabase_url: str | None = Field(None, validation_alias="SUPABASE_URL")
    supabase_secret_key: SecretStr | None = Field(None, validation_alias="SUPABASE_SECRET_KEY")


# ------------------------------------------------------------------------------------------------ helpers


def _conninfo(url: str | URL) -> str:
    parsed = make_url(url) if isinstance(url, str) else url
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


def _qualified(table: str) -> sql.Composable:
    return sql.Identifier(_S, table)


def _where(spec: Spec) -> sql.Composable:
    return sql.SQL(f" WHERE {spec.where}") if spec.where else sql.SQL("")


def _columns(conn: psycopg.Connection, table: str) -> list[str]:
    rows = conn.execute(
        "SELECT attname FROM pg_attribute WHERE attrelid = to_regclass(%s) AND attnum > 0 "
        "AND NOT attisdropped AND attgenerated = '' ORDER BY attnum",
        (f"{_S}.{table}",),
    ).fetchall()
    return [r[0] for r in rows]


def _expr(table: str, column: str) -> sql.Composable:
    if column == "job_id":
        return sql.SQL("NULL::uuid")  # jobs are not copied
    override = OVERRIDES.get((table, column))
    return sql.SQL(override) if override else sql.Identifier(column)


def _schema_version(conn: psycopg.Connection) -> str | None:
    row = conn.execute(f"SELECT version_num FROM {_S}.alembic_version").fetchone()
    return row[0] if row else None


# ------------------------------------------------------------------------------------------------ selection


def _family_block(src: psycopg.Connection, resident_id: uuid.UUID, cap: int) -> list[uuid.UUID] | None:
    """All residents connected to `resident_id` through family_edges; None when larger than `cap`."""
    rows = src.execute(
        f"""WITH RECURSIVE c(id) AS (
                SELECT %(id)s::uuid
              UNION
                SELECT CASE WHEN f.resident_id = c.id THEN f.related_resident_id ELSE f.resident_id END
                FROM c JOIN {_S}.family_edges f ON f.resident_id = c.id OR f.related_resident_id = c.id
                WHERE f.related_resident_id IS NOT NULL
            ) SELECT id FROM c LIMIT %(lim)s""",
        {"id": resident_id, "lim": cap + 1},
    ).fetchall()
    return None if len(rows) > cap else [r[0] for r in rows]


def select_residents(
    src: psycopg.Connection,
    source_version: str,
    *,
    seed: int,
    target: int,
    max_residents: int,
    block_cap: int = 200,
) -> tuple[list[uuid.UUID], dict[str, Any]]:
    """Whole family blocks, evidence-cohort-with-documents first, in a seeded md5 order, until `target` residents."""
    has_docs = f"EXISTS (SELECT 1 FROM {_S}.documents d WHERE d.resident_id = r.id)"
    has_facts = f"EXISTS (SELECT 1 FROM {_S}.evidence_facts f WHERE f.resident_id = r.id)"
    stages = (
        ("evidence_cohort_with_documents", f"{has_facts} AND {has_docs}"),
        ("residents_with_documents", has_docs),
        ("any_resident", "TRUE"),
    )
    chosen: dict[uuid.UUID, None] = {}  # insertion-ordered set
    sizes: Counter[int] = Counter()
    skipped = Counter()
    stage_blocks: dict[str, int] = {}
    for name, condition in stages:
        if len(chosen) >= target:
            break
        seeds = src.execute(
            f"""SELECT r.id FROM {_S}.residents r JOIN {_S}.datasets ds ON ds.id = r.dataset_id
                WHERE ds.dataset_version = %(v)s AND {condition}
                ORDER BY md5(%(seed)s || r.id::text) LIMIT %(lim)s""",
            {"v": source_version, "seed": str(seed), "lim": max(50 * target, 1000)},
        ).fetchall()
        stage_blocks[name] = 0
        for (rid,) in seeds:
            if rid in chosen:
                continue
            block = _family_block(src, rid, block_cap)
            if block is None:
                skipped["block_larger_than_cap"] += 1
                continue
            if len(chosen) + len(block) > max_residents:
                skipped["would_exceed_max_residents"] += 1
                continue
            chosen.update(dict.fromkeys(block))
            sizes[len(block)] += 1
            stage_blocks[name] += 1
            if len(chosen) >= target:
                break
    return list(chosen), {
        "seed": seed,
        "blocks_by_stage": stage_blocks,
        "block_size_histogram": dict(sorted(sizes.items())),
        "skipped": dict(skipped),
    }


# ------------------------------------------------------------------------------------------------ estimate


def estimate(
    src: psycopg.Connection, params: dict[str, Any], *, with_evaluation_runs: bool
) -> dict[str, dict[str, int]]:
    """{table: {rows, raw_bytes}} for exactly what the copy will read."""
    out: dict[str, dict[str, int]] = {}
    for spec in SPECS:
        if spec.optional and not with_evaluation_runs:
            continue
        row = src.execute(
            sql.SQL("SELECT count(*), coalesce(sum(pg_column_size(t.*)), 0)::bigint FROM {} t{}").format(
                _qualified(spec.table), _where(spec)
            ),
            params,
        ).fetchone()
        out[spec.table] = {"rows": row[0], "raw_bytes": row[1]}
    return out


# ------------------------------------------------------------------------------------------------ documents


def upload_documents(
    src: psycopg.Connection,
    resident_ids: Sequence[uuid.UUID],
    judge_version: str,
    source_store: DocumentStore,
    target_store: DocumentStore,
    *,
    workers: int = 8,
) -> dict[str, Any]:
    """Copy each document's text to the target store under the judge key; verify sha256 against `content_hash`."""
    rows = src.execute(
        f"SELECT id, storage_key, content_hash FROM {_S}.documents WHERE resident_id = ANY(%s::uuid[])",
        (list(resident_ids),),
    ).fetchall()
    problems: dict[str, list[str]] = {"missing_text": [], "hash_mismatch": []}

    def one(row: tuple[uuid.UUID, str, str]) -> tuple[str, int] | None:
        doc_id, key, expected = row
        try:
            text = source_store.read_text(key)
        except FileNotFoundError:
            problems["missing_text"].append(str(doc_id))
            return None
        if content_hash(text) != expected:
            problems["hash_mismatch"].append(str(doc_id))
            return None
        new_key = judge_version + key[key.index("/") :]
        target_store.write_text(new_key, text)
        return new_key, len(text.encode("utf-8"))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        done = [r for r in pool.map(one, rows) if r is not None]
    if problems["missing_text"] or problems["hash_mismatch"]:
        raise JudgeSubsetError(
            f"{len(problems['missing_text'])} document text(s) missing and "
            f"{len(problems['hash_mismatch'])} with a hash mismatch in the source store "
            f"(first ids: {(problems['missing_text'] + problems['hash_mismatch'])[:3]}); nothing was loaded"
        )
    return {"keys": {k for k, _ in done}, "bytes": sum(n for _, n in done), "count": len(done)}


# ------------------------------------------------------------------------------------------------ replace


def _copy_table(
    src: psycopg.Connection, dst: psycopg.Connection, spec: Spec, params: dict[str, Any]
) -> tuple[int, list[str]]:
    src_cols, dst_cols = _columns(src, spec.table), set(_columns(dst, spec.table))
    cols = [c for c in src_cols if c in dst_cols]
    drift = [f"{spec.table}.{c}" for c in src_cols if c not in dst_cols]
    drift += [f"{spec.table}.{c} (target only)" for c in sorted(dst_cols - set(src_cols))]
    select = sql.SQL("SELECT {} FROM {}{}").format(
        sql.SQL(", ").join(_expr(spec.table, c) for c in cols), _qualified(spec.table), _where(spec)
    )
    copy_in = sql.SQL("COPY {} ({}) FROM STDIN").format(
        _qualified(spec.table), sql.SQL(", ").join(sql.Identifier(c) for c in cols)
    )
    with src.cursor() as scur, dst.cursor() as dcur:
        with scur.copy(sql.SQL("COPY ({}) TO STDOUT").format(select), params) as out, dcur.copy(copy_in) as inn:
            for block in out:
                inn.write(block)
        return dcur.rowcount, drift


def _clear_target(cur: psycopg.Cursor, judge_version: str, with_evaluation_runs: bool) -> tuple[set[str], list[uuid.UUID]]:
    """Refuse a target holding another dataset; return the old document keys; empty every copied table."""
    others = cur.execute(
        f"SELECT count(*) FROM {_S}.datasets WHERE dataset_version <> %s", (judge_version,)
    ).fetchone()[0]
    if others:
        raise JudgeSubsetError(
            f"the target holds {others} other dataset(s); the judging database is reserved for the "
            "judge subset, so nothing was changed"
        )
    judge = f"(SELECT id FROM {_S}.datasets WHERE dataset_version = %s)"
    old_keys = {
        r[0]
        for r in cur.execute(
            f"SELECT storage_key FROM {_S}.documents WHERE resident_id IN "
            f"(SELECT id FROM {_S}.residents WHERE dataset_id = {judge})",
            (judge_version,),
        )
    }
    old_invocations = [
        r[0]
        for r in cur.execute(
            f"SELECT DISTINCT invocation_id FROM {_S}.evidence_facts WHERE invocation_id IS NOT NULL "
            f"AND resident_id IN (SELECT id FROM {_S}.residents WHERE dataset_id = {judge})",
            (judge_version,),
        )
    ]
    # TRUNCATE, not DELETE: it frees the old files at commit, so repeated re-runs do not bloat the 500 MB free tier
    # (a DELETE leaves dead tuples until vacuum and roughly doubled the size). CASCADE also empties document_chunks
    # (it references documents) and any other dependent table. Immutability triggers do not fire on TRUNCATE.
    tables = [s.table for s in SPECS if s.table != "model_invocations" and (with_evaluation_runs or not s.optional)]
    cur.execute(
        sql.SQL("TRUNCATE TABLE {} CASCADE").format(sql.SQL(", ").join(_qualified(t) for t in [*tables, "datasets"]))
    )
    if old_invocations:
        cur.execute(f"DELETE FROM {_S}.model_invocations WHERE id = ANY(%s::uuid[])", (old_invocations,))
    return old_keys, old_invocations


def replace_in_target(
    src: psycopg.Connection,
    dst: psycopg.Connection,
    *,
    source_version: str,
    judge_version: str,
    resident_ids: Sequence[uuid.UUID],
    expected: dict[str, dict[str, int]],
    selection: dict[str, Any],
    with_evaluation_runs: bool,
    truncate_first: bool = False,
) -> dict[str, Any]:
    """Replace the judge dataset: clear the copied tables, then copy everything, in one target transaction.

    `truncate_first` clears in an earlier, committed transaction (space is freed at once, so peak size is just the new
    copy) at the price of atomicity: a failed run then leaves the target empty instead of unchanged."""
    src_ds = src.execute(
        f"SELECT generator, generator_version, base_seed, config_hash, status FROM {_S}.datasets "
        "WHERE dataset_version = %s",
        (source_version,),
    ).fetchone()
    copied: dict[str, int] = {}
    drift: list[str] = []
    freed_keys: set[str] = set()
    if truncate_first:
        with dst.transaction(), dst.cursor() as cur:
            freed_keys, _ = _clear_target(cur, judge_version, with_evaluation_runs)
    with dst.transaction(), dst.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = 0")
        old_keys, _ = _clear_target(cur, judge_version, with_evaluation_runs)
        old_keys |= freed_keys
        # The immutability guards refuse rule rows under an approved guideline version (they must be copied
        # as they are); they are re-enabled before commit.
        for table, trigger in GUARDED_TABLES:
            cur.execute(sql.SQL("ALTER TABLE {} DISABLE TRIGGER {}").format(_qualified(table), sql.Identifier(trigger)))

        manifest = {
            "judge_subset": {
                "tool": TOOL_VERSION,
                "source_dataset_version": source_version,
                "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "residents": len(resident_ids),
                "selection": selection,
                "rows_expected": {t: v["rows"] for t, v in expected.items()},
                "note": "Synthetic residents only; ground truth is never exported.",
            }
        }
        dataset_id = cur.execute(
            f"INSERT INTO {_S}.datasets (dataset_version, generator, generator_version, base_seed, config_hash, "
            "resident_target, status, manifest) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                judge_version,
                src_ds[0],
                src_ds[1],
                src_ds[2],
                src_ds[3],
                len(resident_ids),
                src_ds[4],
                Jsonb(manifest),
            ),
        ).fetchone()[0]

        params = {"r": list(resident_ids), "dataset": dataset_id, "judge": judge_version}
        for spec in SPECS:
            if spec.optional and not with_evaluation_runs:
                continue
            copied[spec.table], table_drift = _copy_table(src, dst, spec, params)
            drift += table_drift
            if copied[spec.table] != expected[spec.table]["rows"]:
                raise JudgeSubsetError(
                    f"{spec.table}: copied {copied[spec.table]} rows, expected {expected[spec.table]['rows']}; "
                    "the target transaction was rolled back"
                )
            log.info("copied %-26s %9d rows", spec.table, copied[spec.table])
        for table, trigger in GUARDED_TABLES:
            cur.execute(sql.SQL("ALTER TABLE {} ENABLE TRIGGER {}").format(_qualified(table), sql.Identifier(trigger)))
        new_keys = {
            r[0]
            for r in cur.execute(
                f"SELECT storage_key FROM {_S}.documents WHERE resident_id = ANY(%s::uuid[])",
                (list(resident_ids),),
            )
        }
    return {"rows": copied, "schema_drift": drift, "stale_document_keys": sorted(old_keys - new_keys)}


# ------------------------------------------------------------------------------------------------ orchestration


def run(
    src: psycopg.Connection,
    dst: psycopg.Connection,
    *,
    source_store: DocumentStore,
    target_store: DocumentStore,
    source_version: str = DEFAULT_SOURCE_VERSION,
    judge_version: str | None = None,
    seed: int = 42,
    target: int = 1100,
    max_residents: int = 1200,
    max_mb: float = 300.0,
    dry_run: bool = False,
    with_evaluation_runs: bool = False,
    workers: int = 8,
    truncate_first: bool = False,
) -> dict[str, Any]:
    judge_version = judge_version or f"{source_version}-judge"
    if judge_version == source_version:
        raise JudgeSubsetError("the judge dataset version must differ from the source dataset version")
    if _schema_version(src) != _schema_version(dst):
        raise JudgeSubsetError(
            f"schema versions differ (source {_schema_version(src)}, target {_schema_version(dst)}); "
            "run `alembic upgrade head` on the target first"
        )
    if src.execute(f"SELECT 1 FROM {_S}.datasets WHERE dataset_version = %s", (source_version,)).fetchone() is None:
        raise JudgeSubsetError(f"source dataset {source_version!r} not found")

    resident_ids, selection = select_residents(
        src, source_version, seed=seed, target=target, max_residents=max_residents
    )
    if not resident_ids:
        raise JudgeSubsetError("selection is empty")
    params = {"r": resident_ids, "dataset": uuid.UUID(int=0), "judge": judge_version}
    expected = estimate(src, params, with_evaluation_runs=with_evaluation_runs)
    est_mb = SIZE_OVERHEAD * sum(v["raw_bytes"] for v in expected.values()) / 2**20
    cloud_mb = dst.execute("SELECT pg_database_size(current_database())").fetchone()[0] / 2**20
    report: dict[str, Any] = {
        "dry_run": dry_run,
        "source_dataset_version": source_version,
        "judge_dataset_version": judge_version,
        "residents": len(resident_ids),
        "selection": selection,
        "rows_expected": {t: v["rows"] for t, v in expected.items()},
        "raw_mb": round(sum(v["raw_bytes"] for v in expected.values()) / 2**20, 1),
        "estimated_db_mb": round(est_mb, 1),
        "target_db_mb_before": round(cloud_mb, 1),
        "max_mb": max_mb,
    }
    if est_mb > max_mb:
        raise JudgeSubsetError(
            f"estimated {est_mb:.0f} MB exceeds the {max_mb:.0f} MB budget (ADR-059); lower --target or raise --max-mb"
        )
    report["peak_mb_estimate"] = round(cloud_mb + est_mb, 1)
    if not truncate_first and cloud_mb + est_mb > PEAK_LIMIT_MB:
        raise JudgeSubsetError(
            f"replacing in one transaction would peak near {cloud_mb + est_mb:.0f} MB (old {cloud_mb:.0f} + new "
            f"{est_mb:.0f}) on a {PLAN_LIMIT_MB} MB plan; re-run with --truncate-first (empties the target first)"
        )
    if dry_run:
        return report

    docs = upload_documents(src, resident_ids, judge_version, source_store, target_store, workers=workers)
    result = replace_in_target(
        src,
        dst,
        source_version=source_version,
        judge_version=judge_version,
        resident_ids=resident_ids,
        expected=expected,
        selection=selection,
        with_evaluation_runs=with_evaluation_runs,
        truncate_first=truncate_first,
    )
    stale = [k for k in result.pop("stale_document_keys") if k not in docs["keys"]]
    deleter = getattr(target_store, "delete_many", None)
    removed = deleter(stale) if deleter and stale else 0
    actual_mb = dst.execute(
        "SELECT coalesce(sum(pg_total_relation_size(c.oid)), 0) FROM pg_class c "
        "WHERE c.relnamespace = %s::regnamespace AND c.relkind = 'r'",
        (_S,),
    ).fetchone()[0]
    report.update(
        rows_copied=result["rows"],
        schema_drift=result["schema_drift"],
        documents_uploaded=docs["count"],
        document_bytes=docs["bytes"],
        stale_documents_removed=removed,
        app_schema_mb_after=round(actual_mb / 2**20, 1),
        target_db_mb_after=round(dst.execute("SELECT pg_database_size(current_database())").fetchone()[0] / 2**20, 1),
    )
    return report


# ------------------------------------------------------------------------------------------------ CLI


def _same_database(a: URL, b: URL) -> bool:
    return (a.host, a.port or 5432, a.database) == (b.host, b.port or 5432, b.database)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="anticipa datafabric judge-subset",
        description="Copy a representative family-block subset of the local dataset to the Supabase judging database.",
    )
    p.add_argument("--source-version", default=DEFAULT_SOURCE_VERSION)
    p.add_argument("--judge-version", default=None, help="default: <source-version>-judge")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--target", type=int, default=1100, help="stop adding family blocks at this many residents")
    p.add_argument("--max-residents", type=int, default=1200, help="hard cap (a block that would exceed it is skipped)")
    p.add_argument("--max-mb", type=float, default=300.0, help="refuse when the estimated database size exceeds this")
    p.add_argument("--workers", type=int, default=8, help="parallel document uploads")
    p.add_argument("--dry-run", action="store_true", help="select and estimate only; write nothing")
    p.add_argument(
        "--truncate-first", action="store_true", help="empty the target in its own transaction first (needed when old+new would exceed ~450 MB)"
    )
    p.add_argument("--with-evaluation-runs", action="store_true", help="also copy evaluation_runs (aggregates)")
    p.add_argument("--allow-local-target", action="store_true", help="permit a localhost target (manual testing)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    env = CloudEnv()
    if env.supabase_db_url is None:
        print("judge-subset: SUPABASE_DB_URL is not set", file=sys.stderr)
        return 2
    src_url = make_url(get_settings().sqlalchemy_url())
    dst_url = make_url(env.supabase_db_url.get_secret_value())
    if _same_database(src_url, dst_url):
        print("judge-subset: refusing to use the same database as source and target", file=sys.stderr)
        return 2
    if (dst_url.host in LOCAL_HOSTS or (dst_url.host or "").startswith("127.")) and not args.allow_local_target:
        print("judge-subset: the target is a local database; pass --allow-local-target if intended", file=sys.stderr)
        return 2
    if not (env.supabase_url and env.supabase_secret_key):
        print("judge-subset: SUPABASE_URL and SUPABASE_SECRET_KEY are needed for document storage", file=sys.stderr)
        return 2
    docs = DocStoreSettings()
    target_store = SupabaseStorageDocumentStore(env.supabase_url, env.supabase_secret_key.get_secret_value(), docs.bucket)
    if not args.dry_run:
        target_store.ensure_bucket()
    with psycopg.connect(_conninfo(src_url)) as src, psycopg.connect(_conninfo(dst_url), autocommit=True) as dst:
        src.isolation_level = IsolationLevel.REPEATABLE_READ
        src.read_only = True  # no write is possible on the dev database
        # The cloud role has a 30 s statement_timeout (SEC-05) that protects the API; this maintenance session lifts it for
        # its own connection only (a pooler ignores startup options, so a session-level SET; a fresh session is unaffected).
        dst.execute("SET statement_timeout = 0")
        try:
            report = run(
                src,
                dst,
                source_store=LocalDocumentStore(Path(docs.local_dir)),
                target_store=target_store,
                source_version=args.source_version,
                judge_version=args.judge_version,
                seed=args.seed,
                target=args.target,
                max_residents=args.max_residents,
                max_mb=args.max_mb,
                dry_run=args.dry_run,
                with_evaluation_runs=args.with_evaluation_runs,
                workers=args.workers,
                truncate_first=args.truncate_first,
            )
        except JudgeSubsetError as exc:
            print(f"judge-subset: {exc}", file=sys.stderr)
            return 2
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
