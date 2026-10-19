import os
import ast
import tempfile
from arch_sidecar.decouple import prefactor_decouple

def test_decouple_class_and_verify():
    cli_source = """
import os
import subprocess
import shutil

class ProcessManager:
    \"\"\"Manages background OS processes.\"\"\"
    def __init__(self, tag: str):
        self.tag = tag
        self.proc = None

    def start(self):
        cmd = ["echo", self.tag]
        self.proc = subprocess.Popen(cmd)

def dispatch_cli():
    mgr = ProcessManager("worker")
    return mgr
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        from_file = os.path.join(tmpdir, "cli.py")
        to_file = os.path.join(tmpdir, "process_utils.py")

        with open(from_file, "w") as f:
            f.write(cli_source)

        res = prefactor_decouple(
            symbol="ProcessManager",
            from_file=from_file,
            to_file=to_file,
            project_root=tmpdir,
            add_backward_compat_alias=True
        )

        assert res["success"] is True
        assert res["extracted_symbols"] == ["ProcessManager"]
        assert os.path.exists(to_file)

        # Verify to_file content
        with open(to_file, "r") as f:
            to_content = f.read()
        assert "class ProcessManager:" in to_content
        assert "subprocess" in to_content
        # Ensure syntax is valid
        ast.parse(to_content)

        # Verify from_file content
        with open(from_file, "r") as f:
            from_content = f.read()
        assert "from process_utils import ProcessManager" in from_content
        assert "class ProcessManager:" not in from_content
        assert "def dispatch_cli():" in from_content
        # Ensure syntax is valid
        ast.parse(from_content)

def test_circular_dependency_hazard_blocked():
    source = """
def banner():
    print("Welcome")

def cmd_stream():
    banner()
    return "streaming"
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        from_file = os.path.join(tmpdir, "cli.py")
        to_file = os.path.join(tmpdir, "stream.py")
        with open(from_file, "w") as f:
            f.write(source)

        res = prefactor_decouple(
            symbol="cmd_stream",
            from_file=from_file,
            to_file=to_file,
            project_root=tmpdir
        )

        # Must be blocked by pre-flight check
        assert res["success"] is False
        assert "Circular import hazard" in res["error"] or len(res.get("circular_hazards", [])) > 0
        hazards = res["circular_hazards"]
        assert any(h["symbol"] == "banner" for h in hazards)

def test_scope_shadowing_free_variable_analysis():
    source = """
# Module-level constant & function that remain in cli.py
port = 8080

def config():
    return {"mode": "dark"}

# Extracted function that shadows both 'port' (as param) and 'config' (as loop var)
def start_tunnel(port: int):
    items = ["cf-1", "cf-2"]
    for config in items:
        pass
    return f"tunnel-on-{port}"
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        from_file = os.path.join(tmpdir, "cli.py")
        to_file = os.path.join(tmpdir, "tunnel.py")
        with open(from_file, "w") as f:
            f.write(source)

        res = prefactor_decouple(
            symbol="start_tunnel",
            from_file=from_file,
            to_file=to_file,
            project_root=tmpdir
        )

        # Must succeed with ZERO false positive hazards
        assert res["success"] is True
        assert len(res.get("circular_hazards", [])) == 0
        assert "from tunnel import start_tunnel" in open(from_file).read()

def test_global_state_hazard_blocked():
    source = """
# Module-level constant remaining in cli.py
DEFAULT_TIMEOUT = 30

def worker():
    # Relies on unbound module global DEFAULT_TIMEOUT
    return DEFAULT_TIMEOUT * 2
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        from_file = os.path.join(tmpdir, "cli.py")
        to_file = os.path.join(tmpdir, "worker.py")
        with open(from_file, "w") as f:
            f.write(source)

        res = prefactor_decouple(
            symbol="worker",
            from_file=from_file,
            to_file=to_file,
            project_root=tmpdir,
            allow_globals=False
        )

        assert res["success"] is False
        assert "Unbound module global state" in res["error"]
        assert any(h["symbol"] == "DEFAULT_TIMEOUT" for h in res["global_state_hazards"])
        # Ensure file was not created
        assert not os.path.exists(to_file)

def test_transactional_refactoring_rollback_on_test_failure():
    source = """
def calculate():
    return 42

def runner():
    return calculate()
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        from_file = os.path.join(tmpdir, "math_cli.py")
        to_file = os.path.join(tmpdir, "calc.py")
        with open(from_file, "w") as f:
            f.write(source)

        # Force a failing test command: "exit 1"
        res = prefactor_decouple(
            symbol="calculate",
            from_file=from_file,
            to_file=to_file,
            project_root=tmpdir,
            run_tests="exit 1"
        )

        assert res["success"] is False
        assert res["rolled_back"] is True
        # Verify from_file is restored to exact pre-refactor state
        with open(from_file) as f:
            restored = f.read()
        assert restored == source
        # Verify newly created to_file was removed
        assert not os.path.exists(to_file)


def test_same_package_relative_import_and_future_annotations():
    """
    When extracting inside a Python package (directory with __init__.py),
    decouple must emit `from ._formatter import ...` (NOT `from _formatter import ...`)
    and preserve `from __future__ import annotations`.
    """
    formatting_source = """from __future__ import annotations
import typing as t

FORCED_WIDTH: int | None = None

def measure_table(rows: t.Iterable[tuple[str, str]]) -> tuple[int, ...]:
    return (10, 20)

class HelpFormatter:
    def __init__(self, width: int | None = None) -> None:
        self.width = width or FORCED_WIDTH

    def write_dl(self, rows: t.Sequence[tuple[str, str]]) -> tuple[int, ...]:
        return measure_table(rows)
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        pkg_dir = os.path.join(tmpdir, "src", "click")
        os.makedirs(pkg_dir)
        with open(os.path.join(pkg_dir, "__init__.py"), "w") as f:
            f.write("")
        from_file = os.path.join(pkg_dir, "formatting.py")
        to_file = os.path.join(pkg_dir, "_formatter.py")
        with open(from_file, "w") as f:
            f.write(formatting_source)

        # 1. Extracting HelpFormatter + measure_table without FORCED_WIDTH (an AnnAssign constant)
        # must be blocked by preflight with minimal_safe_closure suggesting FORCED_WIDTH
        blocked_res = prefactor_decouple(
            symbol=["HelpFormatter", "measure_table"],
            from_file=from_file,
            to_file=to_file,
            project_root=tmpdir
        )
        assert blocked_res["success"] is False
        assert any(h["symbol"] == "FORCED_WIDTH" for h in blocked_res["global_state_hazards"])
        assert "FORCED_WIDTH" in blocked_res["minimal_safe_closure"]

        # 2. Extracting the full minimal_safe_closure succeeds and emits `from ._formatter import ...`
        ok_res = prefactor_decouple(
            symbol=blocked_res["minimal_safe_closure"],
            from_file=from_file,
            to_file=to_file,
            project_root=tmpdir,
            run_tests='PYTHONPATH=src python3 -c "from click.formatting import HelpFormatter; hf = HelpFormatter(80); assert hf.write_dl([]) == (10, 20)"'
        )
        assert ok_res["success"] is True
        assert ok_res["import_target"] == "._formatter"

        with open(from_file) as f:
            updated_from = f.read()
        assert "from ._formatter import FORCED_WIDTH, measure_table, HelpFormatter" in updated_from

        with open(to_file) as f:
            created_to = f.read()
        assert "from __future__ import annotations" in created_to
        assert "FORCED_WIDTH: int | None = None" in created_to


def test_subpackage_and_cross_package_import_paths():
    """Tests relative imports across subpackages (`from .sub.mod import ...` / `from ..mod import ...`) and cross-package imports (`from pkg_b.mod import ...`)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pkg_a = os.path.join(tmpdir, "pkg_a")
        pkg_a_sub = os.path.join(pkg_a, "sub")
        pkg_b = os.path.join(tmpdir, "pkg_b")
        os.makedirs(pkg_a_sub)
        os.makedirs(pkg_b)
        for d in (pkg_a, pkg_a_sub, pkg_b):
            with open(os.path.join(d, "__init__.py"), "w") as f:
                f.write("")

        # Case 1: pkg_a/core.py -> pkg_a/sub/helpers.py  =>  from .sub.helpers import helper_fn
        core_path = os.path.join(pkg_a, "core.py")
        sub_helpers_path = os.path.join(pkg_a_sub, "helpers.py")
        with open(core_path, "w") as f:
            f.write("def helper_fn():\n    return 1\n\ndef main_fn():\n    return helper_fn()\n")

        res_sub = prefactor_decouple(
            symbol="helper_fn",
            from_file=core_path,
            to_file=sub_helpers_path,
            project_root=tmpdir
        )
        assert res_sub["success"] is True
        assert res_sub["import_target"] == ".sub.helpers"
        assert "from .sub.helpers import helper_fn" in open(core_path).read()

        # Case 2: pkg_a/sub/child.py -> pkg_a/shared.py  =>  from ..shared import shared_fn
        child_path = os.path.join(pkg_a_sub, "child.py")
        shared_path = os.path.join(pkg_a, "shared.py")
        with open(child_path, "w") as f:
            f.write("def shared_fn():\n    return 2\n")

        res_parent = prefactor_decouple(
            symbol="shared_fn",
            from_file=child_path,
            to_file=shared_path,
            project_root=tmpdir
        )
        assert res_parent["success"] is True
        assert res_parent["import_target"] == "..shared"
        assert "from ..shared import shared_fn" in open(child_path).read()

        # Case 3: Cross-package pkg_a/service.py -> pkg_b/utils.py  =>  from pkg_b.utils import util_fn
        service_path = os.path.join(pkg_a, "service.py")
        b_utils_path = os.path.join(pkg_b, "utils.py")
        with open(service_path, "w") as f:
            f.write("def util_fn():\n    return 3\n")

        res_cross = prefactor_decouple(
            symbol="util_fn",
            from_file=service_path,
            to_file=b_utils_path,
            project_root=tmpdir
        )
        assert res_cross["success"] is True
        assert res_cross["import_target"] == "pkg_b.utils"
        assert "from pkg_b.utils import util_fn" in open(service_path).read()


def test_overload_multi_definition_slicing():
    """Ensures all @overload stubs AND the concrete implementation of a function are extracted together without duplicate import names."""
    source = """from typing import overload, Union

@overload
def to_kv(val: None) -> None:
    ...

@overload
def to_kv(val: str) -> list[str]:
    ...

def to_kv(val: Union[str, None]) -> Union[list[str], None]:
    if val is None:
        return None
    return [val]

def other():
    return to_kv("a")
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        from_file = os.path.join(tmpdir, "utils.py")
        to_file = os.path.join(tmpdir, "_kv.py")
        with open(from_file, "w") as f:
            f.write(source)

        res = prefactor_decouple(
            symbol="to_kv",
            from_file=from_file,
            to_file=to_file,
            project_root=tmpdir
        )
        assert res["success"] is True
        assert res["extracted_symbols"] == ["to_kv"]

        with open(to_file) as f:
            to_code = f.read()
        assert to_code.count("def to_kv") == 3
        assert "from typing import overload, Union" in to_code

        with open(from_file) as f:
            from_code = f.read()
        assert "def to_kv" not in from_code
        assert "from _kv import to_kv\n" in from_code

