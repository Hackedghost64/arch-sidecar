import os
import ast
import re
from typing import Dict, List, Optional, Set, Any, Tuple
from .utils import DEFAULT_IGNORED_DIRS, is_ignored_path, estimate_tokens
from .ts_ast import parse_ts_js_ast
from .go_ast import parse_go_ast
from .rust_ast import parse_rust_ast
from .dart_ast import parse_dart_ast
from .jvm_ast import parse_jvm_ast

class SymbolFinder(ast.NodeVisitor):
    def __init__(self, filename: str, lines: List[str]):
        self.filename = filename
        self.lines = lines
        self.symbols: Dict[str, Dict[str, Any]] = {}
        self.imports: List[str] = []
        self.constants: List[str] = []

    def visit_Assign(self, node: ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and (target.id.isupper() or target.id == "__all__"):
                self.constants.append(target.id)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import):
        start = node.lineno - 1
        end = getattr(node, "end_lineno", node.lineno)
        self.imports.append("".join(self.lines[start:end]))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        start = node.lineno - 1
        end = getattr(node, "end_lineno", node.lineno)
        self.imports.append("".join(self.lines[start:end]))
        self.generic_visit(node)

    def _get_start_line(self, node: ast.AST) -> int:
        dec_lines = [d.lineno for d in getattr(node, "decorator_list", []) if hasattr(d, "lineno")]
        return min([node.lineno] + dec_lines) if dec_lines else node.lineno

    def visit_ClassDef(self, node: ast.ClassDef):
        start_line = self._get_start_line(node)
        start = start_line - 1
        end = getattr(node, "end_lineno", node.lineno)
        code = "".join(self.lines[start:end])
        self.symbols[node.name] = {
            "type": "class",
            "name": node.name,
            "filename": self.filename,
            "start_line": start_line,
            "end_line": end,
            "code": code,
            "ast_node": node,
        }
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef):
        start_line = self._get_start_line(node)
        start = start_line - 1
        end = getattr(node, "end_lineno", node.lineno)
        code = "".join(self.lines[start:end])
        self.symbols[node.name] = {
            "type": "function",
            "name": node.name,
            "filename": self.filename,
            "start_line": start_line,
            "end_line": end,
            "code": code,
            "ast_node": node,
        }
        # Do not recurse into inner functions for top-level map, but generic_visit is fine

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        start_line = self._get_start_line(node)
        start = start_line - 1
        end = getattr(node, "end_lineno", node.lineno)
        code = "".join(self.lines[start:end])
        self.symbols[node.name] = {
            "type": "async_function",
            "name": node.name,
            "filename": self.filename,
            "start_line": start_line,
            "end_line": end,
            "code": code,
            "ast_node": node,
        }

def find_references_in_file(filepath: str, symbol_names: Set[str]) -> List[Dict[str, Any]]:
    """Find lines in a file that reference the given symbol names."""
    if not symbol_names or not os.path.isfile(filepath):
        return []

    pattern = re.compile(rf"\b({'|'.join(re.escape(s) for s in symbol_names)})\b")
    results = []
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            for i, line in enumerate(f, 1):
                match = pattern.search(line)
                if match:
                    results.append({
                        "line_num": i,
                        "line": line.strip(),
                        "symbol": match.group(1),
                    })
    except (OSError, UnicodeDecodeError):
        pass
    return results

def locate_surgical_context(
    task: str,
    root_dir: str = ".",
    target_symbols: Optional[List[str]] = None,
    max_tokens: int = 3500,
) -> Dict[str, Any]:
    """
    Given a task description or query, scans the project AST and dependency graph
    to extract ONLY the exact function/class definitions, their required imports,
    and references across the repo.
    """
    root_abs = os.path.abspath(root_dir)
    single_file_target: Optional[str] = None
    if os.path.isfile(root_abs):
        single_file_target = root_abs
        root_abs = os.path.dirname(root_abs)
    
    # Extract candidate symbol names from task query if not explicitly passed
    symbols_to_find = set(target_symbols or [])
    if not symbols_to_find:
        # Match PascalCase, snake_case identifiers from task text
        tokens = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b", task)
        # Filter common words
        common_stop = {"the", "and", "from", "for", "with", "into", "class", "def", "file", "code", "module", "target", "extract", "decouple", "right", "now", "here"}
        symbols_to_find = {t for t in tokens if t.lower() not in common_stop}

    matched_symbols: Dict[str, Dict[str, Any]] = {}
    file_imports_map: Dict[str, List[str]] = {}
    all_source_files: List[Tuple[str, str]] = []

    file_docstrings_map: Dict[str, Optional[str]] = {}
    file_constants_map: Dict[str, List[str]] = {}

    def _index_py_file(rel_path: str, full_path: str):
        all_source_files.append((rel_path, full_path))
        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as file_obj:
                lines = file_obj.readlines()
            tree = ast.parse("".join(lines), filename=full_path)
            finder = SymbolFinder(rel_path, lines)
            finder.visit(tree)
            file_imports_map[rel_path] = finder.imports
            file_docstrings_map[rel_path] = ast.get_docstring(tree)
            file_constants_map[rel_path] = finder.constants

            for sym_name, sym_info in finder.symbols.items():
                if sym_name in symbols_to_find or any(s.lower() == sym_name.lower() for s in symbols_to_find):
                    sym_info["lang"] = "python"
                    matched_symbols[sym_name] = sym_info
        except (SyntaxError, OSError, UnicodeDecodeError):
            pass

    def _index_ts_file(rel_path: str, full_path: str):
        all_source_files.append((rel_path, full_path))
        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as file_obj:
                content = file_obj.read()
            parsed = parse_ts_js_ast(content, filename=rel_path)
            imports = [f"import {imp['module']}" for imp in parsed.get("imports", [])[:10]]
            file_imports_map[rel_path] = imports
            lang = "typescript" if rel_path.endswith((".ts", ".tsx")) else "javascript"
            for decl in parsed.get("declarations", []):
                sym_name = decl["name"]
                if sym_name in symbols_to_find or any(s.lower() == sym_name.lower() for s in symbols_to_find):
                    matched_symbols[sym_name] = {
                        "type": decl["kind"],
                        "name": sym_name,
                        "filename": rel_path,
                        "start_line": decl.get("start_line", decl.get("line", 1)),
                        "end_line": decl.get("end_line", decl.get("line", 1)),
                        "code": decl.get("code", decl.get("signature", "")),
                        "lang": lang
                    }
        except (OSError, UnicodeDecodeError):
            pass

    def _index_go_file(rel_path: str, full_path: str):
        all_source_files.append((rel_path, full_path))
        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as file_obj:
                content = file_obj.read()
            parsed = parse_go_ast(content, filename=rel_path)
            imports = [f"import \"{imp['module']}\"" for imp in parsed.get("imports", [])[:10]]
            file_imports_map[rel_path] = imports
            for decl in parsed.get("declarations", []):
                sym_name = decl["name"]
                if sym_name in symbols_to_find or any(s.lower() == sym_name.lower() for s in symbols_to_find):
                    matched_symbols[sym_name] = {
                        "type": decl["kind"],
                        "name": sym_name,
                        "filename": rel_path,
                        "start_line": decl["start_line"],
                        "end_line": decl["end_line"],
                        "code": decl["code"],
                        "lang": "go"
                    }
        except (OSError, UnicodeDecodeError):
            pass

    def _index_rs_file(rel_path: str, full_path: str):
        all_source_files.append((rel_path, full_path))
        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as file_obj:
                content = file_obj.read()
            parsed = parse_rust_ast(content, filename=rel_path)
            imports = [f"use {imp['module']};" for imp in parsed.get("imports", [])[:10]]
            file_imports_map[rel_path] = imports
            for decl in parsed.get("declarations", []):
                sym_name = decl["name"]
                if sym_name in symbols_to_find or any(s.lower() == sym_name.lower() for s in symbols_to_find):
                    # Prefer struct/enum/trait/function definition over standalone impl block
                    if sym_name in matched_symbols and matched_symbols[sym_name]["type"] in ("struct", "enum", "trait", "function"):
                        continue
                    matched_symbols[sym_name] = {
                        "type": decl["kind"],
                        "name": sym_name,
                        "filename": rel_path,
                        "start_line": decl["start_line"],
                        "end_line": decl["end_line"],
                        "code": decl["code"],
                        "lang": "rust"
                    }
        except (OSError, UnicodeDecodeError):
            pass

    def _index_dart_file(rel_path: str, full_path: str):
        all_source_files.append((rel_path, full_path))
        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as file_obj:
                content = file_obj.read()
            parsed = parse_dart_ast(content, filename=rel_path)
            imports = [f"import '{imp['module']}';" for imp in parsed.get("imports", [])[:10]]
            file_imports_map[rel_path] = imports
            for decl in parsed.get("declarations", []):
                sym_name = decl["name"]
                short_name = sym_name.split(".")[-1]
                if sym_name in symbols_to_find or short_name in symbols_to_find or any(s.lower() in (sym_name.lower(), short_name.lower()) for s in symbols_to_find):
                    matched_symbols[sym_name] = {
                        "type": decl["kind"],
                        "name": sym_name,
                        "filename": rel_path,
                        "start_line": decl.get("start_line", decl.get("line", 1)),
                        "end_line": decl.get("end_line", decl.get("line", 1)),
                        "code": decl.get("code", decl.get("signature", "")),
                        "lang": "dart"
                    }
        except (OSError, UnicodeDecodeError):
            pass

    def _index_jvm_file(rel_path: str, full_path: str):
        all_source_files.append((rel_path, full_path))
        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as file_obj:
                content = file_obj.read()
            parsed = parse_jvm_ast(content, filename=rel_path)
            imports = [f"import {imp['module']};" for imp in parsed.get("imports", [])[:10]]
            file_imports_map[rel_path] = imports
            lang = parsed.get("language", "kotlin" if rel_path.endswith((".kt", ".kts")) else "java")
            for decl in parsed.get("declarations", []):
                sym_name = decl["name"]
                short_name = sym_name.split(".")[-1]
                if sym_name in symbols_to_find or short_name in symbols_to_find or any(s.lower() in (sym_name.lower(), short_name.lower()) for s in symbols_to_find):
                    matched_symbols[sym_name] = {
                        "type": decl["kind"],
                        "name": sym_name,
                        "filename": rel_path,
                        "start_line": decl.get("start_line", decl.get("line", 1)),
                        "end_line": decl.get("end_line", decl.get("line", 1)),
                        "code": decl.get("code", decl.get("signature", "")),
                        "lang": lang
                    }
        except (OSError, UnicodeDecodeError):
            pass

    def _index_source_file(rel_path: str, full_path: str):
        ext = os.path.splitext(rel_path)[1].lower()
        if ext == ".py":
            _index_py_file(rel_path, full_path)
        elif ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
            _index_ts_file(rel_path, full_path)
        elif ext == ".go":
            _index_go_file(rel_path, full_path)
        elif ext == ".rs":
            _index_rs_file(rel_path, full_path)
        elif ext == ".dart":
            _index_dart_file(rel_path, full_path)
        elif ext in (".java", ".kt", ".kts"):
            _index_jvm_file(rel_path, full_path)

    # 1. First pass: find symbol definitions and imports
    if single_file_target:
        _index_source_file(os.path.basename(single_file_target), single_file_target)
    else:
        for root, dirs, files in os.walk(root_abs):
            dirs[:] = [
                d for d in dirs 
                if not is_ignored_path(os.path.relpath(os.path.join(root, d), root_abs))
            ]
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext in (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".dart", ".java", ".kt", ".kts"):
                    full_path = os.path.join(root, f)
                    rel_path = os.path.relpath(full_path, root_abs)
                    if is_ignored_path(rel_path):
                        continue
                    _index_source_file(rel_path, full_path)

    # 2. Second pass: find references/callers across the codebase (e.g. tests)
    found_symbol_names = set(matched_symbols.keys())
    references_by_file: Dict[str, List[Dict[str, Any]]] = {}

    if found_symbol_names:
        for rel_path, full_path in all_source_files:
            refs = find_references_in_file(full_path, found_symbol_names)
            # Filter out the definition itself
            filtered_refs = []
            for r in refs:
                sym_info = matched_symbols.get(r["symbol"])
                if sym_info and sym_info["filename"] == rel_path and sym_info["start_line"] <= r["line_num"] <= sym_info["end_line"]:
                    continue
                filtered_refs.append(r)
            if filtered_refs:
                references_by_file[rel_path] = filtered_refs

    # 3. Assemble surgical context report
    output: List[str] = []
    output.append(f"# Surgical Context Report for Task: '{task}'")
    output.append(f"# Matched Symbols: {', '.join(found_symbol_names) if found_symbol_names else 'None'}\n")

    for sym_name, sym_info in matched_symbols.items():
        rel = sym_info["filename"]
        start = sym_info["start_line"]
        end = sym_info["end_line"]
        output.append(f"## Target Symbol: `{sym_name}` ({sym_info['type']})")
        output.append(f"Location: `{rel}:{start}-{end}`")
        
        # Containing Module Context (docstring & constants)
        mod_doc = file_docstrings_map.get(rel)
        mod_consts = file_constants_map.get(rel, [])
        if mod_doc or mod_consts:
            output.append(f"### Containing Module Context (`{rel}`):")
            if mod_doc:
                output.append(f'"""{mod_doc.strip()}"""\n')
            if mod_consts:
                output.append(f"Module Constants: {', '.join(mod_consts[:8])}\n")

        # Relevant imports from same file
        lang = sym_info.get("lang", "python")
        rel_imports = file_imports_map.get(rel, [])
        if rel_imports:
            output.append("### Module Imports in Source File:")
            output.append(f"```{lang}")
            output.append("\n".join(rel_imports[:10]).strip())
            output.append("```")
            
        output.append("### Source Definition:")
        output.append(f"```{lang}")
        output.append(sym_info["code"].strip())
        output.append("```\n")

    # References
    if references_by_file:
        output.append("## External Callers & Test References:")
        for rel_file, refs in references_by_file.items():
            output.append(f"- **{rel_file}**:")
            for r in refs[:5]:
                output.append(f"  - Line {r['line_num']}: `{r['line']}`")

    output.append("\n> **Retrieval Limitations Notice:** Static AST analysis extracts lexical definitions, docstrings, imports, and cross-file references. Dynamic imports (e.g. `importlib`), reflection, or environment-variable runtime branches must be verified through execution.")

    context_str = "\n".join(output)
    token_est = estimate_tokens(context_str)

    return {
        "task": task,
        "matched_symbols": list(found_symbol_names),
        "symbol_details": {
            k: {
                "type": v["type"],
                "file": v["filename"],
                "start_line": v["start_line"],
                "end_line": v["end_line"]
            } for k, v in matched_symbols.items()
        },
        "references": {k: len(v) for k, v in references_by_file.items()},
        "estimated_tokens": token_est,
        "context": context_str
    }
