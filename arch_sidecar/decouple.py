import os
import ast
import sys
import difflib
import subprocess
import time
from typing import Dict, List, Optional, Set, Any, Tuple

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
        if isinstance(node.ctx, ast.Store):
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
        return self.loaded_names - self.bound_names

def get_free_variables_in_nodes(nodes: List[ast.AST]) -> Set[str]:
    analyzer = FreeVariableAnalyzer(nodes)
    for n in nodes:
        analyzer.visit(n)
    return analyzer.get_free_variables()

# Alias for backward compatibility
get_used_names_in_nodes = get_free_variables_in_nodes

def find_relevant_imports(source_tree: ast.Module, source_lines: List[str], used_names: Set[str]) -> List[str]:
    """
    Scans the imports in source_tree and extracts the exact lines of imports
    that define any of the names in used_names (plus `from __future__ import ...`
    compiler directives, which govern postponed evaluation of type annotations).
    """
    extracted_imports = []
    seen = set()

    for node in source_tree.body:
        if isinstance(node, ast.Import):
            # Check if any imported name or alias is used
            needed = False
            for alias in node.names:
                name_in_scope = alias.asname or alias.name.split(".")[0]
                if name_in_scope in used_names:
                    needed = True
                    break
            if needed:
                start = node.lineno - 1
                end = getattr(node, "end_lineno", node.lineno)
                stmt = "".join(source_lines[start:end]).strip()
                if stmt not in seen:
                    extracted_imports.append(stmt)
                    seen.add(stmt)

        elif isinstance(node, ast.ImportFrom):
            needed = (node.module == "__future__")
            if not needed:
                for alias in node.names:
                    name_in_scope = alias.asname or alias.name
                    if name_in_scope in used_names:
                        needed = True
                        break
            if needed:
                start = node.lineno - 1
                end = getattr(node, "end_lineno", node.lineno)
                stmt = "".join(source_lines[start:end]).strip()
                if stmt not in seen:
                    extracted_imports.append(stmt)
                    seen.add(stmt)

    return extracted_imports

import builtins
BUILTIN_NAMES = set(dir(builtins))

def detect_coupling_hazards(
    from_tree: ast.Module,
    extracted_symbols: Set[str],
    used_names: Set[str]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Analyzes whether extracted symbols reference remaining local functions/classes
    (circular import hazard) or module-level constants (unbound global state).
    """
    remaining_local_callables: Dict[str, str] = {}
    remaining_local_constants: Dict[str, str] = {}
    
    for node in from_tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name not in extracted_symbols:
                kind = "class" if isinstance(node, ast.ClassDef) else "function"
                remaining_local_callables[node.name] = kind
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id not in extracted_symbols:
                    remaining_local_constants[t.id] = "module_constant"
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id not in extracted_symbols:
                remaining_local_constants[node.target.id] = "module_constant"
                    
    circular_hazards = []
    global_hazards = []
    
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

    return circular_hazards, global_hazards


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
) -> Dict[str, Any]:
    """
    Deterministically decouples symbols from `from_file` into `to_file`.
    Preserves exact formatting and comments using surgical AST slicing.
    Automatically resolves needed dependencies/imports, inserts clean imports,
    maintains backwards compatibility aliases, and runs headless test gates.
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
    
    # Pre-flight check: detect circular dependency hazards and unbound global state
    extracted_names_set = {item[0] for item in nodes_to_extract}
    circular_hazards, global_hazards = detect_coupling_hazards(from_tree, extracted_names_set, used_names)
    safe_closure = compute_minimal_safe_closure(from_tree, symbols)
    
    if circular_hazards and not allow_circular:
        return {
            "success": False,
            "error": "Refactoring blocked: Circular import hazard detected.",
            "circular_hazards": circular_hazards,
            "global_state_hazards": global_hazards,
            "minimal_safe_closure": safe_closure,
            "remediation": [h["recommendation"] for h in circular_hazards] + [
                f"Minimal self-contained symbol closure: {','.join(safe_closure)}"
            ]
        }

    if global_hazards and not allow_globals:
        return {
            "success": False,
            "error": "Refactoring blocked: Unbound module global state / constant detected.",
            "circular_hazards": circular_hazards,
            "global_state_hazards": global_hazards,
            "minimal_safe_closure": safe_closure,
            "remediation": [h["recommendation"] for h in global_hazards] + [
                f"Minimal self-contained symbol closure: {','.join(safe_closure)}"
            ]
        }

    # Identify relevant imports from from_file
    needed_imports = find_relevant_imports(from_tree, from_lines, used_names)

    # Extract exact source lines for each extracted symbol
    extracted_code_blocks = []
    line_ranges_to_remove = []
    
    for sym_name, node, start_idx, end_idx in nodes_to_extract:
        # Include any leading comments or decorators immediately preceding the node
        actual_start = start_idx
        while actual_start > 0 and from_lines[actual_start - 1].strip().startswith(("#", "@")):
            actual_start -= 1
        
        block = "".join(from_lines[actual_start:end_idx]).rstrip()
        extracted_code_blocks.append(block)
        line_ranges_to_remove.append((actual_start, end_idx))

    # Determine module name for import in from_file
    to_module_name = os.path.splitext(os.path.basename(to_path))[0]
    
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
        new_to_content_parts.append("\n\n\n".join(extracted_code_blocks) + "\n")
    else:
        # Append missing imports and code blocks
        existing_to_text = "".join(existing_to_lines)
        missing_imports = [imp for imp in needed_imports if imp not in existing_to_text]
        if missing_imports:
            new_to_content_parts.append("\n".join(missing_imports) + "\n\n")
        new_to_content_parts.append(existing_to_text.rstrip() + "\n\n\n" + "\n\n\n".join(extracted_code_blocks) + "\n")

    new_to_source = "".join(new_to_content_parts)

    # Build updated from_file content
    # Remove extracted line ranges (sorted in reverse order so indices don't shift)
    sorted_ranges = sorted(line_ranges_to_remove, key=lambda x: x[0], reverse=True)
    updated_from_lines = list(from_lines)
    for start_r, end_r in sorted_ranges:
        # Replace the removed block with a clean blank line
        updated_from_lines[start_r:end_r] = ["\n"]

    # Locate where to insert the new import in from_file (after last top-level import)
    last_import_line = 0
    for node in from_tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            end = getattr(node, "end_lineno", node.lineno)
            if end > last_import_line:
                last_import_line = end

    extracted_symbol_names = list(dict.fromkeys(item[0] for item in nodes_to_extract))
    import_target = compute_relative_import_path(from_path, to_path, project_root=root_abs)
    import_stmt = f"from {import_target} import {', '.join(extracted_symbol_names)}\n"
    
    # Insert new import statement
    updated_from_lines.insert(last_import_line, import_stmt)

    # Add backwards-compatible alias if requested (so cli.SleepInhibitor is explicitly preserved)
    if add_backward_compat_alias:
        alias_block = [
            f"# Backward-compatibility aliases (decoupled into {to_module_name}.py)\n"
        ]
        for sym_name in extracted_symbol_names:
            alias_block.append(f"# {sym_name} = {to_module_name}.{sym_name}\n")
        # Place alias notice right after the import
        updated_from_lines.insert(last_import_line + 1, "".join(alias_block))

    new_from_source = "".join(updated_from_lines)

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

    # Generate unified diffs
    diff_from = "".join(difflib.unified_diff(
        from_lines,
        updated_from_lines,
        fromfile=f"a/{os.path.relpath(from_path, root_abs)}",
        tofile=f"b/{os.path.relpath(from_path, root_abs)}",
        n=3
    ))

    diff_to = "".join(difflib.unified_diff(
        existing_to_lines,
        new_to_source.splitlines(keepends=True),
        fromfile=f"a/{os.path.relpath(to_path, root_abs)}" if to_file_existed else "/dev/null",
        tofile=f"b/{os.path.relpath(to_path, root_abs)}",
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
        "from_file": os.path.relpath(from_path, root_abs),
        "to_file": os.path.relpath(to_path, root_abs),
        "import_target": import_target,
        "created_new_module": not to_file_existed,
        "execution_time_ms": round(duration_ms, 2),
        "diff": combined_diff,
        "test_verification": test_result,
        "rolled_back": rolled_back,
        "circular_hazards": circular_hazards,
        "global_state_hazards": global_hazards,
        "minimal_safe_closure": safe_closure,
    }
    if error_msg:
        result_payload["error"] = error_msg
    return result_payload
