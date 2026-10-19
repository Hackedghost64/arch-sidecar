import sys
import json
import os
import traceback
from typing import Dict, Any, Optional

from .digest import generate_digest
from .context import locate_surgical_context
from .decouple import prefactor_decouple
from .planner import plan_adaptive_retrieval
from .uncertainty import scan_codebase_uncertainty
from .workflow import run_workflow
from .guard import check_code_health
from .feature_workflow import run_feature_workflow

SERVER_NAME = "arch-sidecar"
SERVER_VERSION = "0.3.0"
PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "feature_workflow",
        "description": "Evidence-Backed Feature Implementation Workflow ('Understand before changing. Check before trusting. Verify before claiming success.'): (1) Understands requested feature intent, (2) Finds relevant existing anchor implementations, (3) Maps cross-file dependencies, callers, and test files, (4) Produces an actionable plan, (5) Supplies 1-indexed fast-path context, and when verify=True, (6) Runs compiler-grade preflight checks, (7) Executes test verification, and (8) Reports an explicit Verification Ledger of what is and isn't verified.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Feature request description"
                },
                "project_root": {
                    "type": "string",
                    "description": "Path to repository root or target file (default: '.')",
                    "default": "."
                },
                "target_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of target files to focus on or verify"
                },
                "target_symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of symbol names to focus on"
                },
                "verify": {
                    "type": "boolean",
                    "description": "If true, runs preflight checks + test verification and returns the Verification Ledger",
                    "default": False
                },
                "test_command": {
                    "type": "string",
                    "description": "Optional test command to run when verify=True (auto-discovered if omitted)"
                },
                "token_budget": {
                    "type": "integer",
                    "description": "Maximum token budget for context (default: 3200)",
                    "default": 3200
                }
            },
            "required": ["task"]
        }
    },
    {
        "name": "plan_adaptive_retrieval",
        "description": "Task-Aware Adaptive Retrieval Engine: Dynamically selects whole-file (for small files <120 lines), targeted symbols, symbols + dependencies, or symbols + callers, with candidate ranking justifications, token budget limits, and uncertainty classification (Resolved vs Potential vs Unknown).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Task description or target symbols/files (e.g. 'Extract SleepInhibitor and handle tunnel in cli.py')"
                },
                "root_dir": {
                    "type": "string",
                    "description": "Path to the repository root directory (default: '.')",
                    "default": "."
                },
                "target_symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of exact symbol names to focus on"
                },
                "token_budget": {
                    "type": "integer",
                    "description": "Maximum token budget for returned context (default: 3000)",
                    "default": 3000
                },
                "strategy_override": {
                    "type": "string",
                    "enum": ["auto", "whole_file", "targeted_symbols", "symbols_and_dependencies", "symbols_and_callers", "ast_skeleton"],
                    "description": "Optional strategy override (default: 'auto')",
                    "default": "auto"
                }
            },
            "required": ["task"]
        }
    },
    {
        "name": "get_architecture_digest",
        "description": "Instant Context: Generates a high-density, token-efficient (~1,500 token) AST skeleton of the entire codebase. Automatically filters out junk, dist, node_modules, and minified bundles.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "root_dir": {
                    "type": "string",
                    "description": "Path to the repository root directory (default: '.')",
                    "default": "."
                },
                "max_tokens": {
                    "type": "integer",
                    "description": "Maximum token budget for the skeleton digest (default: 4000)",
                    "default": 4000
                }
            }
        }
    },
    {
        "name": "locate_surgical_context",
        "description": "Zero Multi-File Guessing: Locates the exact class and function bodies, dependencies, and external test references needed for a task, returning ~500 tokens of surgical context without reading bloated files.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Task description or target symbol names (e.g. 'Extract SleepInhibitor from cli.py')"
                },
                "root_dir": {
                    "type": "string",
                    "description": "Repository root path (default: '.')",
                    "default": "."
                },
                "target_symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of exact symbol names to extract context for"
                }
            },
            "required": ["task"]
        }
    },
    {
        "name": "scan_uncertainty",
        "description": "Dynamic Reflection & Uncertainty Scanner: Audits Python AST for unresolvable runtime behavior (getattr, setattr, importlib, eval, exec) and classifies confidence as Resolved, Potential, or Unknown.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "root_dir": {
                    "type": "string",
                    "description": "Path to the repository root directory (default: '.')",
                    "default": "."
                }
            }
        }
    },
    {
        "name": "prefactor_decouple",
        "description": "Deterministic Code Splitting: Surgical AST codemod that moves classes or functions from one file to another in 50ms, resolves and moves required imports, adds backward-compatibility aliases, and verifies tests.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "description": "Single symbol name or list of symbol names to decouple",
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}}
                    ]
                },
                "from_file": {
                    "type": "string",
                    "description": "Source file to extract symbol from (e.g. 'cli.py')"
                },
                "to_file": {
                    "type": "string",
                    "description": "Target module to move symbol into (e.g. 'system.py')"
                },
                "project_root": {
                    "type": "string",
                    "description": "Project root directory (default: '.')",
                    "default": "."
                },
                "run_tests": {
                    "type": "string",
                    "description": "Optional test command to run for verification gate (e.g. 'pytest tests/test_inhibitor_and_server.py')"
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, simulates the AST split and returns the diff without modifying files",
                    "default": False
                }
            },
            "required": ["symbol", "from_file", "to_file"]
        }
    },
    {
        "name": "execute_refactor_workflow",
        "description": "End-to-End Orchestrated Refactoring Engine: Executes the complete Task -> Context Plan -> Reflection Audit -> Dependency Preflight -> Deterministic AST Transformation -> Test Execution & Rollback pipeline. Changes are test-gated and rolled back on failure; correctness is only as strong as the supplied test command.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Task description (e.g. 'Decouple SleepInhibitor from cli.py into system.py')"
                },
                "project_root": {
                    "type": "string",
                    "description": "Path to the repository root directory (default: '.')",
                    "default": "."
                },
                "from_file": {
                    "type": "string",
                    "description": "Source file (optional; deduced from task/plan if omitted)"
                },
                "to_file": {
                    "type": "string",
                    "description": "Target file (optional; deduced from task/plan if omitted)"
                },
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of exact symbol names to refactor"
                },
                "test_command": {
                    "type": "string",
                    "description": "Test command to run for verification gate (e.g. 'pytest tests/test_system.py')"
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, simulates transformations without modifying disk",
                    "default": False
                },
                "token_budget": {
                    "type": "integer",
                    "description": "Max token budget for context planning (default: 3000)",
                    "default": 3000
                }
            },
            "required": ["task"]
        }
    },
    {
        "name": "check_code_health",
        "description": "Compiler-Grade Preflight & Architecture Health Guard: Sub-15ms static verification that detects syntax errors, undefined/hallucinated symbols, broken local imports, and circular import cycles before running tests.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target_path": {
                    "type": "string",
                    "description": "Path to the Python file to verify"
                },
                "project_root": {
                    "type": "string",
                    "description": "Project root directory (default: '.')",
                    "default": "."
                }
            },
            "required": ["target_path"]
        }
    }
]

def handle_tool_call(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    try:
        if name == "feature_workflow":
            task = arguments["task"]
            project_root = arguments.get("project_root", ".")
            target_files = arguments.get("target_files")
            target_symbols = arguments.get("target_symbols")
            verify = arguments.get("verify", False)
            test_command = arguments.get("test_command")
            token_budget = arguments.get("token_budget", 3200)

            res = run_feature_workflow(
                task=task,
                project_root=project_root,
                target_files=target_files,
                target_symbols=target_symbols,
                verify=verify,
                test_command=test_command,
                token_budget=token_budget,
            )
            text_out = json.dumps(res, indent=2) if verify else res["context"]
            return {
                "content": [
                    {
                        "type": "text",
                        "text": text_out
                    }
                ],
                "isError": not res.get("success", False)
            }

        elif name == "plan_adaptive_retrieval":
            task = arguments["task"]
            root_dir = arguments.get("root_dir", ".")
            target_symbols = arguments.get("target_symbols")
            token_budget = arguments.get("token_budget", 3000)
            strategy_override = arguments.get("strategy_override")
            if strategy_override == "auto":
                strategy_override = None

            res = plan_adaptive_retrieval(
                task=task,
                root_dir=root_dir,
                target_symbols=target_symbols,
                token_budget=token_budget,
                strategy_override=strategy_override
            )
            return {
                "content": [
                    {
                        "type": "text",
                        "text": res["context"]
                    }
                ],
                "isError": False
            }

        elif name == "get_architecture_digest":
            root_dir = arguments.get("root_dir", ".")
            max_tokens = arguments.get("max_tokens", 4000)
            res = generate_digest(root_dir=root_dir, max_tokens=max_tokens)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": res["digest"]
                    }
                ],
                "isError": False
            }

        elif name == "locate_surgical_context":
            task = arguments["task"]
            root_dir = arguments.get("root_dir", ".")
            target_symbols = arguments.get("target_symbols")
            res = locate_surgical_context(task=task, root_dir=root_dir, target_symbols=target_symbols)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": res["context"]
                    }
                ],
                "isError": False
            }

        elif name == "scan_uncertainty":
            root_dir = arguments.get("root_dir", ".")
            res = scan_codebase_uncertainty(root_dir=root_dir)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(res, indent=2)
                    }
                ],
                "isError": False
            }

        elif name == "prefactor_decouple":
            symbol = arguments["symbol"]
            from_file = arguments["from_file"]
            to_file = arguments["to_file"]
            project_root = arguments.get("project_root", ".")
            run_tests = arguments.get("run_tests")
            dry_run = arguments.get("dry_run", False)

            res = prefactor_decouple(
                symbol=symbol,
                from_file=from_file,
                to_file=to_file,
                project_root=project_root,
                run_tests=run_tests,
                dry_run=dry_run
            )
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(res, indent=2)
                    }
                ],
                "isError": not res.get("success", False)
            }

        elif name == "execute_refactor_workflow":
            task = arguments["task"]
            project_root = arguments.get("project_root", ".")
            from_file = arguments.get("from_file")
            to_file = arguments.get("to_file")
            symbols = arguments.get("symbols")
            test_command = arguments.get("test_command")
            dry_run = arguments.get("dry_run", False)
            token_budget = arguments.get("token_budget", 3000)

            res = run_workflow(
                task=task,
                project_root=project_root,
                from_file=from_file,
                to_file=to_file,
                symbols=symbols,
                test_command=test_command,
                dry_run=dry_run,
                token_budget=token_budget
            )
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(res, indent=2)
                    }
                ],
                "isError": not res.get("success", False)
            }

        elif name == "check_code_health":
            target_path = arguments["target_path"]
            project_root = arguments.get("project_root", ".")
            res = check_code_health(target_path=target_path, project_root=project_root)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(res, indent=2)
                    }
                ],
                "isError": not res.get("success", False)
            }

        else:
            return {
                "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
                "isError": True
            }

    except Exception as e:
        tb = traceback.format_exc()
        return {
            "content": [{"type": "text", "text": f"Error executing {name}: {str(e)}\n{tb}"}],
            "isError": True
        }

def run_mcp_server():
    """Runs standard stdio JSON-RPC MCP server."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params", {})

        response: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": req_id
        }

        if method == "initialize":
            response["result"] = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {
                        "listChanged": False
                    }
                },
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION
                }
            }
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            response["result"] = {
                "tools": TOOLS
            }
        elif method == "tools/call":
            tool_name = params.get("name")
            tool_args = params.get("arguments", {})
            response["result"] = handle_tool_call(tool_name, tool_args)
        elif method == "ping":
            response["result"] = {}
        else:
            response["error"] = {
                "code": -32601,
                "message": f"Method not found: {method}"
            }

        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()

if __name__ == "__main__":
    run_mcp_server()
