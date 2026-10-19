# `arch-sidecar`
### Architecture-Aware Context Retrieval & Deterministic Refactoring for AI Coding Agents

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-brightgreen.svg)](https://www.python.org/)
[![Model Context Protocol](https://img.shields.io/badge/MCP-Compatible-purple.svg)](https://modelcontextprotocol.io)
[![Tests Passing](https://img.shields.io/badge/tests-9%20passed-success.svg)](tests/)
[![Empirical Case Study](https://img.shields.io/badge/Case%20Study-Documented-orange.svg)](CASE_STUDY.md)

> `arch-sidecar` provides **architecture-aware context retrieval** (`digest` + `locate`) and **deterministic refactoring with pre-flight dependency safety** (`decouple`).
>
> 📖 **Read the empirical benchmark:** [Case Study: Architecture-Aware Retrieval vs. Standard Exploration](CASE_STUDY.md)

---

## 🥊 Capability & Differentiation Matrix

| Capability | Aider (Repo-Map) | Codemap | CALM | GritQL / Codemod | **`arch-sidecar`** |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Primary Focus** | Read-path context | Read-path index | Read-path map + hash locks | Human batch migrations | **Agent refactoring infrastructure** |
| **Token Reduction** | ~80% | ~90% | ~85% | N/A | **94% – 96%** |
| **Architectural Digest** | ✅ Yes | ✅ Yes | ✅ Yes | ❌ No | **✅ Yes (< 100ms AST skeleton)** |
| **Surgical Symbol Context** | Partial | ✅ Yes | ✅ Yes | ❌ No | **✅ Yes (~500 tokens)** |
| **Automated Code Extraction** | ❌ LLM rewrites file | ❌ Not supported | ❌ Not supported | ⚠️ Human-written DSL rules | **✅ Deterministic AST Codemod** |
| **Extraction Latency** | 30–60s (LLM inference) | N/A | N/A | N/A | **1.8ms – 16ms (Local AST)** |
| **LLM Tokens Burned on Refactor** | 5,000 – 15,000 tokens | N/A | N/A | 0 | **0 LLM Tokens** |
| **Circular Import Defense** | ❌ Not verified | ❌ Not verified | ⚠️ Risk report | ❌ Not verified | **✅ Pre-flight Intercept ($FV$)** |
| **Scope Shadowing Analysis** | ❌ Not verified | ❌ Not verified | ❌ Not verified | ❌ Not verified | **✅ Compiler-Grade $FV = \text{Loaded} \setminus \text{Bound}$** |
| **Transactional Rollback** | ❌ No | ❌ No | ❌ No | ❌ No | **✅ Auto-restore on test failure** |
| **Backward-Compatibility Aliasing**| ❌ No | ❌ No | ❌ No | ❌ No | **✅ Automated (`Symbol = mod.Symbol`)** |

---

## ⚡ The 3 Core Tools

### 1. `get_architecture_digest(root_dir=".", max_tokens=4000)`
Delivers a 1,500-token AST skeleton digest in **< 100ms**, automatically pruning build artifacts (`dist/`, `node_modules/`, `.git/`, minified bundles, lockfiles). Prevents agents from choking on 50M+ tokens of compiled JavaScript.

### 2. `locate_surgical_context(task, root_dir=".")`
Uses AST dependency tracking to return only the exact function bodies, decorators, imports, and cross-repo callers/test references (~500 tokens) without opening full files.

### 3. `prefactor_decouple(symbol, from_file, to_file, run_tests="...")`
Deterministic AST codemod that moves classes or functions in **< 15ms for 0 LLM tokens**:
- **Free Variable Analysis:** Implements $FV(E) = \text{Loaded}(E) \setminus \text{Bound}(E)$ to differentiate function parameters from module globals.
- **Circular Import Interceptor:** Stops refactoring pre-flight if extracted code references remaining local functions or classes in the source module.
- **Global State Protection:** Blocks un-extracted module globals and constants to prevent runtime `NameError`s.
- **Transactional Rollback:** If headless test execution fails, restores both source and target files atomically to their exact pre-refactor state.
- **Backward-Compatibility Aliasing:** Automatically injects re-export aliases in the source module so existing unit tests and callers don't break.

---

## 📊 Empirical Multi-Repo Benchmarks

Tested across 5 production and open-source codebases ([`benchmark_results.json`](benchmark_results.json)):

| Repository | Tech Stack | Raw Code Tokens | `arch-sidecar` Digest | **Token Savings** | Digest Latency | Codemod Latency |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: |
| **`psf/requests`** | Python (Core Lib) | 122,643 | **5,970** | **95.1%** | 84.2 ms | 16.08 ms |
| **`pallets/flask`** | Python (Framework) | 151,424 | **5,976** | **96.1%** | 99.3 ms | 3.36 ms |
| **`anime-cli`** | Python + React | 164,378 | **6,006** | **96.3%** | 133.3 ms | 2.91 ms |
| **`gupta-traders`** | TypeScript + React | 37,009 | **2,059** | **94.4%** | 78.4 ms | N/A |
| **`agent-calling`** | Python + MCP | 27,597 | **2,946** | **89.3%** | 28.3 ms | N/A |

> **Real-World Safety Intercept on `psf/requests`:** Attempting to extract `Session` out of `sessions.py` was intercepted in **7.12 ms** because `Session` relies on local `SessionRedirectMixin`, `merge_setting`, and `merge_hooks`. The engine flagged the exact circular hazard and recommended extracting the 4-symbol cluster, which completed in **16.08 ms** with 100% syntax validity.

---

## 🚀 Quickstart

### Installation
```bash
git clone https://github.com/Hackedghost64/arch-sidecar.git
cd arch-sidecar
pip install -e .
```

### Try the Interactive Demo (Takes 5 seconds)
```bash
python3 examples/demo.py
```

### Run Multi-Repo Benchmarks
```bash
python3 benchmark.py
```

### Run Unit Tests
```bash
pytest tests/
```

---

## 🔌 Connecting to Any AI Coding Agent via MCP

Add to your Model Context Protocol (MCP) configuration (`claude_desktop_config.json`, Cursor, Antigravity, or Windsurf):

```json
{
  "mcpServers": {
    "arch-sidecar": {
      "command": "arch-sidecar",
      "args": ["serve"]
    }
  }
}
```

Once connected, your agent gains access to:
- `get_architecture_digest`
- `locate_surgical_context`
- `prefactor_decouple`

---

## 📜 License
MIT License. Free to use in commercial and open-source workflows.
