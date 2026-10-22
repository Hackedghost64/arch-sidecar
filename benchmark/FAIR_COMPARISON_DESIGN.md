# Phase 2: Fair Comparison Design & Competitor Matrix

**Date:** 2026-10-22  
**Evaluation Rig:** Antigravity Automated Benchmark Harness  
**Platform:** Linux Ubuntu (x86_64, Python 3.10.12, Node v22.22.2, uv 0.12.0)

---

## 1. Evaluated Configurations (The 5 Arms)

To objectively evaluate whether Devrith (`arch-sidecar`) offers a measurable advantage, we evaluate five distinct configurations across identical tasks and repositories:

### Arm A: Baseline Agent
- **Description:** Standard AI coding agent using primitive text tools: `grep`, regex pattern matching, sequential whole-file or line-range file reads, and standard manual file edits.
- **Verification Method:** Manual post-edit `pytest` runs.
- **Hypothesis:** Cheap for simple edits on small files (<200 lines); prone to context pollution, repeated re-reads on line shifts, and silent import errors on complex refactors.

### Arm B: Serena (Semantic Code Navigation)
- **Package / Version:** `serena-agent` v2.1.0 (managed via `uvx`)
- **Underlying Technology:** Language Server Protocol (LSP) bridge exposed as an MCP server.
- **Core Operations Supported:**
  - Symbol body definition retrieval (`get_symbol_definition`)
  - Cross-file symbol references (`find_references`)
  - Class inheritance hierarchy inspection
  - LSP-driven atomic rename and document edits
- **Known Boundaries:** Does not provide standalone circular import cycle detection across entire packages without active typecheckers, does not provide architectural subsystem clustering or pre-change coupling metrics.

### Arm C: Repowise (Repository & Code Graph Layer)
- **Package / Version:** `repowise` v0.58.0 (managed via `uvx`)
- **Underlying Technology:** AST code graphs, LanceDB vector index, Tree-sitter language pack, Git co-change history.
- **Core Operations Supported:**
  - `repowise context <symbol>`: Triage cards for files and symbols
  - `repowise symbol <name>`: Symbol body extraction with live-verified line bounds
  - `repowise impacted-tests <target>`: Test file impact mapping
  - `repowise dead-code`: Dead/unused code detection
  - `repowise mcp`: Model Context Protocol server integration
- **Known Boundaries:** Heavy dependency footprint (135 packages, LanceDB 71MB, PyArrow 51MB). Focuses on repository mapping, documentation wikis, and historical metrics; does not perform deterministic AST code extraction (`decouple`) or static lexical scope $FV$ checks.

### Arm D: Devrith Engine (`arch-sidecar`)
- **Package / Version:** `arch-sidecar` v0.3.0
- **Underlying Technology:** Pure Python AST compiler engine, zero external runtime dependencies.
- **Core Operations Supported:**
  - `arch-sidecar feature`: 1-indexed surgical context + caller dependencies (<300ms)
  - `arch-sidecar check`: Compiler-grade lexical scope ($FV = \text{Loaded} \setminus \text{Bound}$) + cycle preflight (<15ms)
  - `arch-sidecar decouple`: Deterministic AST extraction + import relocation + alias injection
  - `arch-sidecar workflow`: Transactional refactoring with automatic rollback on test failure
  - `arch-sidecar cluster` & `impact`: Whole-repository subsystem clustering and blast radius prediction
- **Known Boundaries:** Deep AST codemods and lexical checks are Python-only; dynamic reflection is audited, not resolved.

### Arm E: Serena + Devrith (Composite Arm)
- **Description:** Hybrid workflow leveraging Serena's LSP semantic symbol navigation for broad exploratory discovery, combined with Devrith's sub-15ms static preflight verification and deterministic AST extraction with test rollback.
- **Hypothesis:** Evaluates whether Devrith replaces or complements LSP-based navigation tools.

---

## 2. Competitor Verification & Parity Matrix

| Capability / Operation | Arm A (Baseline) | Arm B (Serena v2.1.0) | Arm C (Repowise v0.58.0) | Arm D (Devrith v0.3.0) | Arm E (Serena + Devrith) |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Startup / Installation Overhead** | None (Builtin) | Moderate (`uvx`, 74 pkgs) | Heavy (`uvx`, 135 pkgs, ~200MB) | Zero (Pure stdlib, 0 pkgs) | Moderate (`uvx` + Devrith) |
| **Symbol Definition Context** | ❌ Grep / line scan | ✅ LSP semantic lookup | ✅ Tree-sitter symbol | ✅ Fast AST surgical slice | ✅ LSP + AST fast slice |
| **Cross-File Caller Discovery** | ⚠️ Text grep (fragile) | ✅ Exact LSP references | ✅ AST code graph | ✅ Fast AST reference map | ✅ LSP exact references |
| **Subsystem Clustering & Instability**| ❌ None | ❌ Unsupported | ⚠️ Co-change git graph | ✅ AST dependency graph & metrics | ✅ AST dependency graph & metrics |
| **Deterministic Code Extraction** | ❌ LLM rewrites file | ⚠️ LSP atomic renames | ❌ Unsupported (read-only) | ✅ Deterministic AST codemod | ✅ Deterministic AST codemod |
| **Circular Import Interception** | ❌ None | ⚠️ Requires full pyright run | ❌ Not verified preflight | ✅ Sub-15ms DAG cycle intercept | ✅ Sub-15ms DAG cycle intercept |
| **Lexical Scope Preflight ($FV$)** | ❌ None | ❌ Relies on LSP diagnostics | ❌ None | ✅ Compiler $FV = \text{Loaded} \setminus \text{Bound}$ | ✅ Compiler $FV = \text{Loaded} \setminus \text{Bound}$ |
| **Pre-Change Impact / Blast Radius** | ❌ Manual search | ⚠️ Find references list | ✅ `impacted-tests` tool | ✅ `impact` BFS blast radius | ✅ Combined impact |
| **Transactional Rollback Gate** | ❌ Manual `git checkout`| ❌ None | ❌ None | ✅ Automated on test failure | ✅ Automated on test failure |

---

## 3. Experimental Controls & Fairness Rules

To ensure strict fairness across all five arms:
1. **Identical Underlying Agent & Model:** All agent arms operate using the exact same LLM model (e.g. Gemini 2.5 Flash / Flash Lite or Claude 3.5 Sonnet) and context window limits.
2. **Identical Repository Revisions:**
   - `psf/requests`: Commit `611c6162cbc4ac2020a2f91c7cfa4f3abf9bbb60`
   - `pallets/click`: Commit `2247b35ea1c47c727d7a06e51fa280e12a863ff6`
   - `django/django`: Commit `79502d08405dbf24efa39e8731320f31035b9e9c`
3. **Identical Success & Verification Criteria:**
   - For safe extractions: Transformed code must pass syntax check, lexical scope verification, and the exact companion test suite (`pytest` or `runtests.py`) without broken imports or regressions.
   - For unsafe extractions: The tool must identify the specific hazard (circular import, mutable global desync, or missing state) and prevent silent corruption.
   - For context queries: Evaluated on token efficiency (total tokens consumed) and precision (presence of target symbols without irrelevant file bloat).
4. **Isolated Worktrees:** All refactoring operations run in temporary git worktrees or throwaway file copies to prevent contamination between arms.
5. **No Synthetic Shortcuts:** When a tool lacks a capability (e.g. Repowise lacks code extraction), it is reported as `unsupported` rather than silently replaced by manual LLM edits.
