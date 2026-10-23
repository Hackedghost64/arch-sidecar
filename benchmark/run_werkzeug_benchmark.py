import ast
import glob
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from collections import defaultdict

sys.path.insert(0, "/home/divyam/Downloads/projects/arch-sidecar")
from arch_sidecar.decouple import prefactor_decouple

PREDICTIONS_PATH = "/home/divyam/Downloads/projects/arch-sidecar/benchmark/predictions_werkzeug_afd4136.json"
RESULTS_PATH = "/home/divyam/Downloads/projects/arch-sidecar/benchmark/ground_truth_werkzeug.json"
REPO_PATH = "/tmp/bench_repos/werkzeug"

def get_balanced_candidates():
    prefix = "src/werkzeug"
    by_mod_type = defaultdict(lambda: defaultdict(list))

    for p in sorted(glob.glob(os.path.join(REPO_PATH, prefix, "**/*.py"), recursive=True)):
        if "__pycache__" in p or "test" in p:
            continue
        rel = os.path.relpath(p, REPO_PATH)
        try:
            with open(p, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read())
        except Exception:
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not node.name.startswith("_"):
                    by_mod_type[rel]["Function"].append((rel, node.name, "Function"))
            elif isinstance(node, ast.ClassDef):
                if not node.name.startswith("_"):
                    by_mod_type[rel]["Class"].append((rel, node.name, "Class"))
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and not t.id.startswith("_") and t.id.isupper():
                        by_mod_type[rel]["Constant"].append((rel, t.id, "Constant"))

    selected = []
    for rel, types in sorted(by_mod_type.items()):
        selected.extend(types["Class"][:1])
        selected.extend(types["Function"][:1])
        selected.extend(types["Constant"][:1])
    return selected

def generate_predictions():
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
        "predictions": []
    }

    for rel_file, symbol_name, stype in candidates:
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

            hazards = []
            for h_key in ["circular_hazards", "global_state_hazards", "state_desync_hazards", "framework_hazards"]:
                for h in res.get(h_key, []):
                    hazards.append(h)

            err_str = res.get("error", "")
            if "Refactoring blocked" in err_str or len(hazards) > 0:
                pred = "REJECT"
                reason = err_str if err_str else f"Detected {len(hazards)} architectural hazard(s)"
            elif res.get("success") is True:
                pred = "ACCEPT"
                reason = "Safe DAG extraction closure verified"
            else:
                pred = "ERROR"
                reason = err_str or "Unknown preflight error"

        except Exception as e:
            dt_ms = (time.perf_counter() - t0) * 1000.0
            pred = "ERROR"
            hazards = []
            reason = str(e)

        manifest["predictions"].append({
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
        })

    with open(PREDICTIONS_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"Predictions written to {PREDICTIONS_PATH}")

def rollback(repo_path):
    subprocess.run("git restore . && git clean -fd", shell=True, cwd=repo_path, capture_output=True)

def run_cmd(cmd, cwd):
    t0 = time.perf_counter()
    res = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    dt_ms = (time.perf_counter() - t0) * 1000.0
    return res.returncode, dt_ms, res.stdout, res.stderr

def run_ground_truth():
    with open(PREDICTIONS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    predictions = data["predictions"]
    print(f"Starting ground truth evaluation for {len(predictions)} Werkzeug predictions...")

    results = []
    done_keys = set()
    if os.path.exists(RESULTS_PATH):
        try:
            with open(RESULTS_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
                results = saved.get("evaluations", [])
                for r in results:
                    done_keys.add((r["from_file"], r["symbol"]))
                print(f"Resuming: {len(done_keys)} already evaluated.")
        except Exception:
            results = []

    # Cache clean baseline pyflakes per file
    baseline_pyflakes = {}
    for item in predictions:
        from_f = item["from_file"]
        if from_f not in baseline_pyflakes:
            rc, _, out, err = run_cmd(f"pyflakes {from_f}", REPO_PATH)
            baseline_pyflakes[from_f] = set((out + "\n" + err).strip().splitlines())

    for idx, item in enumerate(predictions, 1):
        sym = item["symbol"]
        from_file = item["from_file"]
        to_file = item["to_file"]
        pred_label = item["predicted_label"]

        if (from_file, sym) in done_keys:
            continue

        print(f"[{idx}/{len(predictions)}] Evaluating werkzeug::{sym} ({item['symbol_type']}) from {from_file}...")

        rollback(REPO_PATH)

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
            # Stage 1: Import smoke test (package + modified module)
            mod_name = from_file.replace("src/", "").replace(".py", "").replace("/", ".")
            if mod_name.endswith(".__init__"):
                mod_name = mod_name[:-9]
            rc1, stg1_ms, out1, err1 = run_cmd(f'PYTHONPATH=/tmp/bench_repos/werkzeug/src uv run python -c "import werkzeug; import {mod_name}"', REPO_PATH)
            if rc1 != 0:
                ground_truth = "HAZARD"
                failed_stage = "STAGE_1_IMPORT"
                error_details = (err1.strip() or out1.strip())[:500]
            else:
                # Stage 2: Static linter for NEW undefined names relative to clean checkout
                rc2, stg2_ms, out2, err2 = run_cmd(f"pyflakes {from_file} {to_file}", REPO_PATH)
                current_lines = set((out2 + "\n" + err2).strip().splitlines())
                base_lines = baseline_pyflakes.get(from_file, set())
                new_lines = current_lines - base_lines

                new_undefined = [l for l in new_lines if "undefined name" in l or "syntax error" in l.lower() or "syntaxerror" in l.lower()]

                if new_undefined:
                    ground_truth = "HAZARD"
                    failed_stage = "STAGE_2_LINTER"
                    error_details = " | ".join(new_undefined[:3])
                else:
                    # Stage 3: Full native test suite
                    rc3, stg3_ms, out3, err3 = run_cmd('PYTHONPATH=/tmp/bench_repos/werkzeug/src uv run pytest', REPO_PATH)
                    if rc3 != 0:
                        # Rerun to rule out flakiness
                        rc3_retry, _, out3_retry, _ = run_cmd('PYTHONPATH=/tmp/bench_repos/werkzeug/src uv run pytest', REPO_PATH)
                        if rc3_retry != 0:
                            ground_truth = "HAZARD"
                            failed_stage = "STAGE_3_TEST_SUITE"
                            fail_lines = [l for l in out3_retry.splitlines() if l.startswith("FAILED ") or "failed" in l]
                            error_details = " | ".join(fail_lines[-5:]) if fail_lines else "Pytest failed"
                        else:
                            ground_truth = "SAFE"
                            failed_stage = None
                    else:
                        ground_truth = "SAFE"
                        failed_stage = None

        rollback(REPO_PATH)

        if pred_label == "REJECT" and ground_truth == "HAZARD":
            outcome = "TRUE_POSITIVE"
        elif pred_label == "ACCEPT" and ground_truth == "SAFE":
            outcome = "TRUE_NEGATIVE"
        elif pred_label == "REJECT" and ground_truth == "SAFE":
            outcome = "FALSE_POSITIVE"
        elif pred_label == "ACCEPT" and ground_truth == "HAZARD":
            outcome = "FALSE_NEGATIVE"
        else:
            outcome = "UNEXPECTED"

        total_control_ms = apply_ms + stg1_ms + stg2_ms + stg3_ms

        eval_record = {
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

        results.append(eval_record)
        done_keys.add((from_file, sym))

        with open(RESULTS_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "metadata": data["metadata"],
                "completed_count": len(results),
                "evaluations": results
            }, f, indent=2)

        print(f"  -> Pred: {pred_label} | GT: {ground_truth} | Outcome: {outcome} | FailedStage: {failed_stage} | ControlTime: {total_control_ms:.0f}ms")

    print(f"All {len(results)} Werkzeug evaluations complete! Saved to {RESULTS_PATH}.")

if __name__ == '__main__':
    run_ground_truth()
