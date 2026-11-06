import os
import ast
import re
import logging
from enum import Enum
from typing import Dict, List, Optional, Set, Any, Tuple

from .utils import is_ignored_path, estimate_tokens
from .digest import generate_digest
from .context import SymbolFinder, find_references_in_file
from .ts_ast import parse_ts_js_ast
from .go_ast import parse_go_ast
from .rust_ast import parse_rust_ast
from .dart_ast import parse_dart_ast
from .jvm_ast import parse_jvm_ast
from .uncertainty import (
    ConfidenceLevel,
    UncertaintyFinding,
    scan_file_uncertainty,
    UncertaintyScanner
)

logger = logging.getLogger(__name__)

class RetrievalStrategy(str, Enum):
    WHOLE_FILE = "whole_file"
    TARGETED_SYMBOLS = "targeted_symbols"
    SYMBOLS_AND_DEPENDENCIES = "symbols_and_dependencies"
    SYMBOLS_AND_CALLERS = "symbols_and_callers"
    AST_SKELETON = "ast_skeleton"

SMALL_FILE_LINE_THRESHOLD = 120
SMALL_FILE_TOKEN_THRESHOLD = 350

SUPPORTED_SOURCE_EXTENSIONS = (
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".go", ".rs", ".dart", ".java", ".kt", ".kts",
)

STOP_KEYWORDS = {
    "extract", "decouple", "refactor", "module", "class", "function",
    "into", "from", "move", "split", "separate", "file", "code",
    "test", "tests", "update", "add", "remove", "with", "for",
}

GENERIC_IDENTIFIER_PARTS = {
    "name", "names", "get", "set", "data", "item", "items",
    "node", "nodes", "list", "dict", "call", "func", "value",
    "values", "info", "path", "file", "line", "lines", "type",
    "str", "text", "init", "main", "run", "visit", "check",
    "parse", "format", "build",
}

def _get_file_language(fpath: str) -> str:
    ext = os.path.splitext(fpath)[1].lower()
    if ext in (".ts", ".tsx"):
        return "typescript"
    if ext in (".js", ".jsx", ".mjs", ".cjs"):
        return "javascript"
    if ext == ".go":
        return "go"
    if ext == ".rs":
        return "rust"
    if ext == ".dart":
        return "dart"
    if ext in (".kt", ".kts"):
        return "kotlin"
    if ext == ".java":
        return "java"
    return "python"


def _match_exact_or_word_name(symbol_name: str, task: str) -> Tuple[int, Optional[str]]:
    task_lower = task.lower()
    sym_lower = symbol_name.lower()
    if re.search(rf"\b{re.escape(symbol_name)}\b", task):
        return 60, f"exact name match '{symbol_name}' in task"
    if re.search(rf"\b{re.escape(sym_lower)}\b", task_lower):
        return 45, f"case-insensitive word match '{sym_lower}'"
    if len(sym_lower) >= 4 and sym_lower in task_lower:
        return 35, f"case-insensitive substring match '{sym_lower}'"
    return 0, None


def _extract_symbol_parts(symbol_name: str) -> List[str]:
    return [
        p.lower()
        for p in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|\d|\W|$)|\d+", symbol_name)
        if len(p) >= 4 and p.lower() not in STOP_KEYWORDS
    ]


def _match_symbol_keywords(
    sym_parts: List[str], task_lower: str, current_score: int
) -> Tuple[int, List[str], Optional[str]]:
    matched_parts = list(dict.fromkeys(p for p in sym_parts if p in task_lower))
    if not matched_parts or current_score != 0:
        return 0, matched_parts, None
    part_score = sum(4 if p in GENERIC_IDENTIFIER_PARTS else 22 for p in matched_parts)
    return part_score, matched_parts, f"matched keywords: {', '.join(matched_parts)}"


def _match_file_location(
    sym_info: Dict[str, Any], task: str, mentioned_files: Set[str]
) -> Tuple[int, Optional[str]]:
    rel_file = sym_info.get("filename", "")
    file_base = os.path.basename(rel_file)
    if file_base in mentioned_files or (rel_file and rel_file in task):
        return 25, f"located in targeted file '{file_base}'"
    return 0, None


def _find_task_domain_keywords(
    task: str, sym_lower: str, sym_parts: List[str]
) -> List[str]:
    return [
        w.lower()
        for w in dict.fromkeys(re.findall(r"\b\w{4,}\b", task))
        if w.lower() not in STOP_KEYWORDS
        and w.lower() not in GENERIC_IDENTIFIER_PARTS
        and w.lower() != sym_lower
        and w.lower() not in sym_parts
    ]


def _score_symbol_matches(
    sym_matches: List[str], sym_doc_matches: List[str], matched_parts: List[str]
) -> Tuple[int, str]:
    new_words = [w for w in sym_matches if w not in matched_parts]
    if new_words:
        doc_bonus = 15 if any(w in sym_doc_matches for w in new_words) else 0
        semantic_score = min(len(new_words) * 15 + doc_bonus, 45)
    else:
        semantic_score = min(len(sym_matches) * 10, 15)
    return semantic_score, f"semantic/docstring matches: {', '.join(sym_matches[:3])}"


def _score_semantic_matches(
    sym_info: Dict[str, Any],
    docstring: Optional[str],
    task_domain_keywords: List[str],
    matched_parts: List[str],
) -> Tuple[int, Optional[str]]:
    sym_doc = (sym_info.get("docstring") or "").lower()
    mod_doc = (docstring or "").lower()
    sym_code = (sym_info.get("code") or "").lower()

    sym_doc_matches = [w for w in task_domain_keywords if sym_doc and w in sym_doc]
    sym_code_matches = [w for w in task_domain_keywords if sym_code and w in sym_code]
    mod_doc_matches = [w for w in task_domain_keywords if mod_doc and w in mod_doc]

    sym_matches = list(dict.fromkeys(sym_doc_matches + sym_code_matches))
    if sym_matches:
        return _score_symbol_matches(sym_matches, sym_doc_matches, matched_parts)

    if mod_doc_matches:
        mod_score = min(len(mod_doc_matches) * 5, 10)
        return mod_score, f"module docstring matches: {', '.join(mod_doc_matches[:3])}"

    return 0, None


def score_symbol_relevance(
    symbol_name: str,
    sym_info: Dict[str, Any],
    task: str,
    mentioned_files: Set[str],
    docstring: Optional[str] = None
) -> Tuple[int, str]:
    """Scores symbol relevance to the task query and returns (score, reason)."""
    score = 0
    reasons: List[str] = []

    name_score, name_reason = _match_exact_or_word_name(symbol_name, task)
    if name_score:
        score += name_score
        reasons.append(name_reason)

    sym_parts = _extract_symbol_parts(symbol_name)
    kw_score, matched_parts, kw_reason = _match_symbol_keywords(sym_parts, task.lower(), score)
    if kw_score:
        score += kw_score
        reasons.append(kw_reason)

    file_score, file_reason = _match_file_location(sym_info, task, mentioned_files)
    if file_score:
        score += file_score
        reasons.append(file_reason)

    task_keywords = _find_task_domain_keywords(task, symbol_name.lower(), sym_parts)
    sem_score, sem_reason = _score_semantic_matches(sym_info, docstring, task_keywords, matched_parts)
    if sem_score:
        score += sem_score
        reasons.append(sem_reason)

    reason_str = "; ".join(reasons) if reasons else "no direct task match"
    return score, reason_str


class ProjectSymbolIndex:
    """Aggregated index of symbols, imports, docs, lines, and uncertainties."""

    def __init__(self) -> None:
        self.file_lines_map: Dict[str, List[str]] = {}
        self.file_docstrings_map: Dict[str, Optional[str]] = {}
        self.file_constants_map: Dict[str, List[str]] = {}
        self.file_imports_map: Dict[str, List[str]] = {}
        self.all_symbols: Dict[str, Dict[str, Any]] = {}
        self.file_uncertainties: Dict[str, List[UncertaintyFinding]] = {}


def _resolve_project_target(task: str, root_dir: str) -> Tuple[str, Optional[str], Set[str]]:
    root_abs = os.path.abspath(root_dir)
    explicit_file_rel: Optional[str] = None
    if os.path.isfile(root_abs):
        explicit_file_rel = os.path.basename(root_abs)
        root_abs = os.path.dirname(root_abs)

    mentioned = set(re.findall(r"\b[\w\-./]+\.(?:py|js|ts|tsx|jsx|go|rs|dart|java|kt|kts|json)\b", task))
    if explicit_file_rel:
        mentioned.add(explicit_file_rel)
    return root_abs, explicit_file_rel, mentioned


def _collect_source_files(root_abs: str) -> List[Tuple[str, str]]:
    all_files: List[Tuple[str, str]] = []
    for root, dirs, files in os.walk(root_abs):
        dirs[:] = [
            d for d in dirs
            if not is_ignored_path(os.path.relpath(os.path.join(root, d), root_abs))
        ]
        for f in files:
            _, ext = os.path.splitext(f)
            if ext.lower() not in SUPPORTED_SOURCE_EXTENSIONS:
                continue
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, root_abs)
            if not is_ignored_path(rel_path):
                all_files.append((rel_path, full_path))
    return all_files


def _parse_polyglot_declarations(
    content: str, rel_path: str, ext_lower: str
) -> Tuple[List[str], str, List[Dict[str, Any]]]:
    if ext_lower in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
        parsed = parse_ts_js_ast(content, filename=rel_path)
        imports = [f"import {imp['module']}" for imp in parsed.get("imports", [])[:10]]
        lang = "typescript" if ext_lower in (".ts", ".tsx") else "javascript"
        return imports, lang, parsed.get("declarations", [])
    if ext_lower == ".go":
        parsed = parse_go_ast(content, filename=rel_path)
        imports = [f'import "{imp["module"]}"' for imp in parsed.get("imports", [])[:10]]
        return imports, "go", parsed.get("declarations", [])
    if ext_lower == ".rs":
        parsed = parse_rust_ast(content, filename=rel_path)
        imports = [f"use {imp['module']};" for imp in parsed.get("imports", [])[:10]]
        return imports, "rust", parsed.get("declarations", [])
    if ext_lower == ".dart":
        parsed = parse_dart_ast(content, filename=rel_path)
        imports = [f"import '{imp['module']}';" for imp in parsed.get("imports", [])[:10]]
        return imports, "dart", parsed.get("declarations", [])
    if ext_lower in (".java", ".kt", ".kts"):
        parsed = parse_jvm_ast(content, filename=rel_path)
        imports = [f"import {imp['module']};" for imp in parsed.get("imports", [])[:10]]
        default_lang = "kotlin" if ext_lower in (".kt", ".kts") else "java"
        lang = parsed.get("language", default_lang)
        return imports, lang, parsed.get("declarations", [])
    return [], "python", []


def _create_symbol_declarations(
    declarations: List[Dict[str, Any]],
    rel_path: str,
    lang: str,
) -> Dict[str, Dict[str, Any]]:
    created: Dict[str, Dict[str, Any]] = {}
    for decl in declarations:
        sym_name = decl["name"]
        created[f"{rel_path}:{sym_name}"] = {
            "type": decl.get("kind", "symbol"),
            "name": sym_name,
            "filename": rel_path,
            "start_line": decl.get("start_line", decl.get("line", 1)),
            "end_line": decl.get("end_line", decl.get("line", 1)),
            "code": decl.get("code", decl.get("signature", "")),
            "lang": lang,
            "docstring": decl.get("docstring"),
        }
    return created


def _index_python_source(
    index: ProjectSymbolIndex,
    rel_path: str,
    full_path: str,
    content: str,
    lines: List[str],
) -> None:
    tree = ast.parse(content, filename=full_path)
    finder = SymbolFinder(rel_path, lines)
    finder.visit(tree)
    index.file_imports_map[rel_path] = finder.imports
    index.file_docstrings_map[rel_path] = ast.get_docstring(tree)
    index.file_constants_map[rel_path] = finder.constants

    for sym_name, sym_info in finder.symbols.items():
        sym_info["lang"] = "python"
        index.all_symbols[f"{rel_path}:{sym_name}"] = sym_info

    scanner = UncertaintyScanner(rel_path, lines)
    scanner.visit(tree)
    if scanner.findings:
        index.file_uncertainties[rel_path] = scanner.findings


def _index_source_file(index: ProjectSymbolIndex, rel_path: str, full_path: str) -> None:
    try:
        with open(full_path, "r", encoding="utf-8", errors="ignore") as file_obj:
            lines = file_obj.readlines()
        index.file_lines_map[rel_path] = lines
        content = "".join(lines)
        _, ext = os.path.splitext(full_path)
        ext_lower = ext.lower()

        if ext_lower == ".py":
            _index_python_source(index, rel_path, full_path, content, lines)
        else:
            imports, lang, decls = _parse_polyglot_declarations(content, rel_path, ext_lower)
            index.file_imports_map[rel_path] = imports
            decls_map = _create_symbol_declarations(decls, rel_path, lang)
            index.all_symbols.update(decls_map)
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError) as err:
        logger.debug("Failed indexing %s: %s", full_path, err)


def _build_symbol_index(root_abs: str) -> Tuple[List[Tuple[str, str]], ProjectSymbolIndex]:
    source_files = _collect_source_files(root_abs)
    index = ProjectSymbolIndex()
    for rel_path, full_path in source_files:
        _index_source_file(index, rel_path, full_path)
    return source_files, index


def _score_single_candidate(
    full_sym_id: str,
    sym_info: Dict[str, Any],
    task: str,
    mentioned_files: Set[str],
    doc: Optional[str],
    target_symbols: Optional[List[str]],
) -> Optional[Dict[str, Any]]:
    sym_name = sym_info["name"]
    rel_file = sym_info["filename"]

    if target_symbols and (sym_name in target_symbols or any(s.lower() == sym_name.lower() for s in target_symbols)):
        score = 100
        reason = "explicitly specified in target_symbols"
    else:
        score, reason = score_symbol_relevance(sym_name, sym_info, task, mentioned_files, doc)

    if score <= 0:
        return None

    return {
        "id": full_sym_id,
        "name": sym_name,
        "file": rel_file,
        "type": sym_info["type"],
        "start_line": sym_info["start_line"],
        "end_line": sym_info["end_line"],
        "code": sym_info["code"],
        "lang": sym_info.get("lang", _get_file_language(rel_file)),
        "score": score,
        "reason": reason,
        "has_docstring_match": "semantic/docstring matches" in reason and bool(sym_info.get("docstring")),
        "lines_count": sym_info["end_line"] - sym_info["start_line"] + 1,
    }


def _filter_candidate_noise(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not candidates:
        return []
    max_score = candidates[0]["score"]
    if max_score < 40:
        return candidates
    return [
        c for c in candidates
        if c["score"] >= 35 or (c["score"] >= 0.4 * max_score) or c["score"] > 25
    ]


def _score_all_candidates(
    index: ProjectSymbolIndex,
    task: str,
    mentioned_files: Set[str],
    target_symbols: Optional[List[str]],
) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for full_sym_id, sym_info in index.all_symbols.items():
        doc = index.file_docstrings_map.get(sym_info["filename"])
        scored = _score_single_candidate(full_sym_id, sym_info, task, mentioned_files, doc, target_symbols)
        if scored is not None:
            candidates.append(scored)

    candidates.sort(
        key=lambda c: (
            c["score"],
            c.get("has_docstring_match", False),
            c["name"] not in GENERIC_IDENTIFIER_PARTS,
        ),
        reverse=True,
    )
    return _filter_candidate_noise(candidates)


def _try_strategy_override(strategy_override: str) -> Optional[Tuple[RetrievalStrategy, str]]:
    try:
        strategy = RetrievalStrategy(strategy_override)
        return strategy, f"Strategy explicitly requested via override: '{strategy.value}'."
    except ValueError:
        logger.debug("Unknown strategy override: %s", strategy_override)
        return None


def _decide_content_strategy(
    task: str,
    scored_candidates: List[Dict[str, Any]],
    file_lines_map: Dict[str, List[str]],
    explicit_file_rel: Optional[str],
) -> Tuple[RetrievalStrategy, str]:
    primary_file = explicit_file_rel if (explicit_file_rel and explicit_file_rel in file_lines_map) else scored_candidates[0]["file"]
    file_lines = len(file_lines_map.get(primary_file, []))
    file_content = "".join(file_lines_map.get(primary_file, []))
    file_tokens = estimate_tokens(file_content)

    top_files = {c["file"] for c in scored_candidates[:3] if c["score"] >= 35}
    is_compact = (file_lines <= SMALL_FILE_LINE_THRESHOLD or file_tokens <= SMALL_FILE_TOKEN_THRESHOLD)

    if is_compact and (explicit_file_rel is not None or len(top_files) <= 1):
        reason = (
            f"Target file '{primary_file}' is compact ({file_lines} lines, ~{file_tokens} tokens). "
            "Returning full file directly is cheaper than AST decomposition and preserves full lexical context."
        )
        return RetrievalStrategy.WHOLE_FILE, reason

    task_lower = task.lower()
    caller_keywords = {"decouple", "extract", "test", "caller", "used", "callers", "references", "impact"}
    if any(kw in task_lower for kw in caller_keywords):
        reason = (
            "Task involves cross-module refactoring, callers, or testing. "
            "Providing symbols, module context, dependencies, and external references."
        )
        return RetrievalStrategy.SYMBOLS_AND_CALLERS, reason

    reason = (
        "Task targets specific implementation. "
        "Providing symbol definitions, module context, and imported dependencies."
    )
    return RetrievalStrategy.SYMBOLS_AND_DEPENDENCIES, reason


def _select_retrieval_strategy(
    task: str,
    scored_candidates: List[Dict[str, Any]],
    file_lines_map: Dict[str, List[str]],
    strategy_override: Optional[str],
    explicit_file_rel: Optional[str],
) -> Tuple[RetrievalStrategy, str]:
    if strategy_override and strategy_override != "auto":
        override_res = _try_strategy_override(strategy_override)
        if override_res:
            return override_res

    if not scored_candidates:
        return (
            RetrievalStrategy.AST_SKELETON,
            "No specific candidate symbols matched task query. Providing high-density AST skeleton map.",
        )

    return _decide_content_strategy(task, scored_candidates, file_lines_map, explicit_file_rel)


def _pick_whole_file_target(
    explicit_file_rel: Optional[str],
    file_lines_map: Dict[str, List[str]],
    scored_candidates: List[Dict[str, Any]],
) -> str:
    if explicit_file_rel and explicit_file_rel in file_lines_map:
        return explicit_file_rel
    if scored_candidates:
        return scored_candidates[0]["file"]
    return next(iter(file_lines_map.keys()), "")


def _classify_whole_file_candidates(
    scored_candidates: List[Dict[str, Any]],
    target_file: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    selected_symbols: List[Dict[str, Any]] = []
    cross_file_helpers: List[Dict[str, Any]] = []
    ranking_decisions: List[Dict[str, Any]] = []

    for c in scored_candidates:
        c_copy = dict(c)
        if c_copy["file"] == target_file:
            c_copy["included"] = True
            selected_symbols.append(c_copy)
            ranking_decisions.append(c_copy)
        elif len(cross_file_helpers) < 2 and c_copy["score"] >= 35:
            c_copy["included"] = True
            selected_symbols.append(c_copy)
            cross_file_helpers.append(c_copy)
            ranking_decisions.append(c_copy)
        else:
            c_copy["included"] = False
            c_copy["reason"] = f"Located in different file ({c_copy['file']}) while whole-file strategy selected '{target_file}'"
            ranking_decisions.append(c_copy)

    return selected_symbols, cross_file_helpers, ranking_decisions


def _build_whole_file_context(
    target_file: str,
    file_lines_map: Dict[str, List[str]],
    file_uncertainties: Dict[str, List[UncertaintyFinding]],
    scored_candidates: List[Dict[str, Any]],
    strategy: RetrievalStrategy,
    strategy_reason: str,
) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]], List[UncertaintyFinding]]:
    raw_lines = file_lines_map.get(target_file, [])
    numbered_content = "".join(f"{i+1}: {line}" for i, line in enumerate(raw_lines))
    total_lines = len(raw_lines)

    selected_symbols, cross_file_helpers, ranking_decisions = _classify_whole_file_candidates(
        scored_candidates, target_file
    )
    uncertainty_findings = list(file_uncertainties.get(target_file, []))
    target_lang = _get_file_language(target_file)

    output = [
        f"# Adaptive Context Plan (Strategy: `{strategy.value}`)",
        f"> **Strategy Rationale:** {strategy_reason}",
        f"> 💡 **Devrith Fast-Path:** `{target_file}` is compact ({total_lines} lines, 1-indexed below). Edit directly with native tools; skip `view_file` and `decouple`.\n",
        f"## Full File Context: `{target_file}` ({total_lines} lines)",
        f"```{target_lang}",
        numbered_content.rstrip(),
        "```\n",
    ]

    if cross_file_helpers:
        output.append("### Relevant Cross-File Dependencies:")
        for h in cross_file_helpers:
            h_lang = h.get("lang", _get_file_language(h["file"]))
            output.append(f"#### `{h['name']}` ({h['file']}:{h['start_line']}-{h['end_line']})")
            output.append(f"```{h_lang}")
            output.append(h["code"].strip())
            output.append("```\n")

    if uncertainty_findings:
        output.append("### Uncertainty & Dynamic Reflection Notice:")
        for u in uncertainty_findings:
            icon = "⚠️" if u.confidence == ConfidenceLevel.UNKNOWN else "ℹ️"
            output.append(f"- {icon} Line {u.line}: `{u.code}` [{u.confidence.value}] - {u.description}")
    else:
        output.append("### Uncertainty Analysis:")
        output.append("- ✅ All symbols and dependencies are statically RESOLVED (100% confidence). No dynamic reflection detected.")

    return "\n".join(output), selected_symbols, ranking_decisions, uncertainty_findings


def _build_ast_skeleton_context(
    root_dir: str,
    token_budget: int,
    strategy: RetrievalStrategy,
    strategy_reason: str,
) -> str:
    digest_res = generate_digest(root_dir=root_dir, max_tokens=token_budget)
    return (
        f"# Adaptive Context Plan (Strategy: `{strategy.value}`)\n"
        f"> **Strategy Rationale:** {strategy_reason}\n\n"
        + digest_res["digest"]
    )


def _truncate_code_if_needed(code: str, max_lines: int = 120) -> Tuple[str, int]:
    code_lines = code.splitlines(keepends=True)
    if len(code_lines) > max_lines:
        truncated = "".join(code_lines[:max_lines]) + f"\n    # ... [Truncated: {len(code_lines) - max_lines} lines omitted to fit token budget] ...\n"
        return truncated, estimate_tokens(truncated)
    return code, estimate_tokens(code)


def _allocate_candidate_symbols(
    scored_candidates: List[Dict[str, Any]],
    token_budget: int,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    selected_symbols: List[Dict[str, Any]] = []
    ranking_decisions: List[Dict[str, Any]] = []
    budget_notices: List[str] = []

    base_overhead = 80 if token_budget <= 400 else 160
    accumulated_tokens = base_overhead
    max_symbols = 4 if token_budget <= 400 else 8
    max_score = scored_candidates[0]["score"] if scored_candidates else 0

    for i, orig_c in enumerate(scored_candidates):
        c = dict(orig_c)
        sym_tokens = estimate_tokens(c["code"])
        fits = (accumulated_tokens + sym_tokens <= token_budget)

        if len(selected_symbols) < max_symbols and fits:
            c["included"] = True
            selected_symbols.append(c)
            accumulated_tokens += sym_tokens
            ranking_decisions.append(c)
        elif (i == 0 or (c["score"] >= max_score and len(selected_symbols) < max_symbols)) and not fits and token_budget > 400:
            c["code"], sym_tokens = _truncate_code_if_needed(c["code"])
            c["included"] = True
            selected_symbols.append(c)
            accumulated_tokens += sym_tokens
            c["reason"] = f"{c['reason']} (included under budget floor)"
            ranking_decisions.append(c)
            budget_notices.append(f"Symbol '{c['name']}' included under budget floor (~{sym_tokens} tokens).")
        else:
            c["included"] = False
            c["reason"] = f"Excluded to stay within token budget ({token_budget} tokens; candidate requires ~{sym_tokens} tokens)"
            ranking_decisions.append(c)

    if not selected_symbols and scored_candidates:
        top_c = dict(scored_candidates[0])
        top_c["included"] = True
        selected_symbols.append(top_c)
        ranking_decisions[0] = top_c
        budget_notices.append(f"Primary symbol '{top_c['name']}' included under budget floor.")

    return selected_symbols, ranking_decisions, budget_notices


def _format_module_context(
    fpath: str,
    mod_doc: Optional[str],
    mod_consts: List[str],
    mod_imports: List[str],
) -> List[str]:
    lines: List[str] = []
    if mod_doc or mod_consts:
        lines.append("### Containing Module Context:")
        if mod_doc:
            lines.append(f'"""{mod_doc.strip()}"""\n')
        if mod_consts:
            lines.append(f"Top-level Constants: {', '.join(mod_consts[:8])}\n")

    if mod_imports:
        lines.append("### Resolved Static Imports:")
        lines.append(f"```{_get_file_language(fpath)}")
        lines.append("".join(mod_imports[:12]).strip())
        lines.append("```\n")
    return lines


def _is_self_reference(matching_sym: Optional[Dict[str, Any]], rel_path: str, line_num: int) -> bool:
    if not matching_sym:
        return False
    if matching_sym["file"] != rel_path:
        return False
    return matching_sym["start_line"] <= line_num <= matching_sym["end_line"]


def _collect_external_callers(
    all_source_files: List[Tuple[str, str]],
    selected_symbols: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    target_names = {s["name"] for s in selected_symbols}
    sym_by_name = {s["name"]: s for s in selected_symbols}
    refs_by_file: Dict[str, List[Dict[str, Any]]] = {}

    for rel_path, full_path in all_source_files:
        refs = find_references_in_file(full_path, target_names)
        filtered = [
            r for r in refs
            if not _is_self_reference(sym_by_name.get(r["symbol"]), rel_path, r["line_num"])
        ]
        if filtered:
            refs_by_file[rel_path] = filtered

    return refs_by_file


def _format_external_callers(
    refs_by_file: Dict[str, List[Dict[str, Any]]],
    output_lines: List[str],
    token_budget: int,
) -> Tuple[List[str], Optional[str]]:
    if not refs_by_file:
        return [], None

    callers_output = ["## External Callers & Test References:"]
    for rel_file, refs in refs_by_file.items():
        callers_output.append(f"- **{rel_file}**:")
        for r in refs[:4]:
            callers_output.append(f"  - Line {r['line_num']}: `{r['line']}`")

    callers_str = "\n".join(callers_output)
    caller_est = estimate_tokens(callers_str)
    current_est = estimate_tokens("\n".join(output_lines))

    if current_est + caller_est <= token_budget:
        return [callers_str], None

    notice = f"Truncated external test/caller references to stay under {token_budget} token limit."
    truncated_lines = [
        "## External Callers & Test References:",
        f"> **Budget Notice:** Callers truncated ({len(refs_by_file)} files reference target symbols). Increase `--budget` to view callers.",
    ]
    return truncated_lines, notice


def _format_uncertainty_section(
    selected_count: int,
    uncertainty_findings: List[UncertaintyFinding],
) -> List[str]:
    lines = ["\n### Uncertainty & Dynamic Reflection Analysis:"]
    if not uncertainty_findings:
        lines.append("- ✅ All symbol dependencies are statically RESOLVED (100% confidence). No dynamic reflection detected.")
        return lines

    pot_count = sum(1 for u in uncertainty_findings if u.confidence == ConfidenceLevel.POTENTIAL)
    unk_count = sum(1 for u in uncertainty_findings if u.confidence == ConfidenceLevel.UNKNOWN)
    lines.append(f"- Confidence Status: {selected_count} Resolved (Static AST), {pot_count} Potential, {unk_count} Unknown")

    for u in uncertainty_findings[:6]:
        icon = "⚠️" if u.confidence == ConfidenceLevel.UNKNOWN else "ℹ️"
        lines.append(f"- {icon} `{u.file}:{u.line}`: `{u.code}` [{u.confidence.value}] — {u.description}")

    if unk_count > 0:
        lines.append("> ⚠️ **Warning:** Dynamic reflection (getattr/importlib/eval) detected. Verify behavior with runtime test execution.")

    return lines


def _format_candidate_decision_log(ranking_decisions: List[Dict[str, Any]], token_budget: int) -> List[str]:
    output = ["## Candidate Selection & Decision Log:"]
    log_limit = 3 if token_budget <= 400 else 6
    for dec in ranking_decisions[:log_limit]:
        status = "Selected" if dec.get("included") else "Excluded"
        icon = "✅" if dec.get("included") else "❌"
        output.append(f"- {icon} **{status}**: `{dec['name']}` ({dec['file']}:{dec['start_line']}-{dec['end_line']}) — Score: {dec['score']} | {dec['reason']}")
    output.append("")
    return output


def _format_modules_section(
    strategy: RetrievalStrategy,
    selected_symbols: List[Dict[str, Any]],
    index: ProjectSymbolIndex,
) -> Tuple[List[str], List[UncertaintyFinding]]:
    lines: List[str] = []
    uncertainty_findings: List[UncertaintyFinding] = []
    files_involved = sorted(list({s["file"] for s in selected_symbols}))

    for fpath in files_involved:
        mod_doc = index.file_docstrings_map.get(fpath)
        mod_consts = index.file_constants_map.get(fpath, [])
        mod_imports = index.file_imports_map.get(fpath, [])
        uncerts = index.file_uncertainties.get(fpath, [])
        uncertainty_findings.extend(uncerts)

        lines.append(f"## Module: `{fpath}`")
        if strategy in (RetrievalStrategy.SYMBOLS_AND_DEPENDENCIES, RetrievalStrategy.SYMBOLS_AND_CALLERS):
            lines.extend(_format_module_context(fpath, mod_doc, mod_consts, mod_imports))

        syms_in_file = [s for s in selected_symbols if s["file"] == fpath]
        for s in syms_in_file:
            s_lang = s.get("lang", _get_file_language(s["file"]))
            lines.append(f"### Target Symbol: `{s['name']}` ({s['type']})")
            lines.append(f"Location: `{s['file']}:{s['start_line']}-{s['end_line']}`")
            lines.append(f"```{s_lang}")
            lines.append(s["code"].strip())
            lines.append("```\n")

    return lines, uncertainty_findings


def _build_symbol_centric_context(
    strategy: RetrievalStrategy,
    strategy_reason: str,
    scored_candidates: List[Dict[str, Any]],
    index: ProjectSymbolIndex,
    all_source_files: List[Tuple[str, str]],
    token_budget: int,
) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]], List[UncertaintyFinding]]:
    selected_symbols, ranking_decisions, budget_notices = _allocate_candidate_symbols(
        scored_candidates, token_budget
    )

    output = [
        f"# Adaptive Context Plan (Strategy: `{strategy.value}`)",
        f"> **Strategy Rationale:** {strategy_reason}\n",
    ]
    output.extend(_format_candidate_decision_log(ranking_decisions, token_budget))

    mod_lines, uncertainty_findings = _format_modules_section(strategy, selected_symbols, index)
    output.extend(mod_lines)

    if strategy == RetrievalStrategy.SYMBOLS_AND_CALLERS and selected_symbols:
        refs_by_file = _collect_external_callers(all_source_files, selected_symbols)
        caller_lines, notice = _format_external_callers(refs_by_file, output, token_budget)
        output.extend(caller_lines)
        if notice:
            budget_notices.append(notice)

    output.extend(_format_uncertainty_section(len(selected_symbols), uncertainty_findings))

    if budget_notices:
        output.append("\n> **Budget Notice:** " + " ".join(budget_notices))

    return "\n".join(output), selected_symbols, ranking_decisions, uncertainty_findings


def _calculate_token_breakdown(
    file_lines_map: Dict[str, List[str]],
    scored_candidates: List[Dict[str, Any]],
    final_tokens: int,
    token_budget: int,
) -> Dict[str, Any]:
    total_raw_tokens = sum(estimate_tokens("".join(lines)) for lines in file_lines_map.values())
    raw_target_file_tokens = 0
    if scored_candidates:
        involved_files = {c["file"] for c in scored_candidates[:3]}
        raw_target_file_tokens = sum(estimate_tokens("".join(file_lines_map.get(f, []))) for f in involved_files)

    return {
        "raw_codebase_total": total_raw_tokens,
        "raw_target_files": raw_target_file_tokens,
        "planned_context": final_tokens,
        "token_budget": token_budget,
        "savings_vs_target_files_pct": round(
            max(0, (1 - (final_tokens / max(1, raw_target_file_tokens)))) * 100, 1
        ) if raw_target_file_tokens else 0,
        "savings_vs_codebase_pct": round(
            max(0, (1 - (final_tokens / max(1, total_raw_tokens)))) * 100, 1
        ) if total_raw_tokens else 0,
    }


def plan_adaptive_retrieval(
    task: str,
    root_dir: str = ".",
    target_symbols: Optional[List[str]] = None,
    token_budget: int = 3000,
    strategy_override: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Adaptive Task-Aware Retrieval Engine:
    Dynamically balances cost vs fidelity by choosing between:
    - whole_file (for micro-files <120 lines or <350 tokens)
    - targeted_symbols (only the exact symbol definitions)
    - symbols_and_dependencies (symbol + containing module context + imports)
    - symbols_and_callers (symbol + imports + cross-file callers & tests)
    - ast_skeleton (repository-wide orientation when no symbol fits)
    
    Includes multi-candidate ranking with explicit inclusion/exclusion reasons,
    token budget enforcement with graceful truncation, and uncertainty-aware
    static analysis (Resolved vs Potential vs Unknown).
    """
    root_abs, explicit_file_rel, mentioned_files = _resolve_project_target(task, root_dir)
    all_source_files, index = _build_symbol_index(root_abs)
    scored_candidates = _score_all_candidates(index, task, mentioned_files, target_symbols)

    strategy, strategy_reason = _select_retrieval_strategy(
        task, scored_candidates, index.file_lines_map, strategy_override, explicit_file_rel
    )

    if strategy == RetrievalStrategy.WHOLE_FILE:
        target_file = _pick_whole_file_target(explicit_file_rel, index.file_lines_map, scored_candidates)
        context_str, selected_symbols, ranking_decisions, uncertainty_findings = _build_whole_file_context(
            target_file, index.file_lines_map, index.file_uncertainties, scored_candidates, strategy, strategy_reason
        )
    elif strategy == RetrievalStrategy.AST_SKELETON:
        context_str = _build_ast_skeleton_context(root_dir, token_budget, strategy, strategy_reason)
        selected_symbols, ranking_decisions, uncertainty_findings = [], [], []
    else:
        context_str, selected_symbols, ranking_decisions, uncertainty_findings = _build_symbol_centric_context(
            strategy, strategy_reason, scored_candidates, index, all_source_files, token_budget
        )

    final_tokens = estimate_tokens(context_str)
    token_breakdown = _calculate_token_breakdown(index.file_lines_map, scored_candidates, final_tokens, token_budget)

    return {
        "task": task,
        "strategy": strategy.value,
        "strategy_reason": strategy_reason,
        "recommendation": "BYPASS_DECOUPLING" if strategy == RetrievalStrategy.WHOLE_FILE else "PROCEED",
        "selected_symbols": [s["name"] for s in selected_symbols],
        "ranking": ranking_decisions,
        "uncertainty_report": {
            "findings_count": len(uncertainty_findings),
            "findings": [u.to_dict() for u in uncertainty_findings]
        },
        "token_breakdown": token_breakdown,
        "estimated_tokens": final_tokens,
        "context": context_str
    }
