#!/usr/bin/env bash
# Creates isolated pristine working copies for every (task, condition, trial).
# Pinned commits:
#   anime-cli  b3b7506091afaf53ab99ad6724732cdc289575d9  (pre-decoupling parent)
#   requests   611c616 (grafted shallow clone in /tmp/bench_repos/requests)
#   flask      d086db856be187255b8ec61ef409357393020f32
set -euo pipefail
RUNS=${RUNS:-/tmp/ab_runs}
TRIALS=${TRIALS:-2}
rm -rf "$RUNS"; mkdir -p "$RUNS"
for cond in baseline sidecar; do
  for t in $(seq 1 "$TRIALS"); do
    d="$RUNS/t1_anime_${cond}_${t}"
    git clone -q /home/divyam/Downloads/projects/anime-cli "$d"
    git -C "$d" checkout -q b3b7506091afaf53ab99ad6724732cdc289575d9
    rm -rf "$d/.git"   # hide history (HEAD+1 contains the reference solution)

    d="$RUNS/t2_requests_${cond}_${t}"
    cp -r /tmp/bench_repos/requests "$d"; rm -rf "$d/.git"

    d="$RUNS/t3_flask_${cond}_${t}"
    cp -r /tmp/bench_repos/flask "$d"; rm -rf "$d/.git"
  done
done
ls "$RUNS"
