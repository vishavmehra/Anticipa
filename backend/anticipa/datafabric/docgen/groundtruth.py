# ruff: noqa: E501
"""Ground-truth files for generated documents (Docs/09 §6; isolated from the application per ADR-008).

Layout under `data/ground_truth/<dataset_version>/` (the paths Docs/09 §6.2 defines, plus docgen-owned files):
    facts/planted_facts.{dev,test}.jsonl   gt.fact.v1       one label per planted (or absent) fact
    documents/document_index.jsonl         gt.document_index.v1   hashes and scenario tags, never text
    conflicts/conflict_groups.jsonl        gt.conflict_group.v1
    families/document_only_relatives.jsonl gt.family_edge.v1 (provenance document_only; the graph's own file is the
                                           data workstream's and is never touched here)
    enrichment/enrichment_items.jsonl      gt.enrichment.v1 (screenings/exclusions planted by enrich.py: structured or note-only)
    documents/docgen_ledger.jsonl          residents already generated (resume / idempotency)
    documents/docgen_manifest.json         seeds, versions, counts, quota report, per-file sha256
Files are UTF-8, LF, one object per line with sorted keys. Label ids are uuid5, so a regenerated label is the same label;
a crash between the label append and the ledger append can repeat one chunk's lines, so readers de-duplicate by `label_id`.
Nothing in this module (or the label files) is ever read by the pipeline, written to the database or to the document store.
"""

from __future__ import annotations

import hashlib
import math
import os
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path

import orjson

from anticipa.datafabric.docgen.context import GROUND_TRUTH_DIR
from anticipa.datafabric.docgen.phrases import LANGS
from anticipa.datafabric.docgen.planner import (
    CONTROL_P,
    GENERATOR,
    GENERATOR_VERSION,
    INJECTION_P,
    MAX_SCENARIOS,
    PRIMARY_P,
    SECONDARY_P,
)
from anticipa.datafabric.docstore import DocumentStore, content_hash, make_storage_key

FILES = {
    "facts_dev": "facts/planted_facts.dev.jsonl",
    "facts_test": "facts/planted_facts.test.jsonl",
    "doc_index": "documents/document_index.jsonl",
    "conflicts": "conflicts/conflict_groups.jsonl",
    "relatives": "families/document_only_relatives.jsonl",
    "enrichment": "enrichment/enrichment_items.jsonl",
    "ledger": "documents/docgen_ledger.jsonl",
}
MANIFEST = "documents/docgen_manifest.json"
# Docs/09 §5.4 floors, per language, for the 10,000-resident evaluation cohort (this generated set is its pool)
FLOORS = {
    **{f"DC-{n:02d}": 200 for n in range(1, 8)},
    **{f"DC-{n:02d}": 100 for n in range(8, 15)},
    "DC-15": 30,
}
CONTROL_SHARE = 0.15


def atomic_write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(orjson.dumps(obj, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))
    os.replace(tmp, path)


def dumps(obj) -> bytes:
    return orjson.dumps(obj, option=orjson.OPT_SORT_KEYS) + b"\n"


class SeedMismatch(RuntimeError):
    pass


class GroundTruthWriter:
    def __init__(
        self,
        dataset_version: str,
        seed: int,
        as_of: date,
        root: Path = GROUND_TRUTH_DIR,
        *,
        enrich: bool = True,
    ) -> None:
        self.dataset_version, self.seed, self.as_of, self.enrich = dataset_version, seed, as_of, enrich
        self.dir = Path(root) / dataset_version
        self.counts: Counter[str] = Counter()
        self.done: set[str] = set()
        self.docs_of: dict[str, set[str]] = {}  # resident -> ids of the documents whose labels are on disk
        self.created_at = datetime.now(UTC).isoformat()
        manifest_path = self.dir / MANIFEST
        ledger = self.dir / FILES["ledger"]
        if manifest_path.exists():
            old = orjson.loads(manifest_path.read_bytes())
            old_enrich = bool(old.get("enrichment", {}).get("enabled", False))
            if (
                ledger.exists()
                and ledger.stat().st_size
                and (old["seed"], old["generator_version"], old["as_of_date"], old_enrich)
                != (seed, GENERATOR_VERSION, as_of.isoformat(), enrich)
            ):
                raise SeedMismatch(
                    f"{self.dir} holds ground truth for seed={old['seed']} version={old['generator_version']} "
                    f"as_of={old['as_of_date']} enrichment={old_enrich}; use the same values or a different dataset_version"
                )
            self.counts.update(old.get("counts", {}))
            self.created_at = old.get("created_at", self.created_at)
        if ledger.exists():
            self.done = {
                orjson.loads(line)["resident_id"] for line in ledger.read_bytes().splitlines() if line
            }
        index = self.dir / FILES["doc_index"]
        if index.exists():
            for line in index.read_bytes().splitlines():
                if line:
                    d = orjson.loads(line)
                    self.docs_of.setdefault(d["resident_id"], set()).add(d["document_id"])
        if self.done and self.counts.get("residents", 0) != len(self.done):
            self._recount()  # the run that wrote these files died before it wrote the manifest

    def _recount(self) -> None:
        """Rebuild the manifest counts from the files themselves (crash recovery; same keys as `_count`)."""
        c: Counter[str] = Counter()
        language: dict[str, str] = {}
        for line in (self.dir / FILES["doc_index"]).read_bytes().splitlines():
            d = orjson.loads(line)
            lang = d["language"]
            language[d["resident_id"]] = lang
            c[f"documents/type/{d['document_type']}"] += 1
            c[f"documents/language/{lang}"] += 1
            c[f"documents/language_type/{lang}/{d['document_type']}"] += 1
            c[f"code_mixed_documents/{lang}"] += d["code_mixed"]
            c[f"unicode_variant_documents/{lang}"] += d["unicode_variant"]
        pairs: set[tuple[str, str]] = set()
        for name in ("facts_dev", "facts_test"):
            path = self.dir / FILES[name]
            for line in path.read_bytes().splitlines() if path.exists() else []:
                f = orjson.loads(line)
                lang = f["language"]
                c[f"labels/category/{f['category']}"] += 1
                c[f"labels/language/{lang}"] += 1
                c[f"labels/subject/{f['subject']}"] += 1
                c["labels/absent"] += not f["expected_present"]
                for sid in f["scenario_ids"]:
                    c[f"scenario_labels/{sid}/{lang}"] += 1
                    pairs.add((f["resident_id"], sid))
        for resident, sid in pairs:
            c[f"scenario_residents/{sid}/{language.get(resident, 'en')}"] += 1
        path = self.dir / FILES["enrichment"]
        for line in path.read_bytes().splitlines() if path.exists() else []:
            e = orjson.loads(line)
            c[f"enrichment/{e['concept']}/{e['mode']}/{e['status']}"] += 1
            c[f"enrichment_mode/{e['mode']}"] += 1
        for line in (self.dir / FILES["ledger"]).read_bytes().splitlines():
            item = orjson.loads(line)
            c["residents"] += 1
            c[f"residents_{'linked' if item.get('linked', True) else 'unlinked'}"] += 1
            c[f"residents/language/{language.get(item['resident_id'], 'en')}"] += 1
        self.counts = c

    def _append(self, name: str, lines: list[bytes]) -> None:
        if not lines:
            return
        path = self.dir / FILES[name]
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as fh:
            fh.write(b"".join(lines))

    def write_chunk(self, outputs: list) -> None:
        """Append labels first, then the ledger: a resident is only 'done' once its labels are on disk."""
        buckets: dict[str, list[bytes]] = {k: [] for k in FILES}
        for out in outputs:
            facts = "facts_dev" if out.split == "dev" else "facts_test"
            buckets[facts] += [dumps(f) for f in out.facts]
            buckets["doc_index"] += [dumps(r) for r in out.index]
            buckets["conflicts"] += [dumps(r) for r in out.conflicts]
            buckets["relatives"] += [dumps(r) for r in out.relatives]
            buckets["enrichment"] += [dumps(r) for r in out.enrichment]
            self._count(out)
        for name in ("facts_dev", "facts_test", "doc_index", "conflicts", "relatives", "enrichment"):
            self._append(name, buckets[name])
        self._append(
            "ledger",
            [
                dumps({"resident_id": str(o.resident_id), "documents": len(o.docs), "linked": o.linked})
                for o in outputs
            ],
        )
        self.done.update(str(o.resident_id) for o in outputs)
        for o in outputs:
            self.docs_of[str(o.resident_id)] = {str(d.id) for d in o.docs}

    def _count(self, out) -> None:
        c, lang = self.counts, out.language
        c["residents"] += 1
        c[f"residents_{'linked' if out.linked else 'unlinked'}"] += 1
        c[f"residents/language/{lang}"] += 1
        for sid in out.scenarios:
            c[f"scenario_residents/{sid}/{lang}"] += 1
        for doc in out.docs:
            c[f"documents/type/{doc.doc_type}"] += 1
            c[f"documents/language/{lang}"] += 1
            c[f"documents/language_type/{lang}/{doc.doc_type}"] += 1
            c[f"code_mixed_documents/{lang}"] += doc.code_mixed
            c[f"unicode_variant_documents/{lang}"] += doc.unicode_variant
        for e in out.enrichment:
            c[f"enrichment/{e['concept']}/{e['mode']}/{e['status']}"] += 1
            c[f"enrichment_mode/{e['mode']}"] += 1
        for f in out.facts:
            c[f"labels/category/{f['category']}"] += 1
            c[f"labels/language/{lang}"] += 1
            c[f"labels/subject/{f['subject']}"] += 1
            c["labels/absent"] += not f["expected_present"]
            for sid in f["scenario_ids"]:
                c[f"scenario_labels/{sid}/{lang}"] += 1

    def finalize(self, extra: dict | None = None) -> dict:
        files = {}
        for rel in FILES.values():
            path = self.dir / rel
            if path.exists():
                digest, lines, size = hashlib.sha256(), 0, 0
                with path.open("rb") as fh:
                    while block := fh.read(1 << 20):
                        digest.update(block)
                        lines += block.count(b"\n")
                        size += len(block)
                files[rel] = {"sha256": digest.hexdigest(), "lines": lines, "bytes": size}
        counts = dict(sorted((k, v) for k, v in self.counts.items() if v))
        manifest = {
            "schema_version": "gt.docgen_manifest.v1",
            "dataset_version": self.dataset_version,
            "generator": GENERATOR,
            "generator_version": GENERATOR_VERSION,
            "seed": self.seed,
            "as_of_date": self.as_of.isoformat(),
            "created_at": self.created_at,
            "updated_at": datetime.now(UTC).isoformat(),
            "split": {
                "dev": 0.2,
                "test": 0.8,
                "method": "sha256(docgen:split:resident_id); proposed, Docs/09 §5.6",
            },
            "scenario_config": {
                "control": CONTROL_P,
                "primary_each": PRIMARY_P,
                "secondary_each": SECONDARY_P,
                "injection": INJECTION_P,
                "max_scenarios_per_resident": MAX_SCENARIOS,
            },
            "counts": counts,
            "quota_report": quota_report(counts),
            "files": files,
            "notes": [
                "labels carry document_content_sha256; readers de-duplicate by label_id",
                "structured_record_present=false marks a screening mentioned by a document but absent from the structured record",
            ],
            **(extra or {}),
        }
        atomic_write_json(self.dir / MANIFEST, manifest)
        return manifest


def quota_report(counts: dict) -> dict:
    """Achieved vs the Docs/09 §5.4 floors (floors apply to the 10k evaluation cohort drawn from this pool)."""
    report: dict = {
        "basis": "per-language floors for the 10,000-resident evaluation cohort; this is the generated pool",
        "cells": {},
    }
    for lang in LANGS:
        residents = counts.get(f"residents/language/{lang}", 0)
        for sid, floor in FLOORS.items():
            got = counts.get(f"scenario_labels/{sid}/{lang}", 0)
            report["cells"][f"{sid}/{lang}"] = {
                "required": floor,
                "achieved": got,
                "status": "met" if got >= floor else "unmet",
            }
        need = math.ceil(CONTROL_SHARE * residents)
        got = counts.get(f"scenario_residents/DC-00/{lang}", 0)
        report["cells"][f"DC-00/{lang}"] = {
            "required": need,
            "achieved": got,
            "status": "met" if got >= need else "unmet",
        }
    return report


def verify_ground_truth(
    dataset_version: str, store: DocumentStore, *, root: Path = GROUND_TRUTH_DIR, max_docs: int | None = None
) -> dict:
    """Independent integrity check (Docs/09 §6.5 items 1, 2, 7): hashes, quotes, spans, unique label ids."""
    base = Path(root) / dataset_version
    result = Counter()
    docs: dict[str, str] = {}
    lines = [x for x in (base / FILES["doc_index"]).read_bytes().splitlines() if x]
    if (
        max_docs is not None and len(lines) > max_docs
    ):  # evenly spaced across the whole run, not just its first residents
        lines = lines[:: -(-len(lines) // max_docs)]
    for line in lines:
        d = orjson.loads(line)
        result["documents"] += 1
        key = make_storage_key(dataset_version, d["resident_id"], d["document_id"])
        try:
            text = store.read_text(key)
        except FileNotFoundError:
            result["missing_documents"] += 1
            continue
        if content_hash(text) != d["content_sha256"]:
            result["hash_mismatches"] += 1
        docs[d["document_id"]] = text
    seen: set[str] = set()
    for name in ("facts_dev", "facts_test"):
        path = base / FILES[name]
        if not path.exists():
            continue
        for line in path.read_bytes().splitlines():
            f = orjson.loads(line)
            if f["document_id"] is not None and f["document_id"] not in docs:
                continue  # outside the checked sample
            result["labels"] += 1
            if f["label_id"] in seen:
                result["duplicate_label_ids"] += 1
            seen.add(f["label_id"])
            text = docs.get(f["document_id"])
            q = f["quote"]
            if q is not None and (text is None or text[q["start"] : q["end"]] != q["text"] or not q["text"]):
                result["quote_mismatches"] += 1
            inj = f["injected_span"]
            if inj is not None and (text is None or not 0 <= inj["start"] < inj["end"] <= len(text)):
                result["span_errors"] += 1
    return dict(result)
