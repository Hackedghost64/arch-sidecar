import os
import ast
import logging
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Set, Optional, Any, Tuple
from pathlib import Path

from .utils import is_ignored_path

logger = logging.getLogger(__name__)


@dataclass
class QualityIssue:
    file_path: str
    line: int
    symbol: str
    category: str  # "complexity", "anti_pattern", "oop_cohesion", "contract"
    severity: str  # "warning", "error"
    rule: str
    message: str
    suggestion: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class QualityReport:
    total_files: int = 0
    total_issues: int = 0
    warning_count: int = 0
    error_count: int = 0
    clean: bool = True
    score: float = 100.0  # 0.0 to 100.0
    issues: List[QualityIssue] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_files": self.total_files,
            "total_issues": self.total_issues,
            "warning_count": self.warning_count,
            "error_count": self.error_count,
            "clean": self.clean,
            "score": round(self.score, 1),
            "issues": [issue.to_dict() for issue in self.issues],
            "metrics": self.metrics,
        }


# =====================================================================
# 1. Complexity & Nesting Guard
# =====================================================================

class ComplexityVisitor(ast.NodeVisitor):
    """
    Computes cyclomatic complexity and statement nesting depth.
    Detects high cognitive load and opportunities for early-return guard clauses.
    """

    def __init__(self, file_path: str, max_depth: int = 3, max_complexity: int = 8):
        self.file_path = file_path
        self.max_depth = max_depth
        self.max_complexity = max_complexity
        self.issues: List[QualityIssue] = []
        self._current_function: Optional[str] = None
        self._current_depth = 0
        self.function_complexities: Dict[str, int] = {}
        self.max_nesting_found = 0

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._analyze_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._analyze_function(node)

    def _analyze_function(self, node: Any) -> None:
        prev_function = self._current_function
        prev_depth = self._current_depth
        self._current_function = node.name
        self._current_depth = 0

        complexity = self._calculate_complexity(node)
        self.function_complexities[node.name] = complexity

        is_dispatcher = node.name in ("main", "cli", "dispatch", "handle_tool_call")
        error_limit = 150 if is_dispatcher else 15
        warn_limit = 50 if is_dispatcher else self.max_complexity

        if complexity > error_limit:
            self.issues.append(QualityIssue(
                file_path=self.file_path,
                line=node.lineno,
                symbol=node.name,
                category="complexity",
                severity="error",
                rule="excessive_cyclomatic_complexity",
                message=f"Function '{node.name}' has cyclomatic complexity of {complexity} (limit: {error_limit}).",
                suggestion="Decompose this function into smaller, single-purpose helper functions."
            ))
        elif complexity > warn_limit:
            self.issues.append(QualityIssue(
                file_path=self.file_path,
                line=node.lineno,
                symbol=node.name,
                category="complexity",
                severity="warning",
                rule="high_cyclomatic_complexity",
                message=f"Function '{node.name}' has cyclomatic complexity of {complexity} (limit: {warn_limit}).",
                suggestion="Simplify branching logic or extract sub-operations."
            ))

        self._check_guard_clause_opportunity(node)

        for stmt in node.body:
            self._check_statement_nesting(stmt, depth=1)

        self._current_function = prev_function
        self._current_depth = prev_depth

    def _calculate_complexity(self, node: Any) -> int:
        """
        McCabe Cyclomatic Complexity:
        Base = 1
        +1 for each If, For, AsyncFor, While, ExceptHandler
        +1 for each boolean operator (And, Or)
        +1 for comprehension if clauses
        +1 for ternary IfExp
        """
        complexity = 1
        for subnode in ast.walk(node):
            if isinstance(subnode, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler)):
                complexity += 1
            elif isinstance(subnode, ast.BoolOp):
                complexity += len(subnode.values) - 1
            elif isinstance(subnode, ast.comprehension):
                complexity += len(subnode.ifs)
            elif isinstance(subnode, ast.IfExp):
                complexity += 1
        return complexity

    def _record_nesting_issue(self, stmt: ast.stmt, depth: int) -> None:
        if depth >= 5:
            self.issues.append(QualityIssue(
                file_path=self.file_path,
                line=stmt.lineno,
                symbol=self._current_function or "<module>",
                category="complexity",
                severity="error",
                rule="excessive_nesting_depth",
                message=f"Block nested {depth} levels deep (limit: 5).",
                suggestion="Invert conditional guards (early return) or extract inner block into a dedicated function."
            ))
        elif depth > self.max_depth:
            self.issues.append(QualityIssue(
                file_path=self.file_path,
                line=stmt.lineno,
                symbol=self._current_function or "<module>",
                category="complexity",
                severity="warning",
                rule="deep_nesting",
                message=f"Block nested {depth} levels deep (limit: {self.max_depth}).",
                suggestion="Use early returns or guard clauses to flatten the control flow."
            ))

    def _get_loop_if_bodies(self, stmt: ast.stmt, depth: int) -> List[List[ast.stmt]]:
        bodies = [stmt.body]
        orelse = getattr(stmt, "orelse", None)
        if not orelse:
            return bodies
        if len(orelse) == 1 and isinstance(orelse[0], ast.If):
            self._check_statement_nesting(orelse[0], depth)
        else:
            bodies.append(orelse)
        return bodies

    def _get_try_bodies(self, stmt: ast.Try) -> List[List[ast.stmt]]:
        bodies = [stmt.body]
        for h in stmt.handlers:
            bodies.append(h.body)
        if stmt.orelse:
            bodies.append(stmt.orelse)
        if stmt.finalbody:
            bodies.append(stmt.finalbody)
        return bodies

    def _get_compound_bodies(self, stmt: ast.stmt, depth: int) -> List[List[ast.stmt]]:
        if isinstance(stmt, (ast.If, ast.While, ast.For, ast.AsyncFor)):
            return self._get_loop_if_bodies(stmt, depth)
        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            return [stmt.body]
        if isinstance(stmt, ast.Try):
            return self._get_try_bodies(stmt)
        return []

    def _check_statement_nesting(self, stmt: ast.stmt, depth: int) -> None:
        if depth > self.max_nesting_found:
            self.max_nesting_found = depth

        is_compound = isinstance(stmt, (ast.If, ast.While, ast.For, ast.AsyncFor, ast.With, ast.AsyncWith, ast.Try))
        if is_compound:
            self._record_nesting_issue(stmt, depth)

        for body in self._get_compound_bodies(stmt, depth):
            for substate in body:
                self._check_statement_nesting(substate, depth + 1)

    def _check_guard_clause_opportunity(self, node: Any) -> None:
        """
        Detects functions wrapped entirely inside an `if <condition>: ...` with
        an optional trailing `else: return` or empty return.
        """
        if len(node.body) == 1 and isinstance(node.body[0], ast.If):
            if_stmt = node.body[0]
            if len(if_stmt.body) >= 3:
                self.issues.append(QualityIssue(
                    file_path=self.file_path,
                    line=if_stmt.lineno,
                    symbol=node.name,
                    category="complexity",
                    severity="warning",
                    rule="missing_guard_clause",
                    message=f"Function '{node.name}' wraps its entire body inside an if-statement.",
                    suggestion="Invert condition `if not (...): return` to flatten function body."
                ))


# =====================================================================
# 2. Anti-Pattern Sentry
# =====================================================================

class AntiPatternVisitor(ast.NodeVisitor):
    """
    Detects critical software anti-patterns:
    - Silent exception swallowing (`except Exception: pass`, `return None` without logging/raising).
    - Dangerous mutable default arguments (`def f(x=[])`).
    - In-place mutation of caller parameters (`param.append()`, `param[k] = v`).
    """

    MUTATION_METHODS = {"append", "extend", "insert", "pop", "remove", "clear", "update"}

    def __init__(self, file_path: str):
        self.file_path = file_path
        self.issues: List[QualityIssue] = []
        self._current_function: Optional[str] = None
        self._current_params: Set[str] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._analyze_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._analyze_function(node)

    def _analyze_function(self, node: Any) -> None:
        prev_func = self._current_function
        prev_params = self._current_params
        self._current_function = node.name

        all_args = [a.arg for a in node.args.args if a.arg not in ("self", "cls")]
        all_args += [a.arg for a in getattr(node.args, "posonlyargs", []) if a.arg not in ("self", "cls")]
        all_args += [a.arg for a in getattr(node.args, "kwonlyargs", [])]
        self._current_params = set(all_args)

        all_defaults = node.args.defaults + [d for d in getattr(node.args, "kw_defaults", []) if d is not None]
        for default in all_defaults:
            if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                self.issues.append(QualityIssue(
                    file_path=self.file_path,
                    line=node.lineno,
                    symbol=node.name,
                    category="anti_pattern",
                    severity="error",
                    rule="mutable_default_argument",
                    message=f"Function '{node.name}' uses mutable default argument.",
                    suggestion="Replace with `None` and initialize inside the function (`if arg is None: arg = []`)."
                ))

        self.generic_visit(node)
        self._current_function = prev_func
        self._current_params = prev_params

    def visit_Try(self, node: ast.Try) -> None:
        for handler in node.handlers:
            self._check_exception_handler(handler)
        self.generic_visit(node)

    @staticmethod
    def _is_broad_exception(handler: ast.ExceptHandler) -> bool:
        if handler.type is None:
            return True
        return isinstance(handler.type, ast.Name) and handler.type.id in ("Exception", "BaseException")

    @staticmethod
    def _is_logging_call(expr: ast.Call) -> bool:
        func = expr.func
        if isinstance(func, ast.Name):
            return func.id in ("print", "warn")
        if isinstance(func, ast.Attribute):
            return func.attr in ("exception", "error", "warning", "info", "debug")
        return False

    def _inspect_handler_body(self, handler: ast.ExceptHandler) -> Tuple[bool, bool]:
        has_raise = False
        has_logging = False
        for stmt in handler.body:
            if isinstance(stmt, ast.Raise):
                has_raise = True
            elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                if self._is_logging_call(stmt.value):
                    has_logging = True
        return has_raise, has_logging

    def _check_silent_swallow(self, handler: ast.ExceptHandler, is_broad: bool) -> bool:
        if len(handler.body) != 1 or not isinstance(handler.body[0], ast.Pass):
            return False
        severity = "error" if is_broad else "warning"
        self.issues.append(QualityIssue(
            file_path=self.file_path,
            line=handler.lineno,
            symbol=self._current_function or "<module>",
            category="anti_pattern",
            severity=severity,
            rule="silent_exception_swallowing",
            message="Exception silently swallowed with 'pass' without logging or re-raising.",
            suggestion="Log the exception with traceback or re-raise a domain-specific exception."
        ))
        return True

    def _check_exception_handler(self, handler: ast.ExceptHandler) -> None:
        is_broad = self._is_broad_exception(handler)
        if self._check_silent_swallow(handler, is_broad):
            return

        has_raise, has_logging = self._inspect_handler_body(handler)
        if is_broad and not has_raise and not has_logging:
            self.issues.append(QualityIssue(
                file_path=self.file_path,
                line=handler.lineno,
                symbol=self._current_function or "<module>",
                category="anti_pattern",
                severity="warning",
                rule="unlogged_broad_exception",
                message="Broad exception caught without logging or re-raising.",
                suggestion="Catch a specific exception type or log the failure before handling."
            ))

    def visit_Call(self, node: ast.Call) -> None:
        if self._current_function and isinstance(node.func, ast.Attribute):
            target = node.func.value
            method_name = node.func.attr
            if isinstance(target, ast.Name) and target.id in self._current_params:
                if method_name in self.MUTATION_METHODS:
                    self.issues.append(QualityIssue(
                        file_path=self.file_path,
                        line=node.lineno,
                        symbol=self._current_function,
                        category="anti_pattern",
                        severity="warning",
                        rule="parameter_mutation",
                        message=f"Parameter '{target.id}' mutated in-place via '.{method_name}()'.",
                        suggestion="Treat input parameters as immutable. Return a new copy instead of mutating."
                    ))
        self.generic_visit(node)

    def _check_subscript_target_mutation(self, target: ast.AST, lineno: int) -> None:
        if not (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)):
            return
        if target.value.id in self._current_params:
            self.issues.append(QualityIssue(
                file_path=self.file_path,
                line=lineno,
                symbol=self._current_function or "<unknown>",
                category="anti_pattern",
                severity="warning",
                rule="parameter_subscript_mutation",
                message=f"Parameter '{target.value.id}' mutated in-place via subscript assignment.",
                suggestion="Create and return a new dictionary/list copy rather than mutating caller's state."
            ))

    def visit_Assign(self, node: ast.Assign) -> None:
        if not self._current_function:
            self.generic_visit(node)
            return

        for target in node.targets:
            self._check_subscript_target_mutation(target, node.lineno)
        self.generic_visit(node)


# =====================================================================
# 3. OOP & Structural Cohesion Sentry
# =====================================================================

class OOPVisitor(ast.NodeVisitor):
    """
    Enforces clean Object-Oriented and modular design:
    - God Classes (>250 lines or >15 methods).
    - Sprawling Methods (>50 lines).
    - Law of Demeter violations (deep property chains like a.b.c.d()).
    - Method Cohesion (LCOM): methods operating on zero shared instance attributes.
    """

    def __init__(self, file_path: str):
        self.file_path = file_path
        self.issues: List[QualityIssue] = []
        self._current_class: Optional[str] = None
        self._class_methods: Dict[str, Set[str]] = {}
        self._ignored_demeter_nodes: Set[int] = set()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        prev_class = self._current_class
        self._current_class = node.name
        self._class_methods = {}

        class_lines = (node.end_lineno or node.lineno) - node.lineno
        methods = [stmt for stmt in node.body if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))]
        method_count = len(methods)

        if class_lines > 300 or method_count > 15:
            self.issues.append(QualityIssue(
                file_path=self.file_path,
                line=node.lineno,
                symbol=node.name,
                category="oop_cohesion",
                severity="warning",
                rule="god_class",
                message=f"Class '{node.name}' has {method_count} methods and spans {class_lines} lines.",
                suggestion="Decompose this class into smaller, specialized domain objects (Single Responsibility Principle)."
            ))

        for method in methods:
            self._analyze_method(method)

        self._check_class_cohesion(node)
        self._current_class = prev_class

    def _analyze_method(self, node: Any) -> None:
        method_lines = (node.end_lineno or node.lineno) - node.lineno
        if method_lines > 50:
            self.issues.append(QualityIssue(
                file_path=self.file_path,
                line=node.lineno,
                symbol=f"{self._current_class}.{node.name}",
                category="oop_cohesion",
                severity="warning",
                rule="sprawling_method",
                message=f"Method '{self._current_class}.{node.name}' spans {method_lines} lines (limit: 50).",
                suggestion="Extract cohesive subroutines or separate responsibilities."
            ))

        accessed_attrs: Set[str] = set()
        for subnode in ast.walk(node):
            if isinstance(subnode, ast.Attribute) and isinstance(subnode.value, ast.Name):
                if subnode.value.id == "self":
                    accessed_attrs.add(subnode.attr)
        self._class_methods[node.name] = accessed_attrs

        self.visit(node)

    @staticmethod
    def _count_shared_pairs(methods: Dict[str, Set[str]]) -> int:
        shared = 0
        keys = list(methods.keys())
        for i, k1 in enumerate(keys):
            for k2 in keys[i + 1:]:
                if methods[k1] & methods[k2]:
                    shared += 1
        return shared

    def _check_class_cohesion(self, node: ast.ClassDef) -> None:
        business_methods = {
            k: v for k, v in self._class_methods.items()
            if not (k.startswith("__") and k.endswith("__"))
        }
        if len(business_methods) < 4:
            return

        keys_len = len(business_methods)
        total_pairs = (keys_len * (keys_len - 1)) // 2
        if total_pairs <= 0:
            return

        all_shared_pairs = self._count_shared_pairs(business_methods)
        if (all_shared_pairs / total_pairs) < 0.15:
            self.issues.append(QualityIssue(
                file_path=self.file_path,
                line=node.lineno,
                symbol=node.name,
                category="oop_cohesion",
                severity="warning",
                rule="low_method_cohesion",
                message=f"Class '{node.name}' exhibits very low method cohesion (methods do not share instance state).",
                suggestion="Consider splitting unrelated methods into separate classes or standalone utility modules."
            ))

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if id(node) in self._ignored_demeter_nodes:
            self.generic_visit(node)
            return

        depth = 0
        curr = node
        chain_nodes = []
        while isinstance(curr, ast.Attribute):
            depth += 1
            chain_nodes.append(curr)
            curr = curr.value

        if depth >= 3:
            for inner in chain_nodes[1:]:
                self._ignored_demeter_nodes.add(id(inner))

            if not isinstance(curr, ast.Call):
                self.issues.append(QualityIssue(
                    file_path=self.file_path,
                    line=node.lineno,
                    symbol=self._current_class or "<module>",
                    category="oop_cohesion",
                    severity="warning",
                    rule="law_of_demeter_violation",
                    message=f"Deep property access chain ({depth + 1} levels). Violates Law of Demeter.",
                    suggestion="Encapsulate traversal: tell, don't ask. Let the intermediate object provide a direct method."
                ))
        self.generic_visit(node)


# =====================================================================
# 4. Contract & Type Uniformity Sentry
# =====================================================================

def _is_none_return(ret: ast.Return) -> bool:
    if ret.value is None:
        return True
    return isinstance(ret.value, ast.Constant) and ret.value.value is None


def _is_optional_return_annotation(returns_node: Optional[ast.AST]) -> bool:
    if not returns_node or not hasattr(ast, "unparse"):
        return False
    text = ast.unparse(returns_node)
    return "Optional" in text or "None" in text or "|" in text


class ContractVisitor(ast.NodeVisitor):
    """
    Enforces contract consistency:
    - Verifies that all return statements in a function return uniform types.
    - Flags functions mixing value returns with bare `return` or `return None` without Optional annotation.
    """

    def __init__(self, file_path: str):
        self.file_path = file_path
        self.issues: List[QualityIssue] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check_returns(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._check_returns(node)
        self.generic_visit(node)

    def _check_returns(self, node: Any) -> None:
        returns = [s for s in ast.walk(node) if isinstance(s, ast.Return)]
        if len(returns) <= 1:
            return

        has_bare = any(_is_none_return(r) for r in returns)
        has_val = any(not _is_none_return(r) for r in returns)
        if not (has_bare and has_val):
            return

        if _is_optional_return_annotation(getattr(node, "returns", None)):
            return

        self.issues.append(QualityIssue(
            file_path=self.file_path,
            line=node.lineno,
            symbol=node.name,
            category="contract",
            severity="warning",
            rule="inconsistent_return_contract",
            message=f"Function '{node.name}' returns values in some branches and bare None in others without Optional type annotation.",
            suggestion="Annotate return type as `Optional[...]` or ensure all branches return a consistent domain object/error."
        ))


# =====================================================================
# Main Audit Engine
# =====================================================================

def audit_code_string(code_str: str, file_path: str = "<string>") -> List[QualityIssue]:
    """Runs all 4 quality visitors on a code string."""
    try:
        tree = ast.parse(code_str, filename=file_path)
    except SyntaxError as e:
        return [
            QualityIssue(
                file_path=file_path,
                line=e.lineno or 1,
                symbol="<syntax>",
                category="contract",
                severity="error",
                rule="syntax_error",
                message=f"SyntaxError: {e.msg}",
                suggestion="Fix syntax errors before running quality audit."
            )
        ]

    issues: List[QualityIssue] = []

    c_visitor = ComplexityVisitor(file_path=file_path)
    c_visitor.visit(tree)
    issues.extend(c_visitor.issues)

    ap_visitor = AntiPatternVisitor(file_path=file_path)
    ap_visitor.visit(tree)
    issues.extend(ap_visitor.issues)

    oop_visitor = OOPVisitor(file_path=file_path)
    oop_visitor.visit(tree)
    issues.extend(oop_visitor.issues)

    con_visitor = ContractVisitor(file_path=file_path)
    con_visitor.visit(tree)
    issues.extend(con_visitor.issues)

    return issues


def audit_file(file_path: str) -> List[QualityIssue]:
    """Audits a single Python file."""
    if not os.path.isfile(file_path) or not file_path.endswith(".py"):
        return []
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return audit_code_string(content, file_path=file_path)
    except (OSError, UnicodeDecodeError) as e:
        logger.debug("Could not read file %s: %s", file_path, e)
        return [
            QualityIssue(
                file_path=file_path,
                line=1,
                symbol="<io>",
                category="contract",
                severity="error",
                rule="read_error",
                message=f"Could not read file: {e}",
                suggestion="Ensure file exists and has read permissions."
            )
        ]
    except Exception as e:
        logger.debug("Unexpected error reading file %s: %s", file_path, e)
        return [
            QualityIssue(
                file_path=file_path,
                line=1,
                symbol="<io>",
                category="contract",
                severity="error",
                rule="read_error",
                message=f"Could not read file: {e}",
                suggestion="Ensure file exists and has read permissions."
            )
        ]


def _should_skip_dir(rel_root: str) -> bool:
    return rel_root != "." and is_ignored_path(rel_root)


def _is_target_python_file(filename: str, rel_root: str) -> bool:
    if not filename.endswith(".py"):
        return False
    rel_file = filename if rel_root == "." else os.path.join(rel_root, filename)
    return not is_ignored_path(rel_file)


def _collect_python_files(repo_path: str) -> List[str]:
    py_files: List[str] = []
    for root, dirs, files in os.walk(repo_path):
        rel_root = os.path.relpath(root, repo_path)
        if _should_skip_dir(rel_root):
            dirs.clear()
            continue
        dirs[:] = [
            d for d in dirs
            if not _should_skip_dir(d if rel_root == "." else os.path.join(rel_root, d))
        ]
        for f in files:
            if _is_target_python_file(f, rel_root):
                py_files.append(os.path.join(root, f))
    return py_files


def _calculate_score(total_files: int, error_count: int, warning_count: int) -> float:
    if total_files <= 0:
        return 100.0
    penalty = (error_count * 10.0) + (warning_count * 2.0)
    penalty_per_file = penalty / total_files
    return max(0.0, min(100.0, 100.0 - penalty_per_file))


def _build_metrics(issues: List[QualityIssue], total_files: int) -> Dict[str, Any]:
    categories = ("complexity", "anti_pattern", "oop_cohesion", "contract")
    category_counts = {cat: 0 for cat in categories}
    affected_files: Set[str] = set()
    for issue in issues:
        affected_files.add(issue.file_path)
        if issue.category in category_counts:
            category_counts[issue.category] += 1

    clean_count = max(0, total_files - len(affected_files))
    clean_pct = (clean_count / total_files * 100.0) if total_files > 0 else 100.0
    issues_per_file = round(len(issues) / max(1, total_files), 2)
    return {
        "categories": category_counts,
        "clean_files": clean_count,
        "affected_files": len(affected_files),
        "clean_files_percentage": round(clean_pct, 1),
        "issues_per_file": issues_per_file,
    }


def audit_repository(repo_path: str, strict: bool = False) -> QualityReport:
    """
    Recursively scans and audits all Python source files in a repository.
    Computes quality scores and metrics.
    """
    py_files = _collect_python_files(repo_path)
    all_issues: List[QualityIssue] = []
    for fpath in py_files:
        all_issues.extend(audit_file(fpath))

    error_count = sum(1 for i in all_issues if i.severity == "error")
    warning_count = sum(1 for i in all_issues if i.severity == "warning")
    score = _calculate_score(len(py_files), error_count, warning_count)
    clean = (len(all_issues) == 0) if strict else (error_count == 0)

    return QualityReport(
        total_files=len(py_files),
        total_issues=len(all_issues),
        warning_count=warning_count,
        error_count=error_count,
        clean=clean,
        score=score,
        issues=all_issues,
        metrics=_build_metrics(all_issues, len(py_files)),
    )
