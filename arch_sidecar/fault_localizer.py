import ast
import json
import logging
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TRACEBACK_LINE_RE = re.compile(r"^([^\s:]+\.py):(\d+):\s+in\s+([A-Za-z_][A-Za-z0-9_]*)")
ASSERT_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _extract_params_from_ast(func_code: str, func_name: str) -> Optional[List[str]]:
    try:
        tree = ast.parse(func_code)
    except SyntaxError as exc:
        logger.debug("SyntaxError parsing function params for %s: %s", func_name, exc)
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return [arg.arg for arg in node.args.args if arg.arg not in ("self", "cls")]
    return None


def extract_function_params(func_code: str, func_name: str) -> List[str]:
    """Extract ordered parameter names from a Python function definition."""
    ast_params = _extract_params_from_ast(func_code, func_name)
    if ast_params is not None:
        return ast_params
    match = re.search(rf"def\s+{re.escape(func_name)}\s*\(([^)]*)\)", func_code)
    if not match:
        return []
    raw_names = [part.split(":")[0].split("=")[0].strip() for part in match.group(1).split(",")]
    return [name for name in raw_names if name and name not in ("self", "cls")]


def canonicalize_ast_line(line: str, param_names: List[str]) -> str:
    """Replace function parameter identifiers with positional tokens ($ARG0, $ARG1, ...)."""
    canonical = line.strip()
    indexed = sorted(enumerate(param_names), key=lambda item: -len(item[1]))
    for idx, pname in indexed:
        canonical = re.sub(rf"\b{re.escape(pname)}\b", f"$ARG{idx}", canonical)
    return canonical


def decanonicalize_ast_line(canonical_line: str, param_names: List[str]) -> str:
    """Restore positional tokens ($ARG0, $ARG1, ...) to concrete function parameter names."""
    restored = canonical_line.strip()
    for idx, pname in enumerate(param_names):
        restored = restored.replace(f"$ARG{idx}", pname)
    return restored


def _default_cache_path() -> str:
    base_dir = os.environ.get("DEVRITH_HOME", os.path.expanduser("~/.devrith"))
    return os.path.join(base_dir, "ast_repair_cache.json")


class ASTCanonicalRepairCache:
    """
    Deterministic O(1) AST canonical repair table.
    Maps canonicalized buggy AST lines ($ARG0, $ARG1) to verified canonical fixes.
    """

    def __init__(self, cache_path: Optional[str] = None):
        self.cache_path = cache_path or _default_cache_path()
        self.rules: Dict[str, Dict[str, Any]] = self._load()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if not os.path.isfile(self.cache_path):
            return {}
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("Failed loading AST repair cache %s: %s", self.cache_path, exc)
            return {}

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(self.rules, f, indent=2, sort_keys=True)
        except OSError as exc:
            logger.debug("Failed saving AST repair cache %s: %s", self.cache_path, exc)

    def record_repair(
        self,
        func_code: str,
        func_name: str,
        buggy_line: str,
        fixed_line: str,
    ) -> Dict[str, str]:
        """Canonicalize a verified repair and persist it to the O(1) repair table."""
        params = extract_function_params(func_code, func_name)
        canon_bug = canonicalize_ast_line(buggy_line, params)
        canon_fix = canonicalize_ast_line(fixed_line, params)
        entry = {
            "canonical_bug": canon_bug,
            "canonical_fix": canon_fix,
            "source_function": func_name,
        }
        self.rules[canon_bug] = entry
        self._save()
        return entry

    def lookup_repair(
        self,
        func_code: str,
        func_name: str,
        start_line: int = 1,
    ) -> Optional[Dict[str, Any]]:
        """Scan a function's lines for a known canonical bug pattern and synthesize the fix."""
        if not self.rules:
            return None
        params = extract_function_params(func_code, func_name)
        for offset, raw_line in enumerate(func_code.splitlines()):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith(("#", "def ", "async def ")):
                continue
            canon = canonicalize_ast_line(stripped, params)
            matched = self.rules.get(canon)
            if matched and matched.get("canonical_fix") != canon:
                indent = raw_line[: len(raw_line) - len(raw_line.lstrip())]
                concrete_fix = decanonicalize_ast_line(matched["canonical_fix"], params)
                return {
                    "function": func_name,
                    "line_number": start_line + offset,
                    "buggy_line": stripped,
                    "suggested_line": f"{indent}{concrete_fix}",
                    "canonical_bug": canon,
                    "canonical_fix": matched["canonical_fix"],
                }
        return None


def _is_non_test_py_file(rel_path: str) -> bool:
    norm = rel_path.replace("\\", "/")
    base = os.path.basename(norm)
    if base.startswith("test_") or base.endswith("_test.py"):
        return False
    return "/tests/" not in f"/{norm}"


def _match_symbol_at_line(
    analyzer: Any, line_no: int, func_hint: str
) -> Optional[str]:
    for sym_name, sym_info in analyzer.symbols.items():
        if sym_info["start_line"] <= line_no <= sym_info["end_line"]:
            return sym_name
    if func_hint in analyzer.symbols:
        return func_hint
    return None


def _extract_traceback_frame_hits(
    pytest_output: str,
    file_analyzers: Dict[str, Any],
) -> List[Tuple[str, str, int]]:
    hits: List[Tuple[str, str, int]] = []
    for raw_line in pytest_output.splitlines():
        match = TRACEBACK_LINE_RE.match(raw_line.strip())
        if not match:
            continue
        rel_f, line_str, func_name = match.group(1), match.group(2), match.group(3)
        if not _is_non_test_py_file(rel_f) or rel_f not in file_analyzers:
            continue
        line_no = int(line_str)
        sym = _match_symbol_at_line(file_analyzers[rel_f], line_no, func_name)
        if sym:
            hits.append((rel_f, sym, line_no))
    return hits


IGNORED_ASSERT_CALLS = frozenset({"assert", "pytest", "approx", "len", "round", "int", "float"})


def _find_call_in_analyzers(
    call_name: str,
    file_analyzers: Dict[str, Any],
) -> List[Tuple[str, str, int]]:
    if call_name in IGNORED_ASSERT_CALLS:
        return []
    matched: List[Tuple[str, str, int]] = []
    for rel_f, analyzer in file_analyzers.items():
        if _is_non_test_py_file(rel_f) and call_name in analyzer.symbols:
            matched.append((rel_f, call_name, analyzer.symbols[call_name]["start_line"]))
    return matched


def _extract_assertion_call_hits(
    pytest_output: str,
    file_analyzers: Dict[str, Any],
) -> List[Tuple[str, str, int]]:
    hits: List[Tuple[str, str, int]] = []
    for raw_line in pytest_output.splitlines():
        stripped = raw_line.strip()
        if not stripped.startswith((">", "E")):
            continue
        for call_name in ASSERT_CALL_RE.findall(stripped):
            hits.extend(_find_call_in_analyzers(call_name, file_analyzers))
    return hits


def parse_pytest_traceback_targets(
    pytest_output: str,
    file_analyzers: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Parse pytest failure output and map stack frames or failed assertion calls
    back to concrete non-test AST symbols in `file_analyzers`.
    """
    combined = _extract_traceback_frame_hits(pytest_output, file_analyzers)
    combined.extend(_extract_assertion_call_hits(pytest_output, file_analyzers))
    results: List[Dict[str, Any]] = []
    seen: set = set()
    for rel_f, sym_name, line_no in combined:
        key = (rel_f, sym_name)
        if key in seen:
            continue
        seen.add(key)
        results.append({
            "file": rel_f,
            "symbol": sym_name,
            "line": line_no,
        })
    return results


def localize_pytest_faults(
    root_abs: str,
    file_analyzers: Dict[str, Any],
    timeout_sec: int = 8,
) -> List[Dict[str, Any]]:
    """
    Execute pytest with short tracebacks to localize failing source symbols
    when static lexical scoring is ambiguous on bugfix tasks.
    """
    cmd = [sys.executable, "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider"]
    try:
        proc = subprocess.run(
            cmd,
            cwd=root_abs,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        if proc.returncode == 0:
            return []
        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        return parse_pytest_traceback_targets(output, file_analyzers)
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug("Pytest fault localization skipped in %s: %s", root_abs, exc)
        return []
