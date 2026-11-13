"""The fixed AI cohort (owner decision: at most 10,000 residents ever go through paid model inference).

`select_cohort` draws a deterministic, seeded, stratified sample (language x age band x sex x district, with a
reserved share of residents whose documents carry planted difficult cases), and `CohortManifest`
persists it so
every later extraction/embedding job can be checked against the exact same resident list. The manifest
lives in
the document store (`cohorts/<cohort_id>.json`: local disk in development, Supabase Storage in the cloud).

ADR-008: the optional `difficult_resident_ids` come from the evaluation side. This module only uses membership
("this resident has at least one planted scenario"), never labels; the manifest records a count, not which
residents or which scenarios, and the extraction pipeline never sees either.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections import Counter
from collections.abc import Collection, Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from anticipa.datafabric.models import Dataset, Document, Resident

MAX_COHORT_RESIDENTS = 10_000  # fixed by the owner; a larger cohort needs `override`
SCHEMA_VERSION = "ai-cohort.v1"
AGE_BANDS = ((0, 17), (18, 29), (30, 39), (40, 49), (50, 59), (60, 69), (70, 79), (80, 200))
DEFAULT_AS_OF = date(2026, 11, 1)  # fixed reference date so the same seed always gives the same cohort
COHORT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,80}$")
CONTROL_SCENARIO = "DC-00"  # docgen's control scenario: documents without a planted difficult case


class CohortManifest(BaseModel):
    schema_version: str = SCHEMA_VERSION
    cohort_id: str
    seed: int
    target_size: int
    size: int
    dataset_version: str | None
    as_of: date
    stratify_by: list[str] = ["language", "age_band", "sex", "district"]
    difficult_share_target: float
    difficult_count: int
    created_at: datetime
    distributions: dict[str, dict[str, int]]
    strata: int
    resident_ids_sha256: str
    resident_ids: list[uuid.UUID] = Field(default_factory=list, repr=False)

    def summary(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"resident_ids"})


def cohort_key(cohort_id: str) -> str:
    if not COHORT_ID_RE.fullmatch(cohort_id):
        raise ValueError(f"invalid cohort id: {cohort_id!r}")
    return f"cohorts/{cohort_id}.json"


def default_cohort_id(seed: int, size: int, dataset_version: str | None) -> str:
    return f"ai-{(dataset_version or 'all').lower()}-s{seed}-n{size}"


def ids_sha256(ids: Iterable[uuid.UUID]) -> str:
    return hashlib.sha256("\n".join(sorted(str(i) for i in ids)).encode("ascii")).hexdigest()


def age_band(birth: date | None, as_of: date) -> str:
    if birth is None:
        return "unknown"
    age = as_of.year - birth.year - ((as_of.month, as_of.day) < (birth.month, birth.day))
    for lo, hi in AGE_BANDS:
        if lo <= age <= hi:
            return f"{lo}+" if hi == 200 else f"{lo}-{hi}"
    return "unknown"


def _rank(seed: int, rid: uuid.UUID) -> str:
    return hashlib.sha256(f"{seed}:{rid}".encode("ascii")).hexdigest()


def allocate(sizes: dict[tuple, int], take: int) -> dict[tuple, int]:
    """Proportional allocation of `take` over strata (largest remainder, ties by key, never above a
    stratum)."""
    total = sum(sizes.values())
    take = min(take, total)
    if take <= 0 or total == 0:
        return dict.fromkeys(sizes, 0)
    exact = {k: take * n / total for k, n in sizes.items()}
    out = {k: min(math.floor(v), sizes[k]) for k, v in exact.items()}
    order = sorted(sizes, key=lambda k: (-(exact[k] - math.floor(exact[k])), str(k)))
    left = take - sum(out.values())
    while left > 0:
        progressed = False
        for k in order:
            if left and out[k] < sizes[k]:
                out[k] += 1
                left -= 1
                progressed = True
        if not progressed:
            break
    return out


def allocate_nested(sizes: dict[tuple, int], take: int) -> dict[tuple, int]:
    """Two-level proportional allocation: first across languages (the dimension the owner cares most about, so
    en/hi/ar proportions are exact up to rounding), then across the remaining strata inside each language."""
    by_lang: dict[Any, dict[tuple, int]] = {}
    for k, n in sizes.items():
        by_lang.setdefault(k[0], {})[k] = n
    per_lang = allocate({(lang,): sum(v.values()) for lang, v in by_lang.items()}, take)
    out: dict[tuple, int] = {}
    for lang, sub in by_lang.items():
        out.update(allocate(sub, per_lang[(lang,)]))
    return out


def select_cohort(
    session: Session,
    *,
    seed: int,
    size: int = MAX_COHORT_RESIDENTS,
    dataset_version: str | None = None,
    as_of: date = DEFAULT_AS_OF,
    difficult_resident_ids: Collection[uuid.UUID] | None = None,
    difficult_share: float = 0.20,
    cohort_id: str | None = None,
    override: bool = False,
    now: datetime | None = None,
) -> CohortManifest:
    """Deterministic stratified sample of residents that have at least one document. Same seed, same
    population,
    same arguments: the same resident list, in the same order."""
    if size < 1:
        raise ValueError("size must be positive")
    if size > MAX_COHORT_RESIDENTS and not override:
        raise ValueError(
            f"the AI cohort is fixed at {MAX_COHORT_RESIDENTS} residents; larger needs an override"
        )
    if not 0 <= difficult_share <= 1:
        raise ValueError("difficult_share must be between 0 and 1")
    has_doc = select(Document.id).where(Document.resident_id == Resident.id).exists()
    stmt = select(
        Resident.id,
        Resident.preferred_language,
        Resident.birth_date,
        Resident.administrative_sex,
        Resident.district_code,
    ).where(has_doc)
    if dataset_version:
        stmt = stmt.where(
            Resident.dataset_id
            == select(Dataset.id).where(Dataset.dataset_version == dataset_version).scalar_subquery()
        )
    rows = session.execute(stmt).all()
    difficult = set(difficult_resident_ids or ())
    strata: dict[tuple, list[uuid.UUID]] = {}
    for rid, lang, birth, sex, district in rows:
        key = (lang or "unknown", age_band(birth, as_of), sex or "unknown", district or "unknown")
        strata.setdefault(key, []).append(rid)
    for members in strata.values():
        members.sort(key=lambda r: _rank(seed, r))  # seeded, order-independent of how the rows came back

    target = min(size, len(rows))
    chosen: list[uuid.UUID] = []
    taken: set[uuid.UUID] = set()
    want_difficult = round(target * difficult_share) if difficult else 0
    if want_difficult:
        pool = {k: [r for r in v if r in difficult] for k, v in strata.items()}
        quota = allocate_nested({k: len(v) for k, v in pool.items() if v}, want_difficult)
        for k in sorted(quota, key=str):
            picked = pool[k][: quota[k]]
            chosen += picked
            taken.update(picked)
    rest = {k: [r for r in v if r not in taken] for k, v in strata.items()}
    quota = allocate_nested({k: len(v) for k, v in rest.items() if v}, target - len(chosen))
    for k in sorted(quota, key=str):
        chosen += rest[k][: quota[k]]
    chosen = sorted(chosen, key=lambda r: _rank(seed, r))  # stable, seed-defined order

    by_id = {r[0]: r for r in rows}
    dims = {"language": Counter(), "age_band": Counter(), "sex": Counter(), "district": Counter()}
    for rid in chosen:
        _, lang, birth, sex, district = by_id[rid]
        dims["language"][lang or "unknown"] += 1
        dims["age_band"][age_band(birth, as_of)] += 1
        dims["sex"][sex or "unknown"] += 1
        dims["district"][district or "unknown"] += 1
    return CohortManifest(
        cohort_id=cohort_id or default_cohort_id(seed, size, dataset_version),
        seed=seed,
        target_size=size,
        size=len(chosen),
        dataset_version=dataset_version,
        as_of=as_of,
        difficult_share_target=difficult_share if difficult else 0.0,
        difficult_count=sum(1 for r in chosen if r in difficult),
        created_at=now or datetime.now(UTC),
        distributions={k: dict(sorted(v.items())) for k, v in dims.items()},
        strata=len(
            {
                (by_id[r][1] or "unknown", age_band(by_id[r][2], as_of), by_id[r][3], by_id[r][4])
                for r in chosen
            }
        ),
        resident_ids_sha256=ids_sha256(chosen),
        resident_ids=chosen,
    )


# ----------------------------------------------------------------------------------------------- persistence
def save_manifest(store: Any, manifest: CohortManifest) -> str:
    """Persist (overwrite allowed only by the caller's own decision). Returns the storage key."""
    key = cohort_key(manifest.cohort_id)
    store.write_text(key, manifest.model_dump_json(indent=1))
    return key


def load_manifest(store: Any, cohort_id: str) -> CohortManifest:
    """Load and verify a manifest. Raises FileNotFoundError (missing) or ValueError (tampered /
    inconsistent)."""
    manifest = CohortManifest.model_validate(json.loads(store.read_text(cohort_key(cohort_id))))
    if manifest.cohort_id != cohort_id or ids_sha256(manifest.resident_ids) != manifest.resident_ids_sha256:
        raise ValueError(f"cohort manifest {cohort_id!r} failed its integrity check")
    if len(manifest.resident_ids) != manifest.size or manifest.size > MAX_COHORT_RESIDENTS:
        raise ValueError(f"cohort manifest {cohort_id!r} has an invalid size")
    return manifest


def docgen_difficult_residents(dataset_version: str, root: Path | None = None) -> set[uuid.UUID]:
    """Residents with at least one planted non-control scenario, from the docgen ground-truth document index.

    Only `resident_id` and the *membership* of a non-control scenario are read (never text, spans or labels).
    Returns an empty set when the index does not exist yet."""
    from anticipa.datafabric.docgen.context import GROUND_TRUTH_DIR

    path = (root or GROUND_TRUTH_DIR) / dataset_version / "documents" / "document_index.jsonl"
    out: set[uuid.UUID] = set()
    if not path.is_file():
        return out
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            rec = json.loads(line)
            if any(s != CONTROL_SCENARIO for s in rec.get("scenario_ids") or ()):
                out.add(uuid.UUID(str(rec["resident_id"])))
    return out
