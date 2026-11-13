"""Report assembly helpers and the Markdown rendering (Docs/09 §12).

Callers: anticipa.evaluation.runner / cli. The Markdown is generated from the report JSON only: every number in it is
a value (rounded to 4 decimals for ratios) of that JSON, pass/fail wording comes from gate results, and every table
says what produced it. Instruction: deliver a working, tested, demonstrable Anticipa MVP including the evaluation
report.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from anticipa.config import ROOT_DIR

SCHEMA_VERSION = "anticipa.eval.report.v1"
BANNER = "Results are measured against planted ground truth on generated data. This is a software evaluation, not a clinical validation."
LANGS = ("en", "hi", "ar")

LIMITATIONS = (
    "Generated notes are cleaner and more regular than real clinical documentation, so these results do not transfer to real records and are not a clinical validation.",
    "The extraction prompt and the document templates come from the same project, and every ground truth label was produced by the generator and has not been reviewed by a person, so the labels themselves are unverified and the label error rate was not measured.",
    "Concept matching for the lenient and strict scores is a token based heuristic with a small synonym table, not a terminology service. Detection by location and category is reported separately and does not depend on it.",
    "The hallucination rate counts any accepted fact with no label at that location, so true but unlabelled statements inflate it. The adjudicated rate needs a person and was not measured.",
    "Hindi and Arabic text quality is that of the generator templates and no language competent reviewer has checked it. Small groups carry a low sample flag and wide intervals.",
    "Guideline rules are demonstration rules. Rule replay shows the engine implements the authored cases. The end to end comparison uses an expected outcome built from planted screenings and exclusions for three guidelines only, and residents with no planted item are scored separately because screenings recorded by the population generator are not part of the ground truth.",
    "The expected outcome for end to end eligibility does not model dates written only as a month or a year in a note, or a note that gives no date, so some disagreements for screenings documented only in notes reflect that simplification and not an engine error.",
    "Some temporal labels, for example medications listed without a date, come from the structured record and not from wording in the note, so the temporal accuracy and the historical as current rate mix extraction errors with label conventions.",
    "The ground truth files laboratory and vital sign results under observation while the extractor often files them as diagnostic finding. Detection that ignores category is reported separately so that a filing difference is not read as a missed statement.",
    "Capacity, outreach, appointments and completion evidence are simulated, so planner and closure figures describe pipeline behaviour on simulated data and say nothing about real clinics.",
    "Cost is estimated from configured prices and is not reconciled with a provider bill. Cache hits are logged with zero cost.",
    "All gate thresholds are proposals awaiting the owner's decision and were set before any measurement. A gate that could not be measured is reported as not evaluated and never as passed.",
    "Nothing technically prevents reading test labels on disk. The access ledger and the count of earlier final runs make misuse visible.",
)


def git_identity() -> dict[str, Any]:
    def run(*args: str) -> str:
        proc = subprocess.run(["git", *args], cwd=ROOT_DIR, capture_output=True, text=True, timeout=20, check=False)  # noqa: S603, S607
        return proc.stdout.strip()

    try:
        return {"git_commit": run("rev-parse", "HEAD") or None, "dirty": bool(run("status", "--porcelain")), "branch": run("rev-parse", "--abbrev-ref", "HEAD") or None}
    except Exception:  # noqa: BLE001 - identity is best effort
        return {"git_commit": None, "dirty": None, "branch": None}


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def digest(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------------------------- markdown
def f(x: Any) -> str:
    if x is None:
        return "n/a"
    if isinstance(x, bool):
        return "yes" if x else "no"
    if isinstance(x, float):
        return f"{x:.4f}"
    return str(x)


def table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    if not rows:
        return "_(no data)_\n"
    head = "| " + " | ".join(headers) + " |\n|" + "|".join("---" for _ in headers) + "|\n"
    return head + "\n".join("| " + " | ".join(f(c) for c in r) + " |" for r in rows) + "\n"


def _rate_cell(m: Mapping[str, Any] | None) -> str:
    if not m or m.get("value") is None:
        return "n/a"
    flag = " (low n)" if "low_n" in (m.get("flags") or []) else ""
    ci = m.get("ci95")
    return f"{f(m['value'])}{flag}" + (f" [{f(ci[0])}, {f(ci[1])}]" if ci else "")


def _by(m: Mapping[str, Any], dim: str) -> Mapping[str, Any]:
    return (m.get("breakdowns") or {}).get(dim, {})


def _dim_table(M: Mapping[str, Any], dim: str, keys: Sequence[str], ids: Sequence[tuple[str, str]]) -> str:
    rows = []
    for k in keys:
        rows.append([k, *[_rate_cell(_by(M[mid], dim).get(k)) if mid in M else "n/a" for mid, _ in ids],
                     (_by(M["EX-02.A"], dim).get(k) or {}).get("denominator")])  # fmt: skip
    return table([dim, *[label for _, label in ids], "labels (n)"], rows)


def render_markdown(r: Mapping[str, Any]) -> str:
    M, env = r["metrics"], r["environment"]
    out: list[str] = [f"# Anticipa evaluation report - {r['run_id']}\n", f"> {r['synthetic_banner']}\n"]
    out.append("## 1. Run identity\n")
    out.append(table(["field", "value"], [
        ["run id", r["run_id"]], ["status", r["status"]], ["run type", r["run_type"]], ["split", r["dataset"]["split"]],
        ["dataset version", r["dataset"]["dataset_version"]], ["ground truth id", r["ground_truth"]["ground_truth_id"]],
        ["cohort", r["dataset"].get("cohort_id") or "none (all residents with labels)"],
        ["code commit", r["code"].get("git_commit")], ["working tree dirty", r["code"].get("dirty")],
        ["environment", env["description"]], ["created at", r["created_at"]], ["figures are", "measured on the stored pipeline results (no model calls by the evaluator)"],
        ["prior final runs on this dataset", r["ground_truth"]["prior_final_runs"]],
    ]))  # fmt: skip
    out.append("## 2. Coverage: target vs actual\n")
    out.append(table(["tier", "target", "actual completed", "note"], [[c["tier"], c["target"], c["actual_completed"], c["note"]] for c in r["coverage"]]))
    out.append("## 3. Gate summary (thresholds are proposals pending Q-037)\n")
    out.append(table(["result", "count"], sorted(r["gate_summary"].items())))
    out.append(table(["gate", "tier", "metric", "rule", "observed", "result", "threshold status"], [
        [g["gate_id"], g["tier"], g["metric_id"], f"{g['comparator']} {f(g['threshold'])}" + (f" (per language {f(g.get('threshold_per_language'))})" if g.get("threshold_per_language") is not None else ""),
         g["observed"], g["result"], g["threshold_status"]] for g in r["gates"]]))  # fmt: skip

    out.append("## 4. Extraction quality (measured; AI evidence cohort; extraction model per section 10)\n")
    sc = r["scope"]["extraction"]
    out.append(table(["scope", "value"], [[k, v] for k, v in sc.items() if not isinstance(v, dict)] + [["facts in unscored categories", json.dumps(sc.get("facts_in_unscored_categories", {}))]]))
    pr = [("EX-01.A", "precision A"), ("EX-02.A", "recall A"), ("EX-03.A", "F1 A"), ("EX-02.T", "recall T"), ("EX-03.T", "F1 T"), ("EX-05.A", "detection recall A (L1)")]
    out.append("### By language (strict match; A = accepted facts, T = accepted + routed to review; value [95% interval])\n")
    out.append(_dim_table(M, "language", LANGS, pr))
    out.append("### By category\n")
    cats = sorted(_by(M["EX-02.A"], "category")) if "EX-02.A" in M else []
    out.append(_dim_table(M, "category", cats, pr))
    out.append("### Lenient match (concept only, view A) by language\n")
    out.append(_dim_table(M, "language", LANGS, [("EX-04.A.precision", "lenient precision"), ("EX-04.A.recall", "lenient recall"), ("EX-04.A.f1", "lenient F1")]))
    out.append("### Attribute accuracy on matched facts (view A), by language\n")
    acc = [("EX-08a", "subject"), ("EX-08b", "relationship"), ("EX-09", "assertion"), ("EX-10", "temporal"), ("EX-11a", "event date"), ("EX-11b", "age at onset")]
    out.append(_dim_table(M, "language", LANGS, acc))
    out.append("### Quote verification, review routing and rejection (candidate view C), by language\n")
    out.append(_dim_table(M, "language", LANGS, [("EX-06", "quote verified"), ("EX-07", "quote not found"), ("EX-12", "hallucinated (A)"), ("EX-12c", "unsupported (C)"), ("EX-14", "absent-slot false positive"), ("EX-18a", "review rate")]))
    out.append("### Difficult cases (DC-xx = planted scenario, view A)\n")
    sids = sorted(_by(M["EX-02.A"], "scenario_id")) if "EX-02.A" in M else []
    out.append(_dim_table(M, "scenario_id", sids, [("EX-02.A", "recall A"), ("EX-05.A", "detection A"), ("EX-08a", "subject acc"), ("EX-09", "assertion acc"), ("EX-10", "temporal acc")]))
    out.append("### Other extraction measurements\n")
    out.append(table(["metric", "value", "note"], [
        ["EX-15c conflict preservation", _rate_cell(M.get("EX-15c")), M["EX-15c"].get("note")], ["EX-16 duplicate predictions (view A)", M["EX-16.A"]["value"], "not scored as false positives"],
        ["EX-18b error interception", _rate_cell(M.get("EX-18b")), "erroneous candidate facts that were not accepted"], ["EX-18c review precision", _rate_cell(M.get("EX-18c")), ""],
        ["EX-13 adjudicated hallucination", "not measured", M["EX-13"]["reason"]], ["EX-17a terminology accuracy", "not applicable", M["EX-17a"]["reason"]],
        ["EX-11a unparsed predicted dates", M["EX-11a"].get("unparsed_predicted_dates"), "counted as wrong in EX-11a"],
    ]))  # fmt: skip

    out.append("## 5. Safety-critical extraction errors (accepted facts; lower is better)\n")
    rows = []
    for sid in ("SAF-01", "SAF-02", "SAF-03", "SAF-04", "SAF-05", "SAF-06", "SAF-07", "SAF-08", "SAF-09"):
        m = M.get(sid)
        if m:
            rows.append([sid, m.get("metric"), m.get("count"), m.get("denominator"), _rate_cell(m), *[_rate_cell(_by(m, "language").get(lg)) for lg in LANGS]])
    out.append(table(["id", "failure", "count", "denominator", "rate [95%]", "en", "hi", "ar"], rows))
    out.append(f"Case ids are listed per metric in `safety_cases` of the JSON report ({sum(v['count'] for v in r['safety_case_summary'].values())} instances in total); the Markdown shows counts only.\n")

    out.append("## 6. Eligibility\n")
    el = r["eligibility"]
    reg = el["mode_r"]
    out.append(f"### Mode R: authored regression cases through the engine ({reg['environment']})\n")
    out.append(table(["measure", "value"], [["cases", reg["cases"]], ["passed", reg["passed"]], ["EL-02 agreement", _rate_cell(M.get("EL-02"))], ["EL-02b reason-code agreement", _rate_cell(M.get("EL-02b"))],
                                            ["EL-08 non-deterministic cases", M["EL-08"]["value"]], ["failed cases", len(reg["failures"])]]))  # fmt: skip
    e = el["mode_e"]
    out.append("### Mode E: persisted evaluations vs the independent oracle from planted screenings/exclusions\n")
    out.append(table(["measure", "value"], [["scored pairs (oracle complete)", e["scope"]["pairs_scored"]], ["pairs with no planted item (oracle incomplete)", e["scope"]["pairs_no_planted_item"]],
                                            ["EL-01 agreement", _rate_cell(M.get("EL-01"))], ["EL-01b incl. no planted item", _rate_cell(M.get("EL-01b"))],
                                            ["EL-03 false-ineligible", _rate_cell(M.get("EL-03"))], ["EL-04 false-eligible", _rate_cell(M.get("EL-04"))],
                                            ["EL-05a indeterminate recall", _rate_cell(M.get("EL-05a"))], ["EL-05b unsafe determinate", _rate_cell(M.get("EL-05b"))],
                                            ["EL-06 over-indeterminate", _rate_cell(M.get("EL-06"))], ["EL-09 explanation completeness", _rate_cell(M.get("EL-09"))],
                                            ["skipped", json.dumps(e["scope"]["skipped"])]]))  # fmt: skip
    conf = e["confusion_expected_by_returned"]
    out.append("Confusion matrix (rows = expected, columns = returned):\n")
    out.append(table(["expected", "eligible", "ineligible", "indeterminate", "error"], [[k, *[v[c] for c in ("eligible", "ineligible", "indeterminate", "error")]] for k, v in conf.items()]))
    out.append(table(["guideline", "agreement"], [[k, _rate_cell(v)] for k, v in (M.get("EL-01", {}).get("by_guideline") or {}).items()]))
    out.append(table(["basis", "agreement"], [[k, _rate_cell(v)] for k, v in (M.get("EL-01", {}).get("by_basis") or {}).items()]))
    out.append(table(["item documented as", "agreement"], [[k, _rate_cell(v)] for k, v in (M.get("EL-01", {}).get("by_item_mode") or {}).items()]))
    out.append(table(["disagreement cause", "count"], sorted((M.get("EL-11", {}).get("causes") or {}).items())))

    out.append("## 7. Capacity planner (SIMULATED capacity)\n")
    pl = r["planning"]
    if pl.get("status") == "not_evaluated":
        out.append(f"Not evaluated: {pl['reason']}.\n")
    else:
        out.append(table(["measure", "value"], [["completed runs evaluated", pl["runs_evaluated"]], ["CP-01 hard-constraint violations", pl["CP-01"]["value"]], ["constraints checked", ", ".join(pl["CP-01"]["constraints_checked"])],
                                                ["declared but unchecked constraints", ", ".join(pl["CP-01"]["declared_constraints_without_validator"]) or "none"], ["CP-04 unallocated explained", _rate_cell(pl["CP-04"])],
                                                ["CP-03 allocation rate (latest)", _rate_cell(pl["CP-03"])], ["CP-02a slot utilisation (latest)", _rate_cell(pl["CP-02a"])], ["CP-02b time utilisation (latest)", _rate_cell(pl["CP-02b"])],
                                                ["CP-09 unvalidated plans", pl["CP-09"]["value"]]]))  # fmt: skip
        out.append(table(["violations by constraint", "count"], sorted(pl["CP-01"]["by_constraint"].items())))
        out.append("Not evaluated: " + "; ".join(f"{k} ({pl[k]['reason']})" for k in ("CP-05", "CP-06", "CP-08")) + ".\n")

    out.append("## 8. Equity (parity gaps are flagged for human interpretation, never labelled fair or unfair)\n")
    eq = r["equity"]
    out.append(table(["measure", "value"], [[k, v] for k, v in eq["summary"].items()]))
    for dim, groups in eq.get("allocation_by_group", {}).items():
        out.append(f"Allocation rate by {dim} (latest planning run, simulated):\n")
        out.append(table(["group", "rate [95%]", "candidate cases"], [[g, _rate_cell(v), v["denominator"]] for g, v in groups.items()]))
    out.append(table(["Mode E agreement by", "group", "agreement"], [[d, g, _rate_cell(v)] for d, k in (("language", "by_language"), ("district", "by_district")) for g, v in (M.get("EL-01", {}).get(k) or {}).items()]))

    out.append("## 9. Loop closure (SIMULATED)\n")
    lc = r["closure"]
    out.append(table(["measure", "value"], [["LC-01 completed without verified evidence (must be 0)", lc["LC-01"]["value"]], ["completed cases", lc["LC-01"]["n"]], ["LC-02 invalid-closure probe", _rate_cell(lc["LC-02"])],
                                            ["LC-03 status/history mismatches (must be 0)", lc["LC-03"]["value"]], ["LC-05 rows not labelled simulated (must be 0)", lc["LC-05"]["value"]],
                                            ["EQ-08 message language match", _rate_cell(lc["EQ-08"])], ["EQ-09 simulated completion rate", _rate_cell(lc["EQ-09"])]]))  # fmt: skip
    out.append(table(["funnel stage (simulated)", "cases"], sorted(lc["LC-04"]["funnel"].items())))

    out.append("## 10. Cost, throughput, latency, reliability (measured from model_invocations; estimated prices)\n")
    pf = r["performance"]
    out.append(table(["measure", "value", "unit"], [[k, pf[k]["value"], pf[k].get("unit")] for k in ("PF-01", "PF-02a", "PF-02b", "PF-03")] + [["PF-05b document failure rate", _rate_cell(pf["PF-05b"]), "ratio"], ["PF-05a retry rate", _rate_cell(pf["PF-05a"]), "ratio"],
                                                                                                                               ["PF-05c fallback rate", _rate_cell(pf["PF-05c"]), "ratio"], ["PF-06 cost recorded", _rate_cell(pf["PF-06"]), "ratio"], ["cache hits", pf["cache_hits"]["value"], "calls"]]))  # fmt: skip
    out.append(f"Cost basis: {pf['PF-01']['cost_basis']}. Total cost of the attributed calls: {f(pf['PF-01']['total_cost_usd'])} USD over {pf['PF-01']['calls']} calls.\n")
    out.append(table(["provider/model/task", "calls", "cost USD", "input tokens", "output tokens", "latency n", "p50 ms", "p95 ms"], [
        [k, v["calls"], v["cost_usd"], v["input_tokens"], v["output_tokens"], (pf["PF-04"]["by_provider_model_task"].get(k) or {}).get("n"), (pf["PF-04"]["by_provider_model_task"].get(k) or {}).get("p50_ms"), (pf["PF-04"]["by_provider_model_task"].get(k) or {}).get("p95_ms")]
        for k, v in pf["PF-01"]["by_provider_model_task"].items()]))  # fmt: skip

    out.append("## 11. Provider comparison\n")
    out.append("Not run. ADR-063/065 route extraction to a single Claude model with escalation, and forbid unnecessary model comparisons and Nebius fallback; retrieval (RET-*) and the embedding benchmark were not run (no retrieval ground truth exists; needs an approved cost estimate).\n")
    out.append("## 12. Projections\n")
    out.append("None. No figure in this report is a projection.\n" if not r["projections"] else table(["name", "formula", "value"], [[p["name"], p["formula"], p["value"]] for p in r["projections"]]))
    out.append("## 13. Exclusions and failures\n")
    out.append(table(["item", "count / reason"], [[e["item"], e["reason"]] for e in r["exclusions"]]))
    out.append(table(["failure", "count"], [["documents failed after retries", r["failures"]["documents_failed"]], *sorted(r["failures"]["by_error_category"].items())]))
    out.append("## 14. Limitations (read before quoting any number)\n")
    out.append("\n".join(f"{i}. {t}" for i, t in enumerate(r["limitations"], 1)) + "\n")
    out.append("## 15. Matching policy and label tiers\n")
    out.append(table(["label tier", "labels"], sorted(r["ground_truth"]["label_tiers"].items())))
    out.append("\n".join(f"- {t}" for t in r["configuration"]["matching_policy"]) + "\n")
    return "\n".join(out)


def write_outputs(report: Mapping[str, Any], out_dir: Path | None, docs_dir: Path | None) -> dict[str, str]:
    """Atomic writes (temp + replace): an interrupted write never leaves a partial report (FI-20)."""
    written: dict[str, str] = {}

    def atomic(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
        written[path.name] = str(path)

    js = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False, default=str)
    if out_dir:
        atomic(out_dir / "report.json", js)
        atomic(out_dir / "report.md", render_markdown(report))
    if docs_dir:
        tag = f"{str(report['created_at'])[:10]}_{report['dataset']['split']}"
        atomic(docs_dir / f"evaluation_report_{tag}.md", render_markdown(report))
        atomic(docs_dir / f"evaluation_report_{tag}.json", js)
    return written

