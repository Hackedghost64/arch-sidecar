"""
Go AST & Lexical Analysis Engine for Devrith.

Provides:
1. High-fidelity lexical AST parsing for Go (.go) files.
2. Symbol extraction: functions, receiver methods, structs, interfaces, and types with exact line ranges.
3. Package declaration and import graph mapping.
4. Clean architectural skeleton formatting for digests.
"""

import re
from typing import Dict, List, Any


def strip_go_comments(code: str) -> str:
    """Strips single-line and multi-line comments from Go code while preserving string/rune literals."""
    output: List[str] = []
    i = 0
    n = len(code)
    in_string = False
    in_rune = False
    in_raw_string = False

    while i < n:
        c = code[i]
        # Check escape inside normal string
        if (in_string or in_rune) and c == "\\":
            output.append(c)
            if i + 1 < n:
                output.append(code[i + 1])
                i += 2
                continue
            i += 1
            continue

        # Raw string literal `...`
        if not in_string and not in_rune and c == "`":
            in_raw_string = not in_raw_string
            output.append(c)
            i += 1
            continue

        # Double-quoted string "..."
        if not in_raw_string and not in_rune and c == '"':
            in_string = not in_string
            output.append(c)
            i += 1
            continue

        # Rune literal '...'
        if not in_raw_string and not in_string and c == "'":
            in_rune = not in_rune
            output.append(c)
            i += 1
            continue

        # Outside strings: check comments
        if not in_string and not in_rune and not in_raw_string:
            if c == "/" and i + 1 < n and code[i + 1] == "/":
                newline_pos = code.find("\n", i)
                if newline_pos == -1:
                    break
                output.append("\n")
                i = newline_pos + 1
                continue
            elif c == "/" and i + 1 < n and code[i + 1] == "*":
                end_pos = code.find("*/", i + 2)
                if end_pos == -1:
                    break
                comment_newlines = code[i : end_pos + 2].count("\n")
                output.append("\n" * comment_newlines)
                i = end_pos + 2
                continue

        output.append(c)
        i += 1

    return "".join(output)


def parse_go_ast(source_text: str, filename: str = "") -> Dict[str, Any]:
    """
    Parses a Go source file and extracts:
    - package name
    - imports (module, line)
    - declarations (functions, methods, structs, interfaces, types, constants)
    """
    clean_code = strip_go_comments(source_text)
    lines = clean_code.splitlines(keepends=True)

    package_name = ""
    imports: List[Dict[str, Any]] = []
    declarations: List[Dict[str, Any]] = []

    # 1. Package name
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("package "):
            parts = stripped.split()
            if len(parts) >= 2:
                package_name = parts[1].strip()
            break

    # 2. Imports
    in_import_block = False
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("import ("):
            in_import_block = True
            continue
        if in_import_block:
            if stripped == ")":
                in_import_block = False
            elif stripped:
                imp_match = re.search(r'["\']([^"\']+)["\']', stripped)
                if imp_match:
                    imports.append({"module": imp_match.group(1), "line": idx + 1})
            continue
        if stripped.startswith("import ") and '"' in stripped:
            imp_match = re.search(r'["\']([^"\']+)["\']', stripped)
            if imp_match:
                imports.append({"module": imp_match.group(1), "line": idx + 1})

    # 3. Declarations (Functions, Methods, Types, Structs, Interfaces, Constants, Vars)
    func_pat = re.compile(r"^func\s+(?:\((?:[^)]+)\)\s+)?([A-Za-z0-9_]+)(?:\[[^\]]+\])?\s*\(")
    type_pat = re.compile(r"^type\s+([A-Za-z0-9_]+)(?:\[[^\]]+\])?\s+(struct|interface|[A-Za-z0-9_]+)")
    const_pat = re.compile(r"^(?:const|var)\s+([A-Za-z0-9_]+)\s*=")
    grouped_block_start = re.compile(r"^(const|var)\s*\(")

    in_grouped_block = False
    grouped_kind = ""

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        # Check grouped const/var block
        m_group = grouped_block_start.match(stripped)
        if m_group:
            in_grouped_block = True
            grouped_kind = m_group.group(1)
            continue

        if in_grouped_block:
            if stripped == ")":
                in_grouped_block = False
                grouped_kind = ""
                continue
            # Extract identifier from line in grouped block
            # e.g.: "Reset Attribute = iota", "Bold", "Status = 200", "mapReset map[...]"
            m_id = re.match(r"^([A-Za-z0-9_]+)", stripped)
            if m_id:
                name = m_id.group(1)
                if name != "_":
                    declarations.append({
                        "kind": grouped_kind,
                        "name": name,
                        "is_exported": name[0].isupper() if name else False,
                        "signature": stripped,
                        "line": idx + 1,
                        "start_line": idx + 1,
                        "end_line": idx + 1,
                        "code": stripped,
                    })
            continue

        # Function or Method (including generic functions func Foo[T any](...))
        m_func = func_pat.match(stripped)
        if m_func:
            name = m_func.group(1)
            is_method = "(" in stripped[: stripped.find(name)]
            kind = "method" if is_method else "function"

            brace_count = 0
            found_brace = False
            end_idx = idx
            for j in range(idx, len(lines)):
                l = lines[j]
                brace_count += l.count("{") - l.count("}")
                if "{" in l:
                    found_brace = True
                if found_brace and brace_count <= 0:
                    end_idx = j
                    break

            decl_code = "".join(lines[idx : end_idx + 1])
            sig = stripped.split("{")[0].strip()
            declarations.append({
                "kind": kind,
                "name": name,
                "is_exported": name[0].isupper() if name else False,
                "signature": sig,
                "line": idx + 1,
                "start_line": idx + 1,
                "end_line": end_idx + 1,
                "code": decl_code,
            })
            continue

        # Type (struct, interface, alias)
        m_type = type_pat.match(stripped)
        if m_type:
            name = m_type.group(1)
            type_category = m_type.group(2)
            kind = type_category if type_category in ("struct", "interface") else "type"

            brace_count = 0
            found_brace = False
            end_idx = idx
            for j in range(idx, len(lines)):
                l = lines[j]
                brace_count += l.count("{") - l.count("}")
                if "{" in l:
                    found_brace = True
                if found_brace and brace_count <= 0:
                    end_idx = j
                    break
                if not found_brace and j > idx and not lines[j].startswith(("\t", " ")):
                    end_idx = j - 1
                    break

            decl_code = "".join(lines[idx : end_idx + 1])
            sig = stripped.split("{")[0].strip()
            declarations.append({
                "kind": kind,
                "name": name,
                "is_exported": name[0].isupper() if name else False,
                "signature": sig,
                "line": idx + 1,
                "start_line": idx + 1,
                "end_line": end_idx + 1,
                "code": decl_code,
            })
            continue

        # Top-level const or var
        m_const = const_pat.match(stripped)
        if m_const:
            name = m_const.group(1)
            kind = "const" if stripped.startswith("const") else "var"
            declarations.append({
                "kind": kind,
                "name": name,
                "is_exported": name[0].isupper() if name else False,
                "signature": stripped,
                "line": idx + 1,
                "start_line": idx + 1,
                "end_line": idx + 1,
                "code": line,
            })

    return {
        "filename": filename,
        "package": package_name,
        "imports": imports,
        "declarations": declarations,
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
