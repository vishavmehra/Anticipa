"""Gate semantics (T-11), ground-truth reader integrity (FI-18 / GT_*), test-split guard and isolation (ADR-008, INV-06)."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import builders as b
import pytest

from anticipa.evaluation import gates, gt
from anticipa.evaluation.matching import Label

PKG = Path(__file__).resolve().parents[2] / "anticipa"


def m(value, **kw):
    return {"value": value, "status": "measured", **kw}


def lang(**vals):
    return {"breakdowns": {"language": {k: m(v) for k, v in vals.items()}}}


def result(gl, gid):
    return next(g for g in gl if g["gate_id"] == gid)


def test_gate_pass_fail_warn_and_per_language_thresholds():
    metrics = {
        "EX-01.A": m(0.96, **lang(en=0.97, hi=0.95, ar=0.88)),  # overall ok, ar below 0.90 -> block fails
        "EX-10": m(0.80, **lang(en=0.8, hi=0.8, ar=0.8)),  # warn gate below threshold
        "EX-08a": m(0.98, **lang(en=0.98, hi=0.97, ar=0.96)),
        "EX-12": m(0.01, **lang(en=0.01, hi=0.01, ar=0.01)),
    }
    gl = gates.evaluate_gates(metrics, partial=False, checksum_verified=True)
    assert result(gl, "SG-02")["result"] == "fail" and result(gl, "SG-02")["threshold_status"] == "proposal"
    assert result(gl, "SG-07")["result"] == "warn"
    assert result(gl, "SG-04")["result"] == "pass" and result(gl, "SG-08a")["result"] == "pass"


def test_unmeasured_metrics_are_not_evaluated_never_pass():
    gl = gates.evaluate_gates({"EX-06": {"value": None, "status": "not_applicable"}}, partial=False, checksum_verified=True)
    assert result(gl, "SG-01")["result"] == "not_evaluated" and result(gl, "SG-16")["result"] == "not_evaluated"
    assert result(gl, "SG-16")["threshold"] is None  # cost budget undefined (Q-017)
    assert gates.summarise(gl).get("pass", 0) == 0 or all(g["result"] != "pass" for g in gl if g["metric_id"] == "EX-06")


def test_zero_count_over_an_empty_population_is_not_a_pass():
    gl = gates.evaluate_gates({"LC-01": {"value": 0, "status": "measured", "n": 0}, "LC-05": {"value": 0, "status": "measured", "n": 12}}, partial=False, checksum_verified=True)
    assert result(gl, "INV-03")["result"] == "not_evaluated" and result(gl, "INV-12")["result"] == "pass"


def test_missing_language_cell_prevents_a_clean_pass():
    gl = gates.evaluate_gates({"EX-09": m(0.99, **lang(en=0.99, hi=0.99))}, partial=False, checksum_verified=True)
    assert result(gl, "SG-06")["result"] == "not_evaluated"


def test_tier1_zero_tolerance_invariants():
    ok = {"CP-01": m(0), "CP-09": m(0), "SAF-05": m(0), "LC-01": m(0), "EL-02": m(1.0), "EL-08": m(0), "CP-04": m(1.0), "LC-02": m(1.0), "LC-05": m(0)}
    gl = gates.evaluate_gates(ok, partial=False, checksum_verified=True)
    assert all(g["result"] == "pass" for g in gl if g["tier"] == 1)
    bad = gates.evaluate_gates({**ok, "LC-01": m(2)}, partial=False, checksum_verified=True)
    assert result(bad, "INV-03")["result"] == "fail" and gates.exit_code(bad, partial=False) == 1


def test_partial_runs_never_satisfy_gates_and_exit_3():
    gl = gates.evaluate_gates({"CP-01": m(0)}, partial=True, checksum_verified=True)
    assert all(g["result"] == "skipped" for g in gl if g["metric_id"] != "ground_truth_checksums") and gates.exit_code(gl, partial=True) == 3


def test_release_profile_refuses_unevaluated_blockers():
    gl = gates.evaluate_gates({}, partial=False, checksum_verified=True)
    assert gates.exit_code(gl, partial=False, profile="dev") == 0 and gates.exit_code(gl, partial=False, profile="release") == 1


# ----------------------------------------------------------------------------------------------- ground-truth reader
def make_gt(tmp_path, split="dev"):
    lab = b.label(1, res="r1", doc="d1")
    sha = hashlib.sha256(b"doc").hexdigest()
    facts = [b.gt_fact(lab, "d1", sha, split=split), b.gt_fact(b.label(2, res="r2", doc="d2"), "d2", sha, split="test")]
    index = [{"document_id": "d1", "resident_id": "r1", "split": split, "content_sha256": sha, "language": "en"},
             {"document_id": "d2", "resident_id": "r2", "split": "test", "content_sha256": sha, "language": "en"}]  # fmt: skip
    b.write_gt(tmp_path, "syn-20261108-s42", facts, index)
    return tmp_path


def test_reader_loads_one_split_dedupes_and_round_trips_labels(tmp_path):
    root = make_gt(tmp_path)
    dev = gt.load_ground_truth("syn-20261108-s42", split="dev", root=root)
    assert [f["resident_id"] for f in dev.facts] == ["r1"] and set(dev.doc_index) == {"d1"}
    label = Label.from_gt(dev.facts[0])
    assert label.label_id == b.label(1).label_id and label.spans == b.label(1).spans and label.subject_type == "family_member"
    assert dev.checksum_verified and dev.ground_truth_id.startswith("syn-20261108-s42/test_v1@") and dev.tiers()["programmatic"] == 1
    both = gt.load_ground_truth("syn-20261108-s42", split="all", root=root)
    assert {f["resident_id"] for f in both.facts} == {"r1", "r2"}
    only = gt.load_ground_truth("syn-20261108-s42", resident_ids=["r2"], split="all", root=root)
    assert [f["resident_id"] for f in only.facts] == ["r2"]


def test_one_altered_byte_is_a_gt_integrity_error(tmp_path):
    root = make_gt(tmp_path)
    path = root / "syn-20261108-s42" / "facts" / "planted_facts.dev.jsonl"
    path.write_bytes(path.read_bytes().replace(b'"temporal": "historical"', b'"temporal": "historicaL"'))
    with pytest.raises(gt.GtIntegrityError):
        gt.load_ground_truth("syn-20261108-s42", root=root)
    assert gt.load_ground_truth("syn-20261108-s42", root=root, verify=False).checksum_verified is False  # explicit, recorded opt-out


def test_missing_or_wrong_version_fails_closed(tmp_path):
    root = make_gt(tmp_path)
    with pytest.raises(gt.GtIntegrityError):
        gt.load_ground_truth("syn-20990101-s1", root=root)
    manifest = root / "syn-20261108-s42" / "documents" / "docgen_manifest.json"
    manifest.write_text(json.dumps({**json.loads(manifest.read_text()), "dataset_version": "syn-other-s1"}), encoding="utf-8")
    with pytest.raises(gt.GtVersionMismatch):
        gt.load_ground_truth("syn-20261108-s42", root=root)


def test_test_split_access_is_logged_append_only(tmp_path):
    root = make_gt(tmp_path)
    g = gt.load_ground_truth("syn-20261108-s42", split="test", root=root)
    assert gt.prior_final_runs(root, "syn-20261108-s42") == 0
    gt.append_test_access(g, actor="tester", run_id="run-1", command="x")
    gt.append_test_access(g, actor="tester", run_id="run-2", command="x")
    assert gt.prior_final_runs(root, "syn-20261108-s42") == 2
    lines = (root / "syn-20261108-s42" / "test_access.jsonl").read_text().splitlines()
    assert json.loads(lines[0])["run_id"] == "run-1" and json.loads(lines[1])["ground_truth_id"] == g.ground_truth_id


# ----------------------------------------------------------------------------------------------- isolation (INV-06)
def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found |= {f"{node.module}.{a.name}" for a in node.names}
    return found


# One reported exception to ADR-008 / 09 §15.2: the owner approved dev split prompt comparisons by the AI workstream. The file is an
# operator tool (never imported by runtime code); the test below pins the property that makes it safe: it can only load the DEV split.
ALLOWED_OPERATOR_TOOLS = {"evidence/devcompare.py", "evidence\devcompare.py"}


def test_the_dev_compare_operator_tool_can_only_load_the_dev_split():
    path = PKG / "evidence" / "devcompare.py"
    if not path.exists():
        pytest.skip("no such tool")
    calls = [n for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", getattr(n.func, "id", "")) in ("load_ground_truth", "preflight")]  # fmt: skip
    assert calls, "expected the tool to load ground truth"
    for call in calls:
        split = next((k.value for k in call.keywords if k.arg == "split"), None)
        assert isinstance(split, ast.Constant) and split.value == "dev", f"line {call.lineno}: the tool must pass split='dev'"
    imported = _imports(path)
    assert not any(i.endswith(("append_test_access", "prior_final_runs")) for i in imported)  # never the test access ledger
    runtime = [p.name for p in (PKG / "evidence").glob("*.py") if p.name != "devcompare.py" and "devcompare" in p.read_text(encoding="utf-8")]
    assert runtime == [], runtime  # nothing in the runtime pipeline imports the tool


def test_no_pipeline_module_imports_the_evaluation_package():
    offenders = []
    for mod in ("ai", "evidence", "guidelines", "caregaps", "planning", "engagement", "closure", "datafabric", "analytics", "jobs"):
        for path in (PKG / mod).rglob("*.py"):
            if any(i == "anticipa.evaluation" or i.startswith("anticipa.evaluation.") for i in _imports(path)):
                offenders.append(str(path.relative_to(PKG)))
    offenders = [o for o in offenders if o not in ALLOWED_OPERATOR_TOOLS]
    assert offenders == []


def test_evaluator_never_imports_a_model_provider_or_the_router():
    """The evaluator makes no model calls (INV-05): it must not import the router, adapters or provider SDKs."""
    banned = ("anticipa.ai.router", "anticipa.ai.claude", "anticipa.ai.nebius", "anthropic", "openai", "httpx")
    offenders = []
    for path in (PKG / "evaluation").glob("*.py"):
        offenders += [f"{path.name}: {i}" for i in _imports(path) if i in banned or i.startswith(("anticipa.ai.router.", "anticipa.ai.claude.", "anticipa.ai.nebius."))]
    assert offenders == []


def test_only_the_gt_module_knows_the_label_file_layout():
    knows = [p.name for p in (PKG / "evaluation").glob("*.py") if "planted_facts" in p.read_text(encoding="utf-8")]
    assert knows == ["gt.py"], knows


def test_no_markdown_runtime_files_in_the_package():
    assert not list(PKG.rglob("*.md")) and not list((Path(__file__).parent).glob("*.md"))
