# ruff: noqa: E501
"""Seeded enrichment: realistic preventive-screening history that Synthea does not produce (owner request).

Synthea gives eligible residents almost no completed screenings or exclusions (no hysterectomy, HPV tests or family
history; mammograms only on the cancer path), so without enrichment every resident would look overdue. This step plants,
per resident and deterministically from (seed, dataset_version, resident_id):
  (a) completion records (colonoscopy, FIT, mammogram, Pap, HPV test) and exclusions (hysterectomy, total colectomy,
      bilateral mastectomy) as `anticipa.clinical_records` rows: source_system='anticipa_enrichment',
      source_reference='enrichment://<dataset_version>/<resident_id>/<concept>/<id>', synthetic_status='synthetic';
  (b) for a share of them, the same completion documented ONLY in a clinical note (no structured row), the demo story
      "Anticipa finds screenings that exist only in notes".
Family history stays document-only (docgen scenarios + the family graph). Every item is written to the ground truth
(`enrichment/enrichment_items.jsonl`). Codes are the evidence selectors of config/guidelines/rules_draft.json.
Real Synthea records win: a screening family the resident already has is never enriched.

ASSUMPTIONS (documented in the manifest; magnitudes follow commonly reported US survey figures such as CDC NHIS/BRFSS
up-to-date rates, but they are chosen for a plausible demo, not fitted to any dataset, and carry no clinical claim):
  A-E1 up to date: CRC 60% (ages 45-75), mammography 70% (women 40-74), cervical 75% (women 21-65);
  A-E2 outside those ages the rate is lower (CRC 35% at 76+, mammography 40% at 75+);
  A-E3 not up to date: 35% have an older (stale) completion, the rest none;
  A-E4 modality: CRC colonoscopy 65% / FIT 35%; cervical Pap 65% / HPV test 35%;
  A-E5 up to date means inside the interval (colonoscopy 10 y, FIT 1 y, mammogram 2 y, Pap 3 y, HPV 5 y);
  A-E6 exclusions: hysterectomy in women 35+, 3% rising 0.5 points per year of age to 25% (half total hysterectomy);
      total colectomy and bilateral mastectomy 0.4% each from age 35;
  A-E7 30% of completions and 40% of exclusions are documented in a note only (no structured record);
  A-E8 every planted test result is normal (results are not part of this story).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from anticipa.core.ids import uuid5_of
from anticipa.datafabric.docgen.context import ENRICHMENT_SOURCE, Rec, ResidentContext
from anticipa.datafabric.docgen.planner import age_on, make_rng, rand_date

ENRICHMENT_VERSION = "enrich/1"
SNOMED, LOINC = "http://snomed.info/sct", "http://loinc.org"
NEGATIVE = {"system": SNOMED, "code": "260385009", "display": "Negative (qualifier value)"}

# concept -> (resource_type, category, code_system, code, display, tag, fact_type [rules_draft.json selector name])
CODES = {
    "colonoscopy": (
        "Procedure",
        "procedure",
        SNOMED,
        "73761001",
        "Colonoscopy (procedure)",
        "colonoscopy",
        "colonoscopy",
    ),
    "fit": (
        "Observation",
        "observation",
        LOINC,
        "57905-2",
        "Hemoglobin [Presence] in Stool from gastrointestinal lower by Immunoassay --1st specimen",
        "fit_fobt",
        "fit",
    ),
    "mammography": (
        "Procedure",
        "procedure",
        SNOMED,
        "24623002",
        "Screening mammography (procedure)",
        "mammography",
        "screening_mammogram",
    ),
    "pap_smear": (
        "Observation",
        "observation",
        LOINC,
        "10524-7",
        "Microscopic observation [Identifier] in Cervix by Cyto stain",
        "cervical_cytology",
        "pap_smear",
    ),
    "hpv_test": (
        "Observation",
        "observation",
        LOINC,
        "21440-3",
        "Human papilloma virus 16+18+31+33+35+45+51+52+56 DNA [Presence] in Cervix by Probe",
        "hpv_test",
        "hpv_test",
    ),
    "hysterectomy": (
        "Procedure",
        "procedure",
        SNOMED,
        "236886002",
        "Hysterectomy (procedure)",
        "hysterectomy",
        "hysterectomy",
    ),
    "total_hysterectomy": (
        "Procedure",
        "procedure",
        SNOMED,
        "116140006",
        "Total hysterectomy (procedure)",
        "hysterectomy",
        "total_hysterectomy",
    ),
    "total_colectomy": (
        "Procedure",
        "procedure",
        SNOMED,
        "26390003",
        "Total colectomy (procedure)",
        "total_colectomy",
        "total_colectomy",
    ),
    "bilateral_mastectomy": (
        "Procedure",
        "procedure",
        SNOMED,
        "27865001",
        "Bilateral mastectomy (procedure)",
        "bilateral_mastectomy",
        "bilateral_mastectomy",
    ),
}
FAMILIES = {"crc": {"colonoscopy", "fit"}, "breast": {"mammography"}, "cervical": {"pap_smear", "hpv_test"}}
RATES = {
    "up_to_date": {
        "crc": 0.60,
        "crc_76_plus": 0.35,
        "mammography": 0.70,
        "mammography_75_plus": 0.40,
        "cervical": 0.75,
    },
    "stale_given_not_up_to_date": 0.35,
    "modality": {"colonoscopy": 0.65, "pap_smear": 0.65},
    "interval_years": {"colonoscopy": 10, "fit": 1, "mammography": 2, "pap_smear": 3, "hpv_test": 5},
    "stale_years": {
        "colonoscopy": (11, 15),
        "fit": (2, 6),
        "mammography": (3, 8),
        "pap_smear": (4, 9),
        "hpv_test": (6, 10),
    },
    "hysterectomy": {"from_age": 35, "base": 0.03, "per_year": 0.005, "cap": 0.25, "total_share": 0.5},
    "total_colectomy": {"from_age": 35, "p": 0.004},
    "bilateral_mastectomy": {"from_age": 35, "p": 0.004},
    "note_only_share": {"completion": 0.30, "exclusion": 0.40},
    "min_age": {"colonoscopy": 40, "fit": 40, "mammography": 35, "pap_smear": 21, "hpv_test": 25},
}


@dataclass
class EnrichedItem:
    concept: str  # key of CODES (hysterectomy may be total_hysterectomy for the structured row)
    when: date
    mode: str  # structured | note_only
    status: str  # up_to_date | stale | exclusion
    family: str  # crc | breast | cervical | exclusion
    precision: str = "day"  # how a note-only statement writes the date

    @property
    def proc(self) -> str:  # the docgen procedure concept used in text and labels
        return "hysterectomy" if self.concept == "total_hysterectomy" else self.concept


def _existing_families(ctx: ResidentContext) -> set[str]:
    have = {r.tag for r in ctx.procedures if r.tag} | {t for t in ctx.observations}
    out = set()
    for fam, concepts in FAMILIES.items():
        tags = {CODES[c][5] for c in concepts} | ({"fobt_screening"} if fam == "crc" else set())
        if tags & have:
            out.add(fam)
    out |= {t for t in ("hysterectomy", "total_colectomy", "bilateral_mastectomy") if t in have}
    return out


def plan_enrichment(
    ctx: ResidentContext, seed: int, dataset_version: str, as_of: date, *, control: bool = False
) -> list[EnrichedItem]:
    """Pure function of (seed, version, resident, existing records); `control` residents get day-precision dates only."""
    rng = make_rng(seed, dataset_version, ctx.id, "enrich")
    age, female = age_on(ctx.birth_date, as_of), ctx.sex == "female"
    have = _existing_families(ctx)
    items: list[EnrichedItem] = []
    newest = as_of - timedelta(days=30)

    def when_in(concept: str, lo_years: float, hi_years: float) -> date | None:
        """A date in [as_of - hi_years, as_of - lo_years] that is also after the test's minimum age; None if impossible."""
        lo = max(
            ctx.birth_date + timedelta(days=int(365.25 * RATES["min_age"][concept])),
            as_of - timedelta(days=int(365.25 * hi_years)),
        )
        hi = min(newest, as_of - timedelta(days=int(365.25 * lo_years)))
        return None if lo > hi else rand_date(rng, lo, hi)

    def mode_of(kind: str) -> str:
        return "note_only" if rng.random() < RATES["note_only_share"][kind] else "structured"

    def completion(family: str, concept: str, p_up: float) -> None:
        if family in have:
            return
        draw = rng.random()
        interval = RATES["interval_years"][concept]
        if draw < p_up:
            when, status = when_in(concept, 0, interval), "up_to_date"
        elif draw < p_up + (1 - p_up) * RATES["stale_given_not_up_to_date"]:
            when, status = when_in(concept, *RATES["stale_years"][concept]), "stale"
        else:
            return
        mode = mode_of("completion")
        if when is not None:
            items.append(EnrichedItem(concept, when, mode, status, family))

    # exclusions first: they remove the corresponding screening (a real exclusion record counts too)
    excluded = {
        fam
        for tag, fam in (
            ("hysterectomy", "cervical"),
            ("total_colectomy", "crc"),
            ("bilateral_mastectomy", "breast"),
        )
        if tag in have
    }
    h = RATES["hysterectomy"]
    if female and age >= h["from_age"] and "hysterectomy" not in have:
        if rng.random() < min(h["base"] + h["per_year"] * (age - h["from_age"]), h["cap"]):
            concept = "total_hysterectomy" if rng.random() < h["total_share"] else "hysterectomy"
            when = rand_date(
                rng, ctx.birth_date + timedelta(days=int(365.25 * 30)), as_of - timedelta(days=365)
            )
            items.append(EnrichedItem(concept, when, mode_of("exclusion"), "exclusion", "exclusion"))
            excluded.add("cervical")
    if (
        age >= RATES["total_colectomy"]["from_age"]
        and "total_colectomy" not in have
        and rng.random() < RATES["total_colectomy"]["p"]
    ):
        when = rand_date(rng, ctx.birth_date + timedelta(days=int(365.25 * 30)), as_of - timedelta(days=365))
        items.append(EnrichedItem("total_colectomy", when, mode_of("exclusion"), "exclusion", "exclusion"))
        excluded.add("crc")
    if (
        female
        and age >= RATES["bilateral_mastectomy"]["from_age"]
        and "bilateral_mastectomy" not in have
        and rng.random() < RATES["bilateral_mastectomy"]["p"]
    ):
        when = rand_date(rng, ctx.birth_date + timedelta(days=int(365.25 * 30)), as_of - timedelta(days=365))
        items.append(
            EnrichedItem("bilateral_mastectomy", when, mode_of("exclusion"), "exclusion", "exclusion")
        )
        excluded.add("breast")

    up = RATES["up_to_date"]
    if "crc" not in excluded and age >= 45:
        crc = "colonoscopy" if rng.random() < RATES["modality"]["colonoscopy"] else "fit"
        completion("crc", crc, up["crc"] if age <= 75 else up["crc_76_plus"])
    if "breast" not in excluded and female and age >= 40:
        completion("breast", "mammography", up["mammography"] if age <= 74 else up["mammography_75_plus"])
    if "cervical" not in excluded and female and 21 <= age <= 65:
        pap = rng.random() < RATES["modality"]["pap_smear"] or age < RATES["min_age"]["hpv_test"]
        completion("cervical", "pap_smear" if pap else "hpv_test", up["cervical"])

    for it in items:
        it.precision = (
            "day"
            if (control or it.mode == "structured")
            else rng.choices(["day", "month", "year", "unknown"], [0.45, 0.25, 0.20, 0.10])[0]
        )
    return items


def _source_id(dataset_version: str, resident_id: uuid.UUID, it: EnrichedItem) -> str:
    return str(uuid5_of(f"{dataset_version}:enrich:{resident_id}:{it.concept}:{it.when.isoformat()}"))


def apply_enrichment(ctx: ResidentContext, items: list[EnrichedItem], dataset_version: str) -> None:
    """Make the plan visible to document generation: structured items become records, note-only items are kept apart."""
    for it in items:
        if it.mode == "note_only":
            ctx.note_only.append(it)
            continue
        _, category, system, code, display, tag, fact_type = CODES[it.concept]
        value = {"tag": tag, "fact_type": fact_type, "enrichment": True}
        if category == "observation":
            value["concept"] = NEGATIVE
        rec = Rec(
            category,
            system,
            code,
            display,
            it.when,
            None,
            "completed" if category == "procedure" else "final",
            tag,
            value,
            _source_id(dataset_version, ctx.id, it),
            enriched=True,
        )
        (ctx.procedures if category == "procedure" else []).append(rec)
        if category == "observation":
            old = ctx.observations.get(tag)
            if old is None or it.when > old.start:
                ctx.observations[tag] = rec
    ctx.procedures.sort(key=lambda r: r.start)


def clinical_record_rows(ctx: ResidentContext, items: list[EnrichedItem], dataset_version: str) -> list[dict]:
    """`anticipa.clinical_records` rows for the structured items (UNIQUE(resident_id, resource_type, source_resource_id))."""
    rows = []
    for it in items:
        if it.mode != "structured":
            continue
        resource_type, category, system, code, display, tag, fact_type = CODES[it.concept]
        sid = _source_id(dataset_version, ctx.id, it)
        value = {
            "tag": tag,
            "fact_type": fact_type,
            "enrichment": True,
            "enrichment_version": ENRICHMENT_VERSION,
        }
        if category == "observation":
            value["concept"] = NEGATIVE
        rows.append(
            {
                "id": uuid5_of(f"{dataset_version}:enrich-record:{sid}"),
                "resident_id": ctx.id,
                "resource_type": resource_type,
                "source_resource_id": sid,
                "source_system": ENRICHMENT_SOURCE,
                "category": category,
                "code_system": system,
                "code": code,
                "display": display,
                "effective_start": datetime(it.when.year, it.when.month, it.when.day, 9, 0, tzinfo=UTC),
                "status": "completed" if category == "procedure" else "final",
                "value_json": value,
                "source_reference": f"enrichment://{dataset_version}/{ctx.id}/{it.concept}/{sid}",
                "synthetic_status": "synthetic",
            }
        )
    return rows


def ground_truth_records(
    ctx: ResidentContext, items: list[EnrichedItem], dataset_version: str, seed: int
) -> list[dict]:
    """gt.enrichment.v1: what was planted, where, and (for note-only items) that no structured record exists."""
    out = []
    for it in items:
        _, _, system, code, display, _, fact_type = CODES[it.concept]
        sid = _source_id(dataset_version, ctx.id, it)
        out.append(
            {
                "schema_version": "gt.enrichment.v1",
                "dataset_version": dataset_version,
                "resident_id": str(ctx.id),
                "enrichment_id": sid,
                "enrichment_version": ENRICHMENT_VERSION,
                "seed": seed,
                "concept": it.proc,
                "fact_type": fact_type,
                "family": it.family,
                "status": it.status,
                "mode": it.mode,
                "code": {"system": system, "code": code, "display": display},
                "event_date": it.when.isoformat(),
                "note_precision": it.precision if it.mode == "note_only" else None,
                "structured_record": it.mode == "structured",
                "source_reference": f"enrichment://{dataset_version}/{ctx.id}/{it.concept}/{sid}"
                if it.mode == "structured"
                else None,
                "document_label_id": None,
                "synthetic_status": "synthetic",
            }
        )
    return out


ASSUMPTIONS = {
    "version": ENRICHMENT_VERSION,
    "rates": RATES,
    "text": __doc__.split("ASSUMPTIONS", 1)[1].strip(),
}
