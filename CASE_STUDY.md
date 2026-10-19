# Case Study: Architecture-Aware Retrieval vs. Standard Repository Exploration

A controlled empirical comparison evaluating whether `arch-sidecar` reduces the cognitive effort, roundtrips, and exploration latency required for an AI coding agent to complete a codebase architectural audit.

---

## 1. Objective
Measure the difference in agent performance when performing an architectural audit on an unfamiliar, mixed-stack codebase using:
- **Condition A (Standard Agent):** Standard terminal search and inspection tools (`find`, `grep`, custom one-liners, and raw file viewing).
- **Condition B (Sidecar-Powered Agent):** `arch-sidecar` architecture-aware tools (`digest` and `locate`).

---

## 2. Test Environment

| Parameter | Specification |
| :--- | :--- |
| **Target Repository** | [`anime-cli`](https://github.com/Hackedghost64/anime-cli) (Python CLI + FastAPI + React + Android) |
| **Target Commit** | `ee7e4aa` (17 Python modules, 4,338 lines of code, ~164k total codebase tokens) |
| **`arch-sidecar` Commit** | `32d73f5` |
| **Model** | Gemini 3.8 Flash (Agentic Mode, Antigravity) |
| **Date & Environment** | October 19, 2026 · Linux x86_64 Ubuntu 22.04 LTS |

### The Exact Audit Task Prompt
Both subagents were dispatched simultaneously with the identical prompt:
> *"1. Identify the most bloated and tightly coupled files in the codebase.*  
> *2. Specifically locate where low-level OS process management, sleep inhibition, or network tunnels are mixed into the CLI presentation loop.*  
> *3. List the functions and classes involved, where they are referenced, and what dependencies they pull in.*  
> *4. Report your final architectural diagnosis, tracking tool operations, token volume, and wall-clock duration."*

---

## 3. Empirical Results & Telemetry

| Metric | Condition A (Standard Agent) | Condition B (`arch-sidecar`) | **Impact** |
| :--- | :---: | :---: | :---: |
| **Context Acquisition Latency** | **151.0 seconds (2.5 min)** | **1.85 seconds** | **~80x faster** |
| **Tool Invocations Required** | **20 inspection operations** | **3 CLI commands** | **85% fewer roundtrips** |
| **Agent Trajectory Steps** | **46 steps** | **24 steps** | **48% shorter reasoning trace** |
| **Exploration Method** | `find`, `wc -l`, 5 `grep` passes, custom python AST scripts, 9 file reads | 1 `digest` + 2 targeted `locate` queries | **Eliminated manual searching** |
| **Token Accounting** | ~19,500 bytes (~4,800 tokens) ingested across raw file slices | 4,006 AST tokens (entire repo) + ~1,510 tokens (two locate packets) | **Structured, high-density context** |

---

## 4. Retrieval Transcript & Tool Call Breakdown

### Condition B (`arch-sidecar`) Trajectory:
1. `arch-sidecar digest /home/divyam/Downloads/projects/anime-cli` (1 command, ~4,000 tokens):
   - Instant structural skeleton of all 86 files, classes, methods, and docstrings.
2. `arch-sidecar locate "SleepInhibitor"` (1 command, ~754 tokens):
   - Extracted complete `SleepInhibitor` class implementation (lines 25–91 of `system.py`), required imports (`subprocess`, `shutil`), and all call sites in `cli.py:233, 258` and `tests/test_inhibitor_and_server.py:3`.
3. `arch-sidecar locate "start_cloudflared_tunnel"` (1 command, ~756 tokens):
   - Extracted full function implementation (lines 94–156 of `system.py`), tunnel parameters, and Cloudflare DoH `1.1.1.1` propagation logic.
4. Selective inspection:
   - Agent viewed only lines 1–40 of `system.py` and `cli.py` to inspect the top-level module docstring and import headers, leaving the remaining 1,240 lines of `cli.py` untouched.

### Condition A (Standard Agent) Trajectory:
- Took **20 sequential tool calls**:
  - `find . -name "*.py"`
  - `wc -l` on all files
  - Multiple `grep` calls searching for `"sleep"`, `"inhibit"`, `"tunnel"`, `"Popen"`, `"threading"`
  - Manual Python one-liners to inspect AST nodes in `cli.py`
  - Reading lines 1–100, 200–350, and 600–750 of `cli.py`
  - Reading `player.py`, `main.py`, and `system.py` in chunks
  - Incurred 2.5 minutes of sequential roundtrip tool latency before arriving at the same finding.

---

## 5. Architectural Quality Assessment

Both agents produced high-quality, technically accurate diagnoses:
- **Common Findings:** Both identified `cli.py` (1,280 lines) as the primary coupling hub and `player.py` (755 lines) as the secondary monolithic subsystem. Both identified `SleepInhibitor`'s dual GNOME/systemd fallback and `start_cloudflared_tunnel` subprocess lifecycle.
- **Subagent A Unique Finding:** Identified that `anilab/binge.py` and `anilab/schedule.py` perform inverted deferred imports (`from cli import fzf_select`).
- **Subagent B Unique Finding:** Rapidly analyzed the tunnel's DNS-over-HTTPS (DoH) verification logic on Cloudflare endpoint `1.1.1.1` directly from the `locate` packet.

---

## 6. Known Limitations
1. **Preliminary Single-Pair Trial:** This report documents a live, single-pair comparative run. Multi-trial distributions (e.g., 5+ runs with randomized order across 3+ repositories) are required to establish variance and confidence intervals.
2. **Static vs. Dynamic Boundaries:** Static AST retrieval captures lexical definitions, imports, and call sites, but cannot evaluate runtime reflection (e.g. `importlib`) or conditional branches dictated by environment variables without execution.

---

## 7. Reproduction Instructions

To reproduce the benchmark:
```bash
git clone https://github.com/Hackedghost64/arch-sidecar.git
cd arch-sidecar
pip install -e .

# Run the automated multi-repo benchmark suite
python3 benchmark.py

# Run the interactive walkthrough demo
python3 examples/demo.py
```
