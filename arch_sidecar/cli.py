import argparse
import json
import sys
import os

from .digest import generate_digest
from .context import locate_surgical_context
from .decouple import prefactor_decouple
from .mcp_server import run_mcp_server
from .planner import plan_adaptive_retrieval
from .uncertainty import scan_codebase_uncertainty
from .workflow import run_workflow

def main():
    parser = argparse.ArgumentParser(
        prog="arch-sidecar",
        description="Universal Architectural Sidecar & Decoupling Engine (MCP Server + CLI)"
    )
    subparsers = parser.add_subparsers(dest="command", help="Subcommand to execute")

    # Command: workflow (End-to-End Orchestrated Pipeline)
    wf_parser = subparsers.add_parser(
        "workflow",
        help="End-to-End Pipeline: Task -> Context Plan -> Reflection Audit -> Preflight -> Transform -> Test Verification"
    )
    wf_parser.add_argument("task", help="Task description (e.g. 'Decouple SleepInhibitor from cli.py into system.py')")
    wf_parser.add_argument("path", nargs="?", default=".", help="Root directory of the codebase (default: .)")
    wf_parser.add_argument("--from", dest="from_file", help="Source file (optional; deduced from task/plan if omitted)")
    wf_parser.add_argument("--to", dest="to_file", help="Target file (optional; deduced from task/plan if omitted)")
    wf_parser.add_argument("--symbols", "-s", help="Comma-separated symbol names (optional; deduced from task/plan if omitted)")
    wf_parser.add_argument("--test", help="Test command to run for verification gate (e.g. 'pytest tests/test_system.py')")
    wf_parser.add_argument("--dry-run", action="store_true", help="Simulate transformations without modifying disk")
    wf_parser.add_argument("--budget", "-b", type=int, default=3000, help="Max token budget for context planning")
    wf_parser.add_argument("--json", action="store_true", help="Output full JSON telemetry payload")

    # Command: plan (Adaptive Task-Aware Retrieval Engine)
    plan_parser = subparsers.add_parser("plan", help="Task-aware adaptive retrieval: dynamically select whole-file vs AST slice vs callers with candidate ranking and uncertainty analysis")
    plan_parser.add_argument("task", help="Task description or symbol to plan context for")
    plan_parser.add_argument("path", nargs="?", default=".", help="Root directory of the codebase (default: .)")
    plan_parser.add_argument("--budget", "-b", type=int, default=3000, help="Maximum token budget (default: 3000)")
    plan_parser.add_argument(
        "--strategy",
        choices=["auto", "whole_file", "targeted_symbols", "symbols_and_dependencies", "symbols_and_callers", "ast_skeleton"],
        default="auto",
        help="Strategy override (default: auto)"
    )
    plan_parser.add_argument("--symbols", "-s", help="Comma-separated explicit symbol names (optional)")
    plan_parser.add_argument("--json", action="store_true", help="Output full structured JSON plan")

    # Command: uncertainty
    uncert_parser = subparsers.add_parser("uncertainty", help="Scan codebase for dynamic reflection (getattr, importlib, eval, exec)")
    uncert_parser.add_argument("path", nargs="?", default=".", help="Root directory of the codebase (default: .)")
    uncert_parser.add_argument("--json", action="store_true", help="Output findings as JSON")

    # Command: digest
    digest_parser = subparsers.add_parser("digest", help="Generate an AST skeleton digest of a codebase")
    digest_parser.add_argument("path", nargs="?", default=".", help="Root directory of the codebase (default: .)")
    digest_parser.add_argument("--max-tokens", type=int, default=4000, help="Max token budget (default: 4000)")
    digest_parser.add_argument("--output", "-o", help="File path to save the digest (optional)")

    # Command: locate
    locate_parser = subparsers.add_parser("locate", help="Locate surgical context for a specific task")
    locate_parser.add_argument("task", help="Task description or symbol to locate")
    locate_parser.add_argument("path", nargs="?", default=".", help="Root directory of the codebase (default: .)")
    locate_parser.add_argument("--symbols", "-s", help="Comma-separated explicit symbol names (optional)")
    locate_parser.add_argument("--adaptive", action="store_true", help="Use adaptive retrieval planner instead of raw surgical slice")

    # Command: decouple
    decouple_parser = subparsers.add_parser("decouple", help="Deterministically decouple symbols into a new/existing module")
    decouple_parser.add_argument("symbols", help="Comma-separated symbol names to decouple (e.g. SleepInhibitor,start_cloudflared_tunnel)")
    decouple_parser.add_argument("from_file", help="Source file (e.g. cli.py)")
    decouple_parser.add_argument("to_file", help="Target file (e.g. system.py)")
    decouple_parser.add_argument("--root", default=".", help="Project root directory (default: .)")
    decouple_parser.add_argument("--test", help="Test command to run for headless verification (e.g. 'pytest tests/test_inhibitor_and_server.py')")
    decouple_parser.add_argument("--dry-run", action="store_true", help="Simulate AST split and show diff without modifying files")

    # Command: serve (MCP server)
    subparsers.add_parser("serve", help="Run as stdio Model Context Protocol (MCP) server")

    args = parser.parse_args()

    if args.command == "workflow":
        syms_list = [s.strip() for s in args.symbols.split(",")] if args.symbols else None
        res = run_workflow(
            task=args.task,
            project_root=args.path,
            from_file=args.from_file,
            to_file=args.to_file,
            symbols=syms_list,
            test_command=args.test,
            dry_run=args.dry_run,
            token_budget=args.budget
        )
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            tel = res.get("telemetry", {})
            status = tel.get("status", "UNKNOWN")
            stages = " -> ".join(res.get("stages_executed", []))
            print(f"🚀 [arch-sidecar workflow] Pipeline: {res.get('pipeline')} | Status: {status}")
            print(f"   Stages: {stages}")
            print(f"   Total Latency: {tel.get('total_latency_ms', 0)}ms | Total Tokens: ~{tel.get('total_tokens', 0)}")
            
            if res.get("pipeline") == "retrieval_only":
                print(f"\n{res.get('context')}")
            else:
                if res.get("success"):
                    print(f"✅ Extracted: {', '.join(res.get('extracted_symbols', []))}")
                    print(f"   From: {res.get('from_file')} -> To: {res.get('to_file')}")
                    if res.get("diff"):
                        print("\n--- Diff ---")
                        print(res["diff"])
                    tv = res.get("test_verification")
                    if tv:
                        gate = "PASSED" if tv.get("passed") else "FAILED"
                        print(f"\n🧪 Verification Gate: {gate} ({tv.get('command')}) in {tv.get('duration_ms')}ms")
                else:
                    print(f"❌ Workflow Blocked/Failed: {res.get('error')}", file=sys.stderr)
                    if res.get("rolled_back"):
                        print("🔄 Transactional Rollback applied: disk restored to original pristine state.", file=sys.stderr)
                    sys.exit(1)

    elif args.command == "plan":
        syms_list = [s.strip() for s in args.symbols.split(",")] if args.symbols else None
        res = plan_adaptive_retrieval(
            task=args.task,
            root_dir=args.path,
            target_symbols=syms_list,
            token_budget=args.budget,
            strategy_override=args.strategy if args.strategy != "auto" else None
        )
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(res["context"])
            print(
                f"\n# [arch-sidecar plan] Strategy: {res['strategy']} | Budget: {res['token_breakdown']['token_budget']} | "
                f"Context Tokens: ~{res['estimated_tokens']} | Savings vs Whole Codebase: {res['token_breakdown']['savings_vs_codebase_pct']}%",
                file=sys.stderr
            )

    elif args.command == "uncertainty":
        res = scan_codebase_uncertainty(root_dir=args.path)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(f"# Codebase Dynamic Uncertainty Report ({args.path})")
            print(f"Total dynamic reflection points: {res['total_uncertainties']}")
            print(f"  - Potential (literal dynamic calls): {res['potential_count']}")
            print(f"  - Unknown (dynamic variable reflection/eval): {res['unknown_count']}\n")
            for f in res["findings"]:
                icon = "⚠️" if f["confidence"] == "Unknown" else "ℹ️"
                print(f"{icon} {f['file']}:{f['line']} [{f['confidence']} - {f['pattern']}]")
                print(f"   Code: {f['code']}")
                print(f"   Note: {f['description']}\n")

    elif args.command == "digest":
        res = generate_digest(root_dir=args.path, max_tokens=args.max_tokens)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(res["digest"])
            print(f"Digest generated ({res['files_indexed']} files, ~{res['estimated_tokens']} tokens) -> {args.output}")
        else:
            print(res["digest"])
            print(f"\n# Summary: {res['files_indexed']} files indexed, ~{res['estimated_tokens']} estimated tokens.", file=sys.stderr)

    elif args.command == "locate":
        symbols_list = [s.strip() for s in args.symbols.split(",")] if args.symbols else None
        if getattr(args, "adaptive", False):
            res = plan_adaptive_retrieval(task=args.task, root_dir=args.path, target_symbols=symbols_list)
            print(res["context"])
            print(f"\n# Adaptive Strategy: {res['strategy']} | Estimated Tokens: ~{res['estimated_tokens']}", file=sys.stderr)
        else:
            res = locate_surgical_context(task=args.task, root_dir=args.path, target_symbols=symbols_list)
            print(res["context"])
            print(f"\n# Estimated Tokens: ~{res['estimated_tokens']}", file=sys.stderr)

    elif args.command == "decouple":
        sym_list = [s.strip() for s in args.symbols.split(",")]
        res = prefactor_decouple(
            symbol=sym_list,
            from_file=args.from_file,
            to_file=args.to_file,
            project_root=args.root,
            run_tests=args.test,
            dry_run=args.dry_run
        )
        if res.get("success"):
            print(f"✅ Decoupled successfully in {res['execution_time_ms']}ms:")
            print(f"   Symbols: {', '.join(res['extracted_symbols'])}")
            print(f"   From: {res['from_file']}")
            print(f"   To:   {res['to_file']}")
            if res.get("diff"):
                print("\n--- Diff Generated ---")
                print(res["diff"])
            if res.get("test_verification"):
                tv = res["test_verification"]
                status = "PASSED" if tv.get("passed") else "FAILED"
                print(f"\n🧪 Verification Gate ({tv.get('command')}): {status}")
                if tv.get("output"):
                    print(tv["output"])
        else:
            print(f"❌ Decoupling failed: {res.get('error')}", file=sys.stderr)
            if res.get("test_verification"):
                print(f"Test failure output:\n{res['test_verification'].get('output')}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "serve":
        run_mcp_server()

    else:
        if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
            res = generate_digest(root_dir=sys.argv[1])
            print(res["digest"])
        else:
            parser.print_help()

if __name__ == "__main__":
    main()
