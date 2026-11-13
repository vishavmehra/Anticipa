# ruff: noqa: E501
"""Resident contexts: the structured facts the document generator plants from, plus the loaders that build them.

A ResidentContext holds only the *bounded* subset of a resident's structured history that documents mention
(recent conditions, medications, screening procedures, latest value per allow-listed observation, last encounter
date). Both loaders feed `ContextBuilder`, so the database path and the on-disk Synthea path behave identically.

Family relatives: `data/ground_truth/<V>/families/family_graph.jsonl` (gt.family_edge.v1) is read when it exists
(Docs/09 §6.4.3). `family_edges` in the database is deliberately not used: its direction semantics are not fixed
by the contract, and a wrongly oriented edge would plant a false relationship. Relatives with no graph entry are
synthesised deterministically as document-only relatives (planner.py) and recorded as such in the ground truth.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import orjson
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from anticipa.config import ROOT_DIR
from anticipa.datafabric.docgen.phrases import RECORD_TAG_TO_PROC
from anticipa.datafabric.models import ClinicalRecord, Dataset, Resident

DATASET_VERSION_RE = re.compile(r"^syn-(\d{4})(\d{2})(\d{2})-s(\d+)$")
GROUND_TRUTH_DIR = ROOT_DIR / "data" / "ground_truth"
_NON_CONDITION = (
    "(finding)",
    "(situation)",
    "(procedure)",
    "(regime/therapy)",
    "(person)",
    "(social concept)",
)
OBS_TAGS = frozenset(
    {
        "blood_pressure",
        "bmi",
        "hba1c",
        "glucose",
        "fasting_glucose",
        "total_cholesterol",
        "hdl_cholesterol",
        "ldl_cholesterol",
        "triglycerides",
        "psa",
        "fit_fobt",
        "cervical_cytology",
        "hpv_test",
    }
)
SCREENING_PROC_TAGS = frozenset(
    {
        "colonoscopy",
        "mammography",
        "fobt_screening",
        "hysterectomy",
        "total_colectomy",
        "bilateral_mastectomy",
    }
)  # screening completions and the exclusions that end screening
ENRICHMENT_SOURCE = "anticipa_enrichment"  # clinical_records.source_system of rows planted by enrich.py
KEEP_MEDS = 5


@dataclass
class Rec:
    category: str
    code_system: str | None
    code: str | None
    display: str
    start: date | None
    end: date | None
    status: str | None
    tag: str | None
    value: dict
    source_id: str | None
    enriched: bool = (
        False  # planted by enrich.py (re-derived on every run, never read back from the database)
    )


@dataclass
class Relative:
    key: str  # stable across documents of one resident
    relationship: str
    sex: str
    side: str | None
    birth_year: int
    resident_id: uuid.UUID | None = None  # set only for a relative who is a resident in the graph
    edge_id: uuid.UUID | None = None
    provenance: str = "document_only"
    decoy: bool = False


@dataclass
class ResidentContext:
    id: uuid.UUID
    source_patient_id: str
    birth_date: date
    sex: str
    language: str
    district: str | None = None
    deceased: date | None = None
    conditions: list[Rec] = field(default_factory=list)
    medications: list[Rec] = field(default_factory=list)
    procedures: list[Rec] = field(default_factory=list)
    observations: dict[str, Rec] = field(default_factory=dict)
    last_encounter: date | None = None
    graph_relatives: list[Relative] = field(default_factory=list)
    note_only: list = field(
        default_factory=list
    )  # EnrichedItem documented only in a note (no structured record)
    linked: bool = True  # in a block the family linker has processed: family-dependent facts are allowed


def dataset_date(dataset_version: str) -> date:
    """Reference date encoded in `syn-YYYYMMDD-sN` (the evaluation clock never uses the wall clock)."""
    m = DATASET_VERSION_RE.match(dataset_version)
    if not m:
        raise ValueError(f"dataset_version must look like syn-YYYYMMDD-sN: {dataset_version!r}")
    return date(int(m[1]), int(m[2]), int(m[3]))


def _day(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not hasattr(value, "hour"):
        return value
    return date.fromisoformat(str(value)[:10]) if isinstance(value, str) else value.date()


def clean_condition(display: str) -> str:
    return re.sub(r"\s*\((disorder|morphologic abnormality)\)\s*$", "", display).strip()


def _order_key(start, code, display, source_id) -> tuple:
    return (start, code or "", display or "", source_id or "")


def add_record(
    ctx: ResidentContext, category: str, code_system, code, display, start, end, status, value, source_id
) -> None:
    """Apply the bounding rules: documents never need a resident's whole history."""
    start_d, end_d = _day(start), _day(end)
    value = value or {}
    tag = value.get("tag")
    if category == "condition":
        if display and not display.endswith(_NON_CONDITION) and start_d:
            ctx.conditions.append(
                Rec(category, code_system, code, display, start_d, end_d, status, None, value, source_id)
            )
    elif category == "medication":
        if display and start_d:
            ctx.medications.append(
                Rec(category, code_system, code, display, start_d, end_d, status, None, value, source_id)
            )
    elif category == "procedure":
        if tag in SCREENING_PROC_TAGS and start_d:
            ctx.procedures.append(
                Rec(category, code_system, code, display, start_d, end_d, status, tag, value, source_id)
            )
    elif category == "observation":
        if tag in OBS_TAGS and start_d:
            old = ctx.observations.get(tag)
            if old is None or _order_key(start_d, code, display, source_id) > _order(old):
                ctx.observations[tag] = Rec(
                    category, code_system, code, display, start_d, None, status, tag, value, source_id
                )
    elif category == "encounter" and start_d and (ctx.last_encounter is None or start_d > ctx.last_encounter):
        ctx.last_encounter = start_d


def _order(r: Rec) -> tuple:
    """Total order for records: dates tie constantly in real data, and the database returns ties in any order."""
    return (r.start, r.code or "", r.display or "", r.source_id or "")


def finalize(ctx: ResidentContext, as_of: date) -> ResidentContext:
    """Drop records dated after `as_of`, order newest first, trim medications. Deterministic whatever the row order."""
    ctx.conditions = sorted((r for r in ctx.conditions if r.start <= as_of), key=_order, reverse=True)
    meds = sorted((r for r in ctx.medications if r.start <= as_of), key=_order, reverse=True)
    seen, kept = set(), []
    for r in meds:
        if r.display not in seen:
            seen.add(r.display)
            kept.append(r)
    ctx.medications = kept[:KEEP_MEDS]
    ctx.procedures = sorted((r for r in ctx.procedures if r.start <= as_of), key=_order)
    ctx.observations = {t: r for t, r in ctx.observations.items() if r.start <= as_of}
    if ctx.last_encounter and ctx.last_encounter > as_of:
        ctx.last_encounter = None
    return ctx


def proc_concept_of(rec: Rec) -> str | None:
    return RECORD_TAG_TO_PROC.get(rec.tag or "")


def linked_resident_ids(dataset_version: str, dataset_manifest: dict | None) -> set[uuid.UUID]:
    """Residents of the blocks the family linker has finished (`datasets.manifest.family_linker.blocks`).

    Block membership is recomputed from the batches' Patient files exactly as the linker did.
    ponytail: a set of ids is fine for tens of blocks; keep a per-block lookup if a 1M run links 100 blocks.
    """
    from anticipa.datafabric import family
    from anticipa.datafabric.synthea import SYNTHEA_DIR

    blocks = ((dataset_manifest or {}).get("family_linker") or {}).get("blocks") or {}
    ids: set[uuid.UUID] = set()
    for info in blocks.values():
        first, stop = info["batches"]
        ids.update(family.block_resident_ids(dataset_version, SYNTHEA_DIR, list(range(first, stop))))
    return ids


# ---- ground-truth family graph (optional input) ------------------------------------------------------------------
def load_family_graph(
    dataset_version: str, as_of: date, root: Path = GROUND_TRUTH_DIR
) -> dict[uuid.UUID, list[Relative]]:
    """resident_id -> relatives, from gt.family_edge.v1 lines that have a `related_person_key` or resident link."""
    path = root / dataset_version / "families" / "family_graph.jsonl"
    out: dict[uuid.UUID, list[Relative]] = defaultdict(list)
    if not path.is_file():
        return out
    from anticipa.datafabric.docgen.phrases import RELATIONS

    with path.open("rb") as fh:
        for line in fh:
            e = orjson.loads(line)
            rel = e.get("relationship_type")
            if rel not in RELATIONS or e.get("provenance") == "document_only":
                continue
            related = e.get("related_resident_id")
            by = (
                e.get("birth_year") or 0
            )  # 0 = unknown: resolved from `residents` (DB) or synthesised (planner)
            out[uuid.UUID(e["resident_id"])].append(
                Relative(
                    key=str(related or e.get("related_person_key") or e["edge_id"]),
                    relationship=rel,
                    sex=RELATIONS[rel]["sex"],
                    side=RELATIONS[rel]["side"],
                    birth_year=int(by),
                    resident_id=uuid.UUID(related) if related else None,
                    edge_id=uuid.UUID(e["edge_id"]),
                    provenance=e.get("provenance", "synthea_link"),
                    decoy=bool(e.get("decoy")),
                )
            )
    return out


# ---- loaders ---------------------------------------------------------------------------------------------------
def contexts_from_db(
    session: Session,
    dataset_version: str,
    *,
    chunk_size: int = 500,
    limit: int | None = None,
    after: uuid.UUID | None = None,
    graph: dict[uuid.UUID, list[Relative]] | None = None,
    as_of: date,
    linked_ids: set[uuid.UUID] | None = None,
) -> Iterator[list[ResidentContext]]:
    """Keyset-paginated chunks of ResidentContext for one dataset (bounded memory)."""
    dataset_id = session.execute(
        select(Dataset.id).where(Dataset.dataset_version == dataset_version)
    ).scalar_one_or_none()
    if dataset_id is None:
        raise LookupError(f"dataset {dataset_version!r} is not in the database")
    seen = 0
    cursor = after
    while limit is None or seen < limit:
        take = chunk_size if limit is None else min(chunk_size, limit - seen)
        q = select(Resident).where(Resident.dataset_id == dataset_id).order_by(Resident.id).limit(take)
        if cursor is not None:
            q = q.where(Resident.id > cursor)
        residents = session.execute(q).scalars().all()
        if not residents:
            return
        by_id = {
            r.id: ResidentContext(
                id=r.id,
                source_patient_id=r.source_patient_id,
                birth_date=r.birth_date,
                sex=r.administrative_sex or "unknown",
                language=r.preferred_language or "en",
                district=r.district_code,
                deceased=r.deceased_date,
                graph_relatives=(graph or {}).get(r.id, []),
                linked=linked_ids is None or r.id in linked_ids,
            )
            for r in residents
            if r.birth_date is not None
        }
        ids = list(by_id)
        unknown = {
            r.resident_id: []
            for c in by_id.values()
            for r in c.graph_relatives
            if r.resident_id and r.birth_year <= 0
        }
        for r in (r for c in by_id.values() for r in c.graph_relatives if r.resident_id in unknown):
            unknown[r.resident_id].append(r)
        if unknown:  # real birth years of linked relatives (the graph file carries ids only)
            for rid, born in session.execute(
                select(Resident.id, Resident.birth_date).where(Resident.id.in_(list(unknown)))
            ).all():
                for r in unknown[rid]:
                    r.birth_year = born.year if born else 0
        tag = ClinicalRecord.value_json["tag"].astext
        rows = session.execute(
            select(
                ClinicalRecord.resident_id,
                ClinicalRecord.category,
                ClinicalRecord.code_system,
                ClinicalRecord.code,
                ClinicalRecord.display,
                ClinicalRecord.effective_start,
                ClinicalRecord.effective_end,
                ClinicalRecord.status,
                ClinicalRecord.value_json,
                ClinicalRecord.source_resource_id,
            )
            .where(ClinicalRecord.resident_id.in_(ids))
            # enrichment rows are re-derived deterministically each run; reading them back would change the plan
            .where(ClinicalRecord.source_system.is_distinct_from(ENRICHMENT_SOURCE))
            .where(
                (ClinicalRecord.category.in_(("condition", "medication")))
                | ((ClinicalRecord.category == "procedure") & tag.in_(SCREENING_PROC_TAGS))
                | ((ClinicalRecord.category == "observation") & tag.in_(OBS_TAGS))
            )
        ).all()
        for rid, cat, system, code, display, start, end, status, value, source_id in rows:
            add_record(by_id[rid], cat, system, code, display, start, end, status, value, source_id)
        enc = session.execute(
            select(ClinicalRecord.resident_id, func.max(ClinicalRecord.effective_start))
            .where(ClinicalRecord.resident_id.in_(ids), ClinicalRecord.category == "encounter")
            .group_by(ClinicalRecord.resident_id)
        ).all()
        for rid, last in enc:
            add_record(by_id[rid], "encounter", None, None, None, last, None, None, None, None)
        cursor = residents[-1].id
        seen += len(residents)
        yield [finalize(c, as_of) for c in by_id.values()]


def contexts_from_synthea(
    dataset_version: str,
    *,
    limit: int | None = None,
    graph: dict[uuid.UUID, list[Relative]] | None = None,
    as_of: date,
) -> Iterator[list[ResidentContext]]:
    """One chunk per completed Synthea batch, read from data/synthea/<V>/ without a database."""
    from anticipa.datafabric.normalize import BatchNormalizer
    from anticipa.datafabric.synthea import SYNTHEA_DIR, load_manifest, sorted_batches

    dataset_dir = SYNTHEA_DIR / dataset_version
    manifest = load_manifest(dataset_dir)
    if manifest is None:
        raise FileNotFoundError(f"no Synthea manifest in {dataset_dir}")
    seen = 0
    for index, batch in sorted_batches(manifest):
        if batch.get("status") != "completed" or (limit is not None and seen >= limit):
            continue
        norm = BatchNormalizer(dataset_version, dataset_dir / f"batch-{index}")
        by_id: dict[uuid.UUID, ResidentContext] = {}
        try:
            for rid, _sys, pid, birth, deceased, sex, lang, district, _v in norm.residents():
                if limit is not None and seen + len(by_id) >= limit:
                    continue
                by_id[rid] = ResidentContext(
                    id=rid,
                    source_patient_id=pid,
                    birth_date=date.fromisoformat(birth),
                    sex=sex,
                    language=lang,
                    district=district,
                    deceased=date.fromisoformat(deceased) if deceased else None,
                    graph_relatives=(graph or {}).get(rid, []),
                )
            for row in norm.clinical_records(
                ("Condition", "MedicationRequest", "Procedure", "Observation", "Encounter")
            ):
                rid, _rt, _srid, _ss, cat, system, code, display, start, end, status, value, ref = row
                if rid in by_id:
                    add_record(
                        by_id[rid],
                        cat,
                        system,
                        code,
                        display,
                        start,
                        end,
                        status,
                        orjson.loads(value) if value else None,
                        ref.rsplit("/", 1)[-1],
                    )
        finally:
            norm.close()
        seen += len(by_id)
        yield [finalize(c, as_of) for c in by_id.values()]
