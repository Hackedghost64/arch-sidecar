import os
import tempfile
from arch_sidecar.digest import generate_digest, parse_python_file
from arch_sidecar.utils import is_ignored_path, is_minified_or_binary

def test_parse_python_file():
    code = """
\"\"\"Module docstring here.\"\"\"
import os
import sys
from typing import List, Optional

APP_VERSION = "1.0.0"

class Worker:
    \"\"\"Worker class doc.\"\"\"
    def __init__(self, name: str):
        self.name = name

    async def execute(self, task_id: int = 1) -> bool:
        return True

def standalone_func(data: List[str], flag: bool = False) -> None:
    pass
"""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(code)
        f.flush()
        temp_path = f.name

    try:
        summary = parse_python_file(temp_path)
        assert "class Worker:" in summary
        assert "Worker class doc." in summary
        assert "def __init__(self, name: str)" in summary
        assert "async def execute(self, task_id: int = 1) -> bool" in summary
        assert "def standalone_func(data: List[str], flag: bool = False) -> None" in summary
        assert "APP_VERSION = '1.0.0'" in summary or "APP_VERSION" in summary
    finally:
        os.remove(temp_path)

def test_ignored_paths():
    assert is_ignored_path("node_modules/foo/bar.js")
    assert is_ignored_path("website/dist/bundle.js")
    assert is_ignored_path("build/lib/mod.py")
    assert is_ignored_path(".git/HEAD")
    assert is_ignored_path("bundle.min.js")
    assert is_ignored_path("package-lock.json")
    assert not is_ignored_path("src/core/main.py")
    assert not is_ignored_path("cli.py")

def test_minified_file_detection():
    normal_code = "def foo():\n    return 42\n"
    minified_code = "var a=1;" + "var b=2;" * 400 + "\n"
    
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(normal_code)
        f.flush()
        norm_path = f.name
        
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(minified_code)
        f.flush()
        mini_path = f.name

    try:
        assert not is_minified_or_binary(norm_path)
        assert is_minified_or_binary(mini_path)
    finally:
        os.remove(norm_path)
        os.remove(mini_path)
