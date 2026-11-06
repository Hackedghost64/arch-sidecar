"""
Architectural Cluster Mapping & Change Impact Prediction Engine for Devrith.

Provides:
1. Full repository dependency graph construction (Python, TypeScript, JavaScript).
2. Subsystem cluster discovery (physical and logical partitions).
3. Cluster cohesion, coupling (Ca, Ce), and instability (I) metrics.
4. Pre-change architectural impact prediction (blast radius, cross-cluster reachability, test targeting).
"""

import os
import ast
import re
import json
import time
import logging
from typing import Dict, List, Set, Tuple, Optional, Any, NamedTuple, Union
from collections import defaultdict, deque

from .utils import DEFAULT_IGNORED_DIRS, is_ignored_path, is_minified_or_binary
from .guard import resolve_local_module_path
from .ts_ast import parse_ts_js_ast, resolve_ts_import_path, find_tsconfig
from .go_ast import parse_go_ast
from .rust_ast import parse_rust_ast
from .dart_ast import parse_dart_ast
from .jvm_ast import parse_jvm_ast
from .polyglot_resolver import (
    resolve_go_import_path,
    resolve_rust_import_path,
    resolve_dart_import_path,
    resolve_jvm_import_path,
)

logger = logging.getLogger(__name__)

EXTENSION_TO_LANGUAGE = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "typescript",
    ".jsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".dart": "dart",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
}

POLYGLOT_ENGINES = {
    "go": (parse_go_ast, resolve_go_import_path),
    "rust": (parse_rust_ast, resolve_rust_import_path),
    "dart": (parse_dart_ast, resolve_dart_import_path),
    "kotlin": (parse_jvm_ast, resolve_jvm_import_path),
    "java": (parse_jvm_ast, resolve_jvm_import_path),
    "jvm": (parse_jvm_ast, resolve_jvm_import_path),
}


class DependencyGraph:
    """Directed graph representing file-level and symbol-level module dependencies."""

    def __init__(self, root_dir: str):
        self.root_dir = os.path.abspath(root_dir)
        self.modules: Set[str] = set()
        self.edges: Dict[str, Set[str]] = defaultdict(set)
        self.reverse_edges: Dict[str, Set[str]] = defaultdict(set)
        self.symbols_imported: Dict[Tuple[str, str], List[str]] = defaultdict(list)
        self.module_sizes: Dict[str, int] = {}
        self.module_languages: Dict[str, str] = {}

    def add_module(self, rel_path: str, loc: int, lang: str) -> None:
        self.modules.add(rel_path)
        self.module_sizes[rel_path] = loc
        self.module_languages[rel_path] = lang

    def get_language(self, rel_path: str) -> str:
        return self.module_languages.get(rel_path, "")

    def add_dependency(self, from_mod: str, to_mod: str, symbols: Optional[List[str]] = None) -> None:
        if from_mod == to_mod:
            return
        self.edges[from_mod].add(to_mod)
        self.reverse_edges[to_mod].add(from_mod)
        if symbols:
            self.symbols_imported[(from_mod, to_mod)].extend(symbols)


class _GraphContext(NamedTuple):
    root_abs: str
    file_map: Dict[str, str]
    graph: DependencyGraph


def _read_module_registration(
    full_path: str,
    rel_path: str,
) -> Optional[Tuple[str, int, str]]:
    """Returns (rel_path, loc, language) if the file is a supported source module."""
    _, ext = os.path.splitext(full_path)
    lang = EXTENSION_TO_LANGUAGE.get(ext.lower())
    if not lang or is_minified_or_binary(full_path):
        return None
    try:
        with open(full_path, "r", encoding="utf-8", errors="ignore") as fh:
            lines = fh.readlines()
        return (rel_path, len(lines), lang)
    except (OSError, UnicodeDecodeError) as exc:
        logger.debug("Failed reading module %s: %s", full_path, exc)
        return None


def _extract_file_entries(root: str, files: List[str], root_abs: str) -> List[Tuple[str, str]]:
    entries: List[Tuple[str, str]] = []
    for f in files:
        full_p = os.path.join(root, f)
        rel = os.path.relpath(full_p, root_abs)
        if not is_ignored_path(rel):
            entries.append((full_p, rel))
    return entries


def _scan_directory_files(root_abs: str) -> List[Tuple[str, str]]:
    discovered: List[Tuple[str, str]] = []
    for root, dirs, files in os.walk(root_abs):
        dirs[:] = [
            d for d in dirs
            if not is_ignored_path(os.path.relpath(os.path.join(root, d), root_abs))
        ]
        discovered.extend(_extract_file_entries(root, files, root_abs))
    return discovered


def _discover_and_register_modules(root_abs: str, graph: DependencyGraph) -> Dict[str, str]:
    file_map: Dict[str, str] = {}
    entries = _scan_directory_files(root_abs)
    for full_p, rel_p in entries:
        reg = _read_module_registration(full_p, rel_p)
        if reg is not None:
            rel, loc, lang = reg
            graph.add_module(rel, loc, lang)
            file_map[full_p] = rel
    return file_map


def _is_type_checking(test: ast.AST) -> bool:
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    if isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING":
        return True
    return False


class _RuntimePythonImportVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.runtime_imports: List[Union[ast.Import, ast.ImportFrom]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        _ = self.runtime_imports
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        _ = self.runtime_imports
        return

    def visit_If(self, node: ast.If) -> None:
        _ = self.runtime_imports
        if _is_type_checking(node.test):
            for stmt in node.orelse:
                self.visit(stmt)
            return
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        self.runtime_imports.append(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.runtime_imports.append(node)


def _resolve_relative_import_base(full_p: str, level: int) -> str:
    base_dir = os.path.dirname(full_p)
    for _ in range(max(0, level - 1)):
        base_dir = os.path.dirname(base_dir)
    return base_dir


def _resolve_relative_alias(
    full_p: str,
    level: int,
    base_dir: str,
    alias_name: str,
    ctx: _GraphContext,
) -> Optional[str]:
    cand_sub = os.path.join(base_dir, alias_name + ".py")
    if cand_sub in ctx.file_map:
        return ctx.file_map[cand_sub]
    resolved_abs = resolve_local_module_path(full_p, "." * level, ctx.root_abs)
    if resolved_abs and resolved_abs in ctx.file_map:
        return ctx.file_map[resolved_abs]
    return None


def _handle_relative_dot_import(
    node: ast.ImportFrom,
    full_p: str,
    rel_src: str,
    ctx: _GraphContext,
) -> None:
    level = getattr(node, "level", 0)
    base_dir = _resolve_relative_import_base(full_p, level)
    for alias in node.names:
        target_mod = _resolve_relative_alias(full_p, level, base_dir, alias.name, ctx)
        if target_mod:
            ctx.graph.add_dependency(rel_src, target_mod, [alias.name])


def _handle_standard_import_from(
    node: ast.ImportFrom,
    full_p: str,
    rel_src: str,
    ctx: _GraphContext,
) -> None:
    level = getattr(node, "level", 0)
    module_name = ("." * level) + (node.module or "")
    resolved_abs = resolve_local_module_path(full_p, module_name, ctx.root_abs)
    if resolved_abs and resolved_abs in ctx.file_map:
        syms = [n.name for n in node.names]
        ctx.graph.add_dependency(rel_src, ctx.file_map[resolved_abs], syms)


def _handle_python_import_from(
    node: ast.ImportFrom,
    full_p: str,
    rel_src: str,
    ctx: _GraphContext,
) -> None:
    level = getattr(node, "level", 0)
    if level > 0 and not node.module:
        _handle_relative_dot_import(node, full_p, rel_src, ctx)
        return
    _handle_standard_import_from(node, full_p, rel_src, ctx)


def _handle_python_direct_import(
    node: ast.Import,
    full_p: str,
    rel_src: str,
    ctx: _GraphContext,
) -> None:
    for alias in node.names:
        resolved_abs = resolve_local_module_path(full_p, alias.name, ctx.root_abs)
        if resolved_abs and resolved_abs in ctx.file_map:
            ctx.graph.add_dependency(rel_src, ctx.file_map[resolved_abs], [alias.name])


def _extract_python_dependencies(
    full_p: str,
    rel_src: str,
    ctx: _GraphContext,
) -> None:
    try:
        with open(full_p, "r", encoding="utf-8", errors="ignore") as fh:
            tree = ast.parse(fh.read(), filename=full_p)
    except (SyntaxError, OSError, UnicodeDecodeError) as exc:
        logger.debug("Failed parsing Python AST for %s: %s", full_p, exc)
        return

    visitor = _RuntimePythonImportVisitor()
    visitor.visit(tree)
    for node in visitor.runtime_imports:
        if isinstance(node, ast.Import):
            _handle_python_direct_import(node, full_p, rel_src, ctx)
        elif isinstance(node, ast.ImportFrom):
            _handle_python_import_from(node, full_p, rel_src, ctx)


def _collect_ts_imports(parsed: Dict[str, Any]) -> List[Tuple[str, List[str]]]:
    imports: List[Tuple[str, List[str]]] = []
    for imp in parsed.get("imports", []):
        if not imp.get("is_type_only"):
            imports.append((imp["module"], imp.get("names", [])))
    for re_exp in parsed.get("re_exports", []):
        if not re_exp.get("is_type_only"):
            imports.append((re_exp["module"], [re_exp.get("clause", "*")]))
    return imports


def _resolve_ts_import_edges(
    imports: List[Tuple[str, List[str]]],
    full_p: str,
    rel_src: str,
    tsconfig: Any,
    ctx: _GraphContext,
) -> None:
    for mod_spec, syms in imports:
        resolved = resolve_ts_import_path(mod_spec, full_p, ctx.root_abs, tsconfig)
        if resolved and resolved in ctx.file_map:
            ctx.graph.add_dependency(rel_src, ctx.file_map[resolved], syms)


def _extract_ts_dependencies(
    full_p: str,
    rel_src: str,
    tsconfig: Any,
    ctx: _GraphContext,
) -> None:
    try:
        with open(full_p, "r", encoding="utf-8", errors="ignore") as fh:
            content = fh.read()
        parsed = parse_ts_js_ast(content, filename=full_p)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        logger.debug("Failed parsing TypeScript AST for %s: %s", full_p, exc)
        return
    imports = _collect_ts_imports(parsed)
    _resolve_ts_import_edges(imports, full_p, rel_src, tsconfig, ctx)


def _extract_polyglot_dependencies(
    full_p: str,
    rel_src: str,
    lang: str,
    ctx: _GraphContext,
) -> None:
    engine = POLYGLOT_ENGINES.get(lang)
    if not engine:
        return
    parser_fn, resolver_fn = engine
    try:
        with open(full_p, "r", encoding="utf-8", errors="ignore") as fh:
            content = fh.read()
        parsed = parser_fn(content, filename=full_p)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        logger.debug("Failed parsing %s AST for %s: %s", lang, full_p, exc)
        return

    for imp in parsed.get("imports", []):
        resolved = resolver_fn(imp["module"], full_p, ctx.root_abs)
        if resolved and resolved in ctx.file_map:
            ctx.graph.add_dependency(rel_src, ctx.file_map[resolved], [imp["module"]])


def _extract_file_dependencies(
    full_p: str,
    rel_src: str,
    tsconfig: Any,
    ctx: _GraphContext,
) -> None:
    lang = ctx.graph.get_language(rel_src)
    if lang == "python":
        _extract_python_dependencies(full_p, rel_src, ctx)
    elif lang == "typescript":
        _extract_ts_dependencies(full_p, rel_src, tsconfig, ctx)
    else:
        _extract_polyglot_dependencies(full_p, rel_src, lang, ctx)


def build_repository_dependency_graph(root_dir: str) -> DependencyGraph:
    """
    Builds the complete cross-module dependency graph for a repository.
    Handles Python (`.py`), TypeScript/JavaScript (`.ts`, `.tsx`, `.js`, `.jsx`),
    Go (`.go`), Rust (`.rs`), Dart (`.dart`), and JVM (`.java`, `.kt`, `.kts`).
    """
    root_abs = os.path.abspath(root_dir)
    graph = DependencyGraph(root_abs)
    tsconfig = find_tsconfig(root_abs)
    file_map = _discover_and_register_modules(root_abs, graph)
    ctx = _GraphContext(root_abs=root_abs, file_map=file_map, graph=graph)

    for full_p, rel_src in file_map.items():
        _extract_file_dependencies(full_p, rel_src, tsconfig, ctx)

    return graph


def _determine_cluster_key(mod: str) -> str:
    """Determines the subsystem cluster identifier based on directory hierarchy."""
    d = os.path.dirname(mod).replace("\\", "/")
    if not d or d == ".":
        return "root"
    parts = d.split("/")
    if parts[0] in ("django", "src", "packages", "lib") and len(parts) >= 3:
        return ".".join(parts[:3])
    if len(parts) >= 2:
        return ".".join(parts[:2])
    return parts[0]


def _group_raw_clusters(modules: Set[str]) -> Dict[str, List[str]]:
    raw_clusters: Dict[str, List[str]] = defaultdict(list)
    for mod in modules:
        raw_clusters[_determine_cluster_key(mod)].append(mod)
    return raw_clusters


def _compute_outgoing_edges(
    mods: List[str],
    mod_set: Set[str],
    graph: DependencyGraph,
) -> Tuple[int, int, Set[str]]:
    internal = 0
    external_out = 0
    external_deps: Set[str] = set()
    for m in mods:
        for target in graph.edges.get(m, set()):
            if target in mod_set:
                internal += 1
            else:
                external_out += 1
                external_deps.add(target)
    return internal, external_out, external_deps


def _compute_incoming_callers(
    mods: List[str],
    mod_set: Set[str],
    graph: DependencyGraph,
) -> Set[str]:
    external_callers: Set[str] = set()
    for m in mods:
        for caller in graph.reverse_edges.get(m, set()):
            if caller not in mod_set:
                external_callers.add(caller)
    return external_callers


def _find_top_bridges(mods: List[str], mod_set: Set[str], graph: DependencyGraph) -> List[str]:
    centrality: List[Tuple[str, int]] = []
    for m in mods:
        deg = len(graph.edges.get(m, set()) & mod_set) + len(graph.reverse_edges.get(m, set()) & mod_set)
        centrality.append((m, deg))
    centrality.sort(key=lambda x: x[1], reverse=True)
    return [m for m, deg in centrality[:3] if deg > 0]


def _build_cluster_descriptor(
    c_id: str,
    mods: List[str],
    graph: DependencyGraph,
) -> Dict[str, Any]:
    mod_set = set(mods)
    internal_edges, external_out, external_deps = _compute_outgoing_edges(mods, mod_set, graph)
    external_callers = _compute_incoming_callers(mods, mod_set, graph)

    total_edges = internal_edges + external_out
    cohesion = round(internal_edges / (total_edges + 1e-5), 2)
    ca = len(external_callers)
    ce = len(external_deps)
    instability = round(ce / (ca + ce + 1e-5), 2)
    top_bridges = _find_top_bridges(mods, mod_set, graph)
    total_loc = sum(graph.module_sizes.get(m, 0) for m in mods)

    return {
        "cluster_id": c_id,
        "modules_count": len(mods),
        "total_loc": total_loc,
        "modules": sorted(mods),
        "cohesion_ratio": cohesion,
        "afferent_coupling_ca": ca,
        "efferent_coupling_ce": ce,
        "instability": instability,
        "top_bridge_modules": top_bridges,
    }


def partition_into_subsystems(graph: DependencyGraph) -> Dict[str, Dict[str, Any]]:
    """
    Partitions the dependency graph into logical subsystem clusters based on:
    1. Directory namespace hierarchy (e.g. `django.db.models`, `django.http`).
    2. Internal vs. external dependency density.
    Computes Robert C. Martin's cohesion, coupling (Ca, Ce), and instability (I).
    """
    raw_clusters = _group_raw_clusters(graph.modules)
    clusters: Dict[str, Dict[str, Any]] = {}
    for c_id, mods in raw_clusters.items():
        clusters[c_id] = _build_cluster_descriptor(c_id, mods, graph)
    return clusters


def _resolve_target_module(target: str, root_dir: str, modules: Set[str]) -> Optional[str]:
    """Resolves target file/specifier to a known relative module in the graph."""
    root_abs = os.path.abspath(root_dir)
    target_abs = os.path.abspath(target) if os.path.exists(target) else os.path.abspath(os.path.join(root_abs, target))
    target_rel = os.path.relpath(target_abs, root_abs)
    if target_rel in modules:
        return target_rel
    matched = [m for m in modules if target_rel in m or m.endswith(target)]
    if matched:
        return matched[0]
    return None


def _compute_transitive_reachability(start: str, reverse_edges: Dict[str, Set[str]]) -> List[str]:
    """Computes all modules transitively reaching start via reverse edges (BFS)."""
    reachable: Set[str] = set()
    queue = deque([start])
    visited: Set[str] = {start}

    while queue:
        curr = queue.popleft()
        for caller in reverse_edges.get(curr, set()):
            if caller not in visited:
                visited.add(caller)
                reachable.add(caller)
                queue.append(caller)

    return sorted(list(reachable))


def _identify_impacted_clusters(
    target_rel: str,
    reachable: List[str],
    clusters: Dict[str, Dict[str, Any]],
) -> Tuple[Optional[str], List[str]]:
    target_cluster_id: Optional[str] = None
    touched: Set[str] = set()
    reachable_set = set(reachable)

    for c_id, c_data in clusters.items():
        c_mods = set(c_data["modules"])
        if target_rel in c_mods:
            target_cluster_id = c_id
        if c_mods & reachable_set:
            touched.add(c_id)

    return target_cluster_id, sorted(list(touched))


def _dfs_cycle(
    curr: str,
    path: Tuple[str, ...],
    visited: Set[str],
    edges: Dict[str, Set[str]],
) -> Optional[List[str]]:
    if curr in path:
        idx = path.index(curr)
        return list(path[idx:]) + [curr]
    if curr in visited or len(path) > 10:
        return None
    visited.add(curr)
    new_path = path + (curr,)
    for nxt in edges.get(curr, set()):
        cycle = _dfs_cycle(nxt, new_path, visited, edges)
        if cycle is not None:
            return cycle
    return None


def _detect_cycle(target_rel: str, edges: Dict[str, Set[str]]) -> Tuple[bool, List[str]]:
    cycle_chain = _dfs_cycle(target_rel, (), set(), edges)
    if cycle_chain:
        return True, cycle_chain
    return False, []


def _is_matching_test(mod: str, target_base: str, caller_bases: List[str]) -> bool:
    mod_lower = mod.lower()
    if "test" not in mod_lower:
        return False
    if target_base in mod_lower:
        return True
    return any(cb in mod for cb in caller_bases)


def _recommend_targeted_tests(
    target_rel: str,
    direct_callers: List[str],
    modules: Set[str],
) -> List[str]:
    target_base = os.path.splitext(os.path.basename(target_rel))[0].replace("test_", "")
    caller_bases = [os.path.splitext(os.path.basename(r))[0] for r in direct_callers[:5]]
    recommended: List[str] = []
    for mod in modules:
        if _is_matching_test(mod, target_base, caller_bases):
            recommended.append(mod)
    return recommended


def _evaluate_risk_factors(
    in_cycle: bool,
    cycle_chain: List[str],
    direct_count: int,
    blast_count: int,
    touched: List[str],
) -> List[str]:
    factors: List[str] = []
    if in_cycle:
        factors.append(f"Participates in circular import path ({' -> '.join(cycle_chain[:4])}...)")
    if direct_count >= 10:
        factors.append(f"High direct fan-in: {direct_count} modules directly import this file")
    if blast_count >= 25:
        factors.append(f"Broad blast radius: {blast_count} modules reach this file transitively")
    if len(touched) > 1:
        factors.append(f"Cross-cluster boundary crossing: impacts {len(touched)} distinct subsystems ({', '.join(sorted(touched)[:4])})")
    return factors


def _classify_risk_level(
    in_cycle: bool,
    blast_count: int,
    direct_count: int,
    touched_count: int,
) -> str:
    if in_cycle or blast_count >= 30 or direct_count >= 15:
        return "HIGH_RISK"
    if blast_count >= 8 or direct_count >= 4 or touched_count > 1:
        return "MEDIUM_RISK"
    return "LOW_RISK"


def predict_change_impact(target: str, root_dir: str = ".") -> Dict[str, Any]:
    """
    Change Impact Predictor:
    Given a target file or symbol before modification:
    1. Identifies direct importing modules and call references.
    2. Computes transitive downstream reachability (blast radius).
    3. Identifies crossed subsystem cluster boundaries.
    4. Evaluates cycle participation risk.
    5. Recommends targeted test suites.
    6. Classifies architectural risk (LOW, MEDIUM, HIGH).
    """
    t0 = time.perf_counter()
    graph = build_repository_dependency_graph(root_dir)
    target_rel = _resolve_target_module(target, root_dir, graph.modules)
    if not target_rel:
        return {
            "success": False,
            "error": f"Target '{target}' not found in dependency graph.",
            "duration_ms": round((time.perf_counter() - t0) * 1000, 2),
        }

    clusters = partition_into_subsystems(graph)
    direct_callers = sorted(list(graph.reverse_edges.get(target_rel, set())))
    reachable = _compute_transitive_reachability(target_rel, graph.reverse_edges)
    target_cluster_id, touched = _identify_impacted_clusters(target_rel, reachable, clusters)
    in_cycle, cycle_chain = _detect_cycle(target_rel, graph.edges)
    recommended_tests = _recommend_targeted_tests(target_rel, direct_callers, graph.modules)

    direct_count = len(direct_callers)
    blast_count = len(reachable)
    risk_factors = _evaluate_risk_factors(in_cycle, cycle_chain, direct_count, blast_count, touched)
    risk_level = _classify_risk_level(in_cycle, blast_count, direct_count, len(touched))

    return {
        "success": True,
        "target": target_rel,
        "language": graph.module_languages.get(target_rel, "unknown"),
        "lines_of_code": graph.module_sizes.get(target_rel, 0),
        "cluster": target_cluster_id,
        "risk_level": risk_level,
        "risk_factors": risk_factors,
        "direct_callers_count": direct_count,
        "direct_callers": direct_callers[:15],
        "transitive_blast_radius_count": blast_count,
        "transitive_blast_radius": reachable[:25],
        "touched_clusters_count": len(touched),
        "touched_clusters": touched,
        "in_circular_cycle": in_cycle,
        "cycle_path": cycle_chain,
        "recommended_tests": recommended_tests[:10],
        "duration_ms": round((time.perf_counter() - t0) * 1000, 2),
    }


def format_cluster_map_terminal(clusters: Dict[str, Dict[str, Any]], repo_name: str) -> str:
    """Formats a clean, human-readable terminal output for repository cluster maps."""
    lines: List[str] = [
        f"🗺️  Architectural Subsystem Cluster Map: {repo_name}",
        f"Total Discovered Subsystems: {len(clusters)}\n",
        f"{'Cluster Subsystem':<32} {'Files':<8} {'Cohesion':<10} {'Instability (I)':<16} {'Top Bridges / Facades'}",
        "-" * 95,
    ]

    sorted_clusters = sorted(clusters.values(), key=lambda c: c["modules_count"], reverse=True)
    for c in sorted_clusters:
        c_id = c["cluster_id"]
        count = c["modules_count"]
        cohesion = f"{c['cohesion_ratio']:.2f}"
        i_metric = f"{c['instability']:.2f}"
        bridges = ", ".join(os.path.basename(b) for b in c["top_bridge_modules"][:2]) or "-"
        lines.append(f"{c_id:<32} {count:<8} {cohesion:<10} {i_metric:<16} {bridges}")

    return "\n".join(lines)


def _format_risk_badge(risk: str) -> str:
    color_map = {
        "HIGH_RISK": "🔴",
        "MEDIUM_RISK": "🟡",
    }
    symbol = color_map.get(risk, "🟢")
    return f"Risk Classification: {symbol} {risk}"


def _format_direct_callers(direct_callers: List[str], total_count: int) -> List[str]:
    lines: List[str] = [f"  • Direct Callers ({total_count}):"]
    for dc in direct_callers[:6]:
        lines.append(f"     ↳ {dc}")
    if total_count > 6:
        lines.append(f"     ↳ ... ({total_count - 6} more direct callers)")
    return lines


def _format_recommended_tests(tests: List[str]) -> List[str]:
    if not tests:
        return []
    lines: List[str] = ["\nRecommended Targeted Tests:"]
    for t in tests[:5]:
        lines.append(f"  🧪 {t}")
    return lines


def format_change_impact_terminal(impact: Dict[str, Any]) -> str:
    """Formats a structured, actionable Change Impact Report for terminal & agents."""
    lines: List[str] = [
        f"🧠 Change Impact Analysis: {impact['target']}",
        f"Subsystem Cluster: {impact['cluster']} | Size: {impact['lines_of_code']} LOC | Language: {impact['language']}",
        _format_risk_badge(impact["risk_level"]),
    ]

    if impact.get("risk_factors"):
        lines.append("\nDetected Risk Factors:")
        for rf in impact["risk_factors"]:
            lines.append(f"  ⚠️  {rf}")

    lines.append("\nBlast Radius & Reachability:")
    lines.extend(_format_direct_callers(impact["direct_callers"], impact["direct_callers_count"]))
    lines.append(f"  • Transitive Reachability ({impact['transitive_blast_radius_count']} modules total)")
    lines.append(f"  • Crossed Subsystems ({impact['touched_clusters_count']}): {', '.join(impact['touched_clusters'])}")

    lines.extend(_format_recommended_tests(impact.get("recommended_tests", [])))
    lines.append(f"\nExecution Duration: {impact['duration_ms']}ms")
    return "\n".join(lines)
