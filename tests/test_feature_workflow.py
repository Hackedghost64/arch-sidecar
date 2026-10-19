import os
import tempfile
from arch_sidecar.feature_workflow import run_feature_workflow


def test_feature_workflow_plan_and_impact_map():
    """
    Verifies Steps 1-5 of feature_workflow:
    1. Understands requested change intent
    2. Finds relevant existing anchor implementation
    3. Maps cross-file dependencies, downstream callers, and related tests
    4. Produces a numbered actionable implementation plan
    5. Supplies 1-indexed fast-path context
    """
    db_code = """class Database:
    async def get_items(self) -> list[str]:
        return ["item1", "item2"]

db = Database()
"""
    routes_code = """from db import db

async def list_items():
    items = await db.get_items()
    return {"ok": True, "items": items}
"""
    app_code = """from routes import list_items

async def main():
    return await list_items()
"""
    test_code = """from routes import list_items

def test_routes_smoke():
    assert callable(list_items)
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "db.py"), "w") as f:
            f.write(db_code)
        with open(os.path.join(tmpdir, "routes.py"), "w") as f:
            f.write(routes_code)
        with open(os.path.join(tmpdir, "app.py"), "w") as f:
            f.write(app_code)
        with open(os.path.join(tmpdir, "test_routes.py"), "w") as f:
            f.write(test_code)

        res = run_feature_workflow(
            task="Add count_items endpoint to routes.py calling db.get_items",
            project_root=tmpdir,
        )

        assert res["success"] is True
        assert res["mode"] == "plan_and_context"
        assert res["intent"]["category"] == "add_or_update_endpoint"
        assert res["primary_file"] == "routes.py"
        assert any(a["name"] == "list_items" for a in res["anchor_symbols"])

        # Impact map includes cross-file dependency (`db` / `Database.get_items`), caller (`app.py`), and test (`test_routes.py`)
        impact = res["impact_map"]
        assert any(d["file"] == "db.py" for d in impact["cross_file_dependencies"])
        assert any(c["file"] == "app.py" for c in impact["downstream_callers"])
        assert "test_routes.py" in impact["related_tests"]
        assert "pytest" in (impact["inferred_test_command"] or "")

        # Actionable plan has 4 concrete steps
        assert len(res["implementation_plan"]) >= 3
        # 1-indexed context ready for direct editing
        assert "1: from db import db" in res["context"]
        assert "3: async def list_items():" in res["context"]


def test_feature_workflow_verify_success_and_evidence_ledger():
    """
    Verifies Steps 6-8 of feature_workflow when verify=True:
    Runs compiler-grade preflight + test suite and populates the Verification Ledger.
    """
    calc_code = """def add(a: int, b: int) -> int:
    return a + b

def subtract(a: int, b: int) -> int:
    return a - b
"""
    test_code = """from calc import add, subtract

def test_math():
    assert add(2, 3) == 5
    assert subtract(5, 2) == 3
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        calc_path = os.path.join(tmpdir, "calc.py")
        test_path = os.path.join(tmpdir, "test_calc.py")
        with open(calc_path, "w") as f:
            f.write(calc_code)
        with open(test_path, "w") as f:
            f.write(test_code)

        res = run_feature_workflow(
            task="Add subtract function to calc.py",
            project_root=tmpdir,
            target_files=["calc.py"],
            verify=True,
        )

        assert res["success"] is True
        assert res["mode"] == "verify"
        assert res["test_verification"]["passed"] is True
        ledger = res["verification_ledger"]
        assert any("AST syntax valid" in s for s in ledger["statically_verified"])
        assert any("lexical scope binding passed" in s for s in ledger["statically_verified"])
        assert any("Test suite passed" in s for s in ledger["runtime_verified"])
        assert ledger["unverified_or_residual_risks"] == []


def test_feature_workflow_verify_catches_hallucinated_symbol():
    """
    Verifies that `verify=True` catches an undefined/hallucinated identifier in preflight
    and reports it clearly in `unverified_or_residual_risks`.
    """
    bad_code = """def process_items(items: list[int]) -> int:
    return non_existent_helper_fn(items)
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        mod_path = os.path.join(tmpdir, "processor.py")
        with open(mod_path, "w") as f:
            f.write(bad_code)

        res = run_feature_workflow(
            task="Add process_items to processor.py",
            project_root=tmpdir,
            target_files=["processor.py"],
            verify=True,
        )

        assert res["success"] is False
        ledger = res["verification_ledger"]
        assert ledger["statically_verified"] == []
        assert any("non_existent_helper_fn" in r for r in ledger["unverified_or_residual_risks"])
