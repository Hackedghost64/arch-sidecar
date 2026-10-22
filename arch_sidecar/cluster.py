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
from typing import Dict, List, Set, Tuple, Optional, Any
from collections import defaultdict, deque

from .utils import DEFAULT_IGNORED_DIRS, is_ignored_path, is_minified_or_binary
from .guard import resolve_local_module_path
from .ts_ast import parse_ts_js_ast, resolve_ts_import_path, find_tsconfig


class DependencyGraph:
    """Directed graph representing file-level and symbol-level module dependencies."""
    def __init__(self, root_dir: str):
        self.root_dir = os.path.abspath(root_dir)
        self.modules: Set[str] = set()                     # rel_path
        self.edges: Dict[str, Set[str]] = defaultdict(set) # A -> {B} (A imports B)
        self.reverse_edges: Dict[str, Set[str]] = defaultdict(set) # B -> {A} (B imported by A)
        self.symbols_imported: Dict[Tuple[str, str], List[str]] = defaultdict(list)
        self.module_sizes: Dict[str, int] = {}             # lines of code
        self.module_languages: Dict[str, str] = {}         # "python" or "typescript"

    def add_module(self, rel_path: str, loc: int, lang: str) -> None:
        self.modules.add(rel_path)
        self.module_sizes[rel_path] = loc
        self.module_languages[rel_path] = lang

    def add_dependency(self, from_mod: str, to_mod: str, symbols: Optional[List[str]] = None) -> None:
        if from_mod != to_mod:
            self.edges[from_mod].add(to_mod)
            self.reverse_edges[to_mod].add(from_mod)
            if symbols:
                self.symbols_imported[(from_mod, to_mod)].extend(symbols)


def build_repository_dependency_graph(root_dir: str) -> DependencyGraph:
    """
    Builds the complete cross-module dependency graph for a repository.
    Handles Python (`.py`) and TypeScript/JavaScript (`.ts`, `.tsx`, `.js`, `.jsx`).
    """
    root_abs = os.path.abspath(root_dir)
    graph = DependencyGraph(root_abs)
    tsconfig = find_tsconfig(root_abs)

    # 1. First pass: register all code files
    file_map: Dict[str, str] = {} # abs_path -> rel_path
    
    for root, dirs, files in os.walk(root_abs):
        dirs[:] = [
            d for d in dirs
            if not is_ignored_path(os.path.relpath(os.path.join(root, d), root_abs))
        ]
        
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), root_abs)
            if is_ignored_path(rel):
                continue
            
            _, ext = os.path.splitext(f)
            ext_lower = ext.lower()
            full_p = os.path.join(root, f)
            
            if is_minified_or_binary(full_p):
                continue
                
            if ext_lower == ".py":
                try:
                    with open(full_p, "r", encoding="utf-8", errors="ignore") as fh:
                        lines = fh.readlines()
                    graph.add_module(rel, len(lines), "python")
                    file_map[full_p] = rel
                except Exception:
                    pass
            elif ext_lower in {".ts", ".tsx", ".js", ".jsx"}:
                try:
                    with open(full_p, "r", encoding="utf-8", errors="ignore") as fh:
                        lines = fh.readlines()
                    graph.add_module(rel, len(lines), "typescript")
                    file_map[full_p] = rel
                except Exception:
                    pass

    # 2. Second pass: extract imports and connect directed edges
    for full_p, rel_src in file_map.items():
        lang = graph.module_languages.get(rel_src)
        
        if lang == "python":
            try:
                with open(full_p, "r", encoding="utf-8", errors="ignore") as fh:
                    tree = ast.parse(fh.read(), filename=full_p)
                
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            resolved_abs = resolve_local_module_path(full_p, alias.name, root_abs)
                            if resolved_abs and resolved_abs in file_map:
                                graph.add_dependency(rel_src, file_map[resolved_abs], [alias.name])
                    elif isinstance(node, ast.ImportFrom):
                        level = getattr(node, "level", 0)
                        module_name = ("." * level) + (node.module or "")
                        resolved_abs = resolve_local_module_path(full_p, module_name, root_abs)
                        if resolved_abs and resolved_abs in file_map:
                            syms = [n.name for n in node.names]
                            graph.add_dependency(rel_src, file_map[resolved_abs], syms)
            except Exception:
                pass

        elif lang == "typescript":
            try:
                with open(full_p, "r", encoding="utf-8", errors="ignore") as fh:
                    content = fh.read()
                parsed = parse_ts_js_ast(content, filename=full_p)
                
                all_imports = []
                for imp in parsed.get("imports", []):
                    all_imports.append((imp["module"], imp.get("names", [])))
                for re_exp in parsed.get("re_exports", []):
                    all_imports.append((re_exp["module"], [re_exp.get("clause", "*")]))
                    
                for mod_spec, syms in all_imports:
                    resolved = resolve_ts_import_path(mod_spec, full_p, root_abs, tsconfig)
                    if resolved and resolved in file_map:
                        graph.add_dependency(rel_src, file_map[resolved], syms)
            except Exception:
                pass

    return graph


def partition_into_subsystems(graph: DependencyGraph) -> Dict[str, Dict[str, Any]]:
    """
    Partitions the dependency graph into logical subsystem clusters based on:
    1. Directory namespace hierarchy (e.g. `django.db.models`, `django.http`).
    2. Internal vs. external dependency density.
    Computes Robert C. Martin's cohesion, coupling (Ca, Ce), and instability (I).
    """
    raw_clusters: Dict[str, List[str]] = defaultdict(list)

    for mod in graph.modules:
        d = os.path.dirname(mod).replace("\\", "/")
        if not d or d == ".":
            cluster_key = "root"
        else:
            parts = d.split("/")
            if parts[0] in ("django", "src", "packages", "lib") and len(parts) >= 3:
                cluster_key = ".".join(parts[:3])
            elif len(parts) >= 2:
                cluster_key = ".".join(parts[:2])
            else:
                cluster_key = parts[0]
        raw_clusters[cluster_key].append(mod)

    clusters: Dict[str, Dict[str, Any]] = {}

    for c_id, mods in raw_clusters.items():
        mod_set = set(mods)
        internal_edges = 0
        external_out_edges = 0 # Ce (Efferent Coupling)
        external_in_edges = 0  # Ca (Afferent Coupling)
        
        external_deps: Set[str] = set()
        external_callers: Set[str] = set()

        for m in mods:
            # Outgoing edges
            for target in graph.edges.get(m, set()):
                if target in mod_set:
                    internal_edges += 1
                else:
                    external_out_edges += 1
                    external_deps.add(target)
            # Incoming edges
            for caller in graph.reverse_edges.get(m, set()):
                if caller not in mod_set:
                    external_in_edges += 1
                    external_callers.add(caller)

        total_edges = internal_edges + external_out_edges
        cohesion = round(internal_edges / (total_edges + 1e-5), 2)
        
        # Martin's Instability Metric: I = Ce / (Ca + Ce)
        ca = len(external_callers)
        ce = len(external_deps)
        instability = round(ce / (ca + ce + 1e-5), 2)

        # Central modules inside cluster (highest degree inside)
        centrality: List[Tuple[str, int]] = []
        for m in mods:
            deg = len(graph.edges.get(m, set()) & mod_set) + len(graph.reverse_edges.get(m, set()) & mod_set)
            centrality.append((m, deg))
        centrality.sort(key=lambda x: x[1], reverse=True)
        top_bridges = [m for m, deg in centrality[:3] if deg > 0]

        total_loc = sum(graph.module_sizes.get(m, 0) for m in mods)

        clusters[c_id] = {
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

    return clusters


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
    clusters = partition_into_subsystems(graph)

    # Normalize target path
    root_abs = os.path.abspath(root_dir)
    target_abs = os.path.abspath(target) if os.path.exists(target) else os.path.abspath(os.path.join(root_abs, target))
    target_rel = os.path.relpath(target_abs, root_abs)

    if target_rel not in graph.modules:
        # Check if target is a substring or partial match
        matched = [m for m in graph.modules if target_rel in m or m.endswith(target)]
        if matched:
            target_rel = matched[0]
        else:
            return {
                "success": False,
                "error": f"Target '{target}' not found in dependency graph.",
                "duration_ms": round((time.perf_counter() - t0) * 1000, 2)
            }

    # 1. Direct callers / importers
    direct_callers = sorted(list(graph.reverse_edges.get(target_rel, set())))

    # 2. Transitive Downstream Reachability (BFS)
    reachable: Set[str] = set()
    queue = deque([target_rel])
    visited: Set[str] = {target_rel}

    while queue:
        curr = queue.popleft()
        for caller in graph.reverse_edges.get(curr, set()):
            if caller not in visited:
                visited.add(caller)
                reachable.add(caller)
                queue.append(caller)

    reachable_sorted = sorted(list(reachable))

    # 3. Target cluster and touched clusters
    target_cluster_id = None
    touched_clusters: Set[str] = set()

    for c_id, c_data in clusters.items():
        if target_rel in c_data["modules"]:
            target_cluster_id = c_id
        for r in reachable:
            if r in c_data["modules"]:
                touched_clusters.add(c_id)

    # 4. Cycle Participation
    # Check if target_rel is in any cycle
    in_cycle = False
    cycle_chain: List[str] = []

    def find_cycle(curr: str, stack: List[str], seen: Set[str]) -> bool:
        if curr in stack:
            idx = stack.index(curr)
            cycle_chain.extend(stack[idx:] + [curr])
            return True
        if curr in seen or len(stack) > 10:
            return False
        seen.add(curr)
        stack.append(curr)
        for nxt in graph.edges.get(curr, set()):
            if find_cycle(nxt, stack, seen):
                return True
        stack.pop()
        return False

    seen_set: Set[str] = set()
    find_cycle(target_rel, [], seen_set)
    in_cycle = len(cycle_chain) > 0

    # 5. Targeted Test Recommendation
    recommended_tests: List[str] = []
    # Check for tests related to target and affected modules
    target_base = os.path.splitext(os.path.basename(target_rel))[0].replace("test_", "")
    
    for mod in graph.modules:
        if "test" in mod.lower():
            if target_base in mod.lower() or any(os.path.splitext(os.path.basename(r))[0] in mod for r in direct_callers[:5]):
                recommended_tests.append(mod)

    # 6. Risk Classification
    # Score based on blast radius, cross-cluster impact, and cycle participation
    risk_factors: List[str] = []
    
    if in_cycle:
        risk_factors.append(f"Participates in circular import path ({' -> '.join(cycle_chain[:4])}...)")
    if len(direct_callers) >= 10:
        risk_factors.append(f"High direct fan-in: {len(direct_callers)} modules directly import this file")
    if len(reachable) >= 25:
        risk_factors.append(f"Broad blast radius: {len(reachable)} modules reach this file transitively")
    if len(touched_clusters) > 1:
        risk_factors.append(f"Cross-cluster boundary crossing: impacts {len(touched_clusters)} distinct subsystems ({', '.join(sorted(touched_clusters)[:4])})")

    if in_cycle or len(reachable) >= 30 or len(direct_callers) >= 15:
        risk_level = "HIGH_RISK"
    elif len(reachable) >= 8 or len(direct_callers) >= 4 or len(touched_clusters) > 1:
        risk_level = "MEDIUM_RISK"
    else:
        risk_level = "LOW_RISK"

    duration_ms = round((time.perf_counter() - t0) * 1000, 2)

    return {
        "success": True,
        "target": target_rel,
        "language": graph.module_languages.get(target_rel, "unknown"),
        "lines_of_code": graph.module_sizes.get(target_rel, 0),
        "cluster": target_cluster_id,
        "risk_level": risk_level,
        "risk_factors": risk_factors,
        "direct_callers_count": len(direct_callers),
        "direct_callers": direct_callers[:15],
        "transitive_blast_radius_count": len(reachable),
        "transitive_blast_radius": reachable_sorted[:25],
        "touched_clusters_count": len(touched_clusters),
        "touched_clusters": sorted(list(touched_clusters)),
        "in_circular_cycle": in_cycle,
        "cycle_path": cycle_chain,
        "recommended_tests": recommended_tests[:10],
        "duration_ms": duration_ms
    }


def format_cluster_map_terminal(clusters: Dict[str, Dict[str, Any]], repo_name: str) -> str:
    """Formats a clean, human-readable terminal output for repository cluster maps."""
    lines: List[str] = []
    lines.append(f"🗺️  Architectural Subsystem Cluster Map: {repo_name}")
    lines.append(f"Total Discovered Subsystems: {len(clusters)}\n")
    lines.append(f"{'Cluster Subsystem':<32} {'Files':<8} {'Cohesion':<10} {'Instability (I)':<16} {'Top Bridges / Facades'}")
    lines.append("-" * 95)

    # Sort by files count descending
    sorted_clusters = sorted(clusters.values(), key=lambda c: c["modules_count"], reverse=True)

    for c in sorted_clusters:
        c_id = c["cluster_id"]
        count = c["modules_count"]
        cohesion = f"{c['cohesion_ratio']:.2f}"
        i_metric = f"{c['instability']:.2f}"
        bridges = ", ".join(os.path.basename(b) for b in c["top_bridge_modules"][:2]) or "-"
        lines.append(f"{c_id:<32} {count:<8} {cohesion:<10} {i_metric:<16} {bridges}")

    return "\n".join(lines)


def format_change_impact_terminal(impact: Dict[str, Any]) -> str:
    """Formats a structured, actionable Change Impact Report for terminal & agents."""
    lines: List[str] = []
    risk = impact["risk_level"]
    risk_color = "🔴" if risk == "HIGH_RISK" else ("🟡" if risk == "MEDIUM_RISK" else "🟢")
    
    lines.append(f"🧠 Change Impact Analysis: {impact['target']}")
    lines.append(f"Subsystem Cluster: {impact['cluster']} | Size: {impact['lines_of_code']} LOC | Language: {impact['language']}")
    lines.append(f"Risk Classification: {risk_color} {risk}")

    if impact["risk_factors"]:
        lines.append("\nDetected Risk Factors:")
        for rf in impact["risk_factors"]:
            lines.append(f"  ⚠️  {rf}")

    lines.append(f"\nBlast Radius & Reachability:")
    lines.append(f"  • Direct Callers ({impact['direct_callers_count']}):")
    for dc in impact["direct_callers"][:6]:
        lines.append(f"     ↳ {dc}")
    if impact["direct_callers_count"] > 6:
        lines.append(f"     ↳ ... ({impact['direct_callers_count'] - 6} more direct callers)")

    lines.append(f"  • Transitive Reachability ({impact['transitive_blast_radius_count']} modules total)")
    lines.append(f"  • Crossed Subsystems ({impact['touched_clusters_count']}): {', '.join(impact['touched_clusters'])}")

    if impact["recommended_tests"]:
        lines.append("\nRecommended Targeted Tests:")
        for t in impact["recommended_tests"][:5]:
            lines.append(f"  🧪 {t}")

    lines.append(f"\nExecution Duration: {impact['duration_ms']}ms")
    return "\n".join(lines)
