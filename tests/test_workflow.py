import os
import tempfile
import pytest

from arch_sidecar.workflow import run_workflow, WorkflowStage

def test_workflow_retrieval_only():
    """An informational task without refactoring intent stays in retrieval pipeline."""
    code = """
def calculate_metrics():
    return {"latency": 42}
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "metrics.py"), "w") as f:
            f.write(code)

        res = run_workflow(
            task="Inspect calculate_metrics implementation",
            project_root=tmpdir
        )

        assert res["success"] is True
        assert res["pipeline"] == "retrieval_only"
        assert WorkflowStage.RETRIEVAL_PLAN in res["stages_executed"]
        assert WorkflowStage.TRANSFORMATION not in res["stages_executed"]
        assert "calculate_metrics" in res["context"]
        assert res["telemetry"]["status"] == "COMPLETED_RETRIEVAL"

def test_workflow_end_to_end_success_with_test_gate():
    """A refactoring task executes retrieval -> preflight -> transform -> test verification."""
    source_code = """
import math

class NumberCruncher:
    def process(self, n: int) -> int:
        return math.isqrt(n)

def app_main():
    cruncher = NumberCruncher()
    return cruncher.process(144)
"""
    test_code = """
from source import NumberCruncher, app_main

def test_cruncher():
    assert app_main() == 12
    c = NumberCruncher()
    assert c.process(16) == 4
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        src_path = os.path.join(tmpdir, "source.py")
        test_path = os.path.join(tmpdir, "test_app.py")

        with open(src_path, "w") as f:
            f.write(source_code)
        with open(test_path, "w") as f:
            f.write(test_code)

        res = run_workflow(
            task="Decouple NumberCruncher from source.py into cruncher_mod.py",
            project_root=tmpdir,
            from_file="source.py",
            to_file="cruncher_mod.py",
            symbols=["NumberCruncher"],
            test_command=f"pytest {test_path}"
        )

        assert res["success"] is True
        assert res["pipeline"] == "full_refactor"
        assert WorkflowStage.TRANSFORMATION in res["stages_executed"]
        assert WorkflowStage.VERIFICATION in res["stages_executed"]
        assert res["telemetry"]["status"] == "VERIFIED"
        assert res["test_verification"]["passed"] is True

        # Target file created with math import and NumberCruncher
        target_path = os.path.join(tmpdir, "cruncher_mod.py")
        assert os.path.exists(target_path)
        with open(target_path, "r") as f:
            target_content = f.read()
        assert "class NumberCruncher" in target_content
        assert "import math" in target_content

        # Source file retained backward-compatibility alias
        with open(src_path, "r") as f:
            src_content = f.read()
        assert "from cruncher_mod import NumberCruncher" in src_content

def test_workflow_preflight_blocks_circular_import():
    """Stage 3 preflight blocks extraction when the moved symbol depends on code staying behind."""
    source = """
def banner():
    return "Welcome"

def cmd_stream():
    banner()
    return "streaming"
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        src_path = os.path.join(tmpdir, "cli.py")
        with open(src_path, "w") as f:
            f.write(source)

        res = run_workflow(
            task="Decouple cmd_stream from cli.py into stream.py",
            project_root=tmpdir,
            from_file="cli.py",
            to_file="stream.py",
            symbols=["cmd_stream"]
        )

        assert res["success"] is False
        assert "Circular import hazard" in res["error"]
        assert res["telemetry"]["status"] == "PREFLIGHT_BLOCKED"
        # Disk untouched
        assert not os.path.exists(os.path.join(tmpdir, "stream.py"))
        with open(src_path) as f:
            assert f.read() == source

def test_workflow_transactional_rollback_on_test_failure():
    """If Stage 5 tests fail, changes are rolled back to original pristine state."""
    source_code = """
def fragile_compute(x: int) -> int:
    return x * 10
"""
    test_code = """
from source import fragile_compute

def test_strict():
    # Will fail if module layout changes or behavior breaks
    assert fragile_compute(5) == 9999
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        src_path = os.path.join(tmpdir, "source.py")
        test_path = os.path.join(tmpdir, "test_fragile.py")

        with open(src_path, "w") as f:
            f.write(source_code)
        with open(test_path, "w") as f:
            f.write(test_code)

        res = run_workflow(
            task="Decouple fragile_compute from source.py into worker.py",
            project_root=tmpdir,
            from_file="source.py",
            to_file="worker.py",
            symbols=["fragile_compute"],
            test_command=f"pytest {test_path}"
        )

        assert res["success"] is False
        assert res["rolled_back"] is True
        assert res["telemetry"]["status"] == "ROLLED_BACK"

        # Verify source file was rolled back byte-for-byte
        with open(src_path, "r") as f:
            restored = f.read()
        assert restored == source_code

def test_workflow_bypasses_decoupling_on_compact_file():
    """When a general refactor task targets a compact file without explicit arguments, planner recommends bypass."""
    small_code = """
def quick_math(x: int) -> int:
    return x + 10
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "calc.py"), "w") as f:
            f.write(small_code)

        res = run_workflow(
            task="Extract quick_math from calc.py into math_ops.py",
            project_root=tmpdir
        )

        assert res["success"] is True
        assert res["pipeline"] == "bypass_recommended"
        assert res["recommendation"] == "BYPASS_DECOUPLING"
        assert "compact" in res["message"].lower()


def test_planner_direct_file_path_and_line_numbers():
    """
    Passing a direct file path to plan_adaptive_retrieval works seamlessly,
    emits 1-indexed line numbers in WHOLE_FILE mode, and attaches cross-file dependency signatures.
    """
    from arch_sidecar.planner import plan_adaptive_retrieval, RetrievalStrategy

    db_code = """class Database:
    async def get_watchlist(self) -> list[dict]:
        return [{"id": 1}]

db = Database()
"""
    routes_code = """from db import db

def list_watchlist():
    return db
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = os.path.join(tmpdir, "db.py")
        routes_file = os.path.join(tmpdir, "user_routes.py")
        with open(db_file, "w") as f:
            f.write(db_code)
        with open(routes_file, "w") as f:
            f.write(routes_code)

        # Pass the file path directly as root_dir
        res = plan_adaptive_retrieval(
            task="Add watchlist count endpoint in user_routes.py calling get_watchlist",
            root_dir=routes_file
        )
        assert res["strategy"] == RetrievalStrategy.WHOLE_FILE
        assert res["recommendation"] == "BYPASS_DECOUPLING"
        # Verify 1-indexed line numbers are present so agent can edit without an extra view_file call
        assert "1: from db import db" in res["context"]
        assert "3: def list_watchlist():" in res["context"]
        # Verify cross-file dependency signature (`get_watchlist` from `db.py`) is included
        assert "get_watchlist" in res["context"]

