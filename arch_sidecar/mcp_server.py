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
from .cluster import (
    build_repository_dependency_graph,
    partition_into_subsystems,
    predict_change_impact,
)
from .quality import audit_file, audit_repository, QualityReport
from .session_logger import (
    generate_session_summary,
    format_session_report_markdown,
    format_session_report_terminal,
    clear_sessions,
)

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
                },
                "share_state": {
                    "type": "boolean",
                    "description": "If true, automatically binds shared mutable globals and host module symbols across modules",
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
                "share_state": {
                    "type": "boolean",
                    "description": "If true, automatically binds shared mutable globals and host module symbols across modules",
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
    },
    {
        "name": "get_session_report",
        "description": "View real-world test and execution telemetry report collected automatically by Devrith. Returns aggregate metrics (runs count, success rate, commands used, tests passed, preflight catches) and recent session details across all projects or filtered by repository.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_root": {
                    "type": "string",
                    "description": "Optional repository path to filter sessions by (default: None for all projects)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of recent sessions to display (default: 20)",
                    "default": 20
                },
                "format": {
                    "type": "string",
                    "enum": ["markdown", "json", "terminal"],
                    "description": "Output format (default: 'markdown')",
                    "default": "markdown"
                }
            }
        }
    },
    {
        "name": "export_test_report",
        "description": "Generate and save a real-world test and benchmark report to a Markdown file on disk (e.g. REAL_WORLD_REPORT.md).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "output_path": {
                    "type": "string",
                    "description": "File path to save the report to (default: 'REAL_WORLD_REPORT.md')",
                    "default": "REAL_WORLD_REPORT.md"
                },
                "project_root": {
                    "type": "string",
                    "description": "Optional repository path to filter sessions by (default: None for all projects)"
                }
            },
            "required": ["output_path"]
        }
    },
    {
        "name": "get_subsystem_clusters",
        "description": "Architectural Subsystem Cluster Map: Discovers repository subsystem clusters, cohesion, instability, and coupling metrics ($Ca, Ce, I$) across Python and TypeScript/JS.",
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
        "name": "predict_change_impact",
        "description": "Pre-Change Architectural Impact Predictor: Predicts change blast radius, downstream callers, crossed subsystem boundaries, and test recommendations before modifying code.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Target file or symbol name to analyze (e.g. 'options.py' or 'Model')"
                },
                "root_dir": {
                    "type": "string",
                    "description": "Path to the repository root directory (default: '.')",
                    "default": "."
                }
            },
            "required": ["target"]
        }
    },
    {
        "name": "audit_code_quality",
        "description": "Code Craft & OOP Sentry: Audits code quality, OOP cohesion, nesting depth, anti-patterns (silent swallows, mutable defaults, parameter mutations), and contract return consistency.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target_path": {
                    "type": "string",
                    "description": "Path to Python file or repository directory to audit (default: '.')",
                    "default": "."
                },
                "strict": {
                    "type": "boolean",
                    "description": "If true, flags any warnings as failures (zero-tolerance mode)",
                    "default": False
                }
            }
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

        elif name == "get_session_report":
            project_root = arguments.get("project_root")
            limit = arguments.get("limit", 20)
            fmt = arguments.get("format", "markdown")
            summary = generate_session_summary(project_root=project_root, limit=limit)
            if fmt == "json":
                out_text = json.dumps(summary, indent=2, default=str)
            elif fmt == "terminal":
                out_text = format_session_report_terminal(summary)
            else:
                out_text = format_session_report_markdown(summary, project_filter=project_root)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": out_text
                    }
                ],
                "isError": False
            }

        elif name == "export_test_report":
            output_path = arguments["output_path"]
            project_root = arguments.get("project_root")
            summary = generate_session_summary(project_root=project_root)
            md_report = format_session_report_markdown(summary, project_filter=project_root)
            os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(md_report)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Successfully exported Devrith test report to {output_path} ({summary.get('total_sessions')} sessions recorded)."
                    }
                ],
                "isError": False
            }

        elif name == "get_subsystem_clusters":
            root_dir = arguments.get("root_dir", ".")
            graph = build_repository_dependency_graph(root_dir)
            clusters = partition_into_subsystems(graph, root_dir)
            payload = {
                "total_modules": len(graph.modules),
                "total_dependencies": sum(len(deps) for deps in graph.edges.values()),
                "total_clusters": len(clusters),
                "clusters": [
                    {
                        "id": c.cluster_id,
                        "name": c.name,
                        "modules_count": len(c.modules),
                        "modules": c.modules[:15],
                        "cohesion": c.cohesion,
                        "afferent_coupling": c.afferent_coupling,
                        "efferent_coupling": c.efferent_coupling,
                        "instability": c.instability,
                    }
                    for c in clusters
                ]
            }
            return {
                "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
                "isError": False
            }

        elif name == "predict_change_impact":
            target = arguments["target"]
            root_dir = arguments.get("root_dir", ".")
            graph = build_repository_dependency_graph(root_dir)
            clusters = partition_into_subsystems(graph, root_dir)
            impact = predict_change_impact(target, graph, clusters, root_dir)
            payload = {
                "target": target,
                "target_file": impact.target_file,
                "direct_callers_count": len(impact.direct_callers),
                "direct_callers": impact.direct_callers,
                "transitive_blast_radius_count": len(impact.transitive_blast_radius),
                "transitive_blast_radius": impact.transitive_blast_radius[:25],
                "affected_clusters": impact.affected_clusters,
                "recommended_test_files": impact.recommended_test_files,
                "risk_level": impact.risk_level
            }
            return {
                "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
                "isError": False
            }

        elif name == "audit_code_quality":
            target_path = arguments.get("target_path", ".")
            strict = arguments.get("strict", False)
            abs_target = os.path.abspath(target_path)
            if os.path.isfile(abs_target):
                issues = audit_file(abs_target)
                report = QualityReport(
                    total_files=1,
                    total_issues=len(issues),
                    error_count=sum(1 for i in issues if i.severity == "error"),
                    warning_count=sum(1 for i in issues if i.severity == "warning"),
                    clean=(len(issues) == 0 if strict else not any(i.severity == "error" for i in issues)),
                    score=max(0.0, 100.0 - sum(10 if i.severity == "error" else 2 for i in issues)),
                    issues=issues,
                    metrics={
                        "categories": {
                            "complexity": sum(1 for i in issues if i.category == "complexity"),
                            "anti_pattern": sum(1 for i in issues if i.category == "anti_pattern"),
                            "oop_cohesion": sum(1 for i in issues if i.category == "oop_cohesion"),
                            "contract": sum(1 for i in issues if i.category == "contract"),
                        }
                    }
                )
            else:
                report = audit_repository(abs_target, strict=strict)

            return {
                "content": [{"type": "text", "text": json.dumps(report.to_dict(), indent=2)}],
                "isError": False
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
