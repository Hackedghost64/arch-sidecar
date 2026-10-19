import os
import ast
import re
from enum import Enum
from typing import Dict, List, Optional, Set, Any, Tuple

from .utils import is_ignored_path, estimate_tokens
from .digest import generate_digest
from .context import SymbolFinder, find_references_in_file
from .uncertainty import (
    ConfidenceLevel,
    UncertaintyFinding,
    scan_file_uncertainty,
    UncertaintyScanner
)

class RetrievalStrategy(str, Enum):
    WHOLE_FILE = "whole_file"
    TARGETED_SYMBOLS = "targeted_symbols"
    SYMBOLS_AND_DEPENDENCIES = "symbols_and_dependencies"
    SYMBOLS_AND_CALLERS = "symbols_and_callers"
    AST_SKELETON = "ast_skeleton"

SMALL_FILE_LINE_THRESHOLD = 120
SMALL_FILE_TOKEN_THRESHOLD = 350

def score_symbol_relevance(
    symbol_name: str,
    sym_info: Dict[str, Any],
    task: str,
    mentioned_files: Set[str],
    docstring: Optional[str] = None
) -> Tuple[int, str]:
    """Scores symbol relevance to the task query and returns (score, reason)."""
    score = 0
    reasons = []

    task_lower = task.lower()
    sym_lower = symbol_name.lower()

    # 1. Exact match in task
    if re.search(rf"\b{re.escape(symbol_name)}\b", task):
        score += 60
        reasons.append(f"exact name match '{symbol_name}' in task")
    elif sym_lower in task_lower:
        score += 35
        reasons.append(f"case-insensitive name match '{sym_lower}'")

    # 2. Substring/token overlap (split by snake_case or CamelCase)
    sym_parts = [p.lower() for p in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|\d|\W|$)|\d+", symbol_name) if len(p) > 2]
    matched_parts = [p for p in sym_parts if p in task_lower]
    if matched_parts and score == 0:
        score += len(matched_parts) * 15
        reasons.append(f"matched keywords: {', '.join(matched_parts)}")

    # 3. File match (symbol resides in a file explicitly mentioned in task)
    rel_file = sym_info.get("filename", "")
    file_base = os.path.basename(rel_file)
    if file_base in mentioned_files or rel_file in task:
        score += 25
        reasons.append(f"located in targeted file '{file_base}'")

    # 4. Docstring relevance
    if docstring:
        doc_lower = docstring.lower()
        task_words = [w.lower() for w in re.findall(r"\b\w{4,}\b", task) if w.lower() not in {"extract", "decouple", "refactor", "module", "class", "function"}]
        doc_matches = [w for w in task_words if w in doc_lower]
        if doc_matches:
            score += min(len(doc_matches) * 5, 20)
            reasons.append(f"docstring matches: {', '.join(doc_matches[:3])}")

    reason_str = "; ".join(reasons) if reasons else "no direct task match"
    return score, reason_str

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
    root_abs = os.path.abspath(root_dir)

    # 1. Parse mentioned files and explicit symbol names from task
    mentioned_files = set(re.findall(r"\b[\w\-./]+\.(?:py|js|ts|json)\b", task))
    
    # 2. Walk project and index Python symbols
    all_python_files: List[Tuple[str, str]] = []
    file_lines_map: Dict[str, List[str]] = {}
    file_docstrings_map: Dict[str, Optional[str]] = {}
    file_constants_map: Dict[str, List[str]] = {}
    file_imports_map: Dict[str, List[str]] = {}
    all_symbols: Dict[str, Dict[str, Any]] = {}
    file_uncertainties: Dict[str, List[UncertaintyFinding]] = {}

    for root, dirs, files in os.walk(root_abs):
        dirs[:] = [
            d for d in dirs 
            if not is_ignored_path(os.path.relpath(os.path.join(root, d), root_abs))
        ]
        for f in files:
            if f.endswith(".py"):
                full_path = os.path.join(root, f)
                rel_path = os.path.relpath(full_path, root_abs)
                if is_ignored_path(rel_path):
                    continue
                all_python_files.append((rel_path, full_path))

                try:
                    with open(full_path, "r", encoding="utf-8", errors="ignore") as file_obj:
                        lines = file_obj.readlines()
                    file_lines_map[rel_path] = lines
                    tree = ast.parse("".join(lines), filename=full_path)
                    
                    finder = SymbolFinder(rel_path, lines)
                    finder.visit(tree)
                    file_imports_map[rel_path] = finder.imports
                    file_docstrings_map[rel_path] = ast.get_docstring(tree)
                    file_constants_map[rel_path] = finder.constants

                    for sym_name, sym_info in finder.symbols.items():
                        all_symbols[f"{rel_path}:{sym_name}"] = sym_info

                    # Uncertainty scan
                    scanner = UncertaintyScanner(rel_path, lines)
                    scanner.visit(tree)
                    if scanner.findings:
                        file_uncertainties[rel_path] = scanner.findings
                except Exception:
                    continue

    # 3. Score and rank candidates
    scored_candidates = []
    for full_sym_id, sym_info in all_symbols.items():
        sym_name = sym_info["name"]
        rel_file = sym_info["filename"]
        doc = file_docstrings_map.get(rel_file)

        # If user explicitly passed target_symbols, force match
        if target_symbols and (sym_name in target_symbols or any(s.lower() == sym_name.lower() for s in target_symbols)):
            score = 100
            reason = f"explicitly specified in target_symbols"
        else:
            score, reason = score_symbol_relevance(sym_name, sym_info, task, mentioned_files, doc)

        if score > 0:
            scored_candidates.append({
                "id": full_sym_id,
                "name": sym_name,
                "file": rel_file,
                "type": sym_info["type"],
                "start_line": sym_info["start_line"],
                "end_line": sym_info["end_line"],
                "code": sym_info["code"],
                "score": score,
                "reason": reason,
                "lines_count": sym_info["end_line"] - sym_info["start_line"] + 1
            })

    # Sort descending by score
    scored_candidates.sort(key=lambda c: c["score"], reverse=True)

    # Filter noise: if strong name matches exist (score >= 40), drop candidates that only have a generic file match (score <= 25)
    if scored_candidates:
        max_score = scored_candidates[0]["score"]
        if max_score >= 40:
            scored_candidates = [c for c in scored_candidates if c["score"] >= 35 or (c["score"] >= 0.5 * max_score)]

    # 4. Determine Strategy
    strategy = RetrievalStrategy.SYMBOLS_AND_DEPENDENCIES
    strategy_reason = ""

    if strategy_override and strategy_override != "auto":
        try:
            strategy = RetrievalStrategy(strategy_override)
            strategy_reason = f"Strategy explicitly requested via override: '{strategy.value}'."
        except ValueError:
            pass

    if not strategy_reason:
        if not scored_candidates:
            strategy = RetrievalStrategy.AST_SKELETON
            strategy_reason = "No specific candidate symbols matched task query. Providing high-density AST skeleton map."
        else:
            # Check if all top candidates belong to a single micro-file
            primary_file = scored_candidates[0]["file"]
            file_lines = len(file_lines_map.get(primary_file, []))
            file_content = "".join(file_lines_map.get(primary_file, []))
            file_tokens = estimate_tokens(file_content)

            if file_lines <= SMALL_FILE_LINE_THRESHOLD or file_tokens <= SMALL_FILE_TOKEN_THRESHOLD:
                strategy = RetrievalStrategy.WHOLE_FILE
                strategy_reason = (
                    f"Target file '{primary_file}' is compact ({file_lines} lines, ~{file_tokens} tokens). "
                    "Returning full file directly is cheaper than AST decomposition and preserves full lexical context."
                )
            else:
                task_lower = task.lower()
                caller_keywords = {"decouple", "extract", "test", "caller", "used", "callers", "references", "impact"}
                if any(kw in task_lower for kw in caller_keywords):
                    strategy = RetrievalStrategy.SYMBOLS_AND_CALLERS
                    strategy_reason = (
                        "Task involves cross-module refactoring, callers, or testing. "
                        "Providing symbols, module context, dependencies, and external references."
                    )
                else:
                    strategy = RetrievalStrategy.SYMBOLS_AND_DEPENDENCIES
                    strategy_reason = (
                        "Task targets specific implementation. "
                        "Providing symbol definitions, module context, and imported dependencies."
                    )

    # 5. Build Context and Enforce Budget
    ranking_decisions: List[Dict[str, Any]] = []
    budget_notices: List[str] = []
    uncertainty_findings: List[UncertaintyFinding] = []
    selected_symbols: List[Dict[str, Any]] = []

    # Calculate token benchmarks for comparison breakdown
    total_raw_tokens = sum(estimate_tokens("".join(lines)) for lines in file_lines_map.values())
    raw_target_file_tokens = 0
    if scored_candidates:
        involved_files = {c["file"] for c in scored_candidates[:3]}
        raw_target_file_tokens = sum(estimate_tokens("".join(file_lines_map.get(f, []))) for f in involved_files)

    context_str = ""

    # Strategy: WHOLE_FILE
    if strategy == RetrievalStrategy.WHOLE_FILE:
        target_file = scored_candidates[0]["file"] if scored_candidates else list(file_lines_map.keys())[0]
        content = "".join(file_lines_map.get(target_file, []))
        total_lines = len(file_lines_map.get(target_file, []))
        
        # Include top candidates in ranking
        for c in scored_candidates:
            if c["file"] == target_file:
                c["included"] = True
                selected_symbols.append(c)
                ranking_decisions.append(c)
            else:
                c["included"] = False
                c["reason"] = f"Located in different file ({c['file']}) while whole-file strategy selected '{target_file}'"
                ranking_decisions.append(c)

        # Uncertainties in target file
        uncerts = file_uncertainties.get(target_file, [])
        uncertainty_findings.extend(uncerts)

        output = [
            f"# Adaptive Context Plan (Strategy: `{strategy.value}`)",
            f"> **Strategy Rationale:** {strategy_reason}",
            f"> 💡 **Devrith Recommendation:** Target file is compact ({total_lines} lines). Direct file modification has lower complexity than AST slicing. Do not run automated decoupling.\n",
            f"## Full File Context: `{target_file}` ({total_lines} lines)",
            "```python",
            content.strip(),
            "```\n"
        ]

        if uncertainty_findings:
            output.append("### Uncertainty & Dynamic Reflection Notice:")
            for u in uncertainty_findings:
                icon = "⚠️" if u.confidence == ConfidenceLevel.UNKNOWN else "ℹ️"
                output.append(f"- {icon} Line {u.line}: `{u.code}` [{u.confidence.value}] - {u.description}")
        else:
            output.append("### Uncertainty Analysis:")
            output.append("- ✅ All symbols and dependencies are statically RESOLVED (100% confidence). No dynamic reflection detected.")

        context_str = "\n".join(output)

    # Strategy: AST_SKELETON
    elif strategy == RetrievalStrategy.AST_SKELETON:
        digest_res = generate_digest(root_dir=root_dir, max_tokens=token_budget)
        context_str = (
            f"# Adaptive Context Plan (Strategy: `{strategy.value}`)\n"
            f"> **Strategy Rationale:** {strategy_reason}\n\n"
            + digest_res["digest"]
        )

    # Strategy: TARGETED_SYMBOLS, SYMBOLS_AND_DEPENDENCIES, SYMBOLS_AND_CALLERS
    else:
        # Base overhead budget: ~160 tokens (or ~70 tokens on tight budget <400)
        base_overhead = 80 if token_budget <= 400 else 160
        accumulated_tokens = base_overhead
        
        # First pass: Allocate symbols that fit (cap at top 8 symbols)
        max_symbols = 4 if token_budget <= 400 else 8
        for c in scored_candidates:
            sym_tokens = estimate_tokens(c["code"])
            if len(selected_symbols) < max_symbols and (accumulated_tokens + sym_tokens <= token_budget):
                c["included"] = True
                selected_symbols.append(c)
                accumulated_tokens += sym_tokens
                ranking_decisions.append(c)
            else:
                c["included"] = False
                c["reason"] = f"Excluded to stay within token budget ({token_budget} tokens; candidate requires ~{sym_tokens} tokens)"
                ranking_decisions.append(c)

        if not selected_symbols and scored_candidates:
            # If even the first candidate exceeds budget, include at least the top one
            top_c = scored_candidates[0]
            top_c["included"] = True
            selected_symbols.append(top_c)
            ranking_decisions[0] = top_c
            budget_notices.append(f"Primary symbol '{top_c['name']}' included under budget floor.")

        # Second pass: Assemble context components
        output = [
            f"# Adaptive Context Plan (Strategy: `{strategy.value}`)",
            f"> **Strategy Rationale:** {strategy_reason}\n",
        ]
        log_limit = 3 if token_budget <= 400 else 6
        output.append("## Candidate Selection & Decision Log:")
        for dec in ranking_decisions[:log_limit]:
            status = "Selected" if dec.get("included") else "Excluded"
            icon = "✅" if dec.get("included") else "❌"
            output.append(f"- {icon} **{status}**: `{dec['name']}` ({dec['file']}:{dec['start_line']}-{dec['end_line']}) — Score: {dec['score']} | {dec['reason']}")

        output.append("")

        # Group selected symbols by file
        files_involved = sorted(list({s["file"] for s in selected_symbols}))
        for fpath in files_involved:
            mod_doc = file_docstrings_map.get(fpath)
            mod_consts = file_constants_map.get(fpath, [])
            mod_imports = file_imports_map.get(fpath, [])
            uncerts = file_uncertainties.get(fpath, [])
            uncertainty_findings.extend(uncerts)

            syms_in_file = [s for s in selected_symbols if s["file"] == fpath]

            output.append(f"## Module: `{fpath}`")

            # Add module context if strategy >= dependencies
            if strategy in (RetrievalStrategy.SYMBOLS_AND_DEPENDENCIES, RetrievalStrategy.SYMBOLS_AND_CALLERS):
                if mod_doc or mod_consts:
                    output.append("### Containing Module Context:")
                    if mod_doc:
                        output.append(f'"""{mod_doc.strip()}"""\n')
                    if mod_consts:
                        output.append(f"Top-level Constants: {', '.join(mod_consts[:8])}\n")

                if mod_imports:
                    output.append("### Resolved Static Imports:")
                    output.append("```python")
                    output.append("".join(mod_imports[:12]).strip())
                    output.append("```\n")

            # Definitions
            for s in syms_in_file:
                output.append(f"### Target Symbol: `{s['name']}` ({s['type']})")
                output.append(f"Location: `{s['file']}:{s['start_line']}-{s['end_line']}`")
                output.append("```python")
                output.append(s["code"].strip())
                output.append("```\n")

        # External Callers & References (if strategy == SYMBOLS_AND_CALLERS)
        if strategy == RetrievalStrategy.SYMBOLS_AND_CALLERS and selected_symbols:
            target_names = {s["name"] for s in selected_symbols}
            refs_by_file: Dict[str, List[Dict[str, Any]]] = {}

            for rel_path, full_path in all_python_files:
                refs = find_references_in_file(full_path, target_names)
                filtered = []
                for r in refs:
                    matching_sym = next((s for s in selected_symbols if s["name"] == r["symbol"]), None)
                    if matching_sym and matching_sym["file"] == rel_path and matching_sym["start_line"] <= r["line_num"] <= matching_sym["end_line"]:
                        continue
                    filtered.append(r)
                if filtered:
                    refs_by_file[rel_path] = filtered

            if refs_by_file:
                callers_output = ["## External Callers & Test References:"]
                caller_tokens = 0
                for rel_file, refs in refs_by_file.items():
                    callers_output.append(f"- **{rel_file}**:")
                    for r in refs[:4]:
                        callers_output.append(f"  - Line {r['line_num']}: `{r['line']}`")

                callers_str = "\n".join(callers_output)
                caller_est = estimate_tokens(callers_str)

                # Budget check for callers
                current_est = estimate_tokens("\n".join(output))
                if current_est + caller_est <= token_budget:
                    output.append(callers_str)
                else:
                    budget_notices.append(f"Truncated external test/caller references to stay under {token_budget} token limit.")
                    output.append("## External Callers & Test References:")
                    output.append(f"> **Budget Notice:** Callers truncated ({len(refs_by_file)} files reference target symbols). Increase `--budget` to view callers.")

        # Uncertainty analysis section
        output.append("\n### Uncertainty & Dynamic Reflection Analysis:")
        if uncertainty_findings:
            pot_count = sum(1 for u in uncertainty_findings if u.confidence == ConfidenceLevel.POTENTIAL)
            unk_count = sum(1 for u in uncertainty_findings if u.confidence == ConfidenceLevel.UNKNOWN)
            output.append(f"- Confidence Status: {len(selected_symbols)} Resolved (Static AST), {pot_count} Potential, {unk_count} Unknown")
            for u in uncertainty_findings[:6]:
                icon = "⚠️" if u.confidence == ConfidenceLevel.UNKNOWN else "ℹ️"
                output.append(f"- {icon} `{u.file}:{u.line}`: `{u.code}` [{u.confidence.value}] — {u.description}")
            if unk_count > 0:
                output.append("> ⚠️ **Warning:** Dynamic reflection (getattr/importlib/eval) detected. Verify behavior with runtime test execution.")
        else:
            output.append("- ✅ All symbol dependencies are statically RESOLVED (100% confidence). No dynamic reflection detected.")

        if budget_notices:
            output.append("\n> **Budget Notice:** " + " ".join(budget_notices))

        context_str = "\n".join(output)

    final_tokens = estimate_tokens(context_str)

    # Cost breakdown and savings
    token_breakdown = {
        "raw_codebase_total": total_raw_tokens,
        "raw_target_files": raw_target_file_tokens,
        "planned_context": final_tokens,
        "token_budget": token_budget,
        "savings_vs_target_files_pct": round(
            max(0, (1 - (final_tokens / max(1, raw_target_file_tokens)))) * 100, 1
        ) if raw_target_file_tokens else 0,
        "savings_vs_codebase_pct": round(
            max(0, (1 - (final_tokens / max(1, total_raw_tokens)))) * 100, 1
        ) if total_raw_tokens else 0
    }

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
