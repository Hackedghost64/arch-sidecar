import ast
import glob
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, "/home/divyam/Downloads/projects/arch-sidecar")
from arch_sidecar.decouple import prefactor_decouple

PREDICTIONS_PATH = "/home/divyam/Downloads/projects/arch-sidecar/benchmark/predictions_509f131.json"
RESULTS_PATH = "/home/divyam/Downloads/projects/arch-sidecar/benchmark/ground_truth_results.json"

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

def rollback(repo_path):
    subprocess.run("git restore . && git clean -fd", shell=True, cwd=repo_path, capture_output=True)

def run_cmd(cmd, cwd):
    t0 = time.perf_counter()
    res = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    dt_ms = (time.perf_counter() - t0) * 1000.0
    return res.returncode, dt_ms, res.stdout, res.stderr

def main():
    with open(PREDICTIONS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    predictions = data["predictions"]
    print(f"Loaded {len(predictions)} predictions to evaluate against ground-truth...")

    # Load existing progress if available
    results = []
    done_keys = set()
    if os.path.exists(RESULTS_PATH):
        try:
            with open(RESULTS_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
                results = saved.get("evaluations", [])
                for r in results:
                    done_keys.add((r["repo"], r["symbol"]))
                print(f"Resuming: {len(done_keys)} symbols already evaluated.")
        except Exception:
            results = []

    for idx, item in enumerate(predictions, 1):
        repo_name = item["repo"]
        sym = item["symbol"]
        from_file = item["from_file"]
        to_file = item["to_file"]
        pred_label = item["predicted_label"]

        if (repo_name, sym) in done_keys:
            continue

        cfg = REPO_CONFIGS.get(repo_name)
        if not cfg:
            continue
        repo_path = cfg["path"]

        print(f"[{idx}/{len(predictions)}] Evaluating {repo_name}::{sym} ({item['symbol_type']})...")

        # 1. Clean rollback first to ensure pristine state
        rollback(repo_path)

        # 2. Force apply the transformation
        t_apply_0 = time.perf_counter()
        try:
            apply_res = prefactor_decouple(
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
            apply_ms = (time.perf_counter() - t_apply_0) * 1000.0
            apply_ok = apply_res.get("success", False)
            apply_err = apply_res.get("error", "")
        except Exception as e:
            apply_ms = (time.perf_counter() - t_apply_0) * 1000.0
            apply_ok = False
            apply_err = str(e)

        failed_stage = None
        error_details = ""
        stg1_ms = 0.0
        stg2_ms = 0.0
        stg3_ms = 0.0

        if not apply_ok:
            ground_truth = "HAZARD"
            failed_stage = "STAGE_0_APPLY"
            error_details = f"Mover transformation failed: {apply_err}"
        else:
            # Stage 1: Import smoke test
            rc1, stg1_ms, out1, err1 = run_cmd(cfg["import_cmd"], repo_path)
            if rc1 != 0:
                ground_truth = "HAZARD"
                failed_stage = "STAGE_1_IMPORT"
                error_details = (err1.strip() or out1.strip())[:500]
            else:
                # Stage 2: Static linter for undefined names
                # Check pyflakes on both source and extracted file
                linter_cmd = f"pyflakes {from_file} {to_file}"
                rc2, stg2_ms, out2, err2 = run_cmd(linter_cmd, repo_path)
                linter_output = (out2 + "\n" + err2).strip()

                has_undefined = "undefined name" in linter_output
                has_syntax = "syntax error" in linter_output.lower() or "syntaxerror" in linter_output.lower()

                if has_undefined or has_syntax:
                    ground_truth = "HAZARD"
                    failed_stage = "STAGE_2_LINTER"
                    error_details = [line for line in linter_output.splitlines() if "undefined name" in line or "syntax" in line.lower()][:3]
                    error_details = " | ".join(error_details)
                else:
                    # Stage 3: Full native test suite
                    rc3, stg3_ms, out3, err3 = run_cmd(cfg["test_cmd"], repo_path)
                    if rc3 != 0:
                        # Rerun to check for flakiness
                        rc3_retry, _, out3_retry, _ = run_cmd(cfg["test_cmd"], repo_path)
                        if rc3_retry != 0:
                            ground_truth = "HAZARD"
                            failed_stage = "STAGE_3_TEST_SUITE"
                            # Find failed lines in pytest output
                            fail_lines = [l for l in out3_retry.splitlines() if l.startswith("FAILED ") or "failed" in l]
                            error_details = " | ".join(fail_lines[-5:]) if fail_lines else "Pytest failed"
                        else:
                            ground_truth = "SAFE"
                            failed_stage = None
                    else:
                        ground_truth = "SAFE"
                        failed_stage = None

        # Clean rollback immediately after test run
        rollback(repo_path)

        # Classification of prediction outcome
        # Devrith predicted ACCEPT or REJECT
        if pred_label == "REJECT" and ground_truth == "HAZARD":
            outcome = "TRUE_POSITIVE"
        elif pred_label == "ACCEPT" and ground_truth == "SAFE":
            outcome = "TRUE_NEGATIVE"
        elif pred_label == "REJECT" and ground_truth == "SAFE":
            outcome = "FALSE_POSITIVE" # False Rejection
        elif pred_label == "ACCEPT" and ground_truth == "HAZARD":
            outcome = "FALSE_NEGATIVE" # Missed Hazard / Accepted Regression
        else:
            outcome = "UNEXPECTED"

        total_control_ms = apply_ms + stg1_ms + stg2_ms + stg3_ms

        eval_record = {
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

        results.append(eval_record)
        done_keys.add((repo_name, sym))

        # Save progress incrementally
        with open(RESULTS_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "metadata": data["metadata"],
                "completed_count": len(results),
                "evaluations": results
            }, f, indent=2)

        print(f"  -> Pred: {pred_label} | GT: {ground_truth} | Outcome: {outcome} | FailedStage: {failed_stage} | ControlTime: {total_control_ms:.0f}ms")

    print(f"All {len(results)} evaluations complete! Results saved to {RESULTS_PATH}.")

if __name__ == '__main__':
    main()
