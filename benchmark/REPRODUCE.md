# Reproducibility Guide: Devrith Empirical Benchmark Suite

**Rig:** Antigravity Empirical Evaluation Suite  
**Evaluator Target:** `arch-sidecar` (Devrith Engine) vs. Baseline vs. Serena vs. Repowise vs. Composite  
**Environment Requirements:** Linux (Ubuntu 22.04+ recommended), Python 3.9+, Node.js v18+, `uv` (Astral)

---

## 1. Quick Setup & Prerequisites

### 1.1 Install System Tools
```bash
# Verify Python & Git
python3 --version
git --version

# Install uv (if not already present)
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.cargo/env
uv --version
```

### 1.2 Clone Evaluation Repositories
The benchmark uses three pinned open-source codebases:
```bash
mkdir -p /tmp/bench_repos

# PSF / Requests (Pinned to commit 611c616)
git clone https://github.com/psf/requests.git /tmp/bench_repos/requests
git -C /tmp/bench_repos/requests checkout 611c6162cbc4ac2020a2f91c7cfa4f3abf9bbb60

# Pallets / Click (Pinned to commit 2247b35)
git clone https://github.com/pallets/click.git /tmp/bench_repos/click
git -C /tmp/bench_repos/click checkout 2247b35ea1c47c727d7a06e51fa280e12a863ff6

# Django / Django (Pinned to commit 79502d0)
# Note: If Django is in ~/Downloads/projects/django, link it:
mkdir -p /home/divyam/Downloads/projects
if [ ! -d "/home/divyam/Downloads/projects/django" ]; then
    git clone https://github.com/django/django.git /home/divyam/Downloads/projects/django
    git -C /home/divyam/Downloads/projects/django checkout 79502d08405dbf24efa39e8731320f31035b9e9c
fi
```

---

## 2. Running the Benchmark

### 2.1 Run a Single Task Smoke Test
To verify all 5 arms on a single task (e.g. `TASK-01`):
```bash
cd /home/divyam/Downloads/projects/arch-sidecar
python3 benchmark/runner.py --task TASK-01 --arm all
```

### 2.2 Run an Unsafe Extraction Test (Hazard Defense Verification)
To verify how each arm handles circular import cycles and shared mutable state (e.g. `TASK-06`):
```bash
python3 benchmark/runner.py --task TASK-06 --arm all
```
*Expected Result:*
- Baseline & Serena: `UNCAUGHT_HAZARD` (lacks preflight detection)
- Devrith & Composite: `BLOCKED_AS_EXPECTED` (<30ms)

### 2.3 Run the Full 20-Task × 5-Arm Evaluation Suite
```bash
python3 benchmark/runner.py --task all --arm all
```

Results will be saved as structured JSON files into `benchmark/results/<task_id>_<arm>.json`.

---

## 3. Verifying the Results & Generating Reports

To aggregate the JSON results and print the summary statistics:
```bash
python3 -c "
import glob, json
from collections import defaultdict

results = [json.load(open(p)) for p in glob.glob('benchmark/results/*.json')]
by_arm = defaultdict(list)
for r in results:
    by_arm[r['arm']].append(r)

print('=' * 80)
print(f'Empirical Results across {len(results)} Benchmark Trials:')
print('=' * 80)
for arm, runs in sorted(by_arm.items()):
    tokens = sum(r['aggregate']['mean_tokens'] for r in runs) / len(runs)
    lat = sum(r['aggregate']['mean_tool_duration_ms'] for r in runs) / len(runs)
    statuses = defaultdict(int)
    for r in runs:
        statuses[r['aggregate']['primary_status']] += 1
    print(f'[{arm.upper():<10}] Mean Tokens: {tokens:<7.1f} | Mean Latency: {lat:<6.1f}ms | Statuses: {dict(statuses)}')
"
```

---

## 4. Benchmark Artifacts Directory Structure

```
benchmark/
├── INITIAL_AUDIT.md            # Phase 1: Ground-truth codebase inventory
├── FAIR_COMPARISON_DESIGN.md   # Phase 2: Competitor matrix & fairness controls
├── EVALUATION_CORPUS.json      # Phase 3: 20 curated tasks across 3 codebases
├── runner.py                   # Phase 4: Automated multi-arm runner
├── results/                    # Phase 5: 100 raw JSON telemetry files
│   ├── TASK-01_baseline.json
│   ├── TASK-01_devrith.json
│   └── ... (100 files)
├── FINDINGS.md                 # Phase 5: Statistical analysis & numbers
├── COMPETITIVE_ANALYSIS.md     # Phase 6: Core advantage, boundaries & strategy
└── REPRODUCE.md                # Phase 7: Step-by-step reproduction guide
```
