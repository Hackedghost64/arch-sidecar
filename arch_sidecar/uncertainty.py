import ast
import os
import re
from enum import Enum
from typing import Dict, List, Any, Optional, Set
from .utils import is_ignored_path

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

    def visit_Call(self, node: ast.Call):
        func_name = None
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr

        code_snippet = self._get_line_code(node)

        # 1. getattr() checks
        if func_name == "getattr":
            if len(node.args) >= 2:
                attr_arg = node.args[1]
                if isinstance(attr_arg, ast.Constant) and isinstance(attr_arg.value, str):
                    self.findings.append(UncertaintyFinding(
                        file=self.filename,
                        line=node.lineno,
                        confidence=ConfidenceLevel.POTENTIAL,
                        pattern="getattr_literal",
                        code=code_snippet,
                        description=f"Dynamic attribute access with static literal string '{attr_arg.value}'"
                    ))
                else:
                    self.findings.append(UncertaintyFinding(
                        file=self.filename,
                        line=node.lineno,
                        confidence=ConfidenceLevel.UNKNOWN,
                        pattern="getattr_dynamic",
                        code=code_snippet,
                        description="Dynamic attribute access with runtime variable or expression"
                    ))
            else:
                self.findings.append(UncertaintyFinding(
                    file=self.filename,
                    line=node.lineno,
                    confidence=ConfidenceLevel.UNKNOWN,
                    pattern="getattr_dynamic",
                    code=code_snippet,
                    description="Dynamic getattr invocation"
                ))

        # 2. setattr() checks
        elif func_name == "setattr":
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                self.findings.append(UncertaintyFinding(
                    file=self.filename,
                    line=node.lineno,
                    confidence=ConfidenceLevel.POTENTIAL,
                    pattern="setattr_literal",
                    code=code_snippet,
                    description=f"Dynamic attribute mutation with static literal '{node.args[1].value}'"
                ))
            else:
                self.findings.append(UncertaintyFinding(
                    file=self.filename,
                    line=node.lineno,
                    confidence=ConfidenceLevel.UNKNOWN,
                    pattern="setattr_dynamic",
                    code=code_snippet,
                    description="Dynamic attribute mutation with runtime variable or expression"
                ))

        # 3. hasattr() checks
        elif func_name == "hasattr":
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                self.findings.append(UncertaintyFinding(
                    file=self.filename,
                    line=node.lineno,
                    confidence=ConfidenceLevel.POTENTIAL,
                    pattern="hasattr_literal",
                    code=code_snippet,
                    description=f"Runtime attribute introspection with static literal '{node.args[1].value}'"
                ))
            else:
                self.findings.append(UncertaintyFinding(
                    file=self.filename,
                    line=node.lineno,
                    confidence=ConfidenceLevel.UNKNOWN,
                    pattern="hasattr_dynamic",
                    code=code_snippet,
                    description="Runtime attribute introspection with dynamic variable or expression"
                ))

        # 4. importlib.import_module or __import__
        elif (isinstance(node.func, ast.Attribute) and node.func.attr == "import_module") or func_name == "__import__":
            if len(node.args) >= 1 and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                self.findings.append(UncertaintyFinding(
                    file=self.filename,
                    line=node.lineno,
                    confidence=ConfidenceLevel.POTENTIAL,
                    pattern="dynamic_import_literal",
                    code=code_snippet,
                    description=f"Dynamic module import with static literal '{node.args[0].value}'"
                ))
            else:
                self.findings.append(UncertaintyFinding(
                    file=self.filename,
                    line=node.lineno,
                    confidence=ConfidenceLevel.UNKNOWN,
                    pattern="dynamic_import_variable",
                    code=code_snippet,
                    description="Dynamic module import with runtime variable or expression"
                ))

        # 5. eval() and exec()
        elif func_name in ("eval", "exec"):
            self.findings.append(UncertaintyFinding(
                file=self.filename,
                line=node.lineno,
                confidence=ConfidenceLevel.UNKNOWN,
                pattern=f"{func_name}_call",
                code=code_snippet,
                description=f"Arbitrary code execution via {func_name}()"
            ))

        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef):
        # Detect classes implementing dynamic attribute interceptors (__getattr__, __getattribute__)
        code_snippet = self._get_line_code(node)
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if item.name in ("__getattr__", "__getattribute__"):
                    self.findings.append(UncertaintyFinding(
                        file=self.filename,
                        line=item.lineno,
                        confidence=ConfidenceLevel.POTENTIAL,
                        pattern="metaprogramming_getattr",
                        code=self._get_line_code(item),
                        description=f"Class '{node.name}' implements dynamic attribute trap '{item.name}'"
                    ))
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
    except Exception:
        return []

def scan_codebase_uncertainty(root_dir: str = ".") -> Dict[str, Any]:
    """Scans all Python files in root_dir for dynamic uncertainty patterns."""
    root_abs = os.path.abspath(root_dir)
    all_findings: List[UncertaintyFinding] = []

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
                findings = scan_file_uncertainty(full_path, rel_path)
                all_findings.extend(findings)

    resolved_count = 0  # Static AST references
    potential_count = sum(1 for f in all_findings if f.confidence == ConfidenceLevel.POTENTIAL)
    unknown_count = sum(1 for f in all_findings if f.confidence == ConfidenceLevel.UNKNOWN)

    return {
        "total_uncertainties": len(all_findings),
        "potential_count": potential_count,
        "unknown_count": unknown_count,
        "findings": [f.to_dict() for f in all_findings]
    }
