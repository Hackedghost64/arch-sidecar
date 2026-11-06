# `arch-sidecar`
### Architecture-Aware Context Retrieval & Deterministic Refactoring for AI Coding Agents

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-brightgreen.svg)](https://www.python.org/)
[![Model Context Protocol](https://img.shields.io/badge/MCP-Compatible-purple.svg)](https://modelcontextprotocol.io)
[![Tests Passing](https://img.shields.io/badge/tests-119%20passed-success.svg)](tests/)
[![Empirical Case Study](https://img.shields.io/badge/Case%20Study-Documented-orange.svg)](CASE_STUDY.md)

> `arch-sidecar` (Devrith Engine) provides **architecture-aware context retrieval**, **feature implementation planning**, **deterministic AST refactoring**, and **static scope/import preflight verification** for AI coding agents.
>
> ⚡ **Quickstart Guide:** [5-Minute Onboarding & MCP Setup](QUICKSTART.md)  
> 📖 **Read the empirical benchmarks (wins, losses, & boundaries):** [CASE_STUDY.md](CASE_STUDY.md) · [CASE_STUDY_FLASH.md](CASE_STUDY_FLASH.md) · [BENCHMARK_REPORT](benchmark/FINDINGS.md)

---

## ⚡ Quick Install & Update

Install `arch-sidecar` (and its alias `devrith`) in seconds with zero friction:

### One-Liner Install / Update (Linux / macOS / WSL)
Installs or upgrades `arch-sidecar` and `devrith` directly into `~/.local/bin` using isolated virtual environments (`uv` or standard Python `venv`):
```bash
curl -fsSL https://raw.githubusercontent.com/Hackedghost64/arch-sidecar/main/install.sh | bash
```

### Install a Specific Release Tag (e.g., `v0.4.0`)
```bash
DEVRITH_REF=v0.4.0 curl -fsSL https://raw.githubusercontent.com/Hackedghost64/arch-sidecar/main/install.sh | bash
```

### Via `uv`, `pipx`, or `pip`
```bash
uv tool install git+https://github.com/Hackedghost64/arch-sidecar.git@v0.4.0
# or
pipx install git+https://github.com/Hackedghost64/arch-sidecar.git@v0.4.0
# or
pip install --upgrade git+https://github.com/Hackedghost64/arch-sidecar.git@v0.4.0
```

### Self-Update Anytime from CLI
```bash
arch-sidecar update          # or: devrith update
arch-sidecar update --check  # check if a newer release tag is available
```

> [!TIP]
> Both `arch-sidecar` and `devrith` are installed as identical entry points. Use whichever you prefer in your terminal, shell scripts, or agent configurations.

---

## 🚀 What's New in v0.4.0

The `v0.4.0` release expands Devrith into a full-spectrum **6-language polyglot architectural co-processor**, drastically reducing agent context consumption and trial-and-error exploration across modern full-stack, mobile, and systems repositories.

- **6-Language Polyglot Adaptive Retrieval (`planner.py` & `context.py`):** First-class surgical context and symbol-level docstring indexing across **Python**, **TypeScript/JavaScript** (`.ts`, `.tsx`, `.js`, `.jsx`), **Go** (`.go`), **Rust** (`.rs`), **Dart/Flutter** (`.dart`, `pubspec.yaml`), and **Kotlin/Java** (`.kt`, `.kts`, `.java`). Operates via lightweight in-process AST parsers with **zero external binaries or background daemon processes**.
- **Rare-Term IDF Scoring:** Intelligent term weighting down-weights generic, ubiquitous CRUD verbs (`get`, `set`, `handle`, `process`, `fetch`, `execute`) and boosts domain-specific, rare identifiers up to **`+45`**, preventing search dilution and pinpointing exact target files and symbols.
- **Cross-Boundary Route & Environment Linking (`feature_workflow.py`):** Automatically bridges backend endpoints and frontend/mobile client files that share HTTP routes (e.g. `/v1/...`, `/api/...`) or environment variables (`os.environ`, `process.env`, `Platform.environment`). Modifying an API route automatically pulls in the corresponding frontend consumer for cross-layer consistency.
- **Atomic Multi-Symbol Polyglot Decoupling (`polyglot_decouple.py`):** Extract multiple symbols atomically (`["fn1", "fn2"]` or comma-separated) across supported languages in a single transaction with automatic import migration, re-export shims, and `local_dependencies_referenced` hazard tracking. Includes full transactional rollback if tests fail.
- **Real-World Polyglot A/B Benchmark (37,005 LOC across `anime-cli` & `movie-app`):**
  - **70.0% reduction in agent tool calls** (**18 vs. 60** calls)
  - **40.0% faster wall-clock completion** (**6m 19s vs. 10m 32s**)
  - **0 manual exploratory file reads** (down from 28 exploratory reads in the control arm)

---

> [!IMPORTANT]
> **Key Empirical Results from Pre-Registered Multi-Repo Benchmarks:**
> - **Polyglot A/B Benchmark (37,005 LOC across `anime-cli` & `movie-app`):**
>   - **70.0% reduction in agent tool calls** (**18 vs. 60** calls; 3.33x efficiency gain).
>   - **40.0% faster wall-clock completion** (**6m 19s vs. 10m 32s**).
>   - **0 manual exploratory file reads** (down from 28 exploratory reads in the control arm).
>   - *Task 1 (`anime-cli`, 20,704 LOC, 89 files — Python, TypeScript/JS, Kotlin):* Arm A (WITH Devrith): 7 tool calls, 0 `read_file`, 3m 51s, `arch-sidecar feature` runtime 593ms. Arm B (WITHOUT Devrith): 34 tool calls, 5m 54s.
>   - *Task 2 (`movie-app`, 16,301 LOC, 73 files — Python, Dart/Flutter, Kotlin, Java):* Arm A (WITH Devrith): 11 tool calls, 0 `read_file`, 2m 28s, `arch-sidecar feature` runtime 264.56ms. Arm B (WITHOUT Devrith): 26 tool calls, 4m 38s.
> - **Held-Out Hazard Recall (`pallets/werkzeug`, $N = 73$):** Caught **82.9% (29/35)** of breaking structural hazards before code touches disk or tests execute (95% Wilson CI: **[67.3%, 91.9%]**; **96.7%** adjudicated after correcting for filesystem-scan harness artifacts).
> - **False Rejection Rate:** **18.4% (7/38)** on held-out codebases due to conservative relative-import cycle detection (`from . import <submodule>`), and **8.7% (2/23)** on `Flask`.
> - **In-Memory Preflight vs. Staged Test Ladder:** Devrith preflight runs in **~274 ms** in-memory with zero disk writes and zero git rollbacks, compared to **~6,285 ms** for the staged post-apply control ladder and **~10.2s** for full pytest execution.
> - **End-to-End Efficiency:** Delivers **21% – 27% net token savings** on multi-file agent refactoring workflows by eliminating exploratory whole-file re-reads and avoiding test-failure debugging turns.
> - **What It Catches:** Intercepts compiler-grade AST hazards: circular import cycles (including backward-compatibility alias cycles), orphaned lexical bindings, and process stream monkeypatching that slip past naive file editors.
> - **The Structural vs. Behavioral Boundary:** Static preflight is a fast, pre-mutation structural gate. It does not catch behavioral logic drift, filesystem reflection (e.g. tests asserting on directory file listings), or dynamic mock monkeypatching (`unittest.mock.patch`). Dynamic unit tests remain required for behavioral validation.
> - **Where it wins:** Multi-slice mechanical extractions (`decouple` avoids repeated `view_file` re-reads after line shifts: **0 `view_file` calls vs. 13–14** in our `god_app.py` modularization smoke test), complex multi-file `@overload` refactorings (**-26.9% tokens, -22.9% wall time** on `requests`), and catching silent cross-module `global` state desynchronization (`global x` / `global counter`) that passes `ruff`/`pyflakes` and syntax checks.
> - **Where a bare agent beats it:** When a file fits in a single `view_file` call (<800 lines) and the user forces extraction of a function with entangled mutable module state (`bot_tick` in `god_app.py`), `arch-sidecar decouple` blocks the unsafe move but does not auto-generate a state proxy—resulting in **30 median tool calls (`664k` proxy input tokens) for Devrith vs. 12 median tool calls (`346k` proxy input tokens) for the Bare agent** (`n = 3` runs per arm). See [Section 8 of CASE_STUDY.md](CASE_STUDY.md#8-stress-testing-on-entangled-monoliths-god_apppy-wins-losses-and-known-boundaries) for the full data.
> - **Known static analysis boundaries:** Static AST analysis cannot resolve runtime reflection (`getattr`, `eval`, `exec`, `globals()`, `importlib`—flagged via `uncertainty`), runtime monkeypatching (`unittest.mock.patch`), or import-time I/O side effects (e.g., module-level `sqlite3.connect()`).
> - **Language scope:** Surgical context retrieval, route & environment linking, and atomic multi-symbol decoupling (`polyglot_decouple`) support **Python, TypeScript/JavaScript, Go, Rust, Dart/Flutter, and Kotlin/Java**. In-depth AST free-variable ($FV$) scope analysis is currently Python-first. C/C++ supports fast Architectural Digest (`digest`) skeleton generation.

---

## 🥊 Capability & Differentiation Matrix

| Capability | Aider (Repo-Map) | Codemap | CALM | GritQL / Codemod | **`arch-sidecar`** |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Primary Focus** | Read-path context | Read-path index | Read-path map + hash locks | Human batch migrations | **Agent refactoring & feature infrastructure** |
| **Token Reduction** | ~80% | ~90% | ~85% | N/A | **69.7% – 96%** |
| **Architectural Digest** | ✅ Yes | ✅ Yes | ✅ Yes | ❌ No | **✅ Yes (< 100ms AST skeleton)** |
| **Surgical Symbol Context** | Partial | ✅ Yes | ✅ Yes | ❌ No | **✅ Yes (~500 tokens)** |
| **Feature Workflow & Context**| ❌ No | ❌ No | ❌ No | ❌ No | **✅ Fast-path 1-indexed source + tests** |
| **Automated Code Extraction** | ❌ LLM rewrites file | ❌ Not supported | ❌ Not supported | ⚠️ Human-written DSL rules | **✅ Deterministic AST Codemod** |
| **Extraction Latency** | 30–60s (LLM inference) | N/A | N/A | N/A | **1.8ms – 16ms (Local AST)** |
| **Circular Import Defense** | ❌ Not verified | ❌ Not verified | ⚠️ Risk report | ❌ Not verified | **✅ Pre-flight Intercept ($FV$)** |
| **Scope Shadowing Analysis** | ❌ Not verified | ❌ Not verified | ❌ Not verified | ❌ Not verified | **✅ Compiler-Grade $FV = \text{Loaded} \setminus \text{Bound}$** |
| **Transactional Rollback** | ❌ No | ❌ No | ❌ No | ❌ No | **✅ Auto-restore on test failure** |
| **Evidence & Verification Ledger** | ❌ No | ❌ No | ❌ No | ❌ No | **✅ Statically & Runtime Verified** |
| **Automated Session Telemetry** | ❌ No | ❌ No | ❌ No | ❌ No | **✅ Local zero-network session store & reports** |

---

## ⚡ Core CLI & MCP Commands

### 1. Feature Implementation (`arch-sidecar feature` / `devrith feature`)
Delivers 1-indexed target function bodies, cross-file caller dependencies, companion test suites, and cross-boundary route/environment linking in <300ms across 6 supported languages, eliminating 65–70% of agent turns. Employs rare-term IDF scoring to boost domain-specific identifiers up to `+45` while down-weighting generic CRUD verbs. Add `--verify` to run compiler-grade scope preflight and automated test verification.

### 2. Deterministic Refactoring & Symbol Decoupling (`arch-sidecar workflow` / `decouple`)
End-to-end refactoring pipeline: plans context, audits dynamic reflection sites, checks circular import hazards, extracts single or multiple symbols atomically (`["fn1", "fn2"]`), tracks `local_dependencies_referenced`, verifies test suites, and executes atomic transactional rollback if tests fail.

### 3. Code Craft, OOP & Anti-Pattern Audit (`arch-sidecar audit`)
Compiler-grade AST audit that prevents LLMs from introducing procedural spaghetti, deep nesting, silent exception swallows, parameter mutations, and broken return contracts:
```bash
# Audit a single file or repository
arch-sidecar audit src/

# Fast Git staged audit (<10ms)
arch-sidecar audit --staged

# Strict zero-tolerance mode for CI/CD
arch-sidecar audit src/ --strict
```

### 4. Git Integration & Pre-Commit Guard (`arch-sidecar git`)
Install an automated Git pre-commit hook that guards your git history from AI bugs, circular imports, and silent swallows:
```bash
# Install automated Git pre-commit guard
arch-sidecar git hook install

# Predict architectural blast radius of uncommitted/staged git changes
arch-sidecar git impact
```

### 5. Automated Real-World Test & Telemetry Reporting (`arch-sidecar report`)
Automatically logs every execution session locally to `~/.devrith/sessions.jsonl` (zero network traffic). View anytime via terminal or prompt your agent:
```bash
# Terminal ASCII dashboard
arch-sidecar report

# Markdown or exported test report
arch-sidecar report --markdown
arch-sidecar report --export REAL_WORLD_REPORT.md
```

Prompt your AI agent:
- *"Show me the Devrith real-world test report"*
- *"Export our benchmark test report to REAL_WORLD_REPORT.md"*

### 6. Zero-Friction Self-Update (`arch-sidecar update` / `devrith update`)
Keep your Devrith installation up to date with a single command:
```bash
# Check if an update is available without modifying local files
arch-sidecar update --check

# Self-update to the latest release
arch-sidecar update
```

---

## 🌐 Language Support & Capabilities Scope

Devrith provides evidence-backed architectural assistance across modern programming languages:

| Language | Architectural Digest (`digest`) | Junk / Build Pruning | Fast-Path Feature Workflow (`feature`) | Deterministic Codemods (`workflow` / `decouple`) | Scope & Import DAG Preflight (`check`) | Support Tier |
|:---|:---:|:---:|:---:|:---:|:---:|:---:|
| **Python** (`.py`) | **✅ Full** (<100ms) | **✅ Full** | **✅ Full** (1-indexed + tests) | **✅ Full** ($FV$ Analysis) | **✅ Full** (Sub-15ms) | **Tier 1 (First-Class)** |
| **TypeScript & JS** (`.ts`, `.tsx`, `.js`, `.jsx`) | **✅ Full** (<100ms) | **✅ Full** (`node_modules`, `dist`) | **✅ Full** (Surgical context + routes) | **✅ Full** (Multi-symbol mover) | **✅ Full** (Cycle & lexical checks) | **Tier 1 (First-Class)** |
| **Go** (`.go`) | **✅ Full** (<100ms) | **✅ Full** (`vendor`, `bin`) | **✅ Full** (Surgical context + routes) | **✅ Full** (Multi-symbol mover) | **✅ Full** (Import cycle checks) | **Tier 1 (First-Class)** |
| **Rust** (`.rs`) | **✅ Full** (<100ms) | **✅ Full** (`target/`) | **✅ Full** (Surgical context + routes) | **✅ Full** (Multi-symbol mover) | **✅ Full** (Module tree checks) | **Tier 1 (First-Class)** |
| **Dart & Flutter** (`.dart`) | **✅ Full** (<100ms) | **✅ Full** (`.dart_tool`, `build/`) | **✅ Full** (Pubspec discovery + tests) | **✅ Full** (Multi-symbol mover) | **✅ Full** (Dependency analysis) | **Tier 1 (First-Class)** |
| **Kotlin & Java** (`.kt`, `.kts`, `.java`) | **✅ Full** (<100ms) | **✅ Full** (`build/`, `.gradle/`) | **✅ Full** (Surgical context + routes) | **✅ Full** (Multi-symbol mover) | **✅ Full** (Package & class checks) | **Tier 1 (First-Class)** |
| **C & C++** (`.c`, `.h`, `.cpp`, `.hpp`) | **✅ Full** (<100ms) | **✅ Full** (`CMakeFiles`, `.o`, `.a`) | 🔄 Orientation | 🔄 Roadmap | 🔄 Roadmap | **Tier 2 (Orientation)** |

### Zero-Dependency Polyglot Architecture
In `v0.4.0`, Devrith natively implements multi-language AST parsers for TypeScript/JavaScript, Go, Rust, Dart/Flutter, and Kotlin/Java with **0 external binary dependencies** or daemon processes. Surgical context extraction, cross-boundary route/environment linking, and atomic multi-symbol decoupling execute in milliseconds directly within the local process.

---

## 🔌 Connecting to Any AI Coding Agent via MCP

Add to your Model Context Protocol (MCP) configuration (`claude_desktop_config.json`, Cursor, Antigravity, or Windsurf):

```json
{
  "mcpServers": {
    "devrith": {
      "command": "arch-sidecar",
      "args": ["serve"]
    }
  }
}
```

Once connected, your agent gains access to:
- `feature_workflow`
- `audit_code_quality`
- `audit_git_changes`
- `get_git_change_impact`
- `get_subsystem_clusters`
- `predict_change_impact`
- `plan_adaptive_retrieval`
- `execute_refactor_workflow`
- `check_code_health`
- `get_session_report`
- `export_test_report`
- `get_architecture_digest`
- `locate_surgical_context`
- `prefactor_decouple`

---

## 📜 License
MIT License. Free to use in commercial and open-source workflows.
