"""
TypeScript and JavaScript AST & Dependency Analysis Engine for Devrith.

Provides:
1. High-fidelity lexical AST parsing for TS, TSX, JS, and JSX files.
2. Cross-module import graph resolution with tsconfig.json path alias support.
3. Circular dependency detection with runtime vs. type-only distinction.
4. Rich architectural skeleton formatting for repository digests.
"""

import os
import re
import json
from typing import Dict, List, Set, Optional, Tuple, Any


def strip_js_comments(code: str) -> str:
    """
    Strips single-line and multi-line comments from JavaScript/TypeScript code
    while safely preserving string literals ('...', "...", `...`).
    """
    output: List[str] = []
    i = 0
    n = len(code)
    
    in_single_quote = False
    in_double_quote = False
    in_template_literal = False
    in_regex = False
    
    while i < n:
        c = code[i]
        
        # Check escape sequence inside string
        if (in_single_quote or in_double_quote or in_template_literal) and c == "\\":
            output.append(c)
            if i + 1 < n:
                output.append(code[i + 1])
                i += 2
                continue
            i += 1
            continue

        # Handle strings
        if not in_double_quote and not in_template_literal and c == "'":
            in_single_quote = not in_single_quote
            output.append(c)
            i += 1
            continue
        elif not in_single_quote and not in_template_literal and c == '"':
            in_double_quote = not in_double_quote
            output.append(c)
            i += 1
            continue
        elif not in_single_quote and not in_double_quote and c == "`":
            in_template_literal = not in_template_literal
            output.append(c)
            i += 1
            continue
            
        # Outside strings: check comments
        if not in_single_quote and not in_double_quote and not in_template_literal:
            # Single-line comment
            if c == "/" and i + 1 < n and code[i + 1] == "/":
                # Skip to end of line
                newline_pos = code.find("\n", i)
                if newline_pos == -1:
                    break
                output.append("\n")
                i = newline_pos + 1
                continue
            # Multi-line comment
            elif c == "/" and i + 1 < n and code[i + 1] == "*":
                end_pos = code.find("*/", i + 2)
                if end_pos == -1:
                    break
                # Preserve newline count for line numbering
                comment_newlines = code[i:end_pos + 2].count("\n")
                output.append("\n" * comment_newlines)
                i = end_pos + 2
                continue

        output.append(c)
        i += 1

    return "".join(output)


def parse_ts_js_ast(source_text: str, filename: str = "") -> Dict[str, Any]:
    """
    Analyzes TypeScript/JavaScript code and extracts:
    - Imports (module specifier, imported names, default/namespace, is_type_only, line)
    - Re-exports (`export * from ...`, `export { ... } from ...`)
    - Declarations (interfaces, type aliases, classes, functions, enums, constants)
    """
    clean_code = strip_js_comments(source_text)
    lines = clean_code.splitlines()
    
    imports: List[Dict[str, Any]] = []
    re_exports: List[Dict[str, Any]] = []
    declarations: List[Dict[str, Any]] = []

    # Regex for ES imports
    # import [type] { a, b as c } from 'module'
    # import [type] DefaultName from 'module'
    # import * as ns from 'module'
    # import 'module'
    import_regex = re.compile(
        r"import\s+(?:(type)\s+)?(?:([a-zA-Z0-9_$\s{},*]+)\s+from\s+)?['\"]([^'\"]+)['\"]",
        re.MULTILINE
    )

    # Require regex: const x = require('module')
    require_regex = re.compile(
        r"(?:const|let|var)\s+([a-zA-Z0-9_$\s{},:]+)\s*=\s*require\(\s*['\"]([^'\"]+)['\"]\s*\)",
        re.MULTILINE
    )

    # Re-export regex: export [type] { a, b } from 'module' or export * from 'module'
    reexport_regex = re.compile(
        r"export\s+(?:(type)\s+)?(?:(\*|\{[^}]+\}))(?:\s+as\s+([a-zA-Z0-9_$]+))?\s+from\s+['\"]([^'\"]+)['\"]",
        re.MULTILINE
    )

    # Find line numbers by searching in original text
    for match in import_regex.finditer(clean_code):
        is_type = bool(match.group(1))
        clause = match.group(2) or ""
        module_specifier = match.group(3)
        start_pos = match.start()
        line_num = clean_code[:start_pos].count("\n") + 1

        names: List[str] = []
        is_default = False
        is_namespace = False

        clause_clean = clause.strip()
        if not is_type and "{" in clause_clean:
            brace_content = clause_clean[clause_clean.find("{") + 1 : clause_clean.rfind("}")]
            items = [item.strip() for item in brace_content.split(",") if item.strip()]
            if items and all(item.startswith("type ") for item in items):
                is_type = True

        if "* as " in clause_clean:
            is_namespace = True
            ns_name = clause_clean.split("* as ")[-1].strip()
            names.append(ns_name)
        elif "{" in clause_clean:
            # Destructured
            brace_content = clause_clean[clause_clean.find("{") + 1 : clause_clean.rfind("}")]
            for item in brace_content.split(","):
                item_s = item.strip().removeprefix("type ").strip()
                if item_s:
                    names.append(item_s.split(" as ")[0].strip())
            # Check if there's a default import before `{`
            before_brace = clause_clean[: clause_clean.find("{")].strip().rstrip(",")
            if before_brace:
                names.append(before_brace.strip())
                is_default = True
        elif clause_clean:
            names.append(clause_clean)
            is_default = True

        imports.append({
            "module": module_specifier,
            "names": names,
            "is_type_only": is_type,
            "is_default": is_default,
            "is_namespace": is_namespace,
            "line": line_num,
        })

    # CommonJS require
    for match in require_regex.finditer(clean_code):
        clause = match.group(1).strip()
        module_specifier = match.group(2).strip()
        start_pos = match.start()
        line_num = clean_code[:start_pos].count("\n") + 1
        imports.append({
            "module": module_specifier,
            "names": [clause],
            "is_type_only": False,
            "is_default": True,
            "is_namespace": False,
            "line": line_num,
        })

    # Re-exports
    for match in reexport_regex.finditer(clean_code):
        is_type = bool(match.group(1))
        clause = match.group(2).strip()
        alias = match.group(3)
        module_specifier = match.group(4)
        start_pos = match.start()
        line_num = clean_code[:start_pos].count("\n") + 1

        re_exports.append({
            "module": module_specifier,
            "clause": clause,
            "alias": alias,
            "is_type_only": is_type,
            "line": line_num,
        })

    # Declarations: Interfaces, Types, Classes, Functions, Enums, Constants
    # Only match module-level declarations (brace_depth == 0) or explicitly exported items.
    decl_patterns = [
        ("interface", re.compile(r"^(export\s+)?(?:default\s+)?interface\s+([a-zA-Z0-9_$]+)(?:<[^>]+>)?(?:\s+extends\s+[^{]+)?")),
        ("type", re.compile(r"^(export\s+)?type\s+([a-zA-Z0-9_$]+)(?:<[^>]+>)?\s*=")),
        ("class", re.compile(r"^(export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([a-zA-Z0-9_$]+)(?:<[^>]+>)?(?:\s+extends\s+[a-zA-Z0-9_$.]+)?(?:\s+implements\s+[^{]+)?")),
        ("enum", re.compile(r"^(export\s+)?(?:const\s+)?enum\s+([a-zA-Z0-9_$]+)")),
        ("function", re.compile(r"^(export\s+)?(?:default\s+)?(?:async\s+)?function\s+([a-zA-Z0-9_$]+)(?:<[^>]+>)?\s*\(([^)]*)\)")),
        ("const", re.compile(r"^(export\s+)?(?:const|let|var)\s+([a-zA-Z0-9_$]+)(?:\s*:\s*([^=;]+))?\s*=")),
    ]

    method_pat = re.compile(r"^(?:public|private|protected|static|async|\s)*([a-zA-Z0-9_$]+)\s*\(([^)]*)\)\s*(?::\s*([^{]+))?\s*\{")

    current_brace_depth = 0
    current_class: Optional[str] = None
    class_brace_depth = 0

    for line_idx, line in enumerate(lines):
        line_stripped = line.strip()
        if not line_stripped:
            continue

        # Check top-level or exported declarations
        is_at_module_level = (current_brace_depth == 0)
        has_export = line_stripped.startswith("export ") or line_stripped.startswith("export default ")

        matched = False
        if is_at_module_level or has_export:
            for kind, pat in decl_patterns:
                m = pat.match(line_stripped)
                if m:
                    is_exported = bool(m.group(1)) or "export default" in line_stripped
                    name = m.group(2)
                    sig = line_stripped.split("{")[0].strip().rstrip(";")
                    brace_count = 0
                    found_brace = False
                    end_idx = line_idx
                    for j in range(line_idx, len(lines)):
                        l = lines[j]
                        brace_count += l.count("{") - l.count("}")
                        if "{" in l:
                            found_brace = True
                        if found_brace and brace_count <= 0:
                            end_idx = j
                            break
                        if not found_brace and ";" in l:
                            end_idx = j
                            break
                    decl_code = "\n".join(lines[line_idx : end_idx + 1])
                    declarations.append({
                        "kind": kind,
                        "name": name,
                        "is_exported": is_exported,
                        "signature": sig,
                        "line": line_idx + 1,
                        "start_line": line_idx + 1,
                        "end_line": end_idx + 1,
                        "code": decl_code,
                    })
                    if kind == "class":
                        current_class = name
                        class_brace_depth = current_brace_depth + 1
                    matched = True
                    break

        # Check class methods inside class body
        if not matched and current_class and current_brace_depth == class_brace_depth:
            # Check for method definition
            m_meth = method_pat.match(line_stripped)
            if m_meth and not line_stripped.startswith(("if", "for", "while", "switch", "catch")):
                meth_name = m_meth.group(1)
                if meth_name not in ("constructor", "if", "for", "while", "return"):
                    sig = line_stripped.split("{")[0].strip()
                    brace_count = 0
                    found_brace = False
                    end_idx = line_idx
                    for j in range(line_idx, len(lines)):
                        l = lines[j]
                        brace_count += l.count("{") - l.count("}")
                        if "{" in l:
                            found_brace = True
                        if found_brace and brace_count <= 0:
                            end_idx = j
                            break
                    declarations.append({
                        "kind": "method",
                        "name": f"{current_class}.{meth_name}",
                        "is_exported": True,
                        "signature": sig,
                        "line": line_idx + 1,
                        "start_line": line_idx + 1,
                        "end_line": end_idx + 1,
                        "code": "\n".join(lines[line_idx : end_idx + 1]),
                    })

        # Update brace depth
        open_b = line.count("{")
        close_b = line.count("}")
        current_brace_depth += (open_b - close_b)
        if current_class and current_brace_depth < class_brace_depth:
            current_class = None

    return {
        "filename": filename,
        "imports": imports,
        "re_exports": re_exports,
        "declarations": declarations,
        "total_lines": len(lines),
    }


def find_tsconfig(start_dir: str) -> Optional[Dict[str, Any]]:
    """Looks upward for tsconfig.json or jsconfig.json."""
    curr = os.path.abspath(start_dir)
    while True:
        for fname in ("tsconfig.json", "jsconfig.json"):
            path = os.path.join(curr, fname)
            if os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        # Strip simple trailing commas / comments if any
                        content = f.read()
                        clean = re.sub(r"//.*$", "", content, flags=re.MULTILINE)
                        clean = re.sub(r"/\*.*?\*/", "", clean, flags=re.DOTALL)
                        return {"path": path, "config": json.loads(clean), "dir": curr}
                except Exception:
                    pass
        parent = os.path.dirname(curr)
        if parent == curr:
            break
        curr = parent
    return None


def resolve_ts_import_path(
    module_specifier: str,
    source_file: str,
    project_root: str,
    tsconfig: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """
    Resolves a TypeScript/JavaScript module specifier to an absolute file path.
    Supports:
    - Relative imports: `./foo`, `../bar`, `./components`
    - NodeNext/ESM imports: `./foo.js` -> `./foo.ts` / `./foo.tsx`
    - Path aliases: `@/components/*` via tsconfig.json
    - Extension resolutions: `.ts`, `.tsx`, `.d.ts`, `.js`, `.jsx`, `/index.ts`, etc.
    Returns None for external npm packages (e.g. 'react', 'lodash').
    """
    if not module_specifier:
        return None

    candidate_extensions = [
        "", ".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs",
        "/index.ts", "/index.tsx", "/index.d.ts", "/index.js", "/index.jsx"
    ]

    def _try_resolve(base_path: str) -> Optional[str]:
        bases = [base_path]
        if base_path.endswith((".js", ".jsx", ".mjs", ".cjs")):
            stripped = re.sub(r"\.(?:js|jsx|mjs|cjs)$", "", base_path)
            if stripped not in bases:
                bases.append(stripped)

        for b in bases:
            for ext in candidate_extensions:
                full_p = b + ext
                if os.path.isfile(full_p):
                    return os.path.normpath(full_p)
        return None

    # 1. Handle Path Aliases from tsconfig.json
    if tsconfig and not module_specifier.startswith("."):
        compiler_opts = tsconfig.get("config", {}).get("compilerOptions", {})
        base_url = compiler_opts.get("baseUrl", ".")
        base_dir = os.path.normpath(os.path.join(tsconfig["dir"], base_url))
        paths = compiler_opts.get("paths", {})

        for pattern, targets in paths.items():
            # Pattern e.g. "@/*" or "components/*"
            pattern_prefix = pattern.rstrip("*")
            if module_specifier.startswith(pattern_prefix) and targets:
                sub_path = module_specifier[len(pattern_prefix):]
                for target_tpl in targets:
                    target_prefix = target_tpl.rstrip("*")
                    candidate_base = os.path.normpath(os.path.join(base_dir, target_prefix + sub_path))
                    resolved = _try_resolve(candidate_base)
                    if resolved:
                        return resolved

        # Direct baseUrl resolution
        if base_url and base_url != ".":
            candidate_base = os.path.normpath(os.path.join(base_dir, module_specifier))
            resolved = _try_resolve(candidate_base)
            if resolved:
                return resolved

    # 2. Handle Relative Imports
    if module_specifier.startswith("."):
        source_dir = os.path.dirname(os.path.abspath(source_file))
        base_path = os.path.normpath(os.path.join(source_dir, module_specifier))
        resolved = _try_resolve(base_path)
        if resolved:
            return resolved

    return None


def detect_ts_circular_imports(
    target_file: str,
    project_root: str,
    max_depth: int = 15,
) -> List[Dict[str, Any]]:
    """
    Traces the import graph starting from `target_file` and identifies circular cycles.
    Distinguishes between:
    - RUNTIME_CYCLE: cycle contains only runtime value imports (HIGH HAZARD: TDZ / undefined crashes).
    - TYPE_ONLY_CYCLE: cycle contains at least one `import type` (LOW HAZARD: erased at compile time).
    """
    target_abs = os.path.abspath(target_file)
    project_abs = os.path.abspath(project_root)
    tsconfig = find_tsconfig(project_abs)

    cycles: List[Dict[str, Any]] = []
    seen_cycle_keys: Set[str] = set()
    file_ast_cache: Dict[str, Dict[str, Any]] = {}

    def get_parsed_file(fpath: str) -> Optional[Dict[str, Any]]:
        if fpath not in file_ast_cache:
            if not os.path.isfile(fpath):
                return None
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                file_ast_cache[fpath] = parse_ts_js_ast(content, filename=fpath)
            except Exception:
                return None
        return file_ast_cache[fpath]

    # 3-color DFS Cycle Detection
    gray_stack: List[str] = []
    black_set: Set[str] = set()
    edge_types: List[bool] = []  # True if edge is type_only

    def dfs(curr_file: str, depth: int) -> None:
        if depth > max_depth:
            return

        parsed = get_parsed_file(curr_file)
        if not parsed:
            return

        gray_stack.append(curr_file)

        # Traverse imports and re-exports
        all_edges = []
        for imp in parsed["imports"]:
            all_edges.append((imp["module"], imp.get("is_type_only", False), imp.get("line", 1)))
        for re_exp in parsed["re_exports"]:
            all_edges.append((re_exp["module"], re_exp.get("is_type_only", False), re_exp.get("line", 1)))

        for mod_spec, is_type, line_num in all_edges:
            resolved = resolve_ts_import_path(mod_spec, curr_file, project_abs, tsconfig)
            if not resolved or not resolved.startswith(project_abs):
                continue
            # Skip node_modules directories
            path_parts = resolved.replace("\\", "/").split("/")
            if "node_modules" in path_parts:
                continue

            edge_types.append(is_type)

            if resolved in gray_stack:
                cycle_start = gray_stack.index(resolved)
                cycle_path = gray_stack[cycle_start:] + [resolved]
                cycle_edges = edge_types[cycle_start:]

                # A cycle is broken at runtime if ANY edge is type-only!
                # It is a RUNTIME_CYCLE (TDZ hazard) if and only if EVERY edge is a runtime value import.
                is_runtime_cycle = not any(cycle_edges)
                is_type_only = not is_runtime_cycle

                rel_cycle = [os.path.relpath(p, project_abs) for p in cycle_path]
                cycle_str = " -> ".join(rel_cycle)

                if cycle_str not in seen_cycle_keys:
                    seen_cycle_keys.add(cycle_str)
                    cycles.append({
                        "type": "TYPE_ONLY_CYCLE" if is_type_only else "RUNTIME_CYCLE",
                        "severity": "WARNING" if is_type_only else "ERROR",
                        "path": rel_cycle,
                        "length": len(rel_cycle) - 1,
                        "message": (
                            f"Circular import cycle ({cycle_str}): "
                            + ("Erased at compile time via type-only import (safe at runtime)."
                               if is_type_only 
                               else "Runtime values imported in cycle: Temporal Dead Zone / undefined hazard at runtime.")
                        )
                    })
            elif resolved not in black_set:
                dfs(resolved, depth + 1)

            edge_types.pop()

        gray_stack.pop()
        black_set.add(curr_file)

    dfs(target_abs, 0)
    return cycles


def format_ts_digest_skeleton(filepath: str) -> str:
    """
    Produces a concise, high-density architectural skeleton of a TS/JS file
    for `arch-sidecar digest`.
    """
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            code = f.read()
    except Exception as e:
        return f"  [Error reading: {e}]\n"

    parsed = parse_ts_js_ast(code, filename=filepath)
    summary: List[str] = []

    # Imports summary
    if parsed["imports"]:
        import_strs = []
        for imp in parsed["imports"]:
            type_prefix = "type " if imp["is_type_only"] else ""
            names = ", ".join(imp["names"][:4]) + ("..." if len(imp["names"]) > 4 else "")
            import_strs.append(f"from '{imp['module']}' import {type_prefix}{names}")
        summary.append(f"  Imports: {', '.join(import_strs[:8])}")

    # Re-exports summary (Barrel files)
    if parsed["re_exports"]:
        re_strs = []
        for r in parsed["re_exports"]:
            type_prefix = "type " if r["is_type_only"] else ""
            re_strs.append(f"export {type_prefix}{r['clause']} from '{r['module']}'")
        summary.append(f"  Re-exports: {', '.join(re_strs[:6])}")

    # Declarations
    for decl in parsed["declarations"][:25]:
        export_prefix = "export " if (decl["is_exported"] and not decl["signature"].startswith("export")) else ""
        summary.append(f"  {export_prefix}{decl['signature']}")

    if len(parsed["declarations"]) > 25:
        summary.append(f"  ... ({len(parsed['declarations']) - 25} more declarations)")

    return "\n".join(summary) + "\n"
