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

def test_parse_jvm_and_c_cpp_files():
    from arch_sidecar.digest import parse_jvm_file, parse_c_cpp_file

    kotlin_code = """
package com.example.app

data class User(val id: String, val name: String)

class UserRepository {
    suspend fun fetchUser(id: String): User {
        return User(id, "Test")
    }
}
"""
    c_code = """
#include <stdio.h>
#include <stdint.h>

#define MAX_BUFFER 1024

typedef struct {
    uint32_t width;
    uint32_t height;
} ImageMetadata;

void process_frame(const uint8_t* buffer, size_t len);
"""

    with tempfile.NamedTemporaryFile("w", suffix=".kt", delete=False) as f:
        f.write(kotlin_code)
        f.flush()
        kt_path = f.name

    with tempfile.NamedTemporaryFile("w", suffix=".c", delete=False) as f:
        f.write(c_code)
        f.flush()
        c_path = f.name

    try:
        kt_summary = parse_jvm_file(kt_path)
        assert "data class User" in kt_summary
        assert "class UserRepository" in kt_summary
        assert "suspend fun fetchUser" in kt_summary

        c_summary = parse_c_cpp_file(c_path)
        assert "#include <stdio.h>" in c_summary
        assert "#define MAX_BUFFER 1024" in c_summary
        assert "typedef struct" in c_summary
        assert "void process_frame" in c_summary
    finally:
        os.remove(kt_path)
        os.remove(c_path)


def test_parse_python_call_decorators_and_posonlyargs_and_single_file_digest():
    code = """
@app.route("/login", methods=["POST"])
def login(user: str, /, role: str = "admin") -> str:
    return user
"""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(code)
        f.flush()
        temp_path = f.name

    try:
        summary = parse_python_file(temp_path)
        assert "@app.route('/login', methods=['POST'])" in summary
        assert "def login(user: str, /, role: str = 'admin') -> str" in summary

        digest_res = generate_digest(temp_path)
        assert digest_res["files_indexed"] == 1
        assert "def login" in digest_res["digest"]
    finally:
        os.remove(temp_path)


