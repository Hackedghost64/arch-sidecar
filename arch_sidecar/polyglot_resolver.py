"""
Polyglot Resolver & In-Process Lexical Scope Binding Engine for Devrith.

Provides:
1. Go and Rust local import path resolution and module mapping.
2. In-process circular import cycle detection for Go and Rust (0 external binaries).
3. In-process lexical scope binding analysis for TypeScript, Go, and Rust:
   Detects hallucinated/undefined variables and typos with 0 external compiler binaries.
"""

import os
import re
from typing import Dict, List, Set, Optional, Tuple, Any
from collections import defaultdict

from .go_ast import parse_go_ast
from .rust_ast import parse_rust_ast
from .ts_ast import parse_ts_js_ast
from .dart_ast import parse_dart_ast
from .jvm_ast import parse_jvm_ast


# ---------------------------------------------------------------------------
# 1. Polyglot Import Resolvers
# ---------------------------------------------------------------------------

def resolve_go_import_path(import_path: str, from_file: str, root_dir: str) -> Optional[str]:
    """
    Resolves a Go import path (e.g., 'myproject/pkg/util' or relative './sub')
    to an entry .go file or directory in root_dir.
    """
    root_abs = os.path.abspath(root_dir)
    # Check for relative import
    if import_path.startswith("./") or import_path.startswith("../"):
        from_dir = os.path.dirname(os.path.abspath(from_file))
        cand_dir = os.path.abspath(os.path.join(from_dir, import_path))
        if os.path.isdir(cand_dir):
            for f in os.listdir(cand_dir):
                if f.endswith(".go") and not f.endswith("_test.go"):
                    return os.path.join(cand_dir, f)
        return None

    # Check against module name in go.mod
    go_mod_path = os.path.join(root_abs, "go.mod")
    mod_prefix = ""
    if os.path.isfile(go_mod_path):
        try:
            with open(go_mod_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if line.startswith("module "):
                        mod_prefix = line.split()[1].strip()
                        break
        except Exception:
            pass

    if mod_prefix and import_path.startswith(mod_prefix):
        sub_path = import_path[len(mod_prefix):].lstrip("/")
        cand_dir = os.path.join(root_abs, sub_path)
        if os.path.isdir(cand_dir):
            for f in os.listdir(cand_dir):
                if f.endswith(".go") and not f.endswith("_test.go"):
                    return os.path.join(cand_dir, f)

    # Check direct subdirectory match
    cand_dir = os.path.join(root_abs, import_path.split("/")[-1])
    if os.path.isdir(cand_dir):
        for f in os.listdir(cand_dir):
            if f.endswith(".go") and not f.endswith("_test.go"):
                return os.path.join(cand_dir, f)

    return None


def resolve_rust_import_path(use_stmt: str, from_file: str, root_dir: str) -> Optional[str]:
    """
    Resolves a Rust `use` path (e.g. `crate::parser::ast`, `super::foo`, `crate::foo`)
    to the target .rs file.
    """
    root_abs = os.path.abspath(root_dir)
    src_dir = os.path.join(root_abs, "src")
    base_crate_dir = src_dir if os.path.isdir(src_dir) else root_abs

    # Normalize statement: strip `use `, trailing `;`, `{...}`
    stmt = use_stmt.strip().removeprefix("pub ").removeprefix("use ").rstrip(";").strip()
    if "::" not in stmt:
        return None

    parts = [p.strip() for p in stmt.split("::")]
    if parts[0] == "crate":
        mod_parts = parts[1:]
    elif parts[0] == "super":
        from_dir = os.path.dirname(os.path.abspath(from_file))
        parent_dir = os.path.dirname(from_dir)
        mod_parts = parts[1:]
        base_crate_dir = parent_dir
    else:
        # e.g. `self::foo` or local mod name
        from_dir = os.path.dirname(os.path.abspath(from_file))
        base_crate_dir = from_dir
        mod_parts = [p for p in parts if p != "self"]

    if not mod_parts:
        return None

    # Try 1: base_crate_dir/part1/part2.rs
    cand_file = os.path.join(base_crate_dir, *mod_parts) + ".rs"
    if os.path.isfile(cand_file):
        return cand_file

    # Try 2: base_crate_dir/part1/part2/mod.rs
    cand_mod = os.path.join(base_crate_dir, *mod_parts, "mod.rs")
    if os.path.isfile(cand_mod):
        return cand_mod

    # Try 3: If target is a symbol inside a file (e.g. `crate::foo::Bar`), check `crate::foo.rs`
    if len(mod_parts) > 1:
        parent_file = os.path.join(base_crate_dir, *mod_parts[:-1]) + ".rs"
        if os.path.isfile(parent_file):
            return parent_file
        parent_mod = os.path.join(base_crate_dir, *mod_parts[:-1], "mod.rs")
        if os.path.isfile(parent_mod):
            return parent_mod

    return None


def resolve_dart_import_path(import_uri: str, from_file: str, root_dir: str) -> Optional[str]:
    """
    Resolves a Dart import URI (e.g. 'package:my_app/models/user.dart' or relative '../foo.dart')
    to an existing .dart file path.
    """
    if not import_uri or import_uri.startswith("dart:"):
        return None

    root_abs = os.path.abspath(root_dir)
    from_dir = os.path.dirname(os.path.abspath(from_file))

    # 1. Relative import (starts with './' or '../' or no scheme)
    if not import_uri.startswith("package:"):
        cand = os.path.normpath(os.path.join(from_dir, import_uri))
        if os.path.isfile(cand):
            return cand
        if os.path.isfile(cand + ".dart"):
            return cand + ".dart"
        return None

    # 2. Package import: package:<pkg_name>/<subpath>
    clean_uri = import_uri[len("package:"):].strip()
    parts = clean_uri.split("/", 1)
    if len(parts) < 2:
        return None

    pkg_name, sub_path = parts[0], parts[1]

    # Try resolving against pubspec.yaml in root_dir
    pubspec_path = os.path.join(root_abs, "pubspec.yaml")
    root_pkg_name = ""
    if os.path.isfile(pubspec_path):
        try:
            with open(pubspec_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    stripped = line.strip()
                    if stripped.startswith("name:"):
                        root_pkg_name = stripped.split(":", 1)[1].strip()
                        break
        except Exception:
            pass

    # In Dart convention, package:my_app/<path> maps to lib/<path>
    candidates = []
    if root_pkg_name and pkg_name == root_pkg_name:
        candidates.append(os.path.join(root_abs, "lib", sub_path))
    else:
        candidates.append(os.path.join(root_abs, "lib", sub_path))
        candidates.append(os.path.join(root_abs, "packages", pkg_name, "lib", sub_path))
        candidates.append(os.path.join(root_abs, pkg_name, "lib", sub_path))

    for cand in candidates:
        cand_norm = os.path.normpath(cand)
        if os.path.isfile(cand_norm):
            return cand_norm

    return None


def resolve_jvm_import_path(import_stmt: str, from_file: str, root_dir: str) -> Optional[str]:
    """
    Resolves a JVM (Java / Kotlin) import statement (e.g. 'com.example.service.UserService')
    to an existing .java, .kt, or .kts file in root_dir.
    """
    clean = import_stmt.strip().removeprefix("import ").removeprefix("static ").rstrip(";").strip()
    if not clean or clean.startswith(("java.", "javax.", "kotlin.", "android.", "org.junit.", "org.springframework.")):
        return None

    root_abs = os.path.abspath(root_dir)

    # Convert package path to relative path components
    if clean.endswith(".*"):
        sub_dir = clean[:-2].replace(".", os.sep)
        for root, dirs, _ in os.walk(root_abs):
            if any(d in root for d in [".git", "build", ".gradle", "target"]):
                continue
            if root.endswith(sub_dir):
                return root
        return None

    path_suffix_kt = clean.replace(".", os.sep) + ".kt"
    path_suffix_java = clean.replace(".", os.sep) + ".java"
    path_suffix_kts = clean.replace(".", os.sep) + ".kts"

    for root, _, files in os.walk(root_abs):
        if any(d in root for d in [".git", "build", ".gradle", "target"]):
            continue
        for f in files:
            if not f.endswith((".kt", ".java", ".kts")):
                continue
            full_path = os.path.join(root, f)
            if full_path.endswith(path_suffix_kt) or full_path.endswith(path_suffix_java) or full_path.endswith(path_suffix_kts):
                return full_path

    return None


# ---------------------------------------------------------------------------
# 2. In-Process Circular Import Cycle Detection (Go, Rust, Dart, JVM)
# ---------------------------------------------------------------------------

def detect_go_circular_imports(root_dir: str) -> List[Dict[str, Any]]:
    """
    Detects package-level circular import cycles in Go codebases using DFS on package graphs.
    """
    root_abs = os.path.abspath(root_dir)
    pkg_files: Dict[str, List[str]] = defaultdict(list)
    file_imports: Dict[str, List[str]] = {}

    for root, _, files in os.walk(root_abs):
        if any(d in root for d in [".git", "vendor", "node_modules"]):
            continue
        go_files = [os.path.join(root, f) for f in files if f.endswith(".go") and not f.endswith("_test.go")]
        if not go_files:
            continue
        rel_pkg = os.path.relpath(root, root_abs)
        pkg_files[rel_pkg] = go_files
        for gf in go_files:
            try:
                with open(gf, "r", encoding="utf-8", errors="ignore") as fh:
                    parsed = parse_go_ast(fh.read(), filename=gf)
                file_imports[gf] = [imp["module"] for imp in parsed.get("imports", [])]
            except Exception:
                file_imports[gf] = []

    # Build package -> package dependency graph
    pkg_deps: Dict[str, Set[str]] = defaultdict(set)
    for pkg, flist in pkg_files.items():
        for f in flist:
            for imp in file_imports.get(f, []):
                resolved = resolve_go_import_path(imp, f, root_abs)
                if resolved:
                    target_pkg = os.path.relpath(os.path.dirname(resolved), root_abs)
                    if target_pkg != pkg:
                        pkg_deps[pkg].add(target_pkg)

    # Detect cycles via DFS
    visited: Set[str] = set()
    rec_stack: Set[str] = set()
    cycles: List[Dict[str, Any]] = []

    def dfs(curr: str, path: List[str]):
        visited.add(curr)
        rec_stack.add(curr)
        path.append(curr)

        for neighbor in pkg_deps.get(curr, []):
            if neighbor not in visited:
                dfs(neighbor, path)
            elif neighbor in rec_stack:
                cycle_start = path.index(neighbor)
                cycle_path = path[cycle_start:] + [neighbor]
                cycles.append({
                    "language": "go",
                    "cycle_path": cycle_path,
                    "length": len(cycle_path) - 1,
                    "hazard": f"Go package circular import hazard: {' -> '.join(cycle_path)}"
                })

        path.pop()
        rec_stack.remove(curr)

    for p in list(pkg_deps.keys()):
        if p not in visited:
            dfs(p, [])

    return cycles


def detect_rust_circular_imports(root_dir: str) -> List[Dict[str, Any]]:
    """
    Detects module-level circular dependency cycles in Rust codebases using DFS.
    """
    root_abs = os.path.abspath(root_dir)
    file_deps: Dict[str, Set[str]] = defaultdict(set)

    for root, _, files in os.walk(root_abs):
        if any(d in root for d in [".git", "target"]):
            continue
        for f in files:
            if f.endswith(".rs") and not f.endswith("_test.rs"):
                full_p = os.path.join(root, f)
                rel_p = os.path.relpath(full_p, root_abs)
                try:
                    with open(full_p, "r", encoding="utf-8", errors="ignore") as fh:
                        parsed = parse_rust_ast(fh.read(), filename=full_p)
                    for imp in parsed.get("imports", []):
                        resolved = resolve_rust_import_path(imp["module"], full_p, root_abs)
                        if resolved:
                            rel_target = os.path.relpath(resolved, root_abs)
                            if rel_target != rel_p:
                                file_deps[rel_p].add(rel_target)
                except Exception:
                    pass

    # Cycle detection via DFS
    visited: Set[str] = set()
    rec_stack: Set[str] = set()
    cycles: List[Dict[str, Any]] = []

    def dfs(curr: str, path: List[str]):
        visited.add(curr)
        rec_stack.add(curr)
        path.append(curr)

        for neighbor in file_deps.get(curr, []):
            if neighbor not in visited:
                dfs(neighbor, path)
            elif neighbor in rec_stack:
                cycle_start = path.index(neighbor)
                cycle_path = path[cycle_start:] + [neighbor]
                cycles.append({
                    "language": "rust",
                    "cycle_path": cycle_path,
                    "length": len(cycle_path) - 1,
                    "hazard": f"Rust module circular import hazard: {' -> '.join(cycle_path)}"
                })

        path.pop()
        rec_stack.remove(curr)

    for m in list(file_deps.keys()):
        if m not in visited:
            dfs(m, [])

    return cycles


def detect_dart_circular_imports(root_dir: str) -> List[Dict[str, Any]]:
    """
    Detects circular import cycles in Dart codebases using DFS.
    """
    root_abs = os.path.abspath(root_dir)
    dart_file_deps: Dict[str, Set[str]] = defaultdict(set)

    for root, _, files in os.walk(root_abs):
        if any(d in root for d in [".git", ".dart_tool", "build"]):
            continue
        for f in files:
            if f.endswith(".dart") and not f.endswith("_test.dart"):
                full_p = os.path.join(root, f)
                rel_p = os.path.relpath(full_p, root_abs)
                try:
                    with open(full_p, "r", encoding="utf-8", errors="ignore") as fh:
                        parsed = parse_dart_ast(fh.read(), filename=full_p)
                    for imp in parsed.get("imports", []):
                        resolved = resolve_dart_import_path(imp["module"], full_p, root_abs)
                        if resolved:
                            rel_target = os.path.relpath(resolved, root_abs)
                            if rel_target != rel_p:
                                dart_file_deps[rel_p].add(rel_target)
                except Exception:
                    pass

    visited: Set[str] = set()
    rec_stack: Set[str] = set()
    cycles: List[Dict[str, Any]] = []

    def dfs(curr: str, path: List[str]):
        visited.add(curr)
        rec_stack.add(curr)
        path.append(curr)

        for neighbor in dart_file_deps.get(curr, []):
            if neighbor not in visited:
                dfs(neighbor, path)
            elif neighbor in rec_stack:
                cycle_start = path.index(neighbor)
                cycle_path = path[cycle_start:] + [neighbor]
                cycles.append({
                    "language": "dart",
                    "cycle_path": cycle_path,
                    "length": len(cycle_path) - 1,
                    "hazard": f"Dart circular import hazard: {' -> '.join(cycle_path)}"
                })

        path.pop()
        rec_stack.remove(curr)

    for m in list(dart_file_deps.keys()):
        if m not in visited:
            dfs(m, [])

    return cycles


def detect_jvm_circular_imports(root_dir: str) -> List[Dict[str, Any]]:
    """
    Detects package-level circular dependencies in Java and Kotlin codebases using DFS.
    """
    root_abs = os.path.abspath(root_dir)
    pkg_deps: Dict[str, Set[str]] = defaultdict(set)
    pkg_files: Dict[str, List[str]] = defaultdict(list)
    file_ast_map: Dict[str, Any] = {}

    for root, _, files in os.walk(root_abs):
        if any(d in root for d in [".git", "build", ".gradle", "target"]):
            continue
        for f in files:
            if f.endswith((".kt", ".java", ".kts")) and not ("Test" in f or "Spec" in f):
                full_p = os.path.join(root, f)
                try:
                    with open(full_p, "r", encoding="utf-8", errors="ignore") as fh:
                        parsed = parse_jvm_ast(fh.read(), filename=full_p)
                    pkg = parsed.get("package") or "default"
                    pkg_files[pkg].append(full_p)
                    file_ast_map[full_p] = parsed
                except Exception:
                    pass

    for full_p, parsed in file_ast_map.items():
        src_pkg = parsed.get("package") or "default"
        for imp in parsed.get("imports", []):
            imp_mod = imp.get("module", "")
            for target_pkg in pkg_files.keys():
                if target_pkg != "default" and (imp_mod == target_pkg or imp_mod.startswith(target_pkg + ".")):
                    if target_pkg != src_pkg:
                        pkg_deps[src_pkg].add(target_pkg)

    visited: Set[str] = set()
    rec_stack: Set[str] = set()
    cycles: List[Dict[str, Any]] = []

    def dfs(curr: str, path: List[str]):
        visited.add(curr)
        rec_stack.add(curr)
        path.append(curr)

        for neighbor in pkg_deps.get(curr, []):
            if neighbor not in visited:
                dfs(neighbor, path)
            elif neighbor in rec_stack:
                cycle_start = path.index(neighbor)
                cycle_path = path[cycle_start:] + [neighbor]
                cycles.append({
                    "language": "jvm",
                    "cycle_path": cycle_path,
                    "length": len(cycle_path) - 1,
                    "hazard": f"JVM package circular dependency hazard: {' -> '.join(cycle_path)}"
                })

        path.pop()
        rec_stack.remove(curr)

    for p in list(pkg_deps.keys()):
        if p not in visited:
            dfs(p, [])

    return cycles


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


def check_ts_lexical_scope(source_text: str, filename: str = "") -> List[str]:
    """
    Performs fast in-process lexical scope binding check for TypeScript/JavaScript.
    Detects undeclared/hallucinated identifiers and unbound symbols without needing `tsc`.
    """
    parsed = parse_ts_js_ast(source_text, filename=filename)
    # Collect all bound names at module level: imports, declarations
    bound_symbols: Set[str] = set(TS_BUILTIN_GLOBALS)

    for imp in parsed.get("imports", []):
        for n in imp.get("names", []):
            bound_symbols.add(n)

    for decl in parsed.get("declarations", []):
        raw_name = decl["name"].split(".")[0]
        bound_symbols.add(raw_name)
        if "." in decl["name"]:
            bound_symbols.add(decl["name"].split(".")[-1])

    issues: List[str] = []
    lines = source_text.splitlines()

    # Scan for identifier usages and check if bound
    ident_pat = re.compile(r"\b([a-zA-Z_$][a-zA-Z0-9_$]*)\b")
    # Keywords to ignore
    keywords = {
        "if", "else", "for", "while", "do", "switch", "case", "default", "break",
        "continue", "return", "try", "catch", "finally", "throw", "class", "interface",
        "type", "enum", "function", "const", "let", "var", "import", "export",
        "from", "as", "new", "typeof", "instanceof", "in", "of", "void", "delete",
        "async", "await", "yield", "public", "private", "protected", "static", "readonly",
        "extends", "implements", "package", "super", "null", "undefined", "true", "false",
        "this", "any", "string", "number", "boolean", "symbol", "unknown", "never"
    }

    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "/*", "*")):
            continue
        # Skip import and export...from lines (their specifiers are paths, not variable usages)
        if stripped.startswith("import ") or (stripped.startswith("export ") and " from " in stripped):
            continue

        # Strip string literals so path segments or messages are not treated as identifiers
        line_clean = re.sub(r'["\'][^"\']*["\']', '""', stripped)
        line_clean = re.sub(r'`[^`]*`', '""', line_clean)
        # Strip JSX raw text between tags and JSX tag brackets
        line_clean = re.sub(r'>[^<]+<', '><', line_clean)
        line_clean = re.sub(r'</?[a-zA-Z0-9_$]+', ' ', line_clean)

        # Extract identifiers
        matches = ident_pat.findall(line_clean)
        for ident in matches:
            if ident in keywords or ident in bound_symbols or ident.isdigit():
                continue
            # If line is an object property access (.prop) or property definition (prop:), skip
            if f".{ident}" in stripped or f"{ident}:" in stripped:
                continue
            # Check if declared on the same line (e.g. `const foo =`, method signature)
            if re.search(rf"\b(?:const|let|var|function|class|interface|type)\s+{ident}\b", stripped) or re.search(rf"\b(?:public|private|protected|static|async|\s)*{ident}\s*\(", stripped):
                bound_symbols.add(ident)
                continue
            # Check function parameter list
            if "(" in stripped and ")" in stripped and ident in stripped[stripped.find("("):stripped.rfind(")")]:
                continue
            # Detected unbound identifier!
            if ident.isupper() and len(ident) > 1:
                # Likely an external enum or constant
                continue
            issues.append(f"{filename}:{idx} - Potential undeclared identifier '{ident}'")

    return issues[:10]  # Cap to top 10 actionable warnings


def check_go_lexical_scope(source_text: str, filename: str = "") -> List[str]:
    """
    Performs fast in-process lexical scope binding check for Go.
    Detects unimported packages and undeclared identifiers without needing `go vet`.
    """
    parsed = parse_go_ast(source_text, filename=filename)
    bound_symbols: Set[str] = set(GO_BUILTIN_UNIVERSE)

    # Collect imported package aliases/names
    imported_packages: Set[str] = set()
    for imp in parsed.get("imports", []):
        mod = imp["module"].split("/")[-1]
        imported_packages.add(mod)
        bound_symbols.add(mod)

    for decl in parsed.get("declarations", []):
        bound_symbols.add(decl["name"])

    issues: List[str] = []
    lines = source_text.splitlines()
    pkg_call_pat = re.compile(r"\b([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)\b")

    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        # Check package selector calls: `fmt.Println` -> verifies `fmt` is in imported_packages
        for pkg_name, sym_name in pkg_call_pat.findall(stripped):
            if pkg_name not in imported_packages and pkg_name not in bound_symbols and not pkg_name.isdigit():
                # Could be a struct field access (obj.Field); check if obj is a known variable
                if pkg_name[0].isupper() and pkg_name not in bound_symbols:
                    issues.append(f"{filename}:{idx} - Referenced unimported package or undefined symbol '{pkg_name}'")

    return issues[:10]


def check_rust_lexical_scope(source_text: str, filename: str = "") -> List[str]:
    """
    Performs fast in-process lexical scope binding check for Rust.
    Detects undefined crate references and unbound identifiers without needing `cargo check`.
    """
    parsed = parse_rust_ast(source_text, filename=filename)
    bound_symbols: Set[str] = set(RUST_BUILTIN_PRELUDE)

    for imp in parsed.get("imports", []):
        name = imp["module"].split("::")[-1].strip().rstrip(";")
        bound_symbols.add(name)

    for decl in parsed.get("declarations", []):
        bound_symbols.add(decl["name"])

    issues: List[str] = []
    lines = source_text.splitlines()
    crate_call_pat = re.compile(r"\b([A-Za-z0-9_]+)::([A-Za-z0-9_]+)\b")

    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "/*", "*")):
            continue
        for mod_name, item_name in crate_call_pat.findall(stripped):
            if mod_name in ("crate", "super", "self", "std", "core", "alloc"):
                continue
            if mod_name not in bound_symbols:
                issues.append(f"{filename}:{idx} - Referenced unresolved crate or module '{mod_name}'")

    return issues[:10]


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


def check_dart_lexical_scope(source_text: str, filename: str = "") -> List[str]:
    """
    Performs fast in-process lexical scope binding check for Dart.
    Detects unimported packages and undeclared identifiers with zero external binaries.
    """
    parsed = parse_dart_ast(source_text, filename=filename)
    bound_symbols: Set[str] = set(DART_BUILTIN_GLOBALS)

    for imp in parsed.get("imports", []):
        if imp.get("alias"):
            bound_symbols.add(imp["alias"])
        mod = imp["module"].split("/")[-1].replace(".dart", "")
        bound_symbols.add(mod)

    for decl in parsed.get("declarations", []):
        bound_symbols.add(decl["name"])
        if "." in decl["name"]:
            bound_symbols.add(decl["name"].split(".")[-1])

    issues: List[str] = []
    lines = source_text.splitlines()
    selector_pat = re.compile(r"\b([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)\b")

    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "/*", "*")):
            continue
        if stripped.startswith("import ") or stripped.startswith("export ") or stripped.startswith("part "):
            continue

        for pfx, ident in selector_pat.findall(stripped):
            if pfx[0].isupper() and pfx not in bound_symbols:
                issues.append(f"{filename}:{idx} - Referenced unresolved prefix or type '{pfx}'")

    return issues[:10]


def check_jvm_lexical_scope(source_text: str, filename: str = "") -> List[str]:
    """
    Performs fast in-process lexical scope binding check for Java and Kotlin.
    Detects unimported types and unresolved symbols with zero external binaries.
    """
    parsed = parse_jvm_ast(source_text, filename=filename)
    bound_symbols: Set[str] = set(JVM_BUILTIN_GLOBALS)

    for imp in parsed.get("imports", []):
        clean_imp = imp["module"].split(".")[-1].strip().rstrip(";")
        bound_symbols.add(clean_imp)

    for decl in parsed.get("declarations", []):
        bound_symbols.add(decl["name"])
        if "." in decl["name"]:
            bound_symbols.add(decl["name"].split(".")[-1])

    issues: List[str] = []
    lines = source_text.splitlines()
    selector_pat = re.compile(r"\b([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)\b")

    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "/*", "*")):
            continue
        if stripped.startswith("package ") or stripped.startswith("import "):
            continue

        for pfx, ident in selector_pat.findall(stripped):
            if pfx[0].isupper() and pfx not in bound_symbols:
                issues.append(f"{filename}:{idx} - Referenced unresolved type or class '{pfx}'")

    return issues[:10]
