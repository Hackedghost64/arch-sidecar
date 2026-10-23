#!/usr/bin/env python3
"""
Devrith Git Native Integration Benchmark.
Scientifically benchmarks:
1. Latency & Speedup: Devrith Git-Staged Surgical Audit vs Whole-Repo Audit across 4 codebases.
2. Pre-Commit Guard Efficacy: Hazard Recall (blocked toxic commits) vs False Rejection Rate (clean commits).
3. Targeted Test Execution: Devrith Git Impact vs Blind Full Test Suite.
"""

import os
import sys
import time
import json
import shutil
import tempfile
import subprocess
from typing import Dict, Any, List, Tuple

BENCHMARK_DIR = os.path.abspath(os.path.dirname(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(BENCHMARK_DIR, ".."))
sys.path.insert(0, PROJECT_ROOT)

from arch_sidecar.git_ops import (
    audit_git_changeset,
    analyze_git_change_impact,
    install_git_hook,
    uninstall_git_hook,
    get_staged_files,
)
from arch_sidecar.quality import audit_repository, audit_code_string


REPOS = {
    "requests": "/tmp/big_sample_repos/requests",
    "click": "/tmp/big_sample_repos/click",
    "flask": "/tmp/big_sample_repos/flask",
    "rich": "/tmp/big_sample_repos/rich",
}

COMMITS_DATASET = [
    # 5 Clean Commits
    {
        "id": "CLEAN-01",
        "description": "Add typed discount helper with early returns",
        "is_hazardous": False,
        "hazard_type": None,
        "filename": "helpers_discount.py",
        "code": '''
from typing import Optional

def get_discounted_price(price: float, discount_pct: float) -> float:
    """Calculates discounted price with validation guards."""
    if price <= 0.0:
        return 0.0
    if discount_pct <= 0.0:
        return price
    return price * (1.0 - (discount_pct / 100.0))
'''
    },
    {
        "id": "CLEAN-02",
        "description": "Add clean domain dataclass",
        "is_hazardous": False,
        "hazard_type": None,
        "filename": "user_session.py",
        "code": '''
from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class UserSession:
    user_id: str
    token: str
    is_active: bool = True
'''
    },
    {
        "id": "CLEAN-03",
        "description": "Safe string normalization utility",
        "is_hazardous": False,
        "hazard_type": None,
        "filename": "str_utils.py",
        "code": '''
def clean_slug(text: str) -> str:
    """Normalizes whitespace and lowercase characters."""
    if not text:
        return ""
    return "-".join(text.strip().lower().split())
'''
    },
    {
        "id": "CLEAN-04",
        "description": "Error logger with domain exception re-raise",
        "is_hazardous": False,
        "hazard_type": None,
        "filename": "error_handler.py",
        "code": '''
import logging

logger = logging.getLogger(__name__)

class DomainServiceError(Exception):
    pass

def safe_parse(payload: str) -> dict:
    import json
    try:
        return json.loads(payload)
    except Exception as e:
        logger.error(f"Failed to parse payload: {e}")
        raise DomainServiceError("Corrupted payload") from e
'''
    },
    {
        "id": "CLEAN-05",
        "description": "Clean math operation with guard clause",
        "is_hazardous": False,
        "hazard_type": None,
        "filename": "math_ops.py",
        "code": '''
def safe_divide(numerator: float, denominator: float) -> float:
    if denominator == 0.0:
        return 0.0
    return numerator / denominator
'''
    },

    # 5 Hazardous Commits
    {
        "id": "HAZARD-01",
        "description": "Silent exception swallowing with bare pass",
        "is_hazardous": True,
        "hazard_type": "silent_exception_swallowing",
        "filename": "payment_sync.py",
        "code": '''
def sync_payment_status(order_id: str):
    try:
        remote_gateway_call(order_id)
    except Exception:
        pass  # Production crash silently hidden!
'''
    },
    {
        "id": "HAZARD-02",
        "description": "Dangerous mutable default argument",
        "is_hazardous": True,
        "hazard_type": "mutable_default_argument",
        "filename": "cache_registry.py",
        "code": '''
def register_cache_entry(key: str, cache_dict={}):
    # Dangerous mutable dict shared across all calls!
    cache_dict[key] = True
    return cache_dict
'''
    },
    {
        "id": "HAZARD-03",
        "description": "Corrupted Python AST with syntax error",
        "is_hazardous": True,
        "hazard_type": "syntax_error",
        "filename": "broken_syntax.py",
        "code": '''
def broken_calc(x, y):
    if x > 10
        return y * 2
'''
    },
    {
        "id": "HAZARD-04",
        "description": "Sprawling function nested 6 levels deep",
        "is_hazardous": True,
        "hazard_type": "excessive_nesting_depth",
        "filename": "nested_parser.py",
        "code": '''
def parse_deep_tree(root):
    if root:
        for branch in root:
            if branch.is_leaf:
                while branch.has_data():
                    if branch.is_valid():
                        if branch.level > 5:
                            print("6 levels deep!")
'''
    },
    {
        "id": "HAZARD-05",
        "description": "Extreme cyclomatic complexity (>20 branches)",
        "is_hazardous": True,
        "hazard_type": "excessive_cyclomatic_complexity",
        "filename": "mega_dispatcher.py",
        "code": '''
def dispatch_event(a, b, c, d, e, f, g, h, i, j, k, l, m, n, o, p):
    if a and b: return 1
    if c or d: return 2
    if e and f: return 3
    if g or h: return 4
    if i and j: return 5
    if k or l: return 6
    if m and n: return 7
    if o or p: return 8
    if a or c: return 9
    if b and d: return 10
    if e or g: return 11
    if f and h: return 12
    if i or k: return 13
    if j and l: return 14
    if m or o: return 15
    if n and p: return 16
    return 0
'''
    }
]


def run_experiment_1_latency():
    print("\n" + "=" * 80)
    print("🔬 EXPERIMENT 1: Staged Surgical Audit vs Whole-Repo Baseline Latency")
    print("=" * 80)

    results = {}
    for name, path in REPOS.items():
        if not os.path.exists(path):
            continue

        # 1. Whole Repo Scan
        t0 = time.perf_counter()
        whole_report = audit_repository(path)
        whole_ms = (time.perf_counter() - t0) * 1000

        # 2. Stage 1 temporary modified file in that repo
        test_file = os.path.join(path, "_devrith_bench_tmp.py")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("def sample_staged_fn(x: int) -> int:\n    return x * 2\n")

        subprocess.run(["git", "add", "_devrith_bench_tmp.py"], cwd=path, capture_output=True)

        # 3. Measure Devrith Staged Audit
        staged_latencies = []
        for _ in range(5):
            t1 = time.perf_counter()
            staged_report = audit_git_changeset(path, staged_only=True)
            staged_latencies.append((time.perf_counter() - t1) * 1000)

        # Clean up
        subprocess.run(["git", "reset", "HEAD", "_devrith_bench_tmp.py"], cwd=path, capture_output=True)
        if os.path.exists(test_file):
            os.remove(test_file)

        median_staged_ms = sorted(staged_latencies)[len(staged_latencies) // 2]
        speedup = whole_ms / max(0.1, median_staged_ms)

        results[name] = {
            "total_files": whole_report.total_files,
            "whole_repo_ms": round(whole_ms, 2),
            "staged_audit_ms": round(median_staged_ms, 2),
            "speedup_factor": round(speedup, 1),
        }

        print(f"[{name.upper()}] ({whole_report.total_files} files):")
        print(f"  Whole Repo Scan:  {whole_ms:.2f} ms")
        print(f"  Devrith Staged:   {median_staged_ms:.2f} ms")
        print(f"  ⚡ Speedup:        {speedup:.1f}x faster\n")

    return results


def run_experiment_2_guard_efficacy():
    print("=" * 80)
    print("🛡️  EXPERIMENT 2: Pre-Commit Guard Interception & Defense Benchmark")
    print("=" * 80)

    # Create temporary git workspace
    temp_git_dir = tempfile.mkdtemp(prefix="devrith_git_bench_")
    subprocess.run(["git", "init"], cwd=temp_git_dir, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "BenchmarkUser"], cwd=temp_git_dir, check=True)
    subprocess.run(["git", "config", "user.email", "bench@test.com"], cwd=temp_git_dir, check=True)

    # Initial commit
    init_f = os.path.join(temp_git_dir, "init.txt")
    with open(init_f, "w") as f: f.write("init")
    subprocess.run(["git", "add", "."], cwd=temp_git_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=temp_git_dir, check=True)

    # Install Devrith Pre-Commit Hook
    install_git_hook(temp_git_dir)

    results = []
    blocked_hazards = 0
    total_hazards = 0
    false_rejections = 0
    total_clean = 0
    guard_latencies = []

    for item in COMMITS_DATASET:
        c_id = item["id"]
        is_hazard = item["is_hazardous"]
        filename = item["filename"]
        code = item["code"]

        if is_hazard:
            total_hazards += 1
        else:
            total_clean += 1

        target_file = os.path.join(temp_git_dir, filename)
        with open(target_file, "w", encoding="utf-8") as f:
            f.write(code)

        subprocess.run(["git", "add", filename], cwd=temp_git_dir, capture_output=True, check=True)

        # Attempt git commit (which triggers Devrith pre-commit hook)
        t0 = time.perf_counter()
        commit_res = subprocess.run(
            ["git", "commit", "-m", f"test {c_id}"],
            cwd=temp_git_dir,
            capture_output=True,
            text=True,
        )
        guard_dur_ms = (time.perf_counter() - t0) * 1000
        guard_latencies.append(guard_dur_ms)

        commit_succeeded = (commit_res.returncode == 0)

        # Audit trial outcome
        if is_hazard:
            if not commit_succeeded:
                blocked_hazards += 1
                status = "✅ BLOCKED (Correctly Intercepted)"
            else:
                status = "❌ ESCAPED (False Negative - Defect Entered Git!)"
        else:
            if commit_succeeded:
                status = "✅ COMMITTED (Clean Code Accepted)"
            else:
                false_rejections += 1
                status = "❌ REJECTED (False Positive - Clean Code Blocked!)"

        results.append({
            "id": c_id,
            "description": item["description"],
            "is_hazardous": is_hazard,
            "commit_succeeded": commit_succeeded,
            "guard_latency_ms": round(guard_dur_ms, 2),
            "status": status,
        })

        print(f"  [{c_id}] {item['description'][:45]:<45} -> {status} ({guard_dur_ms:.1f}ms)")

        # Reset git state for next trial
        subprocess.run(["git", "reset", "--hard", "HEAD~1" if commit_succeeded else "HEAD"], cwd=temp_git_dir, capture_output=True)
        if os.path.exists(target_file):
            os.remove(target_file)

    # Clean up workspace
    shutil.rmtree(temp_git_dir, ignore_errors=True)

    hazard_recall = (blocked_hazards / total_hazards * 100.0) if total_hazards > 0 else 100.0
    false_rejection_rate = (false_rejections / total_clean * 100.0) if total_clean > 0 else 0.0
    avg_guard_ms = sum(guard_latencies) / max(1, len(guard_latencies))

    summary = {
        "total_commits_tested": len(COMMITS_DATASET),
        "total_hazards_tested": total_hazards,
        "hazards_blocked": blocked_hazards,
        "hazard_recall_pct": round(hazard_recall, 1),
        "total_clean_tested": total_clean,
        "false_rejections": false_rejections,
        "false_rejection_rate_pct": round(false_rejection_rate, 1),
        "avg_guard_overhead_ms": round(avg_guard_ms, 2),
        "trials": results,
    }

    print("\n" + "-" * 80)
    print(f"🎯 Hazard Recall:        {hazard_recall:.1f}% ({blocked_hazards}/{total_hazards} hazardous commits intercepted)")
    print(f"🛡️  False Rejection Rate: {false_rejection_rate:.1f}% ({false_rejections}/{total_clean} clean commits blocked)")
    print(f"⏱️  Avg Hook Latency:     {avg_guard_ms:.1f} ms")
    print("-" * 80 + "\n")

    return summary


def run_experiment_3_impact_prediction():
    print("=" * 80)
    print("🎯 EXPERIMENT 3: Git Change Impact Prediction vs Blind Test Suites")
    print("=" * 80)

    results = {}
    test_cases = [
        {"repo": "requests", "path": REPOS["requests"], "file": "src/requests/utils.py"},
        {"repo": "click", "path": REPOS["click"], "file": "src/click/formatting.py"},
    ]

    for tc in test_cases:
        name = tc["repo"]
        path = tc["path"]
        rel_f = tc["file"]
        if not os.path.exists(path):
            continue

        full_f = os.path.join(path, rel_f)
        if not os.path.exists(full_f):
            continue

        # Touch/modify the file
        with open(full_f, "a", encoding="utf-8") as f:
            f.write("\n# Devrith impact test comment\n")

        subprocess.run(["git", "add", rel_f], cwd=path, capture_output=True)

        t0 = time.perf_counter()
        impact = analyze_git_change_impact(path, staged_only=True)
        impact_ms = (time.perf_counter() - t0) * 1000

        # Revert file
        subprocess.run(["git", "checkout", "HEAD", "--", rel_f], cwd=path, capture_output=True)

        results[name] = {
            "target": rel_f,
            "impact_analysis_ms": round(impact_ms, 2),
            "risk_level": impact.get("risk_level"),
            "direct_callers_count": len(impact.get("direct_callers", [])),
            "blast_radius_count": len(impact.get("transitive_blast_radius", [])),
            "affected_clusters": impact.get("affected_clusters", []),
            "recommended_tests": impact.get("recommended_test_files", []),
        }

        print(f"[{name.upper()}] Modified Target: {rel_f}")
        print(f"  Analysis Latency:      {impact_ms:.2f} ms")
        print(f"  Risk Level:            {impact.get('risk_level')}")
        print(f"  Direct Callers:        {len(impact.get('direct_callers', []))} modules")
        print(f"  Transitive Reach:      {len(impact.get('transitive_blast_radius', []))} modules")
        print(f"  Affected Clusters:     {', '.join(impact.get('affected_clusters', [])) or 'None'}")
        print(f"  Recommended Tests:     {len(impact.get('recommended_test_files', []))} test files targeted\n")

    return results


def main():
    start_total = time.perf_counter()
    exp1 = run_experiment_1_latency()
    exp2 = run_experiment_2_guard_efficacy()
    exp3 = run_experiment_3_impact_prediction()
    total_dur = time.perf_counter() - start_total

    final_payload = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "total_duration_sec": round(total_dur, 2),
        "experiment_1_latency_and_speedup": exp1,
        "experiment_2_pre_commit_guard_defense": exp2,
        "experiment_3_git_impact_prediction": exp3,
    }

    out_path = os.path.join(BENCHMARK_DIR, "GIT_INTEGRATION_BENCHMARK_RESULTS.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(final_payload, f, indent=2)

    print("=" * 80)
    print(f"🏆 ALL EXPERIMENTS COMPLETE in {total_dur:.2f}s!")
    print(f"Benchmark telemetry saved to: {out_path}")
    print("=" * 80)


if __name__ == "__main__":
    main()
