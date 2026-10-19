"""
Devrith Real-World Session Logger & Test Telemetry Engine.

Automatically collects local execution telemetry from real-world usage:
- Timestamp, duration, task, command, project, and git context
- Preflight static analysis results (AST validity, scope binding, import cycles)
- Runtime test executions (commands, passed/failed counts, exit codes)
- Verification ledgers (statically verified, runtime verified, residual risks)
- Token estimates and context metrics

Zero external network traffic. 100% local storage in ~/.devrith/sessions.jsonl
and optional project-level .devrith/sessions.jsonl.
"""

import os
import sys
import json
import time
import uuid
import platform
import subprocess
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Tuple


DEFAULT_GLOBAL_DIR = os.path.expanduser("~/.devrith")
GLOBAL_LOG_FILE = os.path.join(DEFAULT_GLOBAL_DIR, "sessions.jsonl")


def _get_git_info(repo_dir: str) -> Dict[str, Any]:
    """Extract lightweight git metadata without throwing exceptions."""
    if not os.path.isdir(repo_dir):
        return {}
    try:
        # Check if inside git worktree
        res = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=1,
        )
        if res.returncode != 0:
            return {}

        branch_res = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=1,
        )
        branch = branch_res.stdout.strip() if branch_res.returncode == 0 else ""

        commit_res = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=1,
        )
        commit = commit_res.stdout.strip() if commit_res.returncode == 0 else ""

        status_res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=1,
        )
        is_dirty = bool(status_res.stdout.strip()) if status_res.returncode == 0 else False

        return {
            "branch": branch,
            "commit": commit,
            "dirty": is_dirty,
        }
    except Exception:
        return {}


def record_session(
    command: str,
    task: str,
    project_root: str = ".",
    success: bool = True,
    duration_ms: float = 0.0,
    mode: Optional[str] = None,
    target_files: Optional[List[str]] = None,
    target_symbols: Optional[List[str]] = None,
    estimated_tokens: Optional[int] = None,
    preflight: Optional[Dict[str, Any]] = None,
    test_run: Optional[Dict[str, Any]] = None,
    verification_ledger: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    caller: str = "cli",
    extra: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """
    Log an execution session locally to ~/.devrith/sessions.jsonl and project .devrith/sessions.jsonl.
    Fails safely — will never interrupt or crash parent operations.
    Returns session_id on success, or None on failure/disabled.
    """
    if os.environ.get("DEVRITH_DISABLE_TELEMETRY") in ("1", "true", "True"):
        return None

    try:
        session_id = f"sess_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        root_abs = os.path.abspath(project_root) if os.path.exists(project_root) else os.getcwd()
        project_name = os.path.basename(root_abs) if root_abs else "unknown"

        # Sanitize preflight summary
        preflight_summary = None
        if preflight:
            if isinstance(preflight, list):
                total_err = sum(p.get("errors", 0) for p in preflight if isinstance(p, dict))
                total_warn = sum(p.get("warnings", 0) for p in preflight if isinstance(p, dict))
                preflight_summary = {
                    "files_checked": len(preflight),
                    "errors": total_err,
                    "warnings": total_warn,
                    "passed": total_err == 0,
                }
            elif isinstance(preflight, dict):
                preflight_summary = {
                    "files_checked": 1,
                    "errors": preflight.get("errors", 0),
                    "warnings": preflight.get("warnings", 0),
                    "passed": preflight.get("errors", 0) == 0,
                }

        # Sanitize test run summary
        test_summary = None
        if test_run and isinstance(test_run, dict):
            test_summary = {
                "command": test_run.get("command"),
                "passed": bool(test_run.get("passed", False)),
                "returncode": test_run.get("returncode", 0 if test_run.get("passed") else 1),
                "passed_count": test_run.get("passed_count", 0),
                "failed_count": test_run.get("failed_count", 0),
                "duration_ms": test_run.get("duration_ms"),
            }

        # Sanitize ledger summary
        ledger_summary = None
        if verification_ledger and isinstance(verification_ledger, dict):
            stat = verification_ledger.get("statically_verified", []) or []
            runt = verification_ledger.get("runtime_verified", []) or []
            unver = verification_ledger.get("unverified_or_residual_risks", []) or []
            ledger_summary = {
                "statically_verified_count": len(stat),
                "runtime_verified_count": len(runt),
                "unverified_risks_count": len(unver),
                "statically_verified": stat[:5],
                "runtime_verified": runt[:5],
                "unverified_risks": unver[:5],
            }

        record = {
            "session_id": session_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "command": command,
            "task": task,
            "project_root": root_abs,
            "project_name": project_name,
            "git": _get_git_info(root_abs),
            "success": success,
            "duration_ms": round(duration_ms, 2),
            "mode": mode or "default",
            "target_files": target_files or [],
            "target_symbols": target_symbols or [],
            "estimated_tokens": estimated_tokens,
            "preflight": preflight_summary,
            "test_run": test_summary,
            "verification_ledger": ledger_summary,
            "error": str(error) if error else None,
            "caller": caller,
            "env": {
                "python": platform.python_version(),
                "os": platform.system().lower(),
                "sidecar_version": "0.3.0",
            },
            "extra": extra or {},
        }

        # 1. Write to global user storage
        target_global_dir = os.environ.get("DEVRITH_SESSIONS_DIR", DEFAULT_GLOBAL_DIR)
        os.makedirs(target_global_dir, exist_ok=True)
        global_file = os.path.join(target_global_dir, "sessions.jsonl")
        with open(global_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

        # 2. Write to project-local storage if accessible
        try:
            if os.path.isdir(root_abs):
                proj_devrith_dir = os.path.join(root_abs, ".devrith")
                os.makedirs(proj_devrith_dir, exist_ok=True)
                proj_file = os.path.join(proj_devrith_dir, "sessions.jsonl")
                with open(proj_file, "a", encoding="utf-8") as pf:
                    pf.write(json.dumps(record) + "\n")
        except Exception:
            pass

        return session_id
    except Exception:
        return None


def load_sessions(
    project_root: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Load sessions from local JSONL storage.
    If project_root is specified, filters by matching project path or reads project-local sessions.
    Returns newest sessions first.
    """
    records_by_id: Dict[str, Dict[str, Any]] = {}
    files_to_check: List[str] = []

    # Check project-specific first if provided
    if project_root:
        proj_abs = os.path.abspath(project_root)
        proj_file = os.path.join(proj_abs, ".devrith", "sessions.jsonl")
        if os.path.exists(proj_file):
            files_to_check.append(proj_file)

    # Check global file
    target_global_dir = os.environ.get("DEVRITH_SESSIONS_DIR", DEFAULT_GLOBAL_DIR)
    global_file = os.path.join(target_global_dir, "sessions.jsonl")
    if os.path.exists(global_file):
        files_to_check.append(global_file)

    for fpath in files_to_check:
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        sid = rec.get("session_id")
                        if sid:
                            records_by_id[sid] = rec
                    except Exception:
                        continue
        except Exception:
            continue

    records = list(records_by_id.values())

    # Filter by project if requested and reading from global
    if project_root:
        norm_proj = os.path.abspath(project_root)
        records = [
            r for r in records
            if r.get("project_root") == norm_proj or r.get("project_name") == os.path.basename(norm_proj)
        ]

    # Sort descending by timestamp
    records.sort(key=lambda r: r.get("timestamp", ""), reverse=True)

    if limit and limit > 0:
        records = records[:limit]

    return records


def generate_session_summary(
    project_root: Optional[str] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Compute comprehensive telemetry metrics across stored sessions.
    """
    sessions = load_sessions(project_root=project_root, limit=limit)
    total = len(sessions)
    if total == 0:
        return {
            "total_sessions": 0,
            "success_rate_percent": 0.0,
            "avg_duration_ms": 0.0,
            "commands": {},
            "projects": {},
            "verification_stats": {
                "verified_runs": 0,
                "preflight_errors_caught": 0,
                "tests_passed": 0,
                "tests_failed": 0,
            },
            "recent_sessions": [],
        }

    success_count = sum(1 for s in sessions if s.get("success"))
    durations = [s.get("duration_ms", 0) for s in sessions if s.get("duration_ms")]
    avg_dur = round(sum(durations) / len(durations), 2) if durations else 0.0

    commands_count: Dict[str, int] = {}
    projects_count: Dict[str, int] = {}
    verified_runs = 0
    preflight_errors_caught = 0
    tests_passed = 0
    tests_failed = 0
    total_tokens = 0

    for s in sessions:
        cmd = s.get("command", "unknown")
        commands_count[cmd] = commands_count.get(cmd, 0) + 1

        proj = s.get("project_name", "unknown")
        projects_count[proj] = projects_count.get(proj, 0) + 1

        toks = s.get("estimated_tokens")
        if toks:
            total_tokens += toks

        pf = s.get("preflight")
        if pf and isinstance(pf, dict):
            preflight_errors_caught += pf.get("errors", 0)

        tr = s.get("test_run")
        if tr and isinstance(tr, dict):
            tests_passed += tr.get("passed_count", 0)
            tests_failed += tr.get("failed_count", 0)

        vl = s.get("verification_ledger")
        if vl and isinstance(vl, dict):
            if vl.get("statically_verified_count", 0) > 0 and s.get("success"):
                verified_runs += 1

    return {
        "total_sessions": total,
        "success_count": success_count,
        "failed_count": total - success_count,
        "success_rate_percent": round((success_count / total) * 100, 1),
        "avg_duration_ms": avg_dur,
        "total_tokens_estimated": total_tokens,
        "commands": commands_count,
        "projects": projects_count,
        "verification_stats": {
            "verified_runs": verified_runs,
            "preflight_errors_caught": preflight_errors_caught,
            "tests_passed": tests_passed,
            "tests_failed": tests_failed,
        },
        "recent_sessions": sessions[:10],
    }


def format_session_report_markdown(summary: Dict[str, Any], project_filter: Optional[str] = None) -> str:
    """Format session telemetry into an executive GitHub-style Markdown report."""
    total = summary.get("total_sessions", 0)
    if total == 0:
        return (
            "# 📊 Devrith Real-World Test & Telemetry Report\n\n"
            "> No recorded sessions found yet. Run an `arch-sidecar feature`, `workflow`, or `plan` command to generate session data.\n"
        )

    vstats = summary.get("verification_stats", {})
    cmds = summary.get("commands", {})
    projs = summary.get("projects", {})

    lines = [
        "# 📊 Devrith Real-World Test & Telemetry Report",
        f"**Scope:** {project_filter or 'All Projects'} | **Total Runs Recorded:** {total} | **Success Rate:** {summary.get('success_rate_percent')}%",
        "",
        "## 1. High-Level Metrics",
        "",
        "| Metric | Value | Description |",
        "|---|---:|---|",
        f"| **Total Sessions** | `{total}` | Executed through CLI, MCP, or Subagents |",
        f"| **Overall Success Rate** | `{summary.get('success_rate_percent')}%` | Runs completing without preflight or runtime abort |",
        f"| **Average Latency** | `{summary.get('avg_duration_ms')}ms` | End-to-end sidecar processing speed |",
        f"| **AST Preflight Catches** | `{vstats.get('preflight_errors_caught', 0)}` | Bugs/hallucinations intercepted before tests ran |",
        f"| **Automated Tests Passed** | `{vstats.get('tests_passed', 0)}` | Pytest assertions verified green |",
        f"| **Verified Completed Runs** | `{vstats.get('verified_runs', 0)}` | Compiler-grade preflight + test gates confirmed |",
        "",
        "## 2. Command Distribution",
        "",
        "| Command | Invocations | Share |",
        "|---|---:|---:|",
    ]

    for cmd, cnt in sorted(cmds.items(), key=lambda x: x[1], reverse=True):
        share = round((cnt / total) * 100, 1)
        lines.append(f"| `{cmd}` | {cnt} | {share}% |")

    lines.extend([
        "",
        "## 3. Repositories / Projects Tested",
        "",
        "| Repository / Path | Sessions |",
        "|---|---:|",
    ])

    for p, cnt in sorted(projs.items(), key=lambda x: x[1], reverse=True):
        lines.append(f"| `{p}` | {cnt} |")

    lines.extend([
        "",
        "## 4. Recent Execution Sessions",
        "",
        "| Timestamp | Command | Project | Task | Latency | Status |",
        "|---|---|---|---|---:|:---:|",
    ])

    for s in summary.get("recent_sessions", []):
        ts = s.get("timestamp", "").replace("T", " ")[:19]
        cmd = s.get("command", "")
        proj = s.get("project_name", "")
        task_str = (s.get("task") or "")[:45]
        if len(s.get("task") or "") > 45:
            task_str += "..."
        lat = f"{s.get('duration_ms', 0)}ms"
        status = "✅ PASS" if s.get("success") else "❌ FAIL"
        lines.append(f"| {ts} | `{cmd}` | `{proj}` | {task_str} | {lat} | {status} |")

    lines.append("")
    return "\n".join(lines)


def format_session_report_terminal(summary: Dict[str, Any]) -> str:
    """Format session telemetry into a clean, rich terminal ASCII dashboard."""
    total = summary.get("total_sessions", 0)
    if total == 0:
        return "\nℹ️  No Devrith sessions recorded yet. Run `arch-sidecar feature` or `workflow` to collect telemetry.\n"

    vstats = summary.get("verification_stats", {})
    cmds = summary.get("commands", {})
    rate = summary.get("success_rate_percent", 0.0)

    out = []
    out.append("┌─────────────────────────────────────────────────────────────────────────────┐")
    out.append("│                  🚀 DEVRITH REAL-WORLD SESSION TELEMETRY                   │")
    out.append("└─────────────────────────────────────────────────────────────────────────────┘")
    out.append(f"  Total Runs Recorded : {total:<10}    Success Rate : {rate}%")
    out.append(f"  Average Processing  : {summary.get('avg_duration_ms')}ms       AST Catches  : {vstats.get('preflight_errors_caught', 0)}")
    out.append(f"  Unit Tests Passed   : {vstats.get('tests_passed', 0):<10}    Verified Run : {vstats.get('verified_runs', 0)}")
    out.append("───────────────────────────────────────────────────────────────────────────────")
    out.append("  [Command Invocations]")
    for cmd, cnt in sorted(cmds.items(), key=lambda x: x[1], reverse=True):
        bar = "█" * min(25, max(1, int((cnt / total) * 25)))
        out.append(f"    • {cmd:<14} : {cnt:>3} ({round(cnt/total*100):>2}%)  {bar}")

    out.append("───────────────────────────────────────────────────────────────────────────────")
    out.append("  [Recent Sessions]")
    for s in summary.get("recent_sessions", [])[:5]:
        ts = s.get("timestamp", "").replace("T", " ")[:16]
        icon = "✓" if s.get("success") else "✗"
        task_sub = (s.get("task") or "")[:38]
        out.append(f"    {icon} [{ts}] {s.get('command'):<8} | {s.get('project_name'):<12} | {task_sub:<38} ({s.get('duration_ms')}ms)")

    out.append("───────────────────────────────────────────────────────────────────────────────\n")
    return "\n".join(out)


def clear_sessions(project_root: Optional[str] = None) -> int:
    """Clear session log file(s). Returns number of sessions deleted."""
    total_cleared = 0
    if project_root:
        proj_file = os.path.join(os.path.abspath(project_root), ".devrith", "sessions.jsonl")
        if os.path.exists(proj_file):
            try:
                count = sum(1 for _ in open(proj_file, "r", encoding="utf-8"))
                os.remove(proj_file)
                total_cleared += count
            except Exception:
                pass

    target_global_dir = os.environ.get("DEVRITH_SESSIONS_DIR", DEFAULT_GLOBAL_DIR)
    global_file = os.path.join(target_global_dir, "sessions.jsonl")
    if os.path.exists(global_file):
        try:
            count = sum(1 for _ in open(global_file, "r", encoding="utf-8"))
            os.remove(global_file)
            total_cleared += count
        except Exception:
            pass

    return total_cleared
