"""
Go AST & Lexical Analysis Engine for Devrith.

Provides:
1. High-fidelity lexical AST parsing for Go (.go) files.
2. Symbol extraction: functions, receiver methods, structs, interfaces, and types with exact line ranges.
3. Package declaration and import graph mapping.
4. Clean architectural skeleton formatting for digests.
"""

import re
import logging
from typing import Dict, List, Any, Optional, Tuple

logger = logging.getLogger(__name__)

GO_FUNC_PAT = re.compile(r"^func\s+(?:\((?:[^)]+)\)\s+)?([A-Za-z0-9_]+)(?:\[[^\]]+\])?\s*\(")
GO_TYPE_PAT = re.compile(r"^type\s+([A-Za-z0-9_]+)(?:\[[^\]]+\])?\s+(struct|interface|[A-Za-z0-9_]+)")
GO_CONST_PAT = re.compile(r"^(?:const|var)\s+([A-Za-z0-9_]+)\s*=")
GO_GROUPED_BLOCK_START = re.compile(r"^(const|var)\s*\(")


def _handle_go_escape(code: str, i: int, n: int) -> Tuple[str, int]:
    """Extracts escaped character sequence inside string or rune."""
    if i + 1 < n:
        return code[i : i + 2], i + 2
    return code[i], i + 1


def _consume_go_comment(code: str, i: int, n: int) -> Tuple[Optional[str], int]:
    """Extracts a comment replacement and advanced index, or (None, i) if not a comment."""
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


def _handle_in_quote(
    code: str, i: int, n: int, quote_char: str
) -> Tuple[str, int, Optional[str]]:
    """Handles parsing while inside a string, rune, or raw string."""
    c = code[i]
    if c == "\\" and quote_char in ('"', "'"):
        chunk, next_i = _handle_go_escape(code, i, n)
        return chunk, next_i, quote_char
    new_quote = None if c == quote_char else quote_char
    return c, i + 1, new_quote


def strip_go_comments(code: str) -> str:
    """Strips single-line and multi-line comments from Go code while preserving string/rune literals."""
    output: List[str] = []
    i = 0
    n = len(code)
    quote_char: Optional[str] = None

    while i < n:
        c = code[i]
        if quote_char is not None:
            chunk, i, quote_char = _handle_in_quote(code, i, n, quote_char)
            output.append(chunk)
            continue

        rep, next_i = _consume_go_comment(code, i, n)
        if rep is not None:
            output.append(rep)
            i = next_i
            continue

        if c in ('"', "'", "`"):
            quote_char = c

        output.append(c)
        i += 1

    return "".join(output)


def _extract_go_package(lines: List[str]) -> str:
    """Extracts package name from Go source lines."""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("package "):
            parts = stripped.split()
            if len(parts) >= 2:
                return parts[1].strip()
            break
    return ""


def _parse_go_import_spec(stripped: str, line_no: int) -> Optional[Dict[str, Any]]:
    """Extracts quoted module from an import line if present."""
    m = re.search(r'["\']([^"\']+)["\']', stripped)
    if m:
        return {"module": m.group(1), "line": line_no}
    return None


def _handle_import_line(
    stripped: str, in_block: bool, line_no: int
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Updates import block state and extracts any import specifier."""
    if stripped.startswith("import ("):
        return True, None
    if in_block and stripped == ")":
        return False, None
    if in_block:
        return True, _parse_go_import_spec(stripped, line_no)
    if stripped.startswith("import "):
        return False, _parse_go_import_spec(stripped, line_no)
    return False, None


def _extract_go_imports(lines: List[str]) -> List[Dict[str, Any]]:
    """Extracts imports list from Go source lines."""
    imports: List[Dict[str, Any]] = []
    in_block = False

    for idx, line in enumerate(lines):
        stripped = line.strip()
        in_block, imp = _handle_import_line(stripped, in_block, idx + 1)
        if imp:
            imports.append(imp)

    return imports


def _find_go_block_end(lines: List[str], start_idx: int) -> int:
    """Finds ending line index for a brace-enclosed Go block."""
    brace_count = 0
    found_brace = False
    for j in range(start_idx, len(lines)):
        l = lines[j]
        brace_count += l.count("{") - l.count("}")
        if "{" in l:
            found_brace = True
        if found_brace and brace_count <= 0:
            return j
    return start_idx


def _find_go_type_end(lines: List[str], start_idx: int) -> int:
    """Finds ending line index for a Go type declaration (struct, interface, or alias)."""
    brace_count = 0
    found_brace = False
    for j in range(start_idx, len(lines)):
        l = lines[j]
        brace_count += l.count("{") - l.count("}")
        if "{" in l:
            found_brace = True
        if found_brace and brace_count <= 0:
            return j
        if not found_brace and j > start_idx and not l.startswith(("\t", " ")):
            return j - 1
    return start_idx


def _parse_grouped_decl_line(stripped: str, kind: str, line_no: int) -> Optional[Dict[str, Any]]:
    """Parses an entry inside a grouped var/const block."""
    m_id = re.match(r"^([A-Za-z0-9_]+)", stripped)
    if not m_id or m_id.group(1) == "_":
        return None
    name = m_id.group(1)
    return {
        "kind": kind,
        "name": name,
        "is_exported": name[0].isupper() if name else False,
        "signature": stripped,
        "line": line_no,
        "start_line": line_no,
        "end_line": line_no,
        "code": stripped,
    }


def _parse_go_func(lines: List[str], idx: int, stripped: str) -> Optional[Dict[str, Any]]:
    """Parses a function or method definition."""
    m_func = GO_FUNC_PAT.match(stripped)
    if not m_func:
        return None
    name = m_func.group(1)
    is_method = "(" in stripped[: stripped.find(name)]
    kind = "method" if is_method else "function"
    end_idx = _find_go_block_end(lines, idx)
    decl_code = "".join(lines[idx : end_idx + 1])
    sig = stripped.split("{")[0].strip()
    return {
        "kind": kind,
        "name": name,
        "is_exported": name[0].isupper() if name else False,
        "signature": sig,
        "line": idx + 1,
        "start_line": idx + 1,
        "end_line": end_idx + 1,
        "code": decl_code,
    }


def _parse_go_type(lines: List[str], idx: int, stripped: str) -> Optional[Dict[str, Any]]:
    """Parses a type definition (struct, interface, or alias)."""
    m_type = GO_TYPE_PAT.match(stripped)
    if not m_type:
        return None
    name = m_type.group(1)
    type_category = m_type.group(2)
    kind = type_category if type_category in ("struct", "interface") else "type"
    end_idx = _find_go_type_end(lines, idx)
    decl_code = "".join(lines[idx : end_idx + 1])
    sig = stripped.split("{")[0].strip()
    return {
        "kind": kind,
        "name": name,
        "is_exported": name[0].isupper() if name else False,
        "signature": sig,
        "line": idx + 1,
        "start_line": idx + 1,
        "end_line": end_idx + 1,
        "code": decl_code,
    }


def _parse_go_single_const_var(stripped: str, line_no: int, raw_line: str) -> Optional[Dict[str, Any]]:
    """Parses a single top-level const or var line."""
    m_const = GO_CONST_PAT.match(stripped)
    if not m_const:
        return None
    name = m_const.group(1)
    kind = "const" if stripped.startswith("const") else "var"
    return {
        "kind": kind,
        "name": name,
        "is_exported": name[0].isupper() if name else False,
        "signature": stripped,
        "line": line_no,
        "start_line": line_no,
        "end_line": line_no,
        "code": raw_line,
    }


def _parse_go_declaration(lines: List[str], idx: int, stripped: str, raw_line: str) -> Optional[Dict[str, Any]]:
    """Tries parsing function, type, or single const/var declaration."""
    fn_decl = _parse_go_func(lines, idx, stripped)
    if fn_decl:
        return fn_decl
    type_decl = _parse_go_type(lines, idx, stripped)
    if type_decl:
        return type_decl
    return _parse_go_single_const_var(stripped, idx + 1, raw_line)


def _handle_grouped_block(
    stripped: str,
    grouped_kind: str,
    idx: int,
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """Processes a line within a grouped const/var block."""
    if stripped == ")":
        return False, "", None
    decl = _parse_grouped_decl_line(stripped, grouped_kind, idx + 1)
    return True, grouped_kind, decl


def _extract_go_declarations(lines: List[str]) -> List[Dict[str, Any]]:
    """Extracts all top-level symbols from Go source lines."""
    declarations: List[Dict[str, Any]] = []
    in_grouped_block = False
    grouped_kind = ""

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        m_group = GO_GROUPED_BLOCK_START.match(stripped)
        if m_group:
            in_grouped_block = True
            grouped_kind = m_group.group(1)
            continue

        if in_grouped_block:
            in_grouped_block, grouped_kind, decl = _handle_grouped_block(
                stripped, grouped_kind, idx
            )
            if decl:
                declarations.append(decl)
            continue

        decl = _parse_go_declaration(lines, idx, stripped, line)
        if decl:
            declarations.append(decl)

    return declarations


def parse_go_ast(source_text: str, filename: str = "") -> Dict[str, Any]:
    """
    Parses a Go source file and extracts:
    - package name
    - imports (module, line)
    - declarations (functions, methods, structs, interfaces, types, constants)
    """
    clean_code = strip_go_comments(source_text)
    lines = clean_code.splitlines(keepends=True)

    return {
        "filename": filename,
        "package": _extract_go_package(lines),
        "imports": _extract_go_imports(lines),
        "declarations": _extract_go_declarations(lines),
        "total_lines": len(lines),
    }


def format_go_digest_skeleton(source_text: str, filename: str = "") -> str:
    """Formats a clean, high-density architectural skeleton for a Go source file."""
    parsed = parse_go_ast(source_text, filename)
    summary: List[str] = []

    if parsed["package"]:
        summary.append(f"package {parsed['package']}")

    if parsed["imports"]:
        summary.append("imports:")
        for imp in parsed["imports"][:8]:
            summary.append(f"  - {imp['module']}")
        if len(parsed["imports"]) > 8:
            summary.append(f"  ... and {len(parsed['imports']) - 8} more")

    if parsed["declarations"]:
        summary.append("declarations:")
        for d in parsed["declarations"]:
            vis = "pub " if d.get("is_exported") else ""
            summary.append(f"  {vis}{d['signature']}  // L{d['start_line']}-{d['end_line']}")

    return "\n".join(summary)
