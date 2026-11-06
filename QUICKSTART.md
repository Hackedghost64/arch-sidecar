# Devrith Quickstart & Onboarding Guide
**Zero to architecture-aware agent in under 5 minutes.**

Devrith (`arch-sidecar`) acts as an architectural co-processor for AI coding agents (Claude Code, Cursor, Codex, Gemini/Antigravity). It eliminates expensive trial-and-error exploration loops by providing 1-indexed targeted context, dependency blast radius, compiler-grade preflight checks, and verifiable execution ledgers.

---

## 1. Installation & Updates

Devrith can be installed via an automated zero-friction shell installer, modern Python package managers, or directly from source.

### A. One-Liner Install / Update (Linux / macOS / WSL)
Installs or updates both `arch-sidecar` and its short alias `devrith` into `~/.local/bin` using an isolated environment (`uv` or standard Python `venv`):
```bash
curl -fsSL https://raw.githubusercontent.com/Hackedghost64/arch-sidecar/main/install.sh | bash
```

### B. Install a Specific Release Tag (e.g., `v0.4.0`)
```bash
DEVRITH_REF=v0.4.0 curl -fsSL https://raw.githubusercontent.com/Hackedghost64/arch-sidecar/main/install.sh | bash
```

### C. Via `uv`, `pipx`, or `pip`
```bash
# Recommended for uv users
uv tool install git+https://github.com/Hackedghost64/arch-sidecar.git@v0.4.0

# Or via pipx
pipx install git+https://github.com/Hackedghost64/arch-sidecar.git@v0.4.0

# Or via standard pip
pip install --upgrade git+https://github.com/Hackedghost64/arch-sidecar.git@v0.4.0
```

### D. Self-Update Anytime from the CLI
Once installed, Devrith can update itself directly:
```bash
# Check if an update is available without touching your installation
arch-sidecar update --check   # or: devrith update --check

# Self-update to the latest release
arch-sidecar update           # or: devrith update
```

### E. From Source (Development Mode)
```bash
git clone https://github.com/Hackedghost64/arch-sidecar.git
cd arch-sidecar
pip install -e .
```

### Verify the Installation
```bash
arch-sidecar --version
# arch-sidecar 0.4.0 (Devrith Engine)

# Or via the short alias:
devrith --version
# arch-sidecar 0.4.0 (Devrith Engine)
```
> [!NOTE]
> `devrith` and `arch-sidecar` are symlinked aliases to the exact same CLI binary and MCP engine.

---

## 2. Connect to Your AI Coding Agent

Devrith exposes a native Model Context Protocol (MCP) server so your agent can automatically call architectural tools. Either `arch-sidecar serve` or `devrith serve` can be used interchangeably.

### Claude Code CLI
```bash
claude mcp add devrith arch-sidecar serve
# or: claude mcp add devrith devrith serve
```

### Cursor (`~/.cursor/mcp.json` or `.cursor/mcp.json`)
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

### Google Antigravity / Gemini CLI (`agy`)
```bash
agy mcp add devrith arch-sidecar serve
```

---

## 3. The 3 Primary Workflows

### A. Implementing Features (`feature_workflow` / `arch-sidecar feature`)
Instead of having an agent wander through 10+ files with `view_file` and `grep`:

```bash
# 1. Orientation: Returns targeted 1-indexed source & companion test context in <300ms across 6 languages
arch-sidecar feature "Add write_table method to HelpFormatter in formatting.py" /path/to/repo

# 2. Verification: Runs AST scope/import preflight and targeted test suite
arch-sidecar feature "Add write_table method to HelpFormatter in formatting.py" /path/to/repo --verify
```
*Features:*
- **Polyglot Adaptive Retrieval:** First-class surgical context across Python, TypeScript/JS, Go, Rust, Dart/Flutter, and Kotlin/Java.
- **Rare-Term IDF Scoring:** Prioritizes unique domain identifiers with up to `+45` relevance boost while down-weighting generic CRUD verbs (`get`, `set`, `handle`).
- **Cross-Boundary Linking:** Automatically connects backend API endpoints and frontend/mobile files that share HTTP routes or environment variables.

### B. Safe Refactoring & Multi-Symbol Extraction (`workflow` / `decouple`)
Deterministic AST symbol relocation with transactional rollback on test failure:

```bash
# Move one or more symbols atomically across files
arch-sidecar workflow "Extract HelpFormatter out of formatting.py into _formatter.py" /path/to/repo \
  --from src/click/formatting.py \
  --to src/click/_formatter.py \
  --symbols HelpFormatter,measure_table \
  --test "pytest tests/test_formatting.py"
```
*Features:*
- **Atomic Multi-Symbol Extraction:** Relocate multiple symbols (`HelpFormatter,measure_table` or `["fn1", "fn2"]`) in one clean transaction.
- **Hazard Tracking:** Detects `local_dependencies_referenced` (remaining private helpers in the source file) before extraction.
- **Transactional Rollback:** If test gates or preflight checks fail, all modified files are restored to their exact pristine state.

### C. Architectural Impact & Hazard Detection (`plan` / `decouple`)
Detect circular import hazards, dynamic reflection sites (`getattr`, `eval`), and caller blast radius before editing:

```bash
arch-sidecar plan "Decouple dispatch_action from service.py" /path/to/repo
```

---

## 4. The Verification Principle

> **"Understand before changing. Check before trusting. Verify before claiming success."**

When running with `--verify`, Devrith produces a structured **Verification Ledger**:
- **Statically Verified:** Lexical scope, undefined identifiers, unresolved local imports, AST syntax validity.
- **Runtime Verified:** Target test suite execution with exact exit codes and execution times.
- **Unverified / Residual Risks:** Candidly highlights unexercised paths, unmeasured test coverage, and dynamic reflection hazards.

---

## 5. Automated Real-World Session Data & Test Reports

Devrith automatically logs real-world execution telemetry locally (zero external network traffic) to `~/.devrith/sessions.jsonl` and project-level `.devrith/sessions.jsonl`.

### View via Terminal Command
```bash
# Display rich ASCII terminal dashboard
arch-sidecar report

# Output GitHub-flavored Markdown
arch-sidecar report --markdown

# Export a formal test report to a markdown file
arch-sidecar report --export REAL_WORLD_REPORT.md

# View raw JSON telemetry metrics
arch-sidecar report --json
```

### View by Prompting Your AI Agent
Because Devrith's MCP server exposes `get_session_report` and `export_test_report`, you can prompt your connected coding agent (Claude Code, Cursor, Antigravity):
- *"Show me the Devrith real-world test report"*
- *"How many sessions has Devrith executed and what was the pass rate?"*
- *"Export our benchmark test report to REAL_WORLD_REPORT.md"*

The agent will query the local session store and render the live telemetry dashboard directly into the chat!

---

## 6. Language Capabilities Scope

Devrith provides transparent, evidence-backed capabilities across modern languages:

- **Tier 1 (First-Class Polyglot Support):**
  - **Python (`.py`):** 1-indexed targeted context, AST free-variable ($FV$) analysis, circular import defense, AST codemods, scope preflight, and transactional rollback.
  - **TypeScript & JavaScript (`.ts`, `.tsx`, `.js`, `.jsx`):** In-process AST parsing, surgical context extraction, route/environment linking, multi-symbol decoupling, import cycle analysis, and lexical preflight.
  - **Go (`.go`):** AST parsing, surgical function/struct context, route/env linking, import cycle checks, and multi-symbol relocation.
  - **Rust (`.rs`):** In-process syntax analysis, attribute & item extraction, route/env linking, and multi-symbol relocation.
  - **Dart & Flutter (`.dart`):** Surgical context, nearest `pubspec.yaml` discovery, companion test path mapping, and multi-symbol relocation.
  - **Kotlin & Java (`.kt`, `.kts`, `.java`):** AST parsing, surgical class/method context, route/env linking, and multi-symbol relocation.
- **Tier 2 (Architectural Orientation & Digest):**
  - **C & C++ (`.c`, `.h`, `.cpp`, `.hpp`):** Architectural Digest (`arch-sidecar digest` / `get_architecture_digest`), providing sub-100ms structural signature outlines while pruning `CMakeFiles/`, `.o`, `.a`, and build artifacts.

