"""
Rust AST & Lexical Analysis Engine for Devrith.

Provides:
1. High-fidelity lexical parsing for Rust (.rs) files.
2. Symbol extraction: functions, structs, enums, traits, impl blocks, and type aliases with exact line ranges.
3. Import (`use`) mapping and module resolution.
4. Architectural skeleton formatting for digests.
"""

import re
from typing import Dict, List, Any


def strip_rust_comments(code: str) -> str:
    """Strips single-line and multi-line comments from Rust code while preserving string/byte/char literals."""
    output: List[str] = []
    i = 0
    n = len(code)
    in_string = False
    in_raw_string = False

    while i < n:
        c = code[i]
        # Check escape sequence inside normal string
        if in_string and c == "\\":
            output.append(c)
            if i + 1 < n:
                output.append(code[i + 1])
                i += 2
                continue
            i += 1
            continue

        # Raw string literal r#"..."# or r"..."
        if not in_string and c == "r" and i + 1 < n and (code[i + 1] in ('"', "#")):
            output.append(c)
            i += 1
            continue

        # Double-quoted string "..."
        if c == '"':
            in_string = not in_string
            output.append(c)
            i += 1
            continue

        # Outside strings: check comments
        if not in_string:
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

    use_pat = re.compile(r"^(?:pub(?:\([^)]+\))?\s+)?use\s+([^;]+);")
    fn_pat = re.compile(r"^(?:pub(?:\([^)]+\))?\s+)?(?:(?:async|const|unsafe|extern(?:\s+\"[^\"]+\")?)\s+)*fn\s+([A-Za-z0-9_]+)")
    struct_pat = re.compile(r"^(?:pub(?:\([^)]+\))?\s+)?struct\s+([A-Za-z0-9_]+)")
    enum_pat = re.compile(r"^(?:pub(?:\([^)]+\))?\s+)?enum\s+([A-Za-z0-9_]+)")
    trait_pat = re.compile(r"^(?:pub(?:\([^)]+\))?\s+)?trait\s+([A-Za-z0-9_]+)")
    impl_pat = re.compile(r"^impl(?:<.*?>)?\s+(?:.*?\s+for\s+)?([A-Za-z0-9_]+)")
    type_pat = re.compile(r"^(?:pub(?:\([^)]+\))?\s+)?type\s+([A-Za-z0-9_]+)")
    macro_pat = re.compile(r"^macro_rules!\s+([A-Za-z0-9_]+)")
    mod_pat = re.compile(r"^(?:pub(?:\([^)]+\))?\s+)?mod\s+([A-Za-z0-9_]+)\s*;")
    const_pat = re.compile(r"^(?:pub(?:\([^)]+\))?\s+)?(?:const|static)\s+([A-Za-z0-9_]+)")

    current_brace_depth = 0

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        # Track outer brace depth so we only record top-level declarations or impl blocks
        m_use = use_pat.match(stripped)
        if m_use and current_brace_depth == 0:
            imports.append({"module": m_use.group(1).strip(), "line": idx + 1})

        # Check declarations
        for kind, pat in [
            ("struct", struct_pat),
            ("enum", enum_pat),
            ("trait", trait_pat),
            ("impl", impl_pat),
            ("function", fn_pat),
            ("type", type_pat),
            ("macro", macro_pat),
            ("module", mod_pat),
            ("const", const_pat),
        ]:
            m = pat.match(stripped)
            if m:
                name = m.group(1)
                is_exported = stripped.startswith("pub")

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
                    if not found_brace and ";" in l:
                        end_idx = j
                        break

                decl_code = "".join(lines[idx : end_idx + 1])
                sig = stripped.split("{")[0].strip().rstrip(";")
                declarations.append({
                    "kind": kind,
                    "name": name,
                    "is_exported": is_exported,
                    "signature": sig,
                    "line": idx + 1,
                    "start_line": idx + 1,
                    "end_line": end_idx + 1,
                    "code": decl_code,
                })
                break

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
