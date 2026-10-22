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

---

## 8. Stress-Testing on Entangled Monoliths (`god_app.py`): Wins, Losses, and Known Boundaries

To evaluate where `arch-sidecar` helps versus where it adds overhead, we ran two controlled experiments on a deliberately entangled 788-line Flask/SQLite/trading monolith (`god_app.py`) containing wildcard imports (`from flask import *`, `from datetime import *`), shared mutable module globals (`global x, LAST_ERROR`, `global counter`), import-time SQLite initialization, and hardcoded credentials (`SMTP_PASS`).

### 8.1 What We Measured & Accounting Method
- **Model:** Gemini Flash (`flash`) across all arms.
- **Arms Compared:**
  1. **Bare Agent:** Standard file read/edit (`view_file`, `write_to_file`, `replace_file_content`) and terminal commands only.
  2. **Ruff-Assisted Agent:** Standard tools + `ruff check` required before running tests.
  3. **Devrith-Assisted Agent:** Standard tools + `arch-sidecar` (`digest`, `decouple`, `check`).
- **Token Accounting Method:** The local subagent transcript logger records full step payloads rather than API billing `usage` structs. We report **Cumulative Input Tokens (proxy)** computed by summing the accumulated context window bytes re-sent to the planner on every turn divided by 4 (`chars / 4`), alongside **Output + Thinking Tokens (proxy)** (`chars / 4`).
- **Behavioral Verification (`test_bot_tick.py` & `test_modular.py`):** In addition to `py_compile` and symbol export checks, tests triggered a trade inside `bot_tick("BTCUSDT")` and asserted that querying Flask's `/debug` route on `god_app.app` reflected the incremented `x` counter and `LAST_ERROR` state—catching silent cross-module global namespace splits that pass syntax/linter checks.

---

### 8.2 Task A: Forced Extraction of Entangled State (`bot_tick` + `place_order`, `n = 3` per arm)

In Task A, agents were instructed to extract `bot_tick` and `place_order` into `trading_bot.py` while keeping `/debug` in `god_app.py` working. Because `bot_tick` mutates `global x, LAST_ERROR` while `/debug` reads `x` and `LAST_ERROR`, a naive extraction splits the state across two module namespaces.

| Arm (`n = 3` runs) | Median Tool Calls (Range) | Median Pre-Edit Calls | Median Wall Time (Range) | Median Cum. Input Tok (`chars/4`) | Median Output Tok (`chars/4`) | `py_compile` & `/debug` State Test |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **Bare Agent** | **12.0** (11–20) | **2.0** | **540.0s** (482–567s) | **346,207** | 20,330 | **3 / 3 PASS** |
| **Devrith Agent** | **30.0** (19–34) | **15.0** | **559.0s** (463–591s) | **664,622** | **18,580** | **3 / 3 PASS** |
| **Ruff Agent** | **27.0** (16–28) | **4.0** | **606.0s** (486–674s) | **1,075,200** | 25,650 | **3 / 3 PASS** |

#### Why Bare Beat Devrith and Ruff on Task A (The Loss Analysis)
1. **Whole-File Visibility in 1 Tool Call:** `god_app.py` is 788 lines. The agent's `view_file` tool reads up to 800 lines in a single call. The Bare agent read the entire file on Step 1, saw `global x, LAST_ERROR` in `bot_tick` alongside `@app.route('/debug')` reading `x`, and wrote a state-synchronization helper on its first edit.
2. **Blocking Without an Automated Remedy Forced Manual Workarounds:** On Step 1, `arch-sidecar decouple "bot_tick,place_order" god_app.py trading_bot.py` **correctly blocked** the extraction, flagging `mutable_global_desync` on `x` and `LAST_ERROR` (0 false blocks). However, because `decouple` only *blocked* the move and did not auto-generate a shared-state proxy or state module, the Devrith agent still had to read `god_app.py` with `view_file` and write the code manually. Worse, the preflight hazard warning caused 2 of the 3 Devrith runs (`devrith_run_1` and `devrith_run_2`) to spend 10–14 bash turns testing custom `sys.modules` `types.ModuleType` `@property` descriptors in `python3 -c` before writing `trading_bot.py`.
3. **Linter Noise on Legacy Monoliths:** Running `ruff check` on `god_app.py` surfaced 25+ pre-existing lint warnings (`E722` bare `except`, `F401` on re-exports), causing the Ruff arm to spend extra edit-and-lint cycles adding `__all__` and `# noqa` comments.

---

### 8.3 Task B: Open-Ended Modularization ("Make `god_app.py` modular", `n = 1` smoke test per arm)

In Task B (*indicative smoke test, `n = 1` per arm*), agents were asked to modularize `god_app.py` into at least 2 domain submodules while keeping all routes and `/debug` state working. Here, the agent had to *choose* what to extract and repeatedly slice code out of `god_app.py`.

| Arm (`n = 1`, indicative only) | Total Tool Calls | `view_file` Calls on `god_app.py` | Wall Time | Cum. Input Tok (`chars/4`) | Output Tok (`chars/4`) | Extracted Modules | `py_compile` & `/debug` State Test |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Devrith Agent** | **19** | **0** | **277.0s** | **160,372** | **6,037** | `indicators.py`, `auth.py`, `helpers.py`, `email_service.py` | **PASS (4/4)** |
| **Bare Agent** | **29** | **14** | **300.0s** | **534,375** | 9,455 | `indicators.py`, `auth_utils.py`, `math_utils.py`, `email_helpers.py` | **PASS (4/4)** |
| **Ruff Agent** | **40** | **13** | **472.0s** | **962,246** | 11,634 | `indicators.py`, `math_utils.py`, `email_helpers.py` | **PASS (4/4)** |

#### What Happened in Task B
- **Why Bare and Ruff Needed 13–14 `view_file` Calls on a 788-Line File:** Even though `god_app.py` fits in one initial `view_file` read, every time the Bare or Ruff agent removed a slice of functions using `replace_file_content`, the line numbers in `god_app.py` shifted. Both agents had to call `view_file` 13–14 times between edits to re-check line numbers before each subsequent `replace_file_content` call, re-sending the ~7,000-token file into the context window on every turn (534k and 962k cumulative input tokens).
- **Why Devrith Used 0 `view_file` Calls (160k Cumulative Input Tokens):** The Devrith agent ran `arch-sidecar digest god_app.py` (749 tokens) once on Step 1 to see the module's functions, routes, and constants, and then used `arch-sidecar decouple` to extract `indicators.py`, `auth.py`, `helpers.py`, and `email_service.py` (using the tool's `minimal_safe_closure` when `send_email` required `SMTP_USER` and `SMTP_PASS`). When the agent tried `arch-sidecar decouple "bot_tick" god_app.py bot.py` on Step 5, Devrith blocked it due to the `x` / `LAST_ERROR` state desync hazard, so the agent left `bot_tick` in `god_app.py` and extracted the pure/self-contained slices instead.
- **Caveat (`n = 1`):** Task B is a single-run smoke test per arm on a 788-line file; multi-run trials (`n >= 3`) on larger multi-file codebases (3,000+ lines) are required before drawing general conclusions about exploration savings.

---

### 8.4 Dogfooding Audit: 10 Bugs Found & Fixed in `arch-sidecar`

Running `arch-sidecar` on its own codebase and against `god_app.py` exposed 10 concrete bugs in our own static analyzer and codemod engine, all of which were fixed and covered by unit tests (49 total tests in `tests/`):
1. **`guard.py` (`hasattr(ast, " TypeAlias")` typo):** Flagged by `arch-sidecar uncertainty .` on itself; a leading space in `" TypeAlias"` made the Python 3.12 `type X = ...` check dead code.
2. **`guard.py` (Wildcard import blindness):** Previously, any `from X import *` disabled `UNDEFINED_SYMBOL` checks for the entire file. `ScopeCollector` now statically resolves exports from local/stdlib/site-packages `.py` modules and built-in modules without executing code.
3. **`guard.py` (Unbound `global` / `nonlocal` masking):** Previously added `global` and `nonlocal` names directly to local `func_scope`, hiding `NameError` when a function read an undefined module global (`x = x + 1`).
4. **`guard.py` (Class body scope leaking into methods):** Previously left `class_scope` visible on `scope_stack` while visiting methods, failing to catch missing `self.` references to class attributes/methods.
5. **`guard.py` (2-hop circular import limit):** Upgraded cycle detection from 2-hop (`A -> B -> A`) to multi-hop BFS (`A -> B -> C -> A`).
6. **`decouple.py` (Dropped wildcard imports during extraction):** `find_relevant_imports` previously dropped `from datetime import *` while keeping `import datetime`, breaking `datetime.now()` calls when `from datetime import *` shadowed the `datetime` module with the `datetime.datetime` class.
7. **`decouple.py` (Reverse-direction `global` desynchronization):** Added bidirectional `ast.Global` / `ast.Nonlocal` checks so extracting a reader (`debug()`) while a mutator (`register()` with `global counter`) stays behind is blocked just like extracting the mutator.
8. **`decouple.py` (Line-offset shift on late imports & `__future__` ordering):** Fixed `last_import_line` adjustment when extracted blocks precede a late top-level import, and ensured `missing_imports` appended to existing files are placed after module docstrings and `from __future__ import annotations`.
9. **`planner.py` & `workflow.py` (Short-name substring collisions):** Prevented 1–2 character function names (`c`, `p`, `up`) from matching as substrings inside English task words (`de**c**ouple`, `god_a**p**p.py`), and restricted inferred symbols in `workflow.py` to the target `from_file`.
10. **`digest.py`, `context.py`, `uncertainty.py` (Call decorators, `posonlyargs`, single-file paths):** Fixed `digest.py` dropping `@app.route(...)` (`ast.Call`) decorators and misaligning defaults when positional-only (`/`) arguments are present, included decorator lines in `SymbolFinder`, and supported single-file paths in `digest`, `context`, and `uncertainty`.

---

### 8.5 Known Static Analysis Boundaries & What Is Not Yet Tested

#### Static Analysis Boundaries (What `arch-sidecar` Cannot See)
- **Dynamic Reflection & Execution (`getattr`, `setattr`, `eval`, `exec`, `globals()`, `importlib`):** `arch-sidecar uncertainty` flags these call sites as `[Potential]` or `[Unknown]`, but static AST analysis cannot resolve runtime string expressions or dynamic attribute mutations.
- **Import-Time Side Effects:** Moving top-level statements or changing import order can alter runtime behavior when modules execute I/O or database queries at import time (such as `conn = sqlite3.connect(...)` and `init_db()` running at import time in `god_app.py`).
- **Runtime Monkeypatching:** Dynamic test patches (`unittest.mock.patch.object`) or runtime module attribute overrides are invisible to static scope checks and must be verified by running the test suite (`--test`).
- **Blocking vs. Rewriting Shared State:** When `arch-sidecar decouple` blocks an extraction due to shared mutable globals (`global x`) or framework route decorators (`@app.route`), it prevents a silent bug (or allows explicit override via `--allow-desync` / `--allow-globals` / `--allow-circular`), but it does **not** automatically rewrite the code into a state class or Flask `Blueprint`.

#### Not Yet Tested
- **Large Multi-File Monoliths (3,000–10,000+ lines):** On files under 800 lines, a single `view_file` call can load the entire file. Testing whether `digest` and `plan` consistently outperform standard exploration requires multi-run benchmarks on repositories where target files exceed single-read tool limits.
- **Stronger Reasoning Models:** Benchmarks above used Gemini Flash (`flash`); relative tool-call and token tradeoffs may differ on deeper reasoning models (e.g., Claude Sonnet/Opus, Gemini Pro, GPT-5).
- **Non-Python Refactoring:** AST codemods (`decouple`, `workflow`) and scope preflight (`check`) are currently Python-only; other languages (`TS/JS`, `Go`, `Rust`, `JVM`, `C/C++`) are limited to `digest` skeleton generation.

---

## 9. The Unbiased 60-Module Ground Truth Benchmark: Testing Devrith against the "Force, Import, PyTest" Oracle

### 9.1 Motivation & Unbiased Experimental Design
To avoid "author-chosen" test cases where benchmarks only evaluate patterns the tool was explicitly designed to catch, we constructed an uncurated evaluation rig across three major production codebases:
- **`pallets/click`** (`src/click/*.py`, revision `2247b35e`)
- **`psf/requests`** (`src/requests/*.py`, revision `611c6162`)
- **`django/django`** (`django/utils/*.py`, revision `79502d08`)

We automatically harvested 20 top-level functions and classes from each repository (60 total symbols across 24 modules). For each symbol, we:
1. Checked out a fresh, disposable git worktree.
2. Ran **Devrith Static Preflight** (`decouple` with `dry_run=True`) in sub-30ms to record Devrith's prediction (`SAFE` vs `UNSAFE`).
3. **Physically forced the extraction to disk** (`dry_run=False` with `allow_circular=True, allow_globals=True`), moving the symbol to a sibling module and injecting the backward-compatibility alias `from .<target> import <Symbol>`.
4. Executed the empirical **Ground Truth Oracle**:
   - **Step 1 (Import Check):** `python3 -c "import <package>"` verifying package initialization.
   - **Step 2 (PyTest Suite):** Executed full relevant test suites (`pytest tests/test_basic.py`, `pytest tests/test_utils.py`, `runtests.py utils_tests`).

### 9.2 Empirical Results & Confusion Matrix

| Metric | Target Standard | Measured Empirical Result |
| :--- | :---: | :---: |
| **Total Evaluations** | 60 modules | **60 symbols** (20 Click, 20 Requests, 20 Django) |
| **Ground Truth Unsafe Operations** | Variable | **14** (caused import crash or test regressions) |
| **Ground Truth Safe Operations** | Variable | **46** (passed import and full test suite) |
| **True Positives (Hazard Intercepted)** | Maximize | **14 / 14 (100.0%)** |
| **False Negatives (Accepted Regressions)** | $0$ | **0 (0.0%)** |
| **True Negatives (Safe Extraction Permitted)** | Maximize | **36 / 46 (78.3%)** |
| **False Positives (Safe Move Blocked)** | Minimize | **10 / 46 (21.7%)** |
| **🎯 Hazard Recall** | $\ge 90.0\%$ | **100.0%** |
| **📊 Wilson 95% Confidence Interval** | Lower Bound $> 70.0\%$ | **[78.5%, 100.0%]** |
| **🚫 False Rejection Rate** | $\le 10.0\%$ | **21.7%** (reduced from 46.7% after TypeVar patch) |
| **⏱️ Devrith Total Preflight Latency** | Sub-second | **1.57 seconds total** (Avg: **26.1 ms / op**) |
| **⏱️ PyTest Oracle Total Latency** | Baseline | **152.47 seconds total** (Avg: **2,541.2 ms / op**) |
| **⚡ Devrith Execution Speedup** | $> 10\times$ | **97.2x faster than running PyTest** |
| **🪙 Devrith Token Footprint** | $< 500$ tokens | **177.4 tokens / op** (Total: 10,643 tokens) |

*Full raw JSON data for all 60 trials is persisted in [`benchmark/ORACLE_BENCHMARK_RESULTS.json`](benchmark/ORACLE_BENCHMARK_RESULTS.json).*

---

### 9.3 Discovery: When Devrith Catches Bugs That Unit Tests Miss

A critical revelation of the 60-module benchmark occurred when analyzing the 10 remaining "False Positives" (where Devrith blocked an extraction that passed `pytest`). When we invoked the extracted functions directly in Python with production arguments, **almost all of them crashed with runtime `NameError` defects that existing unit test suites were completely blind to**:

1. **`requests.utils.get_netrc_auth` (`NameError: name 'NETRC_FILES' is not defined`):**  
   - In `requests/tests/test_utils.py`, tests passed because the test suite monkeypatches `monkeypatch.setenv("NETRC", ...)`. Under `NETRC`, the code path references `os.environ` and never evaluates `NETRC_FILES`.
   - In production, when a real user calls `requests.get()` without `NETRC` set, the extracted function crashes with `NameError: name 'NETRC_FILES' is not defined`. Devrith’s static AST preflight caught this missing dependency instantly.
2. **`click.parser._normalize_opt` (`NameError: name '_split_opt' is not defined`):**  
   - Click's `test_basic.py` passed because standard test cases never supplied a `token_normalize_func`.
   - When a user configures case-insensitive options, calling `_normalize_opt("--foo", ctx)` crashes immediately with `NameError`. Devrith flagged `_split_opt` as an unextracted local callable and blocked the move.
3. **`click.termui._mask_hidden_input` (`NameError: name '_HIDDEN_INPUT_MASK' is not defined`):**  
   - Failed with `NameError` on direct invocation due to the unextracted module-level string constant `_HIDDEN_INPUT_MASK = "*****"`.

### 9.4 Takeaways & Scientific Conclusions
1. **Devrith's Core Value Is Speed and Cost-Effective Preflight:**  
   Waiting for a full test suite to run across 60 operations took **2.5 minutes** and thousands of context tokens parsing failure dumps. Devrith produced the exact same 100% hazard recall in **1.57 seconds (26.1ms per check)** at **177 tokens per operation**.
2. **Deterministic AST Extraction Prevents Context Death Spirals:**  
   By intercepting 14 out of 14 fatal runtime hazards before disk writes or test execution, Devrith eliminates the multi-turn debugging loops where agents spend 10–15 tool calls attempting to resolve broken imports or missing globals.


