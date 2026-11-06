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
import logging
from dataclasses import dataclass
from typing import Dict, List, Any, Optional, Tuple

logger = logging.getLogger(__name__)

_JVM_TOKEN_PATTERN = re.compile(
    r'("""[\s\S]*?""")|'
    r'("(\\.|[^"\\])*")|'
    r"('(\\.|[^'\\])*')|"
    r"(//[^\n]*)|"
    r"(/\*[\s\S]*?\*/)"
)

_PACKAGE_PAT = re.compile(r"^package\s+([a-zA-Z0-9_.]+);?")
_IMPORT_PAT = re.compile(r"^import\s+(?:static\s+)?([a-zA-Z0-9_.*]+);?")

_KT_CLASS_PAT = re.compile(
    r"^(?:(?:public|private|protected|internal|abstract|final|open|sealed|data|enum|annotation|value|inner)\s+)*"
    r"(class|interface|object|enum\s+class)\s+([a-zA-Z0-9_$]+)"
)
_KT_FUN_PAT = re.compile(
    r"^(?:(?:public|private|protected|internal|abstract|final|open|override|suspend|inline|operator|infix)\s+)*"
    r"fun\s+(?:<[^>]+>\s+)?(?:([a-zA-Z0-9_$<>.]+)\.)?([a-zA-Z0-9_$]+)\s*\("
)
_KT_TYPEALIAS_PAT = re.compile(r"^(?:(?:public|private|internal)\s+)?typealias\s+([a-zA-Z0-9_$]+)")

_JAVA_CLASS_PAT = re.compile(
    r"^(?:(?:public|private|protected|static|abstract|final|sealed|non-sealed)\s+)*"
    r"(class|interface|enum|record|@interface)\s+([a-zA-Z0-9_$]+)"
)
_JAVA_METHOD_PAT = re.compile(
    r"^(?:(?:public|private|protected|static|final|synchronized|abstract|default|native)\s+)*"
    r"(?:<[^>]+>\s+)?([a-zA-Z0-9_$<>,?\[\]]+)\s+([a-zA-Z0-9_$]+)\s*\(([^)]*)\)\s*(?:throws\s+[^{;]+)?\s*(\{|;)"
)
_JAVA_CONTROL_KEYWORDS = ("if", "for", "while", "switch", "return", "throw", "new")


@dataclass(frozen=True)
class _ParseContext:
    lines: List[str]
    raw_lines: List[str]
    idx: int
    stripped: str


def _strip_jvm_comment_match(match: re.Match) -> str:
    text = match.group(0)
    if text.startswith("//"):
        return ""
    if text.startswith("/*"):
        return "\n" * text.count("\n")
    return text


def strip_jvm_comments(code: str) -> str:
    """Strips single-line (//) and multi-line (/* */) comments while preserving string and char literals."""
    return _JVM_TOKEN_PATTERN.sub(_strip_jvm_comment_match, code)


def _find_decl_start(raw_lines: List[str], idx: int) -> int:
    k = idx - 1
    while k >= 0:
        prev = raw_lines[k].strip()
        if not (prev.startswith("@") or prev.startswith("/**") or prev.startswith("*")):
            break
        k -= 1
    return k + 1


def _is_kt_class_exit_line(line: str, j: int, start_idx: int, found_brace: bool) -> bool:
    if found_brace or j <= start_idx:
        return False
    return not line.startswith(("\t", " "))


def _find_kt_class_end(lines: List[str], start_idx: int) -> int:
    brace_count = 0
    found_brace = False
    for j in range(start_idx, len(lines)):
        line = lines[j]
        brace_count += line.count("{") - line.count("}")
        if "{" in line:
            found_brace = True
        if found_brace and brace_count <= 0:
            return j
        if _is_kt_class_exit_line(line, j, start_idx, found_brace):
            return j - 1
    return start_idx


def _is_single_line_fun(line: str, found_brace: bool) -> bool:
    return not found_brace and "=" in line and ";" in line


def _find_kt_fun_end(lines: List[str], start_idx: int) -> int:
    brace_count = 0
    found_brace = False
    for j in range(start_idx, len(lines)):
        line = lines[j]
        brace_count += line.count("{") - line.count("}")
        if "{" in line:
            found_brace = True
        if found_brace and brace_count <= 0:
            return j
        if _is_single_line_fun(line, found_brace):
            return j
    return start_idx


def _find_standard_brace_end(lines: List[str], start_idx: int) -> int:
    brace_count = 0
    found_brace = False
    for j in range(start_idx, len(lines)):
        line = lines[j]
        brace_count += line.count("{") - line.count("}")
        if "{" in line:
            found_brace = True
        if found_brace and brace_count <= 0:
            return j
    return start_idx


def _extract_span_code(raw_lines: List[str], idx: int, start_idx: int, end_idx: int) -> Tuple[int, int, int, str]:
    return (idx + 1, start_idx + 1, end_idx + 1, "".join(raw_lines[start_idx : end_idx + 1]))


def _build_declaration(
    kind: str,
    name: str,
    is_exported: bool,
    sig: str,
    span_and_code: Tuple[int, int, int, str],
) -> Dict[str, Any]:
    line, start_line, end_line, code = span_and_code
    return {
        "kind": kind,
        "name": name,
        "is_exported": is_exported,
        "signature": sig,
        "line": line,
        "start_line": start_line,
        "end_line": end_line,
        "code": code,
    }


def _parse_kt_class(
    lines: List[str],
    raw_lines: List[str],
    idx: int,
    stripped: str,
) -> Optional[Tuple[Dict[str, Any], str]]:
    m = _KT_CLASS_PAT.match(stripped)
    if not m:
        return None
    kind = m.group(1).replace(" ", "_")
    name = m.group(2)
    start_idx = _find_decl_start(raw_lines, idx)
    end_idx = _find_kt_class_end(lines, idx)
    sig = stripped.split("{")[0].strip()
    is_exported = not ("private" in stripped or "internal" in stripped)
    span_code = _extract_span_code(raw_lines, idx, start_idx, end_idx)
    decl = _build_declaration(kind, name, is_exported, sig, span_code)
    return decl, name


def _parse_kt_typealias(
    raw_lines: List[str],
    idx: int,
    stripped: str,
) -> Optional[Dict[str, Any]]:
    m = _KT_TYPEALIAS_PAT.match(stripped)
    if not m:
        return None
    name = m.group(1)
    span_code = (idx + 1, idx + 1, idx + 1, raw_lines[idx])
    return _build_declaration(
        kind="typealias",
        name=name,
        is_exported=("private" not in stripped),
        sig=stripped.rstrip(";"),
        span_and_code=span_code,
    )


def _resolve_kt_fun_name(current_class: Optional[str], ext_receiver: Optional[str], fun_name: str) -> str:
    if current_class:
        return f"{current_class}.{fun_name}"
    if ext_receiver:
        return f"{ext_receiver}.{fun_name}"
    return fun_name


def _parse_kt_fun(
    lines: List[str],
    raw_lines: List[str],
    idx: int,
    stripped: str,
    current_class: Optional[str],
) -> Optional[Dict[str, Any]]:
    m = _KT_FUN_PAT.match(stripped)
    if not m:
        return None
    ext_receiver = m.group(1)
    fun_name = m.group(2)
    full_name = _resolve_kt_fun_name(current_class, ext_receiver, fun_name)
    kind = "method" if current_class else "function"
    start_idx = _find_decl_start(raw_lines, idx)
    end_idx = _find_kt_fun_end(lines, idx)
    sig = stripped.split("{")[0].strip()
    is_exported = not ("private" in stripped or "internal" in stripped)
    span_code = _extract_span_code(raw_lines, idx, start_idx, end_idx)
    return _build_declaration(kind, full_name, is_exported, sig, span_code)


def _dispatch_kt_decl(
    ctx: _ParseContext,
    brace_depth: int,
    class_depth: int,
    current_class: Optional[str],
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    if brace_depth == 0:
        class_res = _parse_kt_class(ctx.lines, ctx.raw_lines, ctx.idx, ctx.stripped)
        if class_res:
            decl, name = class_res
            return decl, name, brace_depth + 1
        alias_decl = _parse_kt_typealias(ctx.raw_lines, ctx.idx, ctx.stripped)
        if alias_decl:
            return alias_decl, current_class, class_depth

    fun_decl = _parse_kt_fun(ctx.lines, ctx.raw_lines, ctx.idx, ctx.stripped, current_class)
    if fun_decl:
        return fun_decl, current_class, class_depth

    return None, current_class, class_depth


def _parse_java_class(
    lines: List[str],
    raw_lines: List[str],
    idx: int,
    stripped: str,
) -> Optional[Tuple[Dict[str, Any], str]]:
    m = _JAVA_CLASS_PAT.match(stripped)
    if not m:
        return None
    kind = m.group(1).replace("@", "")
    name = m.group(2)
    start_idx = _find_decl_start(raw_lines, idx)
    end_idx = _find_standard_brace_end(lines, idx)
    sig = stripped.split("{")[0].strip()
    is_exported = ("public" in stripped or "protected" in stripped)
    span_code = _extract_span_code(raw_lines, idx, start_idx, end_idx)
    decl = _build_declaration(kind, name, is_exported, sig, span_code)
    return decl, name


def _parse_java_method(
    lines: List[str],
    raw_lines: List[str],
    idx: int,
    stripped: str,
    current_class: str,
) -> Optional[Dict[str, Any]]:
    if stripped.startswith(_JAVA_CONTROL_KEYWORDS):
        return None
    m = _JAVA_METHOD_PAT.match(stripped)
    if not m:
        return None
    meth_name = m.group(2)
    is_abstract = (m.group(4) == ";" or "abstract" in stripped)
    start_idx = _find_decl_start(raw_lines, idx)
    end_idx = idx if is_abstract else _find_standard_brace_end(lines, idx)
    sig = stripped.split("{")[0].strip().rstrip(";")
    is_exported = ("public" in stripped or "protected" in stripped)
    span_code = _extract_span_code(raw_lines, idx, start_idx, end_idx)
    return _build_declaration("method", f"{current_class}.{meth_name}", is_exported, sig, span_code)


def _dispatch_java_decl(
    ctx: _ParseContext,
    brace_depth: int,
    class_depth: int,
    current_class: Optional[str],
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    if brace_depth == 0:
        class_res = _parse_java_class(ctx.lines, ctx.raw_lines, ctx.idx, ctx.stripped)
        if class_res:
            decl, name = class_res
            return decl, name, brace_depth + 1

    if current_class and brace_depth == class_depth:
        meth_decl = _parse_java_method(ctx.lines, ctx.raw_lines, ctx.idx, ctx.stripped, current_class)
        if meth_decl:
            return meth_decl, current_class, class_depth

    return None, current_class, class_depth


def _process_jvm_decl(
    is_kotlin: bool,
    ctx: _ParseContext,
    brace_depth: int,
    class_depth: int,
    current_class: Optional[str],
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    if is_kotlin:
        return _dispatch_kt_decl(ctx, brace_depth, class_depth, current_class)
    return _dispatch_java_decl(ctx, brace_depth, class_depth, current_class)


def _detect_jvm_language(filename: str) -> str:
    return "kotlin" if filename.endswith((".kt", ".kts")) else "java"


def _extract_jvm_header(
    stripped: str,
    idx: int,
    brace_depth: int,
    current_pkg: str,
) -> Tuple[str, Optional[Dict[str, Any]]]:
    if brace_depth != 0:
        return current_pkg, None
    m_pkg = _PACKAGE_PAT.match(stripped)
    if m_pkg and not current_pkg:
        return m_pkg.group(1).rstrip(";"), None
    m_imp = _IMPORT_PAT.match(stripped)
    if m_imp:
        return current_pkg, {"module": m_imp.group(1).rstrip(";"), "line": idx + 1}
    return current_pkg, None


def _sync_class_scope(current_class: Optional[str], brace_depth: int, class_depth: int) -> Optional[str]:
    if current_class and brace_depth < class_depth:
        return None
    return current_class


def parse_jvm_ast(source_text: str, filename: str = "") -> Dict[str, Any]:
    """
    Parses a Java (.java) or Kotlin (.kt, .kts) source file and extracts:
    - package declaration
    - imports
    - declarations: classes, interfaces, records, enums, objects, functions, methods
    """
    language = _detect_jvm_language(filename)
    is_kotlin = (language == "kotlin")
    clean_code = strip_jvm_comments(source_text)
    lines = clean_code.splitlines(keepends=True)
    raw_lines = source_text.splitlines(keepends=True)

    package_name = ""
    imports: List[Dict[str, Any]] = []
    declarations: List[Dict[str, Any]] = []

    brace_depth = 0
    current_class: Optional[str] = None
    class_depth = 0

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        package_name, imp = _extract_jvm_header(stripped, idx, brace_depth, package_name)
        if imp:
            imports.append(imp)
            continue

        ctx = _ParseContext(lines=lines, raw_lines=raw_lines, idx=idx, stripped=stripped)
        decl, current_class, class_depth = _process_jvm_decl(
            is_kotlin, ctx, brace_depth, class_depth, current_class
        )
        if decl:
            declarations.append(decl)

        brace_depth += line.count("{") - line.count("}")
        current_class = _sync_class_scope(current_class, brace_depth, class_depth)

    return {
        "filename": filename,
        "language": language,
        "package": package_name,
        "imports": imports,
        "declarations": declarations,
        "total_lines": len(lines),
    }


def _format_jvm_imports(imports: List[Dict[str, Any]]) -> List[str]:
    if not imports:
        return []
    summary = [f"  // Imports ({len(imports)}):"]
    for imp in imports[:8]:
        summary.append(f"  import {imp['module']};")
    if len(imports) > 8:
        summary.append(f"  // ... and {len(imports) - 8} more imports")
    return summary


def _format_jvm_declarations(decls: List[Dict[str, Any]]) -> List[str]:
    if not decls:
        return []
    summary = [f"  // Declarations ({len(decls)}):"]
    for d in decls:
        export_flag = "pub " if d.get("is_exported") else "priv"
        sig = d.get("signature") or d["name"]
        summary.append(f"  [{export_flag} {d['kind']:8}] L{d['start_line']}-{d['end_line']}: {sig}")
    return summary


def format_jvm_digest_skeleton(filepath: str) -> str:
    """Formats an architectural skeleton for a Java or Kotlin file."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError) as e:
        logger.debug("Failed to read JVM file %s: %s", filepath, e)
        return f"  [Error reading JVM: {e}]\n"

    parsed = parse_jvm_ast(content, filename=filepath)
    summary: List[str] = []

    pkg = parsed.get("package")
    if pkg:
        summary.append(f"  package {pkg};")

    summary.extend(_format_jvm_imports(parsed.get("imports", [])))
    summary.extend(_format_jvm_declarations(parsed.get("declarations", [])))

    return "\n".join(summary) + "\n"
