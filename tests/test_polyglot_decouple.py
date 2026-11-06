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


def test_multi_symbol_typescript_decouple(tmp_path: Path):
    # 1. Test moving a List[str] of symbols with backward-compatibility re-export,
    #    import migration, and local dependencies referenced tracking
    src = tmp_path / "service.ts"
    dest = tmp_path / "helpers.ts"
    types = tmp_path / "types.ts"

    types.write_text("export interface UserContext { id: string; role: string; }\n")
    src.write_text("""import { UserContext } from "./types";

export function localHelper(c: UserContext): string {
    return c.id.trim();
}

export function helperOne(c: UserContext): string {
    return localHelper(c) + "-1";
}

export function helperTwo(c: UserContext): string {
    return localHelper(c) + "-2";
}

export function mainService(c: UserContext): string {
    return helperOne(c) + ":" + helperTwo(c);
}
""")

    res = decouple_polyglot_symbol(
        symbol=["helperOne", "helperTwo"],
        from_file=str(src),
        to_file=str(dest),
        project_root=str(tmp_path),
        add_backward_compat_alias=True,
    )

    assert res["success"] is True
    assert res["extracted_symbols"] == ["helperOne", "helperTwo"]
    assert "localHelper" in res["local_dependencies_referenced"]
    assert dest.exists()

    dest_content = dest.read_text()
    # Import from ./types must be migrated
    assert 'import { UserContext } from "./types";' in dest_content
    assert "export function helperOne(c: UserContext): string" in dest_content
    assert "export function helperTwo(c: UserContext): string" in dest_content

    src_content = src.read_text()
    assert 'export { helperOne, helperTwo } from "./helpers";' in src_content
    assert "function helperOne" not in src_content.replace('export { helperOne, helperTwo } from "./helpers";', "")
    assert "function helperTwo" not in src_content.replace('export { helperOne, helperTwo } from "./helpers";', "")
    assert "function localHelper" in src_content
    assert "function mainService" in src_content

    # 2. Test comma-separated string format
    src2 = tmp_path / "calc.ts"
    dest2 = tmp_path / "ops.ts"
    src2.write_text("""export function add(a: number, b: number): number {
    return a + b;
}

export function subtract(a: number, b: number): number {
    return a - b;
}

export function compute(a: number, b: number): number {
    return add(a, b);
}
""")

    res2 = decouple_polyglot_symbol(
        symbol="add, subtract",
        from_file=str(src2),
        to_file=str(dest2),
        project_root=str(tmp_path),
        add_backward_compat_alias=True,
    )

    assert res2["success"] is True
    assert res2["extracted_symbols"] == ["add", "subtract"]
    assert dest2.exists()
    assert "export function add" in dest2.read_text()
    assert "export function subtract" in dest2.read_text()
    assert 'export { add, subtract } from "./ops";' in src2.read_text()


def test_multi_symbol_go_and_rust_decouple(tmp_path: Path):
    # 1. Go multi-symbol decouple
    pkg_dir = tmp_path / "mypkg"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    src_go = pkg_dir / "math.go"
    dest_go = pkg_dir / "helpers.go"

    src_go.write_text("""package mypkg

import "fmt"

func CoreBase(x int) int {
    return x * 2
}

func HelperOne(a int) int {
    fmt.Println("H1")
    return CoreBase(a) + 1
}

func HelperTwo(b int) int {
    return CoreBase(b) + 2
}

func Runner() int {
    return HelperOne(1) + HelperTwo(2)
}
""")

    res_go = decouple_polyglot_symbol(
        symbol=["HelperOne", "HelperTwo"],
        from_file=str(src_go),
        to_file=str(dest_go),
        project_root=str(tmp_path),
    )

    assert res_go["success"] is True
    assert res_go["extracted_symbols"] == ["HelperOne", "HelperTwo"]
    assert "CoreBase" in res_go["local_dependencies_referenced"]
    assert dest_go.exists()

    dest_go_content = dest_go.read_text()
    assert "package mypkg" in dest_go_content
    assert 'import "fmt"' in dest_go_content
    assert "func HelperOne" in dest_go_content
    assert "func HelperTwo" in dest_go_content

    src_go_content = src_go.read_text()
    assert "func HelperOne" not in src_go_content
    assert "func HelperTwo" not in src_go_content
    assert "func CoreBase" in src_go_content
    assert "func Runner" in src_go_content

    # 2. Rust multi-symbol decouple with comma-separated string
    rust_dir = tmp_path / "rust_src"
    rust_dir.mkdir(parents=True, exist_ok=True)
    src_rs = rust_dir / "lib.rs"
    dest_rs = rust_dir / "helpers.rs"

    src_rs.write_text("""use std::path::PathBuf;

pub fn root_calc(p: PathBuf) -> usize {
    p.as_os_str().len()
}

pub fn helper_one(p: PathBuf) -> usize {
    root_calc(p) + 1
}

pub fn helper_two(p: PathBuf) -> usize {
    root_calc(p) + 2
}

pub fn entrypoint() {}
""")

    res_rs = decouple_polyglot_symbol(
        symbol="helper_one,helper_two",
        from_file=str(src_rs),
        to_file=str(dest_rs),
        project_root=str(tmp_path),
        add_backward_compat_alias=True,
    )

    assert res_rs["success"] is True
    assert res_rs["extracted_symbols"] == ["helper_one", "helper_two"]
    assert "root_calc" in res_rs["local_dependencies_referenced"]
    assert dest_rs.exists()

    dest_rs_content = dest_rs.read_text()
    assert "use std::path::PathBuf;" in dest_rs_content
    assert "pub fn helper_one" in dest_rs_content
    assert "pub fn helper_two" in dest_rs_content

    src_rs_content = src_rs.read_text()
    assert "pub use crate::helpers::{helper_one, helper_two};" in src_rs_content
    assert "fn helper_one" not in src_rs_content.replace("pub use crate::helpers::{helper_one, helper_two};", "")
    assert "fn helper_two" not in src_rs_content.replace("pub use crate::helpers::{helper_one, helper_two};", "")
    assert "pub fn root_calc" in src_rs_content
    assert "pub fn entrypoint" in src_rs_content


def test_multi_symbol_missing_symbol_rollback(tmp_path: Path):
    src = tmp_path / "mod.ts"
    dest = tmp_path / "extracted.ts"
    orig_content = "export function existsOne(): void {}\nexport function existsTwo(): void {}\n"
    src.write_text(orig_content)

    res = decouple_polyglot_symbol(
        symbol=["existsOne", "nonExistentSym"],
        from_file=str(src),
        to_file=str(dest),
        project_root=str(tmp_path),
    )

    assert res["success"] is False
    assert "Symbol(s) not found in" in res["error"]
    assert "nonExistentSym" in res["error"]
    assert src.read_text() == orig_content
    assert not dest.exists()


def test_multi_symbol_dart_decouple(tmp_path: Path):
    src = tmp_path / "manager.dart"
    dest = tmp_path / "session.dart"

    src.write_text("""class SessionInfo {
  final String id;
  SessionInfo(this.id);
}

class UserToken {
  final String token;
  UserToken(this.token);
}

class SessionManager {
  SessionInfo current = SessionInfo('abc');
}
""")

    res = decouple_polyglot_symbol(
        symbol=["SessionInfo", "UserToken"],
        from_file=str(src),
        to_file=str(dest),
        project_root=str(tmp_path),
        add_backward_compat_alias=True,
    )

    assert res["success"] is True
    assert res["extracted_symbols"] == ["SessionInfo", "UserToken"]
    assert dest.exists()

    src_content = src.read_text()
    assert "export './session.dart' show SessionInfo, UserToken;" in src_content
    assert "class SessionManager" in src_content
    assert "class SessionInfo" not in src_content
    assert "class UserToken" not in src_content

    dest_content = dest.read_text()
    assert "class SessionInfo" in dest_content
    assert "class UserToken" in dest_content


