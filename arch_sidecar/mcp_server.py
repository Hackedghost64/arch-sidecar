import sys
import json
import os
import traceback
from typing import Dict, Any, Optional

from .digest import generate_digest
from .context import locate_surgical_context
from .decouple import prefactor_decouple

SERVER_NAME = "arch-sidecar"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
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
    }
]

def handle_tool_call(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    try:
        if name == "get_architecture_digest":
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
    # Read messages from stdin line by line
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
            # Notification does not return a response
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
