import ast
import glob
import json
import logging
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

BENCHMARK_DIR = os.path.abspath(os.path.dirname(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(BENCHMARK_DIR, ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from arch_sidecar.decouple import prefactor_decouple

PREDICTIONS_PATH = os.path.join(BENCHMARK_DIR, "predictions_werkzeug_afd4136.json")
RESULTS_PATH = os.path.join(BENCHMARK_DIR, "ground_truth_werkzeug.json")
REPO_PATH = "/tmp/bench_repos/werkzeug"


def _extract_assign_constants(node: ast.AST, rel: str) -> List[Tuple[str, str, str]]:
    if not isinstance(node, ast.Assign):
        return []
    constants: List[Tuple[str, str, str]] = []
    for t in node.targets:
        target_id = getattr(t, "id", "")
        if isinstance(t, ast.Name) and not target_id.startswith("_") and target_id.isupper():
            constants.append((rel, target_id, "Constant"))
    return constants


def _classify_definition_node(node: ast.AST, rel: str) -> Optional[Tuple[str, str, str]]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
        return (rel, node.name, "Function")
    if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
        return (rel, node.name, "Class")
    return None


def _extract_module_symbols(p: str, repo_path: str) -> Dict[str, List[Tuple[str, str, str]]]:
    rel = os.path.relpath(p, repo_path)
    try:
        with open(p, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except (SyntaxError, OSError) as e:
        logger.debug("Failed parsing %s: %s", p, e)
        return {"Class": [], "Function": [], "Constant": []}

    types: Dict[str, List[Tuple[str, str, str]]] = {"Class": [], "Function": [], "Constant": []}
    for node in tree.body:
        item = _classify_definition_node(node, rel)
        if item:
            types[item[2]].append(item)
        else:
            types["Constant"].extend(_extract_assign_constants(node, rel))
    return types


def get_balanced_candidates() -> List[Tuple[str, str, str]]:
    prefix = "src/werkzeug"
    by_mod_type: Dict[str, Dict[str, List[Tuple[str, str, str]]]] = defaultdict(lambda: defaultdict(list))

    pattern = os.path.join(REPO_PATH, prefix, "**/*.py")
    for p in sorted(glob.glob(pattern, recursive=True)):
        if "__pycache__" in p or "test" in p:
            continue
        rel = os.path.relpath(p, REPO_PATH)
        syms = _extract_module_symbols(p, REPO_PATH)
        for category, items in syms.items():
            by_mod_type[rel][category].extend(items)

    selected: List[Tuple[str, str, str]] = []
    for _, types in sorted(by_mod_type.items()):
        selected.extend(types["Class"][:1])
        selected.extend(types["Function"][:1])
        selected.extend(types["Constant"][:1])
    return selected


def _collect_hazards(res: Dict[str, Any]) -> List[Any]:
    hazards: List[Any] = []
    for h_key in ["circular_hazards", "global_state_hazards", "state_desync_hazards", "framework_hazards"]:
        hazards.extend(res.get(h_key, []) or [])
    return hazards


def _classify_prediction(res: Dict[str, Any], hazards: List[Any]) -> Tuple[str, str]:
    err_str = res.get("error", "")
    if "Refactoring blocked" in err_str or len(hazards) > 0:
        reason = err_str if err_str else f"Detected {len(hazards)} architectural hazard(s)"
        return "REJECT", reason
    if res.get("success") is True:
        return "ACCEPT", "Safe DAG extraction closure verified"
    return "ERROR", err_str or "Unknown preflight error"


def _predict_single_symbol(rel_file: str, symbol_name: str, stype: str) -> Dict[str, Any]:
    dir_name = os.path.dirname(rel_file)
    target_file = os.path.join(dir_name, f"_extracted_{symbol_name.lower()}.py")

    t0 = time.perf_counter()
    try:
        res = prefactor_decouple(
            symbol=symbol_name,
            from_file=rel_file,
            to_file=target_file,
            project_root=REPO_PATH,
            dry_run=True,
            add_backward_compat_alias=True
        )
        dt_ms = (time.perf_counter() - t0) * 1000.0
        hazards = _collect_hazards(res)
        pred, reason = _classify_prediction(res, hazards)
    except Exception as e:
        logger.debug("prefactor_decouple prediction failed for %s: %s", symbol_name, e)
        dt_ms = (time.perf_counter() - t0) * 1000.0
        pred = "ERROR"
        hazards = []
        reason = str(e)

    return {
        "repo": "werkzeug",
        "from_file": rel_file,
        "to_file": target_file,
        "symbol": symbol_name,
        "symbol_type": stype,
        "predicted_label": pred,
        "prediction_reason": reason,
        "hazards_count": len(hazards),
        "hazards": hazards,
        "preflight_duration_ms": round(dt_ms, 2)
    }


def generate_predictions() -> None:
    candidates = get_balanced_candidates()
    print(f"Generating predictions for {len(candidates)} balanced symbols in Werkzeug...")

    manifest = {
        "metadata": {
            "freeze_commit": "afd4136",
            "repo": "pallets/werkzeug",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pre_registered_thresholds": {
                "recall_lower_bound_target": 0.70,
                "false_rejection_target": 0.10,
                "max_accepted_regressions": 0,
                "minimum_hazard_gate": 10
            }
        },
        "predictions": [_predict_single_symbol(rel_file, symbol_name, stype) for rel_file, symbol_name, stype in candidates]
    }

    with open(PREDICTIONS_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"Predictions written to {PREDICTIONS_PATH}")


def rollback(repo_path: str) -> None:
    subprocess.run("git restore . && git clean -fd", shell=True, cwd=repo_path, capture_output=True)


def run_cmd(cmd: str, cwd: str) -> Tuple[int, float, str, str]:
    t0 = time.perf_counter()
    res = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    dt_ms = (time.perf_counter() - t0) * 1000.0
    return res.returncode, dt_ms, res.stdout, res.stderr


def _load_existing_results(results_path: str) -> Tuple[List[Dict[str, Any]], Set[Tuple[str, str]]]:
    if not os.path.exists(results_path):
        return [], set()
    try:
        with open(results_path, "r", encoding="utf-8") as f:
            saved = json.load(f)
        results = saved.get("evaluations", [])
        done_keys = {(r["from_file"], r["symbol"]) for r in results}
        print(f"Resuming: {len(done_keys)} already evaluated.")
        return results, done_keys
    except (json.JSONDecodeError, OSError) as e:
        logger.debug("Failed loading existing results from %s: %s", results_path, e)
        return [], set()


def _cache_baseline_pyflakes(predictions: List[Dict[str, Any]]) -> Dict[str, Set[str]]:
    baseline_pyflakes: Dict[str, Set[str]] = {}
    for item in predictions:
        from_f = item["from_file"]
        if from_f not in baseline_pyflakes:
            _, _, out, err = run_cmd(f"pyflakes {from_f}", REPO_PATH)
            baseline_pyflakes[from_f] = set((out + "\n" + err).strip().splitlines())
    return baseline_pyflakes


def _apply_transformation(sym: str, from_file: str, to_file: str) -> Tuple[bool, str, float]:
    t_apply_0 = time.perf_counter()
    try:
        apply_res = prefactor_decouple(
            symbol=sym,
            from_file=from_file,
            to_file=to_file,
            project_root=REPO_PATH,
            dry_run=False,
            allow_circular=True,
            allow_globals=True,
            allow_desync=True,
            add_backward_compat_alias=True
        )
        duration_ms = (time.perf_counter() - t_apply_0) * 1000.0
        return apply_res.get("success", False), apply_res.get("error", ""), duration_ms
    except Exception as e:
        logger.debug("prefactor_decouple crashed for %s: %s", sym, e)
        duration_ms = (time.perf_counter() - t_apply_0) * 1000.0
        return False, str(e), duration_ms


def _check_werkzeug_import(from_file: str) -> Tuple[bool, float, str]:
    mod_name = from_file.replace("src/", "").replace(".py", "").replace("/", ".")
    if mod_name.endswith(".__init__"):
        mod_name = mod_name[:-9]
    cmd = f'PYTHONPATH=/tmp/bench_repos/werkzeug/src uv run python -c "import werkzeug; import {mod_name}"'
    rc1, stg1_ms, out1, err1 = run_cmd(cmd, REPO_PATH)
    if rc1 != 0:
        return False, stg1_ms, (err1.strip() or out1.strip())[:500]
    return True, stg1_ms, ""


def _check_werkzeug_linter(from_file: str, to_file: str, baseline_lines: Set[str]) -> Tuple[bool, float, str]:
    rc2, stg2_ms, out2, err2 = run_cmd(f"pyflakes {from_file} {to_file}", REPO_PATH)
    current_lines = set((out2 + "\n" + err2).strip().splitlines())
    new_lines = current_lines - baseline_lines
    new_undefined = [l for l in new_lines if "undefined name" in l or "syntax error" in l.lower() or "syntaxerror" in l.lower()]
    if new_undefined:
        return False, stg2_ms, " | ".join(new_undefined[:3])
    return True, stg2_ms, ""


def _check_werkzeug_tests() -> Tuple[bool, float, str]:
    cmd = 'PYTHONPATH=/tmp/bench_repos/werkzeug/src uv run pytest'
    rc3, stg3_ms, _, _ = run_cmd(cmd, REPO_PATH)
    if rc3 == 0:
        return True, stg3_ms, ""
    rc3_retry, _, out3_retry, _ = run_cmd(cmd, REPO_PATH)
    if rc3_retry == 0:
        return True, stg3_ms, ""
    fail_lines = [l for l in out3_retry.splitlines() if l.startswith("FAILED ") or "failed" in l]
    details = " | ".join(fail_lines[-5:]) if fail_lines else "Pytest failed"
    return False, stg3_ms, details


def _evaluate_werkzeug_stages(from_file: str, to_file: str, baseline_lines: Set[str]) -> Tuple[str, Optional[str], str, float, float, float]:
    import_ok, stg1_ms, import_err = _check_werkzeug_import(from_file)
    if not import_ok:
        return "HAZARD", "STAGE_1_IMPORT", import_err, stg1_ms, 0.0, 0.0

    lint_ok, stg2_ms, lint_err = _check_werkzeug_linter(from_file, to_file, baseline_lines)
    if not lint_ok:
        return "HAZARD", "STAGE_2_LINTER", lint_err, stg1_ms, stg2_ms, 0.0

    test_ok, stg3_ms, test_err = _check_werkzeug_tests()
    if not test_ok:
        return "HAZARD", "STAGE_3_TEST_SUITE", test_err, stg1_ms, stg2_ms, stg3_ms

    return "SAFE", None, "", stg1_ms, stg2_ms, stg3_ms


def _classify_outcome(pred_label: str, ground_truth: str) -> str:
    mapping = {
        ("REJECT", "HAZARD"): "TRUE_POSITIVE",
        ("ACCEPT", "SAFE"): "TRUE_NEGATIVE",
        ("REJECT", "SAFE"): "FALSE_POSITIVE",
        ("ACCEPT", "HAZARD"): "FALSE_NEGATIVE",
    }
    return mapping.get((pred_label, ground_truth), "UNEXPECTED")


def _evaluate_werkzeug_item(item: Dict[str, Any], baseline_pyflakes: Dict[str, Set[str]], idx: int, total: int) -> Dict[str, Any]:
    sym = item["symbol"]
    from_file = item["from_file"]
    to_file = item["to_file"]
    pred_label = item["predicted_label"]

    print(f"[{idx}/{total}] Evaluating werkzeug::{sym} ({item['symbol_type']}) from {from_file}...")
    rollback(REPO_PATH)

    apply_ok, apply_err, apply_ms = _apply_transformation(sym, from_file, to_file)

    if not apply_ok:
        ground_truth = "HAZARD"
        failed_stage = "STAGE_0_APPLY"
        error_details = f"Mover transformation failed: {apply_err}"
        stg1_ms, stg2_ms, stg3_ms = 0.0, 0.0, 0.0
    else:
        baseline_lines = baseline_pyflakes.get(from_file, set())
        ground_truth, failed_stage, error_details, stg1_ms, stg2_ms, stg3_ms = _evaluate_werkzeug_stages(from_file, to_file, baseline_lines)

    rollback(REPO_PATH)

    outcome = _classify_outcome(pred_label, ground_truth)
    total_control_ms = apply_ms + stg1_ms + stg2_ms + stg3_ms

    print(f"  -> Pred: {pred_label} | GT: {ground_truth} | Outcome: {outcome} | FailedStage: {failed_stage} | ControlTime: {total_control_ms:.0f}ms")

    return {
        "repo": "werkzeug",
        "symbol": sym,
        "symbol_type": item["symbol_type"],
        "from_file": from_file,
        "to_file": to_file,
        "predicted_label": pred_label,
        "ground_truth": ground_truth,
        "outcome": outcome,
        "failed_stage": failed_stage,
        "error_details": error_details,
        "preflight_duration_ms": item["preflight_duration_ms"],
        "control_durations_ms": {
            "apply": round(apply_ms, 1),
            "stage1_import": round(stg1_ms, 1),
            "stage2_linter": round(stg2_ms, 1),
            "stage3_tests": round(stg3_ms, 1),
            "total_control": round(total_control_ms, 1)
        }
    }


def run_ground_truth() -> None:
    with open(PREDICTIONS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    predictions = data["predictions"]
    print(f"Starting ground truth evaluation for {len(predictions)} Werkzeug predictions...")

    results, done_keys = _load_existing_results(RESULTS_PATH)
    baseline_pyflakes = _cache_baseline_pyflakes(predictions)

    for idx, item in enumerate(predictions, 1):
        if (item["from_file"], item["symbol"]) in done_keys:
            continue

        eval_record = _evaluate_werkzeug_item(item, baseline_pyflakes, idx, len(predictions))
        results.append(eval_record)
        done_keys.add((item["from_file"], item["symbol"]))

        with open(RESULTS_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "metadata": data["metadata"],
                "completed_count": len(results),
                "evaluations": results
            }, f, indent=2)

    print(f"All {len(results)} Werkzeug evaluations complete! Saved to {RESULTS_PATH}.")


if __name__ == '__main__':
    run_ground_truth()
