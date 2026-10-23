import ast
import glob
import json
import os
import sys
import time
from datetime import datetime, timezone

# Add arch-sidecar to sys.path
sys.path.insert(0, "/home/divyam/Downloads/projects/arch-sidecar")
from arch_sidecar.decouple import prefactor_decouple

SEEN_SYMBOLS = {
    'click': {'echo', 'Context', 'ShellComplete', '_split_opt', 'get_current_context', 'isatty', 'wrap_text'},
    'requests': {'Session', 'SessionRedirectMixin', '_basic_auth_str', 'merge_setting', 'super_len', 'to_key_val_list', 'where'},
    'django': {'AdminSite', 'QuerySet', 'RANDOM_STRING_CHARS', 'RegexValidator', 'ReverseManyToOneDescriptor', 'capfirst', 'get_random_string', 'slugify'}
}

def extract_candidates(repo_path, prefix, repo_name, max_count=None):
    candidates = []
    seen = SEEN_SYMBOLS.get(repo_name, set())
    for p in sorted(glob.glob(os.path.join(repo_path, prefix, '**/*.py'), recursive=True)):
        if '__pycache__' in p or 'test' in p:
            continue
        rel = os.path.relpath(p, repo_path)
        try:
            with open(p, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read())
        except Exception:
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if not node.name.startswith('_') and node.name not in seen:
                    stype = 'Class' if isinstance(node, ast.ClassDef) else 'Function'
                    candidates.append((rel, node.name, stype))
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and not t.id.startswith('_') and t.id.isupper() and t.id not in seen:
                        candidates.append((rel, t.id, 'Constant'))
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name) and not node.target.id.startswith('_') and node.target.id.isupper() and node.target.id not in seen:
                    candidates.append((rel, node.target.id, 'Constant'))

    if max_count and len(candidates) > max_count:
        step = len(candidates) / max_count
        candidates = [candidates[int(i * step)] for i in range(max_count)]
    return candidates

def main():
    repos = {
        'flask': ('/tmp/bench_repos/flask', 'src/flask', None),
        'click': ('/tmp/bench_repos/click', 'src/click', 30),
        'requests': ('/tmp/bench_repos/requests', 'src/requests', 30),
    }

    manifest = {
        "metadata": {
            "freeze_commit": "509f131",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pre_registered_thresholds": {
                "recall_lower_bound_target": 0.70,
                "false_rejection_target": 0.10,
                "max_accepted_regressions": 0,
                "minimum_hazard_gate": 10,
                "inconclusive_rule": "If N_hazard < 10, recall is marked INCONCLUSIVE DUE TO INSUFFICIENT HAZARD BASE RATE"
            },
            "eval_datasets": {
                "headline": "flask (all 98 public symbols, 100% untuned)",
                "secondary": "click & requests (unseen symbols only, reported separately)",
                "speed_only": "django (time comparison only, slow test suite)"
            }
        },
        "predictions": []
    }

    print("Generating Devrith preflight predictions at freeze commit 509f131...")

    for repo_name, (repo_path, prefix, max_count) in repos.items():
        candidates = extract_candidates(repo_path, prefix, repo_name, max_count)
        print(f"Loaded {len(candidates)} candidates for {repo_name}...")
        for rel_file, symbol_name, stype in candidates:
            dir_name = os.path.dirname(rel_file)
            target_file = os.path.join(dir_name, f"_extracted_{symbol_name.lower()}.py")

            t0 = time.perf_counter()
            try:
                res = prefactor_decouple(
                    symbol=symbol_name,
                    from_file=rel_file,
                    to_file=target_file,
                    project_root=repo_path,
                    dry_run=True,
                    add_backward_compat_alias=True
                )
                dt_ms = (time.perf_counter() - t0) * 1000.0

                hazards = []
                for h_key in ['circular_hazards', 'global_state_hazards', 'state_desync_hazards', 'framework_hazards']:
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
                "repo": repo_name,
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

    output_path = "/home/divyam/Downloads/projects/arch-sidecar/benchmark/predictions_509f131.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"Successfully generated {len(manifest['predictions'])} preflight predictions!")
    print(f"Manifest written to: {output_path}")

if __name__ == '__main__':
    main()
