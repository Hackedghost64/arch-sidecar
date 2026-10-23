import pytest
from arch_sidecar.polyglot_resolver import (
    check_ts_lexical_scope,
    check_go_lexical_scope,
    check_rust_lexical_scope,
)


def test_ts_lexical_scope_detects_hallucinated_ident():
    code_with_typo = """
export function calculateTotal(items: number[]): number {
    const sum = items.reduce((acc, x) => acc + x, 0);
    return sum + nonExistentTaxRate;
}
"""
    issues = check_ts_lexical_scope(code_with_typo, filename="calc.ts")
    assert len(issues) >= 1
    assert any("nonExistentTaxRate" in iss for iss in issues)


def test_ts_lexical_scope_clean_code():
    clean_code = """
import { helper } from "./helper";

export class Calculator {
    public compute(val: number): number {
        const bonus = 10;
        return helper(val) + bonus;
    }
}
"""
    issues = check_ts_lexical_scope(clean_code, filename="clean.ts")
    assert len(issues) == 0


def test_go_lexical_scope_detects_unimported_pkg():
    code_with_unimported_pkg = """package main

func Run() {
    Fmt.Println("Hello world")
}
"""
    issues = check_go_lexical_scope(code_with_unimported_pkg, filename="main.go")
    assert len(issues) >= 1
    assert any("Fmt" in iss for iss in issues)


def test_rust_lexical_scope_detects_unresolved_crate():
    code_with_unresolved_crate = """
pub fn process() {
    unknown_crate::init();
}
"""
    issues = check_rust_lexical_scope(code_with_unresolved_crate, filename="lib.rs")
    assert len(issues) >= 1
    assert any("unknown_crate" in iss for iss in issues)
