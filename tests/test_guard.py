import os
import tempfile
from arch_sidecar.guard import check_code_health


def test_guard_clean_code_with_modern_python_constructs():
    """
    Verifies zero false positives on forward references, comprehensions, lambdas,
    walrus operators, exception handlers, and context managers.
    """
    code = """from __future__ import annotations
import math
from typing import Any

def caller(items: list[int]) -> list[int]:
    # Forward call to helper defined below
    squared = [helper(x) for x in items if (y := x + 1) > 0]
    mapper = lambda val: val * 2
    try:
        res = {k: mapper(k) for k in squared}
    except ValueError as exc:
        print(exc)
        return []
    return list(res.values())

def helper(n: int) -> int:
    return int(math.isqrt(n))
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = os.path.join(tmpdir, "clean_mod.py")
        with open(fpath, "w") as f:
            f.write(code)

        res = check_code_health(fpath, project_root=tmpdir)
        assert res["success"] is True
        assert res["errors"] == 0
        assert res["health_score"] == 1.0
        assert res["issues"] == []


def test_guard_detects_undefined_hallucinated_symbol():
    """Detects unimported or hallucinated identifiers."""
    code = """def compute_total(prices: list[float]) -> float:
    return fast_sum_hallucinated(prices)
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = os.path.join(tmpdir, "billing.py")
        with open(fpath, "w") as f:
            f.write(code)

        res = check_code_health(fpath, project_root=tmpdir)
        assert res["success"] is False
        assert res["errors"] == 1
        issue = res["issues"][0]
        assert issue["type"] == "UNDEFINED_SYMBOL"
        assert issue["symbol"] == "fast_sum_hallucinated"
        assert issue["line"] == 2


def test_guard_detects_broken_local_import_with_fuzzy_suggestion():
    """Detects importing a non-existent symbol from a local module and suggests closest match."""
    utils_code = """def parse_key_val_list(items):
    return list(items)
"""
    consumer_code = """from utils import parse_key_val_lst

def run():
    return parse_key_val_lst([])
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "utils.py"), "w") as f:
            f.write(utils_code)
        consumer_path = os.path.join(tmpdir, "consumer.py")
        with open(consumer_path, "w") as f:
            f.write(consumer_code)

        res = check_code_health(consumer_path, project_root=tmpdir)
        assert res["success"] is False
        broken = [i for i in res["issues"] if i["type"] == "BROKEN_LOCAL_IMPORT"]
        assert len(broken) == 1
        assert broken[0]["symbol"] == "parse_key_val_lst"
        assert "parse_key_val_list" in broken[0]["message"]


def test_guard_detects_circular_import_cycle():
    """Detects circular import cycles between local modules."""
    mod_a = """from mod_b import b_func

def a_func():
    return b_func()
"""
    mod_b = """from mod_a import a_func

def b_func():
    return 1
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        path_a = os.path.join(tmpdir, "mod_a.py")
        path_b = os.path.join(tmpdir, "mod_b.py")
        with open(path_a, "w") as f:
            f.write(mod_a)
        with open(path_b, "w") as f:
            f.write(mod_b)

        res = check_code_health(path_a, project_root=tmpdir)
        assert res["success"] is False
        cycles = [i for i in res["issues"] if i["type"] == "CIRCULAR_IMPORT"]
        assert len(cycles) == 1
        assert "mod_a.py -> mod_b.py -> mod_a.py" in cycles[0]["cycle"]
