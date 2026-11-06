import os
import ast
import sys
import json
import logging
import subprocess
import builtins
import difflib
import importlib.util
import time
from typing import Dict, List, Set, Any, Optional, Tuple

from .utils import find_ruff_binary, run_ruff_autofix

logger = logging.getLogger(__name__)

BUILTIN_NAMES = set(dir(builtins)) | {
    "__name__", "__doc__", "__file__", "__path__", "__package__",
    "__cached__", "__spec__", "__annotations__", "__dict__",
    "__all__", "__loader__", "__builtins__", "self", "cls"
}


def _extract_match_name(node: ast.AST) -> Optional[str]:
    match_as = getattr(ast, "MatchAs", None)
    if match_as and isinstance(node, match_as):
        return node.name
    match_star = getattr(ast, "MatchStar", None)
    if match_star and isinstance(node, match_star):
        return node.name
    match_map = getattr(ast, "MatchMapping", None)
    if match_map and isinstance(node, match_map):
        return node.rest
    return None


def _bind_match_pattern(pattern: ast.AST, bound: Set[str], excluded: Optional[Set[str]] = None) -> None:
    excl = excluded or set()
    for sub in ast.walk(pattern):
        name = _extract_match_name(sub)
        if name and name not in excl:
            bound.add(name)


def _get_nested_stmt_blocks(stmt: ast.stmt) -> List[List[ast.stmt]]:
    if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While, ast.If)):
        return [stmt.body, stmt.orelse]
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        return [stmt.body]
    if isinstance(stmt, ast.Try) or (hasattr(ast, "TryStar") and isinstance(stmt, getattr(ast, "TryStar"))):
        return [stmt.body, stmt.orelse, stmt.finalbody] + [h.body for h in stmt.handlers]
    if hasattr(ast, "Match") and isinstance(stmt, getattr(ast, "Match")):
        return [case.body for case in stmt.cases]
    return []


def _collect_declared_globals_and_nonlocals(stmts: List[ast.stmt], out: Set[str]) -> None:
    """Collects names declared via `global` or `nonlocal` within the current lexical block."""
    for stmt in stmts:
        if isinstance(stmt, (ast.Global, ast.Nonlocal)):
            for name in stmt.names:
                out.add(name)
            continue
        for block in _get_nested_stmt_blocks(stmt):
            _collect_declared_globals_and_nonlocals(block, out)


def _bind_import_from(stmt: ast.ImportFrom, bound: Set[str], excluded: Set[str]) -> None:
    for alias in stmt.names:
        if alias.name == "*":
            bound.add("*")
            continue
        name = alias.asname or alias.name
        if name not in excluded:
            bound.add(name)


def _bind_standard_import(stmt: ast.Import, bound: Set[str], excluded: Set[str]) -> None:
    for alias in stmt.names:
        name = alias.asname or alias.name.split(".")[0]
        if name not in excluded:
            bound.add(name)


def _bind_import(stmt: ast.stmt, bound: Set[str], excluded: Set[str]) -> bool:
    if isinstance(stmt, ast.Import):
        _bind_standard_import(stmt, bound, excluded)
        return True
    if isinstance(stmt, ast.ImportFrom):
        _bind_import_from(stmt, bound, excluded)
        return True
    return False


def _bind_type_alias(stmt: ast.stmt, bound: Set[str], excluded: Set[str]) -> bool:
    type_alias = getattr(ast, "TypeAlias", None)
    if type_alias and isinstance(stmt, type_alias):
        if isinstance(stmt.name, ast.Name) and stmt.name.id not in excluded:
            bound.add(stmt.name.id)
        return True
    return False


def _bind_definition(stmt: ast.stmt, bound: Set[str], excluded: Set[str]) -> bool:
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        if stmt.name not in excluded:
            bound.add(stmt.name)
        return True
    return _bind_type_alias(stmt, bound, excluded)


def _bind_target_names(target: ast.AST, bound: Set[str], excluded: Optional[Set[str]] = None) -> None:
    if isinstance(target, ast.Name):
        if excluded is None or target.id not in excluded:
            bound.add(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            _bind_target_names(elt, bound, excluded)
    elif isinstance(target, ast.Starred):
        _bind_target_names(target.value, bound, excluded)


def _bind_assignment(stmt: ast.stmt, bound: Set[str], excluded: Set[str]) -> bool:
    if isinstance(stmt, ast.Assign):
        for t in stmt.targets:
            _bind_target_names(t, bound, excluded)
        return True
    if isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        _bind_target_names(stmt.target, bound, excluded)
        return True
    return False


def _bind_for_target(stmt: ast.stmt, bound: Set[str], excluded: Set[str]) -> None:
    if isinstance(stmt, (ast.For, ast.AsyncFor)):
        _bind_target_names(stmt.target, bound, excluded)


def _bind_with_items(stmt: ast.stmt, bound: Set[str], excluded: Set[str]) -> None:
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        for item in stmt.items:
            if item.optional_vars:
                _bind_target_names(item.optional_vars, bound, excluded)


def _bind_try_handlers(stmt: ast.stmt, bound: Set[str], excluded: Set[str]) -> None:
    is_try = isinstance(stmt, ast.Try) or (hasattr(ast, "TryStar") and isinstance(stmt, getattr(ast, "TryStar")))
    if not is_try:
        return
    for handler in stmt.handlers:
        if handler.name and handler.name not in excluded:
            bound.add(handler.name)


def _bind_match_cases(stmt: ast.stmt, bound: Set[str], excluded: Set[str]) -> None:
    if hasattr(ast, "Match") and isinstance(stmt, getattr(ast, "Match")):
        for case in stmt.cases:
            _bind_match_pattern(case.pattern, bound, excluded)


def _bind_statement(stmt: ast.stmt, bound: Set[str], excluded: Set[str]) -> None:
    if _bind_definition(stmt, bound, excluded):
        return
    if _bind_import(stmt, bound, excluded):
        return
    if _bind_assignment(stmt, bound, excluded):
        return
    _bind_for_target(stmt, bound, excluded)
    _bind_with_items(stmt, bound, excluded)
    _bind_try_handlers(stmt, bound, excluded)
    _bind_match_cases(stmt, bound, excluded)
    for block in _get_nested_stmt_blocks(stmt):
        _collect_bound_names_in_block(block, bound, excluded)


def _collect_bound_names_in_block(
    stmts: List[ast.stmt],
    bound: Set[str],
    excluded: Optional[Set[str]] = None
) -> None:
    """
    Pass 1 of two-pass lexical scope binding:
    Pre-scans a block of statements (e.g. a module body or function body) and records
    all names bound anywhere in that scope before descending into nested scopes.
    """
    excl = excluded
    if excl is None:
        excl = set()
        _collect_declared_globals_and_nonlocals(stmts, excl)

    for stmt in stmts:
        _bind_statement(stmt, bound, excl)


class _ScopeBase(ast.NodeVisitor):
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

    def _enter_scope(self, scope: Set[str]) -> None:
        self.scope_stack.append(scope)
        self.global_decls_stack.append(set())
        self.nonlocal_decls_stack.append(set())

    def _exit_scope(self) -> None:
        self.scope_stack.pop()
        self.global_decls_stack.pop()
        self.nonlocal_decls_stack.pop()

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

    def _is_name_in_scope_stack(self, name: str) -> bool:
        top_idx = len(self.scope_stack) - 1
        for idx in range(top_idx, -1, -1):
            scope = self.scope_stack[idx]
            if id(scope) in self.class_scope_ids and idx != top_idx:
                continue
            if name in scope or "*" in scope:
                return True
        return False

    def is_defined(self, name: str) -> bool:
        if self.has_wildcard_import or name in BUILTIN_NAMES:
            return True
        if name in self.global_decls_stack[-1]:
            return name in self.module_scope or "*" in self.module_scope
        return self._is_name_in_scope_stack(name)


def _resolve_local_wildcard(
    target_path: Optional[str], rel_or_mod: str, project_root: str
) -> Optional[Set[str]]:
    if not target_path:
        return None
    resolved_local = resolve_local_module_path(target_path, rel_or_mod, project_root)
    if resolved_local and os.path.exists(resolved_local):
        return get_exported_symbols_of_file(resolved_local, allow_dynamic=True)
    return None


def _resolve_spec_symbols(spec: Any) -> Optional[Set[str]]:
    origin = getattr(spec, "origin", None)
    if origin and origin.endswith(".py") and os.path.exists(origin):
        return get_exported_symbols_of_file(origin, allow_dynamic=True)
    return None


def _resolve_stdlib_wildcard(mod_name: str) -> Optional[Set[str]]:
    try:
        spec = importlib.util.find_spec(mod_name)
        if not spec:
            return None
        syms = _resolve_spec_symbols(spec)
        if syms is not None:
            return syms
        if getattr(spec, "origin", None) in ("built-in", "frozen"):
            return set(dir(__import__(mod_name)))
    except Exception as exc:
        logger.debug("Failed to find spec for %s: %s", mod_name, exc)
    return None


def _extract_arg_names(args: ast.arguments) -> Set[str]:
    names: Set[str] = set()
    for arg in getattr(args, "posonlyargs", []):
        names.add(arg.arg)
    for arg in args.args:
        names.add(arg.arg)
    for arg in args.kwonlyargs:
        names.add(arg.arg)
    vararg = args.vararg
    if vararg:
        names.add(vararg.arg)
    kwarg = args.kwarg
    if kwarg:
        names.add(kwarg.arg)
    return names


def _visit_vararg_annotations(visitor: Any, args_obj: ast.arguments) -> None:
    vararg = args_obj.vararg
    if vararg and vararg.annotation:
        visitor.visit(vararg.annotation)
    kwarg = args_obj.kwarg
    if kwarg and kwarg.annotation:
        visitor.visit(kwarg.annotation)


class _StatementScopeVisitor(_ScopeBase):
    def _resolve_wildcard_into_scope(self, node: ast.ImportFrom, scope: Set[str]) -> bool:
        mod_name = node.module or ""
        rel_or_mod = ("." * node.level) + mod_name
        local_syms = _resolve_local_wildcard(self.target_path, rel_or_mod, self.project_root)
        if local_syms:
            for s in local_syms:
                scope.add(s)
            return True

        if node.level == 0 and mod_name:
            stdlib_syms = _resolve_stdlib_wildcard(mod_name)
            if stdlib_syms:
                for s in stdlib_syms:
                    scope.add(s)
                return True
        return False

    def _apply_wildcard_import(self, stmt: ast.ImportFrom) -> None:
        if not any(a.name == "*" for a in stmt.names):
            return
        if not self._resolve_wildcard_into_scope(stmt, self.module_scope):
            self.has_wildcard_import = True
            self.module_scope.add("*")

    def visit_Module(self, node: ast.Module) -> None:
        _collect_bound_names_in_block(node.body, self.module_scope, excluded=set())
        if "*" in self.module_scope:
            self.module_scope.discard("*")
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom):
                    self._apply_wildcard_import(stmt)
        for stmt in node.body:
            self.visit(stmt)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            name = alias.asname or alias.name.split(".")[0]
            self._bind_name_in_active_scope(name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name == "*":
                if not self._resolve_wildcard_into_scope(node, self.current_scope()):
                    self.current_scope().add("*")
                    self.has_wildcard_import = True
                continue
            name = alias.asname or alias.name
            self._bind_name_in_active_scope(name)

    def _visit_arg_defaults(self, args: ast.arguments) -> None:
        for default in args.defaults:
            self.visit(default)
        for kw_def in args.kw_defaults:
            if kw_def is not None:
                self.visit(kw_def)

    def _visit_function_annotations(self, node: Any) -> None:
        if self.postponed_annotations:
            return
        if node.returns:
            self.visit(node.returns)
        args_obj = node.args
        all_args = getattr(args_obj, "posonlyargs", []) + args_obj.args + args_obj.kwonlyargs
        for a in all_args:
            if a.annotation:
                self.visit(a.annotation)
        _visit_vararg_annotations(self, args_obj)

    def _visit_function_like(self, node: Any) -> None:
        self._bind_name_in_active_scope(node.name)
        for dec in node.decorator_list:
            self.visit(dec)
        self._visit_arg_defaults(node.args)
        self._visit_function_annotations(node)

        func_scope = _extract_arg_names(node.args)
        _collect_bound_names_in_block(node.body, func_scope)

        self._enter_scope(func_scope)
        for item in node.body:
            self.visit(item)
        self._exit_scope()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function_like(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function_like(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
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
        self._enter_scope(class_scope)
        for item in node.body:
            self.visit(item)
        self._exit_scope()
        self.class_scope_ids.discard(id(class_scope))

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        self._bind_target_in_active_scope(node.target)
        for item in node.body:
            self.visit(item)
        for item in node.orelse:
            self.visit(item)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.visit(node.iter)
        self._bind_target_in_active_scope(node.target)
        for item in node.body:
            self.visit(item)
        for item in node.orelse:
            self.visit(item)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars:
                self._bind_target_in_active_scope(item.optional_vars)
        for b in node.body:
            self.visit(b)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars:
                self._bind_target_in_active_scope(item.optional_vars)
        for b in node.body:
            self.visit(b)


def _process_comprehension(
    visitor: Any, generators: List[ast.comprehension], elt_nodes: List[ast.AST]
) -> None:
    if generators:
        visitor.visit(generators[0].iter)
    comp_scope: Set[str] = set()
    visitor._enter_scope(comp_scope)
    for idx, gen in enumerate(generators):
        if idx > 0:
            visitor.visit(gen.iter)
        _bind_target_names(gen.target, comp_scope)
        for if_clause in gen.ifs:
            visitor.visit(if_clause)
    for elt in elt_nodes:
        visitor.visit(elt)
    visitor._exit_scope()


class ScopeCollector(_StatementScopeVisitor):
    """
    Two-pass lexical scope analyzer that detects undefined/hallucinated identifiers
    while accurately supporting forward references, comprehensions, lambdas,
    walrus expressions (:=), type annotations, class body isolation, global/nonlocal
    binding semantics, and nested closures.
    """
    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._visit_arg_defaults(node.args)
        lam_scope = _extract_arg_names(node.args)
        self._enter_scope(lam_scope)
        self.visit(node.body)
        self._exit_scope()

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self._bind_target_in_active_scope(target)
            self._visit_subscript_or_attribute_targets(target)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value:
            self.visit(node.value)
        if not self.postponed_annotations and node.annotation:
            self.visit(node.annotation)
        is_class_or_mod = id(self.current_scope()) in self.class_scope_ids or len(self.scope_stack) == 1
        if node.value is not None or is_class_or_mod:
            self._bind_target_in_active_scope(node.target)
        self._visit_subscript_or_attribute_targets(node.target)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.value)
        if isinstance(node.target, ast.Name):
            if not self.is_defined(node.target.id):
                self.undefined_references.append((node.lineno, node.target.id))
            self._bind_name_in_active_scope(node.target.id)
        else:
            self._visit_subscript_or_attribute_targets(node.target)

    def _visit_subscript_or_attribute_targets(self, target: ast.AST) -> None:
        if isinstance(target, ast.Attribute):
            self.visit(target.value)
        elif isinstance(target, ast.Subscript):
            self.visit(target.value)
            self.visit(target.slice)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._visit_subscript_or_attribute_targets(elt)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        self._bind_target_in_active_scope(node.target)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        _process_comprehension(self, node.generators, [node.elt])

    def visit_SetComp(self, node: ast.SetComp) -> None:
        _process_comprehension(self, node.generators, [node.elt])

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        _process_comprehension(self, node.generators, [node.elt])

    def visit_DictComp(self, node: ast.DictComp) -> None:
        _process_comprehension(self, node.generators, [node.key, node.value])

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type:
            self.visit(node.type)
        if node.name:
            self._bind_name_in_active_scope(node.name)
        for item in node.body:
            self.visit(item)

    def visit_Global(self, node: ast.Global) -> None:
        for name in node.names:
            self.global_decls_stack[-1].add(name)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        for name in node.names:
            self.nonlocal_decls_stack[-1].add(name)
            found = any(
                name in self.scope_stack[idx]
                for idx in range(1, len(self.scope_stack) - 1)
                if id(self.scope_stack[idx]) not in self.class_scope_ids
            )
            if not found and not self.has_wildcard_import:
                self.undefined_references.append((node.lineno, name))

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            if not self.is_defined(node.id):
                self.undefined_references.append((node.lineno, node.id))
        elif isinstance(node.ctx, ast.Store):
            self._bind_name_in_active_scope(node.id)


def _check_module_candidates(base_dir: str, mod_rel: str) -> Optional[str]:
    rel = mod_rel.replace(".", os.sep)
    file_cand = os.path.join(base_dir, rel + ".py")
    if os.path.isfile(file_cand):
        return file_cand
    pkg_cand = os.path.join(base_dir, rel, "__init__.py")
    if os.path.isfile(pkg_cand):
        return pkg_cand
    return None


def _resolve_relative_import_path(target_dir: str, import_mod: str) -> Optional[str]:
    dots = len(import_mod) - len(import_mod.lstrip("."))
    sub_mod = import_mod.lstrip(".")
    base_dir = target_dir
    for _ in range(dots - 1):
        base_dir = os.path.dirname(base_dir)

    if not sub_mod:
        pkg_init = os.path.join(base_dir, "__init__.py")
        return pkg_init if os.path.isfile(pkg_init) else None

    return _check_module_candidates(base_dir, sub_mod)


def _is_stdlib_module(import_mod: str) -> bool:
    top_mod = import_mod.split(".")[0]
    stdlib_names = getattr(sys, "stdlib_module_names", None)
    return bool(stdlib_names and top_mod in stdlib_names)


def resolve_local_module_path(target_path: str, import_mod: str, project_root: str) -> Optional[str]:
    """Resolves an imported module name or relative path to a local project file if it exists."""
    target_dir = os.path.dirname(os.path.abspath(target_path))
    root_abs = os.path.abspath(project_root)

    if import_mod.startswith("."):
        return _resolve_relative_import_path(target_dir, import_mod)

    if _is_stdlib_module(import_mod):
        return None

    cand = _check_module_candidates(target_dir, import_mod)
    if cand:
        return cand

    cand = _check_module_candidates(root_abs, import_mod)
    if cand:
        return cand

    for src_dir in ("src", "lib"):
        cand = _check_module_candidates(os.path.join(root_abs, src_dir), import_mod)
        if cand:
            return cand

    return None


def _add_package_submodules(pkg_dir: str, exported: Set[str]) -> None:
    try:
        entries = os.listdir(pkg_dir)
    except OSError as exc:
        logger.debug("Failed to list directory %s: %s", pkg_dir, exc)
        return

    for entry in entries:
        if entry.endswith(".py") and entry != "__init__.py":
            exported.add(entry[:-3])
        elif os.path.isdir(os.path.join(pkg_dir, entry)):
            exported.add(entry)


def get_exported_symbols_of_file(file_path: str, allow_dynamic: bool = False) -> Optional[Set[str]]:
    """
    Returns the set of top-level defined/imported names in `file_path`,
    or None if the file cannot be parsed or uses wildcard `from ... import *`.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=file_path)
    except Exception as exc:
        logger.debug("Failed to parse file %s: %s", file_path, exc)
        return None

    exported: Set[str] = set()
    _collect_bound_names_in_block(tree.body, exported)
    if not allow_dynamic and ("*" in exported or "__getattr__" in exported):
        return None
    exported.discard("*")
    if os.path.basename(file_path) == "__init__.py":
        _add_package_submodules(os.path.dirname(file_path), exported)
    return exported


def _collect_ast_import_items(
    node: ast.Import, file_path: str, project_root: str
) -> List[Tuple[int, str, str]]:
    items: List[Tuple[int, str, str]] = []
    for alias in node.names:
        resolved = resolve_local_module_path(file_path, alias.name, project_root)
        if resolved:
            items.append((node.lineno, alias.name, resolved))
    return items


def _resolve_bare_import_alias(
    node: ast.ImportFrom, alias_name: str, base_dir: str, file_path: str, project_root: str
) -> Optional[Tuple[int, str, str]]:
    cand_sub = os.path.join(base_dir, alias_name + ".py")
    if os.path.isfile(cand_sub):
        return (node.lineno, "." * node.level + alias_name, os.path.abspath(cand_sub))
    mod_name = "." * node.level
    resolved = resolve_local_module_path(file_path, mod_name, project_root)
    if resolved:
        return (node.lineno, mod_name, resolved)
    return None


def _resolve_relative_bare_import_from(
    node: ast.ImportFrom, file_path: str, project_root: str
) -> List[Tuple[int, str, str]]:
    base_dir = os.path.dirname(os.path.abspath(file_path))
    for _ in range(node.level - 1):
        base_dir = os.path.dirname(base_dir)

    results: List[Tuple[int, str, str]] = []
    for alias in node.names:
        item = _resolve_bare_import_alias(node, alias.name, base_dir, file_path, project_root)
        if item:
            results.append(item)
    return results


def _resolve_standard_import_from(
    node: ast.ImportFrom, file_path: str, project_root: str
) -> List[Tuple[int, str, str]]:
    mod_name = node.module or ""
    if node.level > 0:
        mod_name = ("." * node.level) + mod_name
    resolved = resolve_local_module_path(file_path, mod_name, project_root)
    if resolved:
        return [(node.lineno, mod_name, resolved)]
    return []


def _collect_ast_import_from_items(
    node: ast.ImportFrom, file_path: str, project_root: str
) -> List[Tuple[int, str, str]]:
    if node.level > 0 and not node.module:
        return _resolve_relative_bare_import_from(node, file_path, project_root)
    return _resolve_standard_import_from(node, file_path, project_root)


def get_module_imported_files(file_path: str, project_root: str) -> List[Tuple[int, str, str]]:
    """Returns list of (line, import_str, resolved_file_path) for local project files imported by file_path."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=file_path)
    except Exception as exc:
        logger.debug("Failed to read %s: %s", file_path, exc)
        return []

    imported: List[Tuple[int, str, str]] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.extend(_collect_ast_import_items(node, file_path, project_root))
        elif isinstance(node, ast.ImportFrom):
            imported.extend(_collect_ast_import_from_items(node, file_path, project_root))
    return imported


def verify_ast_syntax(source: str, filename: str) -> Tuple[Optional[ast.AST], Optional[Dict[str, Any]]]:
    try:
        tree = ast.parse(source, filename=filename)
        return tree, None
    except SyntaxError as e:
        return None, {
            "severity": "ERROR",
            "type": "SYNTAX_ERROR",
            "line": e.lineno or 0,
            "column": e.offset or 0,
            "message": f"SyntaxError at line {e.lineno}: {e.msg}",
        }


def verify_lexical_scope(tree: ast.AST, target_abs: str, root_abs: str) -> List[Dict[str, Any]]:
    postponed = any(
        isinstance(n, ast.ImportFrom)
        and n.module == "__future__"
        and any(a.name == "annotations" for a in n.names)
        for n in tree.body
    )
    analyzer = ScopeCollector(
        postponed_annotations=postponed,
        target_path=target_abs,
        project_root=root_abs,
    )
    analyzer.visit(tree)

    issues: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for lineno, sym in analyzer.undefined_references:
        if sym not in seen:
            seen.add(sym)
            issues.append({
                "severity": "ERROR",
                "type": "UNDEFINED_SYMBOL",
                "line": lineno,
                "symbol": sym,
                "message": f"Undefined symbol '{sym}' loaded at line {lineno}. Missing import, misnamed symbol, or hallucinated API.",
                "recommendation": f"Import '{sym}' from its defining module or define it before use."
            })
    return issues


def _collect_missing_symbol_issues(
    node: ast.ImportFrom, exported: Set[str], resolved_rel: str
) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    for alias in node.names:
        if alias.name == "*" or alias.name in exported:
            continue
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
    return issues


def _check_import_from_symbol_issues(
    node: ast.ImportFrom, target_abs: str, root_abs: str
) -> List[Dict[str, Any]]:
    if node.module == "__future__":
        return []
    mod_name = ("." * node.level) + (node.module or "") if node.level > 0 else (node.module or "")
    resolved = resolve_local_module_path(target_abs, mod_name, root_abs)
    if not resolved or os.path.abspath(resolved) == target_abs:
        return []
    exported = get_exported_symbols_of_file(resolved)
    if exported is None:
        return []
    resolved_rel = os.path.relpath(resolved, root_abs)
    return _collect_missing_symbol_issues(node, exported, resolved_rel)


def verify_local_imports(tree: ast.AST, target_abs: str, root_abs: str) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            issues.extend(_check_import_from_symbol_issues(node, target_abs, root_abs))
    return issues


def _create_cycle_issue(
    cycle_path: Tuple[List[str], int], lineno: int, rel_path: str, root_abs: str
) -> Tuple[str, Dict[str, Any]]:
    cycle_abs_list, back_line = cycle_path
    rel_list = [os.path.relpath(p, root_abs) for p in cycle_abs_list]
    cycle_str = " -> ".join(rel_list)
    imported_rel = rel_list[1]
    last_rel = rel_list[-2]
    issue = {
        "severity": "ERROR",
        "type": "CIRCULAR_IMPORT",
        "line": lineno,
        "cycle": cycle_str,
        "message": (
            f"Circular import cycle ({cycle_str}): '{rel_path}' imports '{imported_rel}', "
            f"and '{last_rel}' imports back to '{rel_path}' (at line {back_line})."
        ),
        "recommendation": "Extract shared symbols, classes, or state into a leaf module to form a Directed Acyclic Graph (DAG)."
    }
    return cycle_str, issue


class _ImportGraph:
    def __init__(self, root_abs: str, initial_cache: Optional[Dict[str, List[Tuple[int, str, str]]]] = None):
        self.root_abs = root_abs
        self.cache: Dict[str, List[Tuple[int, str, str]]] = dict(initial_cache) if initial_cache else {}

    def get_imports(self, file_path: str) -> List[Tuple[int, str, str]]:
        if file_path not in self.cache:
            self.cache[file_path] = get_module_imported_files(file_path, self.root_abs)
        return self.cache[file_path]


def _bfs_cycle_to_target(
    start_abs: str,
    target_abs: str,
    graph: _ImportGraph,
) -> Optional[Tuple[List[str], int]]:
    queue: List[Tuple[str, List[str]]] = [(start_abs, [target_abs, start_abs])]
    visited: Set[str] = {start_abs}

    while queue:
        curr_abs, path_so_far = queue.pop(0)
        if len(path_so_far) > 9:
            continue
        for edge_line, _, next_raw in graph.get_imports(curr_abs):
            next_abs = os.path.abspath(next_raw)
            if next_abs == target_abs:
                return (path_so_far + [target_abs], edge_line)
            if next_abs not in visited:
                visited.add(next_abs)
                queue.append((next_abs, path_so_far + [next_abs]))
    return None


def detect_circular_imports(target_abs: str, root_abs: str, rel_path: str) -> List[Dict[str, Any]]:
    imported_local_files = get_module_imported_files(target_abs, root_abs)
    graph = _ImportGraph(root_abs, {target_abs: imported_local_files})
    seen_cycles: Set[str] = set()
    issues: List[Dict[str, Any]] = []

    for lineno, _, imported_raw in imported_local_files:
        imported_abs = os.path.abspath(imported_raw)
        if imported_abs == target_abs:
            issues.append({
                "severity": "ERROR",
                "type": "SELF_IMPORT",
                "line": lineno,
                "message": f"Module '{rel_path}' attempts to import itself.",
                "recommendation": "Remove self-import statement."
            })
            continue

        cycle_path = _bfs_cycle_to_target(imported_abs, target_abs, graph)
        if not cycle_path:
            continue
        cycle_str, issue = _create_cycle_issue(cycle_path, lineno, rel_path, root_abs)
        if cycle_str not in seen_cycles:
            seen_cycles.add(cycle_str)
            issues.append(issue)

    return issues


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
    except Exception as exc:
        logger.debug("Ruff run failed on %s: %s", filename, exc)
        return []


def _convert_f821(diag: Dict[str, Any], existing_issues: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    loc = diag.get("location") or {}
    row = loc.get("row", 0)
    already = any(i.get("line") == row and i.get("type") == "UNDEFINED_SYMBOL" for i in existing_issues)
    if already:
        return None
    code = diag.get("code") or ""
    msg = diag.get("message") or ""
    return {
        "severity": "ERROR",
        "type": "UNDEFINED_SYMBOL",
        "code": code,
        "line": row,
        "column": loc.get("column", 0),
        "message": f"Ruff [F821]: {msg}",
        "recommendation": "Import or define this symbol before use."
    }


def _convert_generic_lint(code: str, msg: str, loc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not code.startswith(("E", "F", "TID")):
        return None
    is_err = code.startswith(("E9", "F8"))
    return {
        "severity": "ERROR" if is_err else "WARNING",
        "type": "LINT_ISSUE",
        "code": code,
        "line": loc.get("row", 0),
        "column": loc.get("column", 0),
        "message": f"Ruff [{code}]: {msg}",
        "recommendation": "Resolve lint rule or auto-fix via ruff."
    }


def _convert_ruff_diagnostic(
    diag: Dict[str, Any], existing_issues: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    code = diag.get("code") or ""
    msg = diag.get("message") or ""
    loc = diag.get("location") or {}

    if code == "F401":
        return {
            "severity": "WARNING",
            "type": "UNUSED_IMPORT",
            "code": code,
            "line": loc.get("row", 0),
            "column": loc.get("column", 0),
            "message": f"Ruff [F401]: {msg}",
            "recommendation": "Remove unused import or organize imports with `arch-sidecar check --fix`."
        }
    if code == "F821":
        return _convert_f821(diag, existing_issues)
    return _convert_generic_lint(code, msg, loc)


def _run_optional_ruff_check(
    source: str, target_abs: str, existing_issues: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    ruff_bin = _find_ruff_binary()
    if not ruff_bin:
        return []
    ruff_diags = _run_ruff_on_source(ruff_bin, source, target_abs)
    issues: List[Dict[str, Any]] = []
    for diag in ruff_diags:
        issue = _convert_ruff_diagnostic(diag, existing_issues)
        if issue:
            issues.append(issue)
    return issues


def _empty_differential_ruff_result() -> Dict[str, Any]:
    return {
        "available": False,
        "passed": True,
        "new_issues_count": 0,
        "suppressed_legacy_count": 0,
        "new_issues": [],
        "duration_ms": 0.0,
    }


def _collect_pre_fingerprints(ruff_bin: str, pre_sources: Dict[str, str]) -> Set[Tuple[str, str]]:
    fingerprints: Set[Tuple[str, str]] = set()
    for fname, src in pre_sources.items():
        if not src:
            continue
        for diag in _run_ruff_on_source(ruff_bin, src, fname):
            code = diag.get("code") or ""
            msg = diag.get("message") or ""
            fingerprints.add((code, msg))
    return fingerprints


def _collect_post_diff_issues(
    ruff_bin: str, post_sources: Dict[str, str], pre_fps: Set[Tuple[str, str]]
) -> Tuple[List[Dict[str, Any]], int]:
    new_issues: List[Dict[str, Any]] = []
    suppressed = 0
    for fname, src in post_sources.items():
        for diag in _run_ruff_on_source(ruff_bin, src, fname):
            code = diag.get("code") or ""
            msg = diag.get("message") or ""
            if (code, msg) in pre_fps:
                suppressed += 1
                continue
            loc = diag.get("location") or {}
            new_issues.append({
                "file": fname,
                "code": code,
                "line": loc.get("row", 0),
                "column": loc.get("column", 0),
                "message": msg,
            })
    return new_issues, suppressed


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
        return _empty_differential_ruff_result()

    pre_fingerprints = _collect_pre_fingerprints(ruff_bin, pre_sources)
    new_issues, suppressed_count = _collect_post_diff_issues(ruff_bin, post_sources, pre_fingerprints)
    duration_ms = round((time.perf_counter() - t0) * 1000, 2)
    return {
        "available": True,
        "passed": len(new_issues) == 0,
        "new_issues_count": len(new_issues),
        "suppressed_legacy_count": suppressed_count,
        "new_issues": new_issues,
        "duration_ms": duration_ms,
    }


def _format_polyglot_response(
    issues: List[Dict[str, Any]], rel_path: str, lang: str, start_time: float
) -> Dict[str, Any]:
    errors = sum(1 for i in issues if i["severity"] == "ERROR")
    warnings = sum(1 for i in issues if i["severity"] == "WARNING")
    health_score = max(0.0, round(100.0 - (errors * 25.0 + warnings * 5.0), 1))
    duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
    return {
        "success": errors == 0,
        "file": rel_path,
        "language": lang,
        "health_score": health_score,
        "total_issues": len(issues),
        "errors": errors,
        "warnings": warnings,
        "issues": issues,
        "duration_ms": duration_ms
    }


def _get_tsconfig_prefixes(tsconfig: Optional[Dict[str, Any]]) -> List[str]:
    if not tsconfig:
        return []
    paths = tsconfig.get("config", {}).get("compilerOptions", {}).get("paths", {})
    return [p.rstrip("*") for p in paths.keys()]


def _verify_ts_import(
    imp: Dict[str, Any], target_abs: str, root_abs: str, tsconfig: Any, prefixes: List[str]
) -> Optional[Dict[str, Any]]:
    from .ts_ast import resolve_ts_import_path
    mod_spec = imp.get("module", "")
    is_local = mod_spec.startswith(".") or any(mod_spec.startswith(pfx) for pfx in prefixes if pfx)
    if not is_local:
        return None
    resolved = resolve_ts_import_path(mod_spec, target_abs, root_abs, tsconfig)
    if resolved and os.path.isfile(resolved):
        return None
    rel_path = os.path.relpath(target_abs, root_abs)
    return {
        "severity": "ERROR",
        "type": "BROKEN_LOCAL_IMPORT",
        "line": imp.get("line", 1),
        "module": mod_spec,
        "message": f"Cannot resolve local TypeScript module '{mod_spec}' from '{rel_path}'.",
        "recommendation": "Verify file path and extension (.ts, .tsx, .d.ts, .js) or path aliases in tsconfig.json."
    }


def _convert_ts_cycle(c: Dict[str, Any]) -> Dict[str, Any]:
    is_runtime = c.get("type") == "RUNTIME_CYCLE"
    rec = (
        "Extract shared types or runtime symbols into a leaf module to maintain a DAG."
        if is_runtime
        else "Type-only cycles are erased at compile time, but can be untangled by moving shared interfaces to a types.ts barrel."
    )
    return {
        "severity": c.get("severity", "ERROR"),
        "type": "CIRCULAR_IMPORT" if is_runtime else "TYPE_ONLY_CYCLE",
        "line": 1,
        "cycle": " -> ".join(c.get("path", [])),
        "message": c.get("message", "Circular import cycle detected."),
        "recommendation": rec
    }


def _check_ts_health(
    source: str, target_abs: str, root_abs: str, rel_path: str, start_time: float
) -> Dict[str, Any]:
    from .ts_ast import parse_ts_js_ast, detect_ts_circular_imports, find_tsconfig
    from .polyglot_resolver import check_ts_lexical_scope

    parsed = parse_ts_js_ast(source, filename=target_abs)
    tsconfig = find_tsconfig(root_abs)
    prefixes = _get_tsconfig_prefixes(tsconfig)

    ts_issues: List[Dict[str, Any]] = []
    for imp in parsed.get("imports", []):
        iss = _verify_ts_import(imp, target_abs, root_abs, tsconfig, prefixes)
        if iss:
            ts_issues.append(iss)

    for c in detect_ts_circular_imports(target_abs, root_abs):
        ts_issues.append(_convert_ts_cycle(c))

    for w in check_ts_lexical_scope(source, filename=rel_path):
        ts_issues.append({
            "severity": "WARNING",
            "type": "UNDEFINED_SYMBOL",
            "line": 1,
            "message": w,
            "recommendation": "Declare or import the identifier before using it."
        })

    lang = "typescript" if target_abs.endswith((".ts", ".tsx")) else "javascript"
    return _format_polyglot_response(ts_issues, rel_path, lang, start_time)


def _check_go_health(
    source: str, rel_path: str, root_abs: str, start_time: float
) -> Dict[str, Any]:
    from .go_ast import parse_go_ast
    from .polyglot_resolver import check_go_lexical_scope, detect_go_circular_imports

    parsed = parse_go_ast(source, filename=rel_path)
    go_issues: List[Dict[str, Any]] = []

    if not parsed.get("package"):
        go_issues.append({
            "severity": "ERROR",
            "type": "MISSING_PACKAGE_DECLARATION",
            "line": 1,
            "message": f"Go source file '{rel_path}' is missing a 'package <name>' declaration.",
            "recommendation": "Add a valid package declaration at the top of the file."
        })

    for si in check_go_lexical_scope(source, filename=rel_path):
        go_issues.append({
            "severity": "ERROR",
            "type": "UNDECLARED_SYMBOL_OR_UNIMPORTED_PACKAGE",
            "line": 1,
            "message": si,
            "recommendation": "Import the package or declare the identifier."
        })

    for c in detect_go_circular_imports(root_abs):
        go_issues.append({
            "severity": "ERROR",
            "type": "CIRCULAR_IMPORT",
            "line": 1,
            "message": c["hazard"],
            "recommendation": "Break the package circular dependency."
        })

    return _format_polyglot_response(go_issues, rel_path, "go", start_time)


def _check_rust_health(
    source: str, rel_path: str, root_abs: str, start_time: float
) -> Dict[str, Any]:
    from .rust_ast import parse_rust_ast
    from .polyglot_resolver import check_rust_lexical_scope, detect_rust_circular_imports

    parse_rust_ast(source, filename=rel_path)
    rs_issues: List[Dict[str, Any]] = []

    brace_balance = source.count("{") - source.count("}")
    if brace_balance != 0:
        rs_issues.append({
            "severity": "ERROR",
            "type": "UNBALANCED_BRACES",
            "line": len(source.splitlines()),
            "message": f"Rust source file '{rel_path}' has unbalanced braces (delta: {brace_balance}).",
            "recommendation": "Check for unclosed struct, enum, or function blocks."
        })

    for si in check_rust_lexical_scope(source, filename=rel_path):
        rs_issues.append({
            "severity": "ERROR",
            "type": "UNDECLARED_SYMBOL_OR_UNRESOLVED_CRATE",
            "line": 1,
            "message": si,
            "recommendation": "Use 'use' statement or declare the item."
        })

    for c in detect_rust_circular_imports(root_abs):
        rs_issues.append({
            "severity": "ERROR",
            "type": "CIRCULAR_IMPORT",
            "line": 1,
            "message": c["hazard"],
            "recommendation": "Break the module circular dependency."
        })

    return _format_polyglot_response(rs_issues, rel_path, "rust", start_time)


def _verify_dart_import(
    imp: Dict[str, Any], target_abs: str, root_abs: str, rel_path: str
) -> Optional[Dict[str, Any]]:
    from .polyglot_resolver import resolve_dart_import_path
    mod_uri = imp.get("module", "")
    is_local = mod_uri.startswith(("./", "../")) or not mod_uri.startswith(("dart:", "package:"))
    if not is_local:
        return None
    resolved = resolve_dart_import_path(mod_uri, target_abs, root_abs)
    if resolved and os.path.isfile(resolved):
        return None
    return {
        "severity": "ERROR",
        "type": "BROKEN_LOCAL_IMPORT",
        "line": imp.get("line", 1),
        "module": mod_uri,
        "message": f"Cannot resolve local Dart import '{mod_uri}' from '{rel_path}'.",
        "recommendation": "Verify relative path or file existence."
    }


def _check_dart_health(
    source: str, target_abs: str, root_abs: str, rel_path: str, start_time: float
) -> Dict[str, Any]:
    from .dart_ast import parse_dart_ast
    from .polyglot_resolver import check_dart_lexical_scope, detect_dart_circular_imports

    parsed = parse_dart_ast(source, filename=target_abs)
    dart_issues: List[Dict[str, Any]] = []

    brace_balance = source.count("{") - source.count("}")
    if brace_balance != 0:
        dart_issues.append({
            "severity": "ERROR",
            "type": "UNBALANCED_BRACES",
            "line": len(source.splitlines()),
            "message": f"Dart source file '{rel_path}' has unbalanced braces (delta: {brace_balance}).",
            "recommendation": "Check for unclosed class, function, or block curly braces."
        })

    for imp in parsed.get("imports", []):
        iss = _verify_dart_import(imp, target_abs, root_abs, rel_path)
        if iss:
            dart_issues.append(iss)

    for si in check_dart_lexical_scope(source, filename=rel_path):
        dart_issues.append({
            "severity": "ERROR",
            "type": "UNDECLARED_SYMBOL_OR_UNIMPORTED_PACKAGE",
            "line": 1,
            "message": si,
            "recommendation": "Import the package or declare the identifier."
        })

    for c in detect_dart_circular_imports(root_abs):
        dart_issues.append({
            "severity": "ERROR",
            "type": "CIRCULAR_IMPORT",
            "line": 1,
            "message": c["hazard"],
            "recommendation": "Break the circular import dependency."
        })

    return _format_polyglot_response(dart_issues, rel_path, "dart", start_time)


def _check_jvm_health(
    source: str, target_abs: str, root_abs: str, rel_path: str, start_time: float
) -> Dict[str, Any]:
    from .jvm_ast import parse_jvm_ast
    from .polyglot_resolver import check_jvm_lexical_scope, detect_jvm_circular_imports

    is_kt = target_abs.endswith((".kt", ".kts"))
    lang_name = "kotlin" if is_kt else "java"
    parse_jvm_ast(source, filename=target_abs)
    jvm_issues: List[Dict[str, Any]] = []

    brace_balance = source.count("{") - source.count("}")
    if brace_balance != 0:
        jvm_issues.append({
            "severity": "ERROR",
            "type": "UNBALANCED_BRACES",
            "line": len(source.splitlines()),
            "message": f"{lang_name.capitalize()} source file '{rel_path}' has unbalanced braces (delta: {brace_balance}).",
            "recommendation": "Check for unclosed class, function, or method curly braces."
        })

    for si in check_jvm_lexical_scope(source, filename=rel_path):
        jvm_issues.append({
            "severity": "ERROR",
            "type": "UNDECLARED_SYMBOL_OR_UNRESOLVED_TYPE",
            "line": 1,
            "message": si,
            "recommendation": "Import the package or declare the class/type."
        })

    for c in detect_jvm_circular_imports(root_abs):
        jvm_issues.append({
            "severity": "ERROR",
            "type": "CIRCULAR_IMPORT",
            "line": 1,
            "message": c["hazard"],
            "recommendation": "Break the package circular dependency."
        })

    return _format_polyglot_response(jvm_issues, rel_path, lang_name, start_time)


def _file_not_found_response(target_path: str) -> Dict[str, Any]:
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


def _read_target_source(target_abs: str) -> Optional[str]:
    try:
        with open(target_abs, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as exc:
        logger.debug("Failed to read file %s: %s", target_abs, exc)
        return None


def _read_failure_response(target_path: str) -> Dict[str, Any]:
    return {
        "success": False,
        "error": f"Failed to read file: {target_path}",
        "health_score": 0.0,
        "issues": [],
        "duration_ms": 0.0
    }


def _syntax_error_response(syntax_err: Dict[str, Any], rel_path: str, start_time: float) -> Dict[str, Any]:
    duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
    return {
        "success": False,
        "file": rel_path,
        "health_score": 0.0,
        "total_issues": 1,
        "errors": 1,
        "warnings": 0,
        "issues": [syntax_err],
        "duration_ms": duration_ms
    }


def _compute_health_response(
    issues: List[Dict[str, Any]], rel_path: str, start_time: float
) -> Dict[str, Any]:
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


def _route_polyglot_preflight(
    target_abs: str, source: str, root_abs: str, rel_path: str, start_time: float
) -> Optional[Dict[str, Any]]:
    if target_abs.endswith((".ts", ".tsx", ".js", ".jsx")):
        return _check_ts_health(source, target_abs, root_abs, rel_path, start_time)
    if target_abs.endswith(".go"):
        return _check_go_health(source, rel_path, root_abs, start_time)
    if target_abs.endswith(".rs"):
        return _check_rust_health(source, rel_path, root_abs, start_time)
    if target_abs.endswith(".dart"):
        return _check_dart_health(source, target_abs, root_abs, rel_path, start_time)
    if target_abs.endswith((".kt", ".kts", ".java")):
        return _check_jvm_health(source, target_abs, root_abs, rel_path, start_time)
    return None


def _run_python_health_check(
    target_abs: str, source: str, root_abs: str, start_time: float, enable_ruff: bool
) -> Dict[str, Any]:
    rel_path = os.path.relpath(target_abs, root_abs)
    tree, syntax_err = verify_ast_syntax(source, target_abs)
    if syntax_err or tree is None:
        return _syntax_error_response(syntax_err or {}, rel_path, start_time)

    issues: List[Dict[str, Any]] = []
    issues.extend(verify_lexical_scope(tree, target_abs, root_abs))
    issues.extend(verify_local_imports(tree, target_abs, root_abs))
    issues.extend(detect_circular_imports(target_abs, root_abs, rel_path))

    if enable_ruff:
        issues.extend(_run_optional_ruff_check(source, target_abs, issues))

    return _compute_health_response(issues, rel_path, start_time)


def check_code_health(
    target_path: str,
    project_root: str = ".",
    enable_ruff: bool = False,
) -> Dict[str, Any]:
    """
    Compiler-Grade Preflight & Architecture Health Check for AI Coding Agents:
    1. Syntax & AST Validation (<5ms)
    2. Hallucinated / Undefined Symbol & Variable Names Detection before running tests via two-pass lexical scope analysis (<10ms)
    3. Cross-Module Local Import Verification (`BROKEN_LOCAL_IMPORT` with closest-match suggestions)
    4. Multi-Module Circular Import Cycle Detection (`CIRCULAR_IMPORT`)
    """
    start_time = time.perf_counter()
    target_abs = os.path.abspath(target_path)
    root_abs = os.path.abspath(project_root)
    rel_path = os.path.relpath(target_abs, root_abs)

    if not os.path.exists(target_abs):
        return _file_not_found_response(target_path)

    source = _read_target_source(target_abs)
    if source is None:
        return _read_failure_response(target_path)

    polyglot_res = _route_polyglot_preflight(target_abs, source, root_abs, rel_path, start_time)
    if polyglot_res is not None:
        return polyglot_res

    return _run_python_health_check(target_abs, source, root_abs, start_time, enable_ruff)
