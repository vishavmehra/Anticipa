"""Storage profiles (owner decision "lean storage after 100k", 2026-11-11).

* `full`  - every resident of the first `full_batches` batches (100,000 residents at batch size 500) keeps full detail:
            all conditions, encounters, medications, immunizations and the allow-listed preventive-care observations,
            procedures and diagnostic reports, PLUS every record whose code a guideline selector references.
* `rules` - all later residents keep only what the deterministic rules can use: Patient demographics (`residents`),
            family edges (linker), every Condition / Procedure / Observation / DiagnosticReport whose (system, code) is
            referenced by a selector in `config/guidelines/rules_draft.json`, and each resident's most recent Encounter
            (so the 5-year `record_coverage` prerequisite - "any record in the window" - gives the same answer).
            Medications, immunizations and all other encounters/observations are not stored.

The raw NDJSON.gz stays on disk for provenance; `clinical_records.source_reference` is unchanged, so any skipped record
can be recovered by re-ingesting the batch with `--profile full`. The profile is recorded per batch
(`datasets.manifest.ingest.batches[i].profile`), per family block (`family_linker.blocks[k].profile`) and per resident
(`residents.source_version` carries a `;profile=rules` suffix; no suffix = full).
"""
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from anticipa.config import ROOT_DIR

FULL = "full"
RULES = "rules"
PROFILES = (FULL, RULES)
RULES_FILE = ROOT_DIR / "config" / "guidelines" / "rules_draft.json"
SYSTEM_URI = {
    "SNOMED-CT": "http://snomed.info/sct",
    "LOINC": "http://loinc.org",
}  # rules_draft.json naming -> FHIR URI
MARKER = ";profile="

PROFILE_DEFINITIONS = {
    FULL: (
        "Full detail: all conditions, encounters, medications, immunizations and the allow-listed preventive-care "
        "observations, procedures and diagnostic reports, plus every record whose code a guideline selector references."
    ),
    RULES: (
        "Rule-relevant detail only: demographics, family links, records whose code a guideline selector references "
        "(config/guidelines/rules_draft.json) and each resident's most recent encounter. No medications, immunizations, "
        "other encounters or other observations are stored; the raw files remain for provenance."
    ),
}


@dataclass(frozen=True)
class RuleCodes:
    codes: frozenset[tuple[str, str]]  # (FHIR system URI, code)
    digest: str  # sha256 of the sorted codes; changes iff the referenced code set changes
    selectors: int


@lru_cache
def load_rule_codes(path: Path = RULES_FILE) -> RuleCodes:
    """Every (system, code) referenced by a selector in the guideline rules draft (all clinical categories)."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    selectors = doc["selector_library"]
    codes: set[tuple[str, str]] = set()
    for key, selector in selectors.items():
        for entry in selector.get("codes", []):
            try:
                codes.add((SYSTEM_URI[entry["system"]], str(entry["code"])))
            except KeyError as exc:  # never silently drop a code from the profile
                raise ValueError(
                    f"selector {key!r}: unsupported code system {entry.get('system')!r}"
                ) from exc
    digest = hashlib.sha256("\n".join(f"{s}|{c}" for s, c in sorted(codes)).encode()).hexdigest()
    return RuleCodes(frozenset(codes), digest, len(selectors))


def profile_for_batch(batch_index: int, full_batches: int) -> str:
    return FULL if batch_index < full_batches else RULES


def marked_source_version(version: str | None, profile: str) -> str | None:
    """`residents.source_version` value: the generator version, plus `;profile=rules` for lean residents."""
    return f"{version or ''}{MARKER}{RULES}" if profile == RULES else version


def profile_of(source_version: str | None) -> str:
    return RULES if f"{MARKER}{RULES}" in (source_version or "") else FULL
