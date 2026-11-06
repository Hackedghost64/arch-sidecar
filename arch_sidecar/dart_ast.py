"""
Dart AST & Lexical Analysis Engine for Devrith.

Provides:
1. High-fidelity lexical parsing for Dart (.dart) files.
2. Symbol extraction: classes, mixins, extensions, enums, typedefs, functions, and class methods.
3. Import, export, library, and part directive mapping.
4. Clean architectural skeleton formatting for digests.
"""

import re
import logging
from dataclasses import dataclass
from typing import Dict, List, Any, Optional, Tuple

logger = logging.getLogger(__name__)

_DART_TOKEN_PATTERN = re.compile(
    r'(r"""[\s\S]*?""")|'
    r"(r'''[\s\S]*?''')|"
    r'("""[\s\S]*?""")|'
    r"('''[\s\S]*?''')|"
    r'(r"[^\n"]*")|'
    r"(r'[^\n']*')|"
    r'("(\\.|[^"\\])*")|'
    r"('(\\.|[^'\\])*')|"
    r"(//[^\n]*)|"
    r"(/\*[\s\S]*?\*/)"
)

_IMPORT_PAT = re.compile(
    r"^import\s+['\"]([^'\"]+)['\"](?:\s+as\s+([a-zA-Z0-9_$]+))?(?:\s+(?:show|hide)\s+([^;]+))?;"
)
_EXPORT_PAT = re.compile(
    r"^export\s+['\"]([^'\"]+)['\"](?:\s+(?:show|hide)\s+([^;]+))?;"
)
_LIB_PAT = re.compile(r"^library\s+([a-zA-Z0-9_.]+);")

_CLASS_PAT = re.compile(r"^(?:abstract\s+|sealed\s+|base\s+|interface\s+|final\s+)?class\s+([a-zA-Z0-9_$]+)")
_MIXIN_PAT = re.compile(r"^mixin\s+([a-zA-Z0-9_$]+)")
_EXTENSION_PAT = re.compile(r"^extension\s+([a-zA-Z0-9_$]+)?\s*on\s+")
_ENUM_PAT = re.compile(r"^enum\s+([a-zA-Z0-9_$]+)")
_TYPEDEF_PAT = re.compile(r"^typedef\s+([a-zA-Z0-9_$]+)")
_FUNC_PAT = re.compile(r"^(?:[a-zA-Z0-9_<>,?\s]+\s+)?([a-zA-Z0-9_$]+)\s*\(([^)]*)\)\s*(?:async\s*)?(?:=>|\{)")
_CONST_PAT = re.compile(r"^(?:const|final|var)\s+(?:[a-zA-Z0-9_<>,?\s]+\s+)?([a-zA-Z0-9_$]+)\s*=")

_DART_CONTROL_FLOW = ("if", "for", "while", "switch", "return")
_DART_METHOD_EXCLUSIONS = ("if", "for", "while", "switch", "return", "case", "catch", "finally")

_DART_TYPE_PATTERNS = (
    ("class", _CLASS_PAT),
    ("mixin", _MIXIN_PAT),
    ("extension", _EXTENSION_PAT),
    ("enum", _ENUM_PAT),
    ("typedef", _TYPEDEF_PAT),
)


@dataclass(frozen=True)
class _ParseContext:
    lines: List[str]
    raw_lines: List[str]
    idx: int
    stripped: str


def _strip_dart_comment_match(match: re.Match) -> str:
    text = match.group(0)
    if text.startswith("//"):
        return ""
    if text.startswith("/*"):
        return "\n" * text.count("\n")
    return text


def strip_dart_comments(code: str) -> str:
    """Strips single-line and multi-line comments from Dart code while preserving string literals."""
    return _DART_TOKEN_PATTERN.sub(_strip_dart_comment_match, code)


def _find_dart_decl_start(raw_lines: List[str], idx: int) -> int:
    k = idx - 1
    while k >= 0:
        prev = raw_lines[k].strip()
        if not (prev.startswith("///") or prev.startswith("@")):
            break
        k -= 1
    return k + 1


def _find_dart_block_end(lines: List[str], start_idx: int) -> int:
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


def _is_dart_func_end_line(line: str, found_brace: bool) -> bool:
    if found_brace:
        return False
    return ";" in line or "=>" in line


def _find_dart_func_end(lines: List[str], start_idx: int) -> int:
    brace_count = 0
    found_brace = False
    for j in range(start_idx, len(lines)):
        line = lines[j]
        brace_count += line.count("{") - line.count("}")
        if "{" in line:
            found_brace = True
        if found_brace and brace_count <= 0:
            return j
        if _is_dart_func_end_line(line, found_brace):
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


def _match_dart_type(stripped: str, idx: int) -> Optional[Tuple[str, str]]:
    for kind, pat in _DART_TYPE_PATTERNS:
        m = pat.match(stripped)
        if m:
            name = m.group(1) or f"Extension_{idx + 1}"
            return kind, name
    return None


def _parse_dart_type_decl(
    lines: List[str],
    raw_lines: List[str],
    idx: int,
    stripped: str,
) -> Optional[Tuple[Dict[str, Any], Optional[str]]]:
    matched = _match_dart_type(stripped, idx)
    if not matched:
        return None
    kind, name = matched
    start_idx = _find_dart_decl_start(raw_lines, idx)
    end_idx = _find_dart_block_end(lines, idx)
    sig = stripped.split("{")[0].strip().rstrip(";")
    span_code = _extract_span_code(raw_lines, idx, start_idx, end_idx)
    decl = _build_declaration(kind, name, not name.startswith("_"), sig, span_code)
    new_class = name if kind in ("class", "mixin", "extension", "enum") else None
    return decl, new_class


def _parse_dart_function(
    lines: List[str],
    raw_lines: List[str],
    idx: int,
    stripped: str,
) -> Optional[Dict[str, Any]]:
    if stripped.startswith(_DART_CONTROL_FLOW):
        return None
    m = _FUNC_PAT.match(stripped)
    if not m:
        return None
    name = m.group(1)
    if name in _DART_CONTROL_FLOW:
        return None
    start_idx = _find_dart_decl_start(raw_lines, idx)
    end_idx = _find_dart_func_end(lines, idx)
    sig = stripped.split("{")[0].strip().rstrip(";")
    span_code = _extract_span_code(raw_lines, idx, start_idx, end_idx)
    return _build_declaration("function", name, not name.startswith("_"), sig, span_code)


def _parse_dart_const(
    raw_lines: List[str],
    idx: int,
    stripped: str,
) -> Optional[Dict[str, Any]]:
    m = _CONST_PAT.match(stripped)
    if not m:
        return None
    name = m.group(1)
    span_code = (idx + 1, idx + 1, idx + 1, raw_lines[idx])
    return _build_declaration("const", name, not name.startswith("_"), stripped.rstrip(";"), span_code)


def _parse_dart_method(
    lines: List[str],
    raw_lines: List[str],
    idx: int,
    stripped: str,
    current_class: str,
) -> Optional[Dict[str, Any]]:
    if stripped.startswith(_DART_METHOD_EXCLUSIONS):
        return None
    m = _FUNC_PAT.match(stripped)
    if not m:
        return None
    name = m.group(1)
    if name in _DART_METHOD_EXCLUSIONS:
        return None
    end_idx = _find_dart_func_end(lines, idx)
    sig = stripped.split("{")[0].strip().rstrip(";")
    span_code = (idx + 1, idx + 1, end_idx + 1, "".join(raw_lines[idx : end_idx + 1]))
    return _build_declaration("method", f"{current_class}.{name}", not name.startswith("_"), sig, span_code)


def _parse_dart_directive(
    stripped: str,
    idx: int,
) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    m_lib = _LIB_PAT.match(stripped)
    if m_lib:
        return m_lib.group(1), None, None

    m_imp = _IMPORT_PAT.match(stripped)
    if m_imp:
        imp = {
            "module": m_imp.group(1),
            "alias": m_imp.group(2),
            "filter": m_imp.group(3),
            "line": idx + 1,
        }
        return None, imp, None

    m_exp = _EXPORT_PAT.match(stripped)
    if m_exp:
        exp = {
            "module": m_exp.group(1),
            "filter": m_exp.group(2),
            "line": idx + 1,
        }
        return None, None, exp

    return None, None, None


def _consume_directive(
    stripped: str,
    idx: int,
    brace_depth: int,
    library_name: Optional[str],
) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[Dict[str, Any]], bool]:
    if brace_depth != 0:
        return library_name, None, None, False
    lib, imp, exp = _parse_dart_directive(stripped, idx)
    if lib:
        return (lib if not library_name else library_name), None, None, True
    if imp:
        return library_name, imp, None, True
    if exp:
        return library_name, None, exp, True
    return library_name, None, None, False


def _dispatch_dart_top_level(
    ctx: _ParseContext,
    brace_depth: int,
    class_depth: int,
    current_class: Optional[str],
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    type_res = _parse_dart_type_decl(ctx.lines, ctx.raw_lines, ctx.idx, ctx.stripped)
    if type_res:
        decl, new_class = type_res
        if new_class:
            return decl, new_class, brace_depth + 1
        return decl, current_class, class_depth

    func_decl = _parse_dart_function(ctx.lines, ctx.raw_lines, ctx.idx, ctx.stripped)
    if func_decl:
        return func_decl, current_class, class_depth

    const_decl = _parse_dart_const(ctx.raw_lines, ctx.idx, ctx.stripped)
    if const_decl:
        return const_decl, current_class, class_depth

    return None, current_class, class_depth


def _dispatch_dart_decl(
    ctx: _ParseContext,
    brace_depth: int,
    class_depth: int,
    current_class: Optional[str],
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    if brace_depth == 0:
        return _dispatch_dart_top_level(ctx, brace_depth, class_depth, current_class)

    if current_class and brace_depth == class_depth:
        meth_decl = _parse_dart_method(ctx.lines, ctx.raw_lines, ctx.idx, ctx.stripped, current_class)
        if meth_decl:
            return meth_decl, current_class, class_depth

    return None, current_class, class_depth


def _sync_class_scope(current_class: Optional[str], brace_depth: int, class_depth: int) -> Optional[str]:
    if current_class and brace_depth < class_depth:
        return None
    return current_class


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

    brace_depth = 0
    current_class: Optional[str] = None
    class_depth = 0

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        library_name, imp, exp, handled = _consume_directive(stripped, idx, brace_depth, library_name)
        if handled:
            if imp:
                imports.append(imp)
            elif exp:
                exports.append(exp)
            continue

        ctx = _ParseContext(lines=lines, raw_lines=raw_lines, idx=idx, stripped=stripped)
        decl, current_class, class_depth = _dispatch_dart_decl(
            ctx, brace_depth, class_depth, current_class
        )
        if decl:
            declarations.append(decl)

        brace_depth += line.count("{") - line.count("}")
        current_class = _sync_class_scope(current_class, brace_depth, class_depth)

    return {
        "filename": filename,
        "library": library_name,
        "imports": imports,
        "exports": exports,
        "declarations": declarations,
        "total_lines": len(lines),
    }


def _format_dart_imports(imports: List[Dict[str, Any]]) -> List[str]:
    if not imports:
        return []
    summary = [f"  // Imports ({len(imports)}):"]
    for imp in imports[:8]:
        alias_str = f" as {imp['alias']}" if imp.get("alias") else ""
        summary.append(f"  import '{imp['module']}'{alias_str};")
    if len(imports) > 8:
        summary.append(f"  // ... and {len(imports) - 8} more imports")
    return summary


def _format_dart_declarations(decls: List[Dict[str, Any]]) -> List[str]:
    if not decls:
        return []
    summary = [f"  // Declarations ({len(decls)}):"]
    for d in decls:
        export_flag = "pub " if d.get("is_exported") else "priv"
        sig = d.get("signature") or d["name"]
        summary.append(f"  [{export_flag} {d['kind']:8}] L{d['start_line']}-{d['end_line']}: {sig}")
    return summary


def format_dart_digest_skeleton(filepath: str) -> str:
    """Formats an architectural skeleton for a Dart file."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError) as e:
        logger.debug("Failed to read Dart file %s: %s", filepath, e)
        return f"  [Error reading Dart: {e}]\n"

    parsed = parse_dart_ast(content, filename=filepath)
    summary: List[str] = []

    lib = parsed.get("library")
    if lib:
        summary.append(f"  library {lib};")

    summary.extend(_format_dart_imports(parsed.get("imports", [])))
    summary.extend(_format_dart_declarations(parsed.get("declarations", [])))

    return "\n".join(summary) + "\n"
