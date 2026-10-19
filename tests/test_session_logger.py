"""
Tests for Devrith Automated Real-World Session Logging & Telemetry Engine.
"""

import os
import json
import tempfile
import pytest

from arch_sidecar.session_logger import (
    record_session,
    load_sessions,
    generate_session_summary,
    format_session_report_markdown,
    format_session_report_terminal,
    clear_sessions,
)
from arch_sidecar.feature_workflow import run_feature_workflow
from arch_sidecar.mcp_server import handle_tool_call


@pytest.fixture
def temp_telemetry_dir(monkeypatch):
    """Isolate telemetry storage inside a temporary directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setenv("DEVRITH_SESSIONS_DIR", tmpdir)
        yield tmpdir


def test_record_and_load_session(temp_telemetry_dir):
    """Verify recording and querying sessions."""
    sid1 = record_session(
        command="feature",
        task="Add /health endpoint to api.py",
        project_root=temp_telemetry_dir,
        success=True,
        duration_ms=145.2,
        mode="verify",
        target_files=["api.py"],
        target_symbols=["health_check"],
        preflight=[{"file": "api.py", "errors": 0, "warnings": 0}],
        test_run={"command": "pytest tests/test_api.py", "passed": True, "passed_count": 5, "failed_count": 0},
        verification_ledger={
            "statically_verified": ["AST syntax valid"],
            "runtime_verified": ["5 passed in 100ms"],
            "unverified_or_residual_risks": [],
        },
    )

    assert sid1 is not None
    assert sid1.startswith("sess_")

    sid2 = record_session(
        command="workflow",
        task="Decouple AuthHandler into auth.py",
        project_root=temp_telemetry_dir,
        success=False,
        duration_ms=320.0,
        mode="refactor",
        error="Circular dependency detected",
    )

    sessions = load_sessions()
    assert len(sessions) == 2
    assert sessions[0]["session_id"] == sid2
    assert sessions[1]["session_id"] == sid1
    assert sessions[1]["command"] == "feature"
    assert sessions[1]["verification_ledger"]["statically_verified_count"] == 1
    assert sessions[1]["test_run"]["passed_count"] == 5


def test_telemetry_disable_flag(temp_telemetry_dir, monkeypatch):
    """Verify that DEVRITH_DISABLE_TELEMETRY suppresses recording."""
    monkeypatch.setenv("DEVRITH_DISABLE_TELEMETRY", "1")
    sid = record_session(
        command="feature",
        task="Test task with disabled telemetry",
        project_root=temp_telemetry_dir,
    )
    assert sid is None
    sessions = load_sessions()
    assert len(sessions) == 0


def test_session_summary_and_formatting(temp_telemetry_dir):
    """Verify aggregation metrics and markdown/terminal report rendering."""
    record_session(
        command="feature",
        task="Task 1",
        project_root=temp_telemetry_dir,
        success=True,
        duration_ms=100.0,
        test_run={"passed": True, "passed_count": 10, "failed_count": 0},
        verification_ledger={"statically_verified": ["ok"], "runtime_verified": ["ok"]},
    )
    record_session(
        command="feature",
        task="Task 2",
        project_root=temp_telemetry_dir,
        success=True,
        duration_ms=200.0,
        test_run={"passed": True, "passed_count": 5, "failed_count": 0},
        verification_ledger={"statically_verified": ["ok"], "runtime_verified": ["ok"]},
    )
    record_session(
        command="check",
        task="Check code health",
        project_root=temp_telemetry_dir,
        success=False,
        duration_ms=50.0,
        preflight=[{"file": "app.py", "errors": 2, "warnings": 1}],
    )

    summary = generate_session_summary()
    assert summary["total_sessions"] == 3
    assert summary["success_count"] == 2
    assert summary["failed_count"] == 1
    assert summary["success_rate_percent"] == 66.7
    assert summary["avg_duration_ms"] == 116.67
    assert summary["commands"]["feature"] == 2
    assert summary["commands"]["check"] == 1
    assert summary["verification_stats"]["tests_passed"] == 15
    assert summary["verification_stats"]["preflight_errors_caught"] == 2
    assert summary["verification_stats"]["verified_runs"] == 2

    # Check Markdown formatting
    md = format_session_report_markdown(summary)
    assert "# 📊 Devrith Real-World Test & Telemetry Report" in md
    assert "66.7%" in md
    assert "`feature`" in md
    assert "`check`" in md

    # Check Terminal formatting
    term = format_session_report_terminal(summary)
    assert "DEVRITH REAL-WORLD SESSION TELEMETRY" in term
    assert "66.7%" in term


def test_clear_sessions(temp_telemetry_dir):
    """Verify session log clearing."""
    record_session(command="feature", task="Task to clear", project_root=temp_telemetry_dir)
    assert len(load_sessions()) == 1

    cleared = clear_sessions()
    assert cleared == 1
    assert len(load_sessions()) == 0


def test_feature_workflow_auto_recording(temp_telemetry_dir):
    """Verify run_feature_workflow automatically logs a session."""
    with tempfile.TemporaryDirectory() as proj_dir:
        py_file = os.path.join(proj_dir, "calc.py")
        with open(py_file, "w") as f:
            f.write("def add(a, b):\n    return a + b\n")

        res = run_feature_workflow(
            task="Add multiply function to calc.py",
            project_root=proj_dir,
        )

        assert res.get("session_id") is not None
        assert res["session_id"].startswith("sess_")

        # Verify logged in telemetry store
        sessions = load_sessions(project_root=proj_dir)
        assert len(sessions) == 1
        assert sessions[0]["command"] == "feature"
        assert "multiply" in sessions[0]["task"]


def test_mcp_session_report_tools(temp_telemetry_dir):
    """Verify MCP tools get_session_report and export_test_report."""
    record_session(
        command="feature",
        task="Implement MCP reporting test",
        project_root=temp_telemetry_dir,
        success=True,
        duration_ms=120.0,
    )

    # 1. get_session_report tool
    res = handle_tool_call("get_session_report", {"format": "markdown"})
    assert not res["isError"]
    assert "# 📊 Devrith Real-World Test & Telemetry Report" in res["content"][0]["text"]

    # JSON format
    res_json = handle_tool_call("get_session_report", {"format": "json"})
    assert not res_json["isError"]
    data = json.loads(res_json["content"][0]["text"])
    assert data["total_sessions"] == 1
    assert data["success_count"] == 1

    # 2. export_test_report tool
    export_file = os.path.join(temp_telemetry_dir, "EXPORTED_REPORT.md")
    res_export = handle_tool_call("export_test_report", {"output_path": export_file})
    assert not res_export["isError"]
    assert os.path.exists(export_file)
    with open(export_file) as f:
        content = f.read()
    assert "# 📊 Devrith Real-World Test & Telemetry Report" in content
