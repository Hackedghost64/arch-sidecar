import ast
import logging
import os
import re
from typing import Dict, List, Optional, Set, Any, Tuple
from .utils import DEFAULT_IGNORED_DIRS, is_ignored_path, estimate_tokens
from .ts_ast import parse_ts_js_ast
from .go_ast import parse_go_ast
from .rust_ast import parse_rust_ast
from .dart_ast import parse_dart_ast
from .jvm_ast import parse_jvm_ast

logger = logging.getLogger(__name__)

SUPPORTED_SOURCE_EXTENSIONS = (
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".go", ".rs", ".dart", ".java", ".kt", ".kts",
)

COMMON_STOP_WORDS = {
    "the", "and", "from", "for", "with", "into", "class",
    "def", "file", "code", "module", "target", "extract",
    "decouple", "right", "now", "here",
}


class SymbolFinder(ast.NodeVisitor):
    def __init__(self, filename: str, lines: List[str]):
        self.filename = filename
        self.lines = lines
        self.symbols: Dict[str, Dict[str, Any]] = {}
        self.imports: List[str] = []
        self.constants: List[str] = []

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Name) and (target.id.isupper() or target.id == "__all__"):
                self.constants.append(target.id)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        start = node.lineno - 1
        end = getattr(node, "end_lineno", node.lineno)
        self.imports.append("".join(self.lines[start:end]))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        start = node.lineno - 1
        end = getattr(node, "end_lineno", node.lineno)
        self.imports.append("".join(self.lines[start:end]))
        self.generic_visit(node)

    def _get_start_line(self, node: ast.AST) -> int:
        dec_lines = [d.lineno for d in getattr(node, "decorator_list", []) if hasattr(d, "lineno")]
        return min([node.lineno] + dec_lines) if dec_lines else node.lineno

    def _record_symbol(self, node: ast.AST, sym_type: str, name: str) -> None:
        start_line = self._get_start_line(node)
        start = start_line - 1
        end = getattr(node, "end_lineno", node.lineno)
        code = "".join(self.lines[start:end])
        self.symbols[name] = {
            "type": sym_type,
            "name": name,
            "filename": self.filename,
            "start_line": start_line,
            "end_line": end,
            "code": code,
            "ast_node": node,
            "docstring": ast.get_docstring(node) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else None,
        }

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._record_symbol(node, "class", node.name)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._record_symbol(node, "function", node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._record_symbol(node, "async_function", node.name)


def _scan_lines_for_pattern(lines: List[str], pattern: re.Pattern) -> List[Dict[str, Any]]:
    results = []
    for i, line in enumerate(lines, 1):
        match = pattern.search(line)
        if match:
            results.append({
                "line_num": i,
                "line": line.strip(),
                "symbol": match.group(1),
            })
    return results


def find_references_in_file(filepath: str, symbol_names: Set[str]) -> List[Dict[str, Any]]:
    """Find lines in a file that reference the given symbol names."""
    if not symbol_names or not os.path.isfile(filepath):
        return []

    pattern = re.compile(rf"\b({'|'.join(re.escape(s) for s in symbol_names)})\b")
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        return _scan_lines_for_pattern(lines, pattern)
    except (OSError, UnicodeDecodeError) as exc:
        logger.debug("Skipping reference scan for %s: %s", filepath, exc)
        return []


def _extract_task_symbols(task: str, target_symbols: Optional[List[str]]) -> Set[str]:
    symbols_to_find = set(target_symbols or [])
    if symbols_to_find:
        return symbols_to_find
    tokens = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b", task)
    return {t for t in tokens if t.lower() not in COMMON_STOP_WORDS}


def _matches_symbol(sym_name: str, symbols_to_find: Set[str], check_short: bool = False) -> bool:
    if sym_name in symbols_to_find:
        return True
    sym_lower = sym_name.lower()
    if check_short:
        short_name = sym_name.split(".")[-1]
        short_lower = short_name.lower()
        return short_name in symbols_to_find or any(
            s.lower() in (sym_lower, short_lower) for s in symbols_to_find
        )
    return any(s.lower() == sym_lower for s in symbols_to_find)


class _SurgicalIndexer:
    """Indexes polyglot source files for surgical symbol context retrieval."""

    def __init__(self, symbols_to_find: Set[str]):
        self.symbols_to_find = symbols_to_find
        self.matched_symbols: Dict[str, Dict[str, Any]] = {}
        self.file_imports_map: Dict[str, List[str]] = {}
        self.all_source_files: List[Tuple[str, str]] = []
        self.file_docstrings_map: Dict[str, Optional[str]] = {}
        self.file_constants_map: Dict[str, List[str]] = {}

    def _read_text(self, full_path: str) -> str:
        with open(full_path, "r", encoding="utf-8", errors="ignore") as file_obj:
            return file_obj.read()

    def index_py_file(self, rel_path: str, full_path: str) -> None:
        self.all_source_files.append((rel_path, full_path))
        try:
            content = self._read_text(full_path)
            lines = content.splitlines(keepends=True)
            tree = ast.parse(content, filename=full_path)
            finder = SymbolFinder(rel_path, lines)
            finder.visit(tree)
            self.file_imports_map[rel_path] = finder.imports
            self.file_docstrings_map[rel_path] = ast.get_docstring(tree)
            self.file_constants_map[rel_path] = finder.constants

            for sym_name, sym_info in finder.symbols.items():
                if _matches_symbol(sym_name, self.symbols_to_find):
                    sym_info["lang"] = "python"
                    self.matched_symbols[sym_name] = sym_info
        except (SyntaxError, OSError, UnicodeDecodeError, ValueError) as exc:
            logger.debug("Skipping Python index for %s: %s", rel_path, exc)

    def _store_decl_match(
        self,
        decl: Dict[str, Any],
        rel_path: str,
        lang: str,
        check_short: bool = False,
        prefer_primary_rust: bool = False,
    ) -> None:
        sym_name = decl["name"]
        if not _matches_symbol(sym_name, self.symbols_to_find, check_short=check_short):
            return
        if prefer_primary_rust and sym_name in self.matched_symbols:
            if self.matched_symbols[sym_name]["type"] in ("struct", "enum", "trait", "function"):
                return
        self.matched_symbols[sym_name] = {
            "type": decl["kind"],
            "name": sym_name,
            "filename": rel_path,
            "start_line": decl.get("start_line", decl.get("line", 1)),
            "end_line": decl.get("end_line", decl.get("line", 1)),
            "code": decl.get("code", decl.get("signature", "")),
            "lang": lang,
            "docstring": decl.get("docstring"),
        }

    def index_ts_file(self, rel_path: str, full_path: str) -> None:
        self.all_source_files.append((rel_path, full_path))
        try:
            parsed = parse_ts_js_ast(self._read_text(full_path), filename=rel_path)
            self.file_imports_map[rel_path] = [
                f"import {imp['module']}" for imp in parsed.get("imports", [])[:10]
            ]
            lang = "typescript" if rel_path.endswith((".ts", ".tsx")) else "javascript"
            for decl in parsed.get("declarations", []):
                self._store_decl_match(decl, rel_path, lang)
        except (OSError, UnicodeDecodeError) as exc:
            logger.debug("Skipping TS/JS index for %s: %s", rel_path, exc)

    def index_go_file(self, rel_path: str, full_path: str) -> None:
        self.all_source_files.append((rel_path, full_path))
        try:
            parsed = parse_go_ast(self._read_text(full_path), filename=rel_path)
            self.file_imports_map[rel_path] = [
                f'import "{imp["module"]}"' for imp in parsed.get("imports", [])[:10]
            ]
            for decl in parsed.get("declarations", []):
                self._store_decl_match(decl, rel_path, "go")
        except (OSError, UnicodeDecodeError) as exc:
            logger.debug("Skipping Go index for %s: %s", rel_path, exc)

    def index_rs_file(self, rel_path: str, full_path: str) -> None:
        self.all_source_files.append((rel_path, full_path))
        try:
            parsed = parse_rust_ast(self._read_text(full_path), filename=rel_path)
            self.file_imports_map[rel_path] = [
                f"use {imp['module']};" for imp in parsed.get("imports", [])[:10]
            ]
            for decl in parsed.get("declarations", []):
                self._store_decl_match(decl, rel_path, "rust", prefer_primary_rust=True)
        except (OSError, UnicodeDecodeError) as exc:
            logger.debug("Skipping Rust index for %s: %s", rel_path, exc)

    def index_dart_file(self, rel_path: str, full_path: str) -> None:
        self.all_source_files.append((rel_path, full_path))
        try:
            parsed = parse_dart_ast(self._read_text(full_path), filename=rel_path)
            self.file_imports_map[rel_path] = [
                f"import '{imp['module']}';" for imp in parsed.get("imports", [])[:10]
            ]
            for decl in parsed.get("declarations", []):
                self._store_decl_match(decl, rel_path, "dart", check_short=True)
        except (OSError, UnicodeDecodeError) as exc:
            logger.debug("Skipping Dart index for %s: %s", rel_path, exc)

    def index_jvm_file(self, rel_path: str, full_path: str) -> None:
        self.all_source_files.append((rel_path, full_path))
        try:
            parsed = parse_jvm_ast(self._read_text(full_path), filename=rel_path)
            self.file_imports_map[rel_path] = [
                f"import {imp['module']};" for imp in parsed.get("imports", [])[:10]
            ]
            default_lang = "kotlin" if rel_path.endswith((".kt", ".kts")) else "java"
            lang = parsed.get("language", default_lang)
            for decl in parsed.get("declarations", []):
                self._store_decl_match(decl, rel_path, lang, check_short=True)
        except (OSError, UnicodeDecodeError) as exc:
            logger.debug("Skipping JVM index for %s: %s", rel_path, exc)

    def index_source_file(self, rel_path: str, full_path: str) -> None:
        ext = os.path.splitext(rel_path)[1].lower()
        if ext == ".py":
            self.index_py_file(rel_path, full_path)
        elif ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
            self.index_ts_file(rel_path, full_path)
        elif ext == ".go":
            self.index_go_file(rel_path, full_path)
        elif ext == ".rs":
            self.index_rs_file(rel_path, full_path)
        elif ext == ".dart":
            self.index_dart_file(rel_path, full_path)
        elif ext in (".java", ".kt", ".kts"):
            self.index_jvm_file(rel_path, full_path)


def _discover_source_files(root_abs: str) -> List[Tuple[str, str]]:
    discovered: List[Tuple[str, str]] = []
    for root, dirs, files in os.walk(root_abs):
        dirs[:] = [
            d for d in dirs
            if not is_ignored_path(os.path.relpath(os.path.join(root, d), root_abs))
        ]
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext not in SUPPORTED_SOURCE_EXTENSIONS:
                continue
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, root_abs)
            if not is_ignored_path(rel_path):
                discovered.append((rel_path, full_path))
    return discovered


def _is_self_definition(
    ref: Dict[str, Any],
    rel_path: str,
    matched_symbols: Dict[str, Dict[str, Any]],
) -> bool:
    sym_info = matched_symbols.get(ref["symbol"])
    if not sym_info or sym_info["filename"] != rel_path:
        return False
    return sym_info["start_line"] <= ref["line_num"] <= sym_info["end_line"]


def _collect_external_references(
    all_source_files: List[Tuple[str, str]],
    matched_symbols: Dict[str, Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    found_symbol_names = set(matched_symbols.keys())
    if not found_symbol_names:
        return {}
    references_by_file: Dict[str, List[Dict[str, Any]]] = {}
    for rel_path, full_path in all_source_files:
        refs = find_references_in_file(full_path, found_symbol_names)
        filtered = [r for r in refs if not _is_self_definition(r, rel_path, matched_symbols)]
        if filtered:
            references_by_file[rel_path] = filtered
    return references_by_file


def _format_symbol_section(
    sym_name: str,
    sym_info: Dict[str, Any],
    indexer: _SurgicalIndexer,
) -> List[str]:
    rel = sym_info["filename"]
    start = sym_info["start_line"]
    end = sym_info["end_line"]
    lines = [
        f"## Target Symbol: `{sym_name}` ({sym_info['type']})",
        f"Location: `{rel}:{start}-{end}`",
    ]
    mod_doc = indexer.file_docstrings_map.get(rel)
    mod_consts = indexer.file_constants_map.get(rel, [])
    if mod_doc or mod_consts:
        lines.append(f"### Containing Module Context (`{rel}`):")
        if mod_doc:
            lines.append(f'"""{mod_doc.strip()}"""\n')
        if mod_consts:
            lines.append(f"Module Constants: {', '.join(mod_consts[:8])}\n")

    lang = sym_info.get("lang", "python")
    rel_imports = indexer.file_imports_map.get(rel, [])
    if rel_imports:
        lines.extend([
            "### Module Imports in Source File:",
            f"```{lang}",
            "\n".join(rel_imports[:10]).strip(),
            "```",
        ])

    lines.extend([
        "### Source Definition:",
        f"```{lang}",
        sym_info["code"].strip(),
        "```\n",
    ])
    return lines


def _format_surgical_report(
    task: str,
    indexer: _SurgicalIndexer,
    references_by_file: Dict[str, List[Dict[str, Any]]],
) -> str:
    found_names = list(indexer.matched_symbols.keys())
    output: List[str] = [
        f"# Surgical Context Report for Task: '{task}'",
        f"# Matched Symbols: {', '.join(found_names) if found_names else 'None'}\n",
    ]
    for sym_name, sym_info in indexer.matched_symbols.items():
        output.extend(_format_symbol_section(sym_name, sym_info, indexer))

    if references_by_file:
        output.append("## External Callers & Test References:")
        for rel_file, refs in references_by_file.items():
            output.append(f"- **{rel_file}**:")
            for r in refs[:5]:
                output.append(f"  - Line {r['line_num']}: `{r['line']}`")

    output.append(
        "\n> **Retrieval Limitations Notice:** Static AST analysis extracts lexical definitions, "
        "docstrings, imports, and cross-file references. Dynamic imports (e.g. `importlib`), "
        "reflection, or environment-variable runtime branches must be verified through execution."
    )
    return "\n".join(output)


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

    symbols_to_find = _extract_task_symbols(task, target_symbols)
    indexer = _SurgicalIndexer(symbols_to_find)

    if single_file_target:
        indexer.index_source_file(os.path.basename(single_file_target), single_file_target)
    else:
        for rel_path, full_path in _discover_source_files(root_abs):
            indexer.index_source_file(rel_path, full_path)

    references_by_file = _collect_external_references(
        indexer.all_source_files, indexer.matched_symbols
    )
    context_str = _format_surgical_report(task, indexer, references_by_file)
    token_est = estimate_tokens(context_str)

    return {
        "task": task,
        "matched_symbols": list(indexer.matched_symbols.keys()),
        "symbol_details": {
            k: {
                "type": v["type"],
                "file": v["filename"],
                "start_line": v["start_line"],
                "end_line": v["end_line"],
            }
            for k, v in indexer.matched_symbols.items()
        },
        "references": {k: len(v) for k, v in references_by_file.items()},
        "estimated_tokens": token_est,
        "context": context_str,
    }
