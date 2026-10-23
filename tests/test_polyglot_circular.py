import os
import pytest
from pathlib import Path
from arch_sidecar.polyglot_resolver import (
    detect_go_circular_imports,
    detect_rust_circular_imports,
)


def test_go_circular_imports_detected(tmp_path: Path):
    # Create Go package A importing B, and B importing A
    pkg_a = tmp_path / "pkg_a"
    pkg_b = tmp_path / "pkg_b"
    pkg_a.mkdir()
    pkg_b.mkdir()

    # go.mod
    (tmp_path / "go.mod").write_text("module myapp\n\ngo 1.20\n")

    (pkg_a / "a.go").write_text("""package pkg_a

import "myapp/pkg_b"

func FuncA() {
    pkg_b.FuncB()
}
""")

    (pkg_b / "b.go").write_text("""package pkg_b

import "myapp/pkg_a"

func FuncB() {
    pkg_a.FuncA()
}
""")

    cycles = detect_go_circular_imports(str(tmp_path))
    assert len(cycles) >= 1
    cycle = cycles[0]
    assert cycle["language"] == "go"
    assert "pkg_a" in cycle["cycle_path"]
    assert "pkg_b" in cycle["cycle_path"]


def test_rust_circular_imports_detected(tmp_path: Path):
    # Create Rust module mod_a importing mod_b, and mod_b importing mod_a
    src = tmp_path / "src"
    src.mkdir()

    (src / "mod_a.rs").write_text("""
use crate::mod_b::Bar;

pub struct Foo {
    pub bar: Bar,
}
""")

    (src / "mod_b.rs").write_text("""
use crate::mod_a::Foo;

pub struct Bar {
    pub foo: Option<Box<Foo>>,
}
""")

    cycles = detect_rust_circular_imports(str(tmp_path))
    assert len(cycles) >= 1
    cycle = cycles[0]
    assert cycle["language"] == "rust"
    assert any("mod_a.rs" in p for p in cycle["cycle_path"])
    assert any("mod_b.rs" in p for p in cycle["cycle_path"])
