# Universal Context & Decoupling Engine (`arch-sidecar`)

A Universal Architectural Sidecar and Model Context Protocol (MCP) Server that plugs into any AI agent (**Antigravity**, **Cursor**, **Claude Code**, **Codex**, or **Windsurf**).

Instead of agents burning 100k tokens grepping raw directories, choking on `dist/` or `node_modules/`, and hallucinating code during multi-file refactoring, `arch-sidecar` acts as the deterministic middleware engine.

---

## ⚡ The 3 Agent Superpowers

### 1. `get_architecture_digest(root_dir=".", max_tokens=4000)`
- **Problem solved:** Agents run 10 `ls` and `read_file` commands, read minified React bundles, and burn 60,000+ tokens just learning project structure.
- **Sidecar solution:** Delivers a 1,500-token AST skeleton digest in < 50ms, automatically ignoring build junk (`dist/`, `node_modules/`, `.git/`, minified bundles, lockfiles).

### 2. `locate_surgical_context(task, root_dir=".")`
- **Problem solved:** Agents open 6 whole files trying to understand how a class or function works.
- **Sidecar solution:** Uses AST dependency tracking to return only the exact function bodies, decorators, imports, and cross-repo callers/test references (~500 tokens).

### 3. `prefactor_decouple(symbol, from_file, to_file, run_tests="...")`
- **Problem solved:** Asking an LLM to split an 800-line file takes 45 seconds, risks syntax hallucination, and breaks imports.
- **Sidecar solution:** Deterministic AST codemod moves the target class/function in 20ms for **0 LLM tokens**, automatically carries over required imports, inserts backwards-compatibility aliases, and runs headless test verification (`pytest`).

---

## 🚀 Quickstart & Installation

```bash
cd projects/arch-sidecar
pip install -e .
```

### CLI Usage

```bash
# Generate architecture digest
arch-sidecar digest /path/to/project --max-tokens 2000

# Locate surgical context for a task
arch-sidecar locate "SleepInhibitor" /path/to/project

# Deterministically decouple symbol into a new module & run verification tests
arch-sidecar decouple SleepInhibitor cli.py system.py --root /path/to/project --test "pytest tests/test_inhibitor_and_server.py"
```

### Connecting to Any AI Agent via MCP

Add to your MCP configuration (Claude Code `claude_desktop_config.json`, Cursor `settings.json`, or Antigravity):

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
