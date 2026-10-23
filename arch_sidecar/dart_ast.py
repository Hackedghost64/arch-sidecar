"""
Dart AST & Lexical Analysis Engine for Devrith.

Provides:
1. High-fidelity lexical parsing for Dart (.dart) files.
2. Symbol extraction: classes, mixins, extensions, enums, typedefs, functions, and class methods.
3. Import, export, library, and part directive mapping.
4. Clean architectural skeleton formatting for digests.
"""

import re
from typing import Dict, List, Any, Optional


def strip_dart_comments(code: str) -> str:
    """Strips single-line and multi-line comments from Dart code while preserving string literals."""
    output: List[str] = []
    i = 0
    n = len(code)
    in_single_quote = False
    in_double_quote = False
    in_raw = False

    while i < n:
        c = code[i]
        # Escape sequence inside normal string
        if (in_single_quote or in_double_quote) and not in_raw and c == "\\":
            output.append(c)
            if i + 1 < n:
                output.append(code[i + 1])
                i += 2
                continue
            i += 1
            continue

        # Raw string r'...' or r"..."
        if not in_single_quote and not in_double_quote and c == "r" and i + 1 < n and code[i + 1] in ("'", '"'):
            output.append(c)
            i += 1
            continue

        # Single quote '
        if not in_double_quote and c == "'":
            in_single_quote = not in_single_quote
            output.append(c)
            i += 1
            continue

        # Double quote "
        if not in_single_quote and c == '"':
            in_double_quote = not in_double_quote
            output.append(c)
            i += 1
            continue

        # Outside strings: check comments
        if not in_single_quote and not in_double_quote:
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


def parse_dart_ast(source_text: str, filename: str = "") -> Dict[str, Any]:
    """
    Parses a Dart source file and extracts:
    - imports, exports, library directives
    - declarations: classes, mixins, extensions, enums, typedefs, top-level functions, methods
    """
    clean_code = strip_dart_comments(source_text)
    lines = clean_code.splitlines(keepends=True)
    raw_lines = source_text.splitlines(keepends=True)

    imports: List[Dict[str, Any]] = []
    exports: List[Dict[str, Any]] = []
    declarations: List[Dict[str, Any]] = []
    library_name: Optional[str] = None

    import_pat = re.compile(r"^import\s+['\"]([^'\"]+)['\"](?:\s+as\s+([a-zA-Z0-9_$]+))?(?:\s+(?:show|hide)\s+([^;]+))?;")
    export_pat = re.compile(r"^export\s+['\"]([^'\"]+)['\"](?:\s+(?:show|hide)\s+([^;]+))?;")
    lib_pat = re.compile(r"^library\s+([a-zA-Z0-9_.]+);")

    # Declarations patterns
    class_pat = re.compile(r"^(?:abstract\s+|sealed\s+|base\s+|interface\s+|final\s+)?class\s+([a-zA-Z0-9_$]+)")
    mixin_pat = re.compile(r"^mixin\s+([a-zA-Z0-9_$]+)")
    extension_pat = re.compile(r"^extension\s+([a-zA-Z0-9_$]+)?\s*on\s+")
    enum_pat = re.compile(r"^enum\s+([a-zA-Z0-9_$]+)")
    typedef_pat = re.compile(r"^typedef\s+([a-zA-Z0-9_$]+)")
    func_pat = re.compile(r"^(?:[a-zA-Z0-9_<>,?\s]+\s+)?([a-zA-Z0-9_$]+)\s*\(([^)]*)\)\s*(?:async\s*)?(?:=>|\{)")
    const_pat = re.compile(r"^(?:const|final|var)\s+(?:[a-zA-Z0-9_<>,?\s]+\s+)?([a-zA-Z0-9_$]+)\s*=")

    current_brace_depth = 0
    current_class: Optional[str] = None
    class_brace_depth = 0

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        # 1. Directives
        m_lib = lib_pat.match(stripped)
        if m_lib and current_brace_depth == 0:
            library_name = m_lib.group(1)
            continue

        m_imp = import_pat.match(stripped)
        if m_imp and current_brace_depth == 0:
            imports.append({
                "module": m_imp.group(1),
                "alias": m_imp.group(2),
                "filter": m_imp.group(3),
                "line": idx + 1
            })
            continue

        m_exp = export_pat.match(stripped)
        if m_exp and current_brace_depth == 0:
            exports.append({
                "module": m_exp.group(1),
                "filter": m_exp.group(2),
                "line": idx + 1
            })
            continue

        # 2. Top-level declarations
        is_top_level = (current_brace_depth == 0)
        matched = False

        if is_top_level:
            for kind, pat in [
                ("class", class_pat),
                ("mixin", mixin_pat),
                ("extension", extension_pat),
                ("enum", enum_pat),
                ("typedef", typedef_pat),
            ]:
                m = pat.match(stripped)
                if m:
                    name = m.group(1) or f"Extension_{idx+1}"
                    # Find doc comments or annotations walking back
                    start_line_idx = idx
                    k = idx - 1
                    while k >= 0:
                        prev = raw_lines[k].strip()
                        if prev.startswith("///") or prev.startswith("@"):
                            start_line_idx = k
                            k -= 1
                        else:
                            break

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

                    sig = stripped.split("{")[0].strip().rstrip(";")
                    decl_code = "".join(raw_lines[start_line_idx : end_idx + 1])
                    declarations.append({
                        "kind": kind,
                        "name": name,
                        "is_exported": not name.startswith("_"),
                        "signature": sig,
                        "line": idx + 1,
                        "start_line": start_line_idx + 1,
                        "end_line": end_idx + 1,
                        "code": decl_code,
                    })

                    if kind in ("class", "mixin", "extension", "enum"):
                        current_class = name
                        class_brace_depth = current_brace_depth + 1
                    matched = True
                    break

            if not matched:
                # Top-level function
                m_func = func_pat.match(stripped)
                if m_func and not stripped.startswith(("if", "for", "while", "switch", "return")):
                    name = m_func.group(1)
                    if name not in ("if", "for", "while", "switch", "return"):
                        start_line_idx = idx
                        k = idx - 1
                        while k >= 0:
                            prev = raw_lines[k].strip()
                            if prev.startswith("///") or prev.startswith("@"):
                                start_line_idx = k
                                k -= 1
                            else:
                                break

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
                            if not found_brace and (";" in l or "=>" in l):
                                end_idx = j
                                break

                        sig = stripped.split("{")[0].strip().rstrip(";")
                        decl_code = "".join(raw_lines[start_line_idx : end_idx + 1])
                        declarations.append({
                            "kind": "function",
                            "name": name,
                            "is_exported": not name.startswith("_"),
                            "signature": sig,
                            "line": idx + 1,
                            "start_line": start_line_idx + 1,
                            "end_line": end_idx + 1,
                            "code": decl_code,
                        })
                        matched = True

            if not matched:
                # Top-level const / final
                m_c = const_pat.match(stripped)
                if m_c:
                    name = m_c.group(1)
                    declarations.append({
                        "kind": "const",
                        "name": name,
                        "is_exported": not name.startswith("_"),
                        "signature": stripped.rstrip(";"),
                        "line": idx + 1,
                        "start_line": idx + 1,
                        "end_line": idx + 1,
                        "code": raw_lines[idx],
                    })
                    matched = True

        # Class method / getter / setter
        if not matched and current_class and current_brace_depth == class_brace_depth:
            m_meth = func_pat.match(stripped)
            if m_meth and not stripped.startswith(("if", "for", "while", "switch", "return", "case")):
                name = m_meth.group(1)
                if name not in ("if", "for", "while", "switch", "return", "catch", "finally"):
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
                        if not found_brace and (";" in l or "=>" in l):
                            end_idx = j
                            break

                    sig = stripped.split("{")[0].strip().rstrip(";")
                    declarations.append({
                        "kind": "method",
                        "name": f"{current_class}.{name}",
                        "is_exported": not name.startswith("_"),
                        "signature": sig,
                        "line": idx + 1,
                        "start_line": idx + 1,
                        "end_line": end_idx + 1,
                        "code": "".join(raw_lines[idx : end_idx + 1]),
                    })

        # Update brace depth
        current_brace_depth += (line.count("{") - line.count("}"))
        if current_class and current_brace_depth < class_brace_depth:
            current_class = None

    return {
        "filename": filename,
        "library": library_name,
        "imports": imports,
        "exports": exports,
        "declarations": declarations,
        "total_lines": len(lines),
    }


def format_dart_digest_skeleton(filepath: str) -> str:
    """Formats an architectural skeleton for a Dart file."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception as e:
        return f"  [Error reading Dart: {e}]\n"

    parsed = parse_dart_ast(content, filename=filepath)
    summary: List[str] = []

    if parsed.get("library"):
        summary.append(f"  library {parsed['library']};")

    imports = parsed.get("imports", [])
    if imports:
        summary.append(f"  // Imports ({len(imports)}):")
        for imp in imports[:8]:
            alias_str = f" as {imp['alias']}" if imp.get("alias") else ""
            summary.append(f"  import '{imp['module']}'{alias_str};")
        if len(imports) > 8:
            summary.append(f"  // ... and {len(imports) - 8} more imports")

    decls = parsed.get("declarations", [])
    if decls:
        summary.append(f"  // Declarations ({len(decls)}):")
        for d in decls:
            export_flag = "pub " if d.get("is_exported") else "priv"
            sig = d.get("signature") or d["name"]
            summary.append(f"  [{export_flag} {d['kind']:8}] L{d['start_line']}-{d['end_line']}: {sig}")

    return "\n".join(summary) + "\n"
