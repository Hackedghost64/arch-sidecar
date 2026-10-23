"""
JVM (Kotlin & Java) AST & Lexical Analysis Engine for Devrith.

Provides:
1. High-fidelity lexical parsing for Kotlin (.kt, .kts) and Java (.java) source files.
2. Symbol extraction: classes, data classes, sealed classes, interfaces, records, enums,
   objects, companion objects, functions, methods, and constructors with 1-indexed line spans.
3. Package and import hierarchy extraction.
4. Clean architectural digest skeleton formatting.
"""

import re
from typing import Dict, List, Any, Optional


def strip_jvm_comments(code: str) -> str:
    """Strips single-line (//) and multi-line (/* */) comments while preserving string and char literals."""
    output: List[str] = []
    i = 0
    n = len(code)
    in_string = False
    in_char = False
    in_triple_string = False

    while i < n:
        # Kotlin triple quoted string """..."""
        if not in_string and not in_char and i + 2 < n and code[i : i + 3] == '"""':
            in_triple_string = not in_triple_string
            output.append('"""')
            i += 3
            continue

        c = code[i]
        # Escape sequence inside normal string or char
        if (in_string or in_char) and not in_triple_string and c == "\\":
            output.append(c)
            if i + 1 < n:
                output.append(code[i + 1])
                i += 2
                continue
            i += 1
            continue

        # Normal string "
        if not in_triple_string and not in_char and c == '"':
            in_string = not in_string
            output.append(c)
            i += 1
            continue

        # Char literal '
        if not in_triple_string and not in_string and c == "'":
            in_char = not in_char
            output.append(c)
            i += 1
            continue

        # Outside strings: check comments
        if not in_string and not in_char and not in_triple_string:
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


def parse_jvm_ast(source_text: str, filename: str = "") -> Dict[str, Any]:
    """
    Parses a Java (.java) or Kotlin (.kt, .kts) source file and extracts:
    - package declaration
    - imports
    - declarations: classes, interfaces, records, enums, objects, functions, methods
    """
    is_kotlin = filename.endswith((".kt", ".kts"))
    clean_code = strip_jvm_comments(source_text)
    lines = clean_code.splitlines(keepends=True)
    raw_lines = source_text.splitlines(keepends=True)

    package_name = ""
    imports: List[Dict[str, Any]] = []
    declarations: List[Dict[str, Any]] = []

    package_pat = re.compile(r"^package\s+([a-zA-Z0-9_.]+);?")
    import_pat = re.compile(r"^import\s+(?:static\s+)?([a-zA-Z0-9_.*]+);?")

    # Kotlin patterns
    kt_class_pat = re.compile(
        r"^(?:(?:public|private|protected|internal|abstract|final|open|sealed|data|enum|annotation|value|inner)\s+)*"
        r"(class|interface|object|enum\s+class)\s+([a-zA-Z0-9_$]+)"
    )
    kt_fun_pat = re.compile(
        r"^(?:(?:public|private|protected|internal|abstract|final|open|override|suspend|inline|operator|infix)\s+)*"
        r"fun\s+(?:<[^>]+>\s+)?(?:([a-zA-Z0-9_$<>.]+)\.)?([a-zA-Z0-9_$]+)\s*\("
    )
    kt_typealias_pat = re.compile(r"^(?:(?:public|private|internal)\s+)?typealias\s+([a-zA-Z0-9_$]+)")

    # Java patterns
    java_class_pat = re.compile(
        r"^(?:(?:public|private|protected|static|abstract|final|sealed|non-sealed)\s+)*"
        r"(class|interface|enum|record|@interface)\s+([a-zA-Z0-9_$]+)"
    )
    java_method_pat = re.compile(
        r"^(?:(?:public|private|protected|static|final|synchronized|abstract|default|native)\s+)*"
        r"(?:<[^>]+>\s+)?([a-zA-Z0-9_$<>,?\[\]]+)\s+([a-zA-Z0-9_$]+)\s*\(([^)]*)\)\s*(?:throws\s+[^{;]+)?\s*(\{|;)"
    )

    current_brace_depth = 0
    current_class: Optional[str] = None
    class_brace_depth = 0

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        # 1. Package
        m_pkg = package_pat.match(stripped)
        if m_pkg and current_brace_depth == 0 and not package_name:
            package_name = m_pkg.group(1).rstrip(";")
            continue

        # 2. Imports
        m_imp = import_pat.match(stripped)
        if m_imp and current_brace_depth == 0:
            imports.append({"module": m_imp.group(1).rstrip(";"), "line": idx + 1})
            continue

        # 3. Declarations
        is_top_level = (current_brace_depth == 0)
        matched = False

        if is_kotlin:
            # Kotlin declaration check
            m_class = kt_class_pat.match(stripped)
            if m_class and is_top_level:
                kind = m_class.group(1).replace(" ", "_")
                name = m_class.group(2)

                # Include annotations (@...) and doc comments
                start_line_idx = idx
                k = idx - 1
                while k >= 0:
                    prev = raw_lines[k].strip()
                    if prev.startswith("@") or prev.startswith("/**") or prev.startswith("*"):
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
                    if not found_brace and j > idx and not lines[j].startswith(("\t", " ")):
                        end_idx = j - 1
                        break

                sig = stripped.split("{")[0].strip()
                decl_code = "".join(raw_lines[start_line_idx : end_idx + 1])
                declarations.append({
                    "kind": kind,
                    "name": name,
                    "is_exported": not ("private" in stripped or "internal" in stripped),
                    "signature": sig,
                    "line": idx + 1,
                    "start_line": start_line_idx + 1,
                    "end_line": end_idx + 1,
                    "code": decl_code,
                })
                current_class = name
                class_brace_depth = current_brace_depth + 1
                matched = True

            elif is_top_level:
                m_alias = kt_typealias_pat.match(stripped)
                if m_alias:
                    name = m_alias.group(1)
                    declarations.append({
                        "kind": "typealias",
                        "name": name,
                        "is_exported": "private" not in stripped,
                        "signature": stripped.rstrip(";"),
                        "line": idx + 1,
                        "start_line": idx + 1,
                        "end_line": idx + 1,
                        "code": raw_lines[idx],
                    })
                    matched = True

            # Kotlin Function (top-level or inside class)
            m_fun = kt_fun_pat.match(stripped)
            if not matched and m_fun:
                ext_receiver = m_fun.group(1)
                fun_name = m_fun.group(2)
                full_name = f"{current_class}.{fun_name}" if current_class else (f"{ext_receiver}.{fun_name}" if ext_receiver else fun_name)
                kind = "method" if current_class else "function"

                start_line_idx = idx
                k = idx - 1
                while k >= 0:
                    prev = raw_lines[k].strip()
                    if prev.startswith("@") or prev.startswith("/**") or prev.startswith("*"):
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
                    if not found_brace and "=" in l and ";" in l:
                        end_idx = j
                        break

                sig = stripped.split("{")[0].strip()
                decl_code = "".join(raw_lines[start_line_idx : end_idx + 1])
                declarations.append({
                    "kind": kind,
                    "name": full_name,
                    "is_exported": not ("private" in stripped or "internal" in stripped),
                    "signature": sig,
                    "line": idx + 1,
                    "start_line": start_line_idx + 1,
                    "end_line": end_idx + 1,
                    "code": decl_code,
                })
                matched = True

        else:
            # Java declaration check
            m_class = java_class_pat.match(stripped)
            if m_class and is_top_level:
                kind = m_class.group(1).replace("@", "")
                name = m_class.group(2)

                start_line_idx = idx
                k = idx - 1
                while k >= 0:
                    prev = raw_lines[k].strip()
                    if prev.startswith("@") or prev.startswith("/**") or prev.startswith("*"):
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

                sig = stripped.split("{")[0].strip()
                decl_code = "".join(raw_lines[start_line_idx : end_idx + 1])
                declarations.append({
                    "kind": kind,
                    "name": name,
                    "is_exported": "public" in stripped or "protected" in stripped,
                    "signature": sig,
                    "line": idx + 1,
                    "start_line": start_line_idx + 1,
                    "end_line": end_idx + 1,
                    "code": decl_code,
                })
                current_class = name
                class_brace_depth = current_brace_depth + 1
                matched = True

            elif not matched and current_class and current_brace_depth == class_brace_depth:
                m_meth = java_method_pat.match(stripped)
                if m_meth and not stripped.startswith(("if", "for", "while", "switch", "return", "throw", "new")):
                    ret_type = m_meth.group(1)
                    meth_name = m_meth.group(2)
                    is_abstract = m_meth.group(4) == ";" or "abstract" in stripped

                    start_line_idx = idx
                    k = idx - 1
                    while k >= 0:
                        prev = raw_lines[k].strip()
                        if prev.startswith("@") or prev.startswith("/**") or prev.startswith("*"):
                            start_line_idx = k
                            k -= 1
                        else:
                            break

                    end_idx = idx
                    if not is_abstract:
                        brace_count = 0
                        found_brace = False
                        for j in range(idx, len(lines)):
                            l = lines[j]
                            brace_count += l.count("{") - l.count("}")
                            if "{" in l:
                                found_brace = True
                            if found_brace and brace_count <= 0:
                                end_idx = j
                                break

                    sig = stripped.split("{")[0].strip().rstrip(";")
                    decl_code = "".join(raw_lines[start_line_idx : end_idx + 1])
                    declarations.append({
                        "kind": "method",
                        "name": f"{current_class}.{meth_name}",
                        "is_exported": "public" in stripped or "protected" in stripped,
                        "signature": sig,
                        "line": idx + 1,
                        "start_line": start_line_idx + 1,
                        "end_line": end_idx + 1,
                        "code": decl_code,
                    })
                    matched = True

        # Update brace depth
        current_brace_depth += (line.count("{") - line.count("}"))
        if current_class and current_brace_depth < class_brace_depth:
            current_class = None

    return {
        "filename": filename,
        "language": "kotlin" if is_kotlin else "java",
        "package": package_name,
        "imports": imports,
        "declarations": declarations,
        "total_lines": len(lines),
    }


def format_jvm_digest_skeleton(filepath: str) -> str:
    """Formats an architectural skeleton for a Java or Kotlin file."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception as e:
        return f"  [Error reading JVM: {e}]\n"

    parsed = parse_jvm_ast(content, filename=filepath)
    summary: List[str] = []

    if parsed.get("package"):
        summary.append(f"  package {parsed['package']};")

    imports = parsed.get("imports", [])
    if imports:
        summary.append(f"  // Imports ({len(imports)}):")
        for imp in imports[:8]:
            summary.append(f"  import {imp['module']};")
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
