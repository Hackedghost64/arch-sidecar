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
import logging
from typing import Dict, List, Set, Optional, Tuple, Any

logger = logging.getLogger(__name__)

IMPORT_REGEX = re.compile(
    r"import\s+(?:(type)\s+)?(?:([a-zA-Z0-9_$\s{},*]+)\s+from\s+)?['\"]([^'\"]+)['\"]",
    re.MULTILINE,
)

REQUIRE_REGEX = re.compile(
    r"(?:const|let|var)\s+([a-zA-Z0-9_$\s{},:]+)\s*=\s*require\(\s*['\"]([^'\"]+)['\"]\s*\)",
    re.MULTILINE,
)

REEXPORT_REGEX = re.compile(
    r"export\s+(?:(type)\s+)?(?:(\*|\{[^}]+\}))(?:\s+as\s+([a-zA-Z0-9_$]+))?\s+from\s+['\"]([^'\"]+)['\"]",
    re.MULTILINE,
)

TS_DECL_PATTERNS = [
    ("interface", re.compile(r"^(export\s+)?(?:default\s+)?interface\s+([a-zA-Z0-9_$]+)(?:<[^>]+>)?(?:\s+extends\s+[^{]+)?")),
    ("type", re.compile(r"^(export\s+)?type\s+([a-zA-Z0-9_$]+)(?:<[^>]+>)?\s*=")),
    ("class", re.compile(r"^(export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([a-zA-Z0-9_$]+)(?:<[^>]+>)?(?:\s+extends\s+[a-zA-Z0-9_$.]+)?(?:\s+implements\s+[^{]+)?")),
    ("enum", re.compile(r"^(export\s+)?(?:const\s+)?enum\s+([a-zA-Z0-9_$]+)")),
    ("function", re.compile(r"^(export\s+)?(?:default\s+)?(?:async\s+)?function\s+([a-zA-Z0-9_$]+)(?:<[^>]+>)?\s*\(([^)]*)\)")),
    ("const", re.compile(r"^(export\s+)?(?:const|let|var)\s+([a-zA-Z0-9_$]+)(?:\s*:\s*([^=;]+))?\s*=")),
]

TS_METHOD_PAT = re.compile(r"^(?:public|private|protected|static|async|\s)*([a-zA-Z0-9_$]+)\s*\(([^)]*)\)\s*(?::\s*([^{]+))?\s*\{")
TS_GETTER_SETTER_PAT = re.compile(r"^(?:public|private|protected|static|\s)*(get|set)\s+([a-zA-Z0-9_$]+)\s*(?:\([^)]*\))?\s*(?::\s*([^{]+))?\s*\{")

TS_CANDIDATE_EXTENSIONS = (
    "", ".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs",
    "/index.ts", "/index.tsx", "/index.d.ts", "/index.js", "/index.jsx",
)


def _handle_js_escape(code: str, i: int, n: int) -> Tuple[str, int]:
    """Extracts escaped character inside a string or template literal."""
    if i + 1 < n:
        return code[i : i + 2], i + 2
    return code[i], i + 1


def _consume_js_comment(code: str, i: int, n: int) -> Tuple[Optional[str], int]:
    """Extracts comment replacement and advanced index, or (None, i) if not a comment."""
    if i + 1 >= n or code[i] != "/":
        return None, i
    next_c = code[i + 1]
    if next_c == "/":
        newline_pos = code.find("\n", i)
        if newline_pos == -1:
            return "", n
        return "\n", newline_pos + 1
    if next_c == "*":
        end_pos = code.find("*/", i + 2)
        if end_pos == -1:
            return "", n
        newlines = code[i : end_pos + 2].count("\n")
        return "\n" * newlines, end_pos + 2
    return None, i


def _handle_js_quote(
    code: str, i: int, n: int, quote_char: str
) -> Tuple[str, int, Optional[str]]:
    """Handles parsing while inside a JS/TS quote delimiter."""
    c = code[i]
    if c == "\\":
        chunk, next_i = _handle_js_escape(code, i, n)
        return chunk, next_i, quote_char
    new_quote = None if c == quote_char else quote_char
    return c, i + 1, new_quote


def strip_js_comments(code: str) -> str:
    """
    Strips single-line and multi-line comments from JavaScript/TypeScript code
    while safely preserving string literals ('...', "...", `...`).
    """
    output: List[str] = []
    i = 0
    n = len(code)
    quote_char: Optional[str] = None

    while i < n:
        c = code[i]
        if quote_char is not None:
            chunk, i, quote_char = _handle_js_quote(code, i, n, quote_char)
            output.append(chunk)
            continue

        rep, next_i = _consume_js_comment(code, i, n)
        if rep is not None:
            output.append(rep)
            i = next_i
            continue

        if c in ("'", '"', "`"):
            quote_char = c

        output.append(c)
        i += 1

    return "".join(output)


def _is_all_type_items(brace_content: str) -> bool:
    """Checks whether all items inside a destructured import have a type prefix."""
    items = [item.strip() for item in brace_content.split(",") if item.strip()]
    return bool(items) and all(item.startswith("type ") for item in items)


def _parse_destructured_items(brace_content: str) -> List[str]:
    """Extracts imported symbol names from inside an import brace block."""
    names: List[str] = []
    for item in brace_content.split(","):
        item_s = item.strip().removeprefix("type ").strip()
        if item_s:
            names.append(item_s.split(" as ")[0].strip())
    return names


def _parse_import_clause(
    clause_clean: str, is_type: bool
) -> Tuple[List[str], bool, bool, bool]:
    """Parses the clause between 'import' and 'from' into extracted names and flags."""
    if not clause_clean:
        return [], is_type, False, False

    if "* as " in clause_clean:
        ns_name = clause_clean.split("* as ")[-1].strip()
        return [ns_name], is_type, False, True

    if "{" in clause_clean:
        brace_content = clause_clean[clause_clean.find("{") + 1 : clause_clean.rfind("}")]
        if not is_type and _is_all_type_items(brace_content):
            is_type = True
        names = _parse_destructured_items(brace_content)
        before_brace = clause_clean[: clause_clean.find("{")].strip().rstrip(",")
        is_default = False
        if before_brace:
            names.append(before_brace.strip())
            is_default = True
        return names, is_type, is_default, False

    return [clause_clean], is_type, True, False


def _extract_ts_imports(clean_code: str) -> List[Dict[str, Any]]:
    """Extracts all ES import and CommonJS require statements."""
    imports: List[Dict[str, Any]] = []

    for match in IMPORT_REGEX.finditer(clean_code):
        is_type = bool(match.group(1))
        clause = (match.group(2) or "").strip()
        mod_spec = match.group(3)
        line_num = clean_code[: match.start()].count("\n") + 1

        names, is_type_res, is_def, is_ns = _parse_import_clause(clause, is_type)
        imports.append({
            "module": mod_spec,
            "names": names,
            "is_type_only": is_type_res,
            "is_default": is_def,
            "is_namespace": is_ns,
            "line": line_num,
        })

    for match in REQUIRE_REGEX.finditer(clean_code):
        clause = match.group(1).strip()
        mod_spec = match.group(2).strip()
        line_num = clean_code[: match.start()].count("\n") + 1
        imports.append({
            "module": mod_spec,
            "names": [clause],
            "is_type_only": False,
            "is_default": True,
            "is_namespace": False,
            "line": line_num,
        })

    return imports


def _extract_ts_reexports(clean_code: str) -> List[Dict[str, Any]]:
    """Extracts barrel re-export declarations from cleaned code."""
    re_exports: List[Dict[str, Any]] = []
    for match in REEXPORT_REGEX.finditer(clean_code):
        line_num = clean_code[: match.start()].count("\n") + 1
        re_exports.append({
            "module": match.group(4),
            "clause": match.group(2).strip(),
            "alias": match.group(3),
            "is_type_only": bool(match.group(1)),
            "line": line_num,
        })
    return re_exports


def _find_ts_decl_end(lines: List[str], start_idx: int) -> int:
    """Finds ending line index for a TypeScript declaration block."""
    brace_count = 0
    found_brace = False
    for j in range(start_idx, len(lines)):
        l = lines[j]
        brace_count += l.count("{") - l.count("}")
        if "{" in l:
            found_brace = True
        if found_brace and brace_count <= 0:
            return j
        if not found_brace and ";" in l:
            return j
    return start_idx


def _match_ts_decl(stripped: str) -> Optional[Tuple[str, str, bool]]:
    """Matches a line against top-level declaration patterns."""
    for kind, pat in TS_DECL_PATTERNS:
        m = pat.match(stripped)
        if m:
            is_exported = bool(m.group(1)) or "export default" in stripped
            name = m.group(2)
            return kind, name, is_exported
    return None


def _match_ts_class_member(stripped: str) -> Optional[Tuple[str, str]]:
    """Matches class method, getter, or setter signatures."""
    if stripped.startswith(("if", "for", "while", "switch", "catch", "return")):
        return None
    m_gs = TS_GETTER_SETTER_PAT.match(stripped)
    if m_gs:
        kind = "getter" if m_gs.group(1) == "get" else "setter"
        raw_meth = m_gs.group(2)
        if raw_meth not in ("constructor", "if", "for", "while", "return"):
            return kind, raw_meth
        return None
    m_meth = TS_METHOD_PAT.match(stripped)
    if m_meth:
        raw_meth = m_meth.group(1)
        if raw_meth not in ("constructor", "if", "for", "while", "return"):
            return "method", raw_meth
    return None


def _build_ts_declaration(
    lines: List[str],
    line_idx: int,
    stripped: str,
    decl_info: Tuple[str, str, bool],
) -> Dict[str, Any]:
    """Constructs a declaration record dictionary."""
    kind, name, is_exported = decl_info
    end_idx = _find_ts_decl_end(lines, line_idx)
    sig = stripped.split("{")[0].strip().rstrip(";")
    decl_code = "\n".join(lines[line_idx : end_idx + 1])
    return {
        "kind": kind,
        "name": name,
        "is_exported": is_exported,
        "signature": sig,
        "line": line_idx + 1,
        "start_line": line_idx + 1,
        "end_line": end_idx + 1,
        "code": decl_code,
    }


def _try_extract_top_decl(
    lines: List[str], line_idx: int, stripped: str, at_module: bool
) -> Optional[Tuple[Dict[str, Any], Optional[str]]]:
    """Extracts top-level or exported symbol if pattern matches."""
    has_export = stripped.startswith(("export ", "export default "))
    if not at_module and not has_export:
        return None
    decl_info = _match_ts_decl(stripped)
    if not decl_info:
        return None
    decl = _build_ts_declaration(lines, line_idx, stripped, decl_info)
    cls_name = decl_info[1] if decl_info[0] == "class" else None
    return decl, cls_name


def _try_extract_class_member(
    lines: List[str], line_idx: int, stripped: str, current_class: str
) -> Optional[Dict[str, Any]]:
    """Extracts class member method or getter/setter."""
    member = _match_ts_class_member(stripped)
    if not member:
        return None
    member_info = (member[0], f"{current_class}.{member[1]}", True)
    return _build_ts_declaration(lines, line_idx, stripped, member_info)


def _is_inside_class(
    current_class: Optional[str],
    current_brace_depth: int,
    class_brace_depth: int,
) -> bool:
    """Checks whether current parsing position is directly inside class body."""
    return current_class is not None and current_brace_depth == class_brace_depth


def _prune_class_context(
    current_class: Optional[str],
    current_depth: int,
    class_depth: int,
) -> Optional[str]:
    """Clears active class name once brace depth closes below class scope."""
    if current_class and current_depth < class_depth:
        return None
    return current_class


def _extract_ts_declarations(lines: List[str]) -> List[Dict[str, Any]]:
    """Extracts all top-level declarations and class methods from TS source lines."""
    declarations: List[Dict[str, Any]] = []
    current_brace_depth = 0
    current_class: Optional[str] = None
    class_brace_depth = 0

    for line_idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        top_res = _try_extract_top_decl(lines, line_idx, stripped, current_brace_depth == 0)
        if top_res:
            decl, new_cls = top_res
            declarations.append(decl)
            if new_cls:
                current_class, class_brace_depth = new_cls, current_brace_depth + 1
        elif _is_inside_class(current_class, current_brace_depth, class_brace_depth):
            assert current_class is not None
            m_decl = _try_extract_class_member(lines, line_idx, stripped, current_class)
            if m_decl:
                declarations.append(m_decl)

        current_brace_depth += line.count("{") - line.count("}")
        current_class = _prune_class_context(
            current_class, current_brace_depth, class_brace_depth
        )

    return declarations


def parse_ts_js_ast(source_text: str, filename: str = "") -> Dict[str, Any]:
    """
    Analyzes TypeScript/JavaScript code and extracts:
    - Imports (module specifier, imported names, default/namespace, is_type_only, line)
    - Re-exports (`export * from ...`, `export { ... } from ...`)
    - Declarations (interfaces, type aliases, classes, functions, enums, constants)
    """
    clean_code = strip_js_comments(source_text)
    lines = clean_code.splitlines()

    return {
        "filename": filename,
        "imports": _extract_ts_imports(clean_code),
        "re_exports": _extract_ts_reexports(clean_code),
        "declarations": _extract_ts_declarations(lines),
        "total_lines": len(lines),
    }


def _load_tsconfig_file(path: str, curr_dir: str) -> Optional[Dict[str, Any]]:
    """Attempts to read and parse a tsconfig/jsconfig JSON file."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        clean = re.sub(r"//.*$", "", content, flags=re.MULTILINE)
        clean = re.sub(r"/\*.*?\*/", "", clean, flags=re.DOTALL)
        return {"path": path, "config": json.loads(clean), "dir": curr_dir}
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("Failed to parse tsconfig at %s: %s", path, e)
        return None


def _check_tsconfig_in_dir(curr: str) -> Optional[Dict[str, Any]]:
    """Checks for tsconfig.json or jsconfig.json in a directory."""
    for fname in ("tsconfig.json", "jsconfig.json"):
        cfg = _load_tsconfig_file(os.path.join(curr, fname), curr)
        if cfg:
            return cfg
    return None


def find_tsconfig(start_dir: str) -> Optional[Dict[str, Any]]:
    """Looks upward for tsconfig.json or jsconfig.json."""
    curr = os.path.abspath(start_dir)
    while True:
        cfg = _check_tsconfig_in_dir(curr)
        if cfg:
            return cfg
        parent = os.path.dirname(curr)
        if parent == curr:
            break
        curr = parent
    return None


def _try_resolve_ts_path(base_path: str) -> Optional[str]:
    """Tries resolving a candidate base path across TypeScript/JS extensions."""
    bases = [base_path]
    if base_path.endswith((".js", ".jsx", ".mjs", ".cjs")):
        stripped = re.sub(r"\.(?:js|jsx|mjs|cjs)$", "", base_path)
        if stripped not in bases:
            bases.append(stripped)

    for b in bases:
        for ext in TS_CANDIDATE_EXTENSIONS:
            full_p = b + ext
            if os.path.isfile(full_p):
                return os.path.normpath(full_p)
    return None


def _resolve_tsconfig_pattern(
    mod_spec: str,
    pattern: str,
    targets: List[str],
    base_dir: str,
) -> Optional[str]:
    """Resolves an aliased path according to a tsconfig paths mapping."""
    prefix = pattern.rstrip("*")
    if not mod_spec.startswith(prefix) or not targets:
        return None
    sub = mod_spec[len(prefix) :]
    for target_tpl in targets:
        target_prefix = target_tpl.rstrip("*")
        candidate = os.path.normpath(os.path.join(base_dir, target_prefix + sub))
        resolved = _try_resolve_ts_path(candidate)
        if resolved:
            return resolved
    return None


def _resolve_ts_alias_path(mod_spec: str, tsconfig: Dict[str, Any]) -> Optional[str]:
    """Resolves module specifier using tsconfig compilerOptions (paths and baseUrl)."""
    compiler_opts = tsconfig.get("config", {}).get("compilerOptions", {})
    base_url = compiler_opts.get("baseUrl", ".")
    base_dir = os.path.normpath(os.path.join(tsconfig["dir"], base_url))
    paths = compiler_opts.get("paths", {})

    for pattern, targets in paths.items():
        resolved = _resolve_tsconfig_pattern(mod_spec, pattern, targets, base_dir)
        if resolved:
            return resolved

    if base_url and base_url != ".":
        candidate = os.path.normpath(os.path.join(base_dir, mod_spec))
        return _try_resolve_ts_path(candidate)
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

    if tsconfig and not module_specifier.startswith("."):
        resolved = _resolve_ts_alias_path(module_specifier, tsconfig)
        if resolved:
            return resolved

    if module_specifier.startswith("."):
        source_dir = os.path.dirname(os.path.abspath(source_file))
        base_path = os.path.normpath(os.path.join(source_dir, module_specifier))
        return _try_resolve_ts_path(base_path)

    return None


def _format_cycle_record(
    cycle_str: str, rel_cycle: List[str], is_type_only: bool
) -> Dict[str, Any]:
    """Builds structured dictionary for a detected cycle."""
    msg_detail = (
        "Erased at compile time via type-only import (safe at runtime)."
        if is_type_only
        else "Runtime values imported in cycle: Temporal Dead Zone / undefined hazard at runtime."
    )
    return {
        "type": "TYPE_ONLY_CYCLE" if is_type_only else "RUNTIME_CYCLE",
        "severity": "WARNING" if is_type_only else "ERROR",
        "path": rel_cycle,
        "length": len(rel_cycle) - 1,
        "message": f"Circular import cycle ({cycle_str}): {msg_detail}",
    }


class TSCycleDetector:
    """3-color DFS cycle detector for TypeScript/JavaScript dependency graphs."""

    def __init__(self, project_root: str, max_depth: int = 15):
        self.project_abs = os.path.abspath(project_root)
        self.tsconfig = find_tsconfig(self.project_abs)
        self.max_depth = max_depth
        self.cycles: List[Dict[str, Any]] = []
        self.seen_cycle_keys: Set[str] = set()
        self.file_ast_cache: Dict[str, Dict[str, Any]] = {}
        self.gray_stack: List[str] = []
        self.black_set: Set[str] = set()
        self.edge_types: List[bool] = []

    def get_parsed_file(self, fpath: str) -> Optional[Dict[str, Any]]:
        """Parses and caches AST for a file."""
        if fpath in self.file_ast_cache:
            return self.file_ast_cache[fpath]
        if not os.path.isfile(fpath):
            return None
        try:
            with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            parsed = parse_ts_js_ast(content, filename=fpath)
            self.file_ast_cache[fpath] = parsed
            return parsed
        except OSError as e:
            logger.debug("Failed to read TypeScript file %s: %s", fpath, e)
            return None

    def _record_cycle(self, resolved: str) -> None:
        """Processes and records a detected cycle."""
        cycle_start = self.gray_stack.index(resolved)
        cycle_path = self.gray_stack[cycle_start:] + [resolved]
        cycle_edges = self.edge_types[cycle_start:]
        is_type_only = any(cycle_edges)
        rel_cycle = [os.path.relpath(p, self.project_abs) for p in cycle_path]
        cycle_str = " -> ".join(rel_cycle)

        if cycle_str not in self.seen_cycle_keys:
            self.seen_cycle_keys.add(cycle_str)
            self.cycles.append(_format_cycle_record(cycle_str, rel_cycle, is_type_only))

    def _process_edge(
        self, curr_file: str, mod_spec: str, is_type: bool, depth: int
    ) -> None:
        """Evaluates a single dependency edge in the graph."""
        resolved = resolve_ts_import_path(mod_spec, curr_file, self.project_abs, self.tsconfig)
        if not resolved or not resolved.startswith(self.project_abs):
            return
        if "node_modules" in resolved.replace("\\", "/").split("/"):
            return

        self.edge_types.append(is_type)
        if resolved in self.gray_stack:
            self._record_cycle(resolved)
        elif resolved not in self.black_set:
            self._dfs(resolved, depth + 1)
        self.edge_types.pop()

    def _dfs(self, curr_file: str, depth: int) -> None:
        """Traverses nodes depth-first to detect circular dependency paths."""
        if depth > self.max_depth:
            return
        parsed = self.get_parsed_file(curr_file)
        if not parsed:
            return

        self.gray_stack.append(curr_file)
        for imp in parsed["imports"]:
            self._process_edge(curr_file, imp["module"], imp.get("is_type_only", False), depth)
        for re_exp in parsed["re_exports"]:
            self._process_edge(curr_file, re_exp["module"], re_exp.get("is_type_only", False), depth)

        self.gray_stack.pop()
        self.black_set.add(curr_file)

    def detect(self, target_file: str) -> List[Dict[str, Any]]:
        """Runs cycle detection starting from target_file."""
        target_abs = os.path.abspath(target_file)
        self._dfs(target_abs, 0)
        return self.cycles


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
    detector = TSCycleDetector(project_root, max_depth=max_depth)
    return detector.detect(target_file)


def _format_ts_import_entry(imp: Dict[str, Any]) -> str:
    """Formats a single import line for skeleton digest."""
    type_prefix = "type " if imp.get("is_type_only") else ""
    names = ", ".join(imp.get("names", [])[:4])
    if len(imp.get("names", [])) > 4:
        names += "..."
    return f"from '{imp['module']}' import {type_prefix}{names}"


def _format_ts_imports_summary(imports: List[Dict[str, Any]]) -> Optional[str]:
    """Summarizes first 8 imports for skeleton digest."""
    if not imports:
        return None
    entries = [_format_ts_import_entry(imp) for imp in imports[:8]]
    return f"  Imports: {', '.join(entries)}"


def _format_ts_reexports_summary(re_exports: List[Dict[str, Any]]) -> Optional[str]:
    """Summarizes barrel re-exports for skeleton digest."""
    if not re_exports:
        return None
    re_strs = []
    for r in re_exports[:6]:
        type_prefix = "type " if r.get("is_type_only") else ""
        re_strs.append(f"export {type_prefix}{r['clause']} from '{r['module']}'")
    return f"  Re-exports: {', '.join(re_strs)}"


def _format_ts_declarations_summary(decls: List[Dict[str, Any]]) -> List[str]:
    """Summarizes declarations up to 25 items for skeleton digest."""
    lines: List[str] = []
    for decl in decls[:25]:
        sig = decl["signature"]
        needs_export = decl.get("is_exported") and not sig.startswith("export")
        prefix = "export " if needs_export else ""
        lines.append(f"  {prefix}{sig}")
    if len(decls) > 25:
        lines.append(f"  ... ({len(decls) - 25} more declarations)")
    return lines


def format_ts_digest_skeleton(filepath: str) -> str:
    """
    Produces a concise, high-density architectural skeleton of a TS/JS file
    for `arch-sidecar digest`.
    """
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            code = f.read()
    except OSError as e:
        logger.debug("Failed to read %s for skeleton digest: %s", filepath, e)
        return f"  [Error reading: {e}]\n"

    parsed = parse_ts_js_ast(code, filename=filepath)
    summary: List[str] = []

    imp_summary = _format_ts_imports_summary(parsed["imports"])
    if imp_summary:
        summary.append(imp_summary)

    re_summary = _format_ts_reexports_summary(parsed["re_exports"])
    if re_summary:
        summary.append(re_summary)

    summary.extend(_format_ts_declarations_summary(parsed["declarations"]))
    return "\n".join(summary) + "\n"
