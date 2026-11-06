import os
import ast
import sys
import difflib
import subprocess
import time
import logging
from typing import Dict, List, Optional, Set, Any, Tuple

from .utils import run_ruff_autofix

logger = logging.getLogger(__name__)

def _collect_declared_globals_from_nodes(nodes: List[ast.AST]) -> Set[str]:
    declared: Set[str] = set()
    for n in nodes:
        for sub in ast.walk(n):
            if isinstance(sub, (ast.Global, ast.Nonlocal)):
                declared.update(sub.names)
    return declared


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
        self.declared_globals: Set[str] = _collect_declared_globals_from_nodes(root_nodes)

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
        if isinstance(curr, ast.Name) and isinstance(curr.ctx, ast.Load):
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


def _collect_wildcard_modules(source_tree: ast.Module) -> Set[str]:
    wildcard_modules: Set[str] = set()
    for node in source_tree.body:
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if any(alias.name == "*" for alias in node.names):
                wildcard_modules.add(node.module)
    return wildcard_modules


def _explicit_names_from_node(node: ast.AST) -> List[str]:
    if isinstance(node, ast.Import):
        return [alias.asname or alias.name.split(".")[0] for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        return [alias.asname or alias.name for alias in node.names if alias.name != "*"]
    return []


def _collect_explicit_imports(source_tree: ast.Module) -> Set[str]:
    explicitly_imported: Set[str] = set()
    for node in source_tree.body:
        explicitly_imported.update(_explicit_names_from_node(node))
    return explicitly_imported


def _slice_node_text(node: ast.AST, source_lines: List[str]) -> str:
    start = node.lineno - 1
    end = getattr(node, "end_lineno", node.lineno)
    return "".join(source_lines[start:end]).strip()


def _format_alias(alias: ast.alias) -> str:
    return f"{alias.name} as {alias.asname}" if alias.asname else alias.name


def _format_import_from_aliases(node: ast.ImportFrom, aliases: List[ast.alias]) -> str:
    dots = "." * (node.level or 0)
    mod_str = node.module or ""
    names_str = ", ".join(_format_alias(a) for a in aliases)
    return f"from {dots}{mod_str} import {names_str}"


def _extract_import_stmts(
    node: ast.Import,
    source_lines: List[str],
    used_names: Set[str],
    wildcard_modules: Set[str],
) -> List[str]:
    needed_aliases = []
    is_multi = len(node.names) > 1
    for alias in node.names:
        name_in_scope = alias.asname or alias.name.split(".")[0]
        if name_in_scope not in used_names:
            continue
        if is_multi and name_in_scope in wildcard_modules:
            continue
        needed_aliases.append(alias)
    if not needed_aliases:
        return []
    if not is_multi:
        return [_slice_node_text(node, source_lines)]
    return [f"import {_format_alias(alias)}" for alias in needed_aliases]


def _is_wildcard_import_needed(node: ast.ImportFrom, unexplained: Set[str]) -> bool:
    import importlib
    if not unexplained:
        return False
    if node.level == 0 and node.module:
        try:
            mod_obj = importlib.import_module(node.module)
            return any(hasattr(mod_obj, u) for u in unexplained)
        except ImportError:
            return True
    return True


def _extract_import_from_stmts(
    node: ast.ImportFrom,
    source_lines: List[str],
    used_names: Set[str],
    unexplained: Set[str],
) -> List[str]:
    if node.module == "__future__":
        return [_slice_node_text(node, source_lines)]
    if any(alias.name == "*" for alias in node.names):
        if _is_wildcard_import_needed(node, unexplained):
            return [_slice_node_text(node, source_lines)]
        return []
    needed_aliases = [a for a in node.names if (a.asname or a.name) in used_names]
    if not needed_aliases:
        return []
    if len(needed_aliases) == len(node.names):
        return [_slice_node_text(node, source_lines)]
    return [_format_import_from_aliases(node, needed_aliases)]


def _extract_type_checking_node_stmts(sub: ast.AST, used_names: Set[str]) -> List[str]:
    if isinstance(sub, ast.Import):
        return [
            f"import {_format_alias(alias)}"
            for alias in sub.names
            if (alias.asname or alias.name.split(".")[0]) in used_names
        ]
    if isinstance(sub, ast.ImportFrom):
        needed = [a for a in sub.names if (a.asname or a.name) in used_names]
        if needed:
            return [_format_import_from_aliases(sub, needed)]
    return []


def _extract_type_checking_imports(source_tree: ast.Module, used_names: Set[str]) -> List[str]:
    tc_stmts: List[str] = []
    for node in source_tree.body:
        if not isinstance(node, ast.If) or "TYPE_CHECKING" not in ast.unparse(node.test):
            continue
        for sub in node.body:
            tc_stmts.extend(_extract_type_checking_node_stmts(sub, used_names))
    return tc_stmts


def _is_typevar_assign(node: ast.AST, used_names: Set[str]) -> bool:
    if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
        return False
    func_name = ast.unparse(node.value.func)
    if not any(tv in func_name for tv in ("TypeVar", "ParamSpec", "TypeVarTuple")):
        return False
    return any(isinstance(t, ast.Name) and t.id in used_names for t in node.targets)


def _append_unique(target_list: List[str], candidates: List[str], seen: Set[str]) -> None:
    for stmt in candidates:
        if stmt not in seen:
            target_list.append(stmt)
            seen.add(stmt)


def _append_type_checking_block(extracted_imports: List[str], tc_imports: List[str]) -> None:
    if not tc_imports:
        return
    has_typing = any("import typing as t" in imp or "from typing import" in imp for imp in extracted_imports)
    if not has_typing:
        extracted_imports.append("import typing as t")
    extracted_imports.append("if t.TYPE_CHECKING:\n    " + "\n    ".join(tc_imports))


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
    extracted_imports: List[str] = []
    seen: Set[str] = set()
    excluded = excluded_names or set()

    wildcard_modules = _collect_wildcard_modules(source_tree)
    non_shadowed_explicit = _collect_explicit_imports(source_tree) - wildcard_modules
    unexplained = used_names - BUILTIN_NAMES - excluded - non_shadowed_explicit

    for node in source_tree.body:
        if isinstance(node, ast.Import):
            _append_unique(extracted_imports, _extract_import_stmts(node, source_lines, used_names, wildcard_modules), seen)
        elif isinstance(node, ast.ImportFrom):
            _append_unique(extracted_imports, _extract_import_from_stmts(node, source_lines, used_names, unexplained), seen)

    type_checking_imports: List[str] = []
    _append_unique(type_checking_imports, _extract_type_checking_imports(source_tree, used_names), seen)
    _append_type_checking_block(extracted_imports, type_checking_imports)

    typevar_stmts = [ast.unparse(n) for n in source_tree.body if _is_typevar_assign(n, used_names)]
    _append_unique(extracted_imports, typevar_stmts, seen)
    return extracted_imports


import builtins
import re
from .guard import run_differential_ruff
BUILTIN_NAMES = set(dir(builtins))
SECRET_PATTERN = re.compile(r'(_KEY|_SECRET|_TOKEN|_PASS|_PASSWORD)$', re.IGNORECASE)
_TYPING_CONSTRUCT_NAMES = ("TypeVar", "ParamSpec", "NewType", "TypeAliasType", "TypeVarTuple")


def _is_typing_construct_value(val: Optional[ast.AST]) -> bool:
    if not (val and isinstance(val, ast.Call)):
        return False
    fn = val.func
    fn_id = getattr(fn, "id", "") or getattr(fn, "attr", "")
    return fn_id in _TYPING_CONSTRUCT_NAMES


def _record_assign_constants(
    node: ast.Assign,
    extracted_symbols: Set[str],
    remaining_constants: Dict[str, str],
) -> bool:
    target_names = [t.id for t in node.targets if isinstance(t, ast.Name)]
    if any(name in extracted_symbols for name in target_names):
        return False
    if not _is_typing_construct_value(node.value):
        for name in target_names:
            remaining_constants[name] = "module_constant"
    return True


def _record_ann_assign_constant(
    node: ast.AnnAssign,
    extracted_symbols: Set[str],
    remaining_constants: Dict[str, str],
) -> bool:
    if not isinstance(node.target, ast.Name):
        return True
    if node.target.id in extracted_symbols:
        return False
    if not _is_typing_construct_value(node.value):
        remaining_constants[node.target.id] = "module_constant"
    return True


def _partition_remaining_module_body(
    from_tree: ast.Module,
    extracted_symbols: Set[str],
) -> Tuple[Dict[str, str], Dict[str, str], List[ast.AST]]:
    remaining_callables: Dict[str, str] = {}
    remaining_constants: Dict[str, str] = {}
    remaining_nodes: List[ast.AST] = []

    for node in from_tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name not in extracted_symbols:
                remaining_callables[node.name] = "class" if isinstance(node, ast.ClassDef) else "function"
                remaining_nodes.append(node)
        elif isinstance(node, ast.Assign):
            if _record_assign_constants(node, extracted_symbols, remaining_constants):
                remaining_nodes.append(node)
        elif isinstance(node, ast.AnnAssign):
            if _record_ann_assign_constant(node, extracted_symbols, remaining_constants):
                remaining_nodes.append(node)
        else:
            remaining_nodes.append(node)

    return remaining_callables, remaining_constants, remaining_nodes


def _collect_circular_and_global_hazards(
    non_builtin_used: Set[str],
    remaining_callables: Dict[str, str],
    remaining_constants: Dict[str, str],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    circular_hazards: List[Dict[str, Any]] = []
    global_hazards: List[Dict[str, Any]] = []
    for name in non_builtin_used:
        if name in remaining_callables:
            circular_hazards.append({
                "symbol": name,
                "type": remaining_callables[name],
                "hazard": f"Circular import hazard: extracted code calls remaining local symbol '{name}'",
                "recommendation": f"Also extract '{name}' into the target module, or move it to a shared intermediate utility.",
            })
        elif name in remaining_constants:
            global_hazards.append({
                "symbol": name,
                "type": "constant",
                "hazard": f"Unbound module global: extracted code references local module constant '{name}'",
                "recommendation": f"Pass '{name}' as a parameter or extract '{name}' to a shared config module.",
            })
    return circular_hazards, global_hazards


def _is_name_referenced_in_nodes(target_name: str, nodes: List[ast.AST]) -> bool:
    for r_node in nodes:
        for sub in ast.walk(r_node):
            if isinstance(sub, ast.Name) and sub.id == target_name:
                return True
    return False


def _is_stream_wrapper_name(name: str) -> bool:
    name_lower = name.lower()
    return any(
        name_lower == s or name_lower.endswith(f"_{s}") or name_lower.startswith(f"{s}_")
        for s in ("stdout", "stderr", "stdin")
    )


def _make_global_desync_hazard(g_name: str, direction_desc: str) -> Dict[str, Any]:
    return {
        "symbol": g_name,
        "type": "mutable_global_desync",
        "hazard": f"Mutable shared module state desync hazard: {direction_desc}",
        "recommendation": f"Encapsulate '{g_name}' into a shared state class, singleton, or accessor functions (e.g. get_{g_name}(), set_{g_name}()) in a shared module rather than splitting global state across module namespaces.",
    }


def _collect_state_desync_hazards(
    declared_globals: Optional[Set[str]],
    remaining_body_nodes: List[ast.AST],
    non_builtin_used: Set[str],
    extracted_symbols: Set[str],
) -> List[Dict[str, Any]]:
    state_desync_hazards: List[Dict[str, Any]] = []
    seen_desync: Set[str] = set()

    for g_name in sorted(declared_globals or set()):
        if _is_name_referenced_in_nodes(g_name, remaining_body_nodes):
            seen_desync.add(g_name)
            desc = f"extracted function declares/mutates module global '{g_name}' via 'global {g_name}', but '{g_name}' is also accessed by remaining code in the source module."
            state_desync_hazards.append(_make_global_desync_hazard(g_name, desc))

    remaining_declared = _collect_declared_globals_from_nodes(remaining_body_nodes) - seen_desync
    relevant_remaining = remaining_declared & (non_builtin_used | extracted_symbols)
    for g_name in sorted(relevant_remaining):
        seen_desync.add(g_name)
        desc = f"remaining code mutates module global '{g_name}' via 'global {g_name}', while extracted code reads or moves '{g_name}' into a separate module namespace."
        state_desync_hazards.append(_make_global_desync_hazard(g_name, desc))

    for name in sorted(non_builtin_used - seen_desync):
        if _is_stream_wrapper_name(name):
            seen_desync.add(name)
            state_desync_hazards.append({
                "symbol": name,
                "type": "global_stream_entanglement",
                "hazard": f"Global stream entanglement hazard: extracted code depends on module-level process stream wrapper '{name}'.",
                "recommendation": f"Stream function '{name}' binds to mutable process streams (sys.stdout/stderr/stdin). Pass the stream explicitly as a parameter or re-run with --share-state to preserve stream monkeypatching and redirection.",
            })

    return state_desync_hazards


def _check_node_route_decorators(node: ast.AST) -> List[Dict[str, Any]]:
    hazards: List[Dict[str, Any]] = []
    for dec in getattr(node, "decorator_list", []):
        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr == "route":
            app_name = dec.func.value.id if isinstance(dec.func.value, ast.Name) else "app"
            hazards.append({
                "symbol": getattr(node, "name", ""),
                "type": "flask_route_decorator",
                "hazard": f"Framework route decorator hazard: extracted route handler '{getattr(node, 'name', '')}' is decorated with '@{app_name}.route'.",
                "recommendation": "Route handlers should be migrated to a Flask Blueprint (e.g. 'bp = Blueprint(...)') or APIRouter rather than extracting the core application instance.",
            })
    return hazards


def _check_node_flask_instance(node: ast.Assign) -> List[Dict[str, Any]]:
    if not (isinstance(node.value, ast.Call) and getattr(node.value.func, "id", "") == "Flask"):
        return []
    return [
        {
            "symbol": t.id,
            "type": "flask_app_instance",
            "hazard": f"Framework application instance hazard: '{t.id}' is a core Flask application instance.",
            "recommendation": "Do not decouple the central Flask app instance into submodules. Use Flask Blueprints ('Blueprint') to decouple route groups.",
        }
        for t in node.targets
        if isinstance(t, ast.Name)
    ]


def _collect_framework_hazards(extracted_nodes: Optional[List[ast.AST]]) -> List[Dict[str, Any]]:
    framework_hazards: List[Dict[str, Any]] = []
    for node in extracted_nodes or []:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            framework_hazards.extend(_check_node_route_decorators(node))
        elif isinstance(node, ast.Assign):
            framework_hazards.extend(_check_node_flask_instance(node))
    return framework_hazards


def _collect_secret_warnings(extracted_symbols: Set[str]) -> List[Dict[str, Any]]:
    return [
        {
            "symbol": sym,
            "type": "hardcoded_secret",
            "hazard": f"Secret credential hazard: symbol '{sym}' appears to contain a hardcoded credential or secret.",
            "recommendation": f"Do not decouple static credentials into submodules. Refactor '{sym}' to load securely from environment variables (e.g. os.environ.get('{sym}')).",
        }
        for sym in extracted_symbols
        if SECRET_PATTERN.search(sym)
    ]


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
    remaining_callables, remaining_constants, remaining_body_nodes = _partition_remaining_module_body(
        from_tree, extracted_symbols
    )
    non_builtin_used = used_names - BUILTIN_NAMES

    circular_hazards, global_hazards = _collect_circular_and_global_hazards(
        non_builtin_used, remaining_callables, remaining_constants
    )
    state_desync_hazards = _collect_state_desync_hazards(
        declared_globals, remaining_body_nodes, non_builtin_used, extracted_symbols
    )
    framework_hazards = _collect_framework_hazards(extracted_nodes)
    secret_warnings = _collect_secret_warnings(extracted_symbols)

    return circular_hazards, global_hazards, state_desync_hazards, framework_hazards, secret_warnings


def _extract_node_symbol_defs(node: ast.AST) -> List[Tuple[str, ast.AST]]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [(node.name, node)]
    if isinstance(node, ast.Assign):
        return [(t.id, node) for t in node.targets if isinstance(t, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [(node.target.id, node)]
    return []


def _collect_local_definitions(from_tree: ast.Module) -> Tuple[Dict[str, List[ast.AST]], List[str]]:
    local_defs: Dict[str, List[ast.AST]] = {}
    def_order: List[str] = []
    for node in from_tree.body:
        for name, def_node in _extract_node_symbol_defs(node):
            if name not in local_defs:
                local_defs[name] = []
                def_order.append(name)
            local_defs[name].append(def_node)
    return local_defs, def_order


def _expand_closure_dependencies(
    initial_symbols: List[str],
    local_defs: Dict[str, List[ast.AST]],
    def_order: List[str],
) -> List[str]:
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

    ordered = list(dict.fromkeys(initial_symbols))
    for sym in def_order:
        if sym in closure_set and sym not in ordered:
            ordered.append(sym)
    return ordered


def compute_minimal_safe_closure(
    from_tree: ast.Module,
    initial_symbols: List[str]
) -> List[str]:
    """
    Computes the transitive closure of local callables and constants in `from_tree`
    required by `initial_symbols` so the extracted slice has zero back-references
    to `from_tree`.
    """
    local_defs, def_order = _collect_local_definitions(from_tree)
    return _expand_closure_dependencies(initial_symbols, local_defs, def_order)


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


def _is_same_package(to_in_pkg: bool, to_abs: str, to_dir: str, from_pkg_root: str) -> bool:
    if to_in_pkg and get_package_root(to_abs) == from_pkg_root:
        return True
    if not os.path.exists(to_dir) and (to_dir == from_pkg_root or to_dir.startswith(from_pkg_root + os.sep)):
        return True
    return False


def _compute_same_package_rel_import(from_dir: str, to_dir: str, to_mod: str) -> str:
    try:
        rel_dir = os.path.relpath(to_dir, from_dir)
    except ValueError:
        return to_mod

    parts = rel_dir.split(os.sep)
    up_levels = sum(1 for p in parts if p == "..")
    down_parts = [p for p in parts if p not in ("..", ".")]
    dots = "." * (up_levels + 1)
    if down_parts:
        return f"{dots}{'.'.join(down_parts)}.{to_mod}"
    return f"{dots}{to_mod}"


def _compute_cross_package_import(to_abs: str, to_dir: str, to_mod: str) -> str:
    to_pkg_root = get_package_root(to_abs)
    sys_path_base = os.path.dirname(to_pkg_root)
    rel_pkg_dir = os.path.relpath(to_dir, sys_path_base)
    return f"{rel_pkg_dir.replace(os.sep, '.')}.{to_mod}"


def _compute_non_package_subdir_import(to_dir: str, to_mod: str, project_root: str) -> Optional[str]:
    root_abs = os.path.abspath(project_root)
    if not to_dir.startswith(root_abs + os.sep):
        return None
    rel_dir = os.path.relpath(to_dir, root_abs)
    parts = [p for p in rel_dir.split(os.sep) if p not in (".", "")]
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts:
        return f"{'.'.join(parts)}.{to_mod}"
    return None


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
        if _is_same_package(to_in_pkg, to_abs, to_dir, from_pkg_root):
            return _compute_same_package_rel_import(from_dir, to_dir, to_mod)

    if to_in_pkg:
        return _compute_cross_package_import(to_abs, to_dir, to_mod)

    if from_dir != to_dir:
        subdir_import = _compute_non_package_subdir_import(to_dir, to_mod, project_root)
        if subdir_import:
            return subdir_import

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


def _is_dunder_all_assign(node: ast.AST) -> bool:
    if not isinstance(node, ast.Assign) or not isinstance(node.value, (ast.List, ast.Tuple)):
        return False
    return any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)


def _extract_string_literals(node: ast.AST) -> List[str]:
    if not isinstance(node, (ast.List, ast.Tuple)):
        return []
    return [
        elt.value for elt in node.elts
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
    ]


def _format_dunder_all_replacement(
    lines: List[str],
    assign_node: ast.Assign,
    new_symbols: List[str]
) -> str:
    existing = _extract_string_literals(assign_node.value)
    merged = sorted(set(existing) | set(new_symbols))
    inner = ", ".join(f'"{s}"' for s in merged)
    formatted = f"__all__ = [{inner}]\n"
    start_idx = assign_node.lineno - 1
    end_idx = getattr(assign_node, "end_lineno", assign_node.lineno)
    updated = list(lines)
    updated[start_idx:end_idx] = [formatted]
    return "".join(updated)


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
        if _is_dunder_all_assign(node):
            assert isinstance(node, ast.Assign)
            return _format_dunder_all_replacement(lines, node, new_symbols), True
    return source_text, False


def _collect_existing_to_deps(to_abs: str, root_abs: str, rel_to: str) -> Set[str]:
    from .guard import get_module_imported_files
    deps: Set[str] = set()
    for _, _, resolved_path in get_module_imported_files(to_abs, root_abs):
        rel_dep = os.path.relpath(os.path.abspath(resolved_path), root_abs)
        if rel_dep != rel_to:
            deps.add(rel_dep)
    return deps


def _resolve_import_node_deps(node: ast.AST, to_abs: str, root_abs: str, rel_to: str) -> Set[str]:
    from .guard import resolve_local_module_path
    deps: Set[str] = set()
    mod_names: List[str] = []
    if isinstance(node, ast.Import):
        mod_names = [alias.name for alias in node.names]
    elif isinstance(node, ast.ImportFrom):
        level = getattr(node, "level", 0)
        mod_names = [("." * level) + (node.module or "")]

    for name in mod_names:
        res = resolve_local_module_path(to_abs, name, root_abs)
        if not res:
            continue
        rel_dep = os.path.relpath(os.path.abspath(res), root_abs)
        if rel_dep != rel_to:
            deps.add(rel_dep)
    return deps


def _collect_needed_import_deps(
    needed_import_lines: List[str],
    to_abs: str,
    root_abs: str,
    rel_to: str
) -> Set[str]:
    if not needed_import_lines:
        return set()
    try:
        dummy_tree = ast.parse("\n".join(needed_import_lines))
    except Exception as exc:
        logger.debug("Failed to parse needed import lines: %s", exc)
        return set()

    deps: Set[str] = set()
    for node in ast.walk(dummy_tree):
        deps.update(_resolve_import_node_deps(node, to_abs, root_abs, rel_to))
    return deps


def _find_alias_cycle_bfs(
    graph_edges: Dict[str, Set[str]],
    projected_to_deps: Set[str],
    rel_from: str,
    rel_to: str
) -> Optional[List[str]]:
    queue: List[Tuple[str, List[str]]] = []
    visited: Set[str] = set()

    for dep in sorted(projected_to_deps):
        if dep == rel_from:
            return [rel_from, rel_to, rel_from]
        queue.append((dep, [rel_from, rel_to, dep]))
        visited.add(dep)

    while queue:
        curr, path = queue.pop(0)
        if curr == rel_from:
            return path
        if len(path) > 12:
            continue

        for next_dep in sorted(graph_edges.get(curr, set())):
            if next_dep == rel_from:
                return path + [rel_from]
            if next_dep not in visited:
                visited.add(next_dep)
                queue.append((next_dep, path + [next_dep]))

    return None


def check_projected_alias_cycle(
    from_path: str,
    to_path: str,
    project_root: str,
    needed_import_lines: List[str],
    to_file_existed: bool,
    add_backward_compat_alias: bool,
    shared_host_symbols: Optional[Set[str]] = None,
) -> Optional[List[str]]:
    """
    Simulates the backward-compatibility alias edge (from_path -> to_path)
    on the repository dependency graph and verifies whether to_path transitively
    reaches from_path, which would form an alias-induced circular import cycle.

    Returns the cycle path [from_rel, to_rel, ..., from_rel] if a cycle is formed,
    or None if the projected graph remains a DAG.
    """
    if not add_backward_compat_alias:
        return None

    from .cluster import build_repository_dependency_graph

    root_abs = os.path.abspath(project_root)
    from_abs = os.path.abspath(from_path)
    to_abs = os.path.abspath(to_path)

    rel_from = os.path.relpath(from_abs, root_abs)
    rel_to = os.path.relpath(to_abs, root_abs)

    try:
        graph = build_repository_dependency_graph(root_abs)
    except Exception as exc:
        logger.debug("Failed to build repository dependency graph: %s", exc)
        return None

    projected_to_deps: Set[str] = set()

    # A. Existing imports if to_file already existed on disk
    if to_file_existed and os.path.isfile(to_abs):
        projected_to_deps.update(_collect_existing_to_deps(to_abs, root_abs, rel_to))

    # B. Dependencies introduced by needed_imports
    projected_to_deps.update(_collect_needed_import_deps(needed_import_lines, to_abs, root_abs, rel_to))

    # C. If shared_host_symbols are bound, to_file imports from_file
    if shared_host_symbols:
        projected_to_deps.add(rel_from)

    return _find_alias_cycle_bfs(graph.edges, projected_to_deps, rel_from, rel_to)


def _resolve_refactor_paths(
    from_file: str, to_file: str, project_root: str
) -> Tuple[str, str, str, str, str]:
    root_abs = os.path.abspath(project_root)
    from_path = from_file if os.path.isabs(from_file) else os.path.join(root_abs, from_file)
    to_path = to_file if os.path.isabs(to_file) else os.path.join(root_abs, to_file)
    rel_from = os.path.relpath(from_path, root_abs)
    rel_to = os.path.relpath(to_path, root_abs)
    return from_path, to_path, rel_from, rel_to, root_abs


def _load_and_parse_source(
    from_path: str
) -> Tuple[Optional[List[str]], Optional[str], Optional[ast.Module], Optional[str]]:
    if not os.path.exists(from_path):
        return None, None, None, f"Source file does not exist: {from_path}"
    with open(from_path, "r", encoding="utf-8") as f:
        from_lines = f.readlines()
    from_source = "".join(from_lines)
    try:
        from_tree = ast.parse(from_source, filename=from_path)
    except SyntaxError as e:
        return from_lines, from_source, None, f"SyntaxError in source file {from_path}: {e}"
    return from_lines, from_source, from_tree, None


def _match_node_to_extract(
    node: ast.AST, requested_symbols: Set[str]
) -> Optional[Tuple[str, ast.AST, int, int]]:
    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        if node.name in requested_symbols:
            dec_lines = [d.lineno for d in getattr(node, "decorator_list", []) if hasattr(d, "lineno")]
            start_line = (min([node.lineno] + dec_lines) if dec_lines else node.lineno) - 1
            end_line = getattr(node, "end_lineno", node.lineno)
            return (node.name, node, start_line, end_line)
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id in requested_symbols:
                start_line = node.lineno - 1
                end_line = getattr(node, "end_lineno", node.lineno)
                return (t.id, node, start_line, end_line)
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        if node.target.id in requested_symbols:
            start_line = node.lineno - 1
            end_line = getattr(node, "end_lineno", node.lineno)
            return (node.target.id, node, start_line, end_line)
    return None


def _locate_symbols_to_extract(
    from_tree: ast.Module, requested_symbols: Set[str]
) -> Tuple[List[Tuple[str, ast.AST, int, int]], Set[str]]:
    nodes_to_extract: List[Tuple[str, ast.AST, int, int]] = []
    found_symbols: Set[str] = set()
    for node in from_tree.body:
        matched = _match_node_to_extract(node, requested_symbols)
        if matched:
            nodes_to_extract.append(matched)
            found_symbols.add(matched[0])
    return nodes_to_extract, found_symbols


def _evaluate_alias_cycle(
    from_path: str,
    to_path: str,
    root_abs: str,
    needed_imports: List[str],
    to_file_existed: bool,
    add_backward_compat_alias: bool,
    shared_host_symbols: Set[str],
    share_state: bool,
    symbols: List[str],
    rel_from: str,
    rel_to: str,
) -> Optional[Dict[str, Any]]:
    alias_cycle_path = check_projected_alias_cycle(
        from_path=from_path,
        to_path=to_path,
        project_root=root_abs,
        needed_import_lines=needed_imports,
        to_file_existed=to_file_existed,
        add_backward_compat_alias=add_backward_compat_alias,
        shared_host_symbols=shared_host_symbols if share_state else None,
    )
    if not alias_cycle_path:
        return None
    cycle_str = " -> ".join(alias_cycle_path)
    return {
        "symbol": ", ".join(symbols),
        "type": "alias_cycle_hazard",
        "cycle": cycle_str,
        "hazard": f"Alias-induced circular import cycle: backward-compatibility alias in '{rel_from}' imports '{rel_to}', but '{rel_to}' transitively reaches '{rel_from}' via {' -> '.join(alias_cycle_path[1:])}.",
        "recommendation": f"Do not insert a backward-compatibility alias in '{rel_from}' (re-run with add_backward_compat_alias=False), or extract intermediate dependencies into a separate leaf module."
    }


def _check_hazard_blocks(
    framework_hazards: List[Dict[str, Any]],
    state_desync_hazards: List[Dict[str, Any]],
    circular_hazards: List[Dict[str, Any]],
    global_hazards: List[Dict[str, Any]],
    secret_warnings: List[Dict[str, Any]],
    safe_closure: List[str],
    allow_desync: bool,
    allow_circular: bool,
    allow_globals: bool,
    share_state: bool,
    share_state_hint: str,
) -> Optional[Dict[str, Any]]:
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
    return None


def _extract_source_code_blocks(
    nodes_to_extract: List[Tuple[str, ast.AST, int, int]],
    from_lines: List[str],
    share_state: bool,
    shared_host_symbols: Set[str],
    desync_syms: Set[str],
) -> Tuple[List[str], List[Tuple[int, int]]]:
    extracted_code_blocks: List[str] = []
    line_ranges_to_remove: List[Tuple[int, int]] = []

    for sym_name, node, start_idx, end_idx in nodes_to_extract:
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

    return extracted_code_blocks, line_ranges_to_remove


def _build_shared_runtime_header_if_needed(
    share_state: bool,
    shared_host_symbols: Set[str],
    to_path: str,
    from_path: str,
    root_abs: str,
) -> str:
    if not (share_state and shared_host_symbols):
        return ""
    host_import_target = compute_relative_import_path(to_path, from_path, project_root=root_abs)
    return _build_shared_state_runtime_header(
        host_import_target=host_import_target,
        host_filename=os.path.basename(from_path),
        shared_symbols=shared_host_symbols,
    ) + "\n\n"


def _find_future_and_docstring_end(tree: ast.Module) -> int:
    insert_idx = 0
    for n in tree.body:
        if isinstance(n, ast.Expr) and isinstance(getattr(n, "value", None), ast.Constant) and isinstance(n.value.value, str) and insert_idx == 0:
            insert_idx = getattr(n, "end_lineno", n.lineno)
        elif isinstance(n, ast.ImportFrom) and n.module == "__future__":
            insert_idx = getattr(n, "end_lineno", n.lineno)
        else:
            break
    return insert_idx


def _insert_missing_imports(
    existing_to_lines: List[str], missing_imports: List[str], to_path: str
) -> str:
    existing_to_text = "".join(existing_to_lines)
    insert_idx = 0
    try:
        existing_to_tree = ast.parse(existing_to_text, filename=to_path)
        insert_idx = _find_future_and_docstring_end(existing_to_tree)
    except SyntaxError:
        insert_idx = 0

    prefix_lines = existing_to_lines[:insert_idx]
    suffix_lines = existing_to_lines[insert_idx:]
    return (
        "".join(prefix_lines)
        + ("\n" if prefix_lines and not prefix_lines[-1].endswith("\n") else "")
        + "\n".join(missing_imports)
        + "\n\n"
        + "".join(suffix_lines)
    )


def _build_new_to_source(
    to_file_existed: bool,
    existing_to_lines: List[str],
    to_module_name: str,
    from_path: str,
    to_path: str,
    needed_imports: List[str],
    shared_runtime_header: str,
    extracted_code_blocks: List[str],
    extracted_symbol_names: List[str],
    sorted_dunder_all: str,
) -> str:
    if not to_file_existed:
        parts = [f'"""\nDecoupled module: {to_module_name}\nExtracted from {os.path.basename(from_path)}\n"""\n']
        if needed_imports:
            parts.append("\n".join(needed_imports) + "\n\n")
        if shared_runtime_header:
            parts.append(shared_runtime_header)
        parts.append("\n\n\n".join(extracted_code_blocks) + "\n\n" + sorted_dunder_all)
        return "".join(parts)

    existing_to_text = "".join(existing_to_lines)
    missing_imports = [imp for imp in needed_imports if imp not in existing_to_text]
    if missing_imports:
        rebuilt_existing = _insert_missing_imports(existing_to_lines, missing_imports, to_path)
        base_to_text = rebuilt_existing.rstrip()
    else:
        base_to_text = existing_to_text.rstrip()

    if shared_runtime_header and "_ARCH_SHARED_SYMBOLS" not in base_to_text:
        base_to_text = base_to_text + "\n\n\n" + shared_runtime_header.rstrip()
    combined_to = base_to_text + "\n\n\n" + "\n\n\n".join(extracted_code_blocks) + "\n"
    combined_to, had_all = _upsert_dunder_all_in_source(combined_to, extracted_symbol_names)
    if not had_all:
        combined_to = combined_to.rstrip() + "\n\n" + sorted_dunder_all
    return combined_to


def _find_last_import_line(tree: ast.Module) -> int:
    last_line = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            end = getattr(node, "end_lineno", node.lineno)
            if end > last_line:
                last_line = end
    return last_line


def _remove_extracted_ranges_and_adjust_import(
    from_lines: List[str],
    line_ranges_to_remove: List[Tuple[int, int]],
    orig_last_import_line: int,
) -> Tuple[List[str], int]:
    sorted_ranges = sorted(line_ranges_to_remove, key=lambda x: x[0], reverse=True)
    updated_lines = list(from_lines)
    last_import_line = orig_last_import_line
    for start_r, end_r in sorted_ranges:
        updated_lines[start_r:end_r] = ["\n"]
        if end_r <= orig_last_import_line:
            removed_delta = (end_r - start_r) - 1
            last_import_line = max(0, last_import_line - removed_delta)
    return updated_lines, last_import_line


def _build_updated_from_source(
    from_lines: List[str],
    from_tree: ast.Module,
    line_ranges_to_remove: List[Tuple[int, int]],
    import_target: str,
    extracted_symbol_names: List[str],
    add_backward_compat_alias: bool,
    to_module_name: str,
    sorted_dunder_all: str,
) -> Tuple[str, List[str]]:
    orig_last_import = _find_last_import_line(from_tree)
    updated_lines, last_import_line = _remove_extracted_ranges_and_adjust_import(
        from_lines, line_ranges_to_remove, orig_last_import
    )

    import_stmt = f"from {import_target} import {', '.join(extracted_symbol_names)}\n"
    updated_lines.insert(last_import_line, import_stmt)

    if add_backward_compat_alias:
        alias_block = [f"# Backward-compatibility aliases (decoupled into {to_module_name}.py)\n"]
        for sym_name in extracted_symbol_names:
            alias_block.append(f"# {sym_name} = {to_module_name}.{sym_name}\n")
        updated_lines.insert(last_import_line + 1, "".join(alias_block))

    new_from_source = "".join(updated_lines)
    if add_backward_compat_alias:
        new_from_source, had_from_all = _upsert_dunder_all_in_source(new_from_source, extracted_symbol_names)
        if not had_from_all:
            updated_lines.insert(last_import_line + 2, sorted_dunder_all)
            new_from_source = "".join(updated_lines)
        else:
            updated_lines = new_from_source.splitlines(keepends=True)

    return new_from_source, updated_lines


def _verify_ast_syntaxes(
    new_to_source: str, to_path: str, new_from_source: str, from_path: str
) -> Optional[str]:
    try:
        ast.parse(new_to_source, filename=to_path)
    except SyntaxError as e:
        return f"SyntaxError in generated target file {to_path}: {e}"

    try:
        ast.parse(new_from_source, filename=from_path)
    except SyntaxError as e:
        return f"SyntaxError in updated source file {from_path}: {e}"

    return None


def _generate_diffs(
    rel_from: str,
    rel_to: str,
    from_lines: List[str],
    updated_from_lines: List[str],
    existing_to_lines: List[str],
    new_to_source: str,
    to_file_existed: bool,
) -> str:
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
    return f"{diff_to}\n{diff_from}"


def _apply_decouple_writes(
    to_path: str,
    new_to_source: str,
    from_path: str,
    new_from_source: str,
    ruff_cleanup: bool,
    root_abs: str,
) -> None:
    os.makedirs(os.path.dirname(to_path), exist_ok=True)
    with open(to_path, "w", encoding="utf-8") as f:
        f.write(new_to_source)
    with open(from_path, "w", encoding="utf-8") as f:
        f.write(new_from_source)

    if ruff_cleanup:
        run_ruff_autofix([to_path, from_path], project_root=root_abs)


def _rollback_files(
    from_path: str,
    from_source: str,
    to_path: str,
    to_file_existed: bool,
    existing_to_lines: List[str],
) -> None:
    with open(from_path, "w", encoding="utf-8") as f:
        f.write(from_source)
    if to_file_existed:
        with open(to_path, "w", encoding="utf-8") as f:
            f.write("".join(existing_to_lines))
    elif os.path.exists(to_path):
        os.remove(to_path)


def _run_test_verification_gate(
    run_tests: str,
    root_abs: str,
    from_path: str,
    from_source: str,
    to_path: str,
    to_file_existed: bool,
    existing_to_lines: List[str],
) -> Tuple[Optional[Dict[str, Any]], bool, Optional[str]]:
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
            _rollback_files(from_path, from_source, to_path, to_file_existed, existing_to_lines)
            return test_result, True, f"Verification gate failed (exit code {res.returncode}). Transactional rollback restored files."
        return test_result, False, None
    except Exception as e:
        logger.debug("Verification gate raised exception during test execution: %s", e)
        _rollback_files(from_path, from_source, to_path, to_file_existed, existing_to_lines)
        test_result = {
            "command": run_tests,
            "passed": False,
            "error": str(e)
        }
        return test_result, True, f"Verification gate raised exception: {e}. Transactional rollback restored files."


def _execute_and_verify(
    run_tests: Optional[str],
    dry_run: bool,
    to_path: str,
    new_to_source: str,
    from_path: str,
    new_from_source: str,
    from_source: str,
    existing_to_lines: List[str],
    to_file_existed: bool,
    ruff_cleanup: bool,
    root_abs: str,
) -> Tuple[Optional[Dict[str, Any]], bool, Optional[str]]:
    if not dry_run:
        _apply_decouple_writes(to_path, new_to_source, from_path, new_from_source, ruff_cleanup, root_abs)

    if run_tests and not dry_run:
        return _run_test_verification_gate(
            run_tests, root_abs, from_path, from_source, to_path, to_file_existed, existing_to_lines
        )
    return None, False, None


def _load_existing_to_lines(to_path: str, to_file_existed: bool) -> List[str]:
    if not to_file_existed:
        return []
    with open(to_path, "r", encoding="utf-8") as f:
        return f.readlines()


def _build_decouple_result_payload(
    is_success: bool,
    extracted_symbol_names: List[str],
    rel_from: str,
    rel_to: str,
    import_target: str,
    created_new_module: bool,
    shared_host_symbols: Set[str],
    share_state: bool,
    duration_ms: float,
    combined_diff: str,
    ruff_gate: Dict[str, Any],
    test_result: Optional[Dict[str, Any]],
    rolled_back: bool,
    circular_hazards: List[Dict[str, Any]],
    global_hazards: List[Dict[str, Any]],
    state_desync_hazards: List[Dict[str, Any]],
    framework_hazards: List[Dict[str, Any]],
    secret_warnings: List[Dict[str, Any]],
    safe_closure: List[str],
    error_msg: Optional[str],
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "success": is_success,
        "extracted_symbols": extracted_symbol_names,
        "from_file": rel_from,
        "to_file": rel_to,
        "import_target": import_target,
        "created_new_module": created_new_module,
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
        payload["error"] = error_msg
    return payload


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
    from_path, to_path, rel_from, rel_to, root_abs = _resolve_refactor_paths(from_file, to_file, project_root)
    to_file_existed = os.path.exists(to_path)

    from_lines, from_source, from_tree, load_err = _load_and_parse_source(from_path)
    if load_err:
        return {"success": False, "error": load_err}
    assert from_lines is not None and from_source is not None and from_tree is not None

    nodes_to_extract, found_symbols = _locate_symbols_to_extract(from_tree, set(symbols))
    missing_symbols = [s for s in symbols if s not in found_symbols]
    if missing_symbols:
        return {"success": False, "error": f"Symbols not found in {from_path}: {missing_symbols}"}

    ast_nodes = [item[1] for item in nodes_to_extract]
    used_names = get_used_names_in_nodes(ast_nodes)
    declared_globals = get_declared_globals_in_nodes(ast_nodes)
    extracted_names_set = {item[0] for item in nodes_to_extract}

    circular_hazards, global_hazards, state_desync_hazards, framework_hazards, secret_warnings = detect_coupling_hazards(
        from_tree, extracted_names_set, used_names, declared_globals=declared_globals, extracted_nodes=ast_nodes
    )
    safe_closure = compute_minimal_safe_closure(from_tree, symbols)
    share_state_hint = "Re-run with --share-state (or share_state=True) to automatically bind shared mutable globals and remaining host symbols across modules."

    desync_syms = {h["symbol"] for h in state_desync_hazards}
    shared_host_symbols: Set[str] = set()
    if share_state:
        shared_host_symbols = (
            desync_syms
            | {h["symbol"] for h in circular_hazards}
            | {h["symbol"] for h in global_hazards}
        )

    needed_imports = find_relevant_imports(
        from_tree,
        from_lines,
        used_names - shared_host_symbols,
        source_path=from_path,
        project_root=root_abs,
        excluded_names=extracted_names_set | shared_host_symbols,
    )

    alias_hazard = _evaluate_alias_cycle(
        from_path, to_path, root_abs, needed_imports, to_file_existed,
        add_backward_compat_alias, shared_host_symbols, share_state,
        symbols, rel_from, rel_to
    )
    if alias_hazard:
        circular_hazards.append(alias_hazard)

    hazard_block = _check_hazard_blocks(
        framework_hazards, state_desync_hazards, circular_hazards, global_hazards,
        secret_warnings, safe_closure, allow_desync, allow_circular, allow_globals,
        share_state, share_state_hint
    )
    if hazard_block:
        return hazard_block

    extracted_code_blocks, line_ranges_to_remove = _extract_source_code_blocks(
        nodes_to_extract, from_lines, share_state, shared_host_symbols, desync_syms
    )

    to_module_name = os.path.splitext(os.path.basename(to_path))[0]
    extracted_symbol_names = list(dict.fromkeys(item[0] for item in nodes_to_extract))
    all_syms_str = ", ".join(f'"{s}"' for s in sorted(set(extracted_symbol_names)))
    sorted_dunder_all = f"__all__ = [{all_syms_str}]\n"

    shared_runtime_header = _build_shared_runtime_header_if_needed(
        share_state, shared_host_symbols, to_path, from_path, root_abs
    )
    existing_to_lines = _load_existing_to_lines(to_path, to_file_existed)

    new_to_source = _build_new_to_source(
        to_file_existed=to_file_existed,
        existing_to_lines=existing_to_lines,
        to_module_name=to_module_name,
        from_path=from_path,
        to_path=to_path,
        needed_imports=needed_imports,
        shared_runtime_header=shared_runtime_header,
        extracted_code_blocks=extracted_code_blocks,
        extracted_symbol_names=extracted_symbol_names,
        sorted_dunder_all=sorted_dunder_all,
    )

    import_target = compute_relative_import_path(from_path, to_path, project_root=root_abs)
    new_from_source, updated_from_lines = _build_updated_from_source(
        from_lines=from_lines,
        from_tree=from_tree,
        line_ranges_to_remove=line_ranges_to_remove,
        import_target=import_target,
        extracted_symbol_names=extracted_symbol_names,
        add_backward_compat_alias=add_backward_compat_alias,
        to_module_name=to_module_name,
        sorted_dunder_all=sorted_dunder_all,
    )

    syntax_err = _verify_ast_syntaxes(new_to_source, to_path, new_from_source, from_path)
    if syntax_err:
        return {"success": False, "error": syntax_err}

    pre_sources = {rel_from: from_source}
    if to_file_existed:
        pre_sources[rel_to] = "".join(existing_to_lines)
    post_sources = {rel_from: new_from_source, rel_to: new_to_source}
    ruff_gate = run_differential_ruff(pre_sources=pre_sources, post_sources=post_sources)

    combined_diff = _generate_diffs(
        rel_from, rel_to, from_lines, updated_from_lines,
        existing_to_lines, new_to_source, to_file_existed
    )

    test_result, rolled_back, error_msg = _execute_and_verify(
        run_tests, dry_run, to_path, new_to_source, from_path, new_from_source,
        from_source, existing_to_lines, to_file_existed, ruff_cleanup, root_abs
    )

    duration_ms = (time.perf_counter() - t_start) * 1000
    is_success = test_result is None or bool(test_result.get("passed", True))

    return _build_decouple_result_payload(
        is_success=is_success,
        extracted_symbol_names=extracted_symbol_names,
        rel_from=rel_from,
        rel_to=rel_to,
        import_target=import_target,
        created_new_module=not to_file_existed,
        shared_host_symbols=shared_host_symbols,
        share_state=share_state,
        duration_ms=duration_ms,
        combined_diff=combined_diff,
        ruff_gate=ruff_gate,
        test_result=test_result,
        rolled_back=rolled_back,
        circular_hazards=circular_hazards,
        global_hazards=global_hazards,
        state_desync_hazards=state_desync_hazards,
        framework_hazards=framework_hazards,
        secret_warnings=secret_warnings,
        safe_closure=safe_closure,
        error_msg=error_msg,
    )

