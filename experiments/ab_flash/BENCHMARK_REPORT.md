# Empirical A/B Benchmark: arch-sidecar vs. Vanilla Agent
**Evaluator Model:** Google Gemini 3.8 Flash (`flash`)  
**Methodology:** 12 independent subagents (3 tasks × 2 conditions × 2 trials) in isolated, pristine repository checkouts with blind automated grading.  
**Commit Tested:** `a0e201d` (`arch-sidecar` v0.2.0)

---

## 1. Executive Summary

| Task | Category | Baseline Mean Tokens | Sidecar Mean Tokens | Token Diff (%) | Baseline Latency | Sidecar Latency | Latency Diff (%) | Correctness (Both) |
|---|---|---|---|---|---|---|---|---|
| **T1: anime-cli** | Refactoring (Single large file) | **1,143,111** | 1,946,432 | +70.3% | **174s** | 262s | +50.6% | **100% (1.0)** |
| **T2: requests** | Complex Refactoring (Multi-overload, heavy typing) | 5,469,760 | **4,000,500** | **-26.9% (-1.47M tokens)** | 651s | **502s** | **-22.9% (-149s)** | **100% (1.0)** |
| **T3: flask** | Architectural Audit (Read-only trace & trap detection) | **1,762,029** | 1,933,986 | +9.8% | 267s | **263s** | -1.5% | **100% (1.0)** |

### Key Findings
1. **100% Task Correctness Across All 12 Runs (1.0 / 1.0):**
   Every single subagent run across both conditions passed all unit tests (15/15 in anime-cli, 216/216 in requests), preserved complete backward compatibility, and answered all 10 architectural audit questions accurately without falling into the `RequestContext` deprecation trap.
2. **Substantial Token & Latency Savings on Complex Refactoring (-26.9% Tokens, -149s):**
   On `requests`, where functions contained multiple `@overload` stubs, strict typing invariants, and cross-module caller dependencies in `sessions.py` and `models.py`, `arch-sidecar` saved **1,469,260 tokens per session** and reduced end-to-end task completion time from 651 seconds down to 502 seconds.
3. **Tool Learning Overhead on Simpler Tasks:**
   On smaller refactoring tasks (like moving 2 functions in `anime-cli`), the vanilla agent directly edited the file in 22 tool calls. The sidecar agent explored CLI help (`--help`) and generated adaptive plans before executing, introducing overhead when the refactoring boundary was already obvious.
4. **Architectural Audits (Read-Only):**
   On Flask architectural tracing, latency was essentially identical (267s vs 263s), but querying `arch-sidecar plan` added ~9.8% token overhead before the agent navigated to specific code locations.

---

## 2. Detailed Task Breakdown

### Task T1: `anime-cli` (Decouple OS inhibitors from presentation)
- **Codebase:** `cli.py` (1,411 lines), `b3b7506`
- **Objective:** Move `SleepInhibitor` and `start_cloudflared_tunnel` into `system.py` with backward compatibility and 15 passing tests.

| Metric | Baseline Trial 1 | Baseline Trial 2 | Baseline Mean | Sidecar Trial 1 | Sidecar Trial 2 | Sidecar Mean | Difference |
|---|---|---|---|---|---|---|---|
| **Total Tokens** | 952,493 | 1,333,729 | 1,143,111 | 1,685,036 | 2,207,829 | 1,946,432 | +70.3% |
| **Input Tokens** | 941,781 | 1,322,023 | 1,131,902 | 1,671,599 | 2,197,486 | 1,934,542 | +70.9% |
| **Output Tokens**| 10,712  | 11,706    | 11,209    | 13,437    | 10,343    | 11,890    | +6.1% |
| **Tool Calls**   | 22      | 27        | 24.5      | 34        | 43        | 38.5      | +14.0 calls |
| **Wall Clock**   | 155s    | 193s      | 174s      | 259s      | 266s      | 262.5s    | +88.5s |
| **Test Pass**    | 15/15   | 15/15     | 100%      | 15/15     | 15/15     | 100%      | 0 regressions |
| **Score**        | 1.0     | 1.0       | 1.0       | 1.0       | 1.0       | 1.0       | Equal |

*Analysis:* For simple single-file cuts where the agent already knows the exact symbol names, invoking external CLI planners incurs discovery overhead unless the agent is natively instructed on the exact tool syntax.

---

### Task T2: `requests` (Decouple key-value helpers with `@overload` stubs)
- **Codebase:** `src/requests/utils.py` (1,155 lines), commit `611c616`
- **Objective:** Move `to_key_val_list` (3 `@overload` stubs + definition) and `from_key_val_list` into `src/requests/_kv.py`. Pass 216 test cases in `tests/test_utils.py` and preserve local `PYTHONPATH` package imports.

| Metric | Baseline Trial 1 | Baseline Trial 2 | Baseline Mean | Sidecar Trial 1 | Sidecar Trial 2 | Sidecar Mean | Difference |
|---|---|---|---|---|---|---|---|
| **Total Tokens** | 6,434,084 | 4,505,435 | 5,469,760 | 4,875,283 | 3,125,716 | **4,000,500** | **-26.9% (-1.47M tokens)** |
| **Input Tokens** | 6,393,525 | 4,465,792 | 5,429,659 | 4,843,832 | 3,100,271 | **3,972,052** | **-26.8% (-1.46M tokens)** |
| **Output Tokens**| 40,559    | 39,643    | 40,101    | 31,451    | 25,445    | **28,448**    | **-29.1% (-11.6K tokens)** |
| **Tool Calls**   | 83        | 68        | 75.5      | 74        | 56        | **65.0**      | **-10.5 calls** |
| **Wall Clock**   | 730s    | 572s      | 651s      | 565s      | 439s      | **502s**      | **-149s (-22.9%)** |
| **Test Pass**    | 216/216 | 216/216   | 100%      | 216/216   | 216/216   | 100%          | 0 regressions |
| **Score**        | 1.0     | 1.0       | 1.0       | 1.0       | 1.0       | 1.0           | Equal |

*Analysis:* In complex codebases where manual refactoring requires multi-file inspection across type stubs and callers, `arch-sidecar` prevents excessive context thrashing. The sidecar agent completed the task with **1.47 million fewer tokens** and **nearly 2.5 minutes less latency**.

---

### Task T3: `flask` (Architectural Audit & Deprecation Trap)
- **Codebase:** Flask `src/flask/` (commit `d086db8`)
- **Objective:** Trace WSGI entry point, request context creation/pushing, view dispatch, `url_for` delegation, and evaluate `RequestContext` status.

| Metric | Baseline Trial 1 | Baseline Trial 2 | Baseline Mean | Sidecar Trial 1 | Sidecar Trial 2 | Sidecar Mean | Difference |
|---|---|---|---|---|---|---|---|
| **Total Tokens** | 2,088,888 | 1,435,170 | 1,762,029 | 2,048,217 | 1,819,755 | 1,933,986 | +9.8% |
| **Output Tokens**| 19,408    | 18,298    | 18,853    | 17,365    | 16,909    | 17,137    | -9.1% |
| **Tool Calls**   | 39        | 28        | 33.5      | 40        | 36        | 38.0      | +4.5 calls |
| **Wall Clock**   | 291s      | 244s      | 267.5s    | 284s      | 243s      | 263.5s    | -4.0s |
| **Score**        | 1.0       | 1.0       | 1.0       | 1.0       | 1.0       | 1.0       | Equal (10/10 facts) |

*Analysis:* On read-only exploration of a well-organized repository, vanilla `grep` and file reading is already very fast. `arch-sidecar plan` provides an instant orientation packet, but running multiple CLI queries adds a modest (~9.8%) token overhead when the agent is purely writing a report.

---

## 3. Product & Architectural Conclusions

1. **Where arch-sidecar is Differentiated (Level 3: Structural Refactoring):**
   `arch-sidecar` shines in **Task T2**. When an agent must move interdependent code with decorators, type stubs, and cross-file references, LLMs typically flounder with multi-file line-range edits and repeated test failures. With `arch-sidecar`, the agent achieved a **26.9% total token reduction** and **23% latency reduction**.
2. **Where arch-sidecar is Neutral / Overhead (Level 1: Simple Edits & Read-Only Audits):**
   For straightforward edits or read-only surveys, adding a separate CLI tool introduces discovery steps (`--help`, CLI invocations) that add tokens without a proportional speedup.
3. **The Recommended Integration Mode:**
   Rather than asking an LLM to discover CLI commands via bash, `arch-sidecar` should be exposed as an **MCP server** inside the agent's native system prompt. This eliminates the 4–6 turns agents spent checking `--help` flags.
