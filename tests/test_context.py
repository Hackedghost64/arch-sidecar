import os
import tempfile
from arch_sidecar.context import locate_surgical_context

def test_locate_surgical_context():
    code_main = """
from helper import utility_calc

def run_service():
    val = utility_calc(10)
    return val
"""
    code_helper = """
import math

def utility_calc(x: int) -> int:
    \"\"\"Calculates square root and returns int.\"\"\"
    return int(math.isqrt(x))
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "main.py"), "w") as f:
            f.write(code_main)
        with open(os.path.join(tmpdir, "helper.py"), "w") as f:
            f.write(code_helper)

        res = locate_surgical_context("utility_calc", root_dir=tmpdir)
        assert "utility_calc" in res["matched_symbols"]
        assert "math.isqrt" in res["context"]
        assert "helper.py" in res["symbol_details"]["utility_calc"]["file"]
