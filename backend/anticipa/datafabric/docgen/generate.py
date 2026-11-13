# ruff: noqa: E501
"""Generation: resident context -> planted facts -> rendered documents -> store, `documents` rows, ground truth.

`build_resident` is a pure function of (context, dataset_version, seed, as_of). `generate` streams residents in chunks
(bounded memory), skips residents already in the ground-truth ledger (resume / incremental runs), and registers
documents idempotently on UNIQUE(resident_id, content_hash). Deterministic templates only: no model calls (owner rule).
"""

from __future__ import annotations

import os
import re
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path

import orjson
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from anticipa.core.ids import uuid5_of
from anticipa.datafabric.assign import _unit
from anticipa.datafabric.docgen.context import (
    GROUND_TRUTH_DIR,
    ResidentContext,
    contexts_from_db,
    contexts_from_synthea,
    dataset_date,
    linked_resident_ids,
    load_family_graph,
)
from anticipa.datafabric.docgen.enrich import (
    ASSUMPTIONS,
    apply_enrichment,
    clinical_record_rows,
    ground_truth_records,
    plan_enrichment,
)
from anticipa.datafabric.docgen.groundtruth import FILES, MANIFEST, GroundTruthWriter
from anticipa.datafabric.docgen.planner import (
    GENERATOR,
    GENERATOR_VERSION,
    assign_scenarios,
    make_rng,
    plan_resident,
    split_of,
)
from anticipa.datafabric.docgen.render import render_doc
from anticipa.datafabric.docstore import DocumentStore, content_hash, get_document_store, make_storage_key
from anticipa.datafabric.models import ClinicalRecord, Dataset, Document

DEGREE = {"mother": 1, "father": 1, "sister": 1, "brother": 1, "daughter": 1, "son": 1}  # others are degree 2
REGISTER_BATCH = 1000


@dataclass
class BuiltDoc:
    id: uuid.UUID
    resident_id: uuid.UUID
    doc_type: str
    language: str
    text: str
    hash: str
    key: str
    created_on: datetime
    code_mixed: bool
    unicode_variant: bool


@dataclass
class ResidentOutput:
    resident_id: uuid.UUID
    language: str
    split: str
    scenarios: list[str]
    linked: bool = True
    docs: list[BuiltDoc] = field(default_factory=list)
    facts: list[dict] = field(default_factory=list)
    index: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    relatives: list[dict] = field(default_factory=list)
    enrichment: list[dict] = field(default_factory=list)  # gt.enrichment.v1
    enrichment_rows: list[dict] = field(default_factory=list)  # clinical_records rows (structured items only)


def build_resident(
    ctx: ResidentContext, dataset_version: str, seed: int, as_of: date, *, enrich: bool = True
) -> ResidentOutput:
    V = dataset_version
    items = []
    if enrich:  # work on a copy: enrichment adds records to the context the planner reads
        ctx = replace(
            ctx,
            procedures=list(ctx.procedures),
            observations=dict(ctx.observations),
            note_only=list(ctx.note_only),
        )
        items = plan_enrichment(ctx, seed, V, as_of, control=assign_scenarios(ctx, as_of) == ["DC-00"])
        apply_enrichment(ctx, items, V)
    plan = plan_resident(ctx, seed, V, as_of)
    out = ResidentOutput(ctx.id, ctx.language, split_of(ctx.id), plan.scenarios, ctx.linked)
    gen = {"generator": GENERATOR, "generator_version": GENERATOR_VERSION, "seed": seed, "method": "template"}
    tag_ids: dict[str, str] = {}
    pending_dups: list[tuple[dict, str]] = []
    members: dict[str, list[str]] = defaultdict(list)

    for spec in plan.docs:
        rendered = render_doc(spec, ctx, make_rng(seed, V, ctx.id, f"render:{spec.doc_type}"))
        text, h = rendered.text, content_hash(rendered.text)
        doc_id = uuid5_of(f"{V}:doc:{ctx.id}:{h}")
        out.docs.append(
            BuiltDoc(
                doc_id,
                ctx.id,
                spec.doc_type,
                ctx.language,
                text,
                h,
                make_storage_key(V, ctx.id, doc_id),
                datetime(spec.when.year, spec.when.month, spec.when.day, 9, 0, tzinfo=UTC),
                rendered.code_mixed,
                rendered.unicode_variant,
            )
        )
        slot, doc_scn = 0, set()
        for ri in rendered.items:
            item = ri.item
            bases = ([item.label] if item.label is not None else []) + list(item.absent)
            for base in bases:
                rec = dict(base)
                rec.setdefault("expected_present", True)
                is_main = base is item.label
                q = ri.quote
                rec["quote"], rec["injected_span"], rec["acceptable_quote_spans"] = None, None, []
                if item.kind == "injection":
                    rec["injected_span"] = {"start": q[0], "end": q[1]}
                elif is_main and q is not None:
                    rec["quote"] = {
                        "text": text[q[0] : q[1]],
                        "start": q[0],
                        "end": q[1],
                        "offset_basis": "original_codepoints",
                    }
                    if ri.line and ri.line != q:
                        rec["acceptable_quote_spans"] = [{"start": ri.line[0], "end": ri.line[1]}]
                label_id = str(uuid5_of(f"{V}:gt:fact:{doc_id}:{slot}"))
                slot += 1
                rec.update(
                    schema_version="gt.fact.v1",
                    label_id=label_id,
                    dataset_version=V,
                    label_version=1,
                    split=out.split,
                    resident_id=str(ctx.id),
                    document_id=str(doc_id),
                    document_content_sha256=h,
                    language=ctx.language,
                    document_type=spec.doc_type,
                    scenario_ids=sorted(set(rec["scenario_ids"])),
                    duplicate_of=None,
                    conflict_group_id=None,
                    generation=gen,
                    label_origin="programmatic",
                    review_status="unreviewed",
                )
                if item.conflict and is_main:
                    rec["conflict_group_id"] = str(uuid5_of(f"{V}:gt:conflict:{ctx.id}:{item.conflict}"))
                    members[item.conflict].append(label_id)
                if is_main and item.tag:
                    tag_ids.setdefault(item.tag, label_id)
                if is_main and item.duplicate_of_tag:
                    pending_dups.append((rec, item.duplicate_of_tag))
                doc_scn.update(rec["scenario_ids"])
                out.facts.append(rec)
        out.index.append(
            {
                "schema_version": "gt.document_index.v1",
                "dataset_version": V,
                "document_id": str(doc_id),
                "resident_id": str(ctx.id),
                "language": ctx.language,
                "document_type": spec.doc_type,
                "content_sha256": h,
                "generation_method": GENERATOR_VERSION,
                "scenario_ids": sorted(doc_scn),
                "label_count": slot,
                "code_mixed": rendered.code_mixed,
                "unicode_variant": rendered.unicode_variant,
                "split": out.split,
                "created_on": out.docs[-1].created_on.isoformat(),
                "synthetic_author_id": f"SYN-AUTH-{int(_unit(ctx.id, 'docgen:author') * 1000):03d}",
            }
        )
    for rec, tag in pending_dups:
        rec["duplicate_of"] = tag_ids.get(tag)
    out.enrichment = ground_truth_records(ctx, items, V, seed)
    for e in out.enrichment:
        e["document_label_id"] = tag_ids.get(f"enrich:{e['concept']}")
    out.enrichment_rows = clinical_record_rows(ctx, items, V)
    for key, ids in members.items():
        info = plan.conflicts.get(key, {"kind": "doc_vs_doc", "structured_refs": []})
        out.conflicts.append(
            {
                "schema_version": "gt.conflict_group.v1",
                "dataset_version": V,
                "resident_id": str(ctx.id),
                "conflict_group_id": str(uuid5_of(f"{V}:gt:conflict:{ctx.id}:{key}")),
                "member_label_ids": ids,
                "kind": info["kind"],
                "expected_resolution": "preserve_both",
                "structured_record_refs": info["structured_refs"],
            }
        )
    for absent in plan.resident_absent:
        rec = dict(absent)
        rec.update(
            schema_version="gt.fact.v1",
            label_id=str(uuid5_of(f"{V}:gt:fact:{ctx.id}:absent:{rec['concept_id']}")),
            dataset_version=V,
            label_version=1,
            split=out.split,
            resident_id=str(ctx.id),
            document_id=None,
            document_content_sha256=None,
            language=ctx.language,
            document_type=None,
            expected_present=False,
            scenario_ids=sorted(set(rec["scenario_ids"])),
            quote=None,
            injected_span=None,
            acceptable_quote_spans=[],
            duplicate_of=None,
            conflict_group_id=None,
            generation=gen,
            label_origin="programmatic",
            review_status="unreviewed",
        )
        out.facts.append(rec)
    for rel in plan.relatives_used.values():
        if rel.resident_id is None and rel.edge_id is None:
            out.relatives.append(
                {
                    "schema_version": "gt.family_edge.v1",
                    "dataset_version": V,
                    "edge_id": str(uuid5_of(f"{V}:gt:family_edge:{ctx.id}:{rel.key}")),
                    "resident_id": str(ctx.id),
                    "related_resident_id": None,
                    "related_person_key": rel.key,
                    "relationship_type": rel.relationship,
                    "degree": DEGREE.get(rel.relationship, 2),
                    "side": rel.side,
                    "household_id": None,
                    "provenance": "document_only",
                    "decoy": False,
                    "birth_year": rel.birth_year,
                    "sex": rel.sex,
                }
            )
    return out


def register_documents(session: Session, docs: list[BuiltDoc]) -> int:
    """Insert `documents` rows; existing (resident_id, content_hash) rows are left untouched. Returns rows inserted."""
    inserted = 0
    for i in range(0, len(docs), REGISTER_BATCH):
        rows = [
            {
                "id": d.id,
                "resident_id": d.resident_id,
                "document_type": d.doc_type,
                "language": d.language,
                "storage_key": d.key,
                "content_hash": d.hash,
                "version": 1,
                "generation_method": GENERATOR_VERSION,
                "synthetic_status": "synthetic",
                "processing_status": "registered",
                "created_on": d.created_on,
            }
            for d in docs[i : i + REGISTER_BATCH]
        ]
        stmt = (
            pg_insert(Document.__table__)
            .values(rows)
            .on_conflict_do_nothing()
            .returning(Document.__table__.c.id)
        )
        inserted += len(session.execute(stmt).all())  # RETURNING counts only the rows actually inserted
    session.commit()
    return inserted


def register_clinical_records(session: Session, rows: list[dict]) -> int:
    """Insert enrichment `clinical_records` rows; UNIQUE(resident_id, resource_type, source_resource_id) makes it idempotent."""
    inserted = 0
    for i in range(0, len(rows), REGISTER_BATCH):
        stmt = (
            pg_insert(ClinicalRecord.__table__).values(rows[i : i + REGISTER_BATCH]).on_conflict_do_nothing()
        )
        inserted += len(session.execute(stmt.returning(ClinicalRecord.__table__.c.id)).all())
    session.commit()
    return inserted


GT_FILES_TO_RESET = (
    "facts",
    "documents/document_index.jsonl",
    "documents/docgen_ledger.jsonl",
    "documents/docgen_manifest.json",
    "conflicts",
    "families/document_only_relatives.jsonl",
    "enrichment",
)  # docgen-owned only: families/family_graph.jsonl and every other file in the directory are never touched


def reset_generated(
    dataset_version: str,
    *,
    session: Session | None = None,
    store_root: Path | None = None,
    gt_root: Path = GROUND_TRUTH_DIR,
    include_enrichment: bool = True,
    apply: bool = False,
) -> dict:
    """Remove everything docgen produced for one dataset so it can be regenerated (e.g. after the family graph exists).

    Dry run unless `apply`. Refuses if evidence has already been extracted from the documents. Touches only
    `documents` rows with generation_method='template_v1', enrichment `clinical_records` rows, the local document
    directory for this version and docgen's own ground-truth files.
    """
    from sqlalchemy import delete, func, select

    from anticipa.datafabric.docgen.context import ENRICHMENT_SOURCE
    from anticipa.datafabric.docstore import DEFAULT_LOCAL_DIR

    result: dict = {"applied": apply}
    if session is not None:
        docs = (
            Document.storage_key.like(f"{dataset_version}/%"),
            Document.generation_method == GENERATOR_VERSION,
        )
        enriched = (
            ClinicalRecord.source_system == ENRICHMENT_SOURCE,
            ClinicalRecord.source_reference.like(f"enrichment://{dataset_version}/%"),
        )
        from anticipa.evidence.models import EvidenceSource

        referenced = session.execute(
            select(func.count())
            .select_from(EvidenceSource)
            .join(Document, Document.id == EvidenceSource.document_id)
            .where(*docs)
        ).scalar_one()
        if referenced:
            raise RuntimeError(
                f"{referenced} evidence sources already reference these documents; refusing to delete them"
            )
        result["documents"] = session.execute(
            select(func.count()).select_from(Document).where(*docs)
        ).scalar_one()
        result["enrichment_records"] = (
            session.execute(select(func.count()).select_from(ClinicalRecord).where(*enriched)).scalar_one()
            if include_enrichment
            else 0
        )
        if apply:
            session.execute(delete(Document).where(*docs))
            if include_enrichment:
                session.execute(delete(ClinicalRecord).where(*enriched))
            session.commit()
    docs_dir = (store_root or DEFAULT_LOCAL_DIR) / dataset_version
    gt_dir = Path(gt_root) / dataset_version
    targets = [docs_dir] + [gt_dir / rel for rel in GT_FILES_TO_RESET]
    result["paths"] = [str(t) for t in targets if t.exists()]
    if apply:
        import shutil

        for t in targets:
            if t.is_dir():
                shutil.rmtree(t)
            elif t.exists():
                t.unlink()
    return result


_RESIDENT_ID = re.compile(rb'"resident_id":"([0-9a-f-]{36})"')


def stale_unlinked(dataset_version: str, gt_root: Path, linked_ids: set[uuid.UUID]) -> set[str]:
    """Residents generated while their block was unlinked that belong to a linked block now."""
    ledger = Path(gt_root) / dataset_version / FILES["ledger"]
    if not ledger.exists():
        return set()
    now_linked = {str(i) for i in linked_ids}
    out = set()
    for line in ledger.read_bytes().splitlines():
        item = orjson.loads(line) if line else None
        if item and not item.get("linked", True) and item["resident_id"] in now_linked:
            out.add(item["resident_id"])
    return out


def purge_residents(
    dataset_version: str,
    resident_ids: set[str],
    *,
    session: Session | None,
    store_root: Path | None,
    gt_root: Path = GROUND_TRUTH_DIR,
) -> dict:
    """Remove docgen output (files, `documents` and enrichment rows, ground-truth lines) of the given residents only.

    Refuses if evidence was already extracted from their documents. Ground-truth files are rewritten without those
    residents' lines (streamed, atomically) and the manifest is dropped so it is recounted from the files.
    """
    import shutil

    from sqlalchemy import delete, func, select

    from anticipa.datafabric.docgen.context import ENRICHMENT_SOURCE
    from anticipa.datafabric.docstore import DEFAULT_LOCAL_DIR

    result = {"residents": len(resident_ids), "documents": 0, "enrichment_records": 0}
    ids = sorted(uuid.UUID(r) for r in resident_ids)
    if session is not None:
        from anticipa.evidence.models import EvidenceSource

        for i in range(0, len(ids), 1000):
            batch = ids[i : i + 1000]
            mine = (Document.resident_id.in_(batch), Document.generation_method == GENERATOR_VERSION)
            referenced = session.execute(
                select(func.count())
                .select_from(EvidenceSource)
                .join(Document, Document.id == EvidenceSource.document_id)
                .where(*mine)
            ).scalar_one()
            if referenced:
                raise RuntimeError(
                    f"{referenced} evidence sources reference these documents; refusing to purge"
                )
            result["documents"] += session.execute(delete(Document).where(*mine)).rowcount
            result["enrichment_records"] += session.execute(
                delete(ClinicalRecord).where(
                    ClinicalRecord.resident_id.in_(batch), ClinicalRecord.source_system == ENRICHMENT_SOURCE
                )
            ).rowcount
        session.commit()
    for rid in resident_ids:
        shutil.rmtree((store_root or DEFAULT_LOCAL_DIR) / dataset_version / rid, ignore_errors=True)
    base = Path(gt_root) / dataset_version
    for rel in FILES.values():
        path = base / rel
        if not path.exists():
            continue
        tmp = path.with_name(path.name + ".tmp")
        with path.open("rb") as src, tmp.open("wb") as dst:
            for line in src:
                m = _RESIDENT_ID.search(line)
                if m is None or m.group(1).decode() not in resident_ids:
                    dst.write(line)
        os.replace(tmp, path)
    (base / MANIFEST).unlink(missing_ok=True)
    return result


class _NoSession:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


@dataclass
class RunReport:
    dataset_version: str
    seed: int
    as_of: str
    residents_seen: int = 0
    residents_generated: int = 0
    skipped_done: int = 0
    skipped_deceased: int = 0
    skipped_unlinked: int = 0
    refreshed_after_linking: int = 0
    documents: int = 0
    documents_registered: int = 0
    enrichment_records_registered: int = 0
    drifted_residents: int = 0
    enrichment_items: int = 0
    labels: int = 0
    seconds: float = 0.0
    manifest: dict = field(default_factory=dict)

    def summary(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "manifest"}
        d["residents_per_second"] = (
            round(self.residents_generated / self.seconds, 1) if self.seconds else None
        )
        d["documents_per_second"] = round(self.documents / self.seconds, 1) if self.seconds else None
        return d


def resolve_as_of(dataset_version: str, override: date | None, dataset_manifest: dict | None) -> date:
    if override:
        return override
    ref = (dataset_manifest or {}).get("reference_date")
    if ref:
        return date(int(ref[:4]), int(ref[4:6]), int(ref[6:8]))
    return dataset_date(dataset_version)


def generate(
    dataset_version: str,
    *,
    seed: int,
    residents: int | None = None,
    source: str = "db",
    chunk_size: int = 500,
    register: bool | None = None,
    store: DocumentStore | None = None,
    gt_root: Path = GROUND_TRUTH_DIR,
    as_of: date | None = None,
    session_factory: Callable[[], Session] | None = None,
    enrich: bool = True,
    family: str | None = None,
    only_linked: bool = False,
    refresh_linked: bool = False,
    on_chunk: Callable[[RunReport, uuid.UUID | None], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> RunReport:
    """Generate documents for up to `residents` residents (None = all) of one dataset. Safe to re-run.

    `family` decides who may receive family-dependent facts: "linked" (default for the database source) only residents
    of blocks the family linker has finished, "all" (default for the file source) everyone, "none" nobody.
    `only_linked` skips the other residents entirely, so they can be generated after their block is linked.
    `refresh_linked` first purges and regenerates residents that were generated before their block was linked.
    """
    if source not in ("db", "synthea"):
        raise ValueError("source must be 'db' or 'synthea'")
    family = family or ("linked" if source == "db" else "all")
    if family not in ("linked", "all", "none"):
        raise ValueError("family must be 'linked', 'all' or 'none'")
    if source == "db" and session_factory is None:
        from anticipa.db import get_sessionmaker

        session_factory = get_sessionmaker()
    register = (source == "db") if register is None else register
    if register and session_factory is None:
        from anticipa.db import get_sessionmaker

        session_factory = get_sessionmaker()
    store = store or get_document_store()
    started = time.perf_counter()

    dataset_manifest = None
    if source == "db":
        with session_factory() as s:
            row = s.execute(
                Dataset.__table__.select().where(Dataset.dataset_version == dataset_version)
            ).first()
            dataset_manifest = row.manifest if row is not None else None
    else:
        from anticipa.datafabric.synthea import SYNTHEA_DIR, load_manifest

        dataset_manifest = load_manifest(SYNTHEA_DIR / dataset_version)
    day = resolve_as_of(dataset_version, as_of, dataset_manifest)
    graph = load_family_graph(dataset_version, day, gt_root)
    linked_ids: set[uuid.UUID] | None = (
        None
        if family == "all"
        else set()
        if family == "none"
        else linked_resident_ids(dataset_version, dataset_manifest)
    )
    purged: dict = {}
    if refresh_linked and linked_ids:
        stale = stale_unlinked(dataset_version, gt_root, linked_ids)
        if stale:
            with session_factory() if session_factory else _NoSession() as s:
                purged = purge_residents(
                    dataset_version,
                    stale,
                    session=s if register else None,
                    store_root=getattr(store, "root", None),
                    gt_root=gt_root,
                )
    writer = GroundTruthWriter(dataset_version, seed, day, gt_root, enrich=enrich)
    report = RunReport(
        dataset_version, seed, day.isoformat(), refreshed_after_linking=purged.get("residents", 0)
    )

    def chunks():
        if source == "synthea":
            yield from contexts_from_synthea(dataset_version, limit=residents, graph=graph, as_of=day)
        else:
            with session_factory() as s:
                yield from contexts_from_db(
                    s, dataset_version, chunk_size=chunk_size, limit=residents, graph=graph, as_of=day
                )

    with ThreadPoolExecutor(max_workers=8) as pool:
        for contexts in chunks():
            if cancelled and cancelled():
                break
            todo: list[
                tuple[ResidentContext, bool]
            ] = []  # (context, labels already written by an earlier run)
            for ctx in contexts:
                report.residents_seen += 1
                ctx.linked = linked_ids is None or ctx.id in linked_ids
                done = str(ctx.id) in writer.done
                if only_linked and not ctx.linked:
                    report.skipped_unlinked += 1
                elif ctx.deceased is not None and ctx.deceased <= day:
                    report.skipped_deceased += 1
                elif done and not register:
                    report.skipped_done += 1
                else:
                    todo.append((ctx, done))
            # Already-done residents are rebuilt (3 ms each, identical content) only to register their documents
            # when an earlier run did not; their labels and files are never rewritten.
            built = [(build_resident(c, dataset_version, seed, day, enrich=enrich), done) for c, done in todo]
            new = [o for o, done in built if not done]
            report.skipped_done += len(built) - len(new)
            docs = [d for o in new for d in o.docs]
            list(pool.map(lambda d: store.write_text(d.key, d.text), docs))  # idempotent overwrite
            if register and built:
                to_register: list[ResidentOutput] = []
                docs_to_register: list[BuiltDoc] = []
                for o, done in built:
                    if (
                        done
                    ):  # never register a document that has no labels on disk (the context changed since)
                        known = writer.docs_of.get(str(o.resident_id), set())
                        if {str(d.id) for d in o.docs} != known:
                            report.drifted_residents += 1
                            docs_to_register += [d for d in o.docs if str(d.id) in known]
                            continue
                    to_register.append(o)
                    docs_to_register += o.docs
                with session_factory() as s:
                    rows = [r for o in to_register for r in o.enrichment_rows]
                    if rows:
                        report.enrichment_records_registered += register_clinical_records(s, rows)
                    report.documents_registered += register_documents(s, docs_to_register)
            writer.write_chunk(new)
            report.residents_generated += len(new)
            report.documents += len(docs)
            report.labels += sum(len(o.facts) for o in new)
            report.enrichment_items += sum(len(o.enrichment) for o in new)
            report.seconds = time.perf_counter() - started
            if on_chunk:
                on_chunk(report, contexts[-1].id if contexts else None)
    report.seconds = time.perf_counter() - started
    report.manifest = writer.finalize(
        {
            "run": report.summary(),
            "source": source,
            "enrichment": {"enabled": enrich, **(ASSUMPTIONS if enrich else {})},
            "family": {
                "mode": family,
                "linked_residents_known": None if linked_ids is None else len(linked_ids),
            },
        }
    )
    return report
