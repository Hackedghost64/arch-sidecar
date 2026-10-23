import os
import ast
import sys
import json
import shutil
import subprocess
import builtins
import difflib
import importlib.util
import time
from typing import Dict, List, Set, Any, Optional, Tuple

from .utils import find_ruff_binary, run_ruff_autofix

BUILTIN_NAMES = set(dir(builtins)) | {
    "__name__", "__doc__", "__file__", "__path__", "__package__",
    "__cached__", "__spec__", "__annotations__", "__dict__",
    "__all__", "__loader__", "__builtins__", "self", "cls"
}


def _collect_declared_globals_and_nonlocals(stmts: List[ast.stmt], out: Set[str]) -> None:
    """Collects names declared via `global` or `nonlocal` within the current lexical block."""
    for stmt in stmts:
        if isinstance(stmt, (ast.Global, ast.Nonlocal)):
            for name in stmt.names:
                out.add(name)
        elif isinstance(stmt, (ast.For, ast.AsyncFor, ast.While, ast.If)):
            _collect_declared_globals_and_nonlocals(stmt.body, out)
            _collect_declared_globals_and_nonlocals(stmt.orelse, out)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            _collect_declared_globals_and_nonlocals(stmt.body, out)
        elif isinstance(stmt, ast.Try):
            _collect_declared_globals_and_nonlocals(stmt.body, out)
            for handler in stmt.handlers:
                _collect_declared_globals_and_nonlocals(handler.body, out)
            _collect_declared_globals_and_nonlocals(stmt.orelse, out)
            _collect_declared_globals_and_nonlocals(stmt.finalbody, out)
        elif hasattr(ast, "TryStar") and isinstance(stmt, getattr(ast, "TryStar")):
            _collect_declared_globals_and_nonlocals(stmt.body, out)
            for handler in stmt.handlers:
                _collect_declared_globals_and_nonlocals(handler.body, out)
            _collect_declared_globals_and_nonlocals(stmt.orelse, out)
            _collect_declared_globals_and_nonlocals(stmt.finalbody, out)
        elif hasattr(ast, "Match") and isinstance(stmt, getattr(ast, "Match")):
            for case in stmt.cases:
                _collect_declared_globals_and_nonlocals(case.body, out)


def _collect_bound_names_in_block(
    stmts: List[ast.stmt],
    bound: Set[str],
    excluded: Optional[Set[str]] = None
) -> None:
    """
    Pass 1 of two-pass lexical scope binding:
    Pre-scans a block of statements (e.g. a module body or function body) and records
    all names bound anywhere in that scope before descending into nested scopes.
    Names declared `global` or `nonlocal` in a function scope are excluded from local
    pre-binding so that reading an undefined global/nonlocal before assignment is caught.
    """
    if excluded is None:
        excluded = set()
        _collect_declared_globals_and_nonlocals(stmts, excluded)

    for stmt in stmts:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if stmt.name not in excluded:
                bound.add(stmt.name)
        elif isinstance(stmt, ast.Import):
            for alias in stmt.names:
                name = alias.asname or alias.name.split(".")[0]
                if name not in excluded:
                    bound.add(name)
        elif isinstance(stmt, ast.ImportFrom):
            for alias in stmt.names:
                if alias.name == "*":
                    bound.add("*")
                else:
                    name = alias.asname or alias.name
                    if name not in excluded:
                        bound.add(name)
        elif isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                _bind_target_names(t, bound, excluded)
        elif isinstance(stmt, ast.AnnAssign):
            _bind_target_names(stmt.target, bound, excluded)
        elif isinstance(stmt, ast.AugAssign):
            # AugAssign (x += 1) requires x to be defined before modifying it; do not pre-bind if in excluded
            _bind_target_names(stmt.target, bound, excluded)
        elif isinstance(stmt, (ast.For, ast.AsyncFor)):
            _bind_target_names(stmt.target, bound, excluded)
            _collect_bound_names_in_block(stmt.body, bound, excluded)
            _collect_bound_names_in_block(stmt.orelse, bound, excluded)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                if item.optional_vars:
                    _bind_target_names(item.optional_vars, bound, excluded)
            _collect_bound_names_in_block(stmt.body, bound, excluded)
        elif isinstance(stmt, ast.If):
            _collect_bound_names_in_block(stmt.body, bound, excluded)
            _collect_bound_names_in_block(stmt.orelse, bound, excluded)
        elif isinstance(stmt, ast.While):
            _collect_bound_names_in_block(stmt.body, bound, excluded)
            _collect_bound_names_in_block(stmt.orelse, bound, excluded)
        elif isinstance(stmt, ast.Try):
            _collect_bound_names_in_block(stmt.body, bound, excluded)
            for handler in stmt.handlers:
                if handler.name and handler.name not in excluded:
                    bound.add(handler.name)
                _collect_bound_names_in_block(handler.body, bound, excluded)
            _collect_bound_names_in_block(stmt.orelse, bound, excluded)
            _collect_bound_names_in_block(stmt.finalbody, bound, excluded)
        elif hasattr(ast, "TryStar") and isinstance(stmt, getattr(ast, "TryStar")):
            _collect_bound_names_in_block(stmt.body, bound, excluded)
            for handler in stmt.handlers:
                if handler.name and handler.name not in excluded:
                    bound.add(handler.name)
                _collect_bound_names_in_block(handler.body, bound, excluded)
            _collect_bound_names_in_block(stmt.orelse, bound, excluded)
            _collect_bound_names_in_block(stmt.finalbody, bound, excluded)
        elif hasattr(ast, "Match") and isinstance(stmt, getattr(ast, "Match")):
            for case in stmt.cases:
                _bind_match_pattern(case.pattern, bound, excluded)
                _collect_bound_names_in_block(case.body, bound, excluded)
        elif hasattr(ast, "TypeAlias") and isinstance(stmt, getattr(ast, "TypeAlias")):
            if isinstance(stmt.name, ast.Name) and stmt.name.id not in excluded:
                bound.add(stmt.name.id)


def _bind_target_names(target: ast.AST, bound: Set[str], excluded: Optional[Set[str]] = None) -> None:
    if isinstance(target, ast.Name):
        if excluded is None or target.id not in excluded:
            bound.add(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            _bind_target_names(elt, bound, excluded)
    elif isinstance(target, ast.Starred):
        _bind_target_names(target.value, bound, excluded)


def _bind_match_pattern(pattern: ast.AST, bound: Set[str], excluded: Optional[Set[str]] = None) -> None:
    for sub in ast.walk(pattern):
        if hasattr(ast, "MatchAs") and isinstance(sub, getattr(ast, "MatchAs")) and sub.name:
            if excluded is None or sub.name not in excluded:
                bound.add(sub.name)
        elif hasattr(ast, "MatchStar") and isinstance(sub, getattr(ast, "MatchStar")) and sub.name:
            if excluded is None or sub.name not in excluded:
                bound.add(sub.name)
        elif hasattr(ast, "MatchMapping") and isinstance(sub, getattr(ast, "MatchMapping")) and sub.rest:
            if excluded is None or sub.rest not in excluded:
                bound.add(sub.rest)


class ScopeCollector(ast.NodeVisitor):
    """
    Two-pass lexical scope analyzer that detects undefined/hallucinated identifiers
    while accurately supporting forward references, comprehensions, lambdas,
    walrus expressions (:=), type annotations, class body isolation, global/nonlocal
    binding semantics, and nested closures.
    """
    def __init__(
        self,
        postponed_annotations: bool = False,
        target_path: Optional[str] = None,
        project_root: Optional[str] = None,
    ):
        self.module_scope: Set[str] = set()
        self.scope_stack: List[Set[str]] = [self.module_scope]
        self.class_scope_ids: Set[int] = set()
        self.global_decls_stack: List[Set[str]] = [set()]
        self.nonlocal_decls_stack: List[Set[str]] = [set()]
        self.undefined_references: List[Tuple[int, str]] = []
        self.postponed_annotations = postponed_annotations
        self.has_wildcard_import = False
        self.target_path = target_path
        self.project_root = project_root or "."

    def current_scope(self) -> Set[str]:
        return self.scope_stack[-1]

    def _bind_name_in_active_scope(self, name: str) -> None:
        if name in self.global_decls_stack[-1]:
            self.module_scope.add(name)
        else:
            self.current_scope().add(name)

    def _bind_target_in_active_scope(self, target: ast.AST) -> None:
        if isinstance(target, ast.Name):
            self._bind_name_in_active_scope(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._bind_target_in_active_scope(elt)
        elif isinstance(target, ast.Starred):
            self._bind_target_in_active_scope(target.value)

    def is_defined(self, name: str) -> bool:
        if self.has_wildcard_import or name in BUILTIN_NAMES:
            return True
        if name in self.global_decls_stack[-1]:
            return name in self.module_scope or "*" in self.module_scope
        top_idx = len(self.scope_stack) - 1
        for idx in range(top_idx, -1, -1):
            scope = self.scope_stack[idx]
            # Python lexical scoping: class bodies are NOT enclosing scopes for methods/lambdas inside them
            if id(scope) in self.class_scope_ids and idx != top_idx:
                continue
            if name in scope or "*" in scope:
                return True
        return False

    def _resolve_wildcard_into_scope(self, node: ast.ImportFrom, scope: Set[str]) -> bool:
        """
        Attempts to statically resolve `from X import *` into concrete exported symbols
        so undefined symbol detection remains active for the rest of the file.
        Returns True if concrete symbols were resolved, or False if unresolvable.
        """
        mod_name = node.module or ""
        rel_or_mod = ("." * node.level) + mod_name
        if self.target_path:
            resolved_local = resolve_local_module_path(self.target_path, rel_or_mod, self.project_root)
            if resolved_local and os.path.exists(resolved_local):
                syms = get_exported_symbols_of_file(resolved_local, allow_dynamic=True)
                if syms:
                    scope.update(syms)
                    return True

        if node.level == 0 and mod_name:
            try:
                spec = importlib.util.find_spec(mod_name)
                if spec:
                    if spec.origin and spec.origin.endswith(".py") and os.path.exists(spec.origin):
                        syms = get_exported_symbols_of_file(spec.origin, allow_dynamic=True)
                        if syms:
                            scope.update(syms)
                            return True
                    elif spec.origin in ("built-in", "frozen"):
                        mod_obj = __import__(mod_name)
                        scope.update(dir(mod_obj))
                        return True
            except Exception:
                pass
        return False

    def visit_Module(self, node: ast.Module):
        _collect_bound_names_in_block(node.body, self.module_scope, excluded=set())
        if "*" in self.module_scope:
            self.module_scope.discard("*")
            # Resolve each top-level wildcard import; only fall back to permissive wildcard if unresolvable
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom) and any(a.name == "*" for a in stmt.names):
                    if not self._resolve_wildcard_into_scope(stmt, self.module_scope):
                        self.has_wildcard_import = True
                        self.module_scope.add("*")
        for stmt in node.body:
            self.visit(stmt)

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            name = alias.asname or alias.name.split(".")[0]
            self._bind_name_in_active_scope(name)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        for alias in node.names:
            if alias.name == "*":
                if not self._resolve_wildcard_into_scope(node, self.current_scope()):
                    self.current_scope().add("*")
                    self.has_wildcard_import = True
                continue
            name = alias.asname or alias.name
            self._bind_name_in_active_scope(name)

    def _visit_function_like(self, node: ast.FunctionDef | ast.AsyncFunctionDef):
        self._bind_name_in_active_scope(node.name)
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

        # Pre-collect local bindings inside the function body (excluding global/nonlocal declarations)
        _collect_bound_names_in_block(node.body, func_scope)

        self.scope_stack.append(func_scope)
        self.global_decls_stack.append(set())
        self.nonlocal_decls_stack.append(set())
        for item in node.body:
            self.visit(item)
        self.scope_stack.pop()
        self.global_decls_stack.pop()
        self.nonlocal_decls_stack.pop()

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
        self.global_decls_stack.append(set())
        self.nonlocal_decls_stack.append(set())
        self.visit(node.body)
        self.scope_stack.pop()
        self.global_decls_stack.pop()
        self.nonlocal_decls_stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef):
        self._bind_name_in_active_scope(node.name)
        for dec in node.decorator_list:
            self.visit(dec)
        for base in node.bases:
            self.visit(base)
        for kw in node.keywords:
            self.visit(kw.value)

        class_scope: Set[str] = set()
        _collect_bound_names_in_block(node.body, class_scope)
        self.class_scope_ids.add(id(class_scope))
        self.scope_stack.append(class_scope)
        self.global_decls_stack.append(set())
        self.nonlocal_decls_stack.append(set())
        for item in node.body:
            self.visit(item)
        self.scope_stack.pop()
        self.global_decls_stack.pop()
        self.nonlocal_decls_stack.pop()
        self.class_scope_ids.discard(id(class_scope))

    def visit_Assign(self, node: ast.Assign):
        self.visit(node.value)
        for target in node.targets:
            self._bind_target_in_active_scope(target)
            self._visit_subscript_or_attribute_targets(target)

    def visit_AnnAssign(self, node: ast.AnnAssign):
        if node.value:
            self.visit(node.value)
        if not self.postponed_annotations and node.annotation:
            self.visit(node.annotation)
        if node.value is not None or id(self.current_scope()) in self.class_scope_ids or len(self.scope_stack) == 1:
            self._bind_target_in_active_scope(node.target)
        self._visit_subscript_or_attribute_targets(node.target)

    def visit_AugAssign(self, node: ast.AugAssign):
        self.visit(node.value)
        if isinstance(node.target, ast.Name):
            if not self.is_defined(node.target.id):
                self.undefined_references.append((node.lineno, node.target.id))
            self._bind_name_in_active_scope(node.target.id)
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
        # PEP 572: walrus operator inside comprehensions binds in the enclosing non-comprehension scope
        self._bind_target_in_active_scope(node.target)

    def _visit_comprehension(self, generators: List[ast.comprehension], elt_nodes: List[ast.AST]):
        if generators:
            # Python 3 semantics: outermost iterable is evaluated in enclosing scope
            self.visit(generators[0].iter)
        comp_scope: Set[str] = set()
        self.scope_stack.append(comp_scope)
        self.global_decls_stack.append(set())
        self.nonlocal_decls_stack.append(set())
        for idx, gen in enumerate(generators):
            if idx > 0:
                self.visit(gen.iter)
            _bind_target_names(gen.target, comp_scope)
            for if_clause in gen.ifs:
                self.visit(if_clause)
        for elt in elt_nodes:
            self.visit(elt)
        self.scope_stack.pop()
        self.global_decls_stack.pop()
        self.nonlocal_decls_stack.pop()

    def visit_ListComp(self, node: ast.ListComp):
        self._visit_comprehension(node.generators, [node.elt])

    def visit_SetComp(self, node: ast.SetComp):
        self._visit_comprehension(node.generators, [node.elt])

    def visit_GeneratorExp(self, node: ast.GeneratorExp):
        self._visit_comprehension(node.generators, [node.elt])

    def visit_DictComp(self, node: ast.DictComp):
        self._visit_comprehension(node.generators, [node.key, node.value])

    def visit_For(self, node: ast.For):
        self.visit(node.iter)
        self._bind_target_in_active_scope(node.target)
        for item in node.body:
            self.visit(item)
        for item in node.orelse:
            self.visit(item)

    def visit_AsyncFor(self, node: ast.AsyncFor):
        self.visit(node.iter)
        self._bind_target_in_active_scope(node.target)
        for item in node.body:
            self.visit(item)
        for item in node.orelse:
            self.visit(item)

    def visit_ExceptHandler(self, node: ast.ExceptHandler):
        if node.type:
            self.visit(node.type)
        if node.name:
            self._bind_name_in_active_scope(node.name)
        for item in node.body:
            self.visit(item)

    def visit_With(self, node: ast.With):
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars:
                self._bind_target_in_active_scope(item.optional_vars)
        for b in node.body:
            self.visit(b)

    def visit_AsyncWith(self, node: ast.AsyncWith):
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars:
                self._bind_target_in_active_scope(item.optional_vars)
        for b in node.body:
            self.visit(b)

    def visit_Global(self, node: ast.Global):
        for name in node.names:
            self.global_decls_stack[-1].add(name)

    def visit_Nonlocal(self, node: ast.Nonlocal):
        for name in node.names:
            self.nonlocal_decls_stack[-1].add(name)
            found = any(
                name in self.scope_stack[idx]
                for idx in range(1, len(self.scope_stack) - 1)
                if id(self.scope_stack[idx]) not in self.class_scope_ids
            )
            if not found and not self.has_wildcard_import:
                self.undefined_references.append((node.lineno, name))

    def visit_Name(self, node: ast.Name):
        if isinstance(node.ctx, ast.Load):
            if not self.is_defined(node.id):
                self.undefined_references.append((node.lineno, node.id))
        elif isinstance(node.ctx, ast.Store):
            self._bind_name_in_active_scope(node.id)


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


def get_exported_symbols_of_file(file_path: str, allow_dynamic: bool = False) -> Optional[Set[str]]:
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
    if not allow_dynamic and ("*" in exported or "__getattr__" in exported):
        return None
    exported.discard("*")
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
    project_root: str = ".",
    enable_ruff: bool = False,
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

    # TypeScript / JavaScript preflight routing
    if target_abs.endswith((".ts", ".tsx", ".js", ".jsx")):
        from .ts_ast import (
            parse_ts_js_ast,
            detect_ts_circular_imports,
            resolve_ts_import_path,
            find_tsconfig,
        )
        parsed = parse_ts_js_ast(source, filename=target_abs)
        ts_issues: List[Dict[str, Any]] = []
        tsconfig = find_tsconfig(root_abs)

        # 1. Broken local import resolution
        path_alias_prefixes = []
        if tsconfig:
            paths = tsconfig.get("config", {}).get("compilerOptions", {}).get("paths", {})
            for p in paths.keys():
                path_alias_prefixes.append(p.rstrip("*"))

        for imp in parsed.get("imports", []):
            mod_spec = imp.get("module", "")
            is_local = mod_spec.startswith(".") or any(mod_spec.startswith(pfx) for pfx in path_alias_prefixes if pfx)
            if is_local:
                resolved = resolve_ts_import_path(mod_spec, target_abs, root_abs, tsconfig)
                if not resolved or not os.path.isfile(resolved):
                    ts_issues.append({
                        "severity": "ERROR",
                        "type": "BROKEN_LOCAL_IMPORT",
                        "line": imp.get("line", 1),
                        "module": mod_spec,
                        "message": f"Cannot resolve local TypeScript module '{mod_spec}' from '{rel_path}'.",
                        "recommendation": "Verify file path and extension (.ts, .tsx, .d.ts, .js) or path aliases in tsconfig.json."
                    })

        # 2. Circular import analysis
        cycles = detect_ts_circular_imports(target_abs, root_abs)
        for c in cycles:
            ts_issues.append({
                "severity": c.get("severity", "ERROR"),
                "type": "CIRCULAR_IMPORT" if c.get("type") == "RUNTIME_CYCLE" else "TYPE_ONLY_CYCLE",
                "line": 1,
                "cycle": " -> ".join(c.get("path", [])),
                "message": c.get("message", "Circular import cycle detected."),
                "recommendation": (
                    "Extract shared types or runtime symbols into a leaf module to maintain a DAG."
                    if c.get("type") == "RUNTIME_CYCLE"
                    else "Type-only cycles are erased at compile time, but can be untangled by moving shared interfaces to a types.ts barrel."
                )
            })

        errors = sum(1 for i in ts_issues if i["severity"] == "ERROR")
        warnings = sum(1 for i in ts_issues if i["severity"] == "WARNING")
        health_score = max(0.0, round(100.0 - (errors * 25.0 + warnings * 5.0), 1))
        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

        return {
            "success": errors == 0,
            "file": rel_path,
            "language": "typescript" if target_abs.endswith((".ts", ".tsx")) else "javascript",
            "health_score": health_score,
            "total_issues": len(ts_issues),
            "errors": errors,
            "warnings": warnings,
            "issues": ts_issues,
            "duration_ms": duration_ms
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
    scope_analyzer = ScopeCollector(
        postponed_annotations=postponed_annotations,
        target_path=target_abs,
        project_root=root_abs,
    )
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

    # 4. Circular Import Detection (Direct 2-hop & Multi-hop up to depth 8)
    imported_local_files = get_module_imported_files(target_abs, root_abs)
    seen_cycles = set()
    adj_cache: Dict[str, List[Tuple[int, str, str]]] = {target_abs: imported_local_files}

    def _get_imports(fpath: str) -> List[Tuple[int, str, str]]:
        if fpath not in adj_cache:
            adj_cache[fpath] = get_module_imported_files(fpath, root_abs)
        return adj_cache[fpath]

    for lineno, import_str, imported_abs in imported_local_files:
        imported_abs = os.path.abspath(imported_abs)
        if imported_abs == target_abs:
            issues.append({
                "severity": "ERROR",
                "type": "SELF_IMPORT",
                "line": lineno,
                "message": f"Module '{rel_path}' attempts to import itself.",
                "recommendation": "Remove self-import statement."
            })
            continue

        # BFS from imported_abs looking for a path back to target_abs
        queue: List[Tuple[str, List[str], int]] = [(imported_abs, [target_abs, imported_abs], 0)]
        visited: Set[str] = {imported_abs}
        found_cycle_path: Optional[Tuple[List[str], int]] = None

        while queue and found_cycle_path is None:
            curr_abs, path_so_far, _ = queue.pop(0)
            if len(path_so_far) > 9:
                continue
            for edge_line, _, next_abs in _get_imports(curr_abs):
                next_abs = os.path.abspath(next_abs)
                if next_abs == target_abs:
                    found_cycle_path = (path_so_far + [target_abs], edge_line)
                    break
                if next_abs not in visited:
                    visited.add(next_abs)
                    queue.append((next_abs, path_so_far + [next_abs], edge_line))

        if found_cycle_path is not None:
            cycle_abs_list, back_line = found_cycle_path
            rel_list = [os.path.relpath(p, root_abs) for p in cycle_abs_list]
            cycle_str = " -> ".join(rel_list)
            if cycle_str not in seen_cycles:
                seen_cycles.add(cycle_str)
                imported_rel = rel_list[1]
                last_rel = rel_list[-2]
                issues.append({
                    "severity": "ERROR",
                    "type": "CIRCULAR_IMPORT",
                    "line": lineno,
                    "cycle": cycle_str,
                    "message": (
                        f"Circular import cycle ({cycle_str}): '{rel_path}' imports '{imported_rel}', "
                        f"and '{last_rel}' imports back to '{rel_path}' (at line {back_line})."
                    ),
                    "recommendation": "Extract shared symbols, classes, or state into a leaf module to form a Directed Acyclic Graph (DAG)."
                })

    # 4b. Optional High-Speed Ruff Lint & Import Check
    if enable_ruff:
        ruff_bin = _find_ruff_binary()
        if ruff_bin:
            ruff_diags = _run_ruff_on_source(ruff_bin, source, target_abs)
            for diag in ruff_diags:
                code = diag.get("code") or ""
                msg = diag.get("message") or ""
                loc = diag.get("location") or {}
                row = loc.get("row", 0)
                col = loc.get("column", 0)

                if code == "F401":
                    issues.append({
                        "severity": "WARNING",
                        "type": "UNUSED_IMPORT",
                        "code": code,
                        "line": row,
                        "column": col,
                        "message": f"Ruff [F401]: {msg}",
                        "recommendation": "Remove unused import or organize imports with `arch-sidecar check --fix`."
                    })
                elif code == "F821":
                    if not any(i.get("line") == row and i.get("type") == "UNDEFINED_SYMBOL" for i in issues):
                        issues.append({
                            "severity": "ERROR",
                            "type": "UNDEFINED_SYMBOL",
                            "code": code,
                            "line": row,
                            "column": col,
                            "message": f"Ruff [F821]: {msg}",
                            "recommendation": "Import or define this symbol before use."
                        })
                elif code.startswith("E") or code.startswith("F") or code.startswith("TID"):
                    is_err = code.startswith("E9") or code.startswith("F8")
                    issues.append({
                        "severity": "ERROR" if is_err else "WARNING",
                        "type": "LINT_ISSUE",
                        "code": code,
                        "line": row,
                        "column": col,
                        "message": f"Ruff [{code}]: {msg}",
                        "recommendation": "Resolve lint rule or auto-fix via ruff."
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


def _find_ruff_binary() -> Optional[str]:
    """Locates the ruff binary on PATH or in ~/.local/bin/ruff."""
    return find_ruff_binary()


def _run_ruff_on_source(ruff_bin: str, source_text: str, filename: str) -> List[Dict[str, Any]]:
    """Runs ruff check via stdin on in-memory source text and returns parsed JSON diagnostics."""
    try:
        res = subprocess.run(
            [ruff_bin, "check", "--output-format", "json", "--stdin-filename", filename, "-"],
            input=source_text,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if not res.stdout.strip():
            return []
        data = json.loads(res.stdout)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def run_differential_ruff(
    pre_sources: Dict[str, str],
    post_sources: Dict[str, str],
) -> Dict[str, Any]:
    """
    Differential Ruff Gate:
    Runs `ruff check` on pre-edit and post-edit source strings for touched files,
    suppressing pre-existing legacy lint warnings and reporting ONLY net-new issues
    introduced by the edit.
    """
    t0 = time.perf_counter()
    ruff_bin = _find_ruff_binary()
    if not ruff_bin:
        return {
            "available": False,
            "passed": True,
            "new_issues_count": 0,
            "suppressed_legacy_count": 0,
            "new_issues": [],
            "duration_ms": 0.0,
        }

    pre_fingerprints: Set[Tuple[str, str]] = set()
    for fname, src in pre_sources.items():
        if src:
            for diag in _run_ruff_on_source(ruff_bin, src, fname):
                code = diag.get("code") or ""
                msg = diag.get("message") or ""
                pre_fingerprints.add((code, msg))

    new_issues: List[Dict[str, Any]] = []
    suppressed_count = 0

    for fname, src in post_sources.items():
        for diag in _run_ruff_on_source(ruff_bin, src, fname):
            code = diag.get("code") or ""
            msg = diag.get("message") or ""
            if (code, msg) in pre_fingerprints:
                suppressed_count += 1
            else:
                loc = diag.get("location") or {}
                new_issues.append({
                    "file": fname,
                    "code": code,
                    "line": loc.get("row", 0),
                    "column": loc.get("column", 0),
                    "message": msg,
                })

    duration_ms = round((time.perf_counter() - t0) * 1000, 2)
    return {
        "available": True,
        "passed": len(new_issues) == 0,
        "new_issues_count": len(new_issues),
        "suppressed_legacy_count": suppressed_count,
        "new_issues": new_issues,
        "duration_ms": duration_ms,
    }

