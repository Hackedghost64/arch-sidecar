import os
import tempfile
from arch_sidecar.guard import check_code_health
from arch_sidecar.utils import run_ruff_autofix, find_ruff_binary
from arch_sidecar.decouple import prefactor_decouple


def test_ruff_binary_discovery():
    bin_path = find_ruff_binary()
    assert bin_path is not None
    assert os.path.exists(bin_path)


def test_guard_ruff_unused_import_diagnostic():
    code = """import sys
import math

def calculate(x: int) -> int:
    return x * 2
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = os.path.join(tmpdir, "unused_mod.py")
        with open(fpath, "w") as f:
            f.write(code)

        # Baseline check without ruff
        res_no_ruff = check_code_health(fpath, project_root=tmpdir, enable_ruff=False)
        assert res_no_ruff["success"] is True
        assert res_no_ruff["warnings"] == 0

        # Check with ruff enabled
        res_ruff = check_code_health(fpath, project_root=tmpdir, enable_ruff=True)
        assert res_ruff["success"] is True
        assert res_ruff["warnings"] >= 2
        unused_codes = [i["code"] for i in res_ruff["issues"] if i["type"] == "UNUSED_IMPORT"]
        assert "F401" in unused_codes


def test_ruff_autofix_cleans_unused_imports():
    dirty_code = """import os
import sys
import math

def greet():
    return "hello"
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = os.path.join(tmpdir, "dirty.py")
        with open(fpath, "w") as f:
            f.write(dirty_code)

        success = run_ruff_autofix([fpath], project_root=tmpdir)
        assert success is True

        with open(fpath) as f:
            cleaned = f.read()

        assert "import os" not in cleaned
        assert "import sys" not in cleaned
        assert "import math" not in cleaned
        assert 'return "hello"' in cleaned


def test_decouple_automatically_applies_ruff_cleanup():
    src = """import math
import sys

def extracted_fn(a: int) -> int:
    return int(math.sqrt(a))

def remaining_fn():
    # Only uses remaining logic, not math
    return 42
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        src_path = os.path.join(tmpdir, "source_mod.py")
        dst_path = os.path.join(tmpdir, "dest_mod.py")

        with open(src_path, "w") as f:
            f.write(src)

        res = prefactor_decouple(
            symbol="extracted_fn",
            from_file=src_path,
            to_file=dst_path,
            project_root=tmpdir,
            ruff_cleanup=True,
        )
        assert res["success"] is True

        # Verify destination file exists and is cleanly formatted
        with open(dst_path) as f:
            dst_code = f.read()
        assert "def extracted_fn" in dst_code
        assert "import math" in dst_code

        # Verify source file had unused math/sys imports removed by Ruff
        with open(src_path) as f:
            src_code = f.read()
        assert "def remaining_fn" in src_code
        # math was only needed by extracted_fn, so Ruff autofix removed it from source_mod.py
        assert "import math" not in src_code


def test_ruff_autofix_preserves_reexports_in_all_and_pep484():
    code = """from __future__ import annotations
import math
import os
from sys import version_info as version_info
from typing import Dict

__all__ = ["math", "version_info"]
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = os.path.join(tmpdir, "reexport.py")
        with open(fpath, "w") as f:
            f.write(code)

        run_ruff_autofix([fpath], project_root=tmpdir)
        with open(fpath) as f:
            res = f.read()

        assert "import math" in res, "math must be preserved due to __all__"
        assert "version_info as version_info" in res, "PEP 484 alias must be preserved"
        assert "import os" not in res, "Unused os must be removed"
        assert "from typing import Dict" not in res, "Unused Dict must be removed"


def test_ruff_autofix_handles_syntax_errors_gracefully():
    broken_code = "def broken(:\n    pass\n"
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = os.path.join(tmpdir, "broken.py")
        with open(fpath, "w") as f:
            f.write(broken_code)

        # Must not crash or corrupt file
        res = run_ruff_autofix([fpath], project_root=tmpdir)
        with open(fpath) as f:
            assert f.read() == broken_code

