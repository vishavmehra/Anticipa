"""Generation: resident context -> planted facts -> rendered documents -> store, `documents` rows, ground truth.

`build_resident` is a pure function of (context, dataset_version, seed, as_of). `generate` streams residents in chunks
(bounded memory), skips residents already in the ground-truth ledger (resume / incremental runs), and registers
documents idempotently on UNIQUE(resident_id, content_hash). Deterministic templates only: no model calls (owner rule).
"""

from __future__ import annotations

import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from anticipa.core.ids import uuid5_of
from anticipa.datafabric.assign import _unit
from anticipa.datafabric.docgen.context import (
    GROUND_TRUTH_DIR, ResidentContext, contexts_from_db, contexts_from_synthea, dataset_date, load_family_graph,
)
from anticipa.datafabric.docgen.groundtruth import GroundTruthWriter
from anticipa.datafabric.docgen.planner import GENERATOR, GENERATOR_VERSION, make_rng, plan_resident, split_of
from anticipa.datafabric.docgen.render import render_doc
from anticipa.datafabric.docstore import DocumentStore, content_hash, get_document_store, make_storage_key
from anticipa.datafabric.models import Dataset, Document

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
    docs: list[BuiltDoc] = field(default_factory=list)
    facts: list[dict] = field(default_factory=list)
    index: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    relatives: list[dict] = field(default_factory=list)


def build_resident(ctx: ResidentContext, dataset_version: str, seed: int, as_of: date) -> ResidentOutput:
    V = dataset_version
    plan = plan_resident(ctx, seed, V, as_of)
    out = ResidentOutput(ctx.id, ctx.language, split_of(ctx.id), plan.scenarios)
    gen = {"generator": GENERATOR, "generator_version": GENERATOR_VERSION, "seed": seed, "method": "template"}
    tag_ids: dict[str, str] = {}
    pending_dups: list[tuple[dict, str]] = []
    members: dict[str, list[str]] = defaultdict(list)

    for spec in plan.docs:
        rendered = render_doc(spec, ctx, make_rng(seed, V, ctx.id, f"render:{spec.doc_type}"))
        text, h = rendered.text, content_hash(rendered.text)
        doc_id = uuid5_of(f"{V}:doc:{ctx.id}:{h}")
        out.docs.append(BuiltDoc(
            doc_id, ctx.id, spec.doc_type, ctx.language, text, h, make_storage_key(V, ctx.id, doc_id),
            datetime(spec.when.year, spec.when.month, spec.when.day, 9, 0, tzinfo=UTC), rendered.code_mixed,
            rendered.unicode_variant,
        ))
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
                    rec["quote"] = {"text": text[q[0] : q[1]], "start": q[0], "end": q[1], "offset_basis": "original_codepoints"}
                    if ri.line and ri.line != q:
                        rec["acceptable_quote_spans"] = [{"start": ri.line[0], "end": ri.line[1]}]
                label_id = str(uuid5_of(f"{V}:gt:fact:{doc_id}:{slot}"))
                slot += 1
                rec.update(
                    schema_version="gt.fact.v1", label_id=label_id, dataset_version=V, label_version=1, split=out.split,
                    resident_id=str(ctx.id), document_id=str(doc_id), document_content_sha256=h, language=ctx.language,
                    document_type=spec.doc_type, scenario_ids=sorted(set(rec["scenario_ids"])), duplicate_of=None,
                    conflict_group_id=None, generation=gen, label_origin="programmatic", review_status="unreviewed",
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
        out.index.append({
            "schema_version": "gt.document_index.v1", "dataset_version": V, "document_id": str(doc_id),
            "resident_id": str(ctx.id), "language": ctx.language, "document_type": spec.doc_type, "content_sha256": h,
            "generation_method": GENERATOR_VERSION, "scenario_ids": sorted(doc_scn), "label_count": slot,
            "code_mixed": rendered.code_mixed, "unicode_variant": rendered.unicode_variant, "split": out.split,
            "created_on": out.docs[-1].created_on.isoformat(),
            "synthetic_author_id": f"SYN-AUTH-{int(_unit(ctx.id, 'docgen:author') * 1000):03d}",
        })
    for rec, tag in pending_dups:
        rec["duplicate_of"] = tag_ids.get(tag)
    for key, ids in members.items():
        info = plan.conflicts.get(key, {"kind": "doc_vs_doc", "structured_refs": []})
        out.conflicts.append({
            "schema_version": "gt.conflict_group.v1", "dataset_version": V, "resident_id": str(ctx.id),
            "conflict_group_id": str(uuid5_of(f"{V}:gt:conflict:{ctx.id}:{key}")), "member_label_ids": ids,
            "kind": info["kind"], "expected_resolution": "preserve_both", "structured_record_refs": info["structured_refs"],
        })
    for absent in plan.resident_absent:
        rec = dict(absent)
        rec.update(
            schema_version="gt.fact.v1", label_id=str(uuid5_of(f"{V}:gt:fact:{ctx.id}:absent:{rec['concept_id']}")),
            dataset_version=V, label_version=1, split=out.split, resident_id=str(ctx.id), document_id=None,
            document_content_sha256=None, language=ctx.language, document_type=None, expected_present=False,
            scenario_ids=sorted(set(rec["scenario_ids"])), quote=None, injected_span=None, acceptable_quote_spans=[],
            duplicate_of=None, conflict_group_id=None, generation=gen, label_origin="programmatic",
            review_status="unreviewed",
        )
        out.facts.append(rec)
    for rel in plan.relatives_used.values():
        if rel.resident_id is None and rel.edge_id is None:
            out.relatives.append({
                "schema_version": "gt.family_edge.v1", "dataset_version": V,
                "edge_id": str(uuid5_of(f"{V}:gt:family_edge:{ctx.id}:{rel.key}")), "resident_id": str(ctx.id),
                "related_resident_id": None, "related_person_key": rel.key, "relationship_type": rel.relationship,
                "degree": DEGREE.get(rel.relationship, 2), "side": rel.side, "household_id": None,
                "provenance": "document_only", "decoy": False, "birth_year": rel.birth_year, "sex": rel.sex,
            })
    return out


def register_documents(session: Session, docs: list[BuiltDoc]) -> int:
    """Insert `documents` rows; existing (resident_id, content_hash) rows are left untouched. Returns rows inserted."""
    inserted = 0
    for i in range(0, len(docs), REGISTER_BATCH):
        rows = [
            {
                "id": d.id, "resident_id": d.resident_id, "document_type": d.doc_type, "language": d.language,
                "storage_key": d.key, "content_hash": d.hash, "version": 1, "generation_method": GENERATOR_VERSION,
                "synthetic_status": "synthetic", "processing_status": "registered", "created_on": d.created_on,
            }
            for d in docs[i : i + REGISTER_BATCH]
        ]
        inserted += session.execute(pg_insert(Document).values(rows).on_conflict_do_nothing()).rowcount
    session.commit()
    return inserted


@dataclass
class RunReport:
    dataset_version: str
    seed: int
    as_of: str
    residents_seen: int = 0
    residents_generated: int = 0
    skipped_done: int = 0
    skipped_deceased: int = 0
    documents: int = 0
    documents_registered: int = 0
    labels: int = 0
    seconds: float = 0.0
    manifest: dict = field(default_factory=dict)

    def summary(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "manifest"}
        d["residents_per_second"] = round(self.residents_generated / self.seconds, 1) if self.seconds else None
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
    dataset_version: str, *, seed: int, residents: int | None = None, source: str = "db", chunk_size: int = 500,
    register: bool | None = None, store: DocumentStore | None = None, gt_root: Path = GROUND_TRUTH_DIR,
    as_of: date | None = None, session_factory: Callable[[], Session] | None = None,
    on_chunk: Callable[[RunReport, uuid.UUID | None], None] | None = None, cancelled: Callable[[], bool] | None = None,
) -> RunReport:
    """Generate documents for up to `residents` residents (None = all) of one dataset. Safe to re-run."""
    if source not in ("db", "synthea"):
        raise ValueError("source must be 'db' or 'synthea'")
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
            row = s.execute(Dataset.__table__.select().where(Dataset.dataset_version == dataset_version)).first()
            dataset_manifest = row.manifest if row is not None else None
    else:
        from anticipa.datafabric.synthea import SYNTHEA_DIR, load_manifest

        dataset_manifest = load_manifest(SYNTHEA_DIR / dataset_version)
    day = resolve_as_of(dataset_version, as_of, dataset_manifest)
    graph = load_family_graph(dataset_version, day, gt_root)
    writer = GroundTruthWriter(dataset_version, seed, day, gt_root)
    report = RunReport(dataset_version, seed, day.isoformat())

    def chunks():
        if source == "synthea":
            yield from contexts_from_synthea(dataset_version, limit=residents, graph=graph, as_of=day)
        else:
            with session_factory() as s:
                yield from contexts_from_db(s, dataset_version, chunk_size=chunk_size, limit=residents, graph=graph, as_of=day)

    with ThreadPoolExecutor(max_workers=8) as pool:
        for contexts in chunks():
            if cancelled and cancelled():
                break
            todo: list[ResidentContext] = []
            for ctx in contexts:
                report.residents_seen += 1
                if str(ctx.id) in writer.done:
                    report.skipped_done += 1
                elif ctx.deceased is not None and ctx.deceased <= day:
                    report.skipped_deceased += 1
                else:
                    todo.append(ctx)
            outputs = [build_resident(c, dataset_version, seed, day) for c in todo]
            docs = [d for o in outputs for d in o.docs]
            list(pool.map(lambda d: store.write_text(d.key, d.text), docs))  # idempotent overwrite
            if register and docs:
                with session_factory() as s:
                    report.documents_registered += register_documents(s, docs)
            writer.write_chunk(outputs)
            report.residents_generated += len(outputs)
            report.documents += len(docs)
            report.labels += sum(len(o.facts) for o in outputs)
            report.seconds = time.perf_counter() - started
            if on_chunk:
                on_chunk(report, contexts[-1].id if contexts else None)
    report.seconds = time.perf_counter() - started
    report.manifest = writer.finalize({"run": report.summary(), "source": source})
    return report
