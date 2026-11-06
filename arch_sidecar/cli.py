import argparse
import json
import sys
import os
import re
import subprocess
import logging
from typing import Tuple, List, Dict, Any, Optional

from . import __version__
from .digest import generate_digest
from .quality import audit_file, audit_repository, QualityReport, QualityIssue
from .git_ops import (
    audit_git_changeset,
    analyze_git_change_impact,
    install_git_hook,
    uninstall_git_hook,
    is_git_repository,
)
from .context import locate_surgical_context
from .decouple import prefactor_decouple
from .polyglot_decouple import decouple_polyglot_symbol
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

logger = logging.getLogger(__name__)

POLYGLOT_EXTENSIONS = frozenset({
    ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".dart", ".kt", ".kts", ".java"
})


def parse_semver(tag: str) -> Tuple[int, ...]:
    """Parse a git tag or version string into a comparable numeric tuple."""
    nums = re.findall(r"\d+", tag)
    return tuple(int(x) for x in nums)


def _clean_tag_ref(line: str) -> Optional[str]:
    """Extract tag name from git ls-remote ref line."""
    if not line or "refs/tags/" not in line:
        return None
    ref = line.split("refs/tags/")[-1].strip()
    return ref[:-3] if ref.endswith("^{}") else ref


def parse_git_tags(git_output: str) -> List[str]:
    """Extract and sort git tags from git ls-remote output in ascending semver order."""
    tags = []
    for line in git_output.splitlines():
        tag = _clean_tag_ref(line.strip())
        if tag and tag not in tags:
            tags.append(tag)
    tags.sort(key=parse_semver)
    return tags


def fetch_remote_tags(
    repo_url: str = "https://github.com/Hackedghost64/arch-sidecar.git",
    timeout: int = 10,
) -> List[str]:
    """Query remote repository tags via git ls-remote."""
    cmd = ["git", "ls-remote", "--tags", "--refs", repo_url]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode == 0 and proc.stdout:
            return parse_git_tags(proc.stdout)
    except (subprocess.SubprocessError, OSError) as e:
        logger.debug("Failed fetching remote tags from %s: %s", repo_url, e)
        return []
    return []


def get_update_status(
    repo_url: str = "https://github.com/Hackedghost64/arch-sidecar.git",
) -> Dict[str, Any]:
    """Compare installed __version__ with remote git tags."""
    tags = fetch_remote_tags(repo_url)
    latest_tag = tags[-1] if tags else None
    latest_ver = latest_tag.lstrip("v") if latest_tag else __version__
    has_update = parse_semver(latest_ver) > parse_semver(__version__)
    return {
        "current_version": __version__,
        "latest_version": latest_ver,
        "latest_tag": latest_tag,
        "update_available": has_update,
        "remote_reachable": latest_tag is not None,
    }


def find_local_repo_root() -> Optional[str]:
    """Find root directory if arch-sidecar is executing within its source git repository."""
    pkg_dir = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.dirname(pkg_dir)
    return candidate if os.path.isdir(os.path.join(candidate, ".git")) else None


def install_via_local_git(repo_root: str) -> Tuple[bool, str]:
    """Update local working copy via fast-forward git pull and editable pip install."""
    pull = subprocess.run(
        ["git", "pull", "--ff-only"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if pull.returncode != 0:
        err = pull.stderr.strip() or pull.stdout.strip()
        return False, f"git pull failed: {err}"
    reinstall = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", ".", "--no-build-isolation"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if reinstall.returncode != 0:
        return False, f"editable install failed: {reinstall.stderr.strip()}"
    return True, "Successfully updated via local git pull and editable install."


def install_via_pip(target_url: str) -> Tuple[bool, str]:
    """Install or upgrade package directly from git URL via pip."""
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", target_url],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip()
        return False, f"pip install failed: {err}"
    return True, f"Successfully installed {target_url}."


def _print_update_status(status: Dict[str, Any]) -> None:
    """Format update check results for terminal display."""
    print("\n🔍 [Devrith Self-Update Check]")
    print(f"  Current Version:  {status['current_version']}")
    print(f"  Latest Version:   {status['latest_version']}")
    if status["update_available"]:
        print("  Update Available: Yes 🚀 (Run 'arch-sidecar update' or 'devrith update' to upgrade)")
    elif not status.get("remote_reachable"):
        print("  Update Available: Unknown (Offline / GitHub unreachable)")
    else:
        print("  Update Available: No (Already on latest version)")


def _report_update_result(
    success: bool,
    message: str,
    current_ver: str,
    target_ver: str,
    as_json: bool,
) -> Dict[str, Any]:
    """Output installation result and format return payload."""
    payload = {
        "success": success,
        "current_version": current_ver,
        "target_version": target_ver,
        "message": message,
    }
    if as_json:
        print(json.dumps(payload, indent=2))
    elif success:
        print(f"\n✅ [Devrith Self-Update] {message}")
    else:
        print(f"\n❌ [Devrith Self-Update] {message}", file=sys.stderr)
    return payload


def run_self_update(
    check_only: bool = False,
    tag: Optional[str] = None,
    as_json: bool = False,
) -> Dict[str, Any]:
    """
    Self-update arch-sidecar (Devrith) to the latest release from GitHub.
    """
    status = get_update_status()
    if check_only:
        if as_json:
            print(json.dumps(status, indent=2))
        else:
            _print_update_status(status)
        return status

    base_repo = "https://github.com/Hackedghost64/arch-sidecar.git"
    target_url = f"git+{base_repo}@{tag}" if tag else f"git+{base_repo}"
    local_root = find_local_repo_root()

    if local_root and not tag:
        ok, msg = install_via_local_git(local_root)
        if ok:
            return _report_update_result(True, msg, status["current_version"], status["latest_version"], as_json)

    ok, msg = install_via_pip(target_url)
    target_ver = tag if tag else status["latest_version"]
    return _report_update_result(ok, msg, status["current_version"], target_ver, as_json)


def _add_feature_and_workflow_subparsers(subparsers: Any) -> None:
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


def _add_plan_and_context_subparsers(subparsers: Any) -> None:
    plan_parser = subparsers.add_parser(
        "plan",
        help="Task-aware adaptive retrieval: dynamically select whole-file vs AST slice vs callers with candidate ranking and uncertainty analysis"
    )
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

    uncert_parser = subparsers.add_parser("uncertainty", help="Scan codebase for dynamic reflection (getattr, importlib, eval, exec)")
    uncert_parser.add_argument("path", nargs="?", default=".", help="Root directory of the codebase (default: .)")
    uncert_parser.add_argument("--json", action="store_true", help="Output findings as JSON")

    digest_parser = subparsers.add_parser("digest", help="Generate an AST skeleton digest of a codebase")
    digest_parser.add_argument("path", nargs="?", default=".", help="Root directory of the codebase (default: .)")
    digest_parser.add_argument("--max-tokens", type=int, default=4000, help="Max token budget (default: 4000)")
    digest_parser.add_argument("--output", "-o", help="File path to save the digest (optional)")

    locate_parser = subparsers.add_parser("locate", help="Locate surgical context for a specific task")
    locate_parser.add_argument("task", help="Task description or symbol to locate")
    locate_parser.add_argument("path", nargs="?", default=".", help="Root directory of the codebase (default: .)")
    locate_parser.add_argument("--symbols", "-s", help="Comma-separated explicit symbol names (optional)")
    locate_parser.add_argument("--adaptive", action="store_true", help="Use adaptive retrieval planner instead of raw surgical slice")


def _add_decouple_and_check_subparsers(subparsers: Any) -> None:
    decouple_parser = subparsers.add_parser("decouple", help="Deterministically decouple symbols into a new/existing module")
    decouple_parser.add_argument("symbols", help="Comma-separated symbol names to decouple (e.g. SleepInhibitor,start_cloudflared_tunnel)")
    decouple_parser.add_argument("from_file", help="Source file (e.g. cli.py)")
    decouple_parser.add_argument("to_file", help="Target file (e.g. system.py)")
    decouple_parser.add_argument("--root", default=".", help="Project root directory (default: .)")
    decouple_parser.add_argument("--test", help="Test command to run for headless verification (e.g. 'pytest tests/test_inhibitor_and_server.py')")
    decouple_parser.add_argument("--dry-run", action="store_true", help="Simulate AST split and show diff without modifying files")
    decouple_parser.add_argument("--share-state", action="store_true", help="Automatically bind shared mutable globals and host symbols across modules")
    decouple_parser.add_argument("--ruff", action="store_true", help="Auto-clean dead imports and format decoupled modules via Ruff ($0.00 cost)")

    check_parser = subparsers.add_parser(
        "check",
        help="Sub-15ms preflight check: detects undefined/hallucinated symbols, circular import cycles, and coupling leaks"
    )
    check_parser.add_argument("file", help="Path to Python file to check")
    check_parser.add_argument("--root", default=".", help="Project root directory (default: .)")
    check_parser.add_argument("--json", action="store_true", help="Output JSON result")
    check_parser.add_argument("--ruff", action="store_true", help="Enable high-speed Ruff lint & unused import diagnostics")
    check_parser.add_argument("--fix", action="store_true", help="Automatically clean unused imports and format via Ruff ($0.00 cost)")

    cluster_parser = subparsers.add_parser(
        "cluster",
        help="Discover repository subsystem clusters, cohesion, instability, and coupling metrics"
    )
    cluster_parser.add_argument("path", nargs="?", default=".", help="Root directory to analyze (default: .)")
    cluster_parser.add_argument("--json", action="store_true", help="Output structured JSON payload")

    impact_parser = subparsers.add_parser(
        "impact",
        help="Predict change blast radius, downstream callers, crossed clusters, and test recommendations before modifying code"
    )
    impact_parser.add_argument("target", help="Target file or symbol to analyze")
    impact_parser.add_argument("path", nargs="?", default=".", help="Root directory of the project (default: .)")
    impact_parser.add_argument("--json", action="store_true", help="Output structured JSON payload")


def _add_audit_and_tools_subparsers(subparsers: Any) -> None:
    audit_parser = subparsers.add_parser(
        "audit",
        help="Audit code quality, OOP cohesion, nesting complexity, anti-patterns, and contract consistency"
    )
    audit_parser.add_argument("path", nargs="?", default=".", help="Target file or repository directory (default: .)")
    audit_parser.add_argument("--strict", "-s", action="store_true", help="Fail if any warnings are detected (zero-tolerance mode)")
    audit_parser.add_argument("--staged", action="store_true", help="Audit only Git staged files (sub-10ms fast path)")
    audit_parser.add_argument("--diff", help="Audit only files modified against a base branch (e.g. --diff main)")
    audit_parser.add_argument("--json", action="store_true", help="Output raw structured JSON")

    git_parser = subparsers.add_parser(
        "git",
        help="Devrith Git integration: pre-commit hooks, staged audits, and PR blast radius"
    )
    git_parser.add_argument("action", choices=["hook", "impact"], help="Git action: 'hook' or 'impact'")
    git_parser.add_argument("subaction", nargs="?", choices=["install", "uninstall"], default="install")
    git_parser.add_argument("--path", default=".", help="Repository root path (default: .)")
    git_parser.add_argument("--base", default="main", help="Base branch for impact comparison (default: main)")
    git_parser.add_argument("--staged", action="store_true", help="Target only staged files for impact analysis")
    git_parser.add_argument("--json", action="store_true", help="Output raw JSON payload")

    subparsers.add_parser("serve", help="Run as stdio Model Context Protocol (MCP) server")

    report_parser = subparsers.add_parser(
        "report",
        aliases=["sessions"],
        help="View real-world execution telemetry and test session report collected automatically by Devrith"
    )
    report_parser.add_argument("path", nargs="?", default=None, help="Optional project directory filter")
    report_parser.add_argument("--json", action="store_true", help="Output raw telemetry summary JSON")
    report_parser.add_argument("--markdown", "-m", action="store_true", help="Output GitHub Markdown format")
    report_parser.add_argument("--export", "-o", help="Save Markdown report to disk")
    report_parser.add_argument("--limit", "-l", type=int, default=20, help="Maximum recent sessions (default: 20)")
    report_parser.add_argument("--clear", action="store_true", help="Clear local session logs")

    p_update = subparsers.add_parser("update", help="Self-update arch-sidecar (Devrith) to latest release from GitHub")
    p_update.add_argument("--check", action="store_true", help="Only check for latest version on GitHub")
    p_update.add_argument("--tag", type=str, default=None, help="Install specific git tag or branch")
    p_update.add_argument("--json", action="store_true", help="Output update status as JSON")


def build_cli_parser() -> argparse.ArgumentParser:
    """Builds and configures the complete CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="arch-sidecar",
        description="Universal Architectural Sidecar & Decoupling Engine (MCP Server + CLI)"
    )
    parser.add_argument(
        "--version", "-v",
        action="version",
        version=f"arch-sidecar {__version__} (Devrith Engine)",
        help="Show version and exit"
    )
    subparsers = parser.add_subparsers(dest="command", help="Subcommand to execute")
    _add_feature_and_workflow_subparsers(subparsers)
    _add_plan_and_context_subparsers(subparsers)
    _add_decouple_and_check_subparsers(subparsers)
    _add_audit_and_tools_subparsers(subparsers)
    return parser


def _print_feature_ledger(ledger: Dict[str, Any]) -> None:
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


def _handle_feature_verify_output(res: Dict[str, Any]) -> None:
    status_icon = "✅" if res.get("success") else "❌"
    status_str = "VERIFIED" if res.get("success") else "FAILED"
    duration = res.get("duration_ms", 0)
    print(f"{status_icon} [arch-sidecar feature --verify] Status: {status_str} ({duration}ms)")
    _print_feature_ledger(res.get("verification_ledger", {}))

    if not res.get("success"):
        tv = res.get("test_verification")
        if tv and tv.get("output"):
            print(f"\n--- Test Output ({tv.get('command')}) ---\n{tv.get('output')}", file=sys.stderr)
        sys.exit(1)


def _handle_feature(args: argparse.Namespace) -> None:
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
        _handle_feature_verify_output(res)


def _print_workflow_success(res: Dict[str, Any]) -> None:
    print(f"✅ Extracted: {', '.join(res.get('extracted_symbols', []))}")
    print(f"   From: {res.get('from_file')} -> To: {res.get('to_file')}")
    if res.get("share_state_bound"):
        print(f"   🔗 Shared State Bound: {', '.join(res['share_state_bound'])}")
    rg = res.get("ruff_gate")
    if rg and rg.get("available"):
        print(
            f"   🧹 Differential Ruff Gate: {rg.get('new_issues_count')} new issues "
            f"({rg.get('suppressed_legacy_count')} pre-existing suppressed)"
        )
    if res.get("diff"):
        print("\n--- Diff ---")
        print(res["diff"])
    tv = res.get("test_verification")
    if tv:
        gate = "PASSED" if tv.get("passed") else "FAILED"
        print(f"\n🧪 Verification Gate: {gate} ({tv.get('command')}) in {tv.get('duration_ms')}ms")


def _print_workflow_hazards(res: Dict[str, Any]) -> None:
    for h in res.get("state_desync_hazards", []):
        print(f"   ⚠️  {h.get('hazard')}", file=sys.stderr)
        print(f"       ↳ {h.get('recommendation')}", file=sys.stderr)
    for h in res.get("circular_hazards", []):
        print(f"   ⚠️  {h.get('hazard')}", file=sys.stderr)
        print(f"       ↳ {h.get('recommendation')}", file=sys.stderr)
    for h in res.get("global_state_hazards", []):
        print(f"   ⚠️  {h.get('hazard')}", file=sys.stderr)
        print(f"       ↳ {h.get('recommendation')}", file=sys.stderr)


def _print_workflow_failure(res: Dict[str, Any]) -> None:
    print(f"❌ Workflow Blocked/Failed: {res.get('error')}", file=sys.stderr)
    _print_workflow_hazards(res)
    if res.get("minimal_safe_closure"):
        print(f"   💡 Minimal self-contained symbol closure: --symbols {','.join(res['minimal_safe_closure'])}", file=sys.stderr)
    has_hazards = res.get("state_desync_hazards") or res.get("circular_hazards") or res.get("global_state_hazards")
    if has_hazards:
        print("   🔧 Tip: Re-run with --share-state to automatically bind shared mutable globals and host symbols across modules.", file=sys.stderr)
    if res.get("rolled_back"):
        print("🔄 Transactional Rollback applied: disk restored to original pristine state.", file=sys.stderr)
    tv = res.get("test_verification")
    if tv and tv.get("output"):
        print(f"\n--- Verification Output ({tv.get('command')}) ---\n{tv.get('output')}", file=sys.stderr)
    sys.exit(1)


def _print_workflow_terminal(res: Dict[str, Any]) -> None:
    tel = res.get("telemetry", {})
    status = tel.get("status", "UNKNOWN")
    stages = " -> ".join(res.get("stages_executed", []))
    print(f"🚀 [arch-sidecar workflow] Pipeline: {res.get('pipeline')} | Status: {status}")
    print(f"   Stages: {stages}")
    print(f"   Total Latency: {tel.get('total_latency_ms', 0)}ms | Total Tokens: ~{tel.get('total_tokens', 0)}")

    pipeline = res.get("pipeline")
    if pipeline == "retrieval_only":
        print(f"\n{res.get('context')}")
    elif pipeline == "bypass_recommended":
        print("\n⚡ [arch-sidecar notice] Compact file detected (<120 lines).")
        print(f"   Reason: {res.get('strategy_reason')}")
        print("   Recommendation: Direct edit with native tools has lower complexity than AST slicing. Decoupling bypassed.")
    elif res.get("success"):
        _print_workflow_success(res)
    else:
        _print_workflow_failure(res)


def _handle_workflow(args: argparse.Namespace) -> None:
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
        _print_workflow_terminal(res)


def _handle_plan(args: argparse.Namespace) -> None:
    syms_list = [s.strip() for s in args.symbols.split(",")] if args.symbols else None
    strategy = args.strategy if args.strategy != "auto" else None
    res = plan_adaptive_retrieval(
        task=args.task,
        root_dir=args.path,
        target_symbols=syms_list,
        token_budget=args.budget,
        strategy_override=strategy,
    )
    if args.json:
        print(json.dumps(res, indent=2))
        return

    print(res["context"])
    notice = ""
    if res.get("recommendation") == "BYPASS_DECOUPLING":
        notice = "\n⚡ [arch-sidecar advisory] Compact target detected (<120 lines). Direct file editing via native tools is recommended over AST decoupling."
    tb = res["token_breakdown"]
    print(
        f"\n# [arch-sidecar plan] Strategy: {res['strategy']} | Budget: {tb['token_budget']} | "
        f"Context Tokens: ~{res['estimated_tokens']} | Savings vs Whole Codebase: {tb['savings_vs_codebase_pct']}%{notice}",
        file=sys.stderr,
    )


def _handle_uncertainty(args: argparse.Namespace) -> None:
    res = scan_codebase_uncertainty(root_dir=args.path)
    if args.json:
        print(json.dumps(res, indent=2))
        return

    print(f"# Codebase Dynamic Uncertainty Report ({args.path})")
    print(f"Total dynamic reflection points: {res['total_uncertainties']}")
    print(f"  - Potential (literal dynamic calls): {res['potential_count']}")
    print(f"  - Unknown (dynamic variable reflection/eval): {res['unknown_count']}\n")
    for f in res["findings"]:
        icon = "⚠️" if f["confidence"] == "Unknown" else "ℹ️"
        print(f"{icon} {f['file']}:{f['line']} [{f['confidence']} - {f['pattern']}]")
        print(f"   Code: {f['code']}")
        print(f"   Note: {f['description']}\n")


def _handle_digest(args: argparse.Namespace) -> None:
    res = generate_digest(root_dir=args.path, max_tokens=args.max_tokens)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(res["digest"])
        print(f"Digest generated ({res['files_indexed']} files, ~{res['estimated_tokens']} tokens) -> {args.output}")
    else:
        print(res["digest"])
        print(
            f"\n# Summary: {res['files_indexed']} files indexed, ~{res['estimated_tokens']} estimated tokens.",
            file=sys.stderr,
        )


def _handle_locate(args: argparse.Namespace) -> None:
    symbols_list = [s.strip() for s in args.symbols.split(",")] if args.symbols else None
    if getattr(args, "adaptive", False):
        res = plan_adaptive_retrieval(task=args.task, root_dir=args.path, target_symbols=symbols_list)
        print(res["context"])
        print(f"\n# Adaptive Strategy: {res['strategy']} | Estimated Tokens: ~{res['estimated_tokens']}", file=sys.stderr)
    else:
        res = locate_surgical_context(task=args.task, root_dir=args.path, target_symbols=symbols_list)
        print(res["context"])
        print(f"\n# Estimated Tokens: ~{res['estimated_tokens']}", file=sys.stderr)


def _execute_cli_decouple(args: argparse.Namespace, sym_list: List[str]) -> Dict[str, Any]:
    ext = os.path.splitext(args.from_file)[1].lower()
    if ext in POLYGLOT_EXTENSIONS:
        return decouple_polyglot_symbol(
            symbol=sym_list[0],
            from_file=args.from_file,
            to_file=args.to_file,
            project_root=args.root,
            test_cmd=args.test,
            dry_run=args.dry_run,
        )
    return prefactor_decouple(
        symbol=sym_list,
        from_file=args.from_file,
        to_file=args.to_file,
        project_root=args.root,
        run_tests=args.test,
        dry_run=args.dry_run,
        share_state=args.share_state,
        ruff_cleanup=getattr(args, "ruff", False),
    )


def _print_decouple_ruff_gate(rg: Optional[Dict[str, Any]]) -> None:
    if not rg or not rg.get("available"):
        return
    print(
        f"   🧹 Differential Ruff Gate: {rg.get('new_issues_count')} new issues "
        f"({rg.get('suppressed_legacy_count')} pre-existing suppressed)"
    )
    for ni in rg.get("new_issues", []):
        print(f"      ⚠️  [{ni.get('code')}] {ni.get('file')}:{ni.get('line')} - {ni.get('message')}")


def _print_decouple_test_gate(tv: Optional[Dict[str, Any]]) -> None:
    if not tv:
        return
    status = "PASSED" if tv.get("passed") else "FAILED"
    print(f"\n🧪 Verification Gate ({tv.get('command')}): {status}")
    if tv.get("output"):
        print(tv["output"])


def _print_decouple_success(res: Dict[str, Any]) -> None:
    print(f"✅ Decoupled successfully in {res['execution_time_ms']}ms:")
    print(f"   Symbols: {', '.join(res['extracted_symbols'])}")
    print(f"   From: {res['from_file']}")
    print(f"   To:   {res['to_file']} (import target: {res.get('import_target')})")
    if res.get("share_state_bound"):
        print(f"   🔗 Shared State Bound: {', '.join(res['share_state_bound'])}")
    _print_decouple_ruff_gate(res.get("ruff_gate"))
    for w in res.get("secret_warnings", []):
        print(f"   ⚠️  [SECRET DETECTED] {w.get('hazard')}")
        print(f"       ↳ {w.get('recommendation')}")
    if res.get("diff"):
        print("\n--- Diff Generated ---")
        print(res["diff"])
    _print_decouple_test_gate(res.get("test_verification"))


def _print_decouple_hazard_items(res: Dict[str, Any]) -> bool:
    hazard_keys = ["state_desync_hazards", "framework_hazards", "circular_hazards", "global_state_hazards", "secret_warnings"]
    has_any = False
    for hk in hazard_keys:
        for h in res.get(hk, []):
            has_any = True
            tag = " [SECRET DETECTED]" if hk == "secret_warnings" else ""
            print(f"   ⚠️{tag} {h.get('hazard')}", file=sys.stderr)
            print(f"       ↳ {h.get('recommendation')}", file=sys.stderr)
    return has_any


def _print_decouple_failure(res: Dict[str, Any]) -> None:
    print(f"❌ Decoupling failed: {res.get('error')}", file=sys.stderr)
    has_hazards = _print_decouple_hazard_items(res)
    if res.get("minimal_safe_closure"):
        print(f"   💡 Minimal self-contained symbol closure: {','.join(res['minimal_safe_closure'])}", file=sys.stderr)
    if has_hazards:
        print("   🔧 Tip: Re-run with --share-state to automatically bind shared mutable globals and host symbols across modules.", file=sys.stderr)
    if res.get("rolled_back"):
        print("🔄 Transactional Rollback applied: disk restored to original pristine state.", file=sys.stderr)
    tv = res.get("test_verification")
    if tv and tv.get("output"):
        print(f"Test failure output:\n{tv.get('output')}", file=sys.stderr)
    sys.exit(1)


def _handle_decouple(args: argparse.Namespace) -> None:
    sym_list = [s.strip() for s in args.symbols.split(",")]
    res = _execute_cli_decouple(args, sym_list)
    if res.get("success"):
        _print_decouple_success(res)
    else:
        _print_decouple_failure(res)


def _print_check_success(res: Dict[str, Any]) -> None:
    print(f"✅ [arch-sidecar check] Clean! Health Score: {res.get('health_score')}/1.0 ({res.get('duration_ms')}ms)")
    print(f"   Target: {res.get('file')} | 0 errors | {res.get('warnings', 0)} warnings")
    for issue in res.get("issues", []):
        if issue.get("severity") != "WARNING":
            continue
        print(f"   [WARN] Line {issue.get('line')}: {issue.get('message')}")
        if issue.get("recommendation"):
            print(f"          ↳ Fix: {issue.get('recommendation')}")


def _print_check_failure(res: Dict[str, Any]) -> None:
    print(f"❌ [arch-sidecar check] Preflight Issues Found! Health Score: {res.get('health_score')}/1.0 ({res.get('duration_ms')}ms)", file=sys.stderr)
    print(f"   Target: {res.get('file')} | {res.get('errors')} error(s) | {res.get('warnings')} warning(s)", file=sys.stderr)
    for issue in res.get("issues", []):
        sev = issue.get("severity")
        print(f"   [{sev}] Line {issue.get('line')}: {issue.get('message')}", file=sys.stderr)
        if issue.get("recommendation"):
            print(f"          ↳ Fix: {issue.get('recommendation')}", file=sys.stderr)
    sys.exit(1)


def _handle_check(args: argparse.Namespace) -> None:
    if getattr(args, "fix", False):
        run_ruff_autofix([args.file], project_root=args.root)

    enable_ruff = bool(getattr(args, "ruff", False) or getattr(args, "fix", False))
    res = check_code_health(args.file, project_root=args.root, enable_ruff=enable_ruff)
    if args.json:
        print(json.dumps(res, indent=2))
        return

    if res.get("success"):
        _print_check_success(res)
    else:
        _print_check_failure(res)


def _handle_cluster(args: argparse.Namespace) -> None:
    graph = build_repository_dependency_graph(args.path)
    clusters = partition_into_subsystems(graph)
    if args.json:
        print(json.dumps(clusters, indent=2))
        return
    repo_name = os.path.basename(os.path.abspath(args.path))
    print(format_cluster_map_terminal(clusters, repo_name))


def _handle_impact(args: argparse.Namespace) -> None:
    impact = predict_change_impact(args.target, root_dir=args.path)
    if args.json:
        print(json.dumps(impact, indent=2))
        return
    if not impact.get("success"):
        print(f"❌ Impact analysis failed: {impact.get('error')}", file=sys.stderr)
        sys.exit(1)
    print(format_change_impact_terminal(impact))


def _tally_audit_issues(issues: List[QualityIssue]) -> Tuple[int, int, Dict[str, int]]:
    errors = 0
    warnings = 0
    categories = {"complexity": 0, "anti_pattern": 0, "oop_cohesion": 0, "contract": 0}
    for i in issues:
        if i.severity == "error":
            errors += 1
        elif i.severity == "warning":
            warnings += 1
        if i.category in categories:
            categories[i.category] += 1
    return errors, warnings, categories


def _build_audit_file_report(target: str, strict: bool) -> QualityReport:
    issues = audit_file(target)
    errors, warnings, categories = _tally_audit_issues(issues)
    is_clean = len(issues) == 0 if strict else (errors == 0)
    score_val = max(0.0, 100.0 - ((errors * 10) + (warnings * 2)))
    return QualityReport(
        total_files=1,
        total_issues=len(issues),
        error_count=errors,
        warning_count=warnings,
        clean=is_clean,
        score=score_val,
        issues=issues,
        metrics={"categories": categories},
    )


def _run_audit_target(target: str, args: argparse.Namespace) -> QualityReport:
    if args.staged or args.diff:
        return audit_git_changeset(target, staged_only=args.staged, base_branch=args.diff, strict=args.strict)
    if os.path.isfile(target):
        return _build_audit_file_report(target, args.strict)
    return audit_repository(target, strict=args.strict)


def _print_audit_issues(issues: List[QualityIssue]) -> None:
    if not issues:
        return
    print("\n📋 Detected Issues & Recommendations:")
    cwd = os.getcwd()
    for issue in issues[:30]:
        icon = "❌" if issue.severity == "error" else "⚠️"
        rel_f = os.path.relpath(issue.file_path, cwd) if os.path.isabs(issue.file_path) else issue.file_path
        print(f"  {icon} [{issue.category.upper()}] {rel_f}:{issue.line} ({issue.symbol})")
        print(f"     Problem: {issue.message}")
        print(f"     Action:  {issue.suggestion}")

    if len(issues) > 30:
        print(f"\n  ... and {len(issues) - 30} more issues (use --json for complete list)")


def _print_audit_report(report: QualityReport) -> None:
    status_icon = "✅" if report.clean else "⚠️"
    print(
        f"\n{status_icon} [Devrith Code Craft Audit] Score: {report.score}/100 | "
        f"Files: {report.total_files} | Issues: {report.total_issues} "
        f"(Errors: {report.error_count}, Warnings: {report.warning_count})"
    )
    if report.metrics and "categories" in report.metrics:
        cats = report.metrics["categories"]
        print(
            f"   Complexity: {cats.get('complexity', 0)} | Anti-Patterns: {cats.get('anti_pattern', 0)} | "
            f"OOP/Cohesion: {cats.get('oop_cohesion', 0)} | Contracts: {cats.get('contract', 0)}"
        )
    _print_audit_issues(report.issues)
    if not report.clean:
        sys.exit(1)


def _handle_audit(args: argparse.Namespace) -> None:
    target = os.path.abspath(args.path)
    report = _run_audit_target(target, args)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        _print_audit_report(report)


def _handle_git_hook(repo_dir: str, args: argparse.Namespace) -> None:
    if args.subaction == "uninstall":
        ok, msg = uninstall_git_hook(repo_dir)
    else:
        ok, msg = install_git_hook(repo_dir, strict=getattr(args, "strict", False))
    icon = "✅" if ok else "❌"
    print(f"{icon} {msg}")
    if not ok:
        sys.exit(1)


def _handle_git_impact(repo_dir: str, args: argparse.Namespace) -> None:
    impact = analyze_git_change_impact(repo_dir, staged_only=args.staged, base_branch=args.base)
    if args.json:
        print(json.dumps(impact, indent=2))
        return

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


def _handle_git(args: argparse.Namespace) -> None:
    repo_dir = os.path.abspath(args.path)
    if args.action == "hook":
        _handle_git_hook(repo_dir, args)
    elif args.action == "impact":
        _handle_git_impact(repo_dir, args)


def _handle_serve(args: argparse.Namespace) -> None:
    run_mcp_server()


def _handle_report(args: argparse.Namespace) -> None:
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


def _handle_update(args: argparse.Namespace) -> None:
    res = run_self_update(check_only=args.check, tag=args.tag, as_json=args.json)
    if not args.check and not res.get("success", True):
        sys.exit(1)


def _handle_default(parser: argparse.ArgumentParser) -> None:
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        res = generate_digest(root_dir=sys.argv[1])
        print(res["digest"])
    else:
        parser.print_help()


_COMMAND_HANDLERS = {
    "feature": _handle_feature,
    "workflow": _handle_workflow,
    "plan": _handle_plan,
    "uncertainty": _handle_uncertainty,
    "digest": _handle_digest,
    "locate": _handle_locate,
    "decouple": _handle_decouple,
    "check": _handle_check,
    "cluster": _handle_cluster,
    "impact": _handle_impact,
    "audit": _handle_audit,
    "git": _handle_git,
    "serve": _handle_serve,
    "report": _handle_report,
    "sessions": _handle_report,
    "update": _handle_update,
}


def main() -> None:
    parser = build_cli_parser()
    args = parser.parse_args()

    handler = _COMMAND_HANDLERS.get(args.command)
    if handler:
        handler(args)
    else:
        _handle_default(parser)


if __name__ == "__main__":
    main()
