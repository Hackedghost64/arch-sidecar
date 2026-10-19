# Devrith Quickstart & Onboarding Guide
**Zero to architecture-aware agent in under 5 minutes.**

Devrith (`arch-sidecar`) acts as an architectural co-processor for AI coding agents (Claude Code, Cursor, Codex, Gemini/Antigravity). It eliminates expensive trial-and-error exploration loops by providing 1-indexed targeted context, dependency blast radius, compiler-grade preflight checks, and verifiable execution ledgers.

---

## 1. Installation

### From Source (Current Beta)
```bash
git clone https://github.com/divyamm/arch-sidecar.git
cd arch-sidecar
pip install -e .
```

Verify the installation:
```bash
arch-sidecar --version
# arch-sidecar 0.3.0 (Devrith Engine)
```

---

## 2. Connect to Your AI Coding Agent

Devrith exposes a native Model Context Protocol (MCP) server so your agent can automatically call architectural tools.

### Claude Code CLI
```bash
claude mcp add devrith arch-sidecar serve
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

### A. Implementing Features (`feature_workflow`)
Instead of having an agent wander through 10+ files with `view_file` and `grep`:

```bash
# 1. Orientation: Returns targeted 1-indexed source & companion test context in <300ms
arch-sidecar feature "Add write_table method to HelpFormatter in formatting.py" /path/to/repo

# 2. Verification: Runs AST scope/import preflight and targeted pytest suite
arch-sidecar feature "Add write_table method to HelpFormatter in formatting.py" /path/to/repo --verify
```

### B. Safe Refactoring & Symbol Extraction (`workflow`)
Deterministic AST symbol relocation with transactional rollback on test failure:

```bash
arch-sidecar workflow "Extract HelpFormatter out of formatting.py into _formatter.py" /path/to/repo \
  --from src/click/formatting.py \
  --to src/click/_formatter.py \
  --symbols HelpFormatter,measure_table \
  --test "pytest tests/test_formatting.py"
```

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

To maintain complete transparency:
- **Full Capabilities (Tier 1 — Full Support):** Currently **Python (`.py`)** is the primary language with full end-to-end capabilities:
  - 1-indexed targeted context & companion test fast-paths (`feature_workflow`).
  - Deterministic AST symbol decoupling without burning LLM tokens (`workflow`).
  - Free Variable mathematical analysis ($FV = \text{Loaded} \setminus \text{Bound}$).
  - Circular import cycle detection and prevention.
  - Sub-15ms lexical scope and hallucinated identifier preflight checks (`check`).
  - Transactional rollback on test failure.
- **Architectural Orientation (Tier 2 — Skeleton & Digest):** **TypeScript, JavaScript, Kotlin, Java, C, C++, Go, Rust, and Dart** have full Architectural Digest support (`arch-sidecar digest` / `get_architecture_digest`), providing sub-100ms structural signature outlines while aggressively pruning `node_modules/`, `build/`, `dist/`, `CMakeFiles/`, `.gradle/`, and minified files.

