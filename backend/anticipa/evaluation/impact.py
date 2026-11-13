"""Proven impact evidence (gap audit B02, B08 and the data half of B09). Zero model spend, read-only, deterministic.

Two baselines, each computed IN MEMORY from the same deterministic code the product runs (nothing is written to an
evaluation, planning or pipeline table; the only output is a JSON report):

  rules baseline   the approved guideline engine fed STRUCTURED RECORDS ONLY, against the same engine fed structured
                   records plus the facts extracted from clinical notes (what Anticipa does today). Measured on the whole
                   AI cohort (no labels needed) and, where planted ground truth exists, scored on the development split
                   only. The held out test split is never read, so its single use ledger is untouched.
  planner baseline a first come first served allocator (nearest clinic first, earliest free slot) over exactly the cases
                   and capacity of the latest OR-Tools planning run, compared with that run's stored allocations.

This module lives in the evaluation package because it reads planted ground truth (ADR-008): pipeline and serving modules
must never import it. CLI: `python -m anticipa.evaluation impact [--out PATH] [--store]`. The API (anticipa.analytics.impact) only
serves the stored report: the newest `data/reports/impact-baselines-*.json`, or the copy `--store` keeps in analytics_snapshots.
Capacity is SIMULATED and the residents are synthetic, so every figure here is about synthetic data (see `limitations`).
"""
# ruff: noqa: E501

from __future__ import annotations

import json
import statistics
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from anticipa.analytics import impact as serving
from anticipa.config import ROOT_DIR
from anticipa.evaluation import eligibility as el
from anticipa.evaluation import gt as gtmod
from anticipa.evaluation import readers
from anticipa.evaluation.stats import percentile, r6, rate
from anticipa.guidelines.evaluator import Fact, ResidentInputs, evaluate_guideline, parse_partial_date
from anticipa.planning.domain import Allocation, PlanConfig, PlannableCase, PoolSpec, SlotSpec

SCHEMA = "anticipa.impact.v1"
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
DUE, REVIEW, DONE = "eligible", "requires_clinical_review", "already_completed"
NOT_DUE = ("not_eligible", "temporarily_excluded")
CASE_OUTCOMES = (DUE, REVIEW)  # the outcomes that open a care gap case (caregaps.service.CASE_OUTCOMES)


# ================================================================================================ rules baseline (pure)
def structured_only(inputs: ResidentInputs) -> ResidentInputs:
    """The same resident with every note derived fact removed (source 'fact'); structured records (source 'record') stay."""
    return replace(inputs, facts=[f for f in inputs.facts if f.source == "record"])


def paired_rows(inputs: ResidentInputs, guidelines: Sequence[Any], on: date, planted: Sequence[Fact] | None = None) -> list[dict[str, Any]]:
    """One row per guideline: outcome with structured records only (`base`), with notes (`now`, current Anticipa) and, when
    `planted` facts are given, with perfect planted evidence (`oracle`). Pure: the evaluator is a pure function (08 §8.2)."""
    base_in = structured_only(inputs)
    oracle_in = replace(base_in, facts=[*base_in.facts, *planted]) if planted is not None else None
    rows = []
    for lg in guidelines:
        def run(i: ResidentInputs, lg=lg) -> tuple[str, str, str | None]:
            r = evaluate_guideline(lg.definition, i, on, lg.sources, lg.eligible_explanation)
            return r.outcome, r.outcome_class, r.reason_code

        b, n = run(base_in), run(inputs)
        o = run(oracle_in) if oracle_in else None
        rows.append({"resident_id": str(inputs.resident.id), "key": lg.guideline_key, "base": b[0], "base_class": b[1], "base_reason": b[2],
                     "now": n[0], "now_class": n[1], "now_reason": n[2], **({"oracle": o[0], "oracle_class": o[1]} if o else {})})  # fmt: skip
    return rows


KINDS = ("newly_due", "newly_review", "due_to_review", "duplicate_outreach_avoided", "outreach_removed_not_eligible", "review_closed_by_notes",
         "paused_for_missing_information", "newly_missing_information", "pathway_or_status_change", "unchanged")  # fmt: skip


def change_kind(base: str, now: str) -> str:
    """What reading the notes did to one decision (base = structured records only, now = with notes)."""
    if base == now:
        return "unchanged"
    if now == DUE:
        return "newly_due" if base != REVIEW else "pathway_or_status_change"
    if now == REVIEW:
        return "due_to_review" if base == DUE else "newly_review"
    if now == "insufficient_evidence":
        return "paused_for_missing_information" if base == DUE else "newly_missing_information"
    if base == DUE and now == DONE:
        return "duplicate_outreach_avoided"
    if base == DUE and now in NOT_DUE:
        return "outreach_removed_not_eligible"
    if base == REVIEW and (now == DONE or now in NOT_DUE):
        return "review_closed_by_notes"
    return "pathway_or_status_change"


def uplift_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """What reading the notes changed, per guideline and overall (pairs = resident x guideline)."""
    out: dict[str, Any] = {}
    for key in ("all", *sorted({r["key"] for r in rows})):
        part = [r for r in rows if key == "all" or r["key"] == key]
        kinds = Counter(change_kind(r["base"], r["now"]) for r in part)
        newly_case = [r for r in part if r["base"] not in CASE_OUTCOMES and r["now"] in CASE_OUTCOMES]
        out[key] = {
            "pairs": len(part),
            "case_pairs_rules_only": sum(r["base"] in CASE_OUTCOMES for r in part),
            "case_pairs_with_notes": sum(r["now"] in CASE_OUTCOMES for r in part),
            "case_opened_only_because_of_notes": len(newly_case),
            "residents_with_case_opened_only_because_of_notes": len({r["resident_id"] for r in newly_case}),
            "case_removed_because_of_notes": sum(r["base"] in CASE_OUTCOMES and r["now"] not in CASE_OUTCOMES for r in part),
            "changes": {k: kinds.get(k, 0) for k in KINDS},
            "transitions": [{"from": a, "to": b, "count": n, "top_reasons_with_notes": dict(Counter(r["now_reason"] for r in part if (r["base"], r["now"]) == (a, b)).most_common(3))}
                            for (a, b), n in sorted(Counter((r["base"], r["now"]) for r in part).items()) if a != b],
        }  # fmt: skip
    return out


def _tally(rows: Sequence[Mapping[str, Any]], truth: Mapping[tuple[str, str], str], returned: str) -> dict[str, Any]:
    """How one approach (`base_class` or `now_class`) fares against `truth` (expected outcome class per resident x guideline)."""
    c: Counter = Counter()
    for r in rows:
        exp = truth.get((r["resident_id"], r["key"]))
        if exp is not None:
            c[exp, r[returned]] += 1
    n = lambda e, *rs: sum(c[e, x] for x in rs)  # noqa: E731
    due, nodue, rev = sum(v for (e, _), v in c.items() if e == "eligible"), sum(v for (e, _), v in c.items() if e == "ineligible"), sum(v for (e, _), v in c.items() if e == "indeterminate")
    return {
        "pairs": sum(c.values()), "agreement": rate(sum(v for (e, x), v in c.items() if e == x), sum(c.values())),
        "truly_due": {"pairs": due, "found_as_due": n("eligible", "eligible"), "missed_as_not_due": n("eligible", "ineligible"), "sent_to_review": n("eligible", "indeterminate")},
        "truly_not_due": {"pairs": nodue, "unnecessary_outreach": n("ineligible", "eligible"), "correctly_not_due": n("ineligible", "ineligible"), "sent_to_review": n("ineligible", "indeterminate")},
        "expected_review": {"pairs": rev, "flagged_for_review": n("indeterminate", "indeterminate"), "firm_answer_instead": n("indeterminate", "eligible", "ineligible")},
        "confusion_expected_by_returned": {e: {x: c[e, x] for x in ("eligible", "ineligible", "indeterminate", "error")} for e in ("eligible", "ineligible", "indeterminate")},
    }  # fmt: skip


def score_against(rows: Sequence[Mapping[str, Any]], truth: Mapping[tuple[str, str], str]) -> dict[str, Any]:
    return {"rules_only": _tally(rows, truth, "base_class"), "with_notes": _tally(rows, truth, "now_class")}


def planted_fact(rec: Mapping[str, Any]) -> Fact | None:
    """gt.fact.v1 label -> engine Fact (what a perfect extractor would have produced); None for labels that must not be extracted."""
    if not rec.get("expected_present"):
        return None
    rng = parse_partial_date(rec.get("event_date"))
    key = rec.get("related_person_key") or rec.get("relationship") or "self"
    age = rec.get("age_at_onset")
    return Fact(
        id=rec["label_id"], source="fact", category=rec["category"], fact_type=rec["fact_type"], code_system=None, code=None,
        subject_type=rec["subject_type"], relationship=rec.get("relationship"), subject_key=key, assertion=rec["assertion"],
        temporal=rec["temporal"], validation=rec.get("expected_validation") or "accepted", date_start=rng[0] if rng else None,
        date_end=rng[1] if rng else None, onset=int(age) if isinstance(age, int | float) and not isinstance(age, bool) else None,
    )  # fmt: skip


# ================================================================================================ rules baseline (DB)
def _chunks(items: Sequence[Any], n: int):
    for i in range(0, len(items), n):
        yield items[i : i + n]


def run_rules_baseline(sessions: Callable[[], Session], *, cohort_path: Path | None = None, log: Callable[[str], None] = lambda s: None) -> dict[str, Any]:
    from anticipa.datafabric.models import Resident
    from anticipa.guidelines.population import load_inputs, resolve_guidelines

    cohort_path = cohort_path or _newest(ROOT_DIR / "data" / "documents" / "cohorts", "ai-*.json")
    cohort = json.loads(cohort_path.read_text(encoding="utf-8"))
    ids = [uuid.UUID(r) for r in cohort["resident_ids"]]
    dataset_version = cohort["dataset_version"]
    t0 = time.perf_counter()
    with sessions() as s:
        persisted = readers.latest_evaluations(s, [str(i) for i in ids])
        # Scope: guidelines whose stored decisions cover the whole cohort, each evaluated on the date it was decided. Guidelines
        # that other workstreams add later and evaluate in parts stay out until complete, so a rerun keeps the same scope.
        decided: dict[str, set[str]] = defaultdict(set)
        date_of: dict[str, date] = {}
        for p in persisted:
            decided[p["guideline_key"]].add(p["resident_id"])
            date_of[p["guideline_key"]] = max(date_of.get(p["guideline_key"], p["evaluation_date"]), p["evaluation_date"])
        keys = {k for k, done in decided.items() if len(done) == len(ids)}
        groups = {d: [lg for lg in resolve_guidelines(s, d) if lg.guideline_key in keys and date_of[lg.guideline_key] == d]
                  for d in sorted({date_of[k] for k in keys})}  # fmt: skip
        guidelines = [lg for group in groups.values() for lg in group]
        on = max(groups)  # the latest decision date; each guideline is evaluated on its own (date_of)
        gt = gtmod.load_ground_truth(dataset_version, resident_ids=[str(i) for i in ids], split="dev")  # dev only: the test split is single use
        dev_ids = {d["resident_id"] for d in gt.doc_index.values()}
        planted: dict[str, list[Fact]] = defaultdict(list)
        types = {t for lg in guidelines for _, sel in lg.deps.selectors for t in sel.fact_type_in if t != "any"}
        for rec in gt.facts:
            if rec["fact_type"] in types and (f := planted_fact(rec)) is not None:
                planted[rec["resident_id"]].append(f)
        facts_planted_total = len(gt.facts)
        residents = readers.load_residents(s, sorted(dev_ids))
        rows: list[dict[str, Any]] = []
        for part in _chunks(ids, 1000):
            res_rows = s.execute(select(Resident.id, Resident.birth_date, Resident.deceased_date, Resident.administrative_sex).where(Resident.id.in_(part))).all()
            loaded = [(d, group, load_inputs(s, res_rows, group, d)) for d, group in groups.items()]
            for r in res_rows:
                rid = str(r.id)
                for d, group, inputs in loaded:
                    rows += paired_rows(inputs[r.id], group, d, planted[rid] if rid in dev_ids else None)
            log(f"rules baseline: {len(rows) // len(guidelines)} of {len(ids)} residents")
    # reproduction check: the recomputed "with notes" outcome must equal the stored latest decision
    stored = {(p["resident_id"], p["guideline_key"]): p["outcome"] for p in persisted}
    same = sum(stored.get((r["resident_id"], r["key"])) == r["now"] for r in rows)
    # planted screening truth (Mode E oracle of the evaluation harness, three guidelines) on the dev split
    by_res: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in gt.enrichment:
        by_res[item["resident_id"]].append(item)
    truth_e: dict[tuple[str, str], str] = {}
    meta_e: dict[tuple[str, str], tuple[str, str]] = {}
    for r in rows:
        rid, key = r["resident_id"], r["key"]
        res = residents.get(rid)
        if rid not in dev_ids or key not in el.ORACLE or res is None or res["birth_date"] is None:
            continue
        items = by_res.get(rid, [])
        exp = el.expected_outcome(key, res["birth_date"], res["sex"], items, date_of[key])
        if exp is None or exp[2] == "no_planted_item":  # oracle incomplete without a planted item (EL-01 scope)
            continue
        fam = el.ORACLE[key][0]
        rel = [i for i in items if i["family"] in (fam, "exclusion")]
        truth_e[rid, key] = exp[0]
        meta_e[rid, key] = (exp[2], rel[0]["mode"] if rel else "none")
    dev_rows = [r for r in rows if r["resident_id"] in dev_ids]
    scored_e = [r for r in dev_rows if (r["resident_id"], r["key"]) in truth_e]
    groups: dict[str, Any] = {}
    for gk in sorted({"/".join(m) for m in meta_e.values()}):
        part = [r for r in scored_e if "/".join(meta_e[r["resident_id"], r["key"]]) == gk]
        groups[gk] = score_against(part, truth_e)
    # planted evidence truth: the SAME approved rules fed the planted facts (all six guidelines), dev split
    truth_o = {(r["resident_id"], r["key"]): r["oracle_class"] for r in dev_rows}
    modee_vs_oracle = rate(sum(truth_o[k] == v for k, v in truth_e.items()), len(truth_e))
    per_key_o = {k: score_against([r for r in dev_rows if r["key"] == k], truth_o) for k in sorted({r["key"] for r in dev_rows})}
    return {
        "method": "The approved guideline engine (a pure function) is evaluated for every resident of the AI cohort twice in memory: once with structured clinical records only and once with structured records plus the facts extracted from notes (current Anticipa). Nothing is written.",
        "scope": {
            "dataset_version": dataset_version, "cohort_id": cohort["cohort_id"], "cohort_resident_ids_sha256": cohort.get("resident_ids_sha256"),
            "residents": len(ids), "guidelines": [{"key": lg.guideline_key, "version": lg.version, "content_hash": lg.content_hash} for lg in sorted(guidelines, key=lambda g: g.guideline_key)],
            "evaluation_date": on.isoformat(), "pairs": len(rows),
            "ground_truth": {"split_scored": "dev", "dev_residents": len(dev_ids), "ground_truth_id": gt.ground_truth_id, "test_labels_loaded": False,
                             "planted_fact_labels_dev_cohort": facts_planted_total,
                             "note": "test split labels were not loaded or scored and its access ledger was not touched (the integrity check hashes the files without parsing them)"},
        },
        "reproduces_stored_decisions": rate(same, len(rows)) | {"metric": "recomputed outcome with notes equals the latest stored eligibility decision (resident x guideline)"},
        "uplift_from_reading_notes": uplift_summary(rows),
        "against_planted_screening_truth": {
            "definition": "Mode E oracle of the evaluation harness (evaluation/eligibility.py expected_outcome): colorectal average risk, breast and cervical screening, residents with a planted screening or exclusion item or a demographic rule that decides; dev split only.",
            "guidelines": sorted(el.ORACLE), "pairs": len(scored_e), **score_against(scored_e, truth_e),
            "by_basis_and_item_mode": groups,
        },
        "against_planted_evidence_truth": {
            "definition": "The same approved rules fed structured records plus the planted notes facts (labels marked expected present, with their planted assertion, temporal status, relationship and validation). It measures how much of the achievable result from reading notes is realised; it is not clinical truth. All six guidelines, dev split only.",
            "agrees_with_screening_truth_on_shared_pairs": modee_vs_oracle, "pairs": len(truth_o), **score_against(dev_rows, truth_o), "by_guideline": per_key_o,
        },
        "seconds": round(time.perf_counter() - t0, 1),
    }  # fmt: skip


def _newest(folder: Path, pattern: str) -> Path:
    files = sorted(folder.glob(pattern))
    if not files:
        raise FileNotFoundError(f"no file matches {folder / pattern}")
    return files[-1]


# ================================================================================================ planner baseline (pure)
@dataclass
class Prepared:
    """The usable pools and slots of one planning input, in the solver's own order, plus travel geography."""

    pool_list: list[PoolSpec]
    pool_slots: list[list[SlotSpec]]
    travel: Any
    by_service: dict[str, list[int]]

    def eligible(self, service: str, district: str, max_tier: int) -> list[int]:
        """Usable pools offering `service` within `max_tier` of `district`, nearest first (the solver's own ordering)."""
        hit = [pi for pi in self.by_service.get(service, ()) if self.travel.tier(district, self.pool_list[pi].district_code) <= max_tier]
        return sorted(hit, key=lambda pi: (self.travel.tier(district, self.pool_list[pi].district_code), pi))


def prepare(cases: Sequence[PlannableCase], pools: Sequence[PoolSpec], slots: Sequence[SlotSpec], cfg: PlanConfig) -> Prepared:
    from anticipa.planning.solver import TravelModel, _usable

    by_id = {p.clinic_service_id: p for p in pools}
    usable: dict[uuid.UUID, list[SlotSpec]] = defaultdict(list)
    for s in slots:
        p = by_id.get(s.clinic_service_id)
        if p is not None and _usable(s, p, cfg):
            usable[s.clinic_service_id].append(s)
    pool_list = sorted((p for p in pools if p.clinic_service_id in usable), key=lambda p: (p.clinic_key, p.service_key))
    by_service: dict[str, list[int]] = defaultdict(list)
    for pi, p in enumerate(pool_list):
        by_service[p.service_key].append(pi)
    return Prepared(pool_list, [sorted(usable[p.clinic_service_id], key=lambda s: (s.start, s.slot_id.int)) for p in pool_list],
                    TravelModel.from_inputs(cases, pools), by_service)  # fmt: skip


def snapshot_hash(cases: Sequence[PlannableCase], prep: Prepared, cfg: PlanConfig) -> str:
    from anticipa.planning.solver import _snapshot_hash

    return _snapshot_hash(cases, prep.pool_list, {p.clinic_service_id: prep.pool_slots[i] for i, p in enumerate(prep.pool_list)}, cfg)


def greedy_assign(cases: Sequence[PlannableCase], prep: Prepared, cfg: PlanConfig, *, by_priority: bool = False) -> dict[uuid.UUID, SlotSpec]:
    """First come first served: cases in arrival order (opened_at, then case id); each takes the earliest free slot at the
    nearest eligible clinic (travel tier, then clinic order) that does not overlap another appointment of the same resident.
    Same hard constraints as the planner; no optimisation. `by_priority` sorts by priority weight first (CP-08's baseline)."""
    free = [[s.free for s in sl] for sl in prep.pool_slots]
    cursor = [0] * len(prep.pool_list)
    residents = Counter(c.resident_id for c in cases)
    busy: dict[uuid.UUID, list[tuple[datetime, datetime]]] = defaultdict(list)
    ahead: dict[tuple[str, str], list[int]] = {}
    placed: dict[uuid.UUID, SlotSpec] = {}
    order = sorted(cases, key=lambda c: (-cfg.weight_of(c.priority) if by_priority else 0, c.opened_at or EPOCH, c.case_id.int))
    for c in order:
        if not c.service_key or not c.district_code:
            continue
        pools = ahead.setdefault((c.service_key, c.district_code), prep.eligible(c.service_key, c.district_code, cfg.max_travel_tier))
        for pi in pools:
            sl, fr, i = prep.pool_slots[pi], free[pi], cursor[pi]
            while i < len(sl) and fr[i] == 0:
                i += 1
            cursor[pi] = i
            multi = residents[c.resident_id] > 1
            j = next((j for j in range(i, len(sl)) if fr[j] and not (multi and any(sl[j].start < be and bs < sl[j].end for bs, be in busy[c.resident_id]))), -1)
            if j < 0:
                continue
            fr[j] -= 1
            if multi:
                busy[c.resident_id].append((sl[j].start, sl[j].end))
            placed[c.case_id] = sl[j]
            break
    return placed


def plan_metrics(cases: Sequence[PlannableCase], placed: Mapping[uuid.UUID, SlotSpec], prep: Prepared, cfg: PlanConfig, pools: Sequence[PoolSpec], slots: Sequence[SlotSpec]) -> dict[str, Any]:
    """Identical definitions for every approach (the planner's own `_equity_summary` and `_metrics` definitions)."""
    from anticipa.planning.validate import validate_plan

    district_of = {p.clinic_service_id: p.district_code for p in prep.pool_list}
    n = len(cases)
    reach: dict[tuple[str, str], bool] = {}
    dims = {d: defaultdict(lambda: {"demand": 0, "reachable": 0, "allocated": 0}) for d in ("district", "language", "service", "priority")}
    tiers: Counter = Counter()
    waits: list[float] = []
    for c in cases:
        k = (c.service_key, c.district_code)
        if k not in reach:
            reach[k] = bool(c.service_key and c.district_code and prep.eligible(c.service_key, c.district_code, cfg.max_travel_tier))
        got = placed.get(c.case_id)
        for dim, val in (("district", c.district_code), ("language", c.language or "unknown"), ("service", c.service_key), ("priority", c.priority or "")):
            row = dims[dim][val]
            row["demand"] += 1
            row["reachable"] += reach[k]
            row["allocated"] += got is not None
        if got is not None:
            tiers[prep.travel.tier(c.district_code, district_of[got.clinic_service_id])] += 1
            waits.append((got.start - cfg.horizon_start).total_seconds() / 86400)
    weights = {c.case_id: cfg.weight_of(c.priority) for c in cases}
    seats = sum(s.free for sl in prep.pool_slots for s in sl)
    demanded = {c.service_key for c in cases}
    demanded_seats = sum(s.free for p, sl in zip(prep.pool_list, prep.pool_slots, strict=True) if p.service_key in demanded for s in sl)
    equity: dict[str, Any] = {}
    for dim in ("district", "language"):
        groups = {g: {**r, "coverage": r6(r["allocated"] / r["demand"])} for g, r in sorted(dims[dim].items())}
        ok = {g: r["allocated"] / r["demand"] for g, r in dims[dim].items() if r["demand"] >= cfg.min_group_size and r["reachable"] > 0}
        equity[dim] = {"groups_compared": len(ok), "min_group_size": cfg.min_group_size, "by_group": groups} | (
            {"lowest_group": min(ok, key=ok.get), "lowest_coverage": r6(min(ok.values())), "highest_coverage": r6(max(ok.values())),
             "gap_points": r6((max(ok.values()) - min(ok.values())) * 100), "ratio": r6(min(ok.values()) / max(ok.values()))} if len(ok) >= 2 else {})  # fmt: skip
    violations = validate_plan([Allocation(cid, s.slot_id) for cid, s in placed.items()], cases, pools, slots, cfg)
    by_service = {k: {**r, "coverage": r6(r["allocated"] / r["demand"])} for k, r in sorted(dims["service"].items())}
    return {
        "cases": n, "allocated": len(placed), "unallocated": n - len(placed), "allocation_rate": rate(len(placed), n),
        "priority_weighted_coverage": rate(sum(weights[c] for c in placed), sum(weights.values())),
        "seat_utilisation": rate(len(placed), seats), "seats_total": seats,
        "seat_utilisation_services_with_cases": rate(len(placed), demanded_seats), "seats_services_with_cases": demanded_seats,
        "coverage_by_priority": {k: {**r, "coverage": r6(r["allocated"] / r["demand"])} for k, r in sorted(dims["priority"].items())},
        "coverage_by_service": by_service, "equity": equity,
        "travel": {"own_district": tiers.get(0, 0), "adjacent_district": tiers.get(1, 0), "tier_counts": {str(t): v for t, v in sorted(tiers.items())},
                   "own_district_share": r6(tiers.get(0, 0) / len(placed)) if placed else None, "mean_tier": r6(sum(t * v for t, v in tiers.items()) / len(placed)) if placed else None},
        "wait_days_from_horizon_start": {"median": r6(statistics.median(waits)) if waits else None, "mean": r6(statistics.fmean(waits)) if waits else None, "p90": r6(percentile(waits, 0.9))},
        "hard_constraint_violations": {"count": len(violations), "by_constraint": dict(sorted(Counter(v.constraint for v in violations).items()))},
    }  # fmt: skip


# ================================================================================================ planner baseline (DB)
def run_planner_baseline(sessions: Callable[[], Session], *, log: Callable[[str], None] = lambda s: None) -> dict[str, Any]:
    from anticipa.caregaps.models import CareGapCase
    from anticipa.planning.models import (
        CapacitySlot,
        ClinicService,
        PlannedAllocation,
        PlanningRun,
        UnallocatedCase,
    )
    from anticipa.planning.service import load_capacity

    t0 = time.perf_counter()
    with sessions() as s:
        run = s.execute(select(PlanningRun).where(PlanningRun.status == "completed").order_by(PlanningRun.created_at.desc()).limit(1)).scalar_one()
        cfg = PlanConfig.model_validate(run.config)
        rows = [r for model in (PlannedAllocation, UnallocatedCase) for r in s.execute(
            select(model.care_gap_case_id, model.resident_id, model.service_key, model.priority, model.district_code, model.language)
            .where(model.planning_run_id == run.id)).all()]  # fmt: skip
        ids = [r.care_gap_case_id for r in rows]
        opened: dict[uuid.UUID, datetime] = {}
        for part in _chunks(ids, 5000):
            opened |= dict(s.execute(select(CareGapCase.id, CareGapCase.opened_at).where(CareGapCase.id.in_(part))).all())
        cases = sorted((PlannableCase(r.care_gap_case_id, r.resident_id, r.service_key or "", r.priority or "", r.district_code or "", r.language or "unknown", opened.get(r.care_gap_case_id)) for r in rows), key=lambda c: c.case_id.int)  # fmt: skip
        pools, slots = load_capacity(s, cfg.horizon_start, cfg.horizon_end)
        later = [set(s.execute(select(m.id).where(m.created_at > run.started_at)).scalars()) for m in (ClinicService, CapacitySlot)]
        pools = [p for p in pools if p.clinic_service_id not in later[0]]  # capacity added after the run is not part of its input
        slots = [x for x in slots if x.slot_id not in later[1] and x.clinic_service_id not in later[0]]
        since = dict(s.execute(select(PlannedAllocation.capacity_slot_id, func.count()).where(PlannedAllocation.allocation_status == "confirmed", PlannedAllocation.confirmed_at >= run.completed_at).group_by(PlannedAllocation.capacity_slot_id)).all())  # fmt: skip
        stored = dict(s.execute(select(PlannedAllocation.care_gap_case_id, PlannedAllocation.capacity_slot_id).where(PlannedAllocation.planning_run_id == run.id, PlannedAllocation.allocation_status != "superseded")).all())  # fmt: skip
        run_meta = {"planning_run_id": str(run.id), "completed_at": run.completed_at.isoformat(), "algorithm_version": run.algorithm_version, "solver_status": (run.solver_parameters or {}).get("status"),
                    "solver_wall_time_s": (run.solver_parameters or {}).get("wall_time_s"), "seed": run.seed, "input_snapshot_hash": run.input_snapshot_hash,
                    "planner_recorded_metrics": {k: (run.metrics or {}).get(k) for k in ("cases_total", "allocated", "coverage", "seats_total", "utilization")}}  # fmt: skip
        recorded_eq = {d: ((run.metrics or {}).get("equity") or {}).get(d, {}).get("gap") for d in ("district", "language")}
    slots = [replace(sl, free=sl.free + since.get(sl.slot_id, 0)) for sl in slots]  # capacity as it was when the plan was made (later confirmations reserved seats)
    log(f"planner baseline: {len(cases)} cases, {len(pools)} pools, {len(slots)} slots")
    prep = prepare(cases, pools, slots, cfg)
    by_slot = {sl.slot_id: sl for sl in slots}
    solver_placed = {cid: by_slot[sid] for cid, sid in stored.items()}
    match = snapshot_hash(cases, prep, cfg) == run.input_snapshot_hash
    greedy = greedy_assign(cases, prep, cfg)
    first, ortools = plan_metrics(cases, greedy, prep, cfg, pools, slots), plan_metrics(cases, solver_placed, prep, cfg, pools, slots)
    return {
        "method": "The cases and capacity of the latest completed OR-Tools planning run are rebuilt from the database (cases from the run's own allocated and unallocated rows, opened_at from the case; capacity as at plan time) and a first come first served allocator is run on them in memory. Both allocations are measured with the same code and checked by the same hard constraint validator.",
        "run": run_meta, "config": json.loads(cfg.model_dump_json()),
        "inputs_match_run_snapshot_hash": match,
        "inputs": {"cases": len(cases), "pools_with_usable_seats": len(prep.pool_list), "usable_seats": ortools["seats_total"], "priorities": dict(Counter(c.priority or "(none)" for c in cases)),
                   "distinct_residents": len({c.resident_id for c in cases})},
        "ortools_measure_matches_planner_equity_gap": {"district_gap_recorded": recorded_eq["district"], "district_gap_measured": ortools["equity"]["district"].get("gap_points"),
                                                       "language_gap_recorded": recorded_eq["language"], "language_gap_measured": ortools["equity"]["language"].get("gap_points"), "note": "recorded gaps are ratios, measured gap_points are percentage points"},  # fmt: skip
        "ortools_cpsat": ortools, "first_come_first_served": first,
        "difference": _diff(first, ortools),
        "seconds": round(time.perf_counter() - t0, 1),
    }  # fmt: skip


def _diff(first: Mapping[str, Any], ortools: Mapping[str, Any]) -> dict[str, Any]:
    f = lambda m, *path: _dig(m, path)  # noqa: E731
    gap = lambda m, d: f(m, "equity", d, "gap_points")  # noqa: E731
    return {
        "additional_cases_allocated_by_ortools": ortools["allocated"] - first["allocated"],
        "allocation_rate_points": r6((ortools["allocation_rate"]["value"] - first["allocation_rate"]["value"]) * 100),
        "district_equity_gap_points": {"first_come": gap(first, "district"), "ortools": gap(ortools, "district")},
        "language_equity_gap_points": {"first_come": gap(first, "language"), "ortools": gap(ortools, "language")},
        "own_district_share": {"first_come": f(first, "travel", "own_district_share"), "ortools": f(ortools, "travel", "own_district_share")},
        "mean_travel_tier": {"first_come": f(first, "travel", "mean_tier"), "ortools": f(ortools, "travel", "mean_tier")},
        "median_wait_days": {"first_come": f(first, "wait_days_from_horizon_start", "median"), "ortools": f(ortools, "wait_days_from_horizon_start", "median")},
        "hard_constraint_violations": {"first_come": first["hard_constraint_violations"]["count"], "ortools": ortools["hard_constraint_violations"]["count"]},
    }  # fmt: skip


def _dig(m: Mapping[str, Any], path: Sequence[str]) -> Any:
    for p in path:
        m = m.get(p) if isinstance(m, Mapping) else None
    return m


# ================================================================================================ measured inputs (B09 data half)
def measured_inputs(sessions: Callable[[], Session], cohort_residents: int) -> dict[str, Any]:
    """Counts and rates a viewer can scale to a chosen population. Everything here is read from the database or from the final
    evaluation report; nothing is projected, no benefit or saving is estimated."""
    from anticipa.caregaps.models import CareGapCase
    from anticipa.datafabric.models import Document, Resident
    from anticipa.evidence.models import EvidenceFact

    with sessions() as s:
        residents = s.scalar(select(func.count()).select_from(Resident)) or 0
        cases, with_case = s.execute(select(func.count(), func.count(func.distinct(CareGapCase.resident_id)))).one()
        facts = dict(s.execute(select(EvidenceFact.validation_status, func.count()).group_by(EvidenceFact.validation_status)).all())
        docs = s.scalar(select(func.count()).select_from(Document).where(Document.processing_status == "extracted")) or 0
    per1000 = lambda n: r6(n / residents * 1000)  # noqa: E731
    out: dict[str, Any] = {
        "basis": "database counts at report time and the final evaluation report; measured on synthetic residents, not projected",
        "residents": residents, "care_gap_cases": cases, "residents_with_a_case": with_case,
        "care_gap_cases_per_1000_residents": per1000(cases), "residents_with_a_case_per_1000_residents": per1000(with_case),
        "ai_cohort": {"residents": cohort_residents, "documents_extracted": docs, "evidence_facts": sum(facts.values()),
                      "evidence_facts_by_validation": dict(sorted(facts.items())), "documents_per_resident": r6(docs / cohort_residents) if cohort_residents else None,
                      "evidence_facts_per_resident": r6(sum(facts.values()) / cohort_residents) if cohort_residents else None},  # fmt: skip
    }
    out["ai_extraction_cost"] = _ai_cost(cohort_residents)
    return out


def _ai_cost(cohort_residents: int) -> dict[str, Any]:
    """Extraction cost and throughput as the final evaluation run measured them (metric PF-01 and PF-03)."""
    try:
        summary = json.loads(_newest(serving.REPORTS_DIR, "evaluation-summary-*.json").read_text(encoding="utf-8"))
        run_id = summary["run"]["run_id"]
        metrics = json.loads((serving.REPORTS_DIR / run_id / "report.json").read_text(encoding="utf-8"))["metrics"]
        pf1, pf3 = metrics["PF-01"], metrics["PF-03"]
    except (FileNotFoundError, KeyError, ValueError) as exc:
        return {"status": "not_available", "reason": f"final evaluation report not readable ({type(exc).__name__})"}
    return {
        "source": f"data/reports/{run_id}/report.json metrics PF-01 and PF-03", "cost_basis": pf1["cost_basis"],
        "total_cost_usd": pf1["total_cost_usd"], "documents": pf1["n"], "usd_per_1000_documents": pf1["value"],
        "usd_per_1000_residents_of_the_ai_cohort": r6(pf1["total_cost_usd"] / cohort_residents * 1000) if cohort_residents else None,
        "documents_per_active_minute": pf3["value"], "calls": pf1["calls"], "escalation_cost_usd": pf1["escalation_cost_usd"],
    }  # fmt: skip


# ================================================================================================ headlines and limitations
WORDS = {3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}
FAMILY_HISTORY = ("brca_genetic_counseling_referral", "brca_risk_assessment", "colorectal_family_history")
LIMITATIONS = [
    "Residents, notes and clinic capacity are synthetic. The baselines show how the mechanism behaves on generated data, not a benefit in a real population.",
    "The planted truth comes from the same generator as the notes, and no person has reviewed its labels. Generated notes are cleaner than real documentation.",
    "Truth scoring uses the development split only (1,969 of the 10,000 cohort residents), the split the extraction prompt was tuned on. The held out test split labels were not loaded or scored for this report.",
    "The screening truth uses exact planted dates, but a note sometimes states only a month or a year, so the expected outcome can be more definite than the note supports. Residents the engine sends to review count as disagreeing with an expected invitation.",
    "The evidence truth is the same approved rules fed the planted facts. It measures what reading notes achieves compared with perfect extraction, not clinical correctness. All six guideline versions are still pending clinical review.",
    "Every care gap case carries the same priority (routine), so priority weighting and coverage of high priority cases cannot be measured on this data. The planner's priority behaviour is covered by unit tests only.",
    "The first come baseline serves cases in the order they were opened (opened_at, then case id; cases were opened in batches), at the nearest clinic with free seats, spilling to adjacent districts. A stricter own district only rule would do worse. District geography is a synthetic ring, not real travel time.",
    "Capacity is simulated. Both plans fill every seat of the screening services, so equal totals follow from seats being the limit.",
    "No outcome such as deaths averted or money saved is estimated: the project holds no benefit model or cited parameter for it.",
    "The AI cost is estimated from configured prices and has not been reconciled with a provider bill.",
]  # fmt: skip


DEFINITIONS = {
    "pair": "One resident evaluated against one approved guideline version (six guidelines).",
    "rules_only": "The approved guideline engine fed structured clinical records only (no fact extracted from a note).",
    "with_notes": "The same engine fed structured records plus the facts extracted from notes: what Anticipa does today.",
    "case_pairs": "Pairs whose outcome is eligible (due) or requires clinical review; these open a care gap case.",
    "newly_due": "Not due with structured records alone, due with notes.",
    "newly_review": "Not in a case with structured records alone (and not due), requires clinical review with notes.",
    "due_to_review": "Due with structured records alone, requires clinical review with notes (for example a documented mastectomy or hysterectomy).",
    "duplicate_outreach_avoided": "Due with structured records alone, already completed with notes (a screening found only in a note, up to date).",
    "outreach_removed_not_eligible": "Due with structured records alone, not eligible or temporarily excluded with notes (a screening already planned, or the resident belongs to another pathway such as colorectal family history).",
    "paused_for_missing_information": "Due with structured records alone, insufficient evidence with notes (a note mentions a screening that cannot be dated or confirmed, or an unresolved exclusion). No invitation until a person resolves it.",
    "newly_missing_information": "Not due with structured records alone, insufficient evidence with notes (an unresolved family history or trigger statement).",
    "review_closed_by_notes": "Requires clinical review with structured records alone, already completed or not due with notes.",
    "pathway_or_status_change": "Any other change of outcome, for example average risk colorectal screening handing over to the family history pathway.",
    "case_opened_only_because_of_notes": "Pairs with no case with structured records alone and a case with notes.",
    "truth_screening": "Expected outcome from the planted screening and exclusion items or a demographic rule (colorectal average risk, breast, cervical), as the evaluation harness defines it. Pairs with no planted item are left out.",
    "truth_evidence": "Outcome of the same approved rules fed structured records plus the planted notes facts, for all six guidelines.",
    "truly_due": "Pairs whose truth is an invitation (expected outcome class eligible).",
    "found_as_due": "Of truly due pairs, those the approach returned as eligible.",
    "missed_as_not_due": "Of truly due pairs, those the approach returned as not eligible or already completed.",
    "sent_to_review": "Pairs the approach returned as indeterminate (requires clinical review or insufficient evidence).",
    "truly_not_due": "Pairs whose truth is no invitation (expected outcome class ineligible).",
    "unnecessary_outreach": "Of truly not due pairs, those the approach returned as eligible (an invitation that should not go out).",
    "expected_review": "Pairs whose truth is clinical review or more evidence (expected outcome class indeterminate).",
    "agreement": "Pairs where the returned outcome class equals the expected class, over all scored pairs, with a 95 percent Wilson interval.",
    "allocation_rate": "Cases given a slot divided by the plannable cases of the planning run.",
    "seat_utilisation": "Cases given a slot divided by the usable free seats of every clinic service (including services no case needs); the _services_with_cases variant counts only seats of services that have cases.",
    "priority_weighted_coverage": "Placed priority weight divided by total priority weight (weights urgent 8, high 4, medium 2, routine and low 1). Equals plain coverage while every case has the same priority.",
    "equity gap_points": "Highest minus lowest coverage, in percentage points, among groups with at least min_group_size cases and a reachable clinic. Coverage of a group is its placed cases divided by its cases. Same definition as the planner's own equity summary.",
    "travel": "Travel tier is the ring distance between the resident's district and the clinic's district (0 own district, 1 adjacent); a synthetic geography, not travel time.",
    "wait_days_from_horizon_start": "Days from the start of the planning horizon to the start of the proposed slot.",
    "hard_constraint_violations": "Violations found by the planner's hard constraint validator (service match, capacity, open clinic, horizon, duration, travel, resident time conflict, one slot per case).",
}  # fmt: skip


def headlines(rules: Mapping[str, Any], planner: Mapping[str, Any]) -> list[str]:
    """Six plain sentences, every number read from the sections above (no dashes, no projections)."""
    u, n = rules["uplift_from_reading_notes"], rules["scope"]["residents"]
    pct = lambda r: f"{r['value'] * 100:.0f} percent"  # noqa: E731
    gcr, bra, cfh = (u[k]["case_pairs_with_notes"] for k in FAMILY_HISTORY)
    fh_records = sum(u[k]["case_pairs_rules_only"] for k in FAMILY_HISTORY)
    ev, sc = rules["against_planted_evidence_truth"], rules["against_planted_screening_truth"]
    due_rec, due_now = ev["rules_only"]["truly_due"], ev["with_notes"]["truly_due"]
    sc_rec, sc_now = sc["rules_only"], sc["with_notes"]
    dev = rules["scope"]["ground_truth"]["dev_residents"]
    ch = u["all"]["changes"]
    first, opt, d = planner["first_come_first_served"], planner["ortools_cpsat"], planner["difference"]
    more = d["additional_cases_allocated_by_ortools"]
    seats = (
        f"The first come first served plan and the optimised plan both place {opt['allocated']:,} of {opt['cases']:,} cases ({pct(opt['allocation_rate'])}) "
        "because seats run out in every screening service, so the optimiser changes who gets a seat, not how many."
        if more == 0
        else f"The optimised plan places {more:,} more of {opt['cases']:,} cases than first come first served ({pct(opt['allocation_rate'])} against {pct(first['allocation_rate'])})."
    )
    paused_reasons = next((t["top_reasons_with_notes"] for t in u["all"]["transitions"] if (t["from"], t["to"]) == (DUE, "insufficient_evidence")), {})
    undated = paused_reasons.get("completion_evidence_indeterminate", 0)
    return [
        f"Reading notes found {gcr + bra + cfh:,} family history care gaps among {n:,} residents (structured records alone found {fh_records:,}): {gcr:,} genetic counselling referrals, {bra:,} BRCA risk assessments and {cfh:,} colorectal family history cases.",
        f"Against what the planted notes say ({dev:,} development residents, all {WORDS.get(len(rules['scope']['guidelines']), len(rules['scope']['guidelines']))} guidelines), structured records alone flagged {due_rec['found_as_due']:,} of the {due_rec['pairs']:,} care gaps that should be flagged, {pct(rate(due_rec['found_as_due'], due_rec['pairs']))}, and reading notes flagged {due_now['found_as_due']:,}, {pct(rate(due_now['found_as_due'], due_now['pairs']))}.",
        f"Notes stopped repeat invitations: {ch['duplicate_outreach_avoided']:,} invitations that structured records alone would send were for screenings already done, and against the planted screening truth invitations that should not go out fell from {sc_rec['truly_not_due']['unnecessary_outreach']:,} to {sc_now['truly_not_due']['unnecessary_outreach']:,} of {sc_now['truly_not_due']['pairs']:,} decisions.",
        f"The trade is caution: {ch['paused_for_missing_information']:,} screening invitations that structured records alone would send are held for review, {undated:,} of them because a note mentions a screening that cannot be dated or confirmed, and against the screening truth {sc_now['truly_due']['sent_to_review']:,} of {sc_now['truly_due']['pairs']:,} overdue care gaps wait for review instead of an invitation.",
        seats,
        f"The optimised plan narrows the gap between the best and worst served district from {d['district_equity_gap_points']['first_come']:.1f} to {d['district_equity_gap_points']['ortools']:.1f} percentage points (language gap {d['language_equity_gap_points']['first_come']:.2f} to {d['language_equity_gap_points']['ortools']:.2f}), raises own district placements from {d['own_district_share']['first_come'] * 100:.1f} to {d['own_district_share']['ortools'] * 100:.1f} percent, and neither plan breaks a hard constraint ({d['hard_constraint_violations']['first_come']} and {d['hard_constraint_violations']['ortools']} violations).",
    ]


# ================================================================================================ report, storage, CLI
VOLATILE = ("generated_at", "seconds", "metrics_digest")


def digest(report: Mapping[str, Any]) -> str:
    import hashlib

    def clean(o: Any) -> Any:
        if isinstance(o, Mapping):
            return {k: clean(v) for k, v in o.items() if k not in VOLATILE}
        return [clean(v) for v in o] if isinstance(o, list) else o

    return hashlib.sha256(json.dumps(clean(report), sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def build_report(sessions: Callable[[], Session], *, log: Callable[[str], None] = lambda s: None) -> dict[str, Any]:
    rules = run_rules_baseline(sessions, log=log)
    planner = run_planner_baseline(sessions, log=log)
    measured = measured_inputs(sessions, rules["scope"]["residents"])
    checks = (
        (rules["reproduces_stored_decisions"]["value"] == 1.0, "recomputed decisions with notes differ from some stored decisions"),
        (planner["inputs_match_run_snapshot_hash"], "planner inputs could not be matched to the run's input snapshot hash"),
    )
    warnings = [message for ok, message in checks if not ok]
    report = {
        "schema_version": SCHEMA, "generated_at": datetime.now(UTC).isoformat(),
        "about": "Proven impact evidence from zero cost, read only baselines computed with the product's own deterministic code on synthetic data. Every number carries its method and denominator; nothing is projected.",
        "headlines": headlines(rules, planner), "warnings": warnings, "rules_baseline": rules, "planner_baseline": planner, "measured_inputs": measured,
        "definitions": DEFINITIONS, "limitations": LIMITATIONS,
    }
    report["metrics_digest"] = digest(report)
    return report


def store_report(sessions: Callable[[], Session], report: Mapping[str, Any]) -> None:
    """Keep a copy in `analytics_snapshots` (aggregates only, no identifiers) so a deployment without `data/` can serve it."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from anticipa.analytics.models import AnalyticsSnapshot

    values = {"payload": dict(report), "computed_at": datetime.fromisoformat(report["generated_at"]), "duration_ms": round((report["rules_baseline"]["seconds"] + report["planner_baseline"]["seconds"]) * 1000), "refreshed_by": "cli"}
    with sessions() as s:
        stmt = pg_insert(AnalyticsSnapshot).values(key=serving.STORE_KEY, **values)
        s.execute(stmt.on_conflict_do_update(index_elements=["key"], set_={k: getattr(stmt.excluded, k) for k in values}))
        s.commit()


def cli(args: Any, sessions: Callable[[], Session]) -> int:
    report = build_report(sessions, log=lambda m: print(m, flush=True))
    out = Path(args.out) if args.out else serving.REPORTS_DIR / f"impact-baselines-{datetime.now(UTC):%Y%m%d}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True, default=str, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.store:
        store_report(sessions, report)
    print(json.dumps({"written": str(out), "stored_in_database": bool(args.store), "metrics_digest": report["metrics_digest"], "headlines": report["headlines"]}, indent=2, ensure_ascii=False))
    return 0
