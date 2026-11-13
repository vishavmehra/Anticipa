"""Ground-truth reader (ADR-008). THE ONLY code that reads `data/ground_truth/<dataset_version>/`.

Callers: anticipa.evaluation.runner / cli. Schemas read: gt.fact.v1 (facts/planted_facts.{dev,test}.jsonl),
gt.document_index.v1, gt.conflict_group.v1, gt.enrichment.v1, gt.family_edge.v1, gt.docgen_manifest.v1 (written by
anticipa.datafabric.docgen). Nothing read here is ever written to a table, a prompt, an index or an API payload.
Instruction: deliver a working, tested, demonstrable Anticipa MVP including the evaluation report.

Differences from Docs/09 §6 (the generator, not this reader, decides them): there is no gt_manifest.json /
CHANGELOG.jsonl / freeze flag; integrity is the per-file sha256 that docgen_manifest.json records. The reader
de-duplicates labels by `label_id` (docgen can repeat one chunk after a crash).
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import orjson

from anticipa.config import ROOT_DIR

GT_ROOT = ROOT_DIR / "data" / "ground_truth"
MANIFEST = "documents/docgen_manifest.json"
FACT_FILES = {"dev": "facts/planted_facts.dev.jsonl", "test": "facts/planted_facts.test.jsonl"}
DOC_INDEX = "documents/document_index.jsonl"
CONFLICTS = "conflicts/conflict_groups.jsonl"
ENRICHMENT = "enrichment/enrichment_items.jsonl"
LEDGER = "test_access.jsonl"
SPLITS = ("dev", "test", "all")


class GtError(RuntimeError):
    code = "GT_ERROR"


class GtIntegrityError(GtError):
    code = "GT_INTEGRITY_ERROR"


class GtDocumentMismatch(GtError):
    code = "GT_DOCUMENT_MISMATCH"


class GtVersionMismatch(GtError):
    code = "GT_VERSION_MISMATCH"


@dataclass
class GroundTruth:
    dataset_version: str
    root: Path
    split: str
    manifest: dict[str, Any]
    ground_truth_id: str
    manifest_sha256: str
    as_of: date
    facts: list[dict[str, Any]] = field(default_factory=list)
    doc_index: dict[str, dict[str, Any]] = field(default_factory=dict)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    enrichment: list[dict[str, Any]] = field(default_factory=list)
    checksum_verified: bool = False

    def tiers(self) -> dict[str, int]:
        """Label counts per quality tier (09 LBL-01). Programmatic labels are never described as reviewed."""
        tiers = dict.fromkeys(("programmatic", "auto_validated", "human_reviewed", "adjudicated"), 0)
        for f in self.facts:
            tiers[f.get("label_origin", "programmatic")] = tiers.get(f.get("label_origin", "programmatic"), 0) + 1
        return tiers


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("rb") as fh:
        for line in fh:
            if line.strip():
                yield orjson.loads(line)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(1 << 20):
            h.update(block)
    return h.hexdigest()


def verify_checksums(root: Path, manifest: dict[str, Any]) -> None:
    """Every file the manifest lists must hash to the recorded sha256 (09 §14 GT_INTEGRITY_ERROR)."""
    for rel, meta in sorted(manifest.get("files", {}).items()):
        path = root / rel
        if not path.is_file():
            raise GtIntegrityError(f"{rel} is listed in the manifest but missing")
        if sha256_file(path) != meta["sha256"]:
            raise GtIntegrityError(f"{rel} does not match the sha256 recorded in the manifest (edited or regenerated)")


def _splits(split: str) -> tuple[str, ...]:
    if split not in SPLITS:
        raise ValueError(f"split must be one of {SPLITS}")
    return ("dev", "test") if split == "all" else (split,)


def load_ground_truth(
    dataset_version: str,
    *,
    resident_ids: Iterable[str] | None = None,
    split: str = "dev",
    root: Path | None = None,
    verify: bool = True,
) -> GroundTruth:
    """Load the labels of `resident_ids` (None = everyone) for one split. Raises GtError subclasses, fail closed."""
    base = Path(root or GT_ROOT) / dataset_version
    mpath = base / MANIFEST
    if not mpath.is_file():
        raise GtIntegrityError(f"no ground truth for {dataset_version!r}: {mpath} is missing")
    raw = mpath.read_bytes()
    manifest = orjson.loads(raw)
    if manifest.get("dataset_version") != dataset_version:
        raise GtVersionMismatch(f"ground truth is for {manifest.get('dataset_version')!r}, not {dataset_version!r}")
    if verify:
        verify_checksums(base, manifest)
    msha = hashlib.sha256(raw).hexdigest()
    wanted = {str(r) for r in resident_ids} if resident_ids is not None else None
    use = _splits(split)
    gt = GroundTruth(
        dataset_version=dataset_version, root=base, split=split, manifest=manifest, manifest_sha256=msha,
        ground_truth_id=f"{dataset_version}/{manifest.get('generator_version', 'docgen')}@{msha[:12]}",
        as_of=date.fromisoformat(manifest["as_of_date"]), checksum_verified=verify,
    )  # fmt: skip
    seen: set[str] = set()
    for sp in use:
        path = base / FACT_FILES[sp]
        if not path.is_file():
            continue
        for rec in iter_jsonl(path):
            if (wanted is not None and rec["resident_id"] not in wanted) or rec["label_id"] in seen:
                continue
            seen.add(rec["label_id"])
            gt.facts.append(rec)
    for rec in iter_jsonl(base / DOC_INDEX):
        if rec["split"] in use and (wanted is None or rec["resident_id"] in wanted):
            gt.doc_index[rec["document_id"]] = rec
    resident_split = {d["resident_id"] for d in gt.doc_index.values()}
    if (base / CONFLICTS).is_file():
        gt.conflicts = [c for c in iter_jsonl(base / CONFLICTS) if c["resident_id"] in resident_split]
    if (base / ENRICHMENT).is_file():
        gt.enrichment = [e for e in iter_jsonl(base / ENRICHMENT) if e["resident_id"] in resident_split]
    return gt


def preflight(dataset_version: str, *, resident_ids: Iterable[str] | None = None, root: Path | None = None, verify: bool = True) -> dict[str, Any]:
    """Everything a run needs EXCEPT the labels: manifest, file checksums and the document index (hashes and splits only).
    Never opens a facts file and never touches the test access ledger, so it is safe to run before a final run."""
    base = Path(root or GT_ROOT) / dataset_version
    mpath = base / MANIFEST
    if not mpath.is_file():
        raise GtIntegrityError(f"no ground truth for {dataset_version!r}: {mpath} is missing")
    raw = mpath.read_bytes()
    manifest = orjson.loads(raw)
    if manifest.get("dataset_version") != dataset_version:
        raise GtVersionMismatch(f"ground truth is for {manifest.get('dataset_version')!r}, not {dataset_version!r}")
    if verify:
        verify_checksums(base, manifest)
    wanted = {str(r) for r in resident_ids} if resident_ids is not None else None
    index = {rec["document_id"]: rec for rec in iter_jsonl(base / DOC_INDEX) if wanted is None or rec["resident_id"] in wanted}
    return {"manifest_sha256": hashlib.sha256(raw).hexdigest(), "generator_version": manifest.get("generator_version"), "as_of": manifest.get("as_of_date"),
            "manifest_updated_at": manifest.get("updated_at"), "checksums_verified": verify, "files_checked": len(manifest.get("files", {})), "doc_index": index}  # fmt: skip


def append_test_access(gt: GroundTruth, *, actor: str, run_id: str, command: str) -> None:
    """Append-only access ledger for reads of the test split (09 §5.6). Never rewritten by this code."""
    rec = {"timestamp": datetime.now(UTC).isoformat(), "actor": actor, "run_id": run_id, "command": command,
           "ground_truth_id": gt.ground_truth_id, "split": gt.split}  # fmt: skip
    with (gt.root / LEDGER).open("ab") as fh:
        fh.write(orjson.dumps(rec, option=orjson.OPT_SORT_KEYS) + b"\n")


def prior_final_runs(root: Path | None, dataset_version: str) -> int:
    path = Path(root or GT_ROOT) / dataset_version / LEDGER
    if not path.is_file():
        return 0
    return sum(1 for _ in iter_jsonl(path))


def actor_name() -> str:
    return os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"
