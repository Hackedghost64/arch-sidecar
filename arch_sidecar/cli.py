import argparse
import json
import sys
import os

from .digest import generate_digest
from .context import locate_surgical_context
from .decouple import prefactor_decouple
from .mcp_server import run_mcp_server

def main():
    parser = argparse.ArgumentParser(
        prog="arch-sidecar",
        description="Universal Architectural Sidecar & Decoupling Engine (MCP Server + CLI)"
    )
    subparsers = parser.add_subparsers(dest="command", help="Subcommand to execute")

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

    if args.command == "digest":
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
        # Default behavior if run without args or with a path: generate digest
        if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
            res = generate_digest(root_dir=sys.argv[1])
            print(res["digest"])
        else:
            parser.print_help()

if __name__ == "__main__":
    main()
