#!/usr/bin/env python3
"""
Multi-Trial Empirical Evaluation Harness for arch-sidecar.
Runs repeated trials across different repositories to establish statistical consistency
and document cases where arch-sidecar excels vs. where it offers negligible benefit.
"""

import os
import sys
import time
import json
from typing import Dict, List, Any

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from arch_sidecar.digest import generate_digest
from arch_sidecar.context import locate_surgical_context
from arch_sidecar.decouple import prefactor_decouple
from arch_sidecar.utils import estimate_tokens

TEST_CASES = [
    {
        "repo": "anime-cli",
        "path": "/home/divyam/Downloads/projects/anime-cli",
        "symbol": "SleepInhibitor",
        "category": "Mixed-stack Monolith (>1,200 lines)",
        "expected_benefit": "HIGH (Large speedup, prevents file-reading sprawl)"
    },
    {
        "repo": "pallets/flask",
        "path": "/tmp/bench_repos/flask",
        "symbol": "Blueprint",
        "category": "Modular Python Framework",
        "expected_benefit": "HIGH (Precise symbol resolution across package)"
    },
    {
        "repo": "psf/requests",
        "path": "/tmp/bench_repos/requests",
        "symbol": "Session",
        "category": "Interconnected Class Hierarchy",
        "expected_benefit": "HIGH (Intercepts circular dependencies pre-flight)"
    },
    {
        "repo": "gupta-traders",
        "path": "/home/divyam/Downloads/projects/gupta-traders",
        "symbol": None,
        "category": "TypeScript / React Frontend with dist/ & node_modules/",
        "expected_benefit": "HIGH (Junk filtering prevents 50M token ingestion)"
    }
]

def run_trials(num_trials: int = 3) -> List[Dict[str, Any]]:
    results = []

    print(f"Starting {num_trials}-trial evaluation harness across {len(TEST_CASES)} repositories...\n")

    for tc in TEST_CASES:
        repo_name = tc["repo"]
        repo_path = tc["path"]
        
        if not os.path.exists(repo_path):
            print(f"Skipping {repo_name} (path not found)")
            continue

        print(f"Evaluating [{repo_name}] ({tc['category']}):")

        digest_latencies = []
        locate_latencies = []
        token_counts = []

        for trial in range(1, num_trials + 1):
            # 1. Digest Trial
            t0 = time.perf_counter()
            d = generate_digest(repo_path)
            t_digest = (time.perf_counter() - t0) * 1000
            digest_latencies.append(t_digest)
            token_counts.append(d["estimated_tokens"])

            # 2. Locate Trial
            if tc["symbol"]:
                t0 = time.perf_counter()
                ctx = locate_surgical_context(tc["symbol"], root_dir=repo_path)
                t_locate = (time.perf_counter() - t0) * 1000
                locate_latencies.append(t_locate)

            print(f"  Trial {trial}/{num_trials}: Digest {t_digest:.1f}ms", end="")
            if tc["symbol"]:
                print(f" | Locate '{tc['symbol']}' {t_locate:.1f}ms", end="")
            print()

        avg_digest_ms = round(sum(digest_latencies) / len(digest_latencies), 2)
        avg_locate_ms = round(sum(locate_latencies) / len(locate_latencies), 2) if locate_latencies else None

        summary = {
            "repo": repo_name,
            "category": tc["category"],
            "expected_benefit": tc["expected_benefit"],
            "avg_digest_tokens": round(sum(token_counts) / len(token_counts)),
            "avg_digest_latency_ms": avg_digest_ms,
            "avg_locate_latency_ms": avg_locate_ms,
            "raw_trials": {
                "digest_ms": [round(x, 2) for x in digest_latencies],
                "locate_ms": [round(x, 2) for x in locate_latencies]
            }
        }
        results.append(summary)
        print(f"  => Average: Digest {avg_digest_ms}ms" + (f", Locate {avg_locate_ms}ms" if avg_locate_ms else "") + "\n")

    return results

if __name__ == "__main__":
    trials = run_trials(3)
    out_file = os.path.join(os.path.dirname(__file__), "trial_results.json")
    with open(out_file, "w") as f:
        json.dump(trials, f, indent=2)
    print(f"Saved repeated trial results to {out_file}")
