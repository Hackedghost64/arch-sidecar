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
