import pytest
from arch_sidecar.go_ast import parse_go_ast
from arch_sidecar.rust_ast import parse_rust_ast
from arch_sidecar.ts_ast import parse_ts_js_ast


def test_go_ast_generics_and_grouped_constants():
    go_code = """package test

const (
    StatusActive = "active"
    StatusPending = "pending"
    _
    StatusFailed = "failed"
)

var (
    GlobalCounter int = 0
)

func Map[T, U any](slice []T, f func(T) U) []U {
    res := make([]U, len(slice))
    return res
}

type Set[T comparable] struct {
    elements map[T]struct{}
}
"""
    parsed = parse_go_ast(go_code, "test.go")
    decls = {d["name"]: d for d in parsed["declarations"]}

    assert "StatusActive" in decls
    assert decls["StatusActive"]["kind"] == "const"
    assert "StatusPending" in decls
    assert "StatusFailed" in decls
    assert "GlobalCounter" in decls
    assert decls["GlobalCounter"]["kind"] == "var"

    assert "Map" in decls
    assert decls["Map"]["kind"] == "function"
    assert "Set" in decls
    assert decls["Set"]["kind"] == "struct"


def test_rust_ast_macros_and_modifiers():
    rust_code = """
macro_rules! my_log {
    ($msg:expr) => { println!("{}", $msg); };
}

pub mod engine;

pub const MAX_CAPACITY: usize = 1024;
static INSTANCE_ID: u64 = 42;

pub const unsafe fn raw_ptr_lookup(ptr: *const u8) -> u8 {
    *ptr
}

impl<T: Clone + Send> MyTrait<T> for Container<T> {
    fn process(&self) {}
}
"""
    parsed = parse_rust_ast(rust_code, "test.rs")
    decls = {d["name"]: d for d in parsed["declarations"]}

    assert "my_log" in decls
    assert decls["my_log"]["kind"] == "macro"

    assert "engine" in decls
    assert decls["engine"]["kind"] == "module"

    assert "MAX_CAPACITY" in decls
    assert decls["MAX_CAPACITY"]["kind"] == "const"

    assert "raw_ptr_lookup" in decls
    assert decls["raw_ptr_lookup"]["kind"] == "function"

    assert "Container" in decls
    assert decls["Container"]["kind"] == "impl"


def test_ts_ast_methods_getters_and_local_var_pruning():
    ts_code = """
export class ServiceManager {
    public constructor() {}

    public async executeTask(name: string): Promise<boolean> {
        const localFlag = true;
        let counter = 0;
        return localFlag;
    }

    public get active(): boolean {
        return true;
    }

    public set active(v: boolean) {
        // setter
    }

    private helper(): void {}
}

export const API_URL = "https://api.example.com";
"""
    parsed = parse_ts_js_ast(ts_code, "test.ts")
    decls = {d["name"]: d for d in parsed["declarations"]}

    # Top-level class and exported const must be present
    assert "ServiceManager" in decls
    assert "API_URL" in decls

    # Class methods must be present
    assert "ServiceManager.executeTask" in decls
    assert "ServiceManager.active" in decls
    assert "ServiceManager.helper" in decls

    # Local variables inside executeTask MUST NOT be extracted
    assert "localFlag" not in decls
    assert "counter" not in decls


def test_rust_ast_preserves_attributes_in_start_line():
    rust_code = """
#[derive(Debug, Clone)]
#[serde(rename_all = "snake_case")]
pub struct Account {
    pub id: u64,
}
"""
    parsed = parse_rust_ast(rust_code, "test.rs")
    decls = {d["name"]: d for d in parsed["declarations"]}

    assert "Account" in decls
    acc = decls["Account"]
    # Start line must include the #[derive] attribute
    assert "#[derive(Debug, Clone)]" in acc["code"]
    assert acc["start_line"] == 2

    # Local variables inside executeTask MUST NOT be extracted
    assert "localFlag" not in decls
    assert "counter" not in decls
