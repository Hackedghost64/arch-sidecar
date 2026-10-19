import os
import ast
import sys
import builtins
import difflib
import time
from typing import Dict, List, Set, Any, Optional, Tuple

BUILTIN_NAMES = set(dir(builtins)) | {
    "__name__", "__doc__", "__file__", "__path__", "__package__",
    "__cached__", "__spec__", "__annotations__", "__dict__",
    "__all__", "__loader__", "__builtins__", "self", "cls"
}


def _collect_bound_names_in_block(stmts: List[ast.stmt], bound: Set[str]) -> None:
    """
    Pass 1 of two-pass lexical scope binding:
    Pre-scans a block of statements (e.g. a module body or function body) and records
    all names bound anywhere in that scope before descending into nested scopes.
    This ensures forward function/class references and late variable bindings within
    the same lexical scope do not trigger false-positive UNDEFINED_SYMBOL errors.
    """
    for stmt in stmts:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(stmt.name)
        elif isinstance(stmt, ast.Import):
            for alias in stmt.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(stmt, ast.ImportFrom):
            for alias in stmt.names:
                if alias.name == "*":
                    bound.add("*")
                else:
                    bound.add(alias.asname or alias.name)
        elif isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                _bind_target_names(t, bound)
        elif isinstance(stmt, ast.AnnAssign):
            _bind_target_names(stmt.target, bound)
        elif isinstance(stmt, ast.AugAssign):
            _bind_target_names(stmt.target, bound)
        elif isinstance(stmt, (ast.For, ast.AsyncFor)):
            _bind_target_names(stmt.target, bound)
            _collect_bound_names_in_block(stmt.body, bound)
            _collect_bound_names_in_block(stmt.orelse, bound)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                if item.optional_vars:
                    _bind_target_names(item.optional_vars, bound)
            _collect_bound_names_in_block(stmt.body, bound)
        elif isinstance(stmt, ast.If):
            _collect_bound_names_in_block(stmt.body, bound)
            _collect_bound_names_in_block(stmt.orelse, bound)
        elif isinstance(stmt, ast.While):
            _collect_bound_names_in_block(stmt.body, bound)
            _collect_bound_names_in_block(stmt.orelse, bound)
        elif isinstance(stmt, ast.Try):
            _collect_bound_names_in_block(stmt.body, bound)
            for handler in stmt.handlers:
                if handler.name:
                    bound.add(handler.name)
                _collect_bound_names_in_block(handler.body, bound)
            _collect_bound_names_in_block(stmt.orelse, bound)
            _collect_bound_names_in_block(stmt.finalbody, bound)
        elif hasattr(ast, "TryStar") and isinstance(stmt, getattr(ast, "TryStar")):
            _collect_bound_names_in_block(stmt.body, bound)
            for handler in stmt.handlers:
                if handler.name:
                    bound.add(handler.name)
                _collect_bound_names_in_block(handler.body, bound)
            _collect_bound_names_in_block(stmt.orelse, bound)
            _collect_bound_names_in_block(stmt.finalbody, bound)
        elif isinstance(stmt, (ast.Global, ast.Nonlocal)):
            for name in stmt.names:
                bound.add(name)
        elif hasattr(ast, "Match") and isinstance(stmt, getattr(ast, "Match")):
            for case in stmt.cases:
                _bind_match_pattern(case.pattern, bound)
                _collect_bound_names_in_block(case.body, bound)
        elif hasattr(ast, " TypeAlias") and isinstance(stmt, getattr(ast, "TypeAlias")):
            if isinstance(stmt.name, ast.Name):
                bound.add(stmt.name.id)


def _bind_target_names(target: ast.AST, bound: Set[str]) -> None:
    if isinstance(target, ast.Name):
        bound.add(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            _bind_target_names(elt, bound)
    elif isinstance(target, ast.Starred):
        _bind_target_names(target.value, bound)


def _bind_match_pattern(pattern: ast.AST, bound: Set[str]) -> None:
    for sub in ast.walk(pattern):
        if hasattr(ast, "MatchAs") and isinstance(sub, getattr(ast, "MatchAs")) and sub.name:
            bound.add(sub.name)
        elif hasattr(ast, "MatchStar") and isinstance(sub, getattr(ast, "MatchStar")) and sub.name:
            bound.add(sub.name)
        elif hasattr(ast, "MatchMapping") and isinstance(sub, getattr(ast, "MatchMapping")) and sub.rest:
            bound.add(sub.rest)


class ScopeCollector(ast.NodeVisitor):
    """
    Two-pass lexical scope analyzer that detects undefined/hallucinated identifiers
    while accurately supporting forward references, comprehensions, lambdas,
    walrus expressions (:=), type annotations, and nested closures.
    """
    def __init__(self, postponed_annotations: bool = False):
        self.module_scope: Set[str] = set()
        self.scope_stack: List[Set[str]] = [self.module_scope]
        self.undefined_references: List[Tuple[int, str]] = []
        self.postponed_annotations = postponed_annotations
        self.has_wildcard_import = False

    def current_scope(self) -> Set[str]:
        return self.scope_stack[-1]

    def is_defined(self, name: str) -> bool:
        if self.has_wildcard_import or name in BUILTIN_NAMES:
            return True
        for scope in reversed(self.scope_stack):
            if name in scope or "*" in scope:
                return True
        return False

    def visit_Module(self, node: ast.Module):
        _collect_bound_names_in_block(node.body, self.module_scope)
        if "*" in self.module_scope:
            self.has_wildcard_import = True
        for stmt in node.body:
            self.visit(stmt)

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            name = alias.asname or alias.name.split(".")[0]
            self.current_scope().add(name)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        for alias in node.names:
            if alias.name == "*":
                self.current_scope().add("*")
                self.has_wildcard_import = True
                continue
            name = alias.asname or alias.name
            self.current_scope().add(name)

    def _visit_function_like(self, node: ast.FunctionDef | ast.AsyncFunctionDef):
        self.current_scope().add(node.name)
        # Visit decorators and default argument values in outer scope
        for dec in node.decorator_list:
            self.visit(dec)
        for default in node.args.defaults:
            self.visit(default)
        for default in node.args.kw_defaults:
            if default is not None:
                self.visit(default)
        if not self.postponed_annotations:
            if node.returns:
                self.visit(node.returns)
            for arg in (
                getattr(node.args, "posonlyargs", [])
                + node.args.args
                + node.args.kwonlyargs
            ):
                if arg.annotation:
                    self.visit(arg.annotation)
            if node.args.vararg and node.args.vararg.annotation:
                self.visit(node.args.vararg.annotation)
            if node.args.kwarg and node.args.kwarg.annotation:
                self.visit(node.args.kwarg.annotation)

        func_scope: Set[str] = set()
        for arg in getattr(node.args, "posonlyargs", []):
            func_scope.add(arg.arg)
        for arg in node.args.args:
            func_scope.add(arg.arg)
        if node.args.vararg:
            func_scope.add(node.args.vararg.arg)
        for arg in node.args.kwonlyargs:
            func_scope.add(arg.arg)
        if node.args.kwarg:
            func_scope.add(node.args.kwarg.arg)

        # Pre-collect local bindings inside the function body
        _collect_bound_names_in_block(node.body, func_scope)

        self.scope_stack.append(func_scope)
        for item in node.body:
            self.visit(item)
        self.scope_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._visit_function_like(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self._visit_function_like(node)

    def visit_Lambda(self, node: ast.Lambda):
        for default in node.args.defaults:
            self.visit(default)
        for default in node.args.kw_defaults:
            if default is not None:
                self.visit(default)
        lam_scope: Set[str] = set()
        for arg in getattr(node.args, "posonlyargs", []):
            lam_scope.add(arg.arg)
        for arg in node.args.args:
            lam_scope.add(arg.arg)
        if node.args.vararg:
            lam_scope.add(node.args.vararg.arg)
        for arg in node.args.kwonlyargs:
            lam_scope.add(arg.arg)
        if node.args.kwarg:
            lam_scope.add(node.args.kwarg.arg)

        self.scope_stack.append(lam_scope)
        self.visit(node.body)
        self.scope_stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef):
        self.current_scope().add(node.name)
        for dec in node.decorator_list:
            self.visit(dec)
        for base in node.bases:
            self.visit(base)
        for kw in node.keywords:
            self.visit(kw.value)

        class_scope: Set[str] = set()
        _collect_bound_names_in_block(node.body, class_scope)
        self.scope_stack.append(class_scope)
        for item in node.body:
            self.visit(item)
        self.scope_stack.pop()

    def visit_Assign(self, node: ast.Assign):
        self.visit(node.value)
        for target in node.targets:
            _bind_target_names(target, self.current_scope())
            self._visit_subscript_or_attribute_targets(target)

    def visit_AnnAssign(self, node: ast.AnnAssign):
        if node.value:
            self.visit(node.value)
        if not self.postponed_annotations and node.annotation:
            self.visit(node.annotation)
        _bind_target_names(node.target, self.current_scope())
        self._visit_subscript_or_attribute_targets(node.target)

    def visit_AugAssign(self, node: ast.AugAssign):
        self.visit(node.value)
        if isinstance(node.target, ast.Name):
            if not self.is_defined(node.target.id):
                self.undefined_references.append((node.lineno, node.target.id))
            self.current_scope().add(node.target.id)
        else:
            self._visit_subscript_or_attribute_targets(node.target)

    def _visit_subscript_or_attribute_targets(self, target: ast.AST):
        """When assigning to `obj.attr = 1` or `arr[i] = 1`, verify `obj`, `arr`, `i` are defined."""
        if isinstance(target, ast.Attribute):
            self.visit(target.value)
        elif isinstance(target, ast.Subscript):
            self.visit(target.value)
            self.visit(target.slice)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._visit_subscript_or_attribute_targets(elt)

    def visit_NamedExpr(self, node: ast.NamedExpr):
        self.visit(node.value)
        _bind_target_names(node.target, self.current_scope())

    def _visit_comprehension_generators(self, generators: List[ast.comprehension], comp_scope: Set[str]):
        for gen in generators:
            self.visit(gen.iter)
            _bind_target_names(gen.target, comp_scope)
            for if_clause in gen.ifs:
                self.visit(if_clause)

    def visit_ListComp(self, node: ast.ListComp):
        comp_scope: Set[str] = set()
        self.scope_stack.append(comp_scope)
        self._visit_comprehension_generators(node.generators, comp_scope)
        self.visit(node.elt)
        self.scope_stack.pop()

    def visit_SetComp(self, node: ast.SetComp):
        comp_scope: Set[str] = set()
        self.scope_stack.append(comp_scope)
        self._visit_comprehension_generators(node.generators, comp_scope)
        self.visit(node.elt)
        self.scope_stack.pop()

    def visit_GeneratorExp(self, node: ast.GeneratorExp):
        comp_scope: Set[str] = set()
        self.scope_stack.append(comp_scope)
        self._visit_comprehension_generators(node.generators, comp_scope)
        self.visit(node.elt)
        self.scope_stack.pop()

    def visit_DictComp(self, node: ast.DictComp):
        comp_scope: Set[str] = set()
        self.scope_stack.append(comp_scope)
        self._visit_comprehension_generators(node.generators, comp_scope)
        self.visit(node.key)
        self.visit(node.value)
        self.scope_stack.pop()

    def visit_For(self, node: ast.For):
        self.visit(node.iter)
        _bind_target_names(node.target, self.current_scope())
        for item in node.body:
            self.visit(item)
        for item in node.orelse:
            self.visit(item)

    def visit_AsyncFor(self, node: ast.AsyncFor):
        self.visit(node.iter)
        _bind_target_names(node.target, self.current_scope())
        for item in node.body:
            self.visit(item)
        for item in node.orelse:
            self.visit(item)

    def visit_ExceptHandler(self, node: ast.ExceptHandler):
        if node.type:
            self.visit(node.type)
        if node.name:
            self.current_scope().add(node.name)
        for item in node.body:
            self.visit(item)

    def visit_With(self, node: ast.With):
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars:
                _bind_target_names(item.optional_vars, self.current_scope())
        for b in node.body:
            self.visit(b)

    def visit_AsyncWith(self, node: ast.AsyncWith):
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars:
                _bind_target_names(item.optional_vars, self.current_scope())
        for b in node.body:
            self.visit(b)

    def visit_Global(self, node: ast.Global):
        for name in node.names:
            self.current_scope().add(name)

    def visit_Nonlocal(self, node: ast.Nonlocal):
        for name in node.names:
            self.current_scope().add(name)

    def visit_Name(self, node: ast.Name):
        if isinstance(node.ctx, ast.Load):
            if not self.is_defined(node.id):
                self.undefined_references.append((node.lineno, node.id))
        elif isinstance(node.ctx, ast.Store):
            self.current_scope().add(node.id)


def resolve_local_module_path(target_path: str, import_mod: str, project_root: str) -> Optional[str]:
    """Resolves an imported module name or relative path to a local project file if it exists."""
    target_dir = os.path.dirname(os.path.abspath(target_path))
    root_abs = os.path.abspath(project_root)

    # 1. Check relative to target_dir (e.g. .other, ..other, or same-directory sibling)
    if import_mod.startswith("."):
        dots = len(import_mod) - len(import_mod.lstrip("."))
        sub_mod = import_mod.lstrip(".")
        base_dir = target_dir
        for _ in range(dots - 1):
            base_dir = os.path.dirname(base_dir)

        if sub_mod:
            cand = os.path.join(base_dir, sub_mod.replace(".", os.sep) + ".py")
            if os.path.exists(cand):
                return cand
            cand_pkg = os.path.join(base_dir, sub_mod.replace(".", os.sep), "__init__.py")
            if os.path.exists(cand_pkg):
                return cand_pkg
        else:
            cand_pkg = os.path.join(base_dir, "__init__.py")
            if os.path.exists(cand_pkg):
                return cand_pkg
        return None

    # 2. Check same directory as target_path
    cand_same = os.path.join(target_dir, import_mod.replace(".", os.sep) + ".py")
    if os.path.exists(cand_same):
        return cand_same

    # 3. Check from project_root
    cand = os.path.join(root_abs, import_mod.replace(".", os.sep) + ".py")
    if os.path.exists(cand):
        return cand
    cand_pkg = os.path.join(root_abs, import_mod.replace(".", os.sep), "__init__.py")
    if os.path.exists(cand_pkg):
        return cand_pkg

    # 4. Check under common src/ directories
    for src_dir in ["src", "lib"]:
        cand = os.path.join(root_abs, src_dir, import_mod.replace(".", os.sep) + ".py")
        if os.path.exists(cand):
            return cand
        cand_pkg = os.path.join(root_abs, src_dir, import_mod.replace(".", os.sep), "__init__.py")
        if os.path.exists(cand_pkg):
            return cand_pkg

    return None


def get_exported_symbols_of_file(file_path: str) -> Optional[Set[str]]:
    """
    Returns the set of top-level defined/imported names in `file_path`,
    or None if the file cannot be parsed or uses wildcard `from ... import *`.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=file_path)
    except Exception:
        return None

    exported: Set[str] = set()
    _collect_bound_names_in_block(tree.body, exported)
    if "*" in exported or "__getattr__" in exported:
        return None
    # Also include submodules if file_path is __init__.py
    if os.path.basename(file_path) == "__init__.py":
        pkg_dir = os.path.dirname(file_path)
        try:
            for entry in os.listdir(pkg_dir):
                if entry.endswith(".py") and entry != "__init__.py":
                    exported.add(entry[:-3])
                elif os.path.isdir(os.path.join(pkg_dir, entry)):
                    exported.add(entry)
        except OSError:
            pass
    return exported


def get_module_imported_files(file_path: str, project_root: str) -> List[Tuple[int, str, str]]:
    """Returns list of (line, import_str, resolved_file_path) for local project files imported by file_path."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=file_path)
    except Exception:
        return []

    imported: List[Tuple[int, str, str]] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                resolved = resolve_local_module_path(file_path, alias.name, project_root)
                if resolved:
                    imported.append((node.lineno, alias.name, resolved))
        elif isinstance(node, ast.ImportFrom):
            mod_name = node.module or ""
            if node.level > 0:
                mod_name = ("." * node.level) + mod_name
            resolved = resolve_local_module_path(file_path, mod_name, project_root)
            if resolved:
                imported.append((node.lineno, mod_name, resolved))
    return imported


def check_code_health(
    target_path: str,
    project_root: str = "."
) -> Dict[str, Any]:
    """
    Compiler-Grade Preflight & Architecture Health Check for AI Coding Agents:
    1. Syntax & AST Validation (<5ms)
    2. Hallucinated / Undefined Symbol Detection via two-pass lexical scope analysis (<10ms)
    3. Cross-Module Local Import Verification (`BROKEN_LOCAL_IMPORT` with closest-match suggestions)
    4. Multi-Module Circular Import Cycle Detection (`CIRCULAR_IMPORT`)
    """
    start_time = time.perf_counter()
    target_abs = os.path.abspath(target_path)
    root_abs = os.path.abspath(project_root)
    rel_path = os.path.relpath(target_abs, root_abs)

    if not os.path.exists(target_abs):
        return {
            "success": False,
            "error": f"File does not exist: {target_path}",
            "health_score": 0.0,
            "issues": [{
                "severity": "ERROR",
                "type": "FILE_NOT_FOUND",
                "line": 0,
                "message": f"Target file '{target_path}' not found."
            }],
            "duration_ms": 0.0
        }

    try:
        with open(target_abs, "r", encoding="utf-8") as f:
            source = f.read()
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to read file: {e}",
            "health_score": 0.0,
            "issues": [],
            "duration_ms": 0.0
        }

    # 1. AST Syntax Check
    try:
        tree = ast.parse(source, filename=target_abs)
    except SyntaxError as e:
        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
        return {
            "success": False,
            "file": rel_path,
            "health_score": 0.0,
            "total_issues": 1,
            "errors": 1,
            "warnings": 0,
            "issues": [{
                "severity": "ERROR",
                "type": "SYNTAX_ERROR",
                "line": e.lineno or 0,
                "column": e.offset or 0,
                "message": f"SyntaxError at line {e.lineno}: {e.msg}"
            }],
            "duration_ms": duration_ms
        }

    issues: List[Dict[str, Any]] = []

    # Check if `from __future__ import annotations` is active
    postponed_annotations = any(
        isinstance(n, ast.ImportFrom)
        and n.module == "__future__"
        and any(a.name == "annotations" for a in n.names)
        for n in tree.body
    )

    # 2. Scope & Undefined / Hallucinated Symbols Analysis
    scope_analyzer = ScopeCollector(postponed_annotations=postponed_annotations)
    scope_analyzer.visit(tree)

    seen_undefined = set()
    for lineno, sym in scope_analyzer.undefined_references:
        if sym not in seen_undefined:
            seen_undefined.add(sym)
            issues.append({
                "severity": "ERROR",
                "type": "UNDEFINED_SYMBOL",
                "line": lineno,
                "symbol": sym,
                "message": f"Undefined symbol '{sym}' loaded at line {lineno}. Missing import, misnamed symbol, or hallucinated API.",
                "recommendation": f"Import '{sym}' from its defining module or define it before use."
            })

    # 3. Cross-Module Local Import Verification (`BROKEN_LOCAL_IMPORT`)
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                continue
            mod_name = node.module or ""
            if node.level > 0:
                mod_name = ("." * node.level) + mod_name
            resolved = resolve_local_module_path(target_abs, mod_name, root_abs)
            if resolved and os.path.abspath(resolved) != target_abs:
                exported = get_exported_symbols_of_file(resolved)
                if exported is not None:
                    resolved_rel = os.path.relpath(resolved, root_abs)
                    for alias in node.names:
                        if alias.name == "*":
                            continue
                        if alias.name not in exported:
                            close = difflib.get_close_matches(alias.name, list(exported), n=3, cutoff=0.5)
                            hint = f" Did you mean: {', '.join(close)}?" if close else ""
                            issues.append({
                                "severity": "ERROR",
                                "type": "BROKEN_LOCAL_IMPORT",
                                "line": node.lineno,
                                "symbol": alias.name,
                                "target_module": resolved_rel,
                                "message": f"Cannot import '{alias.name}' from local module '{resolved_rel}' (symbol does not exist in target file).{hint}",
                                "recommendation": f"Verify symbol name in '{resolved_rel}' or export '{alias.name}' there.{hint}"
                            })

    # 4. Circular Import Detection
    imported_local_files = get_module_imported_files(target_abs, root_abs)
    seen_cycles = set()
    for lineno, import_str, imported_abs in imported_local_files:
        if imported_abs == target_abs:
            issues.append({
                "severity": "ERROR",
                "type": "SELF_IMPORT",
                "line": lineno,
                "message": f"Module '{rel_path}' attempts to import itself.",
                "recommendation": "Remove self-import statement."
            })
            continue

        back_imports = get_module_imported_files(imported_abs, root_abs)
        for back_line, back_mod_str, back_resolved in back_imports:
            if back_resolved == target_abs:
                imported_rel = os.path.relpath(imported_abs, root_abs)
                cycle_key = (rel_path, imported_rel)
                if cycle_key not in seen_cycles:
                    seen_cycles.add(cycle_key)
                    issues.append({
                        "severity": "ERROR",
                        "type": "CIRCULAR_IMPORT",
                        "line": lineno,
                        "cycle": f"{rel_path} -> {imported_rel} -> {rel_path}",
                        "message": f"Circular import cycle: '{rel_path}' imports '{imported_rel}', which imports back to '{rel_path}' (at line {back_line}).",
                        "recommendation": "Extract shared symbols, classes, or state into a leaf module to form a Directed Acyclic Graph (DAG)."
                    })

    # 5. Compute Health Score
    error_count = sum(1 for i in issues if i["severity"] == "ERROR")
    warning_count = sum(1 for i in issues if i["severity"] == "WARNING")

    health_score = max(0.0, 1.0 - (0.25 * error_count) - (0.05 * warning_count))
    duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

    return {
        "success": error_count == 0,
        "file": rel_path,
        "health_score": round(health_score, 2),
        "total_issues": len(issues),
        "errors": error_count,
        "warnings": warning_count,
        "issues": issues,
        "duration_ms": duration_ms
    }
