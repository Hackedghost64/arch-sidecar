"""
Polyglot Resolver & In-Process Lexical Scope Binding Engine for Devrith.

Provides:
1. Go and Rust local import path resolution and module mapping.
2. In-process circular import cycle detection for Go and Rust (0 external binaries).
3. In-process lexical scope binding analysis for TypeScript, Go, and Rust:
   Detects hallucinated/undefined variables and typos with 0 external compiler binaries.
"""

import logging
import os
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

from .dart_ast import parse_dart_ast
from .go_ast import parse_go_ast
from .jvm_ast import parse_jvm_ast
from .rust_ast import parse_rust_ast
from .ts_ast import parse_ts_js_ast

logger = logging.getLogger(__name__)


def _read_file_text(path: str) -> str:
    """Safely reads file content as utf-8 string, ignoring decoding errors."""
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


# ---------------------------------------------------------------------------
# 1. Polyglot Import Resolvers
# ---------------------------------------------------------------------------

def _find_go_entry_file(cand_dir: str) -> Optional[str]:
    """Finds the first non-test .go file inside cand_dir, if directory exists."""
    if not os.path.isdir(cand_dir):
        return None
    for f in os.listdir(cand_dir):
        if f.endswith(".go") and not f.endswith("_test.go"):
            return os.path.join(cand_dir, f)
    return None


def _parse_go_mod_content(content: str) -> str:
    """Extracts module prefix from go.mod file content."""
    for line in content.splitlines():
        if line.startswith("module "):
            parts = line.split()
            if len(parts) > 1:
                return parts[1].strip()
    return ""


def _read_go_module_name(go_mod_path: str) -> str:
    """Reads and parses the module name declared in go.mod."""
    if not os.path.isfile(go_mod_path):
        return ""
    try:
        content = _read_file_text(go_mod_path)
        return _parse_go_mod_content(content)
    except (OSError, UnicodeDecodeError) as exc:
        logger.debug("Failed reading go.mod at %s: %s", go_mod_path, exc)
        return ""


def resolve_go_import_path(import_path: str, from_file: str, root_dir: str) -> Optional[str]:
    """
    Resolves a Go import path (e.g., 'myproject/pkg/util' or relative './sub')
    to an entry .go file or directory in root_dir.
    """
    root_abs = os.path.abspath(root_dir)
    if import_path.startswith(("./", "../")):
        from_dir = os.path.dirname(os.path.abspath(from_file))
        cand_dir = os.path.abspath(os.path.join(from_dir, import_path))
        return _find_go_entry_file(cand_dir)

    mod_prefix = _read_go_module_name(os.path.join(root_abs, "go.mod"))
    if mod_prefix and import_path.startswith(mod_prefix):
        sub_path = import_path[len(mod_prefix):].lstrip("/")
        res = _find_go_entry_file(os.path.join(root_abs, sub_path))
        if res:
            return res

    direct_dir = os.path.join(root_abs, import_path.split("/")[-1])
    return _find_go_entry_file(direct_dir)


def _find_rust_module_file(base_dir: str, parts: List[str]) -> Optional[str]:
    """Checks for <base>/<parts>.rs or <base>/<parts>/mod.rs."""
    cand_file = os.path.join(base_dir, *parts) + ".rs"
    if os.path.isfile(cand_file):
        return cand_file
    cand_mod = os.path.join(base_dir, *parts, "mod.rs")
    if os.path.isfile(cand_mod):
        return cand_mod
    return None


def _resolve_rust_base_and_parts(
    parts: List[str], from_file: str, root_abs: str
) -> Tuple[str, List[str]]:
    """Determines base directory and module parts from Rust use segments."""
    src_dir = os.path.join(root_abs, "src")
    default_base = src_dir if os.path.isdir(src_dir) else root_abs

    if parts[0] == "crate":
        return default_base, parts[1:]
    if parts[0] == "super":
        from_dir = os.path.dirname(os.path.abspath(from_file))
        return os.path.dirname(from_dir), parts[1:]

    from_dir = os.path.dirname(os.path.abspath(from_file))
    mod_parts = [p for p in parts if p != "self"]
    return from_dir, mod_parts


def resolve_rust_import_path(use_stmt: str, from_file: str, root_dir: str) -> Optional[str]:
    """
    Resolves a Rust `use` path (e.g. `crate::parser::ast`, `super::foo`, `crate::foo`)
    to the target .rs file.
    """
    stmt = use_stmt.strip().removeprefix("pub ").removeprefix("use ").rstrip(";").strip()
    if "::" not in stmt:
        return None

    parts = [p.strip() for p in stmt.split("::")]
    root_abs = os.path.abspath(root_dir)
    base_crate_dir, mod_parts = _resolve_rust_base_and_parts(parts, from_file, root_abs)
    if not mod_parts:
        return None

    direct_match = _find_rust_module_file(base_crate_dir, mod_parts)
    if direct_match:
        return direct_match

    if len(mod_parts) > 1:
        return _find_rust_module_file(base_crate_dir, mod_parts[:-1])

    return None


def _resolve_dart_relative(import_uri: str, from_dir: str) -> Optional[str]:
    """Resolves relative Dart import URIs."""
    cand = os.path.normpath(os.path.join(from_dir, import_uri))
    if os.path.isfile(cand):
        return cand
    cand_dart = cand + ".dart"
    if os.path.isfile(cand_dart):
        return cand_dart
    return None


def _parse_pubspec_name(content: str) -> str:
    """Parses root package name from pubspec.yaml content."""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("name:"):
            parts = stripped.split(":", 1)
            if len(parts) > 1:
                return parts[1].strip()
    return ""


def _read_pubspec_pkg_name(pubspec_path: str) -> str:
    """Reads and parses the package name from pubspec.yaml."""
    if not os.path.isfile(pubspec_path):
        return ""
    try:
        content = _read_file_text(pubspec_path)
        return _parse_pubspec_name(content)
    except (OSError, UnicodeDecodeError) as exc:
        logger.debug("Failed reading pubspec.yaml at %s: %s", pubspec_path, exc)
        return ""


def _find_existing_file(candidates: List[str]) -> Optional[str]:
    """Returns the first existing file path among candidates."""
    for cand in candidates:
        cand_norm = os.path.normpath(cand)
        if os.path.isfile(cand_norm):
            return cand_norm
    return None


def _resolve_dart_package(clean_uri: str, root_abs: str) -> Optional[str]:
    """Resolves package: URI to candidate file paths in root_dir."""
    parts = clean_uri.split("/", 1)
    if len(parts) < 2:
        return None

    pkg_name, sub_path = parts[0], parts[1]
    root_pkg_name = _read_pubspec_pkg_name(os.path.join(root_abs, "pubspec.yaml"))

    candidates = [os.path.join(root_abs, "lib", sub_path)]
    if not (root_pkg_name and pkg_name == root_pkg_name):
        candidates.append(os.path.join(root_abs, "packages", pkg_name, "lib", sub_path))
        candidates.append(os.path.join(root_abs, pkg_name, "lib", sub_path))

    return _find_existing_file(candidates)


def resolve_dart_import_path(import_uri: str, from_file: str, root_dir: str) -> Optional[str]:
    """
    Resolves a Dart import URI (e.g. 'package:my_app/models/user.dart' or relative '../foo.dart')
    to an existing .dart file path.
    """
    if not import_uri or import_uri.startswith("dart:"):
        return None

    root_abs = os.path.abspath(root_dir)
    from_dir = os.path.dirname(os.path.abspath(from_file))

    if not import_uri.startswith("package:"):
        return _resolve_dart_relative(import_uri, from_dir)

    clean_uri = import_uri[len("package:"):].strip()
    return _resolve_dart_package(clean_uri, root_abs)


JVM_IGNORED_DIRS: Tuple[str, ...] = (".git", "build", ".gradle", "target")
JVM_EXTERNAL_PREFIXES: Tuple[str, ...] = (
    "java.", "javax.", "kotlin.", "android.", "org.junit.", "org.springframework."
)


def _is_jvm_ignored_path(dir_path: str) -> bool:
    """Checks if a directory path matches ignored JVM build/cache directories."""
    return any(d in dir_path for d in JVM_IGNORED_DIRS)


def _resolve_jvm_wildcard(clean: str, root_abs: str) -> Optional[str]:
    """Resolves a wildcard JVM package import (e.g. 'com.example.*')."""
    sub_dir = clean[:-2].replace(".", os.sep)
    for root, _, _ in os.walk(root_abs):
        if _is_jvm_ignored_path(root):
            continue
        if root.endswith(sub_dir):
            return root
    return None


def _find_matching_jvm_file(
    root: str, files: List[str], target_suffixes: Tuple[str, ...]
) -> Optional[str]:
    """Finds a matching JVM source file in files with given suffixes."""
    for f in files:
        if f.endswith((".kt", ".java", ".kts")):
            full_path = os.path.join(root, f)
            if full_path.endswith(target_suffixes):
                return full_path
    return None


def _resolve_jvm_class_file(clean: str, root_abs: str) -> Optional[str]:
    """Resolves a specific JVM class import to an existing .kt, .java, or .kts file."""
    rel_base = clean.replace(".", os.sep)
    target_suffixes = (rel_base + ".kt", rel_base + ".java", rel_base + ".kts")

    for root, _, files in os.walk(root_abs):
        if _is_jvm_ignored_path(root):
            continue
        matched = _find_matching_jvm_file(root, files, target_suffixes)
        if matched:
            return matched
    return None


def resolve_jvm_import_path(import_stmt: str, from_file: str, root_dir: str) -> Optional[str]:
    """
    Resolves a JVM (Java / Kotlin) import statement (e.g. 'com.example.service.UserService')
    to an existing .java, .kt, or .kts file in root_dir.
    """
    clean = import_stmt.strip().removeprefix("import ").removeprefix("static ").rstrip(";").strip()
    if not clean or clean.startswith(JVM_EXTERNAL_PREFIXES):
        return None

    root_abs = os.path.abspath(root_dir)
    if clean.endswith(".*"):
        return _resolve_jvm_wildcard(clean, root_abs)

    return _resolve_jvm_class_file(clean, root_abs)


# ---------------------------------------------------------------------------
# 2. In-Process Circular Import Cycle Detection (Go, Rust, Dart, JVM)
# ---------------------------------------------------------------------------

def _find_cycles(
    deps: Dict[str, Set[str]],
    language: str,
    hazard_prefix: str,
) -> List[Dict[str, Any]]:
    """
    Detects directed dependency cycles using DFS without in-place parameter mutation.
    """
    visited: Set[str] = set()
    rec_stack: Set[str] = set()
    cycles: List[Dict[str, Any]] = []

    def dfs(curr: str, current_path: Tuple[str, ...]) -> None:
        visited.add(curr)
        rec_stack.add(curr)
        next_path = current_path + (curr,)

        for neighbor in sorted(deps.get(curr, set())):
            if neighbor not in visited:
                dfs(neighbor, next_path)
            elif neighbor in rec_stack:
                cycle_start = next_path.index(neighbor)
                cycle_seq = list(next_path[cycle_start:]) + [neighbor]
                cycles.append({
                    "language": language,
                    "cycle_path": cycle_seq,
                    "length": len(cycle_seq) - 1,
                    "hazard": f"{hazard_prefix}: {' -> '.join(cycle_seq)}",
                })

        rec_stack.remove(curr)

    for node in sorted(deps.keys()):
        if node not in visited:
            dfs(node, ())

    return cycles


def _collect_go_files(root_abs: str) -> Dict[str, List[str]]:
    """Collects all non-test Go source files grouped by relative package directory."""
    pkg_files: Dict[str, List[str]] = defaultdict(list)
    ignored = (".git", "vendor", "node_modules")
    for root, _, files in os.walk(root_abs):
        if any(d in root for d in ignored):
            continue
        go_files = [
            os.path.join(root, f) for f in files
            if f.endswith(".go") and not f.endswith("_test.go")
        ]
        if go_files:
            rel_pkg = os.path.relpath(root, root_abs)
            pkg_files[rel_pkg] = go_files
    return pkg_files


def _parse_go_file_imports(file_path: str) -> List[str]:
    """Safely extracts imported module paths from a Go file."""
    try:
        source = _read_file_text(file_path)
        parsed = parse_go_ast(source, filename=file_path)
        return [imp["module"] for imp in parsed.get("imports", []) if "module" in imp]
    except Exception as exc:
        logger.debug("Failed parsing Go AST for %s: %s", file_path, exc)
        return []


def _resolve_go_file_pkg_targets(
    file_path: str, current_pkg: str, root_abs: str
) -> Set[str]:
    """Resolves external Go package dependencies for a single Go file."""
    targets: Set[str] = set()
    for imp in _parse_go_file_imports(file_path):
        resolved = resolve_go_import_path(imp, file_path, root_abs)
        if resolved:
            target_pkg = os.path.relpath(os.path.dirname(resolved), root_abs)
            if target_pkg != current_pkg:
                targets.add(target_pkg)
    return targets


def _build_go_package_deps(
    pkg_files: Dict[str, List[str]], root_abs: str
) -> Dict[str, Set[str]]:
    """Builds package -> package dependency graph for Go codebase."""
    pkg_deps: Dict[str, Set[str]] = defaultdict(set)
    for pkg, flist in pkg_files.items():
        for f in flist:
            pkg_deps[pkg].update(_resolve_go_file_pkg_targets(f, pkg, root_abs))
    return pkg_deps


def detect_go_circular_imports(root_dir: str) -> List[Dict[str, Any]]:
    """
    Detects package-level circular import cycles in Go codebases using DFS on package graphs.
    """
    root_abs = os.path.abspath(root_dir)
    pkg_files = _collect_go_files(root_abs)
    pkg_deps = _build_go_package_deps(pkg_files, root_abs)
    return _find_cycles(pkg_deps, "go", "Go package circular import hazard")


def _collect_rust_files(root_abs: str) -> List[str]:
    """Collects all non-test Rust source files in root_abs."""
    rs_files: List[str] = []
    ignored = (".git", "target")
    for root, _, files in os.walk(root_abs):
        if any(d in root for d in ignored):
            continue
        valid_files = [
            os.path.join(root, f) for f in files
            if f.endswith(".rs") and not f.endswith("_test.rs")
        ]
        rs_files.extend(valid_files)
    return rs_files


def _parse_rust_file_imports(file_path: str) -> List[str]:
    """Safely extracts imported module paths from a Rust file."""
    try:
        source = _read_file_text(file_path)
        parsed = parse_rust_ast(source, filename=file_path)
        return [imp["module"] for imp in parsed.get("imports", []) if "module" in imp]
    except Exception as exc:
        logger.debug("Failed parsing Rust AST for %s: %s", file_path, exc)
        return []


def _resolve_rust_file_deps(full_p: str, rel_p: str, root_abs: str) -> Set[str]:
    """Resolves local Rust file dependencies for a single Rust source file."""
    deps: Set[str] = set()
    for mod in _parse_rust_file_imports(full_p):
        resolved = resolve_rust_import_path(mod, full_p, root_abs)
        if resolved:
            rel_target = os.path.relpath(resolved, root_abs)
            if rel_target != rel_p:
                deps.add(rel_target)
    return deps


def _build_rust_file_deps(root_abs: str) -> Dict[str, Set[str]]:
    """Builds module-level dependency graph for Rust codebase."""
    file_deps: Dict[str, Set[str]] = defaultdict(set)
    for full_p in _collect_rust_files(root_abs):
        rel_p = os.path.relpath(full_p, root_abs)
        deps = _resolve_rust_file_deps(full_p, rel_p, root_abs)
        if deps:
            file_deps[rel_p] = deps
    return file_deps


def detect_rust_circular_imports(root_dir: str) -> List[Dict[str, Any]]:
    """
    Detects module-level circular dependency cycles in Rust codebases using DFS.
    """
    root_abs = os.path.abspath(root_dir)
    file_deps = _build_rust_file_deps(root_abs)
    return _find_cycles(file_deps, "rust", "Rust module circular import hazard")


def _collect_dart_files(root_abs: str) -> List[str]:
    """Collects all non-test Dart source files in root_abs."""
    dart_files: List[str] = []
    ignored = (".git", ".dart_tool", "build")
    for root, _, files in os.walk(root_abs):
        if any(d in root for d in ignored):
            continue
        valid_files = [
            os.path.join(root, f) for f in files
            if f.endswith(".dart") and not f.endswith("_test.dart")
        ]
        dart_files.extend(valid_files)
    return dart_files


def _parse_dart_file_imports(file_path: str) -> List[str]:
    """Safely extracts imported module paths from a Dart file."""
    try:
        source = _read_file_text(file_path)
        parsed = parse_dart_ast(source, filename=file_path)
        return [imp["module"] for imp in parsed.get("imports", []) if "module" in imp]
    except Exception as exc:
        logger.debug("Failed parsing Dart AST for %s: %s", file_path, exc)
        return []


def _resolve_dart_file_deps(full_p: str, rel_p: str, root_abs: str) -> Set[str]:
    """Resolves local Dart file dependencies for a single Dart source file."""
    deps: Set[str] = set()
    for mod in _parse_dart_file_imports(full_p):
        resolved = resolve_dart_import_path(mod, full_p, root_abs)
        if resolved:
            rel_target = os.path.relpath(resolved, root_abs)
            if rel_target != rel_p:
                deps.add(rel_target)
    return deps


def _build_dart_file_deps(root_abs: str) -> Dict[str, Set[str]]:
    """Builds file dependency graph for Dart codebase."""
    file_deps: Dict[str, Set[str]] = defaultdict(set)
    for full_p in _collect_dart_files(root_abs):
        rel_p = os.path.relpath(full_p, root_abs)
        deps = _resolve_dart_file_deps(full_p, rel_p, root_abs)
        if deps:
            file_deps[rel_p] = deps
    return file_deps


def detect_dart_circular_imports(root_dir: str) -> List[Dict[str, Any]]:
    """
    Detects circular import cycles in Dart codebases using DFS.
    """
    root_abs = os.path.abspath(root_dir)
    file_deps = _build_dart_file_deps(root_abs)
    return _find_cycles(file_deps, "dart", "Dart circular import hazard")


def _collect_jvm_files(root_abs: str) -> List[str]:
    """Collects all non-test Java/Kotlin source files in root_abs."""
    jvm_files: List[str] = []
    ignored = (".git", "build", ".gradle", "target")
    for root, _, files in os.walk(root_abs):
        if any(d in root for d in ignored):
            continue
        valid_files = [
            os.path.join(root, f) for f in files
            if f.endswith((".kt", ".java", ".kts")) and not ("Test" in f or "Spec" in f)
        ]
        jvm_files.extend(valid_files)
    return jvm_files


def _parse_jvm_file(full_p: str) -> Optional[Dict[str, Any]]:
    """Safely parses a JVM source file into AST dictionary."""
    try:
        source = _read_file_text(full_p)
        return parse_jvm_ast(source, filename=full_p)
    except Exception as exc:
        logger.debug("Failed parsing JVM AST for %s: %s", full_p, exc)
        return None


def _collect_jvm_ast_map(
    jvm_files: List[str]
) -> Tuple[Set[str], List[Dict[str, Any]]]:
    """Parses JVM files and indexes declared package names."""
    known_pkgs: Set[str] = set()
    parsed_files: List[Dict[str, Any]] = []
    for f in jvm_files:
        parsed = _parse_jvm_file(f)
        if parsed:
            pkg = parsed.get("package") or "default"
            if pkg != "default":
                known_pkgs.add(pkg)
            parsed_files.append(parsed)
    return known_pkgs, parsed_files


def _match_jvm_import_to_package(
    imp_mod: str, src_pkg: str, known_pkgs: Set[str]
) -> Optional[str]:
    """Matches an imported JVM module to a known internal package."""
    for target_pkg in known_pkgs:
        if target_pkg != src_pkg and (imp_mod == target_pkg or imp_mod.startswith(target_pkg + ".")):
            return target_pkg
    return None


def _build_jvm_package_deps(
    parsed_files: List[Dict[str, Any]], known_pkgs: Set[str]
) -> Dict[str, Set[str]]:
    """Builds package-level dependency graph for JVM codebase."""
    pkg_deps: Dict[str, Set[str]] = defaultdict(set)
    for parsed in parsed_files:
        src_pkg = parsed.get("package") or "default"
        for imp in parsed.get("imports", []):
            imp_mod = imp.get("module", "")
            target_pkg = _match_jvm_import_to_package(imp_mod, src_pkg, known_pkgs)
            if target_pkg:
                pkg_deps[src_pkg].add(target_pkg)
    return pkg_deps


def detect_jvm_circular_imports(root_dir: str) -> List[Dict[str, Any]]:
    """
    Detects package-level circular dependencies in Java and Kotlin codebases using DFS.
    """
    root_abs = os.path.abspath(root_dir)
    jvm_files = _collect_jvm_files(root_abs)
    known_pkgs, parsed_files = _collect_jvm_ast_map(jvm_files)
    pkg_deps = _build_jvm_package_deps(parsed_files, known_pkgs)
    return _find_cycles(pkg_deps, "jvm", "JVM package circular dependency hazard")


# ---------------------------------------------------------------------------
# 3. In-Process Lexical Scope Binding Checkers (Zero External Binaries!)
# ---------------------------------------------------------------------------

TS_BUILTIN_GLOBALS: Set[str] = {
    "console", "window", "document", "globalThis", "process", "module", "exports",
    "require", "setTimeout", "clearTimeout", "setInterval", "clearInterval",
    "fetch", "Promise", "Object", "Array", "String", "Number", "Boolean",
    "RegExp", "Map", "Set", "WeakMap", "WeakSet", "Date", "Math", "JSON",
    "Error", "TypeError", "RangeError", "SyntaxError", "parseInt", "parseFloat",
    "isNaN", "isFinite", "encodeURIComponent", "decodeURIComponent", "encodeURI",
    "decodeURI", "undefined", "NaN", "Infinity", "null", "true", "false",
    "this", "arguments", "Event", "HTMLElement", "CustomEvent", "AbortController",
    "IntersectionObserver", "MutationObserver", "Headers", "Request", "Response",
    "URL", "URLSearchParams", "Blob", "File", "FormData", "Node", "Element",
    "Record", "Partial", "Required", "Readonly", "Pick", "Omit", "Exclude", "Extract",
    "NonNullable", "Parameters", "ReturnType", "InstanceType", "Flip", "gsap", "React",
    # JSX intrinsic tags & common props
    "div", "span", "p", "a", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li",
    "button", "input", "form", "label", "table", "tr", "td", "th", "tbody", "thead",
    "img", "svg", "path", "header", "footer", "nav", "section", "main", "article",
    "aside", "canvas", "select", "option", "textarea", "strong", "em", "code", "pre",
    "hr", "br", "i", "b", "className", "style", "onClick", "onChange", "onSubmit",
    "children", "key", "id", "name", "type", "value", "placeholder", "disabled"
}

GO_BUILTIN_UNIVERSE: Set[str] = {
    "int", "int8", "int16", "int32", "int64", "uint", "uint8", "uint16", "uint32",
    "uint64", "uintptr", "float32", "float64", "complex64", "complex128",
    "byte", "rune", "string", "bool", "error", "any", "comparable",
    "true", "false", "iota", "nil",
    "append", "cap", "close", "complex", "copy", "delete", "imag", "len",
    "make", "new", "panic", "print", "println", "real", "recover", "clear", "min", "max"
}

RUST_BUILTIN_PRELUDE: Set[str] = {
    "bool", "char", "i8", "i16", "i32", "i64", "i128", "isize", "u8", "u16",
    "u32", "u64", "u128", "usize", "f32", "f64", "str", "Self", "self",
    "Option", "Some", "None", "Result", "Ok", "Err",
    "Vec", "String", "Box", "Rc", "Arc", "Cell", "RefCell", "Duration",
    "Clone", "Copy", "Debug", "Default", "Eq", "PartialEq", "Ord", "PartialOrd",
    "Hash", "Send", "Sync", "Sized", "Drop", "Fn", "FnMut", "FnOnce", "Iterator",
    "IntoIterator", "From", "Into", "AsRef", "AsMut",
    "println", "print", "eprintln", "eprint", "format", "panic", "assert",
    "assert_eq", "assert_ne", "debug_assert", "vec", "cfg", "env", "todo",
    "unimplemented", "unreachable"
}

DART_BUILTIN_GLOBALS: Set[str] = {
    "int", "double", "num", "String", "bool", "List", "Map", "Set", "Runes", "Symbol",
    "Object", "dynamic", "void", "Never", "Null", "Future", "Stream", "Duration",
    "DateTime", "Uri", "RegExp", "Iterable", "Iterator", "print", "identical",
    "throw", "rethrow", "assert", "main", "super", "this", "true", "false", "null",
    "override", "required", "deprecated", "pragma",
    # Common Flutter primitives
    "Widget", "StatelessWidget", "StatefulWidget", "State", "BuildContext",
    "Container", "Text", "Row", "Column", "Stack", "Expanded", "SizedBox",
    "Scaffold", "AppBar", "Key", "Center", "Padding", "Icon", "IconButton",
    "EdgeInsets", "Color", "Colors", "TextStyle", "Theme", "Navigator"
}

JVM_BUILTIN_GLOBALS: Set[str] = {
    "String", "Integer", "Long", "Double", "Float", "Boolean", "Byte", "Short",
    "Character", "Object", "Class", "System", "Math", "Thread", "Runnable",
    "Exception", "RuntimeException", "Throwable", "Error", "Override", "Deprecated",
    "SuppressWarnings", "List", "Map", "Set", "Collection", "ArrayList", "HashMap",
    "HashSet", "Arrays", "Collections", "Optional", "Stream", "Void",
    # Kotlin intrinsics
    "Int", "Double", "Float", "Long", "Short", "Byte", "Boolean", "Char",
    "Any", "Unit", "Nothing", "println", "print", "readLine", "listOf", "mutableListOf",
    "mapOf", "mutableMapOf", "setOf", "mutableSetOf", "arrayOf", "require",
    "check", "error", "lazy", "run", "let", "apply", "also", "with", "takeIf", "takeUnless",
    "repeat", "Pair", "Triple", "CharSequence", "StringBuilder"
}

TS_KEYWORDS: Set[str] = {
    "if", "else", "for", "while", "do", "switch", "case", "default", "break",
    "continue", "return", "try", "catch", "finally", "throw", "class", "interface",
    "type", "enum", "function", "const", "let", "var", "import", "export",
    "from", "as", "new", "typeof", "instanceof", "in", "of", "void", "delete",
    "async", "await", "yield", "public", "private", "protected", "static", "readonly",
    "extends", "implements", "package", "super", "null", "undefined", "true", "false",
    "this", "any", "string", "number", "boolean", "symbol", "unknown", "never"
}

RUST_STD_NAMESPACES: Set[str] = {"crate", "super", "self", "std", "core", "alloc"}


def _collect_ts_bound_symbols(parsed: Dict[str, Any]) -> Set[str]:
    """Collects bound symbols from TypeScript AST imports and declarations."""
    bound: Set[str] = set(TS_BUILTIN_GLOBALS)
    for imp in parsed.get("imports", []):
        for n in imp.get("names", []):
            bound.add(n)
    for decl in parsed.get("declarations", []):
        parts = decl["name"].split(".")
        bound.add(parts[0])
        if len(parts) > 1:
            bound.add(parts[-1])
    return bound


def _clean_ts_line(stripped: str) -> str:
    """Removes string literals and JSX tag elements to expose raw identifiers."""
    cleaned = re.sub(r'["\'][^"\']*["\']', '""', stripped)
    cleaned = re.sub(r'`[^`]*`', '""', cleaned)
    cleaned = re.sub(r'>[^<]+<', '><', cleaned)
    return re.sub(r'</?[a-zA-Z0-9_$]+', ' ', cleaned)


def _should_skip_ts_line(stripped: str) -> bool:
    """Returns True if line is empty, comment, or import/export specifier."""
    if not stripped or stripped.startswith(("//", "/*", "*")):
        return True
    if stripped.startswith("import "):
        return True
    return stripped.startswith("export ") and " from " in stripped


def _is_ts_inline_declaration(ident: str, stripped: str) -> bool:
    """Detects if an identifier is declared or defined inline on stripped line."""
    decl_pat = rf"\b(?:const|let|var|function|class|interface|type)\s+{ident}\b"
    func_pat = rf"\b(?:public|private|protected|static|async|\s)*{ident}\s*\("
    return bool(re.search(decl_pat, stripped) or re.search(func_pat, stripped))


def _is_ts_parameter(ident: str, stripped: str) -> bool:
    """Detects if an identifier is present within a function parameter list."""
    if "(" in stripped and ")" in stripped:
        param_section = stripped[stripped.find("("):stripped.rfind(")")]
        return ident in param_section
    return False


def _is_known_or_constant_ident(ident: str, bound_symbols: Set[str]) -> bool:
    """Checks if identifier is keyword, builtin, number, or uppercase constant."""
    if ident in TS_KEYWORDS or ident in bound_symbols or ident.isdigit():
        return True
    return ident.isupper() and len(ident) > 1


def _is_property_or_parameter(ident: str, stripped: str) -> bool:
    """Checks if identifier is an object property or in parameter list."""
    if f".{ident}" in stripped or f"{ident}:" in stripped:
        return True
    return _is_ts_parameter(ident, stripped)


def _is_unbound_ts_ident(
    ident: str, stripped: str, bound_symbols: Set[str]
) -> bool:
    """Checks whether a single TypeScript identifier usage is unbound/undeclared."""
    if _is_known_or_constant_ident(ident, bound_symbols):
        return False
    if _is_property_or_parameter(ident, stripped):
        return False
    if _is_ts_inline_declaration(ident, stripped):
        bound_symbols.add(ident)
        return False
    return True


def check_ts_lexical_scope(source_text: str, filename: str = "") -> List[str]:
    """
    Performs fast in-process lexical scope binding check for TypeScript/JavaScript.
    Detects undeclared/hallucinated identifiers and unbound symbols without needing `tsc`.
    """
    parsed = parse_ts_js_ast(source_text, filename=filename)
    bound_symbols = _collect_ts_bound_symbols(parsed)
    ident_pat = re.compile(r"\b([a-zA-Z_$][a-zA-Z0-9_$]*)\b")
    issues: List[str] = []

    for idx, line in enumerate(source_text.splitlines(), 1):
        stripped = line.strip()
        if _should_skip_ts_line(stripped):
            continue
        line_clean = _clean_ts_line(stripped)
        for ident in ident_pat.findall(line_clean):
            if _is_unbound_ts_ident(ident, stripped, bound_symbols):
                issues.append(f"{filename}:{idx} - Potential undeclared identifier '{ident}'")

    return issues[:10]


def _collect_go_scope_symbols(
    parsed: Dict[str, Any]
) -> Tuple[Set[str], Set[str]]:
    """Extracts bound symbols and imported package names from Go AST."""
    bound: Set[str] = set(GO_BUILTIN_UNIVERSE)
    imported: Set[str] = set()
    for imp in parsed.get("imports", []):
        mod = imp["module"].split("/")[-1]
        imported.add(mod)
        bound.add(mod)
    for decl in parsed.get("declarations", []):
        bound.add(decl["name"])
    return bound, imported


def _is_unresolved_go_package(
    pkg_name: str, bound_symbols: Set[str], imported_packages: Set[str]
) -> bool:
    """Checks if a Go selector package qualifier is unimported or undefined."""
    if pkg_name in imported_packages or pkg_name in bound_symbols or pkg_name.isdigit():
        return False
    return pkg_name[0].isupper() and pkg_name not in bound_symbols


def check_go_lexical_scope(source_text: str, filename: str = "") -> List[str]:
    """
    Performs fast in-process lexical scope binding check for Go.
    Detects unimported packages and undeclared identifiers without needing `go vet`.
    """
    parsed = parse_go_ast(source_text, filename=filename)
    bound_symbols, imported_packages = _collect_go_scope_symbols(parsed)
    issues: List[str] = []
    pkg_call_pat = re.compile(r"\b([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)\b")

    for idx, line in enumerate(source_text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        for pkg_name, _ in pkg_call_pat.findall(stripped):
            if _is_unresolved_go_package(pkg_name, bound_symbols, imported_packages):
                issues.append(f"{filename}:{idx} - Referenced unimported package or undefined symbol '{pkg_name}'")

    return issues[:10]


def _collect_rust_bound_symbols(parsed: Dict[str, Any]) -> Set[str]:
    """Extracts bound symbols from Rust AST imports and declarations."""
    bound: Set[str] = set(RUST_BUILTIN_PRELUDE)
    for imp in parsed.get("imports", []):
        name = imp["module"].split("::")[-1].strip().rstrip(";")
        bound.add(name)
    for decl in parsed.get("declarations", []):
        bound.add(decl["name"])
    return bound


def check_rust_lexical_scope(source_text: str, filename: str = "") -> List[str]:
    """
    Performs fast in-process lexical scope binding check for Rust.
    Detects undefined crate references and unbound identifiers without needing `cargo check`.
    """
    parsed = parse_rust_ast(source_text, filename=filename)
    bound_symbols = _collect_rust_bound_symbols(parsed)
    issues: List[str] = []
    crate_call_pat = re.compile(r"\b([A-Za-z0-9_]+)::([A-Za-z0-9_]+)\b")

    for idx, line in enumerate(source_text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "/*", "*")):
            continue
        for mod_name, _ in crate_call_pat.findall(stripped):
            if mod_name not in RUST_STD_NAMESPACES and mod_name not in bound_symbols:
                issues.append(f"{filename}:{idx} - Referenced unresolved crate or module '{mod_name}'")

    return issues[:10]


def _collect_dart_bound_symbols(parsed: Dict[str, Any]) -> Set[str]:
    """Extracts bound symbols from Dart AST imports and declarations."""
    bound: Set[str] = set(DART_BUILTIN_GLOBALS)
    for imp in parsed.get("imports", []):
        if imp.get("alias"):
            bound.add(imp["alias"])
        mod = imp["module"].split("/")[-1].replace(".dart", "")
        bound.add(mod)

    for decl in parsed.get("declarations", []):
        name = decl["name"]
        bound.add(name)
        if "." in name:
            bound.add(name.split(".")[-1])
    return bound


def _should_skip_dart_line(stripped: str) -> bool:
    """Returns True if line is a comment, empty, or directive."""
    if not stripped or stripped.startswith(("//", "/*", "*")):
        return True
    return stripped.startswith(("import ", "export ", "part "))


def check_dart_lexical_scope(source_text: str, filename: str = "") -> List[str]:
    """
    Performs fast in-process lexical scope binding check for Dart.
    Detects unimported packages and undeclared identifiers with zero external binaries.
    """
    parsed = parse_dart_ast(source_text, filename=filename)
    bound_symbols = _collect_dart_bound_symbols(parsed)
    issues: List[str] = []
    selector_pat = re.compile(r"\b([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)\b")

    for idx, line in enumerate(source_text.splitlines(), 1):
        stripped = line.strip()
        if _should_skip_dart_line(stripped):
            continue
        for pfx, _ in selector_pat.findall(stripped):
            if pfx[0].isupper() and pfx not in bound_symbols:
                issues.append(f"{filename}:{idx} - Referenced unresolved prefix or type '{pfx}'")

    return issues[:10]


def _collect_jvm_bound_symbols(parsed: Dict[str, Any]) -> Set[str]:
    """Extracts bound symbols from JVM AST imports and declarations."""
    bound: Set[str] = set(JVM_BUILTIN_GLOBALS)
    for imp in parsed.get("imports", []):
        clean_imp = imp["module"].split(".")[-1].strip().rstrip(";")
        bound.add(clean_imp)

    for decl in parsed.get("declarations", []):
        name = decl["name"]
        bound.add(name)
        if "." in name:
            bound.add(name.split(".")[-1])
    return bound


def _should_skip_jvm_line(stripped: str) -> bool:
    """Returns True if line is a comment, empty, or package/import statement."""
    if not stripped or stripped.startswith(("//", "/*", "*")):
        return True
    return stripped.startswith(("package ", "import "))


def check_jvm_lexical_scope(source_text: str, filename: str = "") -> List[str]:
    """
    Performs fast in-process lexical scope binding check for Java and Kotlin.
    Detects unimported types and unresolved symbols with zero external binaries.
    """
    parsed = parse_jvm_ast(source_text, filename=filename)
    bound_symbols = _collect_jvm_bound_symbols(parsed)
    issues: List[str] = []
    selector_pat = re.compile(r"\b([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)\b")

    for idx, line in enumerate(source_text.splitlines(), 1):
        stripped = line.strip()
        if _should_skip_jvm_line(stripped):
            continue
        for pfx, _ in selector_pat.findall(stripped):
            if pfx[0].isupper() and pfx not in bound_symbols:
                issues.append(f"{filename}:{idx} - Referenced unresolved type or class '{pfx}'")

    return issues[:10]
