import pytest
from pathlib import Path
from arch_sidecar.polyglot_decouple import decouple_polyglot_symbol


def test_ts_decouple_and_alias(tmp_path: Path):
    src = tmp_path / "service.ts"
    dest = tmp_path / "models.ts"

    src.write_text("""
export interface UserPayload {
    id: string;
    email: string;
}

export class UserService {
    getUser(): UserPayload {
        return { id: "1", email: "test@example.com" };
    }
}
""")

    res = decouple_polyglot_symbol(
        symbol="UserPayload",
        from_file=str(src),
        to_file=str(dest),
        project_root=str(tmp_path),
        add_backward_compat_alias=True,
    )

    assert res["success"] is True
    assert dest.exists()

    # Destination should have UserPayload
    dest_content = dest.read_text()
    assert "export interface UserPayload" in dest_content

    # Source should have backward-compatibility alias re-export
    src_content = src.read_text()
    assert 'export { UserPayload } from "./models";' in src_content
    assert "class UserService" in src_content


def test_ts_decouple_transactional_rollback_on_test_failure(tmp_path: Path):
    src = tmp_path / "utils.ts"
    dest = tmp_path / "helpers.ts"

    orig_content = """export function add(a: number, b: number): number {
    return a + b;
}
"""
    src.write_text(orig_content)

    # Force test failure via failing shell command
    res = decouple_polyglot_symbol(
        symbol="add",
        from_file=str(src),
        to_file=str(dest),
        project_root=str(tmp_path),
        test_cmd="exit 1",
    )

    assert res["success"] is False
    assert "Transaction rolled back" in res["error"]

    # Source file MUST be restored to original contents
    assert src.read_text() == orig_content
    # Destination file MUST not exist (since it didn't exist before)
    assert not dest.exists()


def test_go_decouple(tmp_path: Path):
    pkg_dir = tmp_path / "mypkg"
    pkg_dir.mkdir()
    src = pkg_dir / "math.go"
    dest = pkg_dir / "calc.go"

    src.write_text("""package mypkg

func Add(a int, b int) int {
    return a + b
}

func Multiply(a int, b int) int {
    return a * b
}
""")

    res = decouple_polyglot_symbol(
        symbol="Multiply",
        from_file=str(src),
        to_file=str(dest),
        project_root=str(tmp_path),
    )

    assert res["success"] is True
    assert dest.exists()
    assert "func Multiply" in dest.read_text()
    assert "package mypkg" in dest.read_text()
    assert "func Multiply" not in src.read_text()
    assert "func Add" in src.read_text()


def test_rust_decouple(tmp_path: Path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    src = src_dir / "lib.rs"
    dest = src_dir / "models.rs"

    src.write_text("""
pub struct Config {
    pub port: u16,
}

pub fn run() {}
""")

    res = decouple_polyglot_symbol(
        symbol="Config",
        from_file=str(src),
        to_file=str(dest),
        project_root=str(tmp_path),
        add_backward_compat_alias=True,
    )

    assert res["success"] is True
    assert dest.exists()
    assert "pub struct Config" in dest.read_text()
    assert "pub use crate::models::Config;" in src.read_text()


def test_ts_decouple_migrates_dependent_imports(tmp_path: Path):
    src = tmp_path / "controller.ts"
    dest = tmp_path / "handler.ts"
    models = tmp_path / "types.ts"

    models.write_text("export interface ReqContext { user: string; }\n")
    src.write_text("""
import { ReqContext } from "./types";

export function handleRequest(ctx: ReqContext): string {
    return ctx.user;
}

export function other(): void {}
""")

    res = decouple_polyglot_symbol(
        symbol="handleRequest",
        from_file=str(src),
        to_file=str(dest),
        project_root=str(tmp_path),
        add_backward_compat_alias=True,
    )

    assert res["success"] is True
    assert dest.exists()
    dest_content = dest.read_text()
    # ReqContext import MUST have been migrated to dest.ts!
    assert 'import { ReqContext } from "./types";' in dest_content
    assert "export function handleRequest" in dest_content
