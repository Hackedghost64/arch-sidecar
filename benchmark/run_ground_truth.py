import ast
import glob
import json
import logging
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

BENCHMARK_DIR = os.path.abspath(os.path.dirname(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(BENCHMARK_DIR, ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from arch_sidecar.decouple import prefactor_decouple

PREDICTIONS_PATH = os.path.join(BENCHMARK_DIR, "predictions_509f131.json")
RESULTS_PATH = os.path.join(BENCHMARK_DIR, "ground_truth_results.json")

REPO_CONFIGS = {
    'flask': {
        'path': '/tmp/bench_repos/flask',
        'import_cmd': 'PYTHONPATH=/tmp/bench_repos/flask/src uv run python -c "import flask"',
        'test_cmd': 'PYTHONPATH=/tmp/bench_repos/flask/src uv run pytest',
        'baseline_fails': 0
    },
    'click': {
        'path': '/tmp/bench_repos/click',
        'import_cmd': 'PYTHONPATH=/tmp/bench_repos/click/src python3 -c "import click"',
        'test_cmd': 'PYTHONPATH=/tmp/bench_repos/click/src pytest',
        'baseline_fails': 0
    },
    'requests': {
        'path': '/tmp/bench_repos/requests',
        'import_cmd': 'PYTHONPATH=/tmp/bench_repos/requests/src python3 -c "import requests"',
        'test_cmd': 'PYTHONPATH=/tmp/bench_repos/requests/src pytest tests/test_structures.py tests/test_hooks.py tests/test_help.py tests/test_lowlevel.py',
        'baseline_fails': 0
    }
}


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
        done_keys = {(r["repo"], r["symbol"]) for r in results}
        print(f"Resuming: {len(done_keys)} symbols already evaluated.")
        return results, done_keys
    except (json.JSONDecodeError, OSError) as e:
        logger.debug("Failed loading existing results from %s: %s", results_path, e)
        return [], set()


def _apply_transformation(sym: str, from_file: str, to_file: str, repo_path: str) -> Tuple[bool, str, float]:
    t0 = time.perf_counter()
    try:
        res = prefactor_decouple(
            symbol=sym,
            from_file=from_file,
            to_file=to_file,
            project_root=repo_path,
            dry_run=False,
            allow_circular=True,
            allow_globals=True,
            allow_desync=True,
            add_backward_compat_alias=True
        )
        duration_ms = (time.perf_counter() - t0) * 1000.0
        return res.get("success", False), res.get("error", ""), duration_ms
    except Exception as e:
        logger.debug("prefactor_decouple crashed for %s: %s", sym, e)
        duration_ms = (time.perf_counter() - t0) * 1000.0
        return False, str(e), duration_ms


def _check_linter(from_file: str, to_file: str, repo_path: str) -> Tuple[bool, float, str]:
    linter_cmd = f"pyflakes {from_file} {to_file}"
    _, dt_ms, out, err = run_cmd(linter_cmd, repo_path)
    output = (out + "\n" + err).strip()
    has_undefined = "undefined name" in output
    has_syntax = "syntax error" in output.lower() or "syntaxerror" in output.lower()
    if has_undefined or has_syntax:
        details = [line for line in output.splitlines() if "undefined name" in line or "syntax" in line.lower()][:3]
        return False, dt_ms, " | ".join(details)
    return True, dt_ms, ""


def _check_test_suite(test_cmd: str, repo_path: str) -> Tuple[bool, float, str]:
    rc, dt_ms, _, _ = run_cmd(test_cmd, repo_path)
    if rc == 0:
        return True, dt_ms, ""
    rc_retry, _, out_retry, _ = run_cmd(test_cmd, repo_path)
    if rc_retry == 0:
        return True, dt_ms, ""
    fail_lines = [l for l in out_retry.splitlines() if l.startswith("FAILED ") or "failed" in l]
    details = " | ".join(fail_lines[-5:]) if fail_lines else "Pytest failed"
    return False, dt_ms, details


def _evaluate_stages(cfg: Dict[str, Any], from_file: str, to_file: str, repo_path: str) -> Tuple[str, Optional[str], str, float, float, float]:
    rc1, stg1_ms, out1, err1 = run_cmd(cfg["import_cmd"], repo_path)
    if rc1 != 0:
        return "HAZARD", "STAGE_1_IMPORT", (err1.strip() or out1.strip())[:500], stg1_ms, 0.0, 0.0

    lint_ok, stg2_ms, lint_err = _check_linter(from_file, to_file, repo_path)
    if not lint_ok:
        return "HAZARD", "STAGE_2_LINTER", lint_err, stg1_ms, stg2_ms, 0.0

    test_ok, stg3_ms, test_err = _check_test_suite(cfg["test_cmd"], repo_path)
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


def _evaluate_prediction_item(item: Dict[str, Any], idx: int, total: int) -> Optional[Dict[str, Any]]:
    repo_name = item["repo"]
    sym = item["symbol"]
    from_file = item["from_file"]
    to_file = item["to_file"]
    pred_label = item["predicted_label"]

    cfg = REPO_CONFIGS.get(repo_name)
    if not cfg:
        return None
    repo_path = cfg["path"]

    print(f"[{idx}/{total}] Evaluating {repo_name}::{sym} ({item['symbol_type']})...")
    rollback(repo_path)

    apply_ok, apply_err, apply_ms = _apply_transformation(sym, from_file, to_file, repo_path)

    if not apply_ok:
        ground_truth = "HAZARD"
        failed_stage = "STAGE_0_APPLY"
        error_details = f"Mover transformation failed: {apply_err}"
        stg1_ms, stg2_ms, stg3_ms = 0.0, 0.0, 0.0
    else:
        ground_truth, failed_stage, error_details, stg1_ms, stg2_ms, stg3_ms = _evaluate_stages(cfg, from_file, to_file, repo_path)

    rollback(repo_path)

    outcome = _classify_outcome(pred_label, ground_truth)
    total_control_ms = apply_ms + stg1_ms + stg2_ms + stg3_ms

    print(f"  -> Pred: {pred_label} | GT: {ground_truth} | Outcome: {outcome} | FailedStage: {failed_stage} | ControlTime: {total_control_ms:.0f}ms")

    return {
        "repo": repo_name,
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


def main():
    with open(PREDICTIONS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    predictions = data["predictions"]
    print(f"Loaded {len(predictions)} predictions to evaluate against ground-truth...")

    results, done_keys = _load_existing_results(RESULTS_PATH)

    for idx, item in enumerate(predictions, 1):
        if (item["repo"], item["symbol"]) in done_keys:
            continue
        record = _evaluate_prediction_item(item, idx, len(predictions))
        if not record:
            continue
        results.append(record)
        done_keys.add((item["repo"], item["symbol"]))

        with open(RESULTS_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "metadata": data["metadata"],
                "completed_count": len(results),
                "evaluations": results
            }, f, indent=2)

    print(f"All {len(results)} evaluations complete! Results saved to {RESULTS_PATH}.")


if __name__ == '__main__':
    main()
