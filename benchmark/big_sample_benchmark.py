#!/usr/bin/env python3
"""
Big-Sample Code Craft & Architecture Benchmark.
Scans 112,000+ lines of code across 420+ files in Requests, Click, Flask, and Rich.
Measures:
- AST parsing throughput (files/sec, ms/file)
- Detected architectural smells, anti-patterns, and complexity spikes
- Repository score comparisons
"""

import os
import sys
import time
import json
from typing import Dict, Any, List

BENCHMARK_DIR = os.path.abspath(os.path.dirname(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(BENCHMARK_DIR, ".."))
sys.path.insert(0, PROJECT_ROOT)

from arch_sidecar.quality import audit_repository, QualityReport


TARGET_REPOS = {
    "requests": "/tmp/big_sample_repos/requests",
    "click": "/tmp/big_sample_repos/click",
    "flask": "/tmp/big_sample_repos/flask",
    "rich": "/tmp/big_sample_repos/rich",
}

AI_SLOP_SAMPLES = {
    "ai_generated_feature": '''
def handle_incoming_webhook(request, cache={}):
    # Dangerous mutable default argument
    try:
        data = request.json
        if data is not None:
            if "event" in data:
                if data["event"] == "payment":
                    if "payload" in data:
                        for item in data["payload"]["items"]:
                            if item["status"] == "pending":
                                # 6 levels deep!
                                cache[item["id"]] = item
                                print("Processed item")
        return cache
    except Exception:
        # Silent swallow!
        pass
''',
    "ai_sprawling_god_method": '''
class PaymentProcessor:
    def process_transaction(self, tx):
        # Law of Demeter train-wreck
        zip_code = tx.customer.billing.address.geo.postal_code
        return zip_code
'''
}


def run_benchmark():
    print("=" * 80)
    print("🚀 DEVRITH CODE CRAFT & QUALITY BENCHMARK — BIG SAMPLE RUN")
    print("=" * 80)

    overall_start = time.perf_counter()
    repo_results = {}
    total_files = 0
    total_issues = 0
    total_errors = 0
    total_warnings = 0

    for name, path in TARGET_REPOS.items():
        if not os.path.exists(path):
            print(f"❌ Missing repository: {path}")
            continue

        print(f"\nScanning '{name}' at {path}...")
        t0 = time.perf_counter()
        report = audit_repository(path, strict=False)
        dur_ms = (time.perf_counter() - t0) * 1000

        total_files += report.total_files
        total_issues += report.total_issues
        total_errors += report.error_count
        total_warnings += report.warning_count

        throughput_fps = (report.total_files / (dur_ms / 1000.0)) if dur_ms > 0 else 0
        ms_per_file = dur_ms / max(1, report.total_files)

        repo_results[name] = {
            "files": report.total_files,
            "duration_ms": round(dur_ms, 2),
            "throughput_fps": round(throughput_fps, 1),
            "ms_per_file": round(ms_per_file, 2),
            "score": report.score,
            "errors": report.error_count,
            "warnings": report.warning_count,
            "categories": report.metrics.get("categories", {}),
            "sample_issues": [i.to_dict() for i in report.issues[:5]],
        }

        print(f"  ✓ {report.total_files} files scanned in {dur_ms:.1f}ms ({throughput_fps:.1f} files/sec, {ms_per_file:.2f}ms/file)")
        print(f"  ✓ Score: {report.score}/100 | Issues: {report.total_issues} (Errors: {report.error_count}, Warnings: {report.warning_count})")
        print(f"    - Complexity: {report.metrics.get('categories', {}).get('complexity', 0)}")
        print(f"    - Anti-Patterns: {report.metrics.get('categories', {}).get('anti_pattern', 0)}")
        print(f"    - OOP / Cohesion: {report.metrics.get('categories', {}).get('oop_cohesion', 0)}")
        print(f"    - Contract: {report.metrics.get('categories', {}).get('contract', 0)}")

    # Scan AI Slop Samples
    print("\nScanning Typical AI-Generated Code Snippets...")
    ai_results = {}
    from arch_sidecar.quality import audit_code_string
    for sample_name, code in AI_SLOP_SAMPLES.items():
        issues = audit_code_string(code)
        ai_results[sample_name] = {
            "issues_count": len(issues),
            "issues": [i.to_dict() for i in issues]
        }
        print(f"  [AI Sample: {sample_name}] Found {len(issues)} issues:")
        for issue in issues:
            print(f"    ❌ [{issue.category.upper()}] {issue.rule}: {issue.message}")

    total_dur_s = time.perf_counter() - overall_start

    summary = {
        "benchmark_timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "total_repositories": len(repo_results),
        "total_files_scanned": total_files,
        "total_issues_detected": total_issues,
        "total_errors": total_errors,
        "total_warnings": total_warnings,
        "total_duration_sec": round(total_dur_s, 2),
        "overall_throughput_fps": round(total_files / total_dur_s, 1) if total_dur_s > 0 else 0,
        "repositories": repo_results,
        "ai_slop_samples": ai_results,
    }

    out_file = os.path.join(BENCHMARK_DIR, "BIG_SAMPLE_AUDIT_RESULTS.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print("\n" + "=" * 80)
    print(f"🏁 BIG SAMPLE COMPLETE: {total_files} files scanned in {total_dur_s:.2f}s ({summary['overall_throughput_fps']} files/sec)")
    print(f"Telemetry exported to: {out_file}")
    print("=" * 80)


if __name__ == "__main__":
    run_benchmark()
