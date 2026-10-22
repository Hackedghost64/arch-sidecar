import os
import tempfile
import pytest

from arch_sidecar.planner import (
    plan_adaptive_retrieval,
    RetrievalStrategy,
    SMALL_FILE_LINE_THRESHOLD,
)
from arch_sidecar.uncertainty import (
    scan_file_uncertainty,
    scan_codebase_uncertainty,
    ConfidenceLevel,
)

def test_planner_small_file_selects_whole_file():
    """A compact file (<120 lines) should automatically select whole_file strategy."""
    small_code = """
def quick_helper(x):
    return x * 2

def small_runner():
    return quick_helper(21)
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = os.path.join(tmpdir, "tiny_module.py")
        with open(test_file, "w") as f:
            f.write(small_code.strip())

        res = plan_adaptive_retrieval(
            task="Refactor small_runner",
            root_dir=tmpdir
        )

        assert res["strategy"] == RetrievalStrategy.WHOLE_FILE.value
        assert "tiny_module.py" in res["strategy_reason"]
        assert "small_runner" in res["context"]
        assert "quick_helper" in res["context"]
        assert res["estimated_tokens"] < 200

def test_planner_large_file_selects_surgical_symbols_and_callers():
    """A larger file (>120 lines) with caller/refactor task keywords selects symbols_and_callers with ranking."""
    # Create large file with padding lines
    lines = [
        "import os",
        "import sys",
        "",
        '"""Core System Controller Module"""',
        "GLOBAL_CONST = 42",
        "",
        "class TargetWorker:",
        "    def run(self):",
        "        return 'active'",
        "",
        "class UnrelatedHeavyClass:",
        "    def compute(self):",
        "        return sum(range(100))",
        "",
    ]
    # Pad to over 130 lines
    for i in range(130):
        lines.append(f"def dummy_fn_{i}(): pass")

    caller_code = """
from large_module import TargetWorker

def test_worker():
    w = TargetWorker()
    assert w.run() == 'active'
"""

    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "large_module.py"), "w") as f:
            f.write("\n".join(lines))
        with open(os.path.join(tmpdir, "test_large.py"), "w") as f:
            f.write(caller_code)

        res = plan_adaptive_retrieval(
            task="Decouple and test TargetWorker from large_module.py",
            root_dir=tmpdir
        )

        assert res["strategy"] == RetrievalStrategy.SYMBOLS_AND_CALLERS.value
        assert "TargetWorker" in res["selected_symbols"]
        assert "test_large.py" in res["context"]
        assert "dummy_fn_100" not in res["context"]  # noise pruned
        assert res["ranking"][0]["name"] == "TargetWorker"
        assert res["ranking"][0]["included"] is True
        assert "large_module.py" in res["ranking"][0]["reason"]

def test_planner_token_budget_enforcement():
    """Context generation respects token_budget and excludes lower-priority items gracefully."""
    lines = []
    for i in range(10):
        lines.append(f"class WorkerNode_{i}:\n    def execute(self):\n        return {i * 10}\n")
    for i in range(130):
        lines.append(f"# padding comment line {i}")

    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "cluster.py"), "w") as f:
            f.write("\n".join(lines))

        # Enforce tight budget of 250 tokens
        res = plan_adaptive_retrieval(
            task="Audit WorkerNode_0 and WorkerNode_1 and WorkerNode_2",
            root_dir=tmpdir,
            token_budget=200,
            strategy_override="targeted_symbols"
        )

        assert res["estimated_tokens"] <= 350
        # Check that some candidates were marked excluded to fit budget
        excluded = [c for c in res["ranking"] if not c["included"]]
        assert len(excluded) > 0
        assert "token budget" in excluded[0]["reason"]

def test_uncertainty_scanner_detects_reflection():
    """Scanner detects getattr, importlib, eval, exec and classifies confidence correctly."""
    code = """
import importlib

def dynamic_invoker(mod_name, func_name):
    # Dynamic import with variable -> Unknown
    mod = importlib.import_module(mod_name)
    # Dynamic import with static string -> Potential
    sys_mod = importlib.import_module("sys")
    # Dynamic getattr with variable -> Unknown
    fn = getattr(mod, func_name)
    # getattr with literal -> Potential
    version = getattr(sys_mod, "version")
    # Arbitrary code execution -> Unknown
    eval("1 + 1")
    exec("x = 2")
    return fn()
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = os.path.join(tmpdir, "dyn.py")
        with open(fpath, "w") as f:
            f.write(code)

        findings = scan_file_uncertainty(fpath, "dyn.py")
        patterns = {f.pattern: f.confidence for f in findings}

        assert "dynamic_import_variable" in patterns
        assert patterns["dynamic_import_variable"] == ConfidenceLevel.UNKNOWN

        assert "dynamic_import_literal" in patterns
        assert patterns["dynamic_import_literal"] == ConfidenceLevel.POTENTIAL

        assert "getattr_dynamic" in patterns
        assert patterns["getattr_dynamic"] == ConfidenceLevel.UNKNOWN

        assert "getattr_literal" in patterns
        assert patterns["getattr_literal"] == ConfidenceLevel.POTENTIAL

        assert "eval_call" in patterns
        assert patterns["eval_call"] == ConfidenceLevel.UNKNOWN

        assert "exec_call" in patterns
        assert patterns["exec_call"] == ConfidenceLevel.UNKNOWN

def test_uncertainty_in_adaptive_context_report():
    """Adaptive context report displays uncertainty notice when dynamic reflection is present."""
    code = """
class SafeWorker:
    def execute(self, action_name):
        handler = getattr(self, action_name)
        return handler()
"""
    # Pad to over 130 lines to trigger AST strategy
    lines = [code] + [f"def pad_{i}(): pass" for i in range(130)]

    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "worker.py"), "w") as f:
            f.write("\n".join(lines))

        res = plan_adaptive_retrieval(
            task="Refactor SafeWorker execute method",
            root_dir=tmpdir
        )

        assert "Uncertainty" in res["context"]
        assert "getattr" in res["context"]
        assert res["uncertainty_report"]["findings_count"] > 0


def test_short_symbol_names_do_not_match_substrings_and_single_file_uncertainty():
    """1-2 char function names like `c` or `p` must not match inside words like `decouple` or `app.py`."""
    from arch_sidecar.planner import score_symbol_relevance

    score_c, _ = score_symbol_relevance("c", {"filename": "other.py"}, "Decouple rsi from god_app.py", set())
    score_p, _ = score_symbol_relevance("p", {"filename": "other.py"}, "Decouple rsi from god_app.py", set())
    score_rsi, _ = score_symbol_relevance("rsi", {"filename": "god_app.py"}, "Decouple rsi from god_app.py", {"god_app.py"})

    assert score_c == 0
    assert score_p == 0
    assert score_rsi >= 80

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write("def dyn(x):\n    return eval(x)\n")
        f.flush()
        tpath = f.name

    try:
        u_res = scan_codebase_uncertainty(tpath)
        assert u_res["total_uncertainties"] == 1
        assert u_res["unknown_count"] == 1
    finally:
        os.remove(tpath)

