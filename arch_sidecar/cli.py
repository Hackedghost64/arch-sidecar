import argparse
import json
import sys
import os

from .digest import generate_digest
from .quality import audit_file, audit_repository, QualityReport
from .git_ops import (
    audit_git_changeset,
    analyze_git_change_impact,
    install_git_hook,
    uninstall_git_hook,
    is_git_repository,
)
from .context import locate_surgical_context
from .decouple import prefactor_decouple
from .mcp_server import run_mcp_server
from .planner import plan_adaptive_retrieval
from .uncertainty import scan_codebase_uncertainty
from .workflow import run_workflow
from .guard import check_code_health
from .utils import run_ruff_autofix
from .feature_workflow import run_feature_workflow
from .cluster import (
    build_repository_dependency_graph,
    partition_into_subsystems,
    predict_change_impact,
    format_cluster_map_terminal,
    format_change_impact_terminal,
)
from .session_logger import (
    generate_session_summary,
    format_session_report_markdown,
    format_session_report_terminal,
    clear_sessions,
)

def main():
    parser = argparse.ArgumentParser(
        prog="arch-sidecar",
        description="Universal Architectural Sidecar & Decoupling Engine (MCP Server + CLI)"
    )
    parser.add_argument(
        "--version", "-v",
        action="version",
        version="arch-sidecar 0.3.0 (Devrith Engine)",
        help="Show version and exit"
    )
    subparsers = parser.add_subparsers(dest="command", help="Subcommand to execute")

    # Command: feature (Evidence-Backed Feature Implementation Workflow)
    feat_parser = subparsers.add_parser(
        "feature",
        help="Evidence-Backed Feature Workflow: Intent -> Anchor Discovery -> Impact Map -> Actionable Plan -> 1-Indexed Context -> Preflight & Test Verification"
    )
    feat_parser.add_argument("task", help="Feature request description (e.g. 'Add /watchlist/count endpoint to user_routes.py')")
    feat_parser.add_argument("path", nargs="?", default=".", help="Root directory or target file path (default: .)")
    feat_parser.add_argument("--files", "-f", help="Comma-separated target files to focus on or verify")
    feat_parser.add_argument("--symbols", "-s", help="Comma-separated symbol names to focus on")
    feat_parser.add_argument("--verify", action="store_true", help="Run compiler-grade preflight + test verification on resulting changes and emit Verification Ledger")
    feat_parser.add_argument("--test", help="Explicit test command to run during --verify (auto-discovered if omitted)")
    feat_parser.add_argument("--budget", "-b", type=int, default=3200, help="Token budget for targeted context (default: 3200)")
    feat_parser.add_argument("--json", action="store_true", help="Output structured JSON payload")

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
    wf_parser.add_argument("--share-state", action="store_true", help="Automatically bind shared mutable globals and host symbols across modules")
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
    decouple_parser.add_argument("--share-state", action="store_true", help="Automatically bind shared mutable globals and host symbols across modules")
    decouple_parser.add_argument("--ruff", action="store_true", help="Auto-clean dead imports and format decoupled modules via Ruff ($0.00 cost)")

    # Command: cluster (Architectural Subsystem Cluster Map)
    cluster_parser = subparsers.add_parser(
        "cluster",
        help="Discover repository subsystem clusters, cohesion, instability, and coupling metrics"
    )
    cluster_parser.add_argument("path", nargs="?", default=".", help="Root directory to analyze (default: .)")
    cluster_parser.add_argument("--json", action="store_true", help="Output structured JSON payload")

    # Command: impact (Pre-Change Architectural Impact Predictor)
    impact_parser = subparsers.add_parser(
        "impact",
        help="Predict change blast radius, downstream callers, crossed clusters, and test recommendations before modifying code"
    )
    impact_parser.add_argument("target", help="Target file or symbol to analyze")
    impact_parser.add_argument("path", nargs="?", default=".", help="Root directory of the project (default: .)")
    impact_parser.add_argument("--json", action="store_true", help="Output structured JSON payload")

    # Command: check (Compiler-Grade Preflight & Architecture Health Guard)
    check_parser = subparsers.add_parser(
        "check",
        help="Sub-15ms preflight check: detects undefined/hallucinated symbols, circular import cycles, and coupling leaks"
    )
    check_parser.add_argument("file", help="Path to Python file to check")
    check_parser.add_argument("--root", default=".", help="Project root directory (default: .)")
    check_parser.add_argument("--json", action="store_true", help="Output JSON result")
    check_parser.add_argument("--ruff", action="store_true", help="Enable high-speed Ruff lint & unused import diagnostics")
    check_parser.add_argument("--fix", action="store_true", help="Automatically clean unused imports and format via Ruff ($0.00 cost)")

    # Command: audit (Code Quality, OOP & Contract Guard)
    audit_parser = subparsers.add_parser(
        "audit",
        help="Audit code quality, OOP cohesion, nesting complexity, anti-patterns, and contract consistency"
    )
    audit_parser.add_argument("path", nargs="?", default=".", help="Target file or repository directory (default: .)")
    audit_parser.add_argument("--strict", "-s", action="store_true", help="Fail if any warnings are detected (zero-tolerance mode)")
    audit_parser.add_argument("--staged", action="store_true", help="Audit only Git staged files (sub-10ms fast path)")
    audit_parser.add_argument("--diff", help="Audit only files modified against a base branch (e.g. --diff main)")
    audit_parser.add_argument("--json", action="store_true", help="Output raw structured JSON")

    # Command: git (Git Integration & Hooks)
    git_parser = subparsers.add_parser(
        "git",
        help="Devrith Git integration: pre-commit hooks, staged audits, and PR blast radius"
    )
    git_parser.add_argument("action", choices=["hook", "impact"], help="Git action: 'hook' to manage pre-commit guard, or 'impact' to predict PR blast radius")
    git_parser.add_argument("subaction", nargs="?", choices=["install", "uninstall"], default="install", help="For hook action: 'install' or 'uninstall'")
    git_parser.add_argument("--path", default=".", help="Repository root path (default: .)")
    git_parser.add_argument("--base", default="main", help="Base branch for impact comparison (default: main)")
    git_parser.add_argument("--staged", action="store_true", help="Target only staged files for impact analysis")
    git_parser.add_argument("--json", action="store_true", help="Output raw JSON payload")

    # Command: serve (MCP server)
    subparsers.add_parser("serve", help="Run as stdio Model Context Protocol (MCP) server")

    # Command: report (Real-World Test & Execution Telemetry Dashboard)
    report_parser = subparsers.add_parser(
        "report",
        aliases=["sessions"],
        help="View real-world execution telemetry and test session report collected automatically by Devrith"
    )
    report_parser.add_argument("path", nargs="?", default=None, help="Optional project directory to filter sessions by (default: all)")
    report_parser.add_argument("--json", action="store_true", help="Output raw telemetry summary JSON")
    report_parser.add_argument("--markdown", "-m", action="store_true", help="Output GitHub Markdown format")
    report_parser.add_argument("--export", "-o", help="Save the Markdown report to a file on disk (e.g. REAL_WORLD_REPORT.md)")
    report_parser.add_argument("--limit", "-l", type=int, default=20, help="Maximum number of recent sessions to include (default: 20)")
    report_parser.add_argument("--clear", action="store_true", help="Clear local session logs")

    args = parser.parse_args()

    if args.command == "feature":
        files_list = [f.strip() for f in args.files.split(",")] if args.files else None
        syms_list = [s.strip() for s in args.symbols.split(",")] if args.symbols else None
        res = run_feature_workflow(
            task=args.task,
            project_root=args.path,
            target_files=files_list,
            target_symbols=syms_list,
            verify=args.verify,
            test_command=args.test,
            token_budget=args.budget,
        )
        if args.json:
            print(json.dumps(res, indent=2))
        elif not args.verify:
            print(res["context"])
            print(
                f"\n# [arch-sidecar feature] Target: {res.get('primary_file')} | "
                f"Context Tokens: ~{res.get('estimated_tokens')} | Duration: {res.get('duration_ms')}ms",
                file=sys.stderr,
            )
        else:
            status_icon = "✅" if res.get("success") else "❌"
            print(f"{status_icon} [arch-sidecar feature --verify] Status: {'VERIFIED' if res.get('success') else 'FAILED'} ({res.get('duration_ms')}ms)")
            ledger = res.get("verification_ledger", {})
            print("\n📋 Verification & Evidence Ledger:")
            print("  [Statically Verified]")
            for item in ledger.get("statically_verified", []):
                print(f"    ✓ {item}")
            if not ledger.get("statically_verified"):
                print("    - None (preflight failed)")

            print("  [Runtime Verified]")
            for item in ledger.get("runtime_verified", []):
                print(f"    ✓ {item}")
            if not ledger.get("runtime_verified"):
                print("    - None")

            print("  [Unverified / Residual Risks]")
            for item in ledger.get("unverified_or_residual_risks", []):
                print(f"    ⚠️  {item}")
            if not ledger.get("unverified_or_residual_risks"):
                print("    ℹ️  No issues identified by configured AST & test checks; runtime behavior not covered by tests remains unverified.")

            if not res.get("success"):
                tv = res.get("test_verification")
                if tv and tv.get("output"):
                    print(f"\n--- Test Output ({tv.get('command')}) ---\n{tv.get('output')}", file=sys.stderr)
                sys.exit(1)

    elif args.command == "workflow":
        syms_list = [s.strip() for s in args.symbols.split(",")] if args.symbols else None
        res = run_workflow(
            task=args.task,
            project_root=args.path,
            from_file=args.from_file,
            to_file=args.to_file,
            symbols=syms_list,
            test_command=args.test,
            dry_run=args.dry_run,
            token_budget=args.budget,
            share_state=args.share_state,
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
            elif res.get("pipeline") == "bypass_recommended":
                print(f"\n⚡ [arch-sidecar notice] Compact file detected (<120 lines).")
                print(f"   Reason: {res.get('strategy_reason')}")
                print(f"   Recommendation: Direct edit with native tools has lower complexity than AST slicing. Decoupling bypassed.")
            else:
                if res.get("success"):
                    print(f"✅ Extracted: {', '.join(res.get('extracted_symbols', []))}")
                    print(f"   From: {res.get('from_file')} -> To: {res.get('to_file')}")
                    if res.get("share_state_bound"):
                        print(f"   🔗 Shared State Bound: {', '.join(res['share_state_bound'])}")
                    rg = res.get("ruff_gate")
                    if rg and rg.get("available"):
                        print(f"   🧹 Differential Ruff Gate: {rg.get('new_issues_count')} new issues ({rg.get('suppressed_legacy_count')} pre-existing suppressed)")
                    if res.get("diff"):
                        print("\n--- Diff ---")
                        print(res["diff"])
                    tv = res.get("test_verification")
                    if tv:
                        gate = "PASSED" if tv.get("passed") else "FAILED"
                        print(f"\n🧪 Verification Gate: {gate} ({tv.get('command')}) in {tv.get('duration_ms')}ms")
                else:
                    print(f"❌ Workflow Blocked/Failed: {res.get('error')}", file=sys.stderr)
                    for h in res.get("state_desync_hazards", []):
                        print(f"   ⚠️  {h.get('hazard')}", file=sys.stderr)
                        print(f"       ↳ {h.get('recommendation')}", file=sys.stderr)
                    for h in res.get("circular_hazards", []):
                        print(f"   ⚠️  {h.get('hazard')}", file=sys.stderr)
                        print(f"       ↳ {h.get('recommendation')}", file=sys.stderr)
                    for h in res.get("global_state_hazards", []):
                        print(f"   ⚠️  {h.get('hazard')}", file=sys.stderr)
                        print(f"       ↳ {h.get('recommendation')}", file=sys.stderr)
                    if res.get("minimal_safe_closure"):
                        print(f"   💡 Minimal self-contained symbol closure: --symbols {','.join(res['minimal_safe_closure'])}", file=sys.stderr)
                    if res.get("state_desync_hazards") or res.get("circular_hazards") or res.get("global_state_hazards"):
                        print("   🔧 Tip: Re-run with --share-state to automatically bind shared mutable globals and host symbols across modules.", file=sys.stderr)
                    if res.get("rolled_back"):
                        print("🔄 Transactional Rollback applied: disk restored to original pristine state.", file=sys.stderr)
                    tv = res.get("test_verification")
                    if tv and tv.get("output"):
                        print(f"\n--- Verification Output ({tv.get('command')}) ---\n{tv.get('output')}", file=sys.stderr)
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
            notice = ""
            if res.get("recommendation") == "BYPASS_DECOUPLING":
                notice = "\n⚡ [arch-sidecar advisory] Compact target detected (<120 lines). Direct file editing via native tools is recommended over AST decoupling."
            print(
                f"\n# [arch-sidecar plan] Strategy: {res['strategy']} | Budget: {res['token_breakdown']['token_budget']} | "
                f"Context Tokens: ~{res['estimated_tokens']} | Savings vs Whole Codebase: {res['token_breakdown']['savings_vs_codebase_pct']}%{notice}",
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
            dry_run=args.dry_run,
            share_state=args.share_state,
            ruff_cleanup=getattr(args, "ruff", False),
        )
        if res.get("success"):
            print(f"✅ Decoupled successfully in {res['execution_time_ms']}ms:")
            print(f"   Symbols: {', '.join(res['extracted_symbols'])}")
            print(f"   From: {res['from_file']}")
            print(f"   To:   {res['to_file']} (import target: {res.get('import_target')})")
            if res.get("share_state_bound"):
                print(f"   🔗 Shared State Bound: {', '.join(res['share_state_bound'])}")
            rg = res.get("ruff_gate")
            if rg and rg.get("available"):
                print(f"   🧹 Differential Ruff Gate: {rg.get('new_issues_count')} new issues ({rg.get('suppressed_legacy_count')} pre-existing suppressed)")
                for ni in rg.get("new_issues", []):
                    print(f"      ⚠️  [{ni.get('code')}] {ni.get('file')}:{ni.get('line')} - {ni.get('message')}")
            for w in res.get("secret_warnings", []):
                print(f"   ⚠️  [SECRET DETECTED] {w.get('hazard')}")
                print(f"       ↳ {w.get('recommendation')}")
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
            for h in res.get("state_desync_hazards", []):
                print(f"   ⚠️  {h.get('hazard')}", file=sys.stderr)
                print(f"       ↳ {h.get('recommendation')}", file=sys.stderr)
            for h in res.get("framework_hazards", []):
                print(f"   ⚠️  {h.get('hazard')}", file=sys.stderr)
                print(f"       ↳ {h.get('recommendation')}", file=sys.stderr)
            for h in res.get("circular_hazards", []):
                print(f"   ⚠️  {h.get('hazard')}", file=sys.stderr)
                print(f"       ↳ {h.get('recommendation')}", file=sys.stderr)
            for h in res.get("global_state_hazards", []):
                print(f"   ⚠️  {h.get('hazard')}", file=sys.stderr)
                print(f"       ↳ {h.get('recommendation')}", file=sys.stderr)
            for w in res.get("secret_warnings", []):
                print(f"   ⚠️  {w.get('hazard')}", file=sys.stderr)
                print(f"       ↳ {w.get('recommendation')}", file=sys.stderr)
            if res.get("minimal_safe_closure"):
                print(f"   💡 Minimal self-contained symbol closure: {','.join(res['minimal_safe_closure'])}", file=sys.stderr)
            if res.get("state_desync_hazards") or res.get("circular_hazards") or res.get("global_state_hazards"):
                print("   🔧 Tip: Re-run with --share-state to automatically bind shared mutable globals and host symbols across modules.", file=sys.stderr)
            if res.get("rolled_back"):
                print("🔄 Transactional Rollback applied: disk restored to original pristine state.", file=sys.stderr)
            if res.get("test_verification") and res["test_verification"].get("output"):
                print(f"Test failure output:\n{res['test_verification'].get('output')}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "check":
        if getattr(args, "fix", False):
            run_ruff_autofix([args.file], project_root=args.root)

        enable_ruff = getattr(args, "ruff", False) or getattr(args, "fix", False)
        res = check_code_health(args.file, project_root=args.root, enable_ruff=enable_ruff)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            if res.get("success"):
                print(f"✅ [arch-sidecar check] Clean! Health Score: {res.get('health_score')}/1.0 ({res.get('duration_ms')}ms)")
                print(f"   Target: {res.get('file')} | 0 errors | {res.get('warnings', 0)} warnings")
                for issue in res.get("issues", []):
                    if issue.get("severity") != "WARNING":
                        continue
                    print(f"   [WARN] Line {issue.get('line')}: {issue.get('message')}")
                    if issue.get("recommendation"):
                        print(f"          ↳ Fix: {issue.get('recommendation')}")
            else:
                print(f"❌ [arch-sidecar check] Preflight Issues Found! Health Score: {res.get('health_score')}/1.0 ({res.get('duration_ms')}ms)", file=sys.stderr)
                print(f"   Target: {res.get('file')} | {res.get('errors')} error(s) | {res.get('warnings')} warning(s)", file=sys.stderr)
                for issue in res.get("issues", []):
                    sev = issue.get("severity")
                    print(f"   [{sev}] Line {issue.get('line')}: {issue.get('message')}", file=sys.stderr)
                    if issue.get("recommendation"):
                        print(f"          ↳ Fix: {issue.get('recommendation')}", file=sys.stderr)
                sys.exit(1)

    elif args.command == "cluster":
        graph = build_repository_dependency_graph(args.path)
        clusters = partition_into_subsystems(graph)
        if args.json:
            print(json.dumps(clusters, indent=2))
        else:
            repo_name = os.path.basename(os.path.abspath(args.path))
            print(format_cluster_map_terminal(clusters, repo_name))

    elif args.command == "impact":
        impact = predict_change_impact(args.target, root_dir=args.path)
        if args.json:
            print(json.dumps(impact, indent=2))
        else:
            if not impact.get("success"):
                print(f"❌ Impact analysis failed: {impact.get('error')}", file=sys.stderr)
                sys.exit(1)
            print(format_change_impact_terminal(impact))

    elif args.command == "audit":
        target = os.path.abspath(args.path)
        if args.staged or args.diff:
            report = audit_git_changeset(target, staged_only=args.staged, base_branch=args.diff, strict=args.strict)
        elif os.path.isfile(target):
            issues = audit_file(target)
            report = QualityReport(
                total_files=1,
                total_issues=len(issues),
                error_count=sum(1 for i in issues if i.severity == "error"),
                warning_count=sum(1 for i in issues if i.severity == "warning"),
                clean=(len(issues) == 0 if args.strict else not any(i.severity == "error" for i in issues)),
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
            report = audit_repository(target, strict=args.strict)

        if args.json:
            print(json.dumps(report.to_dict(), indent=2))
        else:
            status_icon = "✅" if report.clean else "⚠️"
            print(f"\n{status_icon} [Devrith Code Craft Audit] Score: {report.score}/100 | Files: {report.total_files} | Issues: {report.total_issues} (Errors: {report.error_count}, Warnings: {report.warning_count})")
            if report.metrics and "categories" in report.metrics:
                cats = report.metrics["categories"]
                print(f"   Complexity: {cats.get('complexity', 0)} | Anti-Patterns: {cats.get('anti_pattern', 0)} | OOP/Cohesion: {cats.get('oop_cohesion', 0)} | Contracts: {cats.get('contract', 0)}")

            if report.issues:
                print("\n📋 Detected Issues & Recommendations:")
                for issue in report.issues[:30]:  # Cap output display
                    icon = "❌" if issue.severity == "error" else "⚠️"
                    rel_f = os.path.relpath(issue.file_path, os.getcwd()) if os.path.isabs(issue.file_path) else issue.file_path
                    print(f"  {icon} [{issue.category.upper()}] {rel_f}:{issue.line} ({issue.symbol})")
                    print(f"     Problem: {issue.message}")
                    print(f"     Action:  {issue.suggestion}")

                if len(report.issues) > 30:
                    print(f"\n  ... and {len(report.issues) - 30} more issues (use --json for complete list)")

            if not report.clean and args.strict:
                sys.exit(1)

    elif args.command == "git":
        repo_dir = os.path.abspath(args.path)
        if args.action == "hook":
            if args.subaction == "install":
                ok, msg = install_git_hook(repo_dir)
                icon = "✅" if ok else "❌"
                print(f"{icon} {msg}")
                if not ok:
                    sys.exit(1)
            elif args.subaction == "uninstall":
                ok, msg = uninstall_git_hook(repo_dir)
                icon = "✅" if ok else "❌"
                print(f"{icon} {msg}")
                if not ok:
                    sys.exit(1)
        elif args.action == "impact":
            impact = analyze_git_change_impact(repo_dir, staged_only=args.staged, base_branch=args.base)
            if args.json:
                print(json.dumps(impact, indent=2))
            else:
                print("\n🔍 [Devrith Git Change Impact Predictor]")
                print(f"  Risk Level: {impact.get('risk_level')}")
                print(f"  Modified Files ({len(impact.get('modified_files', []))}):")
                for f in impact.get("modified_files", []):
                    print(f"    - {f}")
                print(f"\n  Affected Clusters: {', '.join(impact.get('affected_clusters', [])) or 'None'}")
                print(f"  Direct Callers ({len(impact.get('direct_callers', []))}): {', '.join(impact.get('direct_callers', [])[:10]) or 'None'}")
                print(f"  Recommended Tests ({len(impact.get('recommended_test_files', []))}):")
                for t in impact.get("recommended_test_files", []):
                    print(f"    ✓ {t}")

    elif args.command == "serve":
        run_mcp_server()

    elif args.command in ("report", "sessions"):
        if args.clear:
            cleared = clear_sessions(args.path)
            print(f"🗑️  Cleared {cleared} local session log entry/entries.")
            return

        summary = generate_session_summary(project_root=args.path, limit=args.limit)
        if args.json:
            print(json.dumps(summary, indent=2, default=str))
        elif args.markdown:
            print(format_session_report_markdown(summary, project_filter=args.path))
        elif args.export:
            md_content = format_session_report_markdown(summary, project_filter=args.path)
            with open(args.export, "w", encoding="utf-8") as f:
                f.write(md_content)
            print(f"✅ Real-world test report exported to {args.export} ({summary.get('total_sessions')} sessions)")
        else:
            print(format_session_report_terminal(summary))

    else:
        if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
            res = generate_digest(root_dir=sys.argv[1])
            print(res["digest"])
        else:
            parser.print_help()

if __name__ == "__main__":
    main()
