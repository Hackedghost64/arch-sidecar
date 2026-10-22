#!/usr/bin/env python3
"""
Unbiased 60-Module Ground Truth Benchmark Runner.
Tests 60 top-level symbols across Click, Requests, and Django.
Compares Devrith's static preflight predictions against the empirical
'Force, Import, PyTest' Ground-Truth Oracle.
"""

import os
import sys
import time
import json
import math
import shutil
import ast
import subprocess
from typing import Dict, List, Any, Tuple

BENCHMARK_DIR = os.path.abspath(os.path.dirname(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(BENCHMARK_DIR, ".."))
WORKTREE_BASE = "/tmp/bench_oracle_worktrees"

sys.path.insert(0, PROJECT_ROOT)

from arch_sidecar.decouple import prefactor_decouple
from arch_sidecar.guard import check_code_health
from arch_sidecar.utils import estimate_tokens


REPOS = {
    "click": {
        "path": "/tmp/bench_repos/click",
        "import_cmd": ["python3", "-c", "import sys; sys.path.insert(0, 'src'); import click; print('OK')"],
        "test_cmd": ["pytest", "tests/test_basic.py", "-q"],
        "env": {"PYTHONPATH": "src"},
        "files": [
            "src/click/formatting.py",
            "src/click/parser.py",
            "src/click/termui.py",
            "src/click/types.py",
            "src/click/utils.py",
            "src/click/globals.py",
            "src/click/decorators.py",
            "src/click/exceptions.py"
        ]
    },
    "requests": {
        "path": "/tmp/bench_repos/requests",
        "import_cmd": ["python3", "-c", "import sys; sys.path.insert(0, 'src'); import requests; print('OK')"],
        "test_cmd": ["pytest", "tests/test_utils.py", "-q"],
        "env": {"PYTHONPATH": "src"},
        "files": [
            "src/requests/utils.py",
            "src/requests/sessions.py",
            "src/requests/models.py",
            "src/requests/adapters.py",
            "src/requests/auth.py",
            "src/requests/certs.py",
            "src/requests/cookies.py",
            "src/requests/structures.py"
        ]
    },
    "django": {
        "path": "/home/divyam/Downloads/projects/django",
        "import_cmd": [
            "uv", "run", "--with", "asgiref", "--with", "sqlparse", "--with", "tblib", "--python", "3.12",
            "python", "-c", "import os, sys, django; os.environ['DJANGO_SETTINGS_MODULE']='tests.test_sqlite'; sys.path.insert(0, 'tests'); django.setup(); print('OK')"
        ],
        "test_cmd": [
            "uv", "run", "--with", "asgiref", "--with", "sqlparse", "--with", "tblib", "--python", "3.12",
            "./tests/runtests.py", "utils_tests", "--settings=test_sqlite", "-v", "0"
        ],
        "env": {"PYTHONPATH": "."},
        "files": [
            "django/utils/text.py",
            "django/utils/html.py",
            "django/utils/encoding.py",
            "django/utils/functional.py",
            "django/utils/safestring.py",
            "django/utils/dateparse.py",
            "django/utils/crypto.py",
            "django/utils/http.py"
        ]
    }
}


def harvest_symbols(repo_key: str, count: int = 20) -> List[Dict[str, str]]:
    conf = REPOS[repo_key]
    repo_path = conf["path"]
    harvested = []
    
    for rel_path in conf["files"]:
        full_path = os.path.join(repo_path, rel_path)
        if not os.path.exists(full_path):
            continue
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                t = ast.parse(f.read(), filename=full_path)
            for n in t.body:
                if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
                    if not n.name.startswith("__") and len(harvested) < count:
                        harvested.append({
                            "repo": repo_key,
                            "file": rel_path,
                            "symbol": n.name
                        })
        except Exception:
            pass
    return harvested


def wilson_score_interval(successes: int, total: int, z: float = 1.96) -> Tuple[float, float]:
    """Computes Wilson score 95% confidence interval for a binomial proportion."""
    if total == 0:
        return 0.0, 0.0
    p = successes / total
    denom = 1 + (z ** 2) / total
    center = (p + (z ** 2) / (2 * total)) / denom
    spread = (z * math.sqrt((p * (1 - p) / total) + (z ** 2) / (4 * (total ** 2)))) / denom
    lower = max(0.0, center - spread)
    upper = min(1.0, center + spread)
    return round(lower, 4), round(upper, 4)


def setup_clean_worktree(repo_path: str, trial_id: str) -> str:
    target = os.path.join(WORKTREE_BASE, trial_id)
    if os.path.exists(target):
        shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(repo_path, target, ignore=shutil.ignore_patterns(".git", "__pycache__", "build", "dist"))
    return target


def run_oracle_for_symbol(item: Dict[str, str], trial_idx: int) -> Dict[str, Any]:
    repo_key = item["repo"]
    conf = REPOS[repo_key]
    rel_file = item["file"]
    symbol = item["symbol"]
    
    trial_id = f"trial_{repo_key}_{symbol}_{trial_idx}"
    worktree = setup_clean_worktree(conf["path"], trial_id)
    
    from_file = os.path.join(worktree, rel_file)
    dir_name = os.path.dirname(from_file)
    base_name = os.path.splitext(os.path.basename(from_file))[0]
    to_file = os.path.join(dir_name, f"_{base_name}_{symbol}_extracted.py")
    
    # --- PHASE A: Devrith Preflight ---
    t0_dev = time.perf_counter()
    dev_res = prefactor_decouple(
        symbol=symbol,
        from_file=from_file,
        to_file=to_file,
        project_root=worktree,
        dry_run=True
    )
    dev_duration_ms = round((time.perf_counter() - t0_dev) * 1000, 2)
    
    hazards = (
        dev_res.get("circular_hazards") or [] +
        dev_res.get("state_desync_hazards") or [] +
        dev_res.get("framework_hazards") or [] +
        dev_res.get("global_state_hazards") or []
    )
    dev_prediction = "UNSAFE" if (not dev_res.get("success", False) or len(hazards) > 0) else "SAFE"
    dev_tokens = estimate_tokens(dev_res.get("error", "") + str(hazards)) + 120
    
    # --- PHASE B: Force Extraction To Disk ---
    apply_res = prefactor_decouple(
        symbol=symbol,
        from_file=from_file,
        to_file=to_file,
        project_root=worktree,
        dry_run=False,
        allow_circular=True,
        allow_globals=True,
        allow_desync=True
    )
    
    # --- PHASE C: Ground Truth Oracle (Import + PyTest) ---
    t0_oracle = time.perf_counter()
    
    oracle_status = "SAFE"
    oracle_failure = None

    if not apply_res.get("success"):
        oracle_status = "UNSAFE"
        oracle_failure = f"EXTRACTION_FAILED: {apply_res.get('error')}"
    else:
        # Step 1: Import check
        import_env = {**os.environ, **conf.get("env", {})}
        import_proc = subprocess.run(
            conf["import_cmd"],
            cwd=worktree,
            env=import_env,
            capture_output=True,
            text=True,
            timeout=15
        )
        
        if import_proc.returncode != 0:
            oracle_status = "UNSAFE"
            oracle_failure = f"IMPORT_ERROR: {import_proc.stderr.strip()[:200]}"
        else:
            # Step 2: PyTest suite execution
            test_proc = subprocess.run(
                conf["test_cmd"],
                cwd=worktree,
                env=import_env,
                capture_output=True,
                text=True,
                timeout=30
            )
            if test_proc.returncode != 0:
                oracle_status = "UNSAFE"
                oracle_failure = f"TEST_REGRESSION: {test_proc.stdout.strip()[:200]}"
            
    oracle_duration_ms = round((time.perf_counter() - t0_oracle) * 1000, 2)
    
    # Cleanup worktree
    shutil.rmtree(worktree, ignore_errors=True)
    
    return {
        "repo": repo_key,
        "file": rel_file,
        "symbol": symbol,
        "devrith": {
            "prediction": dev_prediction,
            "duration_ms": dev_duration_ms,
            "tokens": dev_tokens,
            "hazards": hazards
        },
        "oracle": {
            "ground_truth": oracle_status,
            "duration_ms": oracle_duration_ms,
            "failure_reason": oracle_failure
        }
    }


def main():
    print("=" * 80)
    print("🔬 RUNNING UNBIASED 60-MODULE GROUND TRUTH BENCHMARK")
    print("Repositories: Click (20) | Requests (20) | Django (20)")
    print("Ground Truth Oracle: Physical AST Move -> Python Import Check -> PyTest Suite")
    print("=" * 80)
    
    os.makedirs(WORKTREE_BASE, exist_ok=True)
    
    all_symbols = []
    all_symbols.extend(harvest_symbols("click", 20))
    all_symbols.extend(harvest_symbols("requests", 20))
    all_symbols.extend(harvest_symbols("django", 20))
    
    print(f"Harvested {len(all_symbols)} unbiased top-level symbols across 3 codebases.\n")
    
    results = []
    tp = 0  # Unsafe & Devrith blocked
    fp = 0  # Safe & Devrith blocked (False Rejection)
    tn = 0  # Safe & Devrith allowed
    fn = 0  # Unsafe & Devrith allowed (Accepted Regression)
    
    total_dev_time = 0.0
    total_oracle_time = 0.0
    total_dev_tokens = 0
    
    for idx, item in enumerate(all_symbols, start=1):
        res = run_oracle_for_symbol(item, idx)
        results.append(res)
        
        dev_pred = res["devrith"]["prediction"]
        oracle_gt = res["oracle"]["ground_truth"]
        
        total_dev_time += res["devrith"]["duration_ms"]
        total_oracle_time += res["oracle"]["duration_ms"]
        total_dev_tokens += res["devrith"]["tokens"]
        
        if oracle_gt == "UNSAFE":
            if dev_pred == "UNSAFE":
                tp += 1
                classification = "✅ TRUE POSITIVE (Hazard Intercepted)"
            else:
                fn += 1
                classification = "❌ FALSE NEGATIVE (Accepted Regression)"
        else:
            if dev_pred == "SAFE":
                tn += 1
                classification = "✅ TRUE NEGATIVE (Safe Extraction Permitted)"
            else:
                fp += 1
                classification = "⚠️ FALSE POSITIVE (False Rejection)"
                
        print(
            f"[{idx:02d}/60] {item['repo']:<8} {item['symbol']:<30} | "
            f"Oracle: {oracle_gt:<6} | Devrith: {dev_pred:<6} | {classification}"
        )
        
    total_unsafe = tp + fn
    total_safe = tn + fp
    total_trials = len(results)
    
    hazard_recall = round(tp / total_unsafe, 4) if total_unsafe > 0 else 1.0
    wilson_low, wilson_high = wilson_score_interval(tp, total_unsafe)
    false_rejection_rate = round(fp / total_safe, 4) if total_safe > 0 else 0.0
    
    print("\n" + "=" * 80)
    print("📈 FINAL EMPIRICAL RESULTS & METRICS")
    print("=" * 80)
    print(f"Total Evaluations: {total_trials}")
    print(f"Ground Truth Unsafe Operations: {total_unsafe}")
    print(f"Ground Truth Safe Operations:   {total_safe}")
    print("-" * 80)
    print(f"True Positives  (Hazard Intercepted):  {tp}")
    print(f"False Negatives (Accepted Regression): {fn}")
    print(f"True Negatives  (Safe Extraction OK):  {tn}")
    print(f"False Positives (False Rejections):    {fp}")
    print("-" * 80)
    print(f"🎯 Hazard Recall:              {hazard_recall * 100:.1f}% (Target: >=90%)")
    print(f"📊 Wilson 95% Confidence Band: [{wilson_low * 100:.1f}%, {wilson_high * 100:.1f}%] (Target Lower Bound: >70%)")
    print(f"🚫 False Rejection Rate:       {false_rejection_rate * 100:.1f}% (Target: <=10%)")
    print(f"💥 Accepted Regressions (FN):  {fn} (Target: 0)")
    print("-" * 80)
    print(f"⏱️ Total Devrith Preflight Time: {total_dev_time / 1000:.2f}s  (Avg: {total_dev_time / total_trials:.1f}ms/op)")
    print(f"⏱️ Total PyTest Oracle Time:     {total_oracle_time / 1000:.2f}s (Avg: {total_oracle_time / total_trials:.1f}ms/op)")
    print(f"⚡ Devrith Speedup Factor:       {total_oracle_time / max(total_dev_time, 0.001):.1f}x faster than running Pytest")
    print(f"🪙 Total Devrith Token Budget:   {total_dev_tokens} tokens (Avg: {total_dev_tokens / total_trials:.1f} tokens/op)")
    print("=" * 80)
    
    # Save full report
    report_path = os.path.join(BENCHMARK_DIR, "ORACLE_BENCHMARK_RESULTS.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({
            "metrics": {
                "total_trials": total_trials,
                "ground_truth_unsafe": total_unsafe,
                "ground_truth_safe": total_safe,
                "true_positives": tp,
                "false_negatives": fn,
                "true_negatives": tn,
                "false_positives": fp,
                "hazard_recall": hazard_recall,
                "wilson_95_ci": [wilson_low, wilson_high],
                "false_rejection_rate": false_rejection_rate,
                "accepted_regressions": fn,
                "devrith_total_ms": total_dev_time,
                "oracle_total_ms": total_oracle_time,
                "speedup_factor": round(total_oracle_time / max(total_dev_time, 0.001), 2)
            },
            "trials": results
        }, f, indent=2)
    print(f"\nSaved raw trial ledger to {report_path}")

if __name__ == "__main__":
    main()
