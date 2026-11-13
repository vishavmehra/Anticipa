"""Eligibility evaluation (Docs/09 §7.5, EL-*). Deterministic; no model calls, ever (INV-05).

Callers: anticipa.evaluation.runner. Instruction: deliver a working, tested, demonstrable Anticipa MVP including the
evaluation report.

Two modes, always reported separately:
  Mode R - rule regression. The 181+ cases authored in config/guidelines/rules_draft.json (expected outcomes written
           with the rules, never adjusted here) are replayed through the Guideline Engine with in-memory evidence.
           It isolates rule logic from extraction. The expectations come from the guideline authors, so this proves
           the engine implements the rules as written, NOT that the rules are clinically right.
  Mode E - end to end. The latest persisted evaluation per (resident, guideline) is compared with an independently
           derived expected outcome built from the PLANTED enrichment ground truth (which screenings and exclusions
           the generator gave each resident, and whether each is documented in a structured record or only in a note)
           plus age/sex at the ground truth's as_of date. Only three guidelines have such an oracle: colorectal
           average-risk, breast and cervical screening. Residents without a planted item are scored separately
           (`no_planted_item`): Synthea may have produced a real screening the ground truth does not list.
"""

from __future__ import annotations

import calendar
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from anticipa.config import ROOT_DIR
from anticipa.evaluation.stats import rate

DRAFT_PATH = ROOT_DIR / "config" / "guidelines" / "rules_draft.json"
CLASSES = ("eligible", "ineligible", "indeterminate", "error")
DETERMINISM_RUNS = 3
# guideline_key -> (planted screening family, exclusion concepts that matter, minimum age)
ORACLE = {
    "colorectal_average_risk": ("crc", {"total_colectomy"}),
    "breast_cancer_screening": ("breast", {"bilateral_mastectomy"}),
    "cervical_cancer_screening": ("cervical", {"hysterectomy"}),
}


# ----------------------------------------------------------------------------------------------- Mode R
def _fact_for(item: Mapping[str, Any], library: Mapping[str, Any]):
    import uuid

    from anticipa.guidelines.evaluator import Fact, parse_partial_date

    rng = parse_partial_date(item.get("date")) if item.get("date") else None
    if "sel" in item:
        sel = library[item["sel"]]
        family = "family_member" in sel["subject_type_in"] and (
            item.get("subject") not in (None, "self") or sel["subject_type_in"] == ["family_member"]
        )
        category = next((c for c in sel["category_in"] if c != "any"), "procedure")
        fact_type = next((t for t in sel["fact_type_in"] if t != "any"), "any")
        relationship = item.get("subject") or (sel.get("relationship_in") or [None])[0]
        assertion = item.get("assertion", sel["assertion_status_in"][0])
        temporal = item.get("temporal", sel["temporal_status_in"][0])
    else:
        family = item.get("subject") not in (None, "self")
        category, fact_type, relationship = item["cat"], item["fact"], item.get("subject")
        assertion, temporal = item.get("assertion", "positive"), item.get("temporal", "historical")
    return Fact(
        id=str(uuid.uuid4()), source="fact", category=category, fact_type=fact_type, code_system=None, code=None,
        subject_type="family_member" if family else "resident", relationship=relationship if family else None,
        subject_key=relationship if family else "self", assertion=assertion, temporal=temporal,
        validation=item.get("validation", "accepted"), date_start=rng[0] if rng else None,
        date_end=rng[1] if rng else None, onset=item.get("onset"),
    )  # fmt: skip


def _canonical(result: Any) -> str:
    return json.dumps(
        {"o": result.outcome, "c": result.outcome_class, "r": result.reason_code, "d": result.decisive_rule_id,
         "k": [c.as_dict() for c in result.criteria], "e": result.evidence_references,
         "m": result.missing_information, "x": result.conflicting_information},
        sort_keys=True, default=str,
    )  # fmt: skip


def run_regression(path: Path | None = None) -> dict[str, Any]:
    """Replay every authored regression case; each is evaluated DETERMINISM_RUNS times (EL-08)."""
    import uuid

    from anticipa.guidelines.evaluator import ResidentFacts, ResidentInputs, evaluate_guideline, shift_back
    from anticipa.guidelines.rules import parse_guideline_definition, parse_rule

    draft = json.loads(Path(path or DRAFT_PATH).read_text(encoding="utf-8"))
    library = draft["selector_library"]
    raw_on = draft.get("evaluation_date") or (draft.get("conventions") or {}).get("test_evaluation_date")
    on = date.fromisoformat(raw_on) if isinstance(raw_on, str) and len(raw_on) == 10 else date(2026, 11, 8)
    total = passed = reason_total = reason_ok = diffs = 0
    failures: list[dict[str, str]] = []
    per: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for g in draft["guidelines"]:
        names: set[str] = set()
        for raw in g["rules"]:
            names |= parse_rule(raw).selectors_used()
        definition = parse_guideline_definition(g["rules"], {n: library[n] for n in sorted(names)}, g.get("decision_policy"))
        for case in g["test_cases"]:
            birth = shift_back(on, years=int(case["age"])) if case.get("age") is not None else None
            facts = [_fact_for(i, library) for i in case["evidence"]]
            latest = None if case.get("no_coverage") else shift_back(on, months=6)
            inputs = ResidentInputs(ResidentFacts(uuid.uuid4(), birth, None, case.get("sex")), facts, latest)
            outs = [evaluate_guideline(definition, inputs, on, g.get("sources", []), g.get("eligible_explanation"))
                    for _ in range(DETERMINISM_RUNS)]  # fmt: skip
            diffs += len({_canonical(o) for o in outs}) > 1
            res, exp = outs[0], case["expected"]
            total += 1
            per[g["guideline_key"]][1] += 1
            ok = res.outcome == exp["outcome"]
            passed += ok
            per[g["guideline_key"]][0] += ok
            if "reason" in exp:
                reason_total += 1
                reason_ok += res.reason_code == exp["reason"]
            if not ok:
                failures.append({"guideline": g["guideline_key"], "case": case["id"], "expected": exp["outcome"],
                                 "got": res.outcome})  # fmt: skip
    return {
        "cases": total, "passed": passed, "failures": failures, "determinism_differences": diffs,
        "determinism_runs_per_case": DETERMINISM_RUNS,
        "reason_codes_checked": reason_total, "reason_codes_matching": reason_ok,
        "per_guideline": {k: {"passed": v[0], "cases": v[1]} for k, v in sorted(per.items())},
        "rules_file": str(Path(path or DRAFT_PATH).name), "evaluation_date": on.isoformat(),
    }  # fmt: skip


def regression_metrics(reg: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "EL-02": {**rate(reg["passed"], reg["cases"]), "metric": "Mode R agreement (rule regression, authored cases)",
                  "failed_cases": reg["failures"][:50],
                  "per_guideline": {k: rate(v["passed"], v["cases"]) for k, v in reg["per_guideline"].items()}},
        "EL-02b": {**rate(reg["reason_codes_matching"], reg["reason_codes_checked"]), "metric": "Mode R reason-code agreement"},
        "EL-08": {"value": reg["determinism_differences"], "unit": "count", "status": "measured",
                  "metric": f"cases whose canonical result differs across {reg['determinism_runs_per_case']} repeats (must be 0)",
                  "n": reg["cases"]},  # fmt: skip
    }


# ----------------------------------------------------------------------------------------------- Mode E
# Screening intervals the planted completions are measured against (generator assumption A-E5, same values as the authored rules):
# up to date means the test is more recent than `interval` years before the evaluation date (window exclusive at the boundary).
INTERVAL_YEARS = {"colonoscopy": 10, "fit": 1, "mammography": 2, "pap_smear": 3, "hpv_test": 5}


def years_before(on: date, years: int) -> date:
    last = calendar.monthrange(on.year - years, on.month)[1]
    return date(on.year - years, on.month, min(on.day, last))


def status_on(item: Mapping[str, Any], on: date) -> str:
    """up_to_date / stale of a planted completion AS OF `on` (the engine's evaluation date), from its event date.

    Dates written only as a month or year in a note are NOT modelled (the exact planted date is used), a documented
    simplification: it can make the expected outcome more definite than what the note supports."""
    years, when = INTERVAL_YEARS.get(item["concept"]), item.get("event_date")
    if years is None or not when:
        return item["status"]  # no interval or no date: trust the status the generator planted
    return "up_to_date" if date.fromisoformat(str(when)[:10]) > years_before(on, years) else "stale"


def age_on(birth: date, on: date) -> int:
    return on.year - birth.year - ((on.month, on.day) < (birth.month, birth.day))


def expected_outcome(
    guideline_key: str, birth: date | None, sex: str | None, items: Sequence[Mapping[str, Any]], on: date
) -> tuple[str, str, str] | None:
    """(expected class, detail, basis) from the planted ground truth, or None when the oracle does not decide.

    Age/sex bands and the treatment of exclusions follow the authored cases of rules_draft.json for the three
    screening guidelines (documented per branch). Completion is `up_to_date` / `stale` as the generator planted it
    at `on`."""
    family, exclusion_concepts = ORACLE[guideline_key]
    if birth is None:
        return None
    age = age_on(birth, on)
    female = (sex or "").lower() == "female"
    screens = [i for i in items if i["family"] == family]
    if family == "cervical":  # an HPV test alone does not count before age 30 (rules_draft cervical t11/t21)
        screens = [i for i in screens if not (i["concept"] == "hpv_test" and age < 30)]
    excl = [i for i in items if i["family"] == "exclusion" and i["concept"] in exclusion_concepts]
    done = any(status_on(i, on) == "up_to_date" for i in screens)

    def out(cls: str, detail: str, basis: str) -> tuple[str, str, str]:
        return cls, detail, basis

    if family != "crc" and not female:
        return out("ineligible", "outside_source_population_sex", "demographic")
    if family == "crc":
        if age < 45 or age > 85:
            return out("ineligible", "age_outside_45_85", "demographic")
        if age > 75:  # 76-85 individualised decision; a planted completion makes the precedence unclear
            return out("indeterminate", "selective_screening_age", "demographic") if not (screens or excl) else None
    elif family == "breast":
        if age < 40:
            return out("ineligible", "below_minimum_age", "demographic")
        if age >= 75:
            return out("indeterminate", "age_75_plus", "demographic") if not (screens or excl) else None
    else:
        if age < 21:
            return out("ineligible", "below_age_21", "demographic")
    if excl:
        if family == "cervical":
            total = any(i["fact_type"] == "total_hysterectomy" for i in excl)
            return out("ineligible", "hysterectomy_cervix_removed", "planted_exclusion") if total else out(
                "indeterminate", "hysterectomy_cervix_status_unconfirmed", "planted_exclusion")
        return out("indeterminate", "exclusion_needs_clinical_review", "planted_exclusion")  # D-01: not auto-excluded
    if family == "cervical" and age > 65:
        return out("ineligible", "already_completed", "planted_completion") if done else out(
            "indeterminate", "age_over_65_adequacy_not_documented", "demographic")
    if done:
        return out("ineligible", "already_completed", "planted_completion")
    if screens:
        return out("eligible", "overdue", "planted_stale")
    return out("eligible", "no_planted_item", "no_planted_item")


def evaluate_mode_e(
    evaluations: Iterable[Mapping[str, Any]],
    residents: Mapping[str, Mapping[str, Any]],
    enrichment: Iterable[Mapping[str, Any]],
    *,
    as_of: date,
    matched_label_ids: set[str] | None = None,
) -> dict[str, Any]:
    by_res: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in enrichment:
        by_res[item["resident_id"]].append(item)
    matched = matched_label_ids or set()
    pairs: list[dict[str, Any]] = []
    skipped: Counter = Counter()
    dates: Counter = Counter()
    complete = incomplete = 0
    for ev in evaluations:
        key = ev["guideline_key"]
        res = residents.get(ev["resident_id"])
        crit = (ev.get("explanation") or {}).get("criteria") or []
        complete_expl = ev["explanation_complete"] if "explanation_complete" in ev else (bool(crit) and all(str(c.get("detail") or "").strip() for c in crit))
        incomplete += not complete_expl
        complete += complete_expl
        if key not in ORACLE:
            skipped["guideline_without_oracle"] += 1
            continue
        if res is None or res["birth_date"] is None:
            skipped["no_birth_date"] += 1
            continue
        dates[ev["evaluation_date"].isoformat()] += 1
        items = by_res.get(ev["resident_id"], [])
        exp = expected_outcome(key, res["birth_date"], res["sex"], items, ev["evaluation_date"])  # the oracle follows the engine's date
        if exp is None:
            skipped["oracle_does_not_decide"] += 1
            continue
        fam, _ = ORACLE[key]
        rel = [i for i in items if i["family"] in (fam, "exclusion")]
        mode = rel[0]["mode"] if rel else "none"
        note_labels = [i.get("document_label_id") for i in rel if i["mode"] == "note_only"]
        pairs.append({
            "key": key, "resident_id": ev["resident_id"], "expected": exp[0], "detail": exp[1], "basis": exp[2],
            "returned": ev["outcome_class"], "outcome": ev["outcome"], "reason": ev.get("reason_code"), "mode": mode,
            "language": res["language"] or "unknown", "district": res["district"] or "unknown",
            "note_extracted": bool(note_labels) and all(lbl in matched for lbl in note_labels if lbl),
        })  # fmt: skip

    def agree(ps: Sequence[dict[str, Any]]) -> dict[str, Any]:
        return rate(sum(p["expected"] == p["returned"] for p in ps), len(ps))

    def grouped(ps: Sequence[dict[str, Any]], dim: str) -> dict[str, Any]:
        g: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for p in ps:
            g[p[dim]].append(p)
        return {k: agree(v) for k, v in sorted(g.items())}

    scored = [p for p in pairs if p["basis"] != "no_planted_item"]
    conf = {e: {r: sum(1 for p in scored if p["expected"] == e and p["returned"] == r) for r in CLASSES} for e in CLASSES[:3]}

    def cond(ps, e, r_in=None, r_out=None):
        den = [p for p in ps if p["expected"] in e]
        return rate(sum(1 for p in den if (r_in is None or p["returned"] in r_in) and (r_out is None or p["returned"] not in r_out)), len(den))

    causes: Counter = Counter()
    for p in scored:
        if p["expected"] == p["returned"]:
            continue
        if p["basis"] == "planted_completion" and p["mode"] == "note_only":
            causes["note_only_completion_not_extracted" if not p["note_extracted"] else "note_only_completion_extracted_outcome_differs"] += 1
        elif p["mode"] == "structured":
            causes["structured_item_outcome_differs"] += 1
        else:
            causes[f"{p['basis']}_outcome_differs"] += 1
    return {
        "EL-01": {**agree(scored), "metric": "Mode E agreement (oracle-complete pairs: demographic or planted item)",
                  "by_guideline": grouped(scored, "key"), "by_basis": grouped(scored, "basis"),
                  "by_item_mode": grouped(scored, "mode"), "by_language": grouped(scored, "language"),
                  "by_district": grouped(scored, "district"), "unit": "ratio"},  # fmt: skip
        "EL-01b": {**agree(pairs), "metric": "Mode E agreement including residents with no planted item (oracle incomplete: Synthea-native screenings are not in the ground truth)",
                   "no_planted_item_pairs": rate(sum(p["expected"] == p["returned"] for p in pairs if p["basis"] == "no_planted_item"),
                                                  sum(1 for p in pairs if p["basis"] == "no_planted_item"))},  # fmt: skip
        "EL-03": {**cond(scored, ("eligible",), ("ineligible",)), "metric": "false-ineligible rate (missed care gap)"},
        "EL-04": {**cond(scored, ("ineligible",), ("eligible",)), "metric": "false-eligible rate (over-outreach)"},
        "EL-05a": {**cond(scored, ("indeterminate",), ("indeterminate",)), "metric": "indeterminate recall"},
        "EL-05b": {**cond(scored, ("indeterminate",), ("eligible", "ineligible")), "metric": "unsafe-determinate rate"},
        "EL-06": {**cond(scored, ("eligible", "ineligible"), ("indeterminate",)), "metric": "over-indeterminate rate (review burden)"},
        "EL-07": {"value": None, "status": "not_applicable", "metric": "criterion-level agreement",
                  "reason": "the ground truth carries no criterion-level expectations"},  # fmt: skip
        "EL-09": {**rate(complete, complete + incomplete), "metric": "explanation completeness (every criterion has a detail text)"},
        "EL-11": {"causes": dict(sorted(causes.items())), "disagreements": sum(causes.values()), "unit": "count",
                  "metric": "root-cause attribution of Mode E disagreements (coarse; no per-fact tracing)", "status": "measured", "value": sum(causes.values())},  # fmt: skip
        "confusion_expected_by_returned": conf,
        "scope": {"pairs_scored": len(scored), "pairs_no_planted_item": len(pairs) - len(scored), "evaluation_dates": dict(sorted(dates.items())),
                  "ground_truth_as_of": as_of.isoformat(),
                  "skipped": dict(sorted(skipped.items())), "evaluations_seen": complete + incomplete},  # fmt: skip
    }
