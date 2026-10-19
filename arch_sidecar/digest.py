import os
import ast
import re
import sys
from typing import Dict, List, Optional, Set, Any
from .utils import DEFAULT_IGNORED_DIRS, is_ignored_path, is_minified_or_binary, estimate_tokens

def _format_arg(arg: ast.arg, default: Optional[ast.AST] = None) -> str:
    """Format an ast.arg with optional type annotation and default value."""
    name = arg.arg
    ann = f": {ast.unparse(arg.annotation)}" if getattr(arg, "annotation", None) else ""
    def_val = f" = {ast.unparse(default)}" if default is not None else ""
    return f"{name}{ann}{def_val}"

def _format_function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Extract a clean, readable function signature with args and return annotation."""
    args_parts = []
    
    # Positional args and defaults
    pos_args = node.args.args
    defaults = node.args.defaults
    num_no_defaults = len(pos_args) - len(defaults)
    
    for i, arg in enumerate(pos_args):
        if i >= num_no_defaults:
            default = defaults[i - num_no_defaults]
        else:
            default = None
        args_parts.append(_format_arg(arg, default))
        
    # *args (vararg)
    if node.args.vararg:
        vararg_str = f"*{node.args.vararg.arg}"
        if getattr(node.args.vararg, "annotation", None):
            vararg_str += f": {ast.unparse(node.args.vararg.annotation)}"
        args_parts.append(vararg_str)
    elif node.args.kwonlyargs:
        args_parts.append("*")
        
    # Keyword-only args
    for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
        args_parts.append(_format_arg(arg, default))
        
    # **kwargs
    if node.args.kwarg:
        kwarg_str = f"**{node.args.kwarg.arg}"
        if getattr(node.args.kwarg, "annotation", None):
            kwarg_str += f": {ast.unparse(node.args.kwarg.annotation)}"
        args_parts.append(kwarg_str)

    prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
    ret = f" -> {ast.unparse(node.returns)}" if getattr(node, "returns", None) else ""
    return f"{prefix}{node.name}({', '.join(args_parts)}){ret}"

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
    except Exception as e:
        return f"  [Error reading file: {e}]\n"

    summary: List[str] = []
    
    # Module docstring
    docstring = ast.get_docstring(tree)
    if include_docstrings and docstring:
        first_line = docstring.strip().split("\n")[0]
        summary.append(f"  \"\"\"{first_line}\"\"\"")

    # Imports
    imports: List[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for n in node.names:
                alias = f" as {n.asname}" if n.asname else ""
                imports.append(f"{n.name}{alias}")
        elif isinstance(node, ast.ImportFrom):
            mod = ("." * node.level) + (node.module or "")
            names = ", ".join((f"{n.name} as {n.asname}" if n.asname else n.name) for n in node.names)
            imports.append(f"from {mod} import {names}")
    
    if imports:
        # Group imports cleanly
        summary.append(f"  Imports: {', '.join(imports)}")

    # Top-level assignments (constants, __all__, configs)
    constants: List[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and (target.id.isupper() or target.id == "__all__"):
                    try:
                        val_str = ast.unparse(node.value)
                        if len(val_str) < 60:
                            constants.append(f"{target.id} = {val_str}")
                        else:
                            constants.append(f"{target.id} = ...")
                    except Exception:
                        constants.append(f"{target.id}")
    if constants:
        summary.append(f"  Constants: {', '.join(constants)}")

    # Classes and Functions
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            bases = [ast.unparse(b) for b in node.bases]
            base_str = f"({', '.join(bases)})" if bases else ""
            summary.append(f"  class {node.name}{base_str}:")
            
            # Class docstring
            cls_doc = ast.get_docstring(node)
            if include_docstrings and cls_doc:
                cls_first_line = cls_doc.strip().split("\n")[0]
                summary.append(f"    \"\"\"{cls_first_line}\"\"\"")
                
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    decorators = [f"@{ast.unparse(d)}" for d in item.decorator_list if hasattr(d, "id") or hasattr(d, "attr")]
                    dec_prefix = f"[{', '.join(decorators)}] " if decorators else ""
                    sig = _format_function_signature(item)
                    summary.append(f"    {dec_prefix}{sig}")
                    
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            decorators = [f"@{ast.unparse(d)}" for d in node.decorator_list if hasattr(d, "id") or hasattr(d, "attr")]
            dec_prefix = f"[{', '.join(decorators)}] " if decorators else ""
            sig = _format_function_signature(node)
            summary.append(f"  {dec_prefix}{sig}")

    return "\n".join(summary) + "\n"

def parse_ts_js_file(filepath: str) -> str:
    """Extracts declarations and imports from TypeScript/JavaScript files."""
    summary: List[str] = []
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith(("//", "/*", "*")):
                    continue
                if re.match(r"^(export\s+)?(class|interface|type|enum|const|function|async\s+function)\s+", stripped):
                    # Clean up function body opening
                    clean = stripped.split("{")[0].strip()
                    summary.append(f"  {clean}")
                elif stripped.startswith("import ") and "from" in stripped:
                    summary.append(f"  {stripped.rstrip(';')}")
    except Exception as e:
        return f"  [Error reading JS/TS: {e}]\n"
        
    return "\n".join(summary[:30]) + ("\n  ..." if len(summary) > 30 else "") + "\n"

def parse_generic_file(filepath: str) -> str:
    """Generic fallback for other languages (Go, Rust, Dart, etc.)."""
    summary: List[str] = []
    keywords = ["fn ", "func ", "pub fn ", "struct ", "impl ", "interface ", "class ", "void ", "Future<", "package ", "import "]
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                stripped = line.strip()
                if any(stripped.startswith(k) or f" {k}" in stripped for k in keywords):
                    clean = stripped.split("{")[0].strip()
                    summary.append(f"  {clean}")
    except Exception as e:
        return f"  [Error reading: {e}]\n"
        
    return "\n".join(summary[:25]) + ("\n  ..." if len(summary) > 25 else "") + "\n"

def generate_digest(
    root_dir: str,
    max_tokens: int = 4000,
    ignored_dirs: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """
    Generates a token-efficient AST skeleton digest of a repository.
    Filters out junk folders (dist, node_modules, .git, minified files).
    """
    if ignored_dirs is None:
        ignored_dirs = DEFAULT_IGNORED_DIRS

    root_abs = os.path.abspath(root_dir)
    root_name = os.path.basename(root_abs)
    
    output: List[str] = []
    output.append(f"# Architectural Digest: {root_name}")
    output.append(f"# Root: {root_abs}\n")

    files_indexed = 0
    scanned_extensions = {".py", ".ts", ".js", ".jsx", ".tsx", ".dart", ".go", ".rs", ".java", ".kt"}

    for root, dirs, files in os.walk(root_abs):
        # Filter directories in-place
        dirs[:] = [
            d for d in dirs 
            if not is_ignored_path(os.path.relpath(os.path.join(root, d), root_abs), ignored_dirs)
        ]
        
        # Sort files for deterministic output
        files.sort()
        for file in files:
            rel_path = os.path.relpath(os.path.join(root, file), root_abs)
            
            if is_ignored_path(rel_path, ignored_dirs):
                continue
                
            _, ext = os.path.splitext(file)
            if ext.lower() not in scanned_extensions:
                continue

            full_path = os.path.join(root, file)
            if is_minified_or_binary(full_path):
                continue

            output.append(f"--- File: {rel_path} ---")
            files_indexed += 1
            
            if ext.lower() == ".py":
                output.append(parse_python_file(full_path))
            elif ext.lower() in {".ts", ".js", ".tsx", ".jsx"}:
                output.append(parse_ts_js_file(full_path))
            else:
                output.append(parse_generic_file(full_path))

    digest_text = "\n".join(output)
    token_est = estimate_tokens(digest_text)

    # If exceeding max_tokens, prune cleanly while preserving top-level map
    if token_est > max_tokens:
        lines = digest_text.splitlines()
        pruned_lines = []
        running_len = 0
        target_char_limit = max_tokens * 4
        
        for line in lines:
            if running_len + len(line) + 1 > target_char_limit:
                pruned_lines.append("\n# [Digest truncated: reached max_tokens budget]")
                break
            pruned_lines.append(line)
            running_len += len(line) + 1
            
        digest_text = "\n".join(pruned_lines)
        token_est = estimate_tokens(digest_text)

    return {
        "root_dir": root_abs,
        "files_indexed": files_indexed,
        "estimated_tokens": token_est,
        "digest": digest_text
    }

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    result = generate_digest(target)
    out_file = "codebase_digest.txt"
    with open(out_file, "w", encoding="utf-8") as out:
        out.write(result["digest"])
    print(f"Done. Digest written to {out_file} ({result['files_indexed']} files, ~{result['estimated_tokens']} tokens).")