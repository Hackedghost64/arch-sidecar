import os
import ast
import sys
import difflib
import subprocess
import time
from typing import Dict, List, Optional, Set, Any, Tuple

from .utils import run_ruff_autofix

class FreeVariableAnalyzer(ast.NodeVisitor):
    """
    Compiler-grade Free Variable Analysis: FV(E) = Loaded(E) \\ Bound(E).
    Tracks variable bindings across function parameters, assignments (Store),
    loop targets, exception handlers, and nested definitions to eliminate
    false-positive coupling hazards caused by shadowed parameters or local variables.
    """
    def __init__(self, root_nodes: List[ast.AST]):
        self.root_nodes = set(root_nodes)
        self.loaded_names: Set[str] = set()
        self.bound_names: Set[str] = set()
        self.declared_globals: Set[str] = set()
        for n in root_nodes:
            for sub in ast.walk(n):
                if isinstance(sub, (ast.Global, ast.Nonlocal)):
                    for name in sub.names:
                        self.declared_globals.add(name)

    def visit_FunctionDef(self, node: ast.FunctionDef):
        if node not in self.root_nodes:
            self.bound_names.add(node.name)
        self._bind_parameters(node.args)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        if node not in self.root_nodes:
            self.bound_names.add(node.name)
        self._bind_parameters(node.args)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef):
        if node not in self.root_nodes:
            self.bound_names.add(node.name)
        self.generic_visit(node)

    def _bind_parameters(self, args: ast.arguments):
        for arg in args.args:
            self.bound_names.add(arg.arg)
        if args.vararg:
            self.bound_names.add(args.vararg.arg)
        for arg in args.kwonlyargs:
            self.bound_names.add(arg.arg)
        if args.kwarg:
            self.bound_names.add(args.kwarg.arg)

    def visit_Name(self, node: ast.Name):
        if node.id in self.declared_globals:
            if isinstance(node.ctx, ast.Load):
                self.loaded_names.add(node.id)
        elif isinstance(node.ctx, ast.Store):
            self.bound_names.add(node.id)
        elif isinstance(node.ctx, ast.Load):
            self.loaded_names.add(node.id)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute):
        # Extract root identifier (e.g., subprocess.Popen -> 'subprocess')
        curr = node
        while isinstance(curr, ast.Attribute):
            curr = curr.value
        if isinstance(curr, ast.Name):
            if isinstance(curr.ctx, ast.Load):
                self.loaded_names.add(curr.id)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler):
        if node.name:
            self.bound_names.add(node.name)
        self.generic_visit(node)

    def get_free_variables(self) -> Set[str]:
        # Free variables required from enclosing module scope or external imports
        return (self.loaded_names - self.bound_names) | self.declared_globals

    def get_declared_globals(self) -> Set[str]:
        return set(self.declared_globals)

def get_free_variables_in_nodes(nodes: List[ast.AST]) -> Set[str]:
    analyzer = FreeVariableAnalyzer(nodes)
    for n in nodes:
        analyzer.visit(n)
    return analyzer.get_free_variables()

def get_declared_globals_in_nodes(nodes: List[ast.AST]) -> Set[str]:
    analyzer = FreeVariableAnalyzer(nodes)
    for n in nodes:
        analyzer.visit(n)
    return analyzer.get_declared_globals()

# Alias for backward compatibility
get_used_names_in_nodes = get_free_variables_in_nodes

def find_relevant_imports(
    source_tree: ast.Module,
    source_lines: List[str],
    used_names: Set[str],
    source_path: Optional[str] = None,
    project_root: str = ".",
    excluded_names: Optional[Set[str]] = None,
) -> List[str]:
    """
    Scans the imports in source_tree and extracts the exact lines of imports
    that define any of the names in used_names (plus `from __future__ import ...`
    compiler directives and wildcard `from X import *` imports that provide used names).
    Splits multi-name imports (`import os, sys, smtplib`) to only the used aliases.
    """
    import importlib

    extracted_imports = []
    seen = set()
    excluded = excluded_names or set()

    # Identify modules that also have a top-level wildcard `from <mod> import *`
    wildcard_modules: Set[str] = set()
    for node in source_tree.body:
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if any(alias.name == "*" for alias in node.names):
                wildcard_modules.add(node.module)

    # Track names explicitly provided by non-wildcard imports
    explicitly_imported: Set[str] = set()
    for node in source_tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                explicitly_imported.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    explicitly_imported.add(alias.asname or alias.name)

    non_shadowed_explicit = explicitly_imported - wildcard_modules

    for node in source_tree.body:
        if isinstance(node, ast.Import):
            needed_aliases = []
            for alias in node.names:
                name_in_scope = alias.asname or alias.name.split(".")[0]
                if name_in_scope in used_names:
                    # Skip `import datetime` if it's part of a multi-import line and shadowed by `from datetime import *`
                    if len(node.names) > 1 and name_in_scope in wildcard_modules:
                        continue
                    needed_aliases.append(alias)
            if needed_aliases:
                if len(node.names) == 1:
                    start = node.lineno - 1
                    end = getattr(node, "end_lineno", node.lineno)
                    stmt = "".join(source_lines[start:end]).strip()
                    if stmt not in seen:
                        extracted_imports.append(stmt)
                        seen.add(stmt)
                else:
                    for alias in needed_aliases:
                        alias_str = f"{alias.name} as {alias.asname}" if alias.asname else alias.name
                        stmt = f"import {alias_str}"
                        if stmt not in seen:
                            extracted_imports.append(stmt)
                            seen.add(stmt)

        elif isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                start = node.lineno - 1
                end = getattr(node, "end_lineno", node.lineno)
                stmt = "".join(source_lines[start:end]).strip()
                if stmt not in seen:
                    extracted_imports.append(stmt)
                    seen.add(stmt)
                continue

            has_wildcard = any(alias.name == "*" for alias in node.names)
            if has_wildcard:
                unexplained = used_names - BUILTIN_NAMES - excluded - non_shadowed_explicit
                needed = False
                if unexplained:
                    if node.level == 0 and node.module:
                        try:
                            mod_obj = importlib.import_module(node.module)
                            needed = any(hasattr(mod_obj, u) for u in unexplained)
                        except Exception:
                            needed = True
                    else:
                        needed = True
                if needed:
                    start = node.lineno - 1
                    end = getattr(node, "end_lineno", node.lineno)
                    stmt = "".join(source_lines[start:end]).strip()
                    if stmt not in seen:
                        extracted_imports.append(stmt)
                        seen.add(stmt)
            else:
                needed_aliases = [
                    alias for alias in node.names
                    if (alias.asname or alias.name) in used_names
                ]
                if needed_aliases:
                    if len(needed_aliases) == len(node.names):
                        start = node.lineno - 1
                        end = getattr(node, "end_lineno", node.lineno)
                        stmt = "".join(source_lines[start:end]).strip()
                    else:
                        dots = "." * (node.level or 0)
                        mod_str = node.module or ""
                        names_str = ", ".join(
                            f"{a.name} as {a.asname}" if a.asname else a.name
                            for a in needed_aliases
                        )
                        stmt = f"from {dots}{mod_str} import {names_str}"
                    if stmt not in seen:
                        extracted_imports.append(stmt)
                        seen.add(stmt)

    return extracted_imports

import builtins
import re
from .guard import run_differential_ruff
BUILTIN_NAMES = set(dir(builtins))
SECRET_PATTERN = re.compile(r'(_KEY|_SECRET|_TOKEN|_PASS|_PASSWORD)$', re.IGNORECASE)

def detect_coupling_hazards(
    from_tree: ast.Module,
    extracted_symbols: Set[str],
    used_names: Set[str],
    declared_globals: Optional[Set[str]] = None,
    extracted_nodes: Optional[List[ast.AST]] = None
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Analyzes whether extracted symbols reference remaining local functions/classes
    (circular import hazard), module-level constants (unbound global state),
    mutated shared globals (state desynchronization hazard in either direction),
    framework route decorators, or hardcoded credential/secret constants.
    """
    remaining_local_callables: Dict[str, str] = {}
    remaining_local_constants: Dict[str, str] = {}
    remaining_body_nodes: List[ast.AST] = []
    
    for node in from_tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name not in extracted_symbols:
                kind = "class" if isinstance(node, ast.ClassDef) else "function"
                remaining_local_callables[node.name] = kind
                remaining_body_nodes.append(node)
        elif isinstance(node, ast.Assign):
            is_extracted = any(isinstance(t, ast.Name) and t.id in extracted_symbols for t in node.targets)
            if not is_extracted:
                # Check if this assignment is a TypeVar or typing construct
                is_type_construct = False
                if isinstance(node.value, ast.Call):
                    fn = node.value.func
                    fn_id = getattr(fn, "id", "") or getattr(fn, "attr", "")
                    if fn_id in ("TypeVar", "ParamSpec", "NewType", "TypeAliasType", "TypeVarTuple"):
                        is_type_construct = True

                for t in node.targets:
                    if isinstance(t, ast.Name):
                        if not is_type_construct:
                            remaining_local_constants[t.id] = "module_constant"
                remaining_body_nodes.append(node)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id not in extracted_symbols:
                is_type_construct = False
                if node.value and isinstance(node.value, ast.Call):
                    fn = node.value.func
                    fn_id = getattr(fn, "id", "") or getattr(fn, "attr", "")
                    if fn_id in ("TypeVar", "ParamSpec", "NewType", "TypeAliasType", "TypeVarTuple"):
                        is_type_construct = True

                if not is_type_construct:
                    remaining_local_constants[node.target.id] = "module_constant"
                remaining_body_nodes.append(node)
        else:
            remaining_body_nodes.append(node)
                    
    circular_hazards = []
    global_hazards = []
    state_desync_hazards = []
    framework_hazards = []
    secret_warnings = []
    seen_desync_symbols: Set[str] = set()
    
    non_builtin_used = used_names - BUILTIN_NAMES
    
    for name in non_builtin_used:
        if name in remaining_local_callables:
            circular_hazards.append({
                "symbol": name,
                "type": remaining_local_callables[name],
                "hazard": f"Circular import hazard: extracted code calls remaining local symbol '{name}'",
                "recommendation": f"Also extract '{name}' into the target module, or move it to a shared intermediate utility."
            })
        elif name in remaining_local_constants:
            global_hazards.append({
                "symbol": name,
                "type": "constant",
                "hazard": f"Unbound module global: extracted code references local module constant '{name}'",
                "recommendation": f"Pass '{name}' as a parameter or extract '{name}' to a shared config module."
            })

    # Direction 1: Extracted function declares `global g_name`, and remaining code accesses `g_name`
    if declared_globals:
        for g_name in sorted(declared_globals):
            is_accessed_in_remaining = False
            for r_node in remaining_body_nodes:
                for sub in ast.walk(r_node):
                    if isinstance(sub, ast.Name) and sub.id == g_name:
                        is_accessed_in_remaining = True
                        break
                if is_accessed_in_remaining:
                    break
            if is_accessed_in_remaining and g_name not in seen_desync_symbols:
                seen_desync_symbols.add(g_name)
                state_desync_hazards.append({
                    "symbol": g_name,
                    "type": "mutable_global_desync",
                    "hazard": f"Mutable shared module state desync hazard: extracted function declares/mutates module global '{g_name}' via 'global {g_name}', but '{g_name}' is also accessed by remaining code in the source module.",
                    "recommendation": f"Encapsulate '{g_name}' into a shared state class, singleton, or accessor functions (e.g. get_{g_name}(), set_{g_name}()) in a shared module rather than splitting global state across module namespaces."
                })

    # Direction 2: Remaining code declares `global g_name`, and extracted code references or extracts `g_name`
    remaining_declared_globals: Set[str] = set()
    for r_node in remaining_body_nodes:
        for sub in ast.walk(r_node):
            if isinstance(sub, (ast.Global, ast.Nonlocal)):
                for g_name in sub.names:
                    remaining_declared_globals.add(g_name)

    for g_name in sorted(remaining_declared_globals):
        if (g_name in non_builtin_used or g_name in extracted_symbols) and g_name not in seen_desync_symbols:
            seen_desync_symbols.add(g_name)
            state_desync_hazards.append({
                "symbol": g_name,
                "type": "mutable_global_desync",
                "hazard": f"Mutable shared module state desync hazard: remaining code mutates module global '{g_name}' via 'global {g_name}', while extracted code reads or moves '{g_name}' into a separate module namespace.",
                "recommendation": f"Encapsulate '{g_name}' into a shared state class, singleton, or accessor functions (e.g. get_{g_name}(), set_{g_name}()) in a shared module rather than splitting global state across module namespaces."
            })

    # Detect framework route decorators and application instances
    if extracted_nodes:
        for node in extracted_nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in getattr(node, "decorator_list", []):
                    if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr == "route":
                        app_name = dec.func.value.id if isinstance(dec.func.value, ast.Name) else "app"
                        framework_hazards.append({
                            "symbol": node.name,
                            "type": "flask_route_decorator",
                            "hazard": f"Framework route decorator hazard: extracted route handler '{node.name}' is decorated with '@{app_name}.route'.",
                            "recommendation": f"Route handlers should be migrated to a Flask Blueprint (e.g. 'bp = Blueprint(...)') or APIRouter rather than extracting the core application instance."
                        })
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        if isinstance(node.value, ast.Call):
                            call_name = getattr(node.value.func, "id", "")
                            if call_name == "Flask":
                                framework_hazards.append({
                                    "symbol": t.id,
                                    "type": "flask_app_instance",
                                    "hazard": f"Framework application instance hazard: '{t.id}' is a core Flask application instance.",
                                    "recommendation": "Do not decouple the central Flask app instance into submodules. Use Flask Blueprints ('Blueprint') to decouple route groups."
                                })

    # Detect hardcoded secret constants
    for sym in extracted_symbols:
        if SECRET_PATTERN.search(sym):
            secret_warnings.append({
                "symbol": sym,
                "type": "hardcoded_secret",
                "hazard": f"Secret credential hazard: symbol '{sym}' appears to contain a hardcoded credential or secret.",
                "recommendation": f"Do not decouple static credentials into submodules. Refactor '{sym}' to load securely from environment variables (e.g. os.environ.get('{sym}'))."
            })

    return circular_hazards, global_hazards, state_desync_hazards, framework_hazards, secret_warnings


def compute_minimal_safe_closure(
    from_tree: ast.Module,
    initial_symbols: List[str]
) -> List[str]:
    """
    Computes the transitive closure of local callables and constants in `from_tree`
    required by `initial_symbols` so the extracted slice has zero back-references
    to `from_tree`.
    """
    local_defs: Dict[str, List[ast.AST]] = {}
    def_order: List[str] = []

    for node in from_tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name not in local_defs:
                local_defs[node.name] = []
                def_order.append(node.name)
            local_defs[node.name].append(node)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    if t.id not in local_defs:
                        local_defs[t.id] = []
                        def_order.append(t.id)
                    local_defs[t.id].append(node)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
            if name not in local_defs:
                local_defs[name] = []
                def_order.append(name)
            local_defs[name].append(node)

    closure_set: Set[str] = set(initial_symbols)
    queue: List[str] = list(initial_symbols)

    while queue:
        curr = queue.pop(0)
        nodes = local_defs.get(curr, [])
        if not nodes:
            continue
        used = get_used_names_in_nodes(nodes) - BUILTIN_NAMES
        for dep in def_order:
            if dep in used and dep not in closure_set:
                closure_set.add(dep)
                queue.append(dep)

    # Return in initial order followed by discovered dependencies in source definition order
    ordered = list(dict.fromkeys(initial_symbols))
    for sym in def_order:
        if sym in closure_set and sym not in ordered:
            ordered.append(sym)
    return ordered


def is_in_package(file_path: str) -> bool:
    """Checks whether the directory containing file_path is a Python package (contains __init__.py)."""
    dir_path = os.path.dirname(os.path.abspath(file_path))
    return os.path.exists(os.path.join(dir_path, "__init__.py"))


def get_package_root(file_path: str) -> str:
    """Walks upward to find the topmost ancestor directory that has __init__.py."""
    current = os.path.dirname(os.path.abspath(file_path))
    pkg_root = current
    while os.path.exists(os.path.join(current, "__init__.py")):
        pkg_root = current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return pkg_root


def compute_relative_import_path(from_file: str, to_file: str, project_root: str = ".") -> str:
    """
    Computes the correct import target for `from_file` importing `to_file`:
    1. Same package & same directory: '.<module>' (e.g. `from ._formatter import HelpFormatter`)
    2. Same package & subpackage/parent: '.sub.<module>' or '..<module>'
    3. Cross-package (different top-level packages or standalone -> package): 'otherpkg.<module>'
    4. Standalone scripts in same directory without __init__.py: '<module>'
    """
    from_abs = os.path.abspath(from_file)
    to_abs = os.path.abspath(to_file)
    from_dir = os.path.dirname(from_abs)
    to_dir = os.path.dirname(to_abs)
    to_mod = os.path.splitext(os.path.basename(to_abs))[0]

    from_in_pkg = is_in_package(from_abs)
    to_in_pkg = is_in_package(to_abs)

    if from_in_pkg:
        from_pkg_root = get_package_root(from_abs)
        if from_dir == to_dir:
            return f".{to_mod}"

        # Same top-level package hierarchy:
        # Either to_dir already has __init__.py with the same package root,
        # or to_dir does not exist on disk yet and lives inside from_pkg_root.
        same_pkg = (
            (to_in_pkg and get_package_root(to_abs) == from_pkg_root)
            or (not os.path.exists(to_dir) and (to_dir == from_pkg_root or to_dir.startswith(from_pkg_root + os.sep)))
        )
        if same_pkg:
            try:
                rel_dir = os.path.relpath(to_dir, from_dir)
            except ValueError:
                return to_mod

            parts = rel_dir.split(os.sep)
            up_levels = 0
            down_parts = []
            for p in parts:
                if p == "..":
                    up_levels += 1
                elif p != ".":
                    down_parts.append(p)

            dots = "." * (up_levels + 1)
            if down_parts:
                return f"{dots}{'.'.join(down_parts)}.{to_mod}"
            else:
                return f"{dots}{to_mod}"

    # Cross-package or standalone script importing from a package
    if to_in_pkg:
        to_pkg_root = get_package_root(to_abs)
        sys_path_base = os.path.dirname(to_pkg_root)
        rel_pkg_dir = os.path.relpath(to_dir, sys_path_base)
        return f"{rel_pkg_dir.replace(os.sep, '.')}.{to_mod}"

    # Different non-package subdirectories under project_root
    if from_dir != to_dir:
        root_abs = os.path.abspath(project_root)
        if to_dir.startswith(root_abs + os.sep):
            rel_dir = os.path.relpath(to_dir, root_abs)
            parts = [p for p in rel_dir.split(os.sep) if p not in (".", "")]
            if parts and parts[0] == "src":
                parts = parts[1:]
            if parts:
                return f"{'.'.join(parts)}.{to_mod}"

    return to_mod


class _SharedStateTransformer(ast.NodeTransformer):
    """
    Rewrites reads and writes of shared host module symbols (`shared_symbols`)
    inside extracted functions/methods to route through `_arch_get(name)` and
    `_arch_set(name, value)`, eliminating global state split and circular import locks.
    """
    def __init__(self, shared_symbols: Set[str], desync_symbols: Set[str]):
        self.shared_symbols = set(shared_symbols)
        self.desync_symbols = set(desync_symbols)
        self._local_shadow_stack: List[Set[str]] = []

    def _active_shared(self) -> Set[str]:
        if not self._local_shadow_stack:
            return self.shared_symbols
        shadowed = set().union(*self._local_shadow_stack)
        return self.shared_symbols - shadowed

    def _compute_fn_shadows(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> Set[str]:
        params: Set[str] = set()
        for arg in getattr(node.args, "posonlyargs", []) + node.args.args + node.args.kwonlyargs:
            params.add(arg.arg)
        if node.args.vararg:
            params.add(node.args.vararg.arg)
        if node.args.kwarg:
            params.add(node.args.kwarg.arg)

        declared_globals: Set[str] = set()
        assigned_locals: Set[str] = set()
        for sub in ast.walk(node):
            if isinstance(sub, (ast.Global, ast.Nonlocal)):
                for g in sub.names:
                    declared_globals.add(g)
            elif isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                assigned_locals.add(sub.id)
            elif isinstance(sub, ast.ExceptHandler) and sub.name:
                assigned_locals.add(sub.name)

        return params | (assigned_locals - declared_globals - self.desync_symbols)

    def _transform_fn(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.AST:
        shadows = self._compute_fn_shadows(node)
        self._local_shadow_stack.append(shadows)
        try:
            new_body = []
            for stmt in node.body:
                res = self.visit(stmt)
                if res is None:
                    continue
                elif isinstance(res, list):
                    new_body.extend(res)
                else:
                    new_body.append(res)
            if not new_body:
                new_body = [ast.Pass()]
            node.body = new_body
        finally:
            self._local_shadow_stack.pop()
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        return self._transform_fn(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        return self._transform_fn(node)

    def visit_Global(self, node: ast.Global) -> Optional[ast.AST]:
        active = self._active_shared()
        remaining = [name for name in node.names if name not in active]
        if not remaining:
            return None
        node.names = remaining
        return node

    def visit_Assign(self, node: ast.Assign) -> ast.AST:
        node.value = self.visit(node.value)
        active = self._active_shared()
        if (
            len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in active
        ):
            sym = node.targets[0].id
            call_node = ast.Call(
                func=ast.Name(id="_arch_set", ctx=ast.Load()),
                args=[ast.Constant(value=sym), node.value],
                keywords=[],
            )
            return ast.copy_location(ast.Expr(value=call_node), node)
        node.targets = [self.visit(t) for t in node.targets]
        return node

    def visit_AugAssign(self, node: ast.AugAssign) -> ast.AST:
        node.value = self.visit(node.value)
        active = self._active_shared()
        if isinstance(node.target, ast.Name) and node.target.id in active:
            sym = node.target.id
            get_call = ast.Call(
                func=ast.Name(id="_arch_get", ctx=ast.Load()),
                args=[ast.Constant(value=sym)],
                keywords=[],
            )
            bin_op = ast.BinOp(left=get_call, op=node.op, right=node.value)
            set_call = ast.Call(
                func=ast.Name(id="_arch_set", ctx=ast.Load()),
                args=[ast.Constant(value=sym), bin_op],
                keywords=[],
            )
            return ast.copy_location(ast.Expr(value=set_call), node)
        node.target = self.visit(node.target)
        return node

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if isinstance(node.ctx, ast.Load) and node.id in self._active_shared():
            call_node = ast.Call(
                func=ast.Name(id="_arch_get", ctx=ast.Load()),
                args=[ast.Constant(value=node.id)],
                keywords=[],
            )
            return ast.copy_location(call_node, node)
        return node


def _build_shared_state_runtime_header(
    host_import_target: str,
    host_filename: str,
    shared_symbols: Set[str],
) -> str:
    """
    Builds a self-contained runtime state-binding header for `to_file` when `share_state=True`.
    Synchronizes reads and writes of shared globals and host callables directly with the
    source module without creating top-level circular imports, while preserving `unittest.mock.patch`
    compatibility on either module.
    """
    sorted_syms = ", ".join(repr(s) for s in sorted(shared_symbols))
    return (
        "import importlib as _arch_importlib\n"
        "import sys as _arch_sys\n\n"
        f"_ARCH_HOST_MODULE = {host_import_target!r}\n"
        f"_ARCH_HOST_FILENAME = {host_filename!r}\n"
        f"_ARCH_SHARED_SYMBOLS = frozenset({{{sorted_syms}}})\n"
        "_ARCH_ORIG_VALS = {}\n\n\n"
        "def _arch_host():\n"
        "    _mod = _arch_sys.modules.get(_ARCH_HOST_MODULE)\n"
        "    if _mod is not None:\n"
        "        return _mod\n"
        "    _main = _arch_sys.modules.get('__main__')\n"
        "    if _main is not None and getattr(_main, '__file__', '').endswith(_ARCH_HOST_FILENAME):\n"
        "        return _main\n"
        "    return _arch_importlib.import_module(_ARCH_HOST_MODULE, package=__package__)\n\n\n"
        "def _arch_get(name: str):\n"
        "    _host = _arch_host()\n"
        "    _host_val = getattr(_host, name)\n"
        "    _orig = _ARCH_ORIG_VALS.setdefault(name, _host_val)\n"
        "    _self_dict = _arch_sys.modules[__name__].__dict__\n"
        "    if name in _self_dict and _self_dict[name] is not _orig:\n"
        "        return _self_dict[name]\n"
        "    return _host_val\n\n\n"
        "def _arch_set(name: str, value):\n"
        "    setattr(_arch_host(), name, value)\n"
        "    _arch_sys.modules[__name__].__dict__.pop(name, None)\n"
        "    return value\n\n\n"
        "def __getattr__(name: str):\n"
        "    if name in _ARCH_SHARED_SYMBOLS:\n"
        "        return getattr(_arch_host(), name)\n"
        "    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')\n"
    )


def _upsert_dunder_all_in_source(source_text: str, new_symbols: List[str]) -> Tuple[str, bool]:
    """
    If `source_text` already has a top-level `__all__ = [...]` assignment, merges `new_symbols`
    into that list (sorted) and returns `(updated_source, True)`. Otherwise returns `(source_text, False)`.
    """
    try:
        tree = ast.parse(source_text)
    except SyntaxError:
        return source_text, False

    lines = source_text.splitlines(keepends=True)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "__all__" and isinstance(node.value, (ast.List, ast.Tuple)):
                    existing_syms: List[str] = []
                    for elt in node.value.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            existing_syms.append(elt.value)
                    merged = sorted(set(existing_syms) | set(new_symbols))
                    inner = ", ".join(f'"{s}"' for s in merged)
                    formatted = f"__all__ = [{inner}]\n"
                    start_idx = node.lineno - 1
                    end_idx = getattr(node, "end_lineno", node.lineno)
                    lines[start_idx:end_idx] = [formatted]
                    return "".join(lines), True
    return source_text, False


def prefactor_decouple(
    symbol: str | List[str],
    from_file: str,
    to_file: str,
    project_root: str = ".",
    add_backward_compat_alias: bool = True,
    run_tests: Optional[str] = None,
    dry_run: bool = False,
    allow_circular: bool = False,
    allow_globals: bool = False,
    allow_desync: bool = False,
    share_state: bool = False,
    ruff_cleanup: bool = False,
) -> Dict[str, Any]:
    """
    Deterministically decouples symbols from `from_file` into `to_file`.
    Preserves exact formatting and comments using surgical AST slicing (or AST state-binding
    when `share_state=True`).
    Automatically resolves needed dependencies/imports, inserts clean imports,
    maintains backwards compatibility `__all__` re-exports, runs differential ruff checks,
    and executes headless test gates with transactional rollback.
    """
    t_start = time.perf_counter()
    symbols = [symbol] if isinstance(symbol, str) else list(symbol)
    root_abs = os.path.abspath(project_root)

    from_path = from_file if os.path.isabs(from_file) else os.path.join(root_abs, from_file)
    to_path = to_file if os.path.isabs(to_file) else os.path.join(root_abs, to_file)

    if not os.path.exists(from_path):
        return {
            "success": False,
            "error": f"Source file does not exist: {from_path}"
        }

    with open(from_path, "r", encoding="utf-8") as f:
        from_lines = f.readlines()
    from_source = "".join(from_lines)

    try:
        from_tree = ast.parse(from_source, filename=from_path)
    except SyntaxError as e:
        return {
            "success": False,
            "error": f"SyntaxError in source file {from_path}: {e}"
        }

    # Locate ALL AST nodes for each requested symbol (including all @overload defs and top-level constants)
    requested_symbols = set(symbols)
    found_symbols: Set[str] = set()
    nodes_to_extract: List[Tuple[str, ast.AST, int, int]] = []
    
    for node in from_tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in requested_symbols:
                dec_lines = [d.lineno for d in getattr(node, "decorator_list", []) if hasattr(d, "lineno")]
                start_line = (min([node.lineno] + dec_lines) if dec_lines else node.lineno) - 1
                end_line = getattr(node, "end_lineno", node.lineno)
                nodes_to_extract.append((node.name, node, start_line, end_line))
                found_symbols.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in requested_symbols:
                    start_line = node.lineno - 1
                    end_line = getattr(node, "end_lineno", node.lineno)
                    nodes_to_extract.append((t.id, node, start_line, end_line))
                    found_symbols.add(t.id)
                    break
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id in requested_symbols:
                start_line = node.lineno - 1
                end_line = getattr(node, "end_lineno", node.lineno)
                nodes_to_extract.append((node.target.id, node, start_line, end_line))
                found_symbols.add(node.target.id)

    missing_symbols = [s for s in symbols if s not in found_symbols]
    if missing_symbols:
        return {
            "success": False,
            "error": f"Symbols not found in {from_path}: {missing_symbols}"
        }

    # Collect used names inside the extracted symbols
    ast_nodes = [item[1] for item in nodes_to_extract]
    used_names = get_used_names_in_nodes(ast_nodes)
    declared_globals = get_declared_globals_in_nodes(ast_nodes)
    
    # Pre-flight check: detect circular dependency hazards, unbound global state, state desync, framework hazards, secrets
    extracted_names_set = {item[0] for item in nodes_to_extract}
    circular_hazards, global_hazards, state_desync_hazards, framework_hazards, secret_warnings = detect_coupling_hazards(
        from_tree, extracted_names_set, used_names, declared_globals=declared_globals, extracted_nodes=ast_nodes
    )
    safe_closure = compute_minimal_safe_closure(from_tree, symbols)
    share_state_hint = "Re-run with --share-state (or share_state=True) to automatically bind shared mutable globals and remaining host symbols across modules."

    if framework_hazards:
        return {
            "success": False,
            "error": "Refactoring blocked: Framework route / application architecture hazard detected.",
            "framework_hazards": framework_hazards,
            "state_desync_hazards": state_desync_hazards,
            "circular_hazards": circular_hazards,
            "global_state_hazards": global_hazards,
            "secret_warnings": secret_warnings,
            "minimal_safe_closure": safe_closure,
            "remediation": [h["recommendation"] for h in framework_hazards]
        }

    if state_desync_hazards and not (allow_desync or share_state):
        return {
            "success": False,
            "error": "Refactoring blocked: Mutable shared module state desynchronization hazard detected.",
            "state_desync_hazards": state_desync_hazards,
            "circular_hazards": circular_hazards,
            "global_state_hazards": global_hazards,
            "framework_hazards": framework_hazards,
            "secret_warnings": secret_warnings,
            "minimal_safe_closure": safe_closure,
            "remediation": [h["recommendation"] for h in state_desync_hazards] + [share_state_hint]
        }

    if circular_hazards and not (allow_circular or share_state):
        return {
            "success": False,
            "error": "Refactoring blocked: Circular import hazard detected.",
            "circular_hazards": circular_hazards,
            "global_state_hazards": global_hazards,
            "state_desync_hazards": state_desync_hazards,
            "framework_hazards": framework_hazards,
            "secret_warnings": secret_warnings,
            "minimal_safe_closure": safe_closure,
            "remediation": [h["recommendation"] for h in circular_hazards] + [
                f"Minimal self-contained symbol closure: {','.join(safe_closure)}",
                share_state_hint,
            ]
        }

    if global_hazards and not (allow_globals or share_state):
        return {
            "success": False,
            "error": "Refactoring blocked: Unbound module global state / constant detected.",
            "circular_hazards": circular_hazards,
            "global_state_hazards": global_hazards,
            "state_desync_hazards": state_desync_hazards,
            "framework_hazards": framework_hazards,
            "secret_warnings": secret_warnings,
            "minimal_safe_closure": safe_closure,
            "remediation": [h["recommendation"] for h in global_hazards] + [
                f"Minimal self-contained symbol closure: {','.join(safe_closure)}",
                share_state_hint,
            ]
        }

    desync_syms = {h["symbol"] for h in state_desync_hazards}
    shared_host_symbols: Set[str] = set()
    if share_state:
        shared_host_symbols = (
            desync_syms
            | {h["symbol"] for h in circular_hazards}
            | {h["symbol"] for h in global_hazards}
        )

    # Identify relevant imports from from_file (excluding extracted symbols and shared host symbols)
    needed_imports = find_relevant_imports(
        from_tree,
        from_lines,
        used_names - shared_host_symbols,
        source_path=from_path,
        project_root=root_abs,
        excluded_names=extracted_names_set | shared_host_symbols,
    )

    # Extract exact source lines (or AST-transformed blocks when share_state binds host symbols)
    extracted_code_blocks = []
    line_ranges_to_remove = []
    
    for sym_name, node, start_idx, end_idx in nodes_to_extract:
        # Include any leading comments or decorators immediately preceding the node
        actual_start = start_idx
        while actual_start > 0 and from_lines[actual_start - 1].strip().startswith(("#", "@")):
            actual_start -= 1
        
        if share_state and shared_host_symbols and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            import copy
            node_copy = copy.deepcopy(node)
            transformer = _SharedStateTransformer(shared_host_symbols, desync_syms)
            transformed_node = transformer.visit(node_copy)
            ast.fix_missing_locations(transformed_node)
            block = ast.unparse(transformed_node).rstrip()
        else:
            block = "".join(from_lines[actual_start:end_idx]).rstrip()
        extracted_code_blocks.append(block)
        line_ranges_to_remove.append((actual_start, end_idx))

    # Determine module name for import in from_file
    to_module_name = os.path.splitext(os.path.basename(to_path))[0]
    extracted_symbol_names = list(dict.fromkeys(item[0] for item in nodes_to_extract))
    all_syms_str = ", ".join(f'"{s}"' for s in sorted(set(extracted_symbol_names)))
    sorted_dunder_all = f"__all__ = [{all_syms_str}]\n"

    shared_runtime_header = ""
    if share_state and shared_host_symbols:
        host_import_target = compute_relative_import_path(to_path, from_path, project_root=root_abs)
        shared_runtime_header = _build_shared_state_runtime_header(
            host_import_target=host_import_target,
            host_filename=os.path.basename(from_path),
            shared_symbols=shared_host_symbols,
        ) + "\n\n"
    
    # Check if to_file already exists
    to_file_existed = os.path.exists(to_path)
    existing_to_lines = []
    if to_file_existed:
        with open(to_path, "r", encoding="utf-8") as f:
            existing_to_lines = f.readlines()
            
    # Build to_file content
    new_to_content_parts = []
    if not to_file_existed:
        new_to_content_parts.append(f'"""\nDecoupled module: {to_module_name}\nExtracted from {os.path.basename(from_path)}\n"""\n')
        if needed_imports:
            new_to_content_parts.append("\n".join(needed_imports) + "\n\n")
        if shared_runtime_header:
            new_to_content_parts.append(shared_runtime_header)
        new_to_content_parts.append("\n\n\n".join(extracted_code_blocks) + "\n\n" + sorted_dunder_all)
    else:
        # Append missing imports after module docstring / __future__ imports, and append code blocks at end
        existing_to_text = "".join(existing_to_lines)
        missing_imports = [imp for imp in needed_imports if imp not in existing_to_text]
        if missing_imports:
            insert_idx = 0
            try:
                existing_to_tree = ast.parse(existing_to_text, filename=to_path)
                for n in existing_to_tree.body:
                    if isinstance(n, ast.Expr) and isinstance(getattr(n, "value", None), ast.Constant) and isinstance(n.value.value, str) and insert_idx == 0:
                        insert_idx = getattr(n, "end_lineno", n.lineno)
                    elif isinstance(n, ast.ImportFrom) and n.module == "__future__":
                        insert_idx = getattr(n, "end_lineno", n.lineno)
                    else:
                        break
            except SyntaxError:
                insert_idx = 0
            prefix_lines = existing_to_lines[:insert_idx]
            suffix_lines = existing_to_lines[insert_idx:]
            rebuilt_existing = (
                "".join(prefix_lines)
                + ("\n" if prefix_lines and not prefix_lines[-1].endswith("\n") else "")
                + "\n".join(missing_imports)
                + "\n\n"
                + "".join(suffix_lines)
            )
            base_to_text = rebuilt_existing.rstrip()
        else:
            base_to_text = existing_to_text.rstrip()

        if shared_runtime_header and "_ARCH_SHARED_SYMBOLS" not in base_to_text:
            base_to_text = base_to_text + "\n\n\n" + shared_runtime_header.rstrip()
        combined_to = base_to_text + "\n\n\n" + "\n\n\n".join(extracted_code_blocks) + "\n"
        combined_to, had_all = _upsert_dunder_all_in_source(combined_to, extracted_symbol_names)
        if not had_all:
            combined_to = combined_to.rstrip() + "\n\n" + sorted_dunder_all
        new_to_content_parts.append(combined_to)

    new_to_source = "".join(new_to_content_parts)

    # Locate where to insert the new import in from_file (after last top-level import in original tree)
    orig_last_import_line = 0
    for node in from_tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            end = getattr(node, "end_lineno", node.lineno)
            if end > orig_last_import_line:
                orig_last_import_line = end

    # Build updated from_file content
    # Remove extracted line ranges (sorted in reverse order so indices don't shift)
    sorted_ranges = sorted(line_ranges_to_remove, key=lambda x: x[0], reverse=True)
    updated_from_lines = list(from_lines)
    last_import_line = orig_last_import_line
    for start_r, end_r in sorted_ranges:
        # Replace the removed block with a clean blank line (1 line replaces (end_r - start_r) lines)
        updated_from_lines[start_r:end_r] = ["\n"]
        if end_r <= orig_last_import_line:
            removed_delta = (end_r - start_r) - 1
            last_import_line = max(0, last_import_line - removed_delta)

    import_target = compute_relative_import_path(from_path, to_path, project_root=root_abs)
    import_stmt = f"from {import_target} import {', '.join(extracted_symbol_names)}\n"
    
    # Insert new import statement
    updated_from_lines.insert(last_import_line, import_stmt)

    # Add backwards-compatible alias and __all__ re-export if requested
    if add_backward_compat_alias:
        alias_block = [
            f"# Backward-compatibility aliases (decoupled into {to_module_name}.py)\n"
        ]
        for sym_name in extracted_symbol_names:
            alias_block.append(f"# {sym_name} = {to_module_name}.{sym_name}\n")
        # Place alias notice right after the import
        updated_from_lines.insert(last_import_line + 1, "".join(alias_block))

    new_from_source = "".join(updated_from_lines)
    if add_backward_compat_alias:
        new_from_source, had_from_all = _upsert_dunder_all_in_source(new_from_source, extracted_symbol_names)
        if not had_from_all:
            # Insert __all__ right after the alias block
            updated_from_lines.insert(last_import_line + 2, sorted_dunder_all)
            new_from_source = "".join(updated_from_lines)
        else:
            updated_from_lines = new_from_source.splitlines(keepends=True)

    # Verification 1: AST Syntax Check on both files
    try:
        ast.parse(new_to_source, filename=to_path)
    except SyntaxError as e:
        return {
            "success": False,
            "error": f"SyntaxError in generated target file {to_path}: {e}"
        }

    try:
        ast.parse(new_from_source, filename=from_path)
    except SyntaxError as e:
        return {
            "success": False,
            "error": f"SyntaxError in updated source file {from_path}: {e}"
        }

    # Verification 1b: Differential Ruff Gate (reports only net-new lint errors on touched files)
    rel_from = os.path.relpath(from_path, root_abs)
    rel_to = os.path.relpath(to_path, root_abs)
    pre_sources = {rel_from: from_source}
    if to_file_existed:
        pre_sources[rel_to] = "".join(existing_to_lines)
    post_sources = {rel_from: new_from_source, rel_to: new_to_source}
    ruff_gate = run_differential_ruff(pre_sources=pre_sources, post_sources=post_sources)

    # Generate unified diffs
    diff_from = "".join(difflib.unified_diff(
        from_lines,
        updated_from_lines,
        fromfile=f"a/{rel_from}",
        tofile=f"b/{rel_from}",
        n=3
    ))

    diff_to = "".join(difflib.unified_diff(
        existing_to_lines,
        new_to_source.splitlines(keepends=True),
        fromfile=f"a/{rel_to}" if to_file_existed else "/dev/null",
        tofile=f"b/{rel_to}",
        n=3
    ))

    combined_diff = f"{diff_to}\n{diff_from}"

    # If not dry_run, write files to disk
    if not dry_run:
        os.makedirs(os.path.dirname(to_path), exist_ok=True)
        with open(to_path, "w", encoding="utf-8") as f:
            f.write(new_to_source)
        with open(from_path, "w", encoding="utf-8") as f:
            f.write(new_from_source)

        if ruff_cleanup:
            # Deterministic cleanup: auto-remove dead imports and format code via Ruff ($0.00 cost)
            run_ruff_autofix([to_path, from_path], project_root=root_abs)

    # Verification 2: Headless Test Gate (e.g. pytest) with Transactional Rollback
    test_result = None
    rolled_back = False
    error_msg = None
    if run_tests and not dry_run:
        t_test = time.perf_counter()
        try:
            res = subprocess.run(
                run_tests,
                cwd=root_abs,
                shell=True,
                capture_output=True,
                text=True,
                timeout=30
            )
            test_duration_ms = round((time.perf_counter() - t_test) * 1000, 2)
            test_passed = (res.returncode == 0)
            test_result = {
                "command": run_tests,
                "passed": test_passed,
                "returncode": res.returncode,
                "duration_ms": test_duration_ms,
                "output": (res.stdout + res.stderr).strip()
            }
            if not test_passed:
                # Transactional Rollback: restore from_file and to_file to exact pre-refactor state
                with open(from_path, "w", encoding="utf-8") as f:
                    f.write(from_source)
                if to_file_existed:
                    with open(to_path, "w", encoding="utf-8") as f:
                        f.write("".join(existing_to_lines))
                else:
                    if os.path.exists(to_path):
                        os.remove(to_path)
                rolled_back = True
                error_msg = f"Verification gate failed (exit code {res.returncode}). Transactional rollback restored files."
        except Exception as e:
            # Rollback on test execution error
            with open(from_path, "w", encoding="utf-8") as f:
                f.write(from_source)
            if to_file_existed:
                with open(to_path, "w", encoding="utf-8") as f:
                    f.write("".join(existing_to_lines))
            else:
                if os.path.exists(to_path):
                    os.remove(to_path)
            rolled_back = True
            error_msg = f"Verification gate raised exception: {e}. Transactional rollback restored files."
            test_result = {
                "command": run_tests,
                "passed": False,
                "error": str(e)
            }

    duration_ms = (time.perf_counter() - t_start) * 1000
    is_success = True if (test_result is None or test_result.get("passed", True)) else False

    result_payload: Dict[str, Any] = {
        "success": is_success,
        "extracted_symbols": extracted_symbol_names,
        "from_file": rel_from,
        "to_file": rel_to,
        "import_target": import_target,
        "created_new_module": not to_file_existed,
        "share_state_bound": sorted(shared_host_symbols) if share_state else [],
        "execution_time_ms": round(duration_ms, 2),
        "diff": combined_diff,
        "ruff_gate": ruff_gate,
        "test_verification": test_result,
        "rolled_back": rolled_back,
        "circular_hazards": circular_hazards,
        "global_state_hazards": global_hazards,
        "state_desync_hazards": state_desync_hazards,
        "framework_hazards": framework_hazards,
        "secret_warnings": secret_warnings,
        "minimal_safe_closure": safe_closure,
    }
    if error_msg:
        result_payload["error"] = error_msg
    return result_payload

