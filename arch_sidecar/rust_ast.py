"""
Rust AST & Lexical Analysis Engine for Devrith.

Provides:
1. High-fidelity lexical parsing for Rust (.rs) files.
2. Symbol extraction: functions, structs, enums, traits, impl blocks, and type aliases with exact line ranges.
3. Import (`use`) mapping and module resolution.
4. Architectural skeleton formatting for digests.
"""

import re
import logging
from typing import Dict, List, Any, Optional, Tuple

logger = logging.getLogger(__name__)

RUST_USE_PAT = re.compile(r"^(?:pub(?:\([^)]+\))?\s+)?use\s+([^;]+);")

RUST_DECL_PATTERNS = [
    ("struct", re.compile(r"^(?:pub(?:\([^)]+\))?\s+)?struct\s+([A-Za-z0-9_]+)")),
    ("enum", re.compile(r"^(?:pub(?:\([^)]+\))?\s+)?enum\s+([A-Za-z0-9_]+)")),
    ("trait", re.compile(r"^(?:pub(?:\([^)]+\))?\s+)?trait\s+([A-Za-z0-9_]+)")),
    ("impl", re.compile(r"^impl(?:<.*?>)?\s+(?:.*?\s+for\s+)?([A-Za-z0-9_]+)")),
    ("function", re.compile(r"^(?:pub(?:\([^)]+\))?\s+)?(?:(?:async|const|unsafe|extern(?:\s+\"[^\"]+\")?)\s+)*fn\s+([A-Za-z0-9_]+)")),
    ("type", re.compile(r"^(?:pub(?:\([^)]+\))?\s+)?type\s+([A-Za-z0-9_]+)")),
    ("macro", re.compile(r"^macro_rules!\s+([A-Za-z0-9_]+)")),
    ("module", re.compile(r"^(?:pub(?:\([^)]+\))?\s+)?mod\s+([A-Za-z0-9_]+)\s*;")),
    ("const", re.compile(r"^(?:pub(?:\([^)]+\))?\s+)?(?:const|static)\s+([A-Za-z0-9_]+)")),
]


def _handle_rust_escape(code: str, i: int, n: int) -> Tuple[str, int]:
    """Extracts escaped character inside a string literal."""
    if i + 1 < n:
        return code[i : i + 2], i + 2
    return code[i], i + 1


def _consume_rust_comment(code: str, i: int, n: int) -> Tuple[Optional[str], int]:
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


def strip_rust_comments(code: str) -> str:
    """Strips single-line and multi-line comments from Rust code while preserving string/byte/char literals."""
    output: List[str] = []
    i = 0
    n = len(code)
    in_string = False

    while i < n:
        c = code[i]
        if in_string:
            if c == "\\":
                chunk, i = _handle_rust_escape(code, i, n)
                output.append(chunk)
                continue
            if c == '"':
                in_string = False
            output.append(c)
            i += 1
            continue

        rep, next_i = _consume_rust_comment(code, i, n)
        if rep is not None:
            output.append(rep)
            i = next_i
            continue

        if c == '"':
            in_string = True

        output.append(c)
        i += 1

    return "".join(output)


def _match_rust_decl(stripped: str) -> Optional[Tuple[str, str]]:
    """Matches a stripped line against Rust declaration patterns."""
    for kind, pat in RUST_DECL_PATTERNS:
        m = pat.match(stripped)
        if m:
            return kind, m.group(1)
    return None


def _find_rust_decl_end(lines: List[str], start_idx: int) -> int:
    """Finds the ending line index of a Rust declaration block or statement."""
    brace_count = 0
    found_brace = False
    for j in range(start_idx, len(lines)):
        line = lines[j]
        brace_count += line.count("{") - line.count("}")
        if "{" in line:
            found_brace = True
        if found_brace and brace_count <= 0:
            return j
        if not found_brace and ";" in line:
            return j
    return start_idx


def _find_rust_preceding_attributes(lines: List[str], start_idx: int) -> int:
    """Finds the top line index including preceding doc comments and attributes."""
    k = start_idx - 1
    start_line_idx = start_idx
    while k >= 0:
        prev = lines[k].strip()
        is_attr = prev.startswith(("#[", "///", "//!")) or prev == "]"
        if not is_attr:
            break
        start_line_idx = k
        k -= 1
    return start_line_idx


def _build_rust_declaration(
    lines: List[str],
    idx: int,
    kind: str,
    name: str,
    stripped: str,
) -> Dict[str, Any]:
    """Constructs a declaration record dictionary for a matched Rust symbol."""
    end_idx = _find_rust_decl_end(lines, idx)
    start_line_idx = _find_rust_preceding_attributes(lines, idx)
    decl_code = "".join(lines[start_line_idx : end_idx + 1])
    sig = stripped.split("{")[0].strip().rstrip(";")
    return {
        "kind": kind,
        "name": name,
        "is_exported": stripped.startswith("pub"),
        "signature": sig,
        "line": idx + 1,
        "start_line": start_line_idx + 1,
        "end_line": end_idx + 1,
        "code": decl_code,
    }


def parse_rust_ast(source_text: str, filename: str = "") -> Dict[str, Any]:
    """
    Parses a Rust source file and extracts:
    - imports (`use ...`)
    - declarations (`fn`, `struct`, `enum`, `trait`, `impl`, `type`)
    """
    clean_code = strip_rust_comments(source_text)
    lines = clean_code.splitlines(keepends=True)

    imports: List[Dict[str, Any]] = []
    declarations: List[Dict[str, Any]] = []
    current_brace_depth = 0

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        if current_brace_depth == 0:
            m_use = RUST_USE_PAT.match(stripped)
            if m_use:
                imports.append({"module": m_use.group(1).strip(), "line": idx + 1})

        decl_info = _match_rust_decl(stripped)
        if decl_info:
            kind, name = decl_info
            declarations.append(_build_rust_declaration(lines, idx, kind, name, stripped))

        current_brace_depth += line.count("{") - line.count("}")
        if current_brace_depth < 0:
            current_brace_depth = 0

    return {
        "filename": filename,
        "imports": imports,
        "declarations": declarations,
        "total_lines": len(lines),
    }


def format_rust_digest_skeleton(source_text: str, filename: str = "") -> str:
    """Formats a clean, high-density architectural skeleton for a Rust source file."""
    parsed = parse_rust_ast(source_text, filename)
    summary: List[str] = []

    if parsed["imports"]:
        summary.append("imports:")
        for imp in parsed["imports"][:8]:
            summary.append(f"  use {imp['module']};")
        if len(parsed["imports"]) > 8:
            summary.append(f"  ... and {len(parsed['imports']) - 8} more")

    if parsed["declarations"]:
        summary.append("declarations:")
        for d in parsed["declarations"]:
            vis = "pub " if d.get("is_exported") else ""
            summary.append(f"  {vis}{d['signature']}  // L{d['start_line']}-{d['end_line']}")

    return "\n".join(summary)
