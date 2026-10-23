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

if __name__ == '__main__':
    generate_predictions()
