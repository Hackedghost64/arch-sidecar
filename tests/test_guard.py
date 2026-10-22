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


def test_guard_wildcard_import_does_not_blind_undefined_symbols():
    """Ensures `from math import *` resolves math symbols while still catching hallucinated names."""
    code = """from math import *

def calc(x: float) -> float:
    valid = sqrt(x) + cos(x)
    return valid + totally_hallucinated_func(x)
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = os.path.join(tmpdir, "wild.py")
        with open(fpath, "w") as f:
            f.write(code)

        res = check_code_health(fpath, project_root=tmpdir)
        assert res["success"] is False
        undef = [i["symbol"] for i in res["issues"] if i["type"] == "UNDEFINED_SYMBOL"]
        assert "totally_hallucinated_func" in undef
        assert "sqrt" not in undef
        assert "cos" not in undef


def test_guard_detects_unbound_global_and_invalid_nonlocal():
    """Ensures `global x` reading an undefined module variable or `nonlocal z` without enclosing binding is caught."""
    code = """
def bad_global():
    global missing_global_counter
    missing_global_counter = missing_global_counter + 1
    return missing_global_counter

def bad_nonlocal():
    nonlocal no_enclosing_var
    no_enclosing_var = 1
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = os.path.join(tmpdir, "scope_bugs.py")
        with open(fpath, "w") as f:
            f.write(code)

        res = check_code_health(fpath, project_root=tmpdir)
        assert res["success"] is False
        undef = [i["symbol"] for i in res["issues"] if i["type"] == "UNDEFINED_SYMBOL"]
        assert "missing_global_counter" in undef
        assert "no_enclosing_var" in undef


def test_guard_class_body_scope_does_not_leak_into_methods():
    """In Python, class attributes and methods are NOT in lexical scope inside methods without `self.` or `ClassName.`."""
    code = """
class Engine:
    DEFAULT_SPEED = 100

    def helper(self) -> int:
        return 1

    def run(self) -> int:
        # Missing self. on both DEFAULT_SPEED and helper!
        return DEFAULT_SPEED + helper()
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = os.path.join(tmpdir, "engine.py")
        with open(fpath, "w") as f:
            f.write(code)

        res = check_code_health(fpath, project_root=tmpdir)
        assert res["success"] is False
        undef = [i["symbol"] for i in res["issues"] if i["type"] == "UNDEFINED_SYMBOL"]
        assert "DEFAULT_SPEED" in undef
        assert "helper" in undef


def test_guard_detects_multi_hop_circular_import_cycle():
    """Detects 3-hop circular import cycles: mod_a -> mod_b -> mod_c -> mod_a."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "mod_a.py"), "w") as f:
            f.write("import mod_b\ndef a(): return 1\n")
        with open(os.path.join(tmpdir, "mod_b.py"), "w") as f:
            f.write("import mod_c\ndef b(): return 2\n")
        with open(os.path.join(tmpdir, "mod_c.py"), "w") as f:
            f.write("import mod_a\ndef c(): return 3\n")

        res = check_code_health(os.path.join(tmpdir, "mod_a.py"), project_root=tmpdir)
        assert res["success"] is False
        cycles = [i for i in res["issues"] if i["type"] == "CIRCULAR_IMPORT"]
        assert len(cycles) == 1
        assert "mod_a.py -> mod_b.py -> mod_c.py -> mod_a.py" in cycles[0]["cycle"]

