# `arch-sidecar`
### Architecture-Aware Context Retrieval & Deterministic Refactoring for AI Coding Agents

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-brightgreen.svg)](https://www.python.org/)
[![Model Context Protocol](https://img.shields.io/badge/MCP-Compatible-purple.svg)](https://modelcontextprotocol.io)
[![Tests Passing](https://img.shields.io/badge/tests-72%20passed-success.svg)](tests/)
[![Empirical Case Study](https://img.shields.io/badge/Case%20Study-Documented-orange.svg)](CASE_STUDY.md)

> `arch-sidecar` (Devrith Engine) provides **architecture-aware context retrieval**, **feature implementation planning**, **deterministic AST refactoring**, and **static scope/import preflight verification** for AI coding agents.
>
> ⚡ **Quickstart Guide:** [5-Minute Onboarding & MCP Setup](QUICKSTART.md)  
> 📖 **Read the empirical benchmarks (wins, losses, & boundaries):** [CASE_STUDY.md](CASE_STUDY.md) · [CASE_STUDY_FLASH.md](CASE_STUDY_FLASH.md) · [BENCHMARK_REPORT](benchmark/FINDINGS.md)

> [!IMPORTANT]
> **Key Empirical Results from Pre-Registered Multi-Repo Benchmarks (`Werkzeug`, `Flask`, `Click`, `Requests`):**
> - **Held-Out Hazard Recall (`pallets/werkzeug`, $N = 73$):** Caught **82.9% (29/35)** of breaking structural hazards before code touches disk or tests execute (95% Wilson CI: **[67.3%, 91.9%]**; **96.7%** adjudicated after correcting for filesystem-scan harness artifacts).
> - **False Rejection Rate:** **18.4% (7/38)** on held-out codebases due to conservative relative-import cycle detection (`from . import <submodule>`), and **8.7% (2/23)** on `Flask`.
> - **In-Memory Preflight vs. Staged Test Ladder:** Devrith preflight runs in **~274 ms** in-memory with zero disk writes and zero git rollbacks, compared to **~6,285 ms** for the staged post-apply control ladder and **~10.2s** for full pytest execution.
> - **End-to-End Efficiency:** Delivers **21% – 27% net token savings** on multi-file agent refactoring workflows by eliminating exploratory whole-file re-reads and avoiding test-failure debugging turns.
> - **What It Catches:** Intercepts compiler-grade AST hazards: circular import cycles (including backward-compatibility alias cycles), orphaned lexical bindings, and process stream monkeypatching that slip past naive file editors.
> - **The Structural vs. Behavioral Boundary:** Static preflight is a fast, pre-mutation structural gate. It does not catch behavioral logic drift, filesystem reflection (e.g. tests asserting on directory file listings), or dynamic mock monkeypatching (`unittest.mock.patch`). Dynamic unit tests remain required for behavioral validation.
> - **Where it wins:** Multi-slice mechanical extractions (`decouple` avoids repeated `view_file` re-reads after line shifts: **0 `view_file` calls vs. 13–14** in our `god_app.py` modularization smoke test), complex multi-file `@overload` refactorings (**-26.9% tokens, -22.9% wall time** on `requests`), and catching silent cross-module `global` state desynchronization (`global x` / `global counter`) that passes `ruff`/`pyflakes` and syntax checks.
> - **Where a bare agent beats it:** When a file fits in a single `view_file` call (<800 lines) and the user forces extraction of a function with entangled mutable module state (`bot_tick` in `god_app.py`), `arch-sidecar decouple` blocks the unsafe move but does not auto-generate a state proxy—resulting in **30 median tool calls (`664k` proxy input tokens) for Devrith vs. 12 median tool calls (`346k` proxy input tokens) for the Bare agent** (`n = 3` runs per arm). See [Section 8 of CASE_STUDY.md](CASE_STUDY.md#8-stress-testing-on-entangled-monoliths-god_apppy-wins-losses-and-known-boundaries) for the full data.
> - **Known static analysis boundaries:** Static AST analysis cannot resolve runtime reflection (`getattr`, `eval`, `exec`, `globals()`, `importlib`—flagged via `uncertainty`), runtime monkeypatching (`unittest.mock.patch`), or import-time I/O side effects (e.g., module-level `sqlite3.connect()`).
> - **Language scope:** Full AST codemods (`decouple`, `workflow`) and scope preflight (`check`) are **Python-only**. TypeScript/JS supports AST tokenization, skeleton digests, and circular import cycle detection. Kotlin/Java, C/C++, Go, Rust, and Dart support fast Architectural Digest (`digest`) skeleton generation.

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

### 1. Feature Implementation (`arch-sidecar feature`)
Delivers 1-indexed target function bodies, cross-file caller dependencies, and companion test suites in <300ms, eliminating 65% of agent turns. Add `--verify` to run compiler-grade scope preflight and automated pytest verification.

### 2. Deterministic Refactoring (`arch-sidecar workflow`)
End-to-end refactoring pipeline: plans context, audits dynamic reflection sites, checks circular import hazards, extracts AST symbols, verifies pytest suite, and executes atomic transactional rollback if tests fail.

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

---

## 🌐 Language Support & Capabilities Scope

Devrith is built to be an honest, evidence-backed tool. We differentiate between **Tier 1 (Full Capabilities)** and **Tier 2 (Architectural Orientation)**:

| Language | Architectural Digest (`digest`) | Junk / Build Pruning | Fast-Path Feature Workflow (`feature`) | Deterministic Codemods (`workflow`) | Scope & Import DAG Preflight (`check`) | Support Tier |
|:---|:---:|:---:|:---:|:---:|:---:|:---:|
| **Python** (`.py`) | **✅ Full** (<100ms) | **✅ Full** | **✅ Full** (1-indexed + tests) | **✅ Full** ($FV$ Analysis) | **✅ Full** (Sub-15ms) | **Tier 1 (Full Capabilities)** |
| **TypeScript & JS** (`.ts`, `.tsx`, `.js`) | **✅ Full** (<100ms) | **✅ Full** (`node_modules`, `dist`) | 🔄 In Progress | 🔄 v0.4 (Babel/oxc) | 🔄 v0.4 (Babel/oxc) | **Tier 2 (Orientation)** |
| **Kotlin & Java** (`.kt`, `.java`) | **✅ Full** (<100ms) | **✅ Full** (`build`, `.gradle`) | 🔄 Roadmap | 🔄 Roadmap | 🔄 Roadmap | **Tier 2 (Orientation)** |
| **C & C++** (`.c`, `.h`, `.cpp`, `.hpp`) | **✅ Full** (<100ms) | **✅ Full** (`CMakeFiles`, `.o`, `.a`) | 🔄 Roadmap | 🔄 Roadmap | 🔄 Roadmap | **Tier 2 (Orientation)** |
| **Go, Rust & Dart** (`.go`, `.rs`, `.dart`) | **✅ Full** (<100ms) | **✅ Full** (`target`, `.dart_tool`) | 🔄 Roadmap | 🔄 Roadmap | 🔄 Roadmap | **Tier 2 (Orientation)** |

### Why Are Deep Capabilities Python-First?
Deep features (`feature_workflow`, deterministic refactoring, free variable analysis, circular dependency defense) require compiler-grade AST parsing. Python includes a zero-dependency, standard library compiler (`ast`) that allows Devrith to run in <2ms without heavy external binaries. 

Expanding deterministic AST refactoring to TypeScript/JavaScript (via `oxc`/`@babel/parser`) and Go/Rust/JVM is actively planned for v0.4+.

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
