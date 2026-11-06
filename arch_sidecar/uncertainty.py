import ast
import logging
import os
import re
from enum import Enum
from typing import Dict, List, Any, Optional, Set
from .utils import is_ignored_path

logger = logging.getLogger(__name__)


class ConfidenceLevel(str, Enum):
    RESOLVED = "Resolved"       # Static import/call verified directly in AST (100% confidence)
    POTENTIAL = "Potential"     # Literal dynamic call, convention or duck-type candidate
    UNKNOWN = "Unknown"         # Runtime variable reflection, eval, exec, dynamic import


class UncertaintyFinding:
    def __init__(
        self,
        file: str,
        line: int,
        confidence: ConfidenceLevel,
        pattern: str,
        code: str,
        description: str
    ):
        self.file = file
        self.line = line
        self.confidence = confidence
        self.pattern = pattern
        self.code = code
        self.description = description

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file": self.file,
            "line": self.line,
            "confidence": self.confidence.value,
            "pattern": self.pattern,
            "code": self.code,
            "description": self.description
        }


def _extract_static_str_arg(node: ast.Call, arg_idx: int) -> Optional[str]:
    """Return the string literal at arg_idx if present, else None."""
    if len(node.args) <= arg_idx:
        return None
    arg = node.args[arg_idx]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    return None


class UncertaintyScanner(ast.NodeVisitor):
    """
    Scans Python AST for dynamic reflection, runtime imports, and meta-programming
    that cannot be resolved with static certainty.
    """
    def __init__(self, filename: str, lines: List[str]):
        self.filename = filename
        self.lines = lines
        self.findings: List[UncertaintyFinding] = []
        self.has_dynamic_all: bool = False

    def _get_line_code(self, node: ast.AST) -> str:
        lineno = getattr(node, "lineno", 1)
        if 1 <= lineno <= len(self.lines):
            return self.lines[lineno - 1].strip()
        return ""

    def _add_finding(
        self,
        line: int,
        confidence: ConfidenceLevel,
        pattern: str,
        code: str,
        description: str,
    ) -> None:
        self.findings.append(UncertaintyFinding(
            file=self.filename,
            line=line,
            confidence=confidence,
            pattern=pattern,
            code=code,
            description=description,
        ))

    def _handle_getattr(self, node: ast.Call, code_snippet: str) -> None:
        if len(node.args) < 2:
            self._add_finding(
                node.lineno, ConfidenceLevel.UNKNOWN, "getattr_dynamic",
                code_snippet, "Dynamic getattr invocation"
            )
            return
        lit = _extract_static_str_arg(node, 1)
        if lit is not None:
            self._add_finding(
                node.lineno, ConfidenceLevel.POTENTIAL, "getattr_literal",
                code_snippet, f"Dynamic attribute access with static literal string '{lit}'"
            )
        else:
            self._add_finding(
                node.lineno, ConfidenceLevel.UNKNOWN, "getattr_dynamic",
                code_snippet, "Dynamic attribute access with runtime variable or expression"
            )

    def _handle_setattr(self, node: ast.Call, code_snippet: str) -> None:
        lit = _extract_static_str_arg(node, 1)
        if lit is not None:
            self._add_finding(
                node.lineno, ConfidenceLevel.POTENTIAL, "setattr_literal",
                code_snippet, f"Dynamic attribute mutation with static literal '{lit}'"
            )
        else:
            self._add_finding(
                node.lineno, ConfidenceLevel.UNKNOWN, "setattr_dynamic",
                code_snippet, "Dynamic attribute mutation with runtime variable or expression"
            )

    def _handle_hasattr(self, node: ast.Call, code_snippet: str) -> None:
        lit = _extract_static_str_arg(node, 1)
        if lit is not None:
            self._add_finding(
                node.lineno, ConfidenceLevel.POTENTIAL, "hasattr_literal",
                code_snippet, f"Runtime attribute introspection with static literal '{lit}'"
            )
        else:
            self._add_finding(
                node.lineno, ConfidenceLevel.UNKNOWN, "hasattr_dynamic",
                code_snippet, "Runtime attribute introspection with dynamic variable or expression"
            )

    def _handle_dynamic_import(self, node: ast.Call, code_snippet: str) -> None:
        lit = _extract_static_str_arg(node, 0)
        if lit is not None:
            self._add_finding(
                node.lineno, ConfidenceLevel.POTENTIAL, "dynamic_import_literal",
                code_snippet, f"Dynamic module import with static literal '{lit}'"
            )
        else:
            self._add_finding(
                node.lineno, ConfidenceLevel.UNKNOWN, "dynamic_import_variable",
                code_snippet, "Dynamic module import with runtime variable or expression"
            )

    def _resolve_call_name(self, node: ast.Call) -> Optional[str]:
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
        return None

    def visit_Call(self, node: ast.Call) -> None:
        func_name = self._resolve_call_name(node)
        code_snippet = self._get_line_code(node)
        handlers = {
            "getattr": self._handle_getattr,
            "setattr": self._handle_setattr,
            "hasattr": self._handle_hasattr,
            "import_module": self._handle_dynamic_import,
            "__import__": self._handle_dynamic_import,
        }
        handler = handlers.get(func_name or "")
        if handler is not None:
            handler(node, code_snippet)
        elif func_name in ("eval", "exec"):
            self._add_finding(
                node.lineno, ConfidenceLevel.UNKNOWN, f"{func_name}_call",
                code_snippet, f"Arbitrary code execution via {func_name}()"
            )
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        # Detect classes implementing dynamic attribute interceptors (__getattr__, __getattribute__)
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if item.name in ("__getattr__", "__getattribute__"):
                self._add_finding(
                    item.lineno,
                    ConfidenceLevel.POTENTIAL,
                    "metaprogramming_getattr",
                    self._get_line_code(item),
                    f"Class '{node.name}' implements dynamic attribute trap '{item.name}'",
                )
        self.generic_visit(node)


def scan_file_uncertainty(filepath: str, rel_path: Optional[str] = None) -> List[UncertaintyFinding]:
    """Scans a single Python file for dynamic reflection and uncertainty patterns."""
    rel = rel_path or filepath
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
            lines = content.splitlines(keepends=True)
        tree = ast.parse(content, filename=filepath)
        scanner = UncertaintyScanner(rel, lines)
        scanner.visit(tree)
        return scanner.findings
    except (OSError, SyntaxError, ValueError, TypeError) as exc:
        logger.debug("Skipping uncertainty scan for %s: %s", rel, exc)
        return []


def _iter_python_files(root_abs: str) -> List[tuple]:
    """Collect (full_path, rel_path) for non-ignored Python files under root_abs."""
    py_files = []
    for root, dirs, files in os.walk(root_abs):
        dirs[:] = [
            d for d in dirs
            if not is_ignored_path(os.path.relpath(os.path.join(root, d), root_abs))
        ]
        for f in files:
            if not f.endswith(".py"):
                continue
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, root_abs)
            if not is_ignored_path(rel_path):
                py_files.append((full_path, rel_path))
    return py_files


def scan_codebase_uncertainty(root_dir: str = ".") -> Dict[str, Any]:
    """Scans all Python files in root_dir (or a single Python file) for dynamic uncertainty patterns."""
    root_abs = os.path.abspath(root_dir)
    all_findings: List[UncertaintyFinding] = []

    if os.path.isfile(root_abs):
        if root_abs.endswith(".py"):
            all_findings.extend(scan_file_uncertainty(root_abs, os.path.basename(root_abs)))
    else:
        for full_path, rel_path in _iter_python_files(root_abs):
            all_findings.extend(scan_file_uncertainty(full_path, rel_path))

    potential_count = sum(1 for f in all_findings if f.confidence == ConfidenceLevel.POTENTIAL)
    unknown_count = sum(1 for f in all_findings if f.confidence == ConfidenceLevel.UNKNOWN)

    return {
        "total_uncertainties": len(all_findings),
        "potential_count": potential_count,
        "unknown_count": unknown_count,
        "findings": [f.to_dict() for f in all_findings]
    }
