#!/usr/bin/env bash
set -euo pipefail

RUNS=${RUNS:-/tmp/round2_runs}
TRIALS=${TRIALS:-2}

rm -rf "$RUNS"
mkdir -p "$RUNS"

for cond in baseline sidecar; do
  for t in $(seq 1 "$TRIALS"); do
    # Task 1: Compact negative control (anime-cli aniskip.py)
    d="$RUNS/r1_compact_${cond}_${t}"
    git clone -q /home/divyam/Downloads/projects/anime-cli "$d"
    rm -rf "$d/.git"

    # Task 2: Click HelpFormatter refactor
    d="$RUNS/r2_click_${cond}_${t}"
    cp -r /tmp/bench_repos/click "$d"
    rm -rf "$d/.git"

    # Task 3: Hazard Crash Gate
    d="$RUNS/r3_hazard_${cond}_${t}"
    cp -r /tmp/bench_repos/hazard_testbed "$d"
  done
done

echo "Prepared pristine run copies:"
ls "$RUNS"
