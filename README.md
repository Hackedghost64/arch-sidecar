# `arch-sidecar`
### Architecture-Aware Context Retrieval & Deterministic Refactoring for AI Coding Agents

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-brightgreen.svg)](https://www.python.org/)
[![Model Context Protocol](https://img.shields.io/badge/MCP-Compatible-purple.svg)](https://modelcontextprotocol.io)
[![Tests Passing](https://img.shields.io/badge/tests-68%20passed-success.svg)](tests/)
[![Empirical Case Study](https://img.shields.io/badge/Case%20Study-Documented-orange.svg)](CASE_STUDY.md)

> `arch-sidecar` (Devrith Engine) provides **architecture-aware context retrieval**, **feature implementation planning**, **deterministic AST refactoring**, and **static scope/import preflight verification** for AI coding agents.
>
> ⚡ **Quickstart Guide:** [5-Minute Onboarding & MCP Setup](QUICKSTART.md)  
> 📖 **Read the empirical benchmarks (wins, losses, & boundaries):** [CASE_STUDY.md](CASE_STUDY.md) · [CASE_STUDY_FLASH.md](CASE_STUDY_FLASH.md) · [BENCHMARK_REPORT](benchmark/FINDINGS.md)

> [!IMPORTANT]
> **Key Results from the Unbiased 60-Module Ground Truth Benchmark (`Click`, `Requests`, `Django`):**
> - **100% Hazard Recall (14/14):** Caught every single breaking circular import, unresolvable symbol, and runtime regression before disk writes or test runs (Wilson 95% CI Lower Bound: **78.5%**).
> - **0 Accepted Regressions:** Zero broken changes escaped to runtime across 60 modules.
> - **97.2x Faster Than PyTest:** Devrith preflight runs in **26.1ms** vs **2,541ms** for the physical PyTest Oracle, at only **177 tokens / op**.
> - **Catches Blind Spots in Unit Tests:** Intercepts real runtime `NameError` bugs that pass unit test suites due to environment monkeypatching (e.g. `requests.utils.get_netrc_auth` missing `NETRC_FILES`).
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

# Strict zero-tolerance mode for CI/CD
arch-sidecar audit src/ --strict
```

### 4. Automated Real-World Test & Telemetry Reporting (`arch-sidecar report`)
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
