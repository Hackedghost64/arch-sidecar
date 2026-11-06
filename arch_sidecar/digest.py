import os
import ast
import logging
import sys
from typing import Dict, List, Optional, Set, Any, Tuple
from .utils import DEFAULT_IGNORED_DIRS, is_ignored_path, is_minified_or_binary, estimate_tokens
from .ts_ast import format_ts_digest_skeleton
from .go_ast import format_go_digest_skeleton
from .rust_ast import format_rust_digest_skeleton
from .dart_ast import format_dart_digest_skeleton
from .jvm_ast import format_jvm_digest_skeleton

logger = logging.getLogger(__name__)

SCANNED_EXTENSIONS = {
    ".py",
    ".ts", ".js", ".jsx", ".tsx",
    ".dart",
    ".go", ".rs",
    ".java", ".kt", ".kts",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx"
}

C_CPP_KEYWORDS = (
    "#include", "#define", "struct ", "typedef struct", "enum ", "union ",
    "class ", "namespace ", "extern ", "template<", "void ", "int ", "char ",
    "bool ", "float ", "double ", "uint8_t", "uint16_t", "uint32_t", "uint64_t",
    "int32_t", "int64_t", "size_t", "static "
)

GENERIC_KEYWORDS = (
    "fn ", "func ", "pub fn ", "struct ", "impl ", "interface ",
    "class ", "void ", "Future<", "package ", "import "
)


def _format_arg(arg: ast.arg, default: Optional[ast.AST] = None) -> str:
    """Format an ast.arg with optional type annotation and default value."""
    name = arg.arg
    ann = f": {ast.unparse(arg.annotation)}" if getattr(arg, "annotation", None) else ""
    def_val = f" = {ast.unparse(default)}" if default is not None else ""
    return f"{name}{ann}{def_val}"


def _format_positional_args(args_node: ast.arguments) -> List[str]:
    posonly = list(getattr(args_node, "posonlyargs", []))
    all_pos = posonly + list(args_node.args)
    defaults = list(args_node.defaults)
    no_def_count = len(all_pos) - len(defaults)

    parts: List[str] = []
    for i, arg in enumerate(all_pos):
        default = defaults[i - no_def_count] if i >= no_def_count else None
        parts.append(_format_arg(arg, default))
        if posonly and i == len(posonly) - 1:
            parts.append("/")
    return parts


def _format_vararg(vararg: Optional[ast.arg], has_kwonly: bool) -> Optional[str]:
    if vararg:
        ann = getattr(vararg, "annotation", None)
        ann_str = f": {ast.unparse(ann)}" if ann else ""
        return f"*{vararg.arg}{ann_str}"
    if has_kwonly:
        return "*"
    return None


def _format_kwonly_args(args_node: ast.arguments) -> List[str]:
    return [
        _format_arg(arg, default)
        for arg, default in zip(args_node.kwonlyargs, args_node.kw_defaults)
    ]


def _format_kwarg(kwarg: Optional[ast.arg]) -> Optional[str]:
    if not kwarg:
        return None
    ann = getattr(kwarg, "annotation", None)
    ann_str = f": {ast.unparse(ann)}" if ann else ""
    return f"**{kwarg.arg}{ann_str}"


def _format_function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Extract a clean, readable function signature with args and return annotation."""
    args_node = node.args
    args_parts = _format_positional_args(args_node)

    vararg_str = _format_vararg(args_node.vararg, bool(args_node.kwonlyargs))
    if vararg_str:
        args_parts.append(vararg_str)

    args_parts.extend(_format_kwonly_args(args_node))

    kwarg_str = _format_kwarg(args_node.kwarg)
    if kwarg_str:
        args_parts.append(kwarg_str)

    prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
    ret_ann = getattr(node, "returns", None)
    ret_str = f" -> {ast.unparse(ret_ann)}" if ret_ann else ""
    return f"{prefix}{node.name}({', '.join(args_parts)}){ret_str}"


def _format_import_node(node: ast.AST) -> List[str]:
    if isinstance(node, ast.Import):
        return [
            f"{n.name} as {n.asname}" if n.asname else n.name
            for n in node.names
        ]
    if isinstance(node, ast.ImportFrom):
        mod = ("." * node.level) + (node.module or "")
        names = ", ".join(
            (f"{n.name} as {n.asname}" if n.asname else n.name)
            for n in node.names
        )
        return [f"from {mod} import {names}"]
    return []


def _extract_py_imports(tree: ast.Module) -> List[str]:
    imports: List[str] = []
    for node in tree.body:
        imports.extend(_format_import_node(node))
    return imports


def _format_constant_target(target_name: str, val_node: ast.AST) -> str:
    try:
        val_str = ast.unparse(val_node)
        val_display = val_str if len(val_str) < 60 else "..."
        return f"{target_name} = {val_display}"
    except (ValueError, SyntaxError, AttributeError) as e:
        logger.debug("Could not unparse constant %s: %s", target_name, e)
        return target_name
    except Exception as e:
        logger.debug("Unexpected error unparsing constant %s: %s", target_name, e)
        return target_name


def _extract_py_constants(tree: ast.Module) -> List[str]:
    constants: List[str] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and (target.id.isupper() or target.id == "__all__"):
                constants.append(_format_constant_target(target.id, node.value))
    return constants


def _format_decorators(decorator_list: List[ast.AST]) -> str:
    if not decorator_list:
        return ""
    decorators = [f"@{ast.unparse(d)}" for d in decorator_list]
    return f"[{', '.join(decorators)}] "


def _format_class_header(node: ast.ClassDef) -> str:
    dec_prefix = _format_decorators(node.decorator_list)
    bases = [ast.unparse(b) for b in node.bases]
    base_str = f"({', '.join(bases)})" if bases else ""
    return f"  {dec_prefix}class {node.name}{base_str}:"


def _format_class_body(node: ast.ClassDef, include_docstrings: bool) -> List[str]:
    lines: List[str] = []
    doc = ast.get_docstring(node)
    if include_docstrings and doc:
        first_line = doc.strip().split("\n")[0]
        lines.append(f"    \"\"\"{first_line}\"\"\"")

    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            dec_prefix = _format_decorators(item.decorator_list)
            sig = _format_function_signature(item)
            lines.append(f"    {dec_prefix}{sig}")
    return lines


def _format_function_item(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    dec_prefix = _format_decorators(node.decorator_list)
    sig = _format_function_signature(node)
    return f"  {dec_prefix}{sig}"


def _extract_py_declarations(tree: ast.Module, include_docstrings: bool) -> List[str]:
    lines: List[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            lines.append(_format_class_header(node))
            lines.extend(_format_class_body(node, include_docstrings))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lines.append(_format_function_item(node))
    return lines


def parse_python_file(filepath: str, include_docstrings: bool = True) -> str:
    """
    Extracts imports, constants, classes (with bases and methods),
    and function signatures from a Python file into an AST skeleton.
    """
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            code = f.read()
            tree = ast.parse(code, filename=filepath)
    except SyntaxError:
        return "  [SyntaxError parsing file]\n"
    except (OSError, UnicodeDecodeError) as e:
        logger.debug("Failed reading python file %s: %s", filepath, e)
        return f"  [Error reading file: {e}]\n"
    except Exception as e:
        logger.debug("Unexpected error parsing python file %s: %s", filepath, e)
        return f"  [Error reading file: {e}]\n"

    summary: List[str] = []
    docstring = ast.get_docstring(tree)
    if include_docstrings and docstring:
        first_line = docstring.strip().split("\n")[0]
        summary.append(f"  \"\"\"{first_line}\"\"\"")

    imports = _extract_py_imports(tree)
    if imports:
        summary.append(f"  Imports: {', '.join(imports)}")

    constants = _extract_py_constants(tree)
    if constants:
        summary.append(f"  Constants: {', '.join(constants)}")

    summary.extend(_extract_py_declarations(tree, include_docstrings))
    return "\n".join(summary) + "\n"


def parse_ts_js_file(filepath: str) -> str:
    """Extracts declarations, interfaces, types, and imports from TypeScript/JavaScript files."""
    return format_ts_digest_skeleton(filepath)


def parse_jvm_file(filepath: str) -> str:
    """Extract declarations, packages, and imports from Java or Kotlin files."""
    return format_jvm_digest_skeleton(filepath)


def parse_dart_file(filepath: str) -> str:
    """Extract declarations, classes, and directives from Dart files."""
    return format_dart_digest_skeleton(filepath)


def _is_c_cpp_comment(line: str) -> bool:
    return line.startswith("//") or line.startswith("/*") or line.startswith("*")


def _clean_c_cpp_line(line: str, keywords: Tuple[str, ...]) -> Optional[str]:
    stripped = line.strip()
    if not stripped or _is_c_cpp_comment(stripped):
        return None
    matches = any(stripped.startswith(k) or f" {k}" in stripped for k in keywords)
    if not matches:
        return None
    clean = stripped.split("{")[0].strip()
    if clean.endswith(";"):
        clean = clean[:-1].strip()
    return clean or None


def _collect_c_cpp_lines(filepath: str) -> List[str]:
    summary: List[str] = []
    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            clean = _clean_c_cpp_line(line, C_CPP_KEYWORDS)
            if clean:
                summary.append(f"  {clean}")
    return summary


def parse_c_cpp_file(filepath: str) -> str:
    """Extract declarations, structs, and definitions from C/C++ files."""
    try:
        summary = _collect_c_cpp_lines(filepath)
    except (OSError, UnicodeDecodeError) as e:
        logger.debug("Failed reading C/C++ file %s: %s", filepath, e)
        return f"  [Error reading C/C++: {e}]\n"
    except Exception as e:
        logger.debug("Unexpected error reading C/C++ file %s: %s", filepath, e)
        return f"  [Error reading C/C++: {e}]\n"

    trunc = "\n  ..." if len(summary) > 30 else ""
    return "\n".join(summary[:30]) + trunc + "\n"


def parse_go_file(filepath: str) -> str:
    """Extract package, imports, and declarations from Go files."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        return format_go_digest_skeleton(content, filename=filepath) + "\n"
    except (OSError, UnicodeDecodeError) as e:
        logger.debug("Failed reading Go file %s: %s", filepath, e)
        return f"  [Error reading Go: {e}]\n"
    except Exception as e:
        logger.debug("Unexpected error reading Go file %s: %s", filepath, e)
        return f"  [Error reading Go: {e}]\n"


def parse_rust_file(filepath: str) -> str:
    """Extract use statements, structs, traits, and functions from Rust files."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        return format_rust_digest_skeleton(content, filename=filepath) + "\n"
    except (OSError, UnicodeDecodeError) as e:
        logger.debug("Failed reading Rust file %s: %s", filepath, e)
        return f"  [Error reading Rust: {e}]\n"
    except Exception as e:
        logger.debug("Unexpected error reading Rust file %s: %s", filepath, e)
        return f"  [Error reading Rust: {e}]\n"


def _clean_generic_line(line: str, keywords: Tuple[str, ...]) -> Optional[str]:
    stripped = line.strip()
    if not stripped:
        return None
    matches = any(stripped.startswith(k) or f" {k}" in stripped for k in keywords)
    if not matches:
        return None
    return stripped.split("{")[0].strip() or None


def _collect_generic_lines(filepath: str) -> List[str]:
    summary: List[str] = []
    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            clean = _clean_generic_line(line, GENERIC_KEYWORDS)
            if clean:
                summary.append(f"  {clean}")
    return summary


def parse_generic_file(filepath: str) -> str:
    """Generic fallback for other languages."""
    try:
        summary = _collect_generic_lines(filepath)
    except (OSError, UnicodeDecodeError) as e:
        logger.debug("Failed reading generic file %s: %s", filepath, e)
        return f"  [Error reading: {e}]\n"
    except Exception as e:
        logger.debug("Unexpected error reading generic file %s: %s", filepath, e)
        return f"  [Error reading: {e}]\n"

    trunc = "\n  ..." if len(summary) > 25 else ""
    return "\n".join(summary[:25]) + trunc + "\n"


def _parse_file_by_extension(path: str, ext: str) -> str:
    low = ext.lower()
    if low == ".py":
        return parse_python_file(path)
    if low in {".ts", ".js", ".tsx", ".jsx"}:
        return parse_ts_js_file(path)
    if low == ".go":
        return parse_go_file(path)
    if low == ".rs":
        return parse_rust_file(path)
    if low == ".dart":
        return parse_dart_file(path)
    if low in {".java", ".kt", ".kts"}:
        return parse_jvm_file(path)
    if low in {".c", ".h", ".cpp", ".hpp", ".cc", ".cxx"}:
        return parse_c_cpp_file(path)
    return parse_generic_file(path)


def _digest_single_file(path: str) -> Tuple[List[str], int]:
    file = os.path.basename(path)
    _, ext = os.path.splitext(file)
    if ext.lower() not in SCANNED_EXTENSIONS or is_minified_or_binary(path):
        return [], 0
    return [f"--- File: {file} ---", _parse_file_by_extension(path, ext)], 1


def _should_skip_file(
    file: str,
    rel_path: str,
    full_path: str,
    ignored_dirs: Set[str]
) -> bool:
    if is_ignored_path(rel_path, ignored_dirs):
        return True
    _, ext = os.path.splitext(file)
    if ext.lower() not in SCANNED_EXTENSIONS:
        return True
    return is_minified_or_binary(full_path)


def _walk_repository(root_abs: str, ignored_dirs: Set[str]) -> Tuple[List[str], int]:
    output: List[str] = []
    files_indexed = 0

    for root, dirs, files in os.walk(root_abs):
        dirs[:] = [
            d for d in dirs
            if not is_ignored_path(os.path.relpath(os.path.join(root, d), root_abs), ignored_dirs)
        ]
        files.sort()
        for file in files:
            rel_path = os.path.relpath(os.path.join(root, file), root_abs)
            full_path = os.path.join(root, file)
            if _should_skip_file(file, rel_path, full_path, ignored_dirs):
                continue

            _, ext = os.path.splitext(file)
            output.append(f"--- File: {rel_path} ---")
            output.append(_parse_file_by_extension(full_path, ext))
            files_indexed += 1

    return output, files_indexed


def _prune_digest(digest_text: str, max_tokens: int) -> Tuple[str, int]:
    lines = digest_text.splitlines()
    pruned_lines: List[str] = []
    running_len = 0
    target_char_limit = max_tokens * 4

    for line in lines:
        if running_len + len(line) + 1 > target_char_limit:
            pruned_lines.append("\n# [Digest truncated: reached max_tokens budget]")
            break
        pruned_lines.append(line)
        running_len += len(line) + 1

    pruned_text = "\n".join(pruned_lines)
    return pruned_text, estimate_tokens(pruned_text)


def generate_digest(
    root_dir: str,
    max_tokens: int = 4000,
    ignored_dirs: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """
    Generates a token-efficient AST skeleton digest of a repository.
    Filters out junk folders (dist, node_modules, .git, minified files).
    """
    active_ignored = DEFAULT_IGNORED_DIRS if ignored_dirs is None else ignored_dirs
    root_abs = os.path.abspath(root_dir)
    root_name = os.path.basename(root_abs)

    output = [
        f"# Architectural Digest: {root_name}",
        f"# Root: {root_abs}\n",
    ]

    if os.path.isfile(root_abs):
        file_lines, files_indexed = _digest_single_file(root_abs)
        output.extend(file_lines)
    else:
        repo_lines, files_indexed = _walk_repository(root_abs, active_ignored)
        output.extend(repo_lines)

    digest_text = "\n".join(output)
    token_est = estimate_tokens(digest_text)
    if token_est > max_tokens:
        digest_text, token_est = _prune_digest(digest_text, max_tokens)

    return {
        "root_dir": root_abs,
        "files_indexed": files_indexed,
        "estimated_tokens": token_est,
        "digest": digest_text,
    }


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    result = generate_digest(target)
    out_file = "codebase_digest.txt"
    with open(out_file, "w", encoding="utf-8") as out:
        out.write(result["digest"])
    print(f"Done. Digest written to {out_file} ({result['files_indexed']} files, ~{result['estimated_tokens']} tokens).")
