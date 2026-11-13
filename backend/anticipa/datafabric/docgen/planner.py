# ruff: noqa: E501
"""Fact planting: decide scenarios per resident, then plan documents whose statements carry their labels.

Facts are planted FIRST (from the resident's structured records and deterministic synthetic relatives); text is
rendered afterwards (render.py). Everything is a pure function of (seed, dataset_version, resident context), using
`random.Random` instances only, so a run is reproducible and resumable. Scenario ids follow Docs/09 §5.4.

Statement `label` dicts are the *expected extraction* (what the document says); `world_truth` records where the
synthetic world differs from the document (DC-03). Labels never leave the ground-truth files (ADR-008).
"""

from __future__ import annotations

import hashlib
import random
import uuid
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from anticipa.datafabric.assign import _unit
from anticipa.datafabric.docgen.context import (
    Rec,
    Relative,
    ResidentContext,
    clean_condition,
    proc_concept_of,
)
from anticipa.datafabric.docgen.phrases import (
    CONDITIONS,
    EXCLUSIONS,
    FH_CONDITIONS,
    FH_ONSET,
    FH_SEX,
    LAB_NAMES,
    OTHER_PERSONS,
    PROC_MIN_AGE,
    PROC_SEX,
    PROCEDURES,
    RELATIONS,
    SCREENINGS,
    SIDE_SWAP,
)

GENERATOR = "docgen"
GENERATOR_VERSION = "template_v1"  # also `documents.generation_method`
# Scenario assignment (Docs/09 §5.4), per resident and independent of generation order. Rates are deliberately high so a
# random or stratified 10,000-resident sample contains well over the evaluation floors; they are NOT population statistics.
CONTROL_P = 0.17  # no difficult case (DC-00)
PRIMARY = tuple(f"DC-{n:02d}" for n in range(1, 8))
PRIMARY_P = 0.16  # each of DC-01..DC-07, drawn independently for non-control residents
SECONDARY = tuple(f"DC-{n:02d}" for n in range(8, 15))
SECONDARY_P = 0.10  # each of DC-08..DC-14
INJECTION_P = 0.04  # DC-15
FAMILY_SCENARIOS = frozenset(
    {"DC-01", "DC-02", "DC-03", "DC-05", "DC-07", "DC-08", "DC-10", "DC-11", "DC-12", "DC-13", "DC-14"}
)
MAX_SCENARIOS = 3
LAB_TAGS = (
    "hba1c",
    "glucose",
    "fasting_glucose",
    "total_cholesterol",
    "hdl_cholesterol",
    "ldl_cholesterol",
    "triglycerides",
    "bmi",
    "psa",
)
_BIRTH_OFFSET = {  # relative birth year minus resident birth year, by relationship
    "mother": (-38, -22),
    "father": (-42, -24),
    "sister": (-8, 8),
    "brother": (-8, 8),
    "daughter": (20, 32),
    "son": (20, 32),
    "maternal_grandmother": (-70, -48),
    "maternal_grandfather": (-70, -48),
    "paternal_grandmother": (-70, -48),
    "paternal_grandfather": (-70, -48),
}
SUBJECT_TYPE = {
    "resident": "resident",
    "relative": "family_member",
    "other_person": "other",
    "unknown": "unknown",
}
SECTION_ORDER = {
    "clinical_note": ("sec.history", "sec.meds", "sec.vitals", "sec.screening", "sec.family", "sec.plan"),
    "discharge_summary": (
        "disch_admin",
        "sec.diagnoses",
        "sec.disch_meds",
        "sec.screening",
        "sec.family",
        "sec.plan",
    ),
    "referral_letter": ("sec.reason", "sec.history", "sec.screening", "sec.family"),
    "lab_report": ("sec.results",),
    "family_history_narrative": ("sec.narrative",),
}


def make_rng(seed: int, dataset_version: str, resident_id: uuid.UUID, purpose: str) -> random.Random:
    digest = hashlib.sha256(f"{seed}:{dataset_version}:{resident_id}:{purpose}".encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))  # noqa: S311  (reproducible synthetic data, not security)


def age_on(birth: date, d: date) -> int:
    return d.year - birth.year - ((d.month, d.day) < (birth.month, birth.day))


def split_of(resident_id: uuid.UUID) -> str:
    """Docs/09 §5.6 proposed 20/80 dev/test split (the evaluation cohort builder may override)."""
    return "dev" if _unit(resident_id, "docgen:split") < 0.20 else "test"


@dataclass
class Item:
    kind: str  # template key in phrases.T
    params: dict = field(default_factory=dict)  # language-neutral
    label: dict | None = None  # expected extraction (None = plain context line)
    tag: str | None = None  # identity within a resident, for duplicate/conflict links
    duplicate_of_tag: str | None = None
    conflict: str | None = None  # "<group key>"; kind recorded in DocPlan.conflicts
    prefix: str | None = None  # template key rendered before the quote (outside it)
    absent: list = field(default_factory=list)  # absent-fact labels attached to a statement without a quote


@dataclass
class DocSpec:
    doc_type: str
    when: date
    sections: dict[str, list] = field(default_factory=dict)  # section key -> [Item | boilerplate-key str]
    unicode_variant: bool = False
    long: bool = False


@dataclass
class DocPlan:
    scenarios: list[str]
    docs: list[DocSpec]
    conflicts: dict[str, dict] = field(
        default_factory=dict
    )  # group key -> {"kind":..., "structured_refs": [...]}
    resident_absent: list[dict] = field(default_factory=list)  # absent labels with no document
    relatives_used: dict[str, Relative] = field(default_factory=dict)


def assign_scenarios(ctx: ResidentContext, as_of: date) -> list[str]:
    if _unit(ctx.id, "docgen:control") < CONTROL_P:
        return ["DC-00"]
    options = proc_options(ctx, as_of)
    # residents outside a linked block get no family-dependent facts: the family graph could later contradict them
    blocked = frozenset() if ctx.linked else FAMILY_SCENARIOS

    def drawn(ids, p, ok=lambda sid: True):
        return [
            sid
            for v, sid in sorted((_unit(ctx.id, f"docgen:{sid}"), sid) for sid in ids)
            if v < p and ok(sid)
        ]

    primary = drawn(PRIMARY, PRIMARY_P, lambda sid: sid not in blocked and (sid != "DC-04" or bool(options)))
    secondary = drawn(
        SECONDARY,
        SECONDARY_P,
        lambda sid: (
            sid not in blocked and (sid != "DC-09" or bool(proc_options(ctx, as_of, exclusions=False)))
        ),
    )
    injected = _unit(ctx.id, "docgen:DC-15") < INJECTION_P
    slots = MAX_SCENARIOS - injected
    chosen = primary[:slots]
    chosen += secondary[: slots - len(chosen)]
    if "DC-06" in chosen:  # DC-06 = no family-history evidence: drop scenarios that plant family statements
        chosen = [sid for sid in chosen if sid not in FAMILY_SCENARIOS]
    chosen += ["DC-15"] if injected else []
    if (
        not chosen
    ):  # a non-control resident always carries at least one difficult case: the most-favoured primary
        allowed = [sid for sid in PRIMARY if sid not in blocked and (sid != "DC-04" or options)]
        chosen = [min((_unit(ctx.id, f"docgen:{sid}"), sid) for sid in allowed)[1]] if allowed else ["DC-00"]
    return chosen


def proc_options(ctx: ResidentContext, as_of: date, *, exclusions: bool = True) -> list[str]:
    age = age_on(ctx.birth_date, as_of)
    pool = SCREENINGS + (EXCLUSIONS if exclusions else ())
    return [p for p in pool if age >= PROC_MIN_AGE[p] and PROC_SEX.get(p) in (None, ctx.sex)]


# ---- labels ----------------------------------------------------------------------------------------------------
def lab(
    category,
    fact_type,
    concept_id,
    *,
    scn=(),
    value=None,
    code=None,
    subject="resident",
    relationship=None,
    assertion="positive",
    temporal="unknown",
    age=None,
    event_date=None,
    validation="accepted",
    forbidden=(),
    world=None,
    rel: Relative | None = None,
    linked: bool = True,
    alt_categories=(),
    **extra,
) -> dict:
    d = {
        "category": category,
        "fact_type": fact_type,
        "concept_id": concept_id,
        "value": value or {},
        "code": code,
        "subject": subject,
        "subject_type": SUBJECT_TYPE[subject],
        "relationship": relationship,
        "subject_person_id": None,
        "family_edge_id": None,
        "related_person_key": None,
        "assertion": assertion,
        "temporal": temporal,
        "age_at_onset": age,
        "event_date": event_date,
        "expected_validation": validation,
        "forbidden_interpretations": list(forbidden),
        "world_truth": world or {"differs_from_document": False, "true_value": None},
        "scenario_ids": list(scn),
        "acceptable_categories": list(alt_categories),
    }
    if rel is not None:
        d["relationship"] = relationship or rel.relationship
        d["related_person_key"] = rel.key
        if linked:
            d["subject_person_id"] = str(rel.resident_id) if rel.resident_id else None
            d["family_edge_id"] = str(rel.edge_id) if rel.edge_id else None
    d.update(extra)
    return d


def _date_val(d: date | None, precision: str) -> dict | None:
    if d is None or precision == "unknown":
        return None
    return {
        "value": {"day": d.isoformat(), "month": d.isoformat()[:7], "year": str(d.year)}[precision],
        "precision": precision,
    }


def _code_of(rec: Rec) -> dict | None:
    if not rec.code:
        return None
    system = (rec.code_system or "").lower()
    short = next((s for s in ("snomed", "rxnorm", "loinc", "icd") if s in system), rec.code_system)
    return {"system": short, "code": rec.code}


def _slug(text: str) -> str:
    out = "".join(c if c.isalnum() else "_" for c in text.lower())
    return "_".join(p for p in out.split("_") if p)[:60]


# ---- relatives ---------------------------------------------------------------------------------------------------
def relatives_of(ctx: ResidentContext, rng: random.Random, as_of: date) -> list[Relative]:
    """Graph relatives first; missing relationships are filled with deterministic document-only relatives."""
    b = ctx.birth_date.year
    have = {r.relationship for r in ctx.graph_relatives if not r.decoy}
    out = [  # linked relatives whose birth year could not be resolved get a plausible one for their relationship
        r
        if r.birth_year > 0
        else replace(r, birth_year=b + rng.randint(*_BIRTH_OFFSET.get(r.relationship, (-40, -15))))
        for r in ctx.graph_relatives
        if not r.decoy
    ]

    def synth(relationship: str, birth_year: int, p: float = 1.0) -> None:
        keep = rng.random() < p  # always consume the draw so later draws stay aligned
        if relationship in have or not keep or not 0 <= as_of.year - birth_year <= 100:
            return
        meta = RELATIONS[relationship]
        out.append(
            Relative(f"docrel:{ctx.id}:{relationship}", relationship, meta["sex"], meta["side"], birth_year)
        )

    synth("mother", b - rng.randint(22, 38))
    synth("father", b - rng.randint(24, 42))
    synth("sister", b + rng.randint(-8, 8), 0.7)
    synth("brother", b + rng.randint(-8, 8), 0.7)
    for gp in (
        "maternal_grandmother",
        "maternal_grandfather",
        "paternal_grandmother",
        "paternal_grandfather",
    ):
        synth(gp, b - rng.randint(48, 70), 0.8)
    for au in ("maternal_aunt", "maternal_uncle", "paternal_aunt", "paternal_uncle"):
        synth(au, b - rng.randint(15, 40), 0.6)
    if age_on(ctx.birth_date, as_of) >= 30:
        synth("daughter", b + rng.randint(20, 32), 0.5)
        synth("son", b + rng.randint(20, 32), 0.5)
    if not out:  # very old residents: parents and grandparents are filtered out above
        out.append(Relative(f"docrel:{ctx.id}:sister", "sister", "female", None, b + rng.randint(-5, 5)))
    return out


def pick_fh(
    rng, rels: list[Relative], as_of: date, *, cond: str | None = None, exclude: set[str] = frozenset()
):
    """(relative, condition, onset age) that is biologically and chronologically plausible."""
    for _ in range(16):
        c = cond or rng.choice(FH_CONDITIONS)
        lo, hi = FH_ONSET[c]
        pool = [
            r
            for r in rels
            if FH_SEX[c] in (None, r.sex) and as_of.year - r.birth_year >= lo and r.key not in exclude
        ]
        if pool:
            r = rng.choice(pool)
            return r, c, rng.randint(lo, min(hi, as_of.year - r.birth_year))
        if cond:
            break
    r = max(
        (x for x in rels if x.key not in exclude), key=lambda x: as_of.year - x.birth_year, default=rels[0]
    )
    c = cond or "colorectal_cancer"
    return r, c, max(FH_ONSET[c][0], 1)


def fh_item(plan: DocPlan, kind: str, rel: Relative, cond: str, age: int | None, scn, **kw) -> Item:
    plan.relatives_used[rel.key] = rel
    if kind in ("fh_pos", "fh_pos_noage", "fh_distractor"):
        kw.setdefault("forbidden", [{"subject": "resident"}])
    label = lab(
        "family_history",
        cond,
        cond,
        value={"condition": cond},
        subject="relative",
        scn=scn,
        rel=rel,
        temporal="historical",
        age=age,
        **kw,
    )
    params = {"rel": label["relationship"], "cond": cond, "age": age}
    return Item(kind, params, label)


# ---- statements about the resident's own records -------------------------------------------------------------------
def record_items(ctx: ResidentContext, scn=()) -> dict[str, list[Item]]:
    sections: dict = {
        "history": [],
        "meds": [],
        "vitals": [],
        "real_screening": [],
        "note_only": [],
        "labs": [],
    }
    for rec in ctx.conditions[:2]:
        text = clean_condition(rec.display)
        label = lab(
            "condition",
            _slug(text),
            f"sct_{rec.code}",
            value={"condition": text},
            code=_code_of(rec),
            scn=scn,
            temporal="historical" if rec.end else "current",
        )
        sections["history"].append(
            Item("cond_listed", {"cond_text": text}, label, tag=f"cond:{rec.source_id}")
        )
    for rec in ctx.medications[:2]:
        label = lab(
            "medication",
            _slug(rec.display),
            f"rx_{rec.code}",
            value={"medication": rec.display},
            code=_code_of(rec),
            scn=scn,
            temporal="current" if rec.status == "active" else "historical",
        )
        sections["meds"].append(Item("med_listed", {"med": rec.display}, label))
    bp = ctx.observations.get("blood_pressure")
    if bp:
        comps = {c.get("code"): c.get("value") for c in bp.value.get("components", [])}
        if comps.get("8480-6") and comps.get("8462-4"):
            s, d = round(comps["8480-6"]), round(comps["8462-4"])
            label = lab(
                "observation",
                "blood_pressure",
                "blood_pressure",
                scn=scn,
                temporal="current",
                value={"systolic": s, "diastolic": d, "unit": "mmHg"},
            )
            sections["vitals"].append(Item("obs_bp", {"sys": s, "dia": d}, label))
    for tag in LAB_TAGS:
        rec = ctx.observations.get(tag)
        if rec and rec.value.get("value") is not None:
            v = round(float(rec.value["value"]), 2)
            unit = str(rec.value.get("unit") or "").replace("mm[Hg]", "mmHg")
            label = lab(
                "observation",
                tag,
                tag,
                scn=scn,
                temporal="current",
                value={"value": v, "unit": unit},
                code=_code_of(rec),
            )
            sections["labs"].append(
                Item(
                    "obs_lab",
                    {"name": LAB_NAMES[tag], "value": f"{v:g}", "unit": unit},
                    label,
                    tag=f"lab:{tag}",
                )
            )
    # real completed screenings (structured records)
    real = [r for r in ctx.procedures + list(ctx.observations.values()) if proc_concept_of(r)]
    for rec in sorted(real, key=lambda r: r.start, reverse=True)[:2]:
        proc = proc_concept_of(rec)
        sections["real_screening"].append(
            proc_item(
                proc,
                rec.start,
                "day",
                scn,
                code=_code_of(rec),
                tag=f"{'enrich' if rec.enriched else 'proc'}:{proc}",
                real=True,
            )
        )
    for it in ctx.note_only:  # completions documented only in a note: no structured record exists (enrich.py)
        sections["note_only"].append(
            proc_item(it.proc, it.when, it.precision, scn, tag=f"enrich:{it.proc}", real=False)
        )
    sections["present"] = {
        i.label["concept_id"] for k in ("real_screening", "note_only") for i in sections[k]
    }
    return sections


def proc_item(
    proc: str,
    when: date | None,
    precision: str,
    scn,
    *,
    code=None,
    tag=None,
    real=False,
    validation="accepted",
    world=None,
) -> Item:
    kind = "proc_done_nodate" if (when is None or precision == "unknown") else f"proc_done_{precision}"
    meta = PROCEDURES[proc]
    alt = ["procedure"] if meta["category"] == "preventive_care" else ["preventive_care"]
    label = lab(
        meta["category"],
        proc,
        proc,
        value={"procedure": proc, "structured_record_present": real},
        code=code,
        temporal="historical",
        event_date=_date_val(when, precision),
        scn=scn,
        alt_categories=alt,
        validation=validation,
        world=world,
    )
    return Item(kind, {"proc": proc, "date": when, "precision": precision}, label, tag=tag)


def outside_proc_item(rng, ctx: ResidentContext, proc: str, as_of: date, scn, **kw) -> Item:
    lo = ctx.birth_date + timedelta(days=365 * PROC_MIN_AGE[proc])
    hi = as_of - timedelta(days=180)
    when = rand_date(rng, min(lo, hi), hi)
    precision = rng.choices(["day", "month", "year", "unknown"], [0.4, 0.25, 0.2, 0.15])[0]
    return proc_item(proc, when, precision, scn, **kw)


def free(rec: dict, options: list[str]) -> list[str]:
    """Options the resident has no screening statement for yet (avoids accidental contradictions with enrichment)."""
    return [o for o in options if o not in rec["present"]] or options


def rand_date(rng, lo: date, hi: date) -> date:
    if hi <= lo:
        return hi
    return lo + timedelta(days=rng.randint(0, (hi - lo).days))


# ---- the plan ----------------------------------------------------------------------------------------------------
def plan_resident(ctx: ResidentContext, seed: int, dataset_version: str, as_of: date) -> DocPlan:
    rng = make_rng(seed, dataset_version, ctx.id, "plan")
    scenarios = assign_scenarios(ctx, as_of)
    control = scenarios == ["DC-00"]
    rels = relatives_of(ctx, make_rng(seed, dataset_version, ctx.id, "relatives"), as_of)
    plan = DocPlan(scenarios, [])
    scn0 = ("DC-00",) if control else ()
    rec = record_items(ctx, scn0)
    born = ctx.birth_date + timedelta(days=1)
    min_when = max(born, as_of - timedelta(days=900))

    def new_doc(doc_type: str, when: date | None = None) -> DocSpec:
        d = DocSpec(doc_type, when or rand_date(rng, min_when, as_of - timedelta(days=1)))
        plan.docs.append(d)
        return d

    note_when = ctx.last_encounter if ctx.last_encounter and ctx.last_encounter >= min_when else None
    note = new_doc("clinical_note", note_when)
    note.sections["sec.history"] = list(rec["history"])
    note.sections["sec.meds"] = list(rec["meds"])
    note.sections["sec.vitals"] = list(rec["vitals"])
    note.sections["sec.screening"] = list(rec["real_screening"]) + list(rec["note_only"])
    note.sections["sec.plan"] = ["plan_boiler"]
    docs: dict[str, DocSpec] = {"clinical_note": note}

    def doc(doc_type: str) -> DocSpec:
        if doc_type not in docs:
            docs[doc_type] = new_doc(doc_type)
        return docs[doc_type]

    def put(doc_type: str, section: str, item: Item) -> Item:
        doc(doc_type).sections.setdefault(section, []).append(item)
        return item

    if rec["labs"]:
        lab_doc = doc("lab_report")
        lab_doc.when = ctx.observations[rec["labs"][0].label["fact_type"]].start
        lab_doc.sections["sec.results"] = list(rec["labs"])
    if rng.random() < 0.20:
        dis = doc("discharge_summary")
        dis.sections["disch_admin"] = ["disch_admin"]
        dis.sections["sec.diagnoses"] = [_clone(i) for i in rec["history"]]
        dis.sections["sec.disch_meds"] = [_clone(i) for i in rec["meds"]]
        dis.sections["sec.plan"] = ["plan_boiler"]
    if rng.random() < 0.30 or "DC-05" in scenarios:
        ref = doc("referral_letter")
        ref.sections["sec.reason"] = ["ref_reason"]
    no_family = "DC-06" in scenarios or not ctx.linked

    # clean baseline family statements (control residents always, others sometimes), never for DC-06
    if not no_family and (control or rng.random() < 0.30):
        r, c, a = pick_fh(rng, rels, as_of)
        put("family_history_narrative", "sec.narrative", fh_item(plan, "fh_pos", r, c, a, scn0))
        doc("family_history_narrative").sections["sec.narrative"].insert(0, "narr_intro")

    for sid in scenarios:
        _SCENARIOS.get(sid, _noop)(plan, ctx, rng, rels, rec, as_of, put, doc, scn=(sid,))
    for d in plan.docs:
        if d.doc_type == "family_history_narrative" and "narr_intro" not in d.sections.get(
            "sec.narrative", []
        ):
            d.sections.setdefault("sec.narrative", []).insert(0, "narr_intro")
    plan.docs.sort(key=lambda d: (d.when, d.doc_type))
    return plan


def _clone(item: Item) -> Item:
    return Item(item.kind, dict(item.params), dict(item.label) if item.label else None, item.tag)


def _noop(*a, **k) -> None:
    return None


# ---- scenario builders: (plan, ctx, rng, rels, rec, as_of, put, doc, scn) --------------------------------------------
def dc00(plan, ctx, rng, rels, rec, as_of, put, doc, scn):
    return None  # control: baseline facts only


def dc01(plan, ctx, rng, rels, rec, as_of, put, doc, scn):  # negated family history
    for _ in range(rng.randint(1, 2)):
        cond = rng.choice(FH_CONDITIONS)
        if rng.random() < 0.5:
            r = rng.choice(
                [x for x in rels if x.relationship in ("mother", "father", "sister", "brother")] or rels
            )
            plan.relatives_used[r.key] = r
            label = lab(
                "family_history",
                cond,
                cond,
                value={"condition": cond},
                subject="relative",
                rel=r,
                scn=scn,
                assertion="negative",
                temporal="unknown",
                forbidden=[{"assertion": "positive"}, {"subject": "resident"}],
            )
            put(
                "family_history_narrative",
                "sec.narrative",
                Item("fh_neg_rel", {"rel": r.relationship, "cond": cond}, label),
            )
        else:
            label = lab(
                "family_history",
                cond,
                cond,
                value={"condition": cond},
                subject="relative",
                relationship="other_relative",
                scn=scn,
                assertion="negative",
                temporal="unknown",
                forbidden=[{"assertion": "positive"}, {"subject": "resident"}],
            )
            put("family_history_narrative", "sec.narrative", Item("fh_neg_any", {"cond": cond}, label))
    if rng.random() < 0.5:  # a positive statement for contrast; it is not itself a DC-01 instance
        r, c, a = pick_fh(rng, rels, as_of)
        put("family_history_narrative", "sec.narrative", fh_item(plan, "fh_pos", r, c, a, ()))


def dc02(plan, ctx, rng, rels, rec, as_of, put, doc, scn):  # wrong relative
    roll = rng.random()
    swappable = [r for r in rels if r.relationship in SIDE_SWAP]
    if roll < 0.5 or (roll < 0.7 and not swappable):  # distractor relative next to the affected one
        r, c, a = pick_fh(rng, rels, as_of)
        well = rng.choice(
            [
                x
                for x in rels
                if x.key != r.key and x.relationship in ("mother", "father", "sister", "brother")
            ]
            or [r]
        )
        item = fh_item(
            plan,
            "fh_distractor",
            r,
            c,
            a,
            scn,
            forbidden=[{"relationship": well.relationship}, {"subject": "resident"}],
        )
        item.params["rel_well"] = well.relationship
        put("family_history_narrative", "sec.narrative", item)
    elif roll < 0.7:  # side swapped relative to the family graph
        for _ in range(8):
            r = rng.choice(swappable)
            c = rng.choice(FH_CONDITIONS)
            lo, hi = FH_ONSET[c]
            if FH_SEX[c] in (None, r.sex) and as_of.year - r.birth_year >= lo:
                break
        else:
            r, c = swappable[0], "colorectal_cancer"
        a = rng.randint(FH_ONSET[c][0], max(FH_ONSET[c][0], min(FH_ONSET[c][1], as_of.year - r.birth_year)))
        swapped = SIDE_SWAP[r.relationship]
        fake = Relative(
            f"docrel:{ctx.id}:{swapped}:text",
            swapped,
            RELATIONS[swapped]["sex"],
            RELATIONS[swapped]["side"],
            r.birth_year,
        )
        world = {
            "differs_from_document": True,
            "true_value": {
                "relationship": r.relationship,
                "family_edge_id": str(r.edge_id) if r.edge_id else None,
            },
        }
        forbid = [{"relationship": r.relationship}]
        if r.edge_id:
            forbid.append({"family_edge_id": str(r.edge_id)})
        item = fh_item(plan, "fh_pos", fake, c, a, scn, world=world, forbidden=forbid, linked=False)
        plan.relatives_used.pop(fake.key, None)  # the text-only relationship is not a graph member
        plan.relatives_used[r.key] = r
        item.label["related_person_key"] = r.key  # the true relative the text mis-describes
        put("family_history_narrative", "sec.narrative", item)
    else:  # the condition belongs to somebody who is not a relative
        c = rng.choice(FH_CONDITIONS)
        idx = rng.randrange(len(OTHER_PERSONS["en"]))
        label = lab(
            "condition",
            c,
            c,
            value={"condition": c},
            subject="other_person",
            scn=scn,
            temporal="historical",
            forbidden=[{"subject": "resident"}, {"subject": "relative"}],
        )
        put(
            "family_history_narrative",
            "sec.narrative",
            Item("fh_other_person", {"cond": c, "who_idx": idx}, label),
        )


def dc03(plan, ctx, rng, rels, rec, as_of, put, doc, scn):  # wrong age / impossible date
    roll = rng.random()
    cond_recs = [r for r in ctx.conditions[:2] if r.start and age_on(ctx.birth_date, r.start) >= 5]
    if roll < 0.5:
        r, c, _ = pick_fh(rng, rels, as_of)
        now_age = as_of.year - r.birth_year
        stated = now_age + rng.randint(5, 15)
        if stated <= 119:
            world = {
                "differs_from_document": True,
                "true_value": {
                    "age_at_onset": None,
                    "relative_age_now": now_age,
                    "reason": "stated_age_exceeds_relative_age",
                },
            }
            put(
                "family_history_narrative",
                "sec.narrative",
                fh_item(plan, "fh_pos", r, c, stated, scn, world=world),
            )
            return
    if roll < 0.8 and cond_recs:
        r = cond_recs[0]
        true_age = age_on(ctx.birth_date, r.start)
        stated = max(1, true_age + rng.choice([-1, 1]) * rng.randint(10, 25))
        text = clean_condition(r.display)
        world = {
            "differs_from_document": True,
            "true_value": {"age_at_onset": true_age, "source": "structured_record"},
        }
        label = lab(
            "condition",
            _slug(text),
            f"sct_{r.code}",
            value={"condition": text},
            code=_code_of(r),
            scn=scn,
            temporal="historical",
            age=stated,
            world=world,
        )
        put("referral_letter", "sec.history", Item("cond_age", {"cond_text": text, "age": stated}, label))
        return
    proc = rng.choice(SCREENINGS)
    year = ctx.birth_date.year + rng.randint(1, 8)
    world = {
        "differs_from_document": True,
        "true_value": {"event_year": None, "reason": "date_before_plausible_age"},
    }
    put("clinical_note", "sec.screening", proc_item(proc, date(year, 6, 15), "year", scn, world=world))


def dc04(plan, ctx, rng, rels, rec, as_of, put, doc, scn):  # completed screening / exclusion with dates
    options = free(rec, proc_options(ctx, as_of))
    existing = rec["real_screening"] + rec["note_only"]
    if existing and rng.random() < 0.7:
        for it in existing:  # retag the statement already planted (no duplicate statement for the same fact)
            it.label["scenario_ids"] = list(scn)
        return
    for _ in range(rng.randint(1, 2)):
        proc = (
            rng.choice(options)
            if rng.random() < 0.3 or not [o for o in options if o in SCREENINGS]
            else rng.choice([o for o in options if o in SCREENINGS])
        )
        put(
            "clinical_note",
            "sec.screening",
            outside_proc_item(rng, ctx, proc, as_of, scn, tag=f"proc:{proc}"),
        )


def dc05(plan, ctx, rng, rels, rec, as_of, put, doc, scn):  # conflicting information
    note, ref = doc("clinical_note"), doc("referral_letter")
    note.when = rand_date(
        rng, max(ctx.birth_date + timedelta(days=1), as_of - timedelta(days=500)), as_of - timedelta(days=200)
    )
    ref.when = note.when + timedelta(days=rng.randint(30, 120))
    roll = rng.random()
    key = "c1"
    if roll < 0.2 and ctx.conditions:  # structured record vs document
        r = ctx.conditions[0]
        text = clean_condition(r.display)
        for d in plan.docs:
            for sec in d.sections.values():
                sec[:] = [
                    i
                    for i in sec
                    if not (isinstance(i, Item) and i.label and i.label["concept_id"] == f"sct_{r.code}")
                ]
        label = lab(
            "condition",
            _slug(text),
            f"sct_{r.code}",
            value={"condition": text},
            code=_code_of(r),
            scn=scn,
            assertion="negative",
            temporal="current",
            forbidden=[{"assertion": "positive"}],
        )
        put("referral_letter", "sec.history", Item("cond_denied", {"cond_text": text}, label, conflict=key))
        plan.conflicts[key] = {"kind": "structured_vs_doc", "structured_refs": [r.source_id]}
    elif roll < 0.45:  # one document says done, a later one says never
        proc = rng.choice(free(rec, proc_options(ctx, as_of, exclusions=False)) or ["fit"])
        a = proc_item(
            proc,
            rand_date(
                rng,
                ctx.birth_date + timedelta(days=365 * PROC_MIN_AGE[proc]),
                note.when - timedelta(days=400),
            ),
            "year",
            scn,
        )
        a.conflict = key
        put("clinical_note", "sec.screening", a)
        label = lab(
            PROCEDURES[proc]["category"],
            proc,
            proc,
            value={"procedure": proc},
            assertion="negative",
            temporal="unknown",
            scn=scn,
            forbidden=[{"assertion": "positive"}],
            alt_categories=[
                "procedure" if PROCEDURES[proc]["category"] == "preventive_care" else "preventive_care"
            ],
        )
        put("referral_letter", "sec.screening", Item("proc_never", {"proc": proc}, label, conflict=key))
        plan.conflicts[key] = {"kind": "doc_vs_doc", "structured_refs": []}
    else:  # family-history age disagrees; a correction counts as doc_updates_doc
        r, c, a1 = pick_fh(rng, rels, as_of)
        lo = FH_ONSET[c][0]
        a2 = max(lo, a1 + rng.choice([-1, 1]) * rng.randint(6, 14))
        if a2 == a1:
            a2 = a1 + 7
        first = fh_item(plan, "fh_pos", r, c, a1, scn)
        second = fh_item(plan, "fh_pos", r, c, a2, scn)
        first.conflict = second.conflict = key
        updates = roll >= 0.75
        if updates:
            second.prefix = "correction"
        put("clinical_note", "sec.family", first)
        put("referral_letter", "sec.family", second)
        plan.conflicts[key] = {"kind": "doc_updates_doc" if updates else "doc_vs_doc", "structured_refs": []}


def dc06(plan, ctx, rng, rels, rec, as_of, put, doc, scn):  # missing evidence: family history not obtained
    absent = [
        lab(
            "family_history", c, c, value={"condition": c}, subject="relative", scn=scn, validation="rejected"
        )
        for c in ("colorectal_cancer", "breast_cancer")
    ]
    for a in absent:
        a["expected_present"] = False
    put("clinical_note", "sec.family", Item("fh_not_obtained", {}, None, absent=absent))
    miss = lab(
        "family_history",
        "ovarian_cancer",
        "ovarian_cancer",
        value={"condition": "ovarian_cancer"},
        subject="relative",
        scn=scn,
        validation="rejected",
    )
    miss["expected_present"] = False
    plan.resident_absent.append(miss)


def dc07(plan, ctx, rng, rels, rec, as_of, put, doc, scn):  # ambiguity
    cond = rng.choice(FH_CONDITIONS)
    variants = [
        lambda: Item(
            "fh_pronoun_cancer",
            {"variant": rng.randint(0, 1)},
            lab(
                "condition",
                "cancer",
                "cancer_unspecified",
                value={"condition": "cancer_unspecified"},
                subject="unknown",
                scn=scn,
                temporal="historical",
                validation="requires_review",
                forbidden=[{"subject": "resident"}],
            ),
        ),
        lambda: Item(
            "fh_bare_neg",
            {"cond": cond},
            lab(
                "condition",
                cond,
                cond,
                value={"condition": cond},
                subject="unknown",
                scn=scn,
                assertion="negative",
                validation="requires_review",
                forbidden=[{"assertion": "positive"}],
            ),
        ),
        lambda: Item(
            "cond_poss",
            {"cond": cond},
            lab(
                "condition",
                cond,
                cond,
                value={"condition": cond},
                scn=scn,
                assertion="uncertain",
                temporal="current",
                validation="requires_review",
            ),
        ),
        lambda: Item(
            "proc_vague",
            {},
            lab(
                "preventive_care",
                "screening_unspecified",
                "screening_unspecified",
                value={"procedure": "screening_unspecified"},
                scn=scn,
                temporal="unknown",
                validation="requires_review",
                alt_categories=["procedure"],
            ),
        ),
    ]
    for i in rng.sample(range(4), 2):
        item = variants[i]()
        section = "sec.family" if item.kind in ("fh_pronoun_cancer", "fh_bare_neg") else "sec.history"
        put("clinical_note", section, item)


def dc08(plan, ctx, rng, rels, rec, as_of, put, doc, scn):  # uncertain / conditional / hypothetical
    cond = rng.choice(("colon_polyps", "breast_cancer", "colorectal_cancer"))
    proc = rng.choice(SCREENINGS[:2])
    r, c, _ = pick_fh(rng, rels, as_of)
    plan.relatives_used[r.key] = r
    pool = [
        Item(
            "cond_poss",
            {"cond": cond},
            lab(
                "condition",
                cond,
                cond,
                value={"condition": cond},
                scn=scn,
                assertion="uncertain",
                temporal="current",
            ),
        ),
        Item(
            "cond_cond",
            {"proc": proc},
            lab(
                "preventive_care",
                proc,
                proc,
                value={"procedure": proc},
                scn=scn,
                assertion="conditional",
                temporal="unknown",
                alt_categories=["procedure"],
            ),
        ),
        Item(
            "fh_poss",
            {"rel": r.relationship, "cond": c},
            lab(
                "family_history",
                c,
                c,
                value={"condition": c},
                subject="relative",
                rel=r,
                scn=scn,
                assertion="uncertain",
                temporal="unknown",
                forbidden=[{"subject": "resident"}],
            ),
        ),
    ]
    for item in rng.sample(pool, 2):
        put("clinical_note", "sec.family" if item.kind == "fh_poss" else "sec.screening", item)


def dc09(plan, ctx, rng, rels, rec, as_of, put, doc, scn):  # historical vs current vs planned
    options = free(rec, proc_options(ctx, as_of, exclusions=False))
    done, planned = rng.sample(options, 2) if len(options) >= 2 else (options[0], options[0])
    put("clinical_note", "sec.screening", outside_proc_item(rng, ctx, done, as_of, scn, tag=f"proc:{done}"))
    when = as_of + timedelta(days=rng.randint(14, 180))
    label = lab(
        PROCEDURES[planned]["category"],
        planned,
        planned,
        value={"procedure": planned},
        scn=scn,
        temporal="planned",
        event_date=_date_val(when, "day"),
        alt_categories=["procedure"],
    )
    put(
        "discharge_summary",
        "sec.screening",
        Item("proc_planned", {"proc": planned, "date": when, "precision": "day"}, label),
    )


def dc10(plan, ctx, rng, rels, rec, as_of, put, doc, scn):  # resident and several relatives in one note
    used: set[str] = set()
    for _ in range(2):
        r, c, a = pick_fh(rng, rels, as_of, exclude=used)
        used.add(r.key)
        put("clinical_note", "sec.family", fh_item(plan, "fh_pos", r, c, a, scn))
    pairs = [
        x
        for x in rels
        if x.relationship in ("mother", "sister", "maternal_aunt") and x.sex == "female" and x.key not in used
    ]
    if len(pairs) >= 2 and as_of.year - min(p.birth_year for p in pairs[:2]) >= FH_ONSET["breast_cancer"][0]:
        a, b = pairs[:2]
        for r in (a, b):
            plan.relatives_used[r.key] = r
        shared = {"rel": a.relationship, "rel2": b.relationship, "cond": "breast_cancer"}
        first = Item(
            "fh_pair",
            shared,
            lab(
                "family_history",
                "breast_cancer",
                "breast_cancer",
                value={"condition": "breast_cancer"},
                subject="relative",
                rel=a,
                scn=scn,
                temporal="historical",
                forbidden=[{"subject": "resident"}],
            ),
        )
        second = Item(
            "fh_pair",
            shared,
            lab(
                "family_history",
                "breast_cancer",
                "breast_cancer",
                value={"condition": "breast_cancer"},
                subject="relative",
                rel=b,
                scn=scn,
                temporal="historical",
                forbidden=[{"subject": "resident"}],
            ),
        )
        first.tag = f"pair:{ctx.id}"
        second.params = {
            **shared,
            "same_line_as": first.tag,
        }  # same sentence as `first`: one rendered line, two labels
        put("clinical_note", "sec.family", first)
        put("clinical_note", "sec.family", second)


def dc11(plan, ctx, rng, rels, rec, as_of, put, doc, scn):  # repeated evidence
    r, c, a = pick_fh(rng, rels, as_of)
    first = fh_item(plan, "fh_pos", r, c, a, scn)
    first.tag = f"dup:{ctx.id}"
    put("family_history_narrative", "sec.narrative", first)
    again = fh_item(plan, "fh_pos", r, c, a, scn)
    again.params["variant"] = 0  # same wording again
    first.params["variant"] = 0
    again.duplicate_of_tag = first.tag
    put("clinical_note", "sec.family", again)


def dc12(plan, ctx, rng, rels, rec, as_of, put, doc, scn):  # long document, facts in separate sections
    note = doc("clinical_note")
    note.long = True
    r, c, a = pick_fh(rng, rels, as_of)
    put("clinical_note", "sec.family", fh_item(plan, "fh_pos", r, c, a, scn))
    options = free(rec, proc_options(ctx, as_of))
    if options:
        put("clinical_note", "sec.screening", outside_proc_item(rng, ctx, rng.choice(options), as_of, scn))


def dc13(plan, ctx, rng, rels, rec, as_of, put, doc, scn):  # Unicode and punctuation variation
    narrative = doc("family_history_narrative")
    narrative.unicode_variant = True
    r, c, a = pick_fh(rng, rels, as_of)
    put("family_history_narrative", "sec.narrative", fh_item(plan, "fh_pos", r, c, a, scn))
    options = free(rec, proc_options(ctx, as_of, exclusions=False))
    if options:
        proc = rng.choice(options)
        lo = ctx.birth_date + timedelta(days=365 * PROC_MIN_AGE[proc])
        when = rand_date(rng, min(lo, as_of - timedelta(days=400)), as_of - timedelta(days=200))
        put("family_history_narrative", "sec.narrative", proc_item(proc, when, "day", scn))
    for sec in narrative.sections.values():  # every labelled statement in a variant document carries the tag
        for it in sec:
            if isinstance(it, Item) and it.label:
                it.label["scenario_ids"] = sorted(set(it.label["scenario_ids"]) | set(scn))


def dc14(plan, ctx, rng, rels, rec, as_of, put, doc, scn):  # terminology variants and confusable terms
    r, c, a = pick_fh(rng, rels, as_of, cond="colorectal_cancer")
    item = fh_item(plan, "fh_pos", r, c, a, scn)
    item.params["term_idx"] = rng.randint(1, len(CONDITIONS[c]["en"]) - 1)
    put("family_history_narrative", "sec.narrative", item)
    r2, _, a2 = pick_fh(rng, rels, as_of, cond="colorectal_cancer", exclude={r.key})
    poly = fh_item(
        plan,
        "fh_pos_noage",
        r2,
        "colon_polyps",
        None,
        scn,
        forbidden=[{"concept_id": "colorectal_cancer"}, {"subject": "resident"}],
    )
    poly.params["term_idx"] = rng.randint(0, 1)
    put("family_history_narrative", "sec.narrative", poly)


def dc15(plan, ctx, rng, rels, rec, as_of, put, doc, scn):  # instruction-like text inside a note
    inj = lab("preventive_care", "screening_eligibility_claim", None, scn=scn, validation="rejected")
    inj["expected_present"] = False
    put("clinical_note", "sec.plan", Item("injection", {}, inj))


_SCENARIOS = {
    f"DC-{n:02d}": fn
    for n, fn in enumerate(
        (dc00, dc01, dc02, dc03, dc04, dc05, dc06, dc07, dc08, dc09, dc10, dc11, dc12, dc13, dc14, dc15)
    )
}
