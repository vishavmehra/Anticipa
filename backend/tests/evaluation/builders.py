"""Hand-built labels, predictions and ground-truth files for the evaluation tests (deliver a working, tested,
demonstrable Anticipa MVP including the evaluation report). Everything is synthetic and tiny so every metric can be
computed by hand. Loaded via sys.path by conftest.py (importlib import mode)."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from anticipa.evaluation.extraction import DocInfo
from anticipa.evaluation.matching import Label, Pred

NS = uuid.UUID("6f1c2a9e-5b7d-4c3e-9a1f-2d8e4b6c0a17")


def uid(*parts: Any) -> str:
    return str(uuid.uuid5(NS, ":".join(str(p) for p in parts)))


def label(i: int, *, doc: str = "doc-1", res: str = "res-1", lang: str = "en", category: str = "family_history",
          fact_type: str = "colorectal_cancer", subject: str = "family_member", relationship: str | None = "mother",
          assertion: str = "positive", temporal: str = "historical", span: tuple[int, int] | None = None, present: bool = True,
          scenarios: tuple[str, ...] = (), event_date: dict | None = None, age: int | None = None, forbidden: tuple = (),
          concept_id: str | None = "same", injected: tuple[int, int] | None = None, group: str | None = None) -> Label:  # fmt: skip
    span = span if span is not None else (i * 100, i * 100 + 40)
    return Label(
        label_id=uid("label", i), resident_id=res, document_id=doc, language=lang, document_type="clinical_note", category=category,
        acceptable_categories=(), fact_type=fact_type, concept_id=fact_type if concept_id == "same" else concept_id,
        names=(fact_type,), subject_type=subject, relationship=relationship if subject == "family_member" else None,
        assertion=assertion, temporal=temporal, event_date=event_date, age_at_onset=age, spans=(span,) if present and span else (),
        expected_present=present, scenario_ids=scenarios, injected_span=injected, conflict_group_id=group, duplicate_of=None,
        method="template", split="dev", forbidden=forbidden,
    )  # fmt: skip


def pred_of(lab: Label, **over: Any) -> Pred:
    """A prediction that is exactly right for `lab` (accepted, verified); override fields to make it wrong."""
    base: dict[str, Any] = {
        "fact_id": uid("pred", lab.label_id, over.pop("salt", "")), "resident_id": lab.resident_id, "document_id": lab.document_id or "doc-1",
        "category": lab.category, "fact_type": lab.fact_type, "concept": None, "subject_type": lab.subject_type,
        "relationship": lab.relationship, "assertion": lab.assertion, "temporal": lab.temporal, "disposition": "accepted",
        "quote_class": "verified", "span": lab.spans[0] if lab.spans else (0, 5), "event_date": None, "age_at_onset": lab.age_at_onset,
        "certainty": "high", "provider": "claude", "model_id": "haiku-test", "prompt_version": "evidence.extract@1",
    }  # fmt: skip
    base.update(over)
    return Pred(**base)


def stray(n: int, *, doc: str = "doc-1", res: str = "res-1", disposition: str = "accepted", **over: Any) -> Pred:
    """A fact at a position no label covers (a hallucination if accepted)."""
    base: dict[str, Any] = {
        "fact_id": uid("stray", doc, n), "resident_id": res, "document_id": doc, "category": "condition", "fact_type": "invented_condition",
        "concept": None, "subject_type": "resident", "relationship": None, "assertion": "positive", "temporal": "current",
        "disposition": disposition, "quote_class": "verified", "span": (90_000 + n * 100, 90_000 + n * 100 + 20),
        "certainty": "medium", "provider": "claude", "model_id": "haiku-test",
    }  # fmt: skip
    base.update(over)
    return Pred(**base)


def docs(*ids: str, lang: str = "en", status: str = "extracted", res: str = "res-1") -> dict[str, DocInfo]:
    return {d: DocInfo(d, res, lang, "clinical_note", status, "template") for d in ids}


# ----------------------------------------------------------------------------------------------- ground-truth files
def write_gt(root: Path, dataset_version: str, facts: list[dict], doc_index: list[dict], *, enrichment: list[dict] | None = None,
             split_files: bool = True, as_of: str = "2026-11-08") -> Path:  # fmt: skip
    """A minimal docgen-shaped ground-truth directory with a valid per-file sha256 manifest."""
    base = root / dataset_version
    files: dict[str, dict] = {}

    def put(rel: str, rows: list[dict]) -> None:
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        data = b"".join(json.dumps(r, sort_keys=True).encode() + b"\n" for r in rows)
        path.write_bytes(data)
        files[rel] = {"sha256": hashlib.sha256(data).hexdigest(), "lines": len(rows), "bytes": len(data)}

    put("facts/planted_facts.dev.jsonl", [f for f in facts if f["split"] == "dev"])
    put("facts/planted_facts.test.jsonl", [f for f in facts if f["split"] == "test"])
    put("documents/document_index.jsonl", doc_index)
    put("conflicts/conflict_groups.jsonl", [])
    put("enrichment/enrichment_items.jsonl", enrichment or [])
    manifest = {"schema_version": "gt.docgen_manifest.v1", "dataset_version": dataset_version, "generator_version": "test_v1", "seed": 1,
                "as_of_date": as_of, "files": files}  # fmt: skip
    (base / "documents").mkdir(parents=True, exist_ok=True)
    (base / "documents" / "docgen_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return base


def gt_fact(lab: Label, document_id: str, sha: str, *, split: str = "dev") -> dict[str, Any]:
    """A gt.fact.v1 dict equivalent to `lab` (the reader/Label round trip is tested)."""
    return {
        "label_id": lab.label_id, "resident_id": lab.resident_id, "document_id": document_id, "document_content_sha256": sha, "language": lab.language,
        "document_type": lab.document_type, "category": lab.category, "acceptable_categories": [], "fact_type": lab.fact_type,
        "concept_id": lab.concept_id, "value": {}, "subject": {"family_member": "relative", "other": "other_person"}.get(lab.subject_type, lab.subject_type),
        "subject_type": lab.subject_type, "relationship": lab.relationship, "assertion": lab.assertion, "temporal": lab.temporal, "event_date": lab.event_date,
        "age_at_onset": lab.age_at_onset, "quote": {"start": lab.spans[0][0], "end": lab.spans[0][1], "text": "x"} if lab.spans else None,
        "acceptable_quote_spans": [], "expected_present": lab.expected_present, "scenario_ids": list(lab.scenario_ids),
        "injected_span": None, "conflict_group_id": None, "duplicate_of": None, "forbidden_interpretations": [], "label_origin": "programmatic",
        "generation": {"method": "template"}, "split": split, "schema_version": "gt.fact.v1",
    }  # fmt: skip
