import os
import ast
import re
import json
import subprocess
import time
import logging
from typing import Dict, List, Optional, Set, Any, Tuple

from .utils import DEFAULT_IGNORED_DIRS, is_ignored_path, is_minified_or_binary, estimate_tokens
from .context import SymbolFinder, find_references_in_file
from .uncertainty import scan_file_uncertainty, ConfidenceLevel
from .guard import check_code_health, resolve_local_module_path
from .fault_localizer import ASTCanonicalRepairCache, localize_pytest_faults
from .session_logger import record_session
from .ts_ast import parse_ts_js_ast
from .go_ast import parse_go_ast
from .rust_ast import parse_rust_ast
from .dart_ast import parse_dart_ast
from .jvm_ast import parse_jvm_ast

logger = logging.getLogger(__name__)


STOPWORDS = {
    "the", "and", "from", "for", "with", "into", "class", "def", "file",
    "code", "module", "target", "extract", "decouple", "move", "split",
    "refactor", "function", "method", "should", "need", "where", "when",
    "what", "which", "that", "this", "have", "does", "using", "only",
    "implement", "feature", "add", "create", "support", "new", "make",
    "update", "change", "allow", "return", "returns", "call", "calling",
}

GENERIC_SUBTOKENS = {
    "name", "names", "get", "set", "data", "item", "items", "node", "nodes",
    "list", "dict", "call", "func", "value", "info", "path", "file", "line",
    "type", "str", "text", "run", "visit",
}


def _extract_all_exports(node: ast.Assign) -> Optional[List[str]]:
    """Extract string exports from __all__ assignment target."""
    has_all = any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
    if not has_all or not isinstance(node.value, (ast.List, ast.Tuple)):
        return None
    return [
        elt.value
        for elt in node.value.elts
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
    ]


class DetailedFileAnalyzer(ast.NodeVisitor):
    """
    Extracts top-level symbols AND class methods (e.g. `ClassName.method_name`),
    decorators, imports, and `__all__` exports so feature planning can anchor on
    specific methods, routes, or CLI commands inside larger classes/modules.
    """
    def __init__(self, rel_path: str, lines: List[str]):
        self.rel_path = rel_path
        self.lines = lines
        self.symbols: Dict[str, Dict[str, Any]] = {}
        self.imports: List[str] = []
        self.import_end_line: int = 0
        self.exports_all: Optional[List[str]] = None
        self._current_class: Optional[str] = None

    def visit_Import(self, node: ast.Import):
        start = node.lineno - 1
        end = getattr(node, "end_lineno", node.lineno)
        self.imports.append("".join(self.lines[start:end]).strip())
        if end > self.import_end_line:
            self.import_end_line = end

    def visit_ImportFrom(self, node: ast.ImportFrom):
        start = node.lineno - 1
        end = getattr(node, "end_lineno", node.lineno)
        self.imports.append("".join(self.lines[start:end]).strip())
        if end > self.import_end_line:
            self.import_end_line = end

    def visit_Assign(self, node: ast.Assign):
        exports = _extract_all_exports(node)
        if exports is not None:
            self.exports_all = exports

    def _record_callable_or_class(self, node: ast.AST, name: str, kind: str):
        dec_lines = [d.lineno for d in getattr(node, "decorator_list", []) if hasattr(d, "lineno")]
        start_line = min([node.lineno] + dec_lines) if dec_lines else node.lineno
        end_line = getattr(node, "end_lineno", node.lineno)
        raw_slice = self.lines[start_line - 1:end_line]
        numbered_code = "".join(f"{start_line + idx}: {line}" for idx, line in enumerate(raw_slice))
        raw_code = "".join(raw_slice)
        signature_line = self.lines[node.lineno - 1].strip() if 0 <= node.lineno - 1 < len(self.lines) else name

        self.symbols[name] = {
            "type": kind,
            "name": name,
            "filename": self.rel_path,
            "start_line": start_line,
            "end_line": end_line,
            "signature": signature_line,
            "code": raw_code,
            "numbered_code": numbered_code,
            "ast_node": node,
        }

    def visit_ClassDef(self, node: ast.ClassDef):
        self._record_callable_or_class(node, node.name, "class")
        prev_cls = self._current_class
        self._current_class = node.name
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual_name = f"{node.name}.{item.name}"
                self._record_callable_or_class(item, qual_name, "method")
        self._current_class = prev_cls

    def visit_FunctionDef(self, node: ast.FunctionDef):
        if self._current_class is None:
            self._record_callable_or_class(node, node.name, "function")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        if self._current_class is None:
            self._record_callable_or_class(node, node.name, "async_function")


class PolyglotFileAnalyzer:
    """Standardized representation of file symbols across TypeScript, Go, and Rust."""
    def __init__(
        self,
        rel_path: str,
        lines: List[str],
        symbols: Dict[str, Dict[str, Any]],
        imports: List[str],
        exports_all: Optional[List[str]] = None,
    ):
        self.rel_path = rel_path
        self.lines = lines
        self.symbols = symbols
        self.imports = imports
        self.import_end_line = 0
        self.exports_all = exports_all


def build_ts_analyzer(rel_path: str, lines: List[str]) -> PolyglotFileAnalyzer:
    parsed = parse_ts_js_ast("".join(lines), filename=rel_path)
    imports = [f"import {imp['module']}" for imp in parsed.get("imports", [])[:15]]
    symbols: Dict[str, Dict[str, Any]] = {}
    for decl in parsed.get("declarations", []):
        name = decl["name"]
        s_line = decl.get("start_line", decl.get("line", 1))
        e_line = decl.get("end_line", decl.get("line", 1))
        raw_slice = lines[s_line - 1 : e_line]
        numbered_code = "".join(f"{s_line + idx}: {line}" for idx, line in enumerate(raw_slice))
        raw_code = "".join(raw_slice)
        symbols[name] = {
            "type": decl.get("kind", "function"),
            "name": name,
            "filename": rel_path,
            "start_line": s_line,
            "end_line": e_line,
            "signature": decl.get("signature", name),
            "code": raw_code,
            "numbered_code": numbered_code,
            "ast_node": None,
        }
    return PolyglotFileAnalyzer(rel_path, lines, symbols, imports)


def build_go_analyzer(rel_path: str, lines: List[str]) -> PolyglotFileAnalyzer:
    parsed = parse_go_ast("".join(lines), filename=rel_path)
    imports = [f"import \"{imp['module']}\"" for imp in parsed.get("imports", [])[:15]]
    symbols: Dict[str, Dict[str, Any]] = {}
    for decl in parsed.get("declarations", []):
        name = decl["name"]
        s_line = decl.get("start_line", 1)
        e_line = decl.get("end_line", 1)
        raw_slice = lines[s_line - 1 : e_line]
        numbered_code = "".join(f"{s_line + idx}: {line}" for idx, line in enumerate(raw_slice))
        raw_code = "".join(raw_slice)
        symbols[name] = {
            "type": decl.get("kind", "function"),
            "name": name,
            "filename": rel_path,
            "start_line": s_line,
            "end_line": e_line,
            "signature": decl.get("signature", name),
            "code": raw_code,
            "numbered_code": numbered_code,
            "ast_node": None,
        }
    return PolyglotFileAnalyzer(rel_path, lines, symbols, imports)


def build_rust_analyzer(rel_path: str, lines: List[str]) -> PolyglotFileAnalyzer:
    parsed = parse_rust_ast("".join(lines), filename=rel_path)
    imports = [f"use {imp['module']};" for imp in parsed.get("imports", [])[:15]]
    symbols: Dict[str, Dict[str, Any]] = {}
    for decl in parsed.get("declarations", []):
        name = decl["name"]
        s_line = decl.get("start_line", 1)
        e_line = decl.get("end_line", 1)
        raw_slice = lines[s_line - 1 : e_line]
        numbered_code = "".join(f"{s_line + idx}: {line}" for idx, line in enumerate(raw_slice))
        raw_code = "".join(raw_slice)
        symbols[name] = {
            "type": decl.get("kind", "function"),
            "name": name,
            "filename": rel_path,
            "start_line": s_line,
            "end_line": e_line,
            "signature": decl.get("signature", name),
            "code": raw_code,
            "numbered_code": numbered_code,
            "ast_node": None,
        }
    return PolyglotFileAnalyzer(rel_path, lines, symbols, imports)


def build_dart_analyzer(rel_path: str, lines: List[str]) -> PolyglotFileAnalyzer:
    parsed = parse_dart_ast("".join(lines), filename=rel_path)
    imports = [f"import '{imp['module']}';" for imp in parsed.get("imports", [])[:15]]
    symbols: Dict[str, Dict[str, Any]] = {}
    for decl in parsed.get("declarations", []):
        name = decl["name"]
        s_line = decl.get("start_line", 1)
        e_line = decl.get("end_line", 1)
        raw_slice = lines[s_line - 1 : e_line]
        numbered_code = "".join(f"{s_line + idx}: {line}" for idx, line in enumerate(raw_slice))
        raw_code = "".join(raw_slice)
        symbols[name] = {
            "type": decl.get("kind", "function"),
            "name": name,
            "filename": rel_path,
            "start_line": s_line,
            "end_line": e_line,
            "signature": decl.get("signature", name),
            "code": raw_code,
            "numbered_code": numbered_code,
            "ast_node": None,
        }
    return PolyglotFileAnalyzer(rel_path, lines, symbols, imports)


def build_jvm_analyzer(rel_path: str, lines: List[str]) -> PolyglotFileAnalyzer:
    parsed = parse_jvm_ast("".join(lines), filename=rel_path)
    imports = [f"import {imp['module']};" for imp in parsed.get("imports", [])[:15]]
    symbols: Dict[str, Dict[str, Any]] = {}
    for decl in parsed.get("declarations", []):
        name = decl["name"]
        s_line = decl.get("start_line", 1)
        e_line = decl.get("end_line", 1)
        raw_slice = lines[s_line - 1 : e_line]
        numbered_code = "".join(f"{s_line + idx}: {line}" for idx, line in enumerate(raw_slice))
        raw_code = "".join(raw_slice)
        symbols[name] = {
            "type": decl.get("kind", "class" if "class" in decl.get("kind", "") else "method"),
            "name": name,
            "filename": rel_path,
            "start_line": s_line,
            "end_line": e_line,
            "signature": decl.get("signature", name),
            "code": raw_code,
            "numbered_code": numbered_code,
            "ast_node": None,
        }
    return PolyglotFileAnalyzer(rel_path, lines, symbols, imports)


CATEGORY_KEYWORDS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("add_or_update_endpoint", ("endpoint", "route", "@router", "@app", "get ", "post ", "delete ", "put ")),
    ("add_or_update_cli_feature", ("option", "flag", "argument", "subcommand", "cli", "@click")),
    ("add_or_extend_class", ("class ", "subclass", "dataclass", "model", "struct", "interface", "trait")),
    ("validation_or_bugfix", ("fix", "bug", "error", "exception", "handle", "validate")),
)


def _detect_feature_category(task_lower: str) -> str:
    for category, kw_tuple in CATEGORY_KEYWORDS:
        if any(k in task_lower for k in kw_tuple):
            return category
    return "add_or_extend_function"


def _extract_task_keywords(raw_tokens: List[str]) -> List[str]:
    keywords: List[str] = []
    seen: Set[str] = set()
    polyglot_exts = (".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".dart", ".kt", ".kts", ".java")
    for tok in raw_tokens:
        tok_low = tok.lower()
        if tok_low in STOPWORDS or tok in seen:
            continue
        if any(tok.endswith(ext) for ext in polyglot_exts):
            continue
        keywords.append(tok)
        seen.add(tok)
    return keywords


def classify_feature_intent(task: str) -> Dict[str, Any]:
    """
    Step 1: Understand the requested change across Python, TypeScript, Go, Rust, Dart, and JVM (Java/Kotlin).
    Extracts intent category, explicit file mentions, identifier tokens, and route/CLI patterns.
    """
    category = _detect_feature_category(task.lower())
    explicit_files = re.findall(r"[\w\-./]+\.(?:py|ts|tsx|js|jsx|go|rs|dart|kt|kts|java)\b", task)
    raw_tokens = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b", task)
    keywords = _extract_task_keywords(raw_tokens)

    return {
        "category": category,
        "explicit_files": explicit_files,
        "keywords": keywords,
    }


def _normalize_test_command_for_root(cmd: str, root_abs: str) -> str:
    """
    If a test command includes a redundant '<project_basename>/' path prefix
    (e.g. 'pytest wrapper/tests/test_x.py' when root_abs already ends with '/wrapper'),
    strip the redundant prefix so the path resolves cleanly inside cwd=root_abs.
    """
    proj_base = os.path.basename(os.path.normpath(root_abs))
    if not proj_base:
        return cmd
    prefix = f"{proj_base}/"
    parts = cmd.split()
    normalized = []
    for part in parts:
        if part.startswith(prefix):
            stripped = part[len(prefix):]
            if not os.path.exists(os.path.join(root_abs, part)) and os.path.exists(os.path.join(root_abs, stripped.split("::")[0])):
                normalized.append(stripped)
                continue
        normalized.append(part)
    return " ".join(normalized)


def detect_git_modified_source_files(root_abs: str) -> List[str]:
    """Detects modified or untracked source files in a git working tree relative to root_abs."""
    valid_exts = (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".dart", ".kt", ".kts", ".java")
    try:
        res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root_abs,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode != 0:
            return []
        files = []
        for line in res.stdout.splitlines():
            if len(line) < 4:
                continue
            path_part = line[3:].strip()
            if " -> " in path_part:
                path_part = path_part.split(" -> ")[-1].strip()
            if path_part.endswith(valid_exts) and os.path.exists(os.path.join(root_abs, path_part)):
                files.append(path_part)
        return files
    except Exception as e:
        logger.debug("Failed detecting git modified files in %s: %s", root_abs, e)
        return []


HTTP_ROUTE_REGEX = re.compile(
    r'["\'](/(?:api|v\d+|auth|users|download|checkin|voices|health|items|orders|billing|stream|sessions)[/\w\-{}:]*)["\']'
)

ENV_VAR_REGEXES = [
    re.compile(r'os\.(?:environ\.get|getenv)\s*\(\s*["\']([A-Z0-9_]{3,})["\']'),
    re.compile(r'os\.environ\s*\[\s*["\']([A-Z0-9_]{3,})["\']\s*\]'),
    re.compile(r'process\.env\.([A-Z0-9_]{3,})'),
    re.compile(r'process\.env\s*\[\s*["\']([A-Z0-9_]{3,})["\']\s*\]'),
    re.compile(r'import\.meta\.env\.([A-Z0-9_]{3,})'),
    re.compile(r'import\.meta\.env\s*\[\s*["\']([A-Z0-9_]{3,})["\']\s*\]'),
    re.compile(r'(?:String|int|bool)\.fromEnvironment\s*\(\s*["\']([A-Z0-9_]{3,})["\']'),
    re.compile(r'Platform\.environment\s*\[\s*["\']([A-Z0-9_]{3,})["\']\s*\]'),
    re.compile(r'os\.(?:Getenv|LookupEnv)\s*\(\s*["\']([A-Z0-9_]{3,})["\']'),
    re.compile(r'(?:std::)?env::var\s*\(\s*["\']([A-Z0-9_]{3,})["\']'),
    re.compile(r'System\.getenv\s*\(\s*["\']([A-Z0-9_]{3,})["\']'),
]

COMMON_IGNORED_ENV = {"PATH", "HOME", "USER", "PWD", "SHELL", "TERM"}


def _extract_corpus_routes_and_envs(
    primary_lines: List[str],
    anchor_symbols: List[Dict[str, Any]],
) -> Tuple[Set[str], Set[str]]:
    """Extract HTTP route literals and environment variable keys from source corpus."""
    anchor_code = "\n".join(
        (a.get("info", {}).get("code", "") if isinstance(a.get("info"), dict) else a.get("code", ""))
        for a in anchor_symbols
    )
    search_corpus = "".join(primary_lines) + "\n" + anchor_code

    extracted_routes: Set[str] = set()
    for match in HTTP_ROUTE_REGEX.findall(search_corpus):
        if match:
            extracted_routes.add(match)

    extracted_env_vars: Set[str] = set()
    for pattern in ENV_VAR_REGEXES:
        for match in pattern.findall(search_corpus):
            if match and match not in COMMON_IGNORED_ENV:
                extracted_env_vars.add(match)

    return extracted_routes, extracted_env_vars


def _scan_line_for_boundary_links(
    line_str: str,
    line_num: int,
    file_path: str,
    routes: Set[str],
    env_vars: Set[str],
) -> List[Dict[str, Any]]:
    """Scan a single line for matching routes or environment variables."""
    results: List[Dict[str, Any]] = []
    trimmed = line_str.strip()
    for route in routes:
        if route in line_str:
            results.append({
                "kind": "route",
                "token": route,
                "file": file_path,
                "line": line_num,
                "snippet": trimmed,
            })
    for env_var in env_vars:
        if re.search(rf"\b{re.escape(env_var)}\b", line_str):
            results.append({
                "kind": "env_var",
                "token": env_var,
                "file": file_path,
                "line": line_num,
                "snippet": trimmed,
            })
    return results


def _scan_file_boundary_links(
    file_path: str,
    lines: List[str],
    routes: Set[str],
    env_vars: Set[str],
) -> List[Dict[str, Any]]:
    """Scan all lines in a file for cross-boundary links."""
    file_links: List[Dict[str, Any]] = []
    for line_idx, line_str in enumerate(lines):
        file_links.extend(
            _scan_line_for_boundary_links(line_str, line_idx + 1, file_path, routes, env_vars)
        )
    return file_links


def _collect_unique_boundary_links(
    file_lines_map: Dict[str, List[str]],
    primary_file: str,
    routes: Set[str],
    env_vars: Set[str],
) -> List[Dict[str, Any]]:
    links: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str, str, int]] = set()
    for other_path, lines in file_lines_map.items():
        if other_path == primary_file:
            continue
        for link in _scan_file_boundary_links(other_path, lines, routes, env_vars):
            key = (link["kind"], link["token"], link["file"], link["line"])
            if key not in seen:
                seen.add(key)
                links.append(link)
    links.sort(key=lambda x: (x["file"], x["line"], x["kind"]))
    return links


def detect_cross_boundary_links(
    primary_file: str,
    primary_lines: List[str],
    anchor_symbols: List[Dict[str, Any]],
    file_lines_map: Dict[str, List[str]],
) -> List[Dict[str, Any]]:
    """
    Extracts HTTP route literals and environment variable keys from the primary file/anchors
    and scans all other files in file_lines_map for cross-boundary references.
    """
    if not primary_file or not primary_lines:
        return []

    routes, env_vars = _extract_corpus_routes_and_envs(primary_lines, anchor_symbols)
    if not routes and not env_vars:
        return []

    return _collect_unique_boundary_links(file_lines_map, primary_file, routes, env_vars)


TEST_DIR_PARTS = {"test", "__tests__"}


def _is_scripting_test_file(file: str, ext: str) -> bool:
    if ext == ".py":
        return file.startswith("test_") or file.endswith("_test.py")
    if ext in (".ts", ".tsx", ".js", ".jsx"):
        return ".test." in file or ".spec." in file
    if ext == ".go":
        return file.endswith("_test.go")
    return False


def _is_compiled_test_file(file: str, rel_low: str, ext: str) -> bool:
    if ext == ".rs":
        return file.endswith("_test.rs") or rel_low.startswith("tests/")
    if ext == ".dart":
        return file.endswith("_test.dart") or rel_low.startswith("test/")
    if ext in (".kt", ".kts"):
        return file.endswith(("Test.kt", "Spec.kt", "Tests.kt"))
    if ext == ".java":
        return file.endswith(("Test.java", "Tests.java", "TestCase.java"))
    return False


def _is_test_filename(file: str, rel_low: str, ext: str) -> bool:
    """Check filename patterns for tests across polyglot languages."""
    if _is_scripting_test_file(file, ext):
        return True
    return _is_compiled_test_file(file, rel_low, ext)


def _is_test_file(file: str, rel_path: str, ext: str) -> bool:
    """Determine whether a file path corresponds to a test suite file."""
    rel_low = rel_path.lower()
    parts = set(rel_low.split(os.sep))
    if parts & TEST_DIR_PARTS or "__tests__" in rel_low:
        return True
    return _is_test_filename(file, rel_low, ext)


def _is_valid_candidate_file(full_path: str, rel_path: str, ext: str, valid_exts: Tuple[str, ...]) -> bool:
    if ext not in valid_exts:
        return False
    return not (is_ignored_path(rel_path) or is_minified_or_binary(full_path))


def _filter_walk_dirs(dirs: List[str]) -> List[str]:
    return [d for d in dirs if d not in DEFAULT_IGNORED_DIRS and not d.startswith(".")]


def _discover_repo_files(root_abs: str) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    """Walk project directory and collect valid source and test files."""
    source_files: List[Tuple[str, str]] = []
    test_files: List[Tuple[str, str]] = []
    valid_source_exts = (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".dart", ".java", ".kt", ".kts")

    for current_root, dirs, files in os.walk(root_abs):
        dirs[:] = _filter_walk_dirs(dirs)
        for file in sorted(files):
            ext = os.path.splitext(file)[1].lower()
            full_path = os.path.join(current_root, file)
            rel_path = os.path.relpath(full_path, root_abs)
            if not _is_valid_candidate_file(full_path, rel_path, ext, valid_source_exts):
                continue
            if _is_test_file(file, rel_path, ext):
                test_files.append((rel_path, full_path))
            else:
                source_files.append((rel_path, full_path))

    if not source_files and test_files:
        source_files = list(test_files)
    return source_files, test_files


def _parse_source_file(rel_path: str, full_path: str) -> Optional[Tuple[Any, List[str], Optional[ast.Module]]]:
    """Parse a single source file into its lines and analyzer representation."""
    try:
        with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        ext = os.path.splitext(rel_path)[1].lower()
        if ext == ".py":
            tree = ast.parse("".join(lines), filename=rel_path)
            analyzer = DetailedFileAnalyzer(rel_path, lines)
            analyzer.visit(tree)
            return analyzer, lines, tree
        if ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
            return build_ts_analyzer(rel_path, lines), lines, None
        if ext == ".go":
            return build_go_analyzer(rel_path, lines), lines, None
        if ext == ".rs":
            return build_rust_analyzer(rel_path, lines), lines, None
        if ext == ".dart":
            return build_dart_analyzer(rel_path, lines), lines, None
        if ext in (".java", ".kt", ".kts"):
            return build_jvm_analyzer(rel_path, lines), lines, None
    except Exception as e:
        logger.debug("Failed parsing source file %s: %s", rel_path, e)
    return None


def _analyze_source_files(
    source_files: List[Tuple[str, str]],
) -> Tuple[Dict[str, Any], Dict[str, List[str]], Dict[str, ast.Module]]:
    """Parse and index all candidate source files across supported languages."""
    file_analyzers: Dict[str, Any] = {}
    file_lines_map: Dict[str, List[str]] = {}
    file_trees_map: Dict[str, ast.Module] = {}

    for rel_path, full_path in source_files:
        parsed = _parse_source_file(rel_path, full_path)
        if not parsed:
            continue
        analyzer, lines, tree = parsed
        file_analyzers[rel_path] = analyzer
        file_lines_map[rel_path] = lines
        if tree is not None:
            file_trees_map[rel_path] = tree

    return file_analyzers, file_lines_map, file_trees_map


def _score_substring_match(kw_low: str, target_low: str, high_score: float, low_score: float) -> Optional[float]:
    if len(kw_low) >= 3 and kw_low in target_low:
        return high_score if kw_low not in GENERIC_SUBTOKENS else low_score
    return None


def _score_keyword_match(kw: str, sym_name: str, short_name: str, code_low: str) -> float:
    """Score match of a single keyword against symbol name and code."""
    kw_low = kw.lower()
    short_low = short_name.lower()
    if kw == sym_name or kw == short_name:
        return 80.0
    if kw_low == short_low:
        return 65.0

    name_match = _score_substring_match(kw_low, short_low, 40.0, 10.0)
    if name_match is not None:
        return name_match

    code_match = _score_substring_match(kw_low, code_low, 18.0, 3.0)
    if code_match is not None:
        return code_match
    return 0.0


def _score_symbol(
    sym_name: str,
    sym_info: Dict[str, Any],
    query_keywords: List[str],
    target_symbols: Optional[List[str]],
    rel_path: str,
    explicit_files: List[str],
) -> float:
    """Calculate relevance score for a symbol relative to query keywords and target symbols."""
    s_score = 0.0
    short_name = sym_name.split(".")[-1]
    code_low = sym_info["code"].lower()

    if target_symbols and (sym_name in target_symbols or short_name in target_symbols):
        s_score += 100.0

    for kw in query_keywords:
        s_score += _score_keyword_match(kw, sym_name, short_name, code_low)

    if any(rel_path.endswith(ef) or os.path.basename(rel_path) == os.path.basename(ef) for ef in explicit_files):
        s_score += 20.0

    return s_score


def _matches_explicit_file(rel_path: str, base_name: str, exp_f: str) -> bool:
    return rel_path == exp_f or rel_path.endswith("/" + exp_f) or base_name == os.path.basename(exp_f)


def _compute_explicit_file_boost(rel_path: str, explicit_files: List[str]) -> float:
    base_name = os.path.basename(rel_path)
    boost = 0.0
    for exp_f in explicit_files:
        if _matches_explicit_file(rel_path, base_name, exp_f):
            boost += 120.0
    return boost


def _compute_stem_keyword_boost(stem: str, query_keywords: List[str]) -> float:
    boost = 0.0
    for kw in query_keywords:
        kw_low = kw.lower()
        if kw_low == stem:
            boost += 45.0
        elif len(kw_low) >= 4 and (kw_low in stem or stem in kw_low):
            boost += 25.0
    return boost


def _collect_file_symbols(
    analyzer_symbols: Dict[str, Any],
    query_keywords: List[str],
    target_symbols: Optional[List[str]],
    rel_path: str,
    explicit_files: List[str],
) -> Tuple[float, List[Dict[str, Any]]]:
    file_sym_boost = 0.0
    symbols: List[Dict[str, Any]] = []
    for sym_name, sym_info in analyzer_symbols.items():
        s_score = _score_symbol(
            sym_name, sym_info, query_keywords, target_symbols, rel_path, explicit_files
        )
        if s_score > 0:
            file_sym_boost += min(s_score * 0.5, 60.0)
            symbols.append({
                "name": sym_name,
                "short_name": sym_name.split(".")[-1],
                "file": rel_path,
                "type": sym_info["type"],
                "start_line": sym_info["start_line"],
                "end_line": sym_info["end_line"],
                "signature": sym_info["signature"],
                "score": round(s_score, 1),
                "info": sym_info,
            })
    return file_sym_boost, symbols


def _score_files_and_symbols(
    file_analyzers: Dict[str, Any],
    intent: Dict[str, Any],
    query_keywords: List[str],
    target_symbols: Optional[List[str]],
) -> Tuple[Dict[str, float], List[Dict[str, Any]]]:
    """Score all files and their contained symbols against feature query terms."""
    file_scores: Dict[str, float] = {}
    scored_symbols: List[Dict[str, Any]] = []

    for rel_path, analyzer in file_analyzers.items():
        stem = os.path.splitext(os.path.basename(rel_path))[0].lower()
        exp_boost = _compute_explicit_file_boost(rel_path, intent["explicit_files"])
        stem_boost = _compute_stem_keyword_boost(stem, query_keywords)
        sym_boost, symbols = _collect_file_symbols(
            analyzer.symbols, query_keywords, target_symbols, rel_path, intent["explicit_files"]
        )
        file_scores[rel_path] = exp_boost + stem_boost + sym_boost
        scored_symbols.extend(symbols)

    scored_symbols.sort(key=lambda x: (-x["score"], x["file"], x["start_line"]))
    return file_scores, scored_symbols


def _should_run_pytest_fault_localizer(
    verify: bool,
    intent: Dict[str, Any],
    target_symbols: Optional[List[str]],
    scored_symbols: List[Dict[str, Any]],
    test_files: List[Tuple[str, str]],
) -> bool:
    if verify or target_symbols or intent.get("explicit_files") or not test_files:
        return False
    if intent.get("category") != "validation_or_bugfix":
        return False
    top_score = scored_symbols[0]["score"] if scored_symbols else 0.0
    return top_score < 65.0


def _boost_or_insert_fault_symbol(
    rel_f: str,
    sym_name: str,
    analyzer: Any,
    scored_symbols: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    updated: List[Dict[str, Any]] = []
    found = False
    for item in scored_symbols:
        if item["file"] == rel_f and item["name"] == sym_name:
            copied = dict(item)
            copied["score"] = round(copied["score"] + 150.0, 1)
            updated.append(copied)
            found = True
        else:
            updated.append(item)
    if found:
        return updated
    sym_info = analyzer.symbols.get(sym_name)
    if not sym_info:
        return updated
    return updated + [{
        "name": sym_name,
        "short_name": sym_name.split(".")[-1],
        "file": rel_f,
        "type": sym_info["type"],
        "start_line": sym_info["start_line"],
        "end_line": sym_info["end_line"],
        "signature": sym_info["signature"],
        "score": 150.0,
        "info": sym_info,
    }]


def _apply_pytest_fault_boost(
    root_abs: str,
    file_analyzers: Dict[str, Any],
    file_scores: Dict[str, float],
    scored_symbols: List[Dict[str, Any]],
) -> Tuple[Dict[str, float], List[Dict[str, Any]]]:
    """Boost file and symbol scores using pytest failure traceback localization."""
    faults = localize_pytest_faults(root_abs, file_analyzers)
    if not faults:
        return file_scores, scored_symbols
    next_scores = dict(file_scores)
    next_symbols = list(scored_symbols)
    for hit in faults:
        rel_f = hit["file"]
        sym_name = hit["symbol"]
        if rel_f not in file_analyzers:
            continue
        next_scores[rel_f] = next_scores.get(rel_f, 0.0) + 150.0
        next_symbols = _boost_or_insert_fault_symbol(rel_f, sym_name, file_analyzers[rel_f], next_symbols)
    next_symbols.sort(key=lambda x: (-x["score"], x["file"], x["start_line"]))
    return next_scores, next_symbols


def _collect_canonical_repair_suggestions(
    anchor_symbols: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Check anchor symbols against the deterministic AST canonical repair cache."""
    cache = ASTCanonicalRepairCache()
    if not cache.rules:
        return []
    suggestions: List[Dict[str, Any]] = []
    for anc in anchor_symbols:
        code = anc.get("info", {}).get("code", "")
        if not code:
            continue
        hit = cache.lookup_repair(code, anc["short_name"], start_line=anc["start_line"])
        if hit:
            hit["file"] = anc["file"]
            suggestions.append(hit)
    return suggestions


def _choose_primary_file(ranked_files: List[Tuple[str, float]]) -> Optional[str]:
    if not ranked_files:
        return None
    positive = [f for f, sc in ranked_files[:2] if sc > 0]
    if positive:
        return positive[0]
    return ranked_files[0][0]


def _build_fallback_anchors(primary_file: str, file_analyzers: Dict[str, Any]) -> List[Dict[str, Any]]:
    if primary_file not in file_analyzers:
        return []
    anchors = []
    for sym_name, sym_info in list(file_analyzers[primary_file].symbols.items())[:3]:
        anchors.append({
            "name": sym_name,
            "short_name": sym_name.split(".")[-1],
            "file": primary_file,
            "type": sym_info["type"],
            "start_line": sym_info["start_line"],
            "end_line": sym_info["end_line"],
            "signature": sym_info["signature"],
            "score": 10.0,
            "info": sym_info,
        })
    return anchors


def _filter_primary_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if any(s["score"] >= 45.0 for s in candidates):
        return [s for s in candidates if s["score"] > 20.0]
    return candidates


def _select_primary_and_anchors(
    file_scores: Dict[str, float],
    scored_symbols: List[Dict[str, Any]],
    file_analyzers: Dict[str, Any],
    file_lines_map: Dict[str, List[str]],
) -> Tuple[Optional[str], List[str], List[Dict[str, Any]]]:
    """Select the primary implementation file and up to 4 top anchor symbols."""
    ranked_files = sorted(file_scores.items(), key=lambda x: (-x[1], len(file_lines_map.get(x[0], []))))
    primary_file = _choose_primary_file(ranked_files)
    primary_lines = file_lines_map.get(primary_file, []) if primary_file else []

    primary_candidates = [s for s in scored_symbols if s["file"] == primary_file]
    primary_candidates = _filter_primary_candidates(primary_candidates)
    anchor_symbols = primary_candidates[:4]
    if not anchor_symbols and primary_file:
        anchor_symbols = _build_fallback_anchors(primary_file, file_analyzers)

    return primary_file, primary_lines, anchor_symbols


def _should_include_imported_symbol(
    local_alias: str,
    sym_target: str,
    anchor_combined_code: str,
    query_keywords: List[str],
    has_fallback: bool,
) -> bool:
    """Determine whether an imported symbol matches anchors, keywords, or fallback."""
    if re.search(rf"\b{re.escape(local_alias)}\b", anchor_combined_code):
        return True
    if any(kw.lower() in sym_target.lower() for kw in query_keywords):
        return True
    return has_fallback


def _format_import_from_module_name(node: ast.ImportFrom) -> str:
    mod = node.module or ""
    if node.level > 0:
        return ("." * node.level) + mod
    return mod


def _resolve_import_from_rel_path(
    node: ast.ImportFrom,
    primary_abs: str,
    root_abs: str,
    primary_file: str,
) -> Optional[str]:
    mod_name = _format_import_from_module_name(node)
    resolved_abs = resolve_local_module_path(primary_abs, mod_name, root_abs)
    if not resolved_abs:
        return None
    resolved_rel = os.path.relpath(resolved_abs, root_abs)
    if resolved_rel == primary_file:
        return None
    return resolved_rel


def _build_imported_dep(sym_target: str, resolved_rel: str, s_info: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "symbol": sym_target,
        "file": resolved_rel,
        "lines": f"{s_info['start_line']}-{s_info['end_line']}",
        "signature": s_info["signature"],
        "numbered_code": s_info["numbered_code"],
    }


def _get_alias_names(alias: ast.alias) -> Tuple[str, str]:
    target = alias.name
    local = alias.asname if alias.asname else target
    return target, local


def _is_unseen_analyzer_symbol(sym_target: str, imp_analyzer: Any, seen: Set[str]) -> bool:
    return sym_target in imp_analyzer.symbols and sym_target not in seen


def _inspect_import_from_node(
    node: ast.ImportFrom,
    ctx: Dict[str, Any],
    file_analyzers: Dict[str, Any],
    seen_dep_names: Set[str],
    has_existing_deps: bool,
) -> List[Dict[str, Any]]:
    """Inspect local symbols imported by primary file and return matching dependencies."""
    resolved_rel = _resolve_import_from_rel_path(
        node, ctx["primary_abs"], ctx["root_abs"], ctx["primary_file"]
    )
    if not resolved_rel or resolved_rel not in file_analyzers:
        return []

    new_deps: List[Dict[str, Any]] = []
    imp_analyzer = file_analyzers[resolved_rel]
    for alias in node.names:
        sym_target, local_alias = _get_alias_names(alias)
        if not _is_unseen_analyzer_symbol(sym_target, imp_analyzer, seen_dep_names):
            continue
        has_fallback = not (has_existing_deps or new_deps)
        if _should_include_imported_symbol(local_alias, sym_target, ctx["anchor_code"], ctx["keywords"], has_fallback):
            new_deps.append(_build_imported_dep(sym_target, resolved_rel, imp_analyzer.symbols[sym_target]))
            seen_dep_names.add(sym_target)
    return new_deps


def _collect_top_scored_deps(
    scored_symbols: List[Dict[str, Any]],
    primary_file: Optional[str],
    seen_dep_names: Set[str],
) -> List[Dict[str, Any]]:
    deps: List[Dict[str, Any]] = []
    for s in scored_symbols:
        if s["file"] == primary_file or s["name"] in seen_dep_names or s["score"] < 35.0:
            continue
        deps.append({
            "symbol": s["name"],
            "file": s["file"],
            "lines": f"{s['start_line']}-{s['end_line']}",
            "signature": s["signature"],
            "numbered_code": s["info"]["numbered_code"],
        })
        seen_dep_names.add(s["name"])
        if len(deps) >= 3:
            break
    return deps


def _collect_ast_import_deps(
    primary_tree: ast.Module,
    ctx: Dict[str, Any],
    file_analyzers: Dict[str, Any],
    seen_dep_names: Set[str],
    has_existing: bool,
) -> List[Dict[str, Any]]:
    imported_deps: List[Dict[str, Any]] = []
    for node in primary_tree.body:
        if isinstance(node, ast.ImportFrom):
            new_deps = _inspect_import_from_node(
                node, ctx, file_analyzers, seen_dep_names, has_existing or bool(imported_deps)
            )
            imported_deps.extend(new_deps)
    return imported_deps


def _find_cross_file_dependencies(
    scored_symbols: List[Dict[str, Any]],
    primary_file: Optional[str],
    anchor_symbols: List[Dict[str, Any]],
    file_trees_map: Dict[str, ast.Module],
    file_analyzers: Dict[str, Any],
    root_abs: str,
    query_keywords: List[str],
) -> List[Dict[str, Any]]:
    """Identify cross-file helper and type dependencies needed by anchor symbols."""
    seen_dep_names: Set[str] = set()
    cross_file_dependencies = _collect_top_scored_deps(
        scored_symbols, primary_file, seen_dep_names
    )

    if primary_file and primary_file in file_trees_map:
        ctx = {
            "primary_abs": os.path.join(root_abs, primary_file),
            "root_abs": root_abs,
            "primary_file": primary_file,
            "anchor_code": "\n".join(a["info"]["code"] for a in anchor_symbols),
            "keywords": query_keywords,
        }
        imported = _collect_ast_import_deps(
            file_trees_map[primary_file],
            ctx,
            file_analyzers,
            seen_dep_names,
            bool(cross_file_dependencies),
        )
        cross_file_dependencies.extend(imported)

    return cross_file_dependencies


def _find_downstream_callers(
    primary_file: Optional[str],
    anchor_symbols: List[Dict[str, Any]],
    source_files: List[Tuple[str, str]],
) -> List[Dict[str, Any]]:
    """Scan source files for downstream callers of the primary module or its anchors."""
    if not primary_file:
        return []
    primary_mod_stem = os.path.splitext(os.path.basename(primary_file))[0]
    search_terms = {primary_mod_stem} | {a["short_name"] for a in anchor_symbols[:3]}
    downstream_callers: List[Dict[str, Any]] = []
    for rel_path, full_path in source_files:
        if rel_path == primary_file:
            continue
        refs = find_references_in_file(full_path, search_terms)
        if refs:
            downstream_callers.append({
                "file": rel_path,
                "reference_count": len(refs),
                "sample_lines": [f"L{r['line_num']}: {r['line']}" for r in refs[:3]],
            })
    return downstream_callers


def _find_related_tests(
    primary_file: Optional[str],
    anchor_symbols: List[Dict[str, Any]],
    test_files: List[Tuple[str, str]],
) -> List[str]:
    """Find companion test files matching primary target file or its anchors."""
    if not primary_file:
        return [t[0] for t in test_files[:2]] if test_files else []
    primary_stem = os.path.splitext(os.path.basename(primary_file))[0]
    search_terms = {primary_stem} | {a["short_name"] for a in anchor_symbols[:3]}
    related_tests: List[str] = []
    for test_rel, test_full in test_files:
        test_base = os.path.basename(test_rel).lower()
        if primary_stem.lower() in test_base:
            related_tests.insert(0, test_rel)
            continue
        refs = find_references_in_file(test_full, search_terms)
        if refs:
            related_tests.append(test_rel)
    if not related_tests and test_files:
        return [t[0] for t in test_files[:2]]
    return related_tests


def _infer_python_test_cmd(test_file: str, root_abs: str) -> str:
    """Build Python pytest execution command."""
    has_src = os.path.isdir(os.path.join(root_abs, "src"))
    prefix = "PYTHONPATH=src " if has_src else ""
    return f"{prefix}python3 -m pytest -q -p no:cacheprovider {test_file}"


def _infer_node_test_cmd(test_file: str, root_abs: str) -> str:
    """Build Node test command inspecting package.json test scripts."""
    pkg_path = os.path.join(root_abs, "package.json")
    if not os.path.isfile(pkg_path):
        return f"npx vitest run {test_file}"
    try:
        with open(pkg_path, "r", encoding="utf-8") as pf:
            pkg_d = json.load(pf)
        scripts = pkg_d.get("scripts", {})
        if "test" in scripts and "vitest" in scripts["test"]:
            return f"npx vitest run {test_file}"
        if "test" in scripts and "jest" in scripts["test"]:
            return f"npx jest {test_file}"
        return f"npm test -- {test_file}"
    except Exception as e:
        logger.debug("Failed reading package.json for test command: %s", e)
        return f"npx vitest run {test_file}"


def _check_flutter_in_pubspec(pubspec_path: str) -> bool:
    """Check if pubspec.yaml includes flutter dependencies."""
    try:
        with open(pubspec_path, "r", encoding="utf-8") as pf:
            return "flutter:" in pf.read()
    except OSError as e:
        logger.debug("Failed reading pubspec.yaml at %s: %s", pubspec_path, e)
    return False


def _find_dart_package_root(test_full_p: str, root_abs: str) -> Tuple[str, bool]:
    """Find the enclosing package directory and detect flutter usage."""
    cur_dir = os.path.dirname(test_full_p)
    while cur_dir and len(cur_dir) >= len(root_abs):
        cand_pub = os.path.join(cur_dir, "pubspec.yaml")
        if os.path.isfile(cand_pub):
            return cur_dir, _check_flutter_in_pubspec(cand_pub)
        parent = os.path.dirname(cur_dir)
        if parent == cur_dir:
            break
        cur_dir = parent
    return root_abs, False


def _infer_dart_test_cmd(test_file: str, root_abs: str) -> str:
    """Build Dart / Flutter test execution command."""
    test_full_p = os.path.join(root_abs, test_file)
    pkg_dir, is_flutter = _find_dart_package_root(test_full_p, root_abs)
    pkg_rel = os.path.relpath(pkg_dir, root_abs)
    test_sub_rel = os.path.relpath(test_full_p, pkg_dir)
    runner = "flutter test" if is_flutter else "dart test"
    if pkg_rel and pkg_rel != ".":
        return f"cd {pkg_rel} && {runner} {test_sub_rel}"
    return f"{runner} {test_sub_rel}"


def _infer_jvm_test_cmd(test_file: str, root_abs: str) -> str:
    """Build Gradle or Maven JVM test execution command."""
    gradlew = os.path.join(root_abs, "gradlew")
    pom = os.path.join(root_abs, "pom.xml")
    stem = os.path.splitext(os.path.basename(test_file))[0]
    if os.path.isfile(gradlew):
        return f"./gradlew test --tests {stem}"
    if os.path.isfile(os.path.join(root_abs, "build.gradle")) or os.path.isfile(os.path.join(root_abs, "build.gradle.kts")):
        return f"gradle test --tests {stem}"
    if os.path.isfile(pom):
        return f"mvn test -Dtest={stem}"
    return f"./gradlew test --tests {stem}"


def _infer_go_test_cmd(test_file: str) -> str:
    """Build Go test execution command."""
    t_dir = os.path.dirname(test_file)
    return f"go test -v ./{t_dir}" if t_dir else "go test -v ."


def _infer_rust_test_cmd(test_file: str) -> str:
    """Build Cargo test execution command."""
    stem = os.path.splitext(os.path.basename(test_file))[0]
    return f"cargo test --test {stem}"


def _dispatch_language_test_cmd(t_ext: str, test_file: str, root_abs: str) -> Optional[str]:
    if t_ext == ".py":
        return _infer_python_test_cmd(test_file, root_abs)
    if t_ext in (".ts", ".tsx", ".js", ".jsx"):
        return _infer_node_test_cmd(test_file, root_abs)
    if t_ext == ".go":
        return _infer_go_test_cmd(test_file)
    if t_ext == ".rs":
        return _infer_rust_test_cmd(test_file)
    if t_ext == ".dart":
        return _infer_dart_test_cmd(test_file, root_abs)
    if t_ext in (".kt", ".kts", ".java"):
        return _infer_jvm_test_cmd(test_file, root_abs)
    return None


def _infer_test_command(
    test_file: Optional[str],
    root_abs: str,
    test_command: Optional[str],
) -> Optional[str]:
    """Infer the appropriate unit test runner command for the primary test file."""
    if test_command:
        return test_command
    if not test_file:
        return None
    t_ext = os.path.splitext(test_file)[1].lower()
    return _dispatch_language_test_cmd(t_ext, test_file, root_abs)


def _plan_target_location_step(primary_file: str, total_lines: int, anchor_symbols: List[Dict[str, Any]]) -> str:
    if anchor_symbols:
        top_anc = anchor_symbols[0]
        return (
            f"1. **Target Location:** Edit `{primary_file}` ({total_lines} lines) near `{top_anc['name']}` "
            f"(lines {top_anc['start_line']}–{top_anc['end_line']}), following its existing signature and return convention."
        )
    return f"1. **Target Location:** Implement the requested change in `{primary_file}` ({total_lines} lines)."


def _plan_dependency_step(
    primary_file: str,
    cross_file_dependencies: List[Dict[str, Any]],
    file_analyzers: Dict[str, Any],
) -> Optional[str]:
    if cross_file_dependencies:
        dep_summary = ", ".join(f"`{d['symbol']}` ({d['file']}:{d['lines']})" for d in cross_file_dependencies[:3])
        return f"2. **Reuse Existing Dependencies:** Call verified project helpers/types: {dep_summary}."
    if primary_file in file_analyzers:
        imports_preview = ", ".join(f"`{imp}`" for imp in file_analyzers[primary_file].imports[:3])
        if imports_preview:
            return f"2. **Module Imports Available:** `{primary_file}` already imports {imports_preview}."
    return None


def _plan_contract_step(
    primary_file: str,
    downstream_callers: List[Dict[str, Any]],
    file_analyzers: Dict[str, Any],
) -> Optional[str]:
    analyzer = file_analyzers.get(primary_file)
    if analyzer and analyzer.exports_all is not None:
        return f"3. **Export Contract:** `{primary_file}` defines `__all__`; add any new public symbol to `__all__` if it must be exported."
    if downstream_callers:
        caller_files = ", ".join(f"`{c['file']}`" for c in downstream_callers[:3])
        return f"3. **Downstream Impact:** Preserving existing signatures in `{primary_file}` keeps callers ({caller_files}) compatible."
    return None


def _plan_verification_step(task: str, project_root: str, inferred_test_cmd: Optional[str]) -> str:
    verify_cmd = f'arch-sidecar feature "{task}" {project_root} --verify'
    if inferred_test_cmd:
        verify_cmd += f' --test "{inferred_test_cmd}"'
    return f"4. **Preflight & Verification:** After editing, run `{verify_cmd}` to execute AST scope/import preflight + test verification."


def _build_implementation_plan(plan_ctx: Dict[str, Any]) -> List[str]:
    """Generate ordered, actionable implementation steps."""
    primary_file = plan_ctx.get("primary_file")
    if not primary_file:
        return []

    plan: List[str] = [
        _plan_target_location_step(
            primary_file,
            len(plan_ctx.get("primary_lines", [])),
            plan_ctx.get("anchor_symbols", []),
        )
    ]

    step2 = _plan_dependency_step(
        primary_file,
        plan_ctx.get("cross_file_dependencies", []),
        plan_ctx.get("file_analyzers", {}),
    )
    if step2:
        plan.append(step2)

    step3 = _plan_contract_step(
        primary_file,
        plan_ctx.get("downstream_callers", []),
        plan_ctx.get("file_analyzers", {}),
    )
    if step3:
        plan.append(step3)

    plan.append(
        _plan_verification_step(
            plan_ctx.get("task", ""),
            plan_ctx.get("project_root", "."),
            plan_ctx.get("inferred_test_cmd"),
        )
    )
    for repair in plan_ctx.get("canonical_repairs", []):
        plan.append(
            f"5. **Deterministic AST Repair Cache Hit (`{repair['function']}` L{repair['line_number']}):** "
            f"Replace `{repair['buggy_line']}` with `{repair['suggested_line'].strip()}`."
        )
    return plan


def _format_impact_map_items(ctx: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    if ctx.get("anchor_symbols"):
        anc_str = ", ".join(f"`{a['name']}` (L{a['start_line']}-{a['end_line']})" for a in ctx["anchor_symbols"])
        lines.append(f"- **Relevant Existing Symbols:** {anc_str}")
    if ctx.get("cross_file_dependencies"):
        dep_str = ", ".join(f"`{d['symbol']}` in `{d['file']}` (L{d['lines']})" for d in ctx["cross_file_dependencies"][:4])
        lines.append(f"- **Cross-File Dependencies:** {dep_str}")
    if ctx.get("cross_boundary_links"):
        cb_str = ", ".join(f"`{c['token']}` in `{c['file']}:L{c['line']}`" for c in ctx["cross_boundary_links"][:4])
        lines.append(f"- **Cross-Boundary Links (Routes / Env):** {cb_str}")
    if ctx.get("downstream_callers"):
        cal_str = ", ".join(f"`{c['file']}` ({c['reference_count']} refs)" for c in ctx["downstream_callers"][:4])
        lines.append(f"- **Downstream Callers / Blast Radius:** {cal_str}")
    else:
        lines.append("- **Downstream Callers / Blast Radius:** Local to target module (0 external callers matched)")
    if ctx.get("related_tests"):
        lines.append(f"- **Related Test Suite:** {', '.join(f'`{t}`' for t in ctx['related_tests'][:3])}")
    if ctx.get("inferred_test_cmd"):
        lines.append(f"- **Verification Command:** `{ctx['inferred_test_cmd']}`")
    return lines


def _build_overview_context_blocks(overview_ctx: Dict[str, Any]) -> List[str]:
    """Build high-level context blocks: header, plan, and impact map."""
    primary_target = overview_ctx.get("primary_file") if overview_ctx.get("primary_file") else "N/A"
    blocks: List[str] = [
        "# Devrith Feature Workflow Plan & Context",
        "> **Principle:** Understand before changing. Check before trusting. Verify before claiming success.",
        f"> **Detected Intent:** `{overview_ctx['intent']['category']}` | **Primary Target:** `{primary_target}`\n",
        "## 1. Actionable Implementation Plan",
    ]
    for step_line in overview_ctx.get("implementation_plan", []):
        blocks.append(step_line)

    blocks.append("\n## 2. Dependency & Impact Map")
    blocks.extend(_format_impact_map_items(overview_ctx))
    return blocks


def _compute_import_end_line(primary_file: str, file_analyzers: Dict[str, Any]) -> int:
    if primary_file not in file_analyzers:
        return 25
    analyzer = file_analyzers[primary_file]
    val = analyzer.import_end_line if analyzer.import_end_line is not None else 25
    return min(val, 45)


def _build_module_header_slice(
    primary_file: str,
    primary_lines: List[str],
    file_analyzers: Dict[str, Any],
) -> Tuple[List[str], int, int]:
    total_lines = len(primary_lines)
    imp_end = _compute_import_end_line(primary_file, file_analyzers)
    header_numbered = "".join(f"{i + 1}: {primary_lines[i]}" for i in range(min(imp_end, total_lines))).rstrip()
    blocks = [
        f"### Module Header & Imports (`{primary_file}:1-{imp_end}`)",
        "```python",
        header_numbered,
        "```",
    ]
    return blocks, imp_end, estimate_tokens(header_numbered)


def _format_large_class_slice(
    anc: Dict[str, Any],
    primary_file: str,
    primary_lines: List[str],
) -> Tuple[List[str], List[Tuple[int, int]], int]:
    s_line, e_line = anc["start_line"], anc["end_line"]
    head_end = min(s_line + 19, e_line)
    cls_tail_start = max(head_end + 1, e_line - 14)
    head_code = "".join(f"{i}: {primary_lines[i - 1]}" for i in range(s_line, head_end + 1)).rstrip()
    tail_code = "".join(f"{i}: {primary_lines[i - 1]}" for i in range(cls_tail_start, e_line + 1)).rstrip()
    blocks = [
        f"### Class Header & Tail: `{anc['name']}` (`{primary_file}:{s_line}-{head_end}` & `{cls_tail_start}-{e_line}`)",
        "```python",
        head_code,
        "# ... (intermediate methods omitted; see matched method anchors below) ...",
        tail_code,
        "```",
    ]
    ranges = [(s_line, head_end), (cls_tail_start, e_line)]
    tokens = estimate_tokens(head_code + tail_code)
    return blocks, ranges, tokens


def _is_covered_by_emitted_ranges(s_line: int, e_line: int, emitted_ranges: List[Tuple[int, int]]) -> bool:
    for r_start, r_end in emitted_ranges:
        if r_start <= s_line and e_line <= r_end:
            return True
    return False


def _build_surgical_target_slices(
    primary_file: str,
    primary_lines: List[str],
    file_analyzers: Dict[str, Any],
    anchor_symbols: List[Dict[str, Any]],
    budget_params: Tuple[int, int],
) -> List[str]:
    """Build targeted 1-indexed code slices for large target modules."""
    token_budget, initial_used_tokens = budget_params
    total_lines = len(primary_lines)
    blocks: List[str] = [
        f"\n## 3. Surgical Target Context: `{primary_file}` ({total_lines} lines total, 1-indexed slices)",
        "> ⚡ **Fast-Path Ready:** Exact 1-indexed line numbers are included below so you can edit directly around these lines.",
    ]
    header_blocks, imp_end, header_tokens = _build_module_header_slice(
        primary_file, primary_lines, file_analyzers
    )
    blocks.extend(header_blocks)
    used_tokens = initial_used_tokens + header_tokens
    emitted_ranges: List[Tuple[int, int]] = [(1, imp_end)]

    for anc in anchor_symbols:
        s_line, e_line = anc["start_line"], anc["end_line"]
        if _is_covered_by_emitted_ranges(s_line, e_line, emitted_ranges):
            continue

        if anc["type"] == "class" and (e_line - s_line) > 70:
            cls_blocks, ranges, cls_tokens = _format_large_class_slice(anc, primary_file, primary_lines)
            blocks.extend(cls_blocks)
            emitted_ranges.extend(ranges)
            used_tokens += cls_tokens
            continue

        anc_code = anc["info"]["numbered_code"].rstrip()
        anc_tokens = estimate_tokens(anc_code)
        if used_tokens + anc_tokens > token_budget and anc != anchor_symbols[0]:
            break
        blocks.extend([
            f"### Anchor Implementation: `{anc['name']}` (`{primary_file}:{s_line}-{e_line}`)",
            "```python",
            anc_code,
            "```",
        ])
        emitted_ranges.append((s_line, e_line))
        used_tokens += anc_tokens

    tail_start = max(1, total_lines - 14)
    if not _is_covered_by_emitted_ranges(tail_start, total_lines, emitted_ranges):
        tail_numbered = "".join(
            f"{i}: {primary_lines[i - 1]}" for i in range(tail_start, total_lines + 1)
        ).rstrip()
        blocks.extend([
            f"### End of File Region (`{primary_file}:{tail_start}-{total_lines}`)",
            "```python",
            tail_numbered,
            "```",
        ])
    return blocks


def _build_target_context_blocks(
    primary_file: Optional[str],
    primary_lines: List[str],
    file_analyzers: Dict[str, Any],
    anchor_symbols: List[Dict[str, Any]],
    budget_params: Tuple[int, int],
) -> List[str]:
    """Format full or surgical 1-indexed target file context."""
    if not primary_file:
        return []
    total_lines = len(primary_lines)
    if total_lines <= 160:
        numbered_full = "".join(f"{i + 1}: {line}" for i, line in enumerate(primary_lines)).rstrip()
        return [
            f"\n## 3. Target Module Context: `{primary_file}` (Full File, {total_lines} lines, 1-indexed)",
            "> ⚡ **Fast-Path Ready:** 1-indexed line numbers are included below. Edit directly without calling `view_file`.",
            "```python",
            numbered_full,
            "```",
        ]
    return _build_surgical_target_slices(
        primary_file,
        primary_lines,
        file_analyzers,
        anchor_symbols,
        budget_params,
    )


def _build_dependency_context_blocks(
    cross_file_dependencies: List[Dict[str, Any]],
) -> List[str]:
    """Format cross-file dependency definitions for context markdown."""
    if not cross_file_dependencies:
        return []
    blocks = ["\n## 4. Cross-File Dependency Signatures & Definitions"]
    for dep in cross_file_dependencies[:3]:
        dep_lines = dep["numbered_code"].rstrip().splitlines()
        dep_snippet = "\n".join(dep_lines[:25]) + "\n# ... (body truncated)" if len(dep_lines) > 25 else "\n".join(dep_lines)
        blocks.extend([
            f"### `{dep['symbol']}` (`{dep['file']}:{dep['lines']}`)",
            "```python",
            dep_snippet,
            "```",
        ])
    return blocks


def _build_test_context_blocks(
    related_tests: List[str],
    root_abs: str,
) -> List[str]:
    """Format companion test suite fast-path tail for immediate appending."""
    if not related_tests:
        return []
    top_test_rel = related_tests[0]
    top_test_abs = os.path.join(root_abs, top_test_rel)
    try:
        with open(top_test_abs, "r", encoding="utf-8", errors="ignore") as tf:
            t_lines = tf.readlines()
        t_total = len(t_lines)
        t_tail_start = max(1, t_total - 17)
        t_tail_numbered = "".join(f"{i}: {t_lines[i - 1]}" for i in range(t_tail_start, t_total + 1)).rstrip()
        return [
            f"\n## 5. Related Test Suite Fast-Path Context: `{top_test_rel}` (lines {t_tail_start}–{t_total} of {t_total}, 1-indexed)",
            "> ⚡ **Fast-Path Ready:** Append new unit tests directly at the end of this file using the 1-indexed lines below.",
            "```python",
            t_tail_numbered,
            "```",
        ]
    except OSError as e:
        logger.debug("Failed reading test context from %s: %s", top_test_abs, e)
        return []


def _build_context_markdown(ctx: Dict[str, Any]) -> str:
    """Compose complete 1-indexed context markdown for the coding agent."""
    blocks = _build_overview_context_blocks(ctx)
    initial_used_tokens = estimate_tokens("\n".join(blocks))
    budget_params = (ctx["token_budget"], initial_used_tokens)
    blocks.extend(
        _build_target_context_blocks(
            ctx.get("primary_file"),
            ctx.get("primary_lines", []),
            ctx.get("file_analyzers", {}),
            ctx.get("anchor_symbols", []),
            budget_params,
        )
    )
    blocks.extend(_build_dependency_context_blocks(ctx.get("cross_file_dependencies", [])))
    blocks.extend(_build_test_context_blocks(ctx.get("related_tests", []), ctx["root_abs"]))
    return "\n".join(blocks)


def _resolve_files_to_verify(
    target_files: Optional[List[str]],
    explicit_single_file: Optional[str],
    primary_file: Optional[str],
    root_abs: str,
) -> List[str]:
    """Determine the list of source files requiring preflight check verification."""
    files_to_check: List[str] = []
    if target_files:
        files_to_check.extend(target_files)
    elif explicit_single_file:
        files_to_check.append(explicit_single_file)
    else:
        git_mod = detect_git_modified_source_files(root_abs)
        if git_mod:
            files_to_check.extend(git_mod)
        elif primary_file:
            files_to_check.append(primary_file)
    return list(dict.fromkeys(files_to_check))


def _run_preflight_checks(
    files_to_check: List[str],
    root_abs: str,
) -> Tuple[List[Dict[str, Any]], List[str], List[str], bool]:
    """Execute compiler-grade preflight checks across target files."""
    preflight_results: List[Dict[str, Any]] = []
    statically_verified: List[str] = []
    unverified_risks: List[str] = []
    total_errors = 0

    for rel_f in files_to_check:
        abs_f = rel_f if os.path.isabs(rel_f) else os.path.join(root_abs, rel_f)
        health = check_code_health(abs_f, project_root=root_abs)
        preflight_results.append(health)
        total_errors += health.get("errors", 0)

    if total_errors == 0 and preflight_results:
        checked_names = ", ".join(r["file"] for r in preflight_results)
        statically_verified.append(f"AST syntax valid across {len(preflight_results)} file(s): {checked_names}")
        statically_verified.append("Two-pass lexical scope binding passed: 0 undefined or hallucinated symbols")
        statically_verified.append("Cross-module local import resolution passed: all imported local symbols exist")
        statically_verified.append("Module import DAG check passed: 0 circular import cycles detected")
        return preflight_results, statically_verified, unverified_risks, True

    for r in preflight_results:
        for issue in r.get("issues", []):
            unverified_risks.append(
                f"[PREFLIGHT {issue['severity']}] {r['file']}:{issue['line']} ({issue['type']}): {issue['message']}"
            )
    return preflight_results, statically_verified, unverified_risks, False


def _run_test_verification(
    cmd_to_run: Optional[str],
    root_abs: str,
) -> Tuple[Optional[Dict[str, Any]], List[str], List[str], bool]:
    """Execute companion test suite and parse pass/fail counts."""
    if not cmd_to_run:
        return None, [], ["No runtime test command was supplied or discovered; runtime behavior is NOT verified by automated tests."], True

    normalized_cmd = _normalize_test_command_for_root(cmd_to_run, root_abs)
    t_test = time.perf_counter()
    try:
        res = subprocess.run(
            normalized_cmd,
            cwd=root_abs,
            shell=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        test_dur_ms = round((time.perf_counter() - t_test) * 1000, 2)
        combined_out = (res.stdout + "\n" + res.stderr).strip()
        passed_match = re.search(r"(\d+)\s+passed", combined_out)
        failed_match = re.search(r"(\d+)\s+failed", combined_out)
        passed_count = int(passed_match.group(1)) if passed_match else (1 if res.returncode == 0 else 0)
        failed_count = int(failed_match.group(1)) if failed_match else (0 if res.returncode == 0 else 1)

        test_verification = {
            "command": normalized_cmd,
            "passed": res.returncode == 0,
            "returncode": res.returncode,
            "passed_count": passed_count,
            "failed_count": failed_count,
            "duration_ms": test_dur_ms,
            "output": combined_out[-2000:],
        }
        if res.returncode == 0:
            runtime_verified = [
                f"Test suite passed: `{normalized_cmd}` (exit 0, {passed_count} passed in {test_dur_ms}ms)"
            ]
            return test_verification, runtime_verified, [], True
        risk = [
            f"Runtime test failure: `{normalized_cmd}` exited with code {res.returncode} ({failed_count} failed)"
        ]
        return test_verification, [], risk, False
    except Exception as e:
        logger.debug("Test execution error for %s: %s", normalized_cmd, e)
        test_verification = {
            "command": normalized_cmd,
            "passed": False,
            "error": str(e),
        }
        return test_verification, [], [f"Test execution error for `{normalized_cmd}`: {e}"], False


def _run_verification_stage(
    verify: bool,
    target_files: Optional[List[str]],
    explicit_single_file: Optional[str],
    primary_file: Optional[str],
    root_abs: str,
    test_command: Optional[str],
    inferred_test_cmd: Optional[str],
    uncertainty_findings: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]], List[str], List[str], List[str], bool]:
    """Coordinate preflight checks, test executions, and verification ledgers."""
    if not verify:
        return [], None, [], [], [
            "Orientation-only run (`--verify` not yet executed). Run `arch-sidecar feature ... --verify` after making edits to verify preflight + tests."
        ], True

    files_to_check = _resolve_files_to_verify(target_files, explicit_single_file, primary_file, root_abs)
    pf_results, stat_ver, unver_risks, pf_ok = _run_preflight_checks(files_to_check, root_abs)

    cmd_to_run = test_command or inferred_test_cmd
    test_ver, runt_ver, test_risks, test_ok = _run_test_verification(cmd_to_run, root_abs)
    unver_risks.extend(test_risks)

    if uncertainty_findings:
        unknown_u = [u for u in uncertainty_findings if u.get("confidence") == ConfidenceLevel.UNKNOWN]
        if unknown_u:
            unver_risks.append(
                f"{len(unknown_u)} dynamic reflection site(s) (`getattr`/`eval`/`importlib`) exist in `{primary_file}`; static call graph cannot bound dynamic string dispatch."
            )

    overall_success = pf_ok and test_ok
    return pf_results, test_ver, stat_ver, runt_ver, unver_risks, overall_success


def _format_feature_workflow_output(
    session_id: Optional[str],
    overall_success: bool,
    verify: bool,
    task: str,
    intent: Dict[str, Any],
    primary_file: Optional[str],
    anchor_symbols: List[Dict[str, Any]],
    cross_file_dependencies: List[Dict[str, Any]],
    cross_boundary_links: List[Dict[str, Any]],
    downstream_callers: List[Dict[str, Any]],
    related_tests: List[str],
    inferred_test_cmd: Optional[str],
    uncertainty_findings: List[Dict[str, Any]],
    implementation_plan: List[str],
    context_markdown: str,
    preflight_results: List[Dict[str, Any]],
    test_verification: Optional[Dict[str, Any]],
    statically_verified: List[str],
    runtime_verified: List[str],
    unverified_risks: List[str],
    duration_ms: float,
) -> Dict[str, Any]:
    """Format final feature workflow dictionary output."""
    return {
        "session_id": session_id,
        "success": overall_success,
        "mode": "verify" if verify else "plan_and_context",
        "task": task,
        "intent": intent,
        "primary_file": primary_file,
        "anchor_symbols": [
            {
                "name": a["name"],
                "type": a["type"],
                "file": a["file"],
                "start_line": a["start_line"],
                "end_line": a["end_line"],
                "signature": a["signature"],
                "score": a["score"],
            }
            for a in anchor_symbols
        ],
        "impact_map": {
            "cross_file_dependencies": [
                {
                    "symbol": d["symbol"],
                    "file": d["file"],
                    "lines": d["lines"],
                    "signature": d["signature"],
                }
                for d in cross_file_dependencies
            ],
            "cross_boundary_links": cross_boundary_links,
            "downstream_callers": downstream_callers,
            "related_tests": related_tests,
            "inferred_test_command": inferred_test_cmd,
            "uncertainty_findings": uncertainty_findings,
        },
        "implementation_plan": implementation_plan,
        "context": context_markdown,
        "estimated_tokens": estimate_tokens(context_markdown),
        "preflight_checks": preflight_results,
        "test_verification": test_verification,
        "verification_ledger": {
            "statically_verified": statically_verified,
            "runtime_verified": runtime_verified,
            "unverified_or_residual_risks": unverified_risks,
        },
        "duration_ms": duration_ms,
    }


def _normalize_feature_intent(
    task: str,
    root_abs: str,
    target_files: Optional[List[str]],
) -> Tuple[str, Optional[str], Dict[str, Any]]:
    explicit_single_file: Optional[str] = None
    if os.path.isfile(root_abs):
        explicit_single_file = os.path.basename(root_abs)
        root_abs = os.path.dirname(root_abs)

    intent = classify_feature_intent(task)
    if explicit_single_file and explicit_single_file not in intent["explicit_files"]:
        intent["explicit_files"].insert(0, explicit_single_file)
    if target_files:
        for tf in target_files:
            if tf not in intent["explicit_files"]:
                intent["explicit_files"].append(tf)

    return root_abs, explicit_single_file, intent


def _scan_uncertainty_findings(primary_file: Optional[str], root_abs: str) -> List[Dict[str, Any]]:
    if not primary_file:
        return []
    u_list = scan_file_uncertainty(os.path.join(root_abs, primary_file), primary_file)
    return [u.to_dict() for u in u_list]


def _record_feature_session(
    task: str,
    root_abs: str,
    session_params: Dict[str, Any],
) -> Optional[str]:
    return record_session(
        command="feature",
        task=task,
        project_root=root_abs,
        success=session_params["overall_success"],
        duration_ms=session_params["duration_ms"],
        mode="verify" if session_params["verify"] else "plan_and_context",
        target_files=session_params["target_files"],
        target_symbols=session_params["target_symbols"],
        estimated_tokens=session_params["estimated_tokens"],
        preflight=session_params["preflight_results"],
        test_run=session_params["test_verification"],
        verification_ledger=session_params["ledger"],
    )


def run_feature_workflow(
    task: str,
    project_root: str = ".",
    target_files: Optional[List[str]] = None,
    target_symbols: Optional[List[str]] = None,
    verify: bool = False,
    test_command: Optional[str] = None,
    token_budget: int = 3200,
) -> Dict[str, Any]:
    """
    Evidence-Backed, Architecture-Aware Feature Workflow:
    "Understand before changing. Check before trusting. Verify before claiming success."

    1. Understand the requested change (`intent`)
    2. Find the relevant existing implementation (`target_files`, `anchor_symbols`)
    3. Map dependencies and likely impact (`impact_map`: callees, callers, tests, uncertainty)
    4. Produce an actionable implementation plan (`implementation_plan`)
    5. Supply the coding agent with targeted 1-indexed context (`context_markdown`)
    6. Run appropriate preflight checks (`preflight_checks` via `check_code_health`)
    7. Verify the resulting changes (`test_verification`)
    8. Clearly report what is and isn't verified (`verification_ledger`)
    """
    t0 = time.perf_counter()
    root_abs, explicit_single_file, intent = _normalize_feature_intent(
        task, os.path.abspath(project_root), target_files
    )

    query_keywords = list(dict.fromkeys((target_symbols if target_symbols else []) + intent["keywords"]))

    # 2. Discovery & Scoring
    source_files, test_files = _discover_repo_files(root_abs)
    file_analyzers, file_lines_map, file_trees_map = _analyze_source_files(source_files)
    file_scores, scored_symbols = _score_files_and_symbols(
        file_analyzers, intent, query_keywords, target_symbols
    )
    if _should_run_pytest_fault_localizer(verify, intent, target_symbols, scored_symbols, test_files):
        file_scores, scored_symbols = _apply_pytest_fault_boost(
            root_abs, file_analyzers, file_scores, scored_symbols
        )

    primary_file, primary_lines, anchor_symbols = _select_primary_and_anchors(
        file_scores, scored_symbols, file_analyzers, file_lines_map
    )
    canonical_repairs = _collect_canonical_repair_suggestions(anchor_symbols)

    # 3. Dependencies & Impact
    cross_file_dependencies = _find_cross_file_dependencies(
        scored_symbols, primary_file, anchor_symbols, file_trees_map, file_analyzers, root_abs, query_keywords
    )
    downstream_callers = _find_downstream_callers(primary_file, anchor_symbols, source_files)
    related_tests = _find_related_tests(primary_file, anchor_symbols, test_files)
    primary_test = related_tests[0] if related_tests else None
    inferred_test_cmd = _infer_test_command(primary_test, root_abs, test_command)

    uncertainty_findings = _scan_uncertainty_findings(primary_file, root_abs)
    cross_boundary_links = detect_cross_boundary_links(
        primary_file=primary_file if primary_file else "",
        primary_lines=primary_lines,
        anchor_symbols=anchor_symbols,
        file_lines_map=file_lines_map,
    )

    # 4. Actionable Implementation Plan
    plan_ctx = {
        "primary_file": primary_file,
        "primary_lines": primary_lines,
        "anchor_symbols": anchor_symbols,
        "cross_file_dependencies": cross_file_dependencies,
        "downstream_callers": downstream_callers,
        "file_analyzers": file_analyzers,
        "task": task,
        "project_root": project_root,
        "inferred_test_cmd": inferred_test_cmd,
        "canonical_repairs": canonical_repairs,
    }
    implementation_plan = _build_implementation_plan(plan_ctx)

    # 5. Targeted 1-Indexed Context
    context_data = {
        "intent": intent,
        "primary_file": primary_file,
        "primary_lines": primary_lines,
        "implementation_plan": implementation_plan,
        "anchor_symbols": anchor_symbols,
        "cross_file_dependencies": cross_file_dependencies,
        "cross_boundary_links": cross_boundary_links,
        "downstream_callers": downstream_callers,
        "related_tests": related_tests,
        "inferred_test_cmd": inferred_test_cmd,
        "file_analyzers": file_analyzers,
        "root_abs": root_abs,
        "token_budget": token_budget,
    }
    context_markdown = _build_context_markdown(context_data)

    # 6, 7, 8. Verification & Ledger
    preflight_results, test_verification, statically_verified, runtime_verified, unverified_risks, overall_success = _run_verification_stage(
        verify, target_files, explicit_single_file, primary_file, root_abs,
        test_command, inferred_test_cmd, uncertainty_findings
    )

    duration_ms = round((time.perf_counter() - t0) * 1000, 2)
    session_target_files = [primary_file] if primary_file else (target_files if target_files else [])
    session_params = {
        "overall_success": overall_success,
        "duration_ms": duration_ms,
        "verify": verify,
        "target_files": session_target_files,
        "target_symbols": [a["name"] for a in anchor_symbols[:3]],
        "estimated_tokens": estimate_tokens(context_markdown),
        "preflight_results": preflight_results,
        "test_verification": test_verification,
        "ledger": {
            "statically_verified": statically_verified,
            "runtime_verified": runtime_verified,
            "unverified_or_residual_risks": unverified_risks,
        },
    }
    session_id = _record_feature_session(task, root_abs, session_params)

    out = _format_feature_workflow_output(
        session_id, overall_success, verify, task, intent, primary_file,
        anchor_symbols, cross_file_dependencies, cross_boundary_links,
        downstream_callers, related_tests, inferred_test_cmd, uncertainty_findings,
        implementation_plan, context_markdown, preflight_results, test_verification,
        statically_verified, runtime_verified, unverified_risks, duration_ms
    )
    out["canonical_repair_suggestions"] = canonical_repairs
    return out
