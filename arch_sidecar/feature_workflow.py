import os
import ast
import re
import subprocess
import time
from typing import Dict, List, Optional, Set, Any, Tuple

from .utils import DEFAULT_IGNORED_DIRS, is_ignored_path, is_minified_or_binary, estimate_tokens
from .context import SymbolFinder, find_references_in_file
from .uncertainty import scan_file_uncertainty, ConfidenceLevel
from .guard import check_code_health, resolve_local_module_path


STOPWORDS = {
    "the", "and", "from", "for", "with", "into", "class", "def", "file",
    "code", "module", "target", "extract", "decouple", "move", "split",
    "refactor", "function", "method", "should", "need", "where", "when",
    "what", "which", "that", "this", "have", "does", "using", "only",
    "implement", "feature", "add", "create", "support", "new", "make",
    "update", "change", "allow", "return", "returns", "call", "calling",
}


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
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id == "__all__":
                if isinstance(node.value, (ast.List, ast.Tuple)):
                    items = []
                    for elt in node.value.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            items.append(elt.value)
                    self.exports_all = items

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


def classify_feature_intent(task: str) -> Dict[str, Any]:
    """
    Step 1: Understand the requested change.
    Extracts intent category, explicit file mentions, identifier tokens, and route/CLI patterns.
    """
    task_lower = task.lower()
    if any(k in task_lower for k in ("endpoint", "route", "@router", "@app", "get ", "post ", "delete ", "put ")):
        category = "add_or_update_endpoint"
    elif any(k in task_lower for k in ("option", "flag", "argument", "subcommand", "cli", "@click")):
        category = "add_or_update_cli_feature"
    elif any(k in task_lower for k in ("class ", "subclass", "dataclass", "model")):
        category = "add_or_extend_class"
    elif any(k in task_lower for k in ("fix", "bug", "error", "exception", "handle", "validate")):
        category = "validation_or_bugfix"
    else:
        category = "add_or_extend_function"

    explicit_files = re.findall(r"[\w\-./]+\.py\b", task)
    raw_tokens = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b", task)
    keywords = []
    seen = set()
    for tok in raw_tokens:
        if tok.lower() not in STOPWORDS and not tok.endswith(".py") and tok not in seen:
            keywords.append(tok)
            seen.add(tok)

    return {
        "category": category,
        "explicit_files": explicit_files,
        "keywords": keywords,
    }


def detect_git_modified_py_files(root_abs: str) -> List[str]:
    """Detects modified or untracked Python files in a git working tree relative to root_abs."""
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
            if path_part.endswith(".py") and os.path.exists(os.path.join(root_abs, path_part)):
                files.append(path_part)
        return files
    except Exception:
        return []


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
    root_abs = os.path.abspath(project_root)
    explicit_single_file: Optional[str] = None
    if os.path.isfile(root_abs):
        explicit_single_file = os.path.basename(root_abs)
        root_abs = os.path.dirname(root_abs)

    # =========================================================================
    # STEP 1: Understand the Requested Change
    # =========================================================================
    intent = classify_feature_intent(task)
    if explicit_single_file and explicit_single_file not in intent["explicit_files"]:
        intent["explicit_files"].insert(0, explicit_single_file)
    if target_files:
        for tf in target_files:
            if tf not in intent["explicit_files"]:
                intent["explicit_files"].append(tf)

    query_keywords = list(dict.fromkeys((target_symbols or []) + intent["keywords"]))

    # =========================================================================
    # STEP 2: Find Relevant Existing Implementation
    # =========================================================================
    source_files: List[Tuple[str, str]] = []
    test_files: List[Tuple[str, str]] = []

    for current_root, dirs, files in os.walk(root_abs):
        dirs[:] = [d for d in dirs if d not in DEFAULT_IGNORED_DIRS and not d.startswith(".")]
        for file in sorted(files):
            if not file.endswith(".py"):
                continue
            full_path = os.path.join(current_root, file)
            rel_path = os.path.relpath(full_path, root_abs)
            if is_ignored_path(rel_path) or is_minified_or_binary(full_path):
                continue
            is_test = "test" in rel_path.lower().split(os.sep) or file.startswith("test_") or file.endswith("_test.py")
            if is_test:
                test_files.append((rel_path, full_path))
            else:
                source_files.append((rel_path, full_path))

    # If all files in the directory are test files or none matched source_files, fall back to all files
    if not source_files and test_files:
        source_files = list(test_files)

    file_analyzers: Dict[str, DetailedFileAnalyzer] = {}
    file_lines_map: Dict[str, List[str]] = {}
    file_trees_map: Dict[str, ast.Module] = {}

    for rel_path, full_path in source_files:
        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            tree = ast.parse("".join(lines), filename=rel_path)
            analyzer = DetailedFileAnalyzer(rel_path, lines)
            analyzer.visit(tree)
            file_analyzers[rel_path] = analyzer
            file_lines_map[rel_path] = lines
            file_trees_map[rel_path] = tree
        except Exception:
            continue

    # Score source files and symbols
    file_scores: Dict[str, float] = {rel: 0.0 for rel in file_analyzers}
    scored_symbols: List[Dict[str, Any]] = []

    for rel_path, analyzer in file_analyzers.items():
        base_name = os.path.basename(rel_path)
        stem = os.path.splitext(base_name)[0].lower()

        # Boost explicitly named files
        for exp_f in intent["explicit_files"]:
            if rel_path == exp_f or rel_path.endswith("/" + exp_f) or base_name == os.path.basename(exp_f):
                file_scores[rel_path] += 120.0

        # Boost filename keyword matches
        for kw in query_keywords:
            kw_low = kw.lower()
            if kw_low == stem:
                file_scores[rel_path] += 45.0
            elif len(kw_low) >= 4 and (kw_low in stem or stem in kw_low):
                file_scores[rel_path] += 25.0

        # Score symbols inside file
        for sym_name, sym_info in analyzer.symbols.items():
            s_score = 0.0
            short_name = sym_name.split(".")[-1]
            code_low = sym_info["code"].lower()

            if target_symbols and (sym_name in target_symbols or short_name in target_symbols):
                s_score += 100.0

            for kw in query_keywords:
                kw_low = kw.lower()
                if kw == sym_name or kw == short_name:
                    s_score += 80.0
                elif kw_low == short_name.lower():
                    s_score += 65.0
                elif len(kw_low) >= 4 and kw_low in short_name.lower():
                    s_score += 35.0
                elif len(kw_low) >= 4 and kw_low in code_low:
                    s_score += 12.0

            if any(rel_path.endswith(ef) or os.path.basename(rel_path) == os.path.basename(ef) for ef in intent["explicit_files"]):
                s_score += 20.0

            if s_score > 0:
                file_scores[rel_path] += min(s_score * 0.5, 60.0)
                scored_symbols.append({
                    "name": sym_name,
                    "short_name": short_name,
                    "file": rel_path,
                    "type": sym_info["type"],
                    "start_line": sym_info["start_line"],
                    "end_line": sym_info["end_line"],
                    "signature": sym_info["signature"],
                    "score": round(s_score, 1),
                    "info": sym_info,
                })

    scored_symbols.sort(key=lambda x: (-x["score"], x["file"], x["start_line"]))
    ranked_files = sorted(file_scores.items(), key=lambda x: (-x[1], len(file_lines_map.get(x[0], []))))
    primary_files = [f for f, sc in ranked_files[:2] if sc > 0] or ([ranked_files[0][0]] if ranked_files else [])

    primary_file = primary_files[0] if primary_files else None
    primary_lines = file_lines_map.get(primary_file, []) if primary_file else []

    # Select anchor symbols in primary_file
    primary_candidates = [s for s in scored_symbols if s["file"] == primary_file]
    if any(s["score"] >= 45.0 for s in primary_candidates):
        primary_candidates = [s for s in primary_candidates if s["score"] > 20.0]
    anchor_symbols = primary_candidates[:4]
    if not anchor_symbols and primary_file and primary_file in file_analyzers:
        # Take up to 3 representative top-level symbols from primary_file
        for sym_name, sym_info in list(file_analyzers[primary_file].symbols.items())[:3]:
            anchor_symbols.append({
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

    # =========================================================================
    # STEP 3: Map Dependencies & Likely Impact
    # =========================================================================
    cross_file_dependencies: List[Dict[str, Any]] = []
    seen_dep_names: Set[str] = set()
    anchor_combined_code = "\n".join(a["info"]["code"] for a in anchor_symbols)

    # 3a. Cross-file symbols matching task keywords or imported by primary_file and used in anchors
    for s in scored_symbols:
        if s["file"] != primary_file and s["name"] not in seen_dep_names and s["score"] >= 35.0:
            cross_file_dependencies.append({
                "symbol": s["name"],
                "file": s["file"],
                "lines": f"{s['start_line']}-{s['end_line']}",
                "signature": s["signature"],
                "numbered_code": s["info"]["numbered_code"],
            })
            seen_dep_names.add(s["name"])
            if len(cross_file_dependencies) >= 3:
                break

    # Also inspect local modules imported by primary_file that are used by anchor_symbols or match keywords
    if primary_file and primary_file in file_trees_map:
        primary_abs = os.path.join(root_abs, primary_file)
        for node in file_trees_map[primary_file].body:
            if isinstance(node, ast.ImportFrom):
                mod_name = node.module or ""
                if node.level > 0:
                    mod_name = ("." * node.level) + mod_name
                resolved_abs = resolve_local_module_path(primary_abs, mod_name, root_abs)
                if resolved_abs:
                    resolved_rel = os.path.relpath(resolved_abs, root_abs)
                    if resolved_rel in file_analyzers and resolved_rel != primary_file:
                        imp_analyzer = file_analyzers[resolved_rel]
                        for alias in node.names:
                            sym_target = alias.name
                            local_alias = alias.asname or alias.name
                            is_used_in_anchor = bool(
                                re.search(rf"\b{re.escape(local_alias)}\b", anchor_combined_code)
                            )
                            is_kw_match = any(kw.lower() in sym_target.lower() for kw in query_keywords)
                            if (
                                sym_target in imp_analyzer.symbols
                                and sym_target not in seen_dep_names
                                and (is_used_in_anchor or is_kw_match or not cross_file_dependencies)
                            ):
                                s_info = imp_analyzer.symbols[sym_target]
                                cross_file_dependencies.append({
                                    "symbol": sym_target,
                                    "file": resolved_rel,
                                    "lines": f"{s_info['start_line']}-{s_info['end_line']}",
                                    "signature": s_info["signature"],
                                    "numbered_code": s_info["numbered_code"],
                                })
                                seen_dep_names.add(sym_target)

    # 3b. Callers / Blast Radius across source files
    downstream_callers: List[Dict[str, Any]] = []
    if primary_file:
        primary_mod_stem = os.path.splitext(os.path.basename(primary_file))[0]
        search_terms = {primary_mod_stem} | {a["short_name"] for a in anchor_symbols[:3]}
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

    # 3c. Related Test Files discovery
    related_tests: List[str] = []
    if primary_file:
        primary_stem = os.path.splitext(os.path.basename(primary_file))[0]
        search_terms = {primary_stem} | {a["short_name"] for a in anchor_symbols[:3]}
        for test_rel, test_full in test_files:
            test_base = os.path.basename(test_rel).lower()
            if primary_stem.lower() in test_base:
                related_tests.insert(0, test_rel)
            else:
                refs = find_references_in_file(test_full, search_terms)
                if refs:
                    related_tests.append(test_rel)
    if not related_tests and test_files:
        related_tests = [t[0] for t in test_files[:2]]

    has_src_layout = os.path.isdir(os.path.join(root_abs, "src"))
    py_prefix = "PYTHONPATH=src " if has_src_layout else ""
    inferred_test_cmd = test_command
    if not inferred_test_cmd and related_tests:
        inferred_test_cmd = f"{py_prefix}python3 -m pytest -q -p no:cacheprovider {related_tests[0]}"

    # 3d. Dynamic Reflection / Uncertainty in primary_file
    uncertainty_findings = []
    if primary_file:
        u_list = scan_file_uncertainty(os.path.join(root_abs, primary_file), primary_file)
        uncertainty_findings = [u.to_dict() for u in u_list]

    # =========================================================================
    # STEP 4: Produce an Actionable Implementation Plan
    # =========================================================================
    implementation_plan: List[str] = []
    if primary_file:
        total_lines = len(primary_lines)
        if anchor_symbols:
            top_anc = anchor_symbols[0]
            implementation_plan.append(
                f"1. **Target Location:** Edit `{primary_file}` ({total_lines} lines) near `{top_anc['name']}` "
                f"(lines {top_anc['start_line']}–{top_anc['end_line']}), following its existing signature and return convention."
            )
        else:
            implementation_plan.append(
                f"1. **Target Location:** Implement the requested change in `{primary_file}` ({total_lines} lines)."
            )

        if cross_file_dependencies:
            dep_summary = ", ".join(f"`{d['symbol']}` ({d['file']}:{d['lines']})" for d in cross_file_dependencies[:3])
            implementation_plan.append(
                f"2. **Reuse Existing Dependencies:** Call verified project helpers/types: {dep_summary}."
            )
        else:
            imports_preview = ", ".join(f"`{imp}`" for imp in (file_analyzers[primary_file].imports[:3] if primary_file in file_analyzers else []))
            if imports_preview:
                implementation_plan.append(
                    f"2. **Module Imports Available:** `{primary_file}` already imports {imports_preview}."
                )

        if primary_file in file_analyzers and file_analyzers[primary_file].exports_all is not None:
            implementation_plan.append(
                f"3. **Export Contract:** `{primary_file}` defines `__all__`; add any new public symbol to `__all__` if it must be exported."
            )
        elif downstream_callers:
            caller_files = ", ".join(f"`{c['file']}`" for c in downstream_callers[:3])
            implementation_plan.append(
                f"3. **Downstream Impact:** Preserving existing signatures in `{primary_file}` keeps callers ({caller_files}) compatible."
            )

        verify_cmd_str = f'arch-sidecar feature "{task}" {project_root} --verify'
        if inferred_test_cmd:
            verify_cmd_str += f' --test "{inferred_test_cmd}"'
        implementation_plan.append(
            f"4. **Preflight & Verification:** After editing, run `{verify_cmd_str}` to execute AST scope/import preflight + test verification."
        )

    # =========================================================================
    # STEP 5: Supply the Coding Agent with Targeted 1-Indexed Context
    # =========================================================================
    context_blocks: List[str] = [
        f"# Devrith Feature Workflow Plan & Context",
        f"> **Principle:** Understand before changing. Check before trusting. Verify before claiming success.",
        f"> **Detected Intent:** `{intent['category']}` | **Primary Target:** `{primary_file or 'N/A'}`\n",
        "## 1. Actionable Implementation Plan",
    ]
    for step_line in implementation_plan:
        context_blocks.append(step_line)

    context_blocks.append("\n## 2. Dependency & Impact Map")
    if anchor_symbols:
        anc_str = ", ".join(f"`{a['name']}` (L{a['start_line']}-{a['end_line']})" for a in anchor_symbols)
        context_blocks.append(f"- **Relevant Existing Symbols:** {anc_str}")
    if cross_file_dependencies:
        dep_str = ", ".join(f"`{d['symbol']}` in `{d['file']}` (L{d['lines']})" for d in cross_file_dependencies[:4])
        context_blocks.append(f"- **Cross-File Dependencies:** {dep_str}")
    if downstream_callers:
        cal_str = ", ".join(f"`{c['file']}` ({c['reference_count']} refs)" for c in downstream_callers[:4])
        context_blocks.append(f"- **Downstream Callers / Blast Radius:** {cal_str}")
    else:
        context_blocks.append("- **Downstream Callers / Blast Radius:** Local to target module (0 external callers matched)")
    if related_tests:
        context_blocks.append(f"- **Related Test Suite:** {', '.join(f'`{t}`' for t in related_tests[:3])}")
    if inferred_test_cmd:
        context_blocks.append(f"- **Verification Command:** `{inferred_test_cmd}`")

    # Targeted Code Context with 1-indexed line numbers so agent can edit immediately without view_file
    if primary_file:
        total_lines = len(primary_lines)
        if total_lines <= 160:
            numbered_full = "".join(f"{i + 1}: {line}" for i, line in enumerate(primary_lines)).rstrip()
            context_blocks.extend([
                f"\n## 3. Target Module Context: `{primary_file}` (Full File, {total_lines} lines, 1-indexed)",
                "> ⚡ **Fast-Path Ready:** 1-indexed line numbers are included below. Edit directly without calling `view_file`.",
                "```python",
                numbered_full,
                "```",
            ])
        else:
            context_blocks.extend([
                f"\n## 3. Surgical Target Context: `{primary_file}` ({total_lines} lines total, 1-indexed slices)",
                "> ⚡ **Fast-Path Ready:** Exact 1-indexed line numbers are included below so you can edit directly around these lines.",
            ])
            # Show module imports header (lines 1..import_end_line)
            imp_end = min(file_analyzers[primary_file].import_end_line or 25, 45)
            header_numbered = "".join(f"{i + 1}: {primary_lines[i]}" for i in range(min(imp_end, total_lines))).rstrip()
            context_blocks.extend([
                f"### Module Header & Imports (`{primary_file}:1-{imp_end}`)",
                "```python",
                header_numbered,
                "```",
            ])
            used_tokens = estimate_tokens("\n".join(context_blocks))
            emitted_ranges: List[Tuple[int, int]] = [(1, imp_end)]

            def _is_covered(s_line: int, e_line: int) -> bool:
                return any(r_start <= s_line and e_line <= r_end for r_start, r_end in emitted_ranges)

            for anc in anchor_symbols:
                s_line, e_line = anc["start_line"], anc["end_line"]
                if _is_covered(s_line, e_line):
                    continue

                # If this anchor is a large class (>70 lines), emit its header (first 20 lines) + tail (last 15 lines)
                # and let specific method anchors provide the detailed method bodies.
                if anc["type"] == "class" and (e_line - s_line) > 70:
                    head_end = min(s_line + 19, e_line)
                    cls_tail_start = max(head_end + 1, e_line - 14)
                    head_code = "".join(f"{i}: {primary_lines[i - 1]}" for i in range(s_line, head_end + 1)).rstrip()
                    tail_code = "".join(f"{i}: {primary_lines[i - 1]}" for i in range(cls_tail_start, e_line + 1)).rstrip()
                    context_blocks.extend([
                        f"### Class Header & Tail: `{anc['name']}` (`{primary_file}:{s_line}-{head_end}` & `{cls_tail_start}-{e_line}`)",
                        "```python",
                        head_code,
                        "# ... (intermediate methods omitted; see matched method anchors below) ...",
                        tail_code,
                        "```",
                    ])
                    emitted_ranges.append((s_line, head_end))
                    emitted_ranges.append((cls_tail_start, e_line))
                    used_tokens += estimate_tokens(head_code + tail_code)
                    continue

                anc_code = anc["info"]["numbered_code"].rstrip()
                anc_tokens = estimate_tokens(anc_code)
                if used_tokens + anc_tokens > token_budget and anc != anchor_symbols[0]:
                    break
                context_blocks.extend([
                    f"### Anchor Implementation: `{anc['name']}` (`{primary_file}:{s_line}-{e_line}`)",
                    "```python",
                    anc_code,
                    "```",
                ])
                emitted_ranges.append((s_line, e_line))
                used_tokens += anc_tokens

            # Also show tail of file (last 15 lines) in case the agent wants to append a new function/endpoint at EOF
            tail_start = max(1, total_lines - 14)
            if not _is_covered(tail_start, total_lines):
                tail_numbered = "".join(
                    f"{i}: {primary_lines[i - 1]}" for i in range(tail_start, total_lines + 1)
                ).rstrip()
                context_blocks.extend([
                    f"### End of File Region (`{primary_file}:{tail_start}-{total_lines}`)",
                    "```python",
                    tail_numbered,
                    "```",
                ])

    if cross_file_dependencies:
        context_blocks.append("\n## 4. Cross-File Dependency Signatures & Definitions")
        for dep in cross_file_dependencies[:3]:
            dep_lines = dep["numbered_code"].rstrip().splitlines()
            if len(dep_lines) > 25:
                dep_snippet = "\n".join(dep_lines[:25]) + "\n# ... (body truncated)"
            else:
                dep_snippet = "\n".join(dep_lines)
            context_blocks.extend([
                f"### `{dep['symbol']}` (`{dep['file']}:{dep['lines']}`)",
                "```python",
                dep_snippet,
                "```",
            ])

    # Include 1-indexed fast-path tail of the primary related test file so the agent can append a unit test without calling view_file
    if related_tests:
        top_test_rel = related_tests[0]
        top_test_abs = os.path.join(root_abs, top_test_rel)
        try:
            with open(top_test_abs, "r", encoding="utf-8", errors="ignore") as tf:
                t_lines = tf.readlines()
            t_total = len(t_lines)
            t_tail_start = max(1, t_total - 17)
            t_tail_numbered = "".join(f"{i}: {t_lines[i - 1]}" for i in range(t_tail_start, t_total + 1)).rstrip()
            context_blocks.extend([
                f"\n## 5. Related Test Suite Fast-Path Context: `{top_test_rel}` (lines {t_tail_start}–{t_total} of {t_total}, 1-indexed)",
                "> ⚡ **Fast-Path Ready:** Append new unit tests directly at the end of this file using the 1-indexed lines below.",
                "```python",
                t_tail_numbered,
                "```",
            ])
        except Exception:
            pass

    context_markdown = "\n".join(context_blocks)

    # =========================================================================
    # STEPS 6, 7, 8: Preflight Checks, Test Verification & Evidence Ledger
    # =========================================================================
    preflight_results: List[Dict[str, Any]] = []
    test_verification: Optional[Dict[str, Any]] = None
    statically_verified: List[str] = []
    runtime_verified: List[str] = []
    unverified_risks: List[str] = []
    overall_success = True

    if verify:
        # Determine which files to verify: explicit target_files, git modified files, or primary_file
        files_to_check: List[str] = []
        if target_files:
            files_to_check.extend(target_files)
        elif explicit_single_file:
            files_to_check.append(explicit_single_file)
        else:
            git_mod = detect_git_modified_py_files(root_abs)
            if git_mod:
                files_to_check.extend(git_mod)
            elif primary_file:
                files_to_check.append(primary_file)

        files_to_check = list(dict.fromkeys(files_to_check))

        total_errors = 0
        total_warnings = 0
        for rel_f in files_to_check:
            abs_f = rel_f if os.path.isabs(rel_f) else os.path.join(root_abs, rel_f)
            health = check_code_health(abs_f, project_root=root_abs)
            preflight_results.append(health)
            total_errors += health.get("errors", 0)
            total_warnings += health.get("warnings", 0)

        if total_errors == 0 and preflight_results:
            checked_names = ", ".join(r["file"] for r in preflight_results)
            statically_verified.append(f"AST syntax valid across {len(preflight_results)} file(s): {checked_names}")
            statically_verified.append("Two-pass lexical scope binding passed: 0 undefined or hallucinated symbols")
            statically_verified.append("Cross-module local import resolution passed: all imported local symbols exist")
            statically_verified.append("Module import DAG check passed: 0 circular import cycles detected")
        else:
            overall_success = False
            for r in preflight_results:
                for issue in r.get("issues", []):
                    unverified_risks.append(
                        f"[PREFLIGHT {issue['severity']}] {r['file']}:{issue['line']} ({issue['type']}): {issue['message']}"
                    )

        # Step 7: Run Test Suite if preflight succeeded (or even if requested, to report full picture)
        cmd_to_run = test_command or inferred_test_cmd
        if cmd_to_run:
            t_test = time.perf_counter()
            try:
                res = subprocess.run(
                    cmd_to_run,
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
                    "command": cmd_to_run,
                    "passed": res.returncode == 0,
                    "returncode": res.returncode,
                    "passed_count": passed_count,
                    "failed_count": failed_count,
                    "duration_ms": test_dur_ms,
                    "output": combined_out[-2000:],
                }
                if res.returncode == 0:
                    runtime_verified.append(
                        f"Test suite passed: `{cmd_to_run}` (exit 0, {passed_count} passed in {test_dur_ms}ms)"
                    )
                else:
                    overall_success = False
                    unverified_risks.append(
                        f"Runtime test failure: `{cmd_to_run}` exited with code {res.returncode} ({failed_count} failed)"
                    )
            except Exception as e:
                overall_success = False
                test_verification = {
                    "command": cmd_to_run,
                    "passed": False,
                    "error": str(e),
                }
                unverified_risks.append(f"Test execution error for `{cmd_to_run}`: {e}")
        else:
            unverified_risks.append(
                "No runtime test command was supplied or discovered; runtime behavior is NOT verified by automated tests."
            )

        if uncertainty_findings:
            unknown_u = [u for u in uncertainty_findings if u.get("confidence") == ConfidenceLevel.UNKNOWN]
            if unknown_u:
                unverified_risks.append(
                    f"{len(unknown_u)} dynamic reflection site(s) (`getattr`/`eval`/`importlib`) exist in `{primary_file}`; static call graph cannot bound dynamic string dispatch."
                )
    else:
        unverified_risks.append(
            "Orientation-only run (`--verify` not yet executed). Run `arch-sidecar feature ... --verify` after making edits to verify preflight + tests."
        )

    duration_ms = round((time.perf_counter() - t0) * 1000, 2)

    return {
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
