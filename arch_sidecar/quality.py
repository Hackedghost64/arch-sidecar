import os
import ast
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Set, Optional, Any, Tuple
from pathlib import Path

from .utils import is_ignored_path


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

        # Calculate Cyclomatic Complexity
        complexity = self._calculate_complexity(node)
        self.function_complexities[node.name] = complexity

        if complexity > 15:
            self.issues.append(QualityIssue(
                file_path=self.file_path,
                line=node.lineno,
                symbol=node.name,
                category="complexity",
                severity="error",
                rule="excessive_cyclomatic_complexity",
                message=f"Function '{node.name}' has cyclomatic complexity of {complexity} (limit: 15).",
                suggestion="Decompose this function into smaller, single-purpose helper functions."
            ))
        elif complexity > self.max_complexity:
            self.issues.append(QualityIssue(
                file_path=self.file_path,
                line=node.lineno,
                symbol=node.name,
                category="complexity",
                severity="warning",
                rule="high_cyclomatic_complexity",
                message=f"Function '{node.name}' has cyclomatic complexity of {complexity} (limit: {self.max_complexity}).",
                suggestion="Simplify branching logic or extract sub-operations."
            ))

        # Check for guard clause opportunity at top of function
        self._check_guard_clause_opportunity(node)

        # Inspect nesting depth within function body
        for stmt in node.body:
            self._check_statement_nesting(stmt, depth=1)

        self._current_function = prev_function
        self._current_depth = prev_depth

    def _calculate_complexity(self, node: Any) -> int:
        """
        McCabe Cyclomatic Complexity:
        Base = 1
        +1 for each If, For, AsyncFor, While, ExceptHandler, With
        +1 for each boolean operator (And, Or)
        +1 for comprehension if clauses
        """
        complexity = 1
        for subnode in ast.walk(node):
            if isinstance(subnode, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler)):
                complexity += 1
            elif isinstance(subnode, ast.BoolOp):
                complexity += len(subnode.values) - 1
            elif isinstance(subnode, ast.comprehension):
                complexity += len(subnode.ifs)
            elif isinstance(subnode, ast.IfExp):  # ternary
                complexity += 1
        return complexity

    def _check_statement_nesting(self, stmt: ast.stmt, depth: int) -> None:
        if depth > self.max_nesting_found:
            self.max_nesting_found = depth

        is_compound = isinstance(stmt, (ast.If, ast.While, ast.For, ast.AsyncFor, ast.With, ast.AsyncWith, ast.Try))
        if is_compound:
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

        # Compound statements that increase indentation/nesting depth
        compound_bodies: List[List[ast.stmt]] = []
        if isinstance(stmt, (ast.If, ast.While, ast.For, ast.AsyncFor)):
            compound_bodies.append(stmt.body)
            if hasattr(stmt, "orelse") and stmt.orelse:
                # Elif does not increase cognitive nesting if it's chained, but raw else does
                if not (len(stmt.orelse) == 1 and isinstance(stmt.orelse[0], ast.If)):
                    compound_bodies.append(stmt.orelse)
                else:
                    # Chained elif: same depth
                    self._check_statement_nesting(stmt.orelse[0], depth)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            compound_bodies.append(stmt.body)
        elif isinstance(stmt, ast.Try):
            compound_bodies.append(stmt.body)
            for h in stmt.handlers:
                compound_bodies.append(h.body)
            if stmt.orelse:
                compound_bodies.append(stmt.orelse)
            if stmt.finalbody:
                compound_bodies.append(stmt.finalbody)

        for body in compound_bodies:
            for substate in body:
                self._check_statement_nesting(substate, depth + 1)

    def _check_guard_clause_opportunity(self, node: Any) -> None:
        """
        Detects functions wrapped entirely inside an `if <condition>: ...` with
        an optional trailing `else: return` or empty return.
        """
        if len(node.body) == 1 and isinstance(node.body[0], ast.If):
            if_stmt = node.body[0]
            # If the if-block contains multiple statements (>3)
            if len(if_stmt.body) >= 3:
                self.issues.append(QualityIssue(
                    file_path=self.file_path,
                    line=if_stmt.lineno,
                    symbol=node.name,
                    category="complexity",
                    severity="warning",
                    rule="missing_guard_clause",
                    message=f"Function '{node.name}' wraps its entire body inside an if-statement.",
                    suggestion=f"Invert condition `if not (...): return` to flatten function body."
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

        # Collect parameter names (excluding self/cls)
        all_args = [a.arg for a in node.args.args if a.arg not in ("self", "cls")]
        all_args += [a.arg for a in getattr(node.args, "posonlyargs", []) if a.arg not in ("self", "cls")]
        all_args += [a.arg for a in getattr(node.args, "kwonlyargs", [])]
        self._current_params = set(all_args)

        # 1. Dangerous mutable default arguments
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

    def _check_exception_handler(self, handler: ast.ExceptHandler) -> None:
        # Check if broad exception
        is_broad = False
        if handler.type is None:
            is_broad = True  # bare except:
        elif isinstance(handler.type, ast.Name) and handler.type.id in ("Exception", "BaseException"):
            is_broad = True

        # Check handler body
        has_logging_or_print = False
        has_raise = False
        returns_value = False

        for stmt in handler.body:
            if isinstance(stmt, ast.Raise):
                has_raise = True
            elif isinstance(stmt, ast.Return):
                returns_value = True
            elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                func = stmt.value.func
                # Detect logger.info, logging.error, print(), etc.
                if isinstance(func, ast.Name) and func.id in ("print", "warn"):
                    has_logging_or_print = True
                elif isinstance(func, ast.Attribute) and func.attr in ("exception", "error", "warning", "info", "debug"):
                    has_logging_or_print = True

        # If bare pass without logging
        if len(handler.body) == 1 and isinstance(handler.body[0], ast.Pass):
            severity = "error" if is_broad else "warning"
            self.issues.append(QualityIssue(
                file_path=self.file_path,
                line=handler.lineno,
                symbol=self._current_function or "<module>",
                category="anti_pattern",
                severity=severity,
                rule="silent_exception_swallowing",
                message=f"Exception silently swallowed with 'pass' without logging or re-raising.",
                suggestion="Log the exception with traceback or re-raise a domain-specific exception."
            ))
        elif is_broad and not has_raise and not has_logging_or_print:
            self.issues.append(QualityIssue(
                file_path=self.file_path,
                line=handler.lineno,
                symbol=self._current_function or "<module>",
                category="anti_pattern",
                severity="warning",
                rule="unlogged_broad_exception",
                message=f"Broad exception caught without logging or re-raising.",
                suggestion="Catch a specific exception type or log the failure before handling."
            ))

    def visit_Call(self, node: ast.Call) -> None:
        # Detect in-place mutation of incoming parameters: param.append(...), param.pop()
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

    def visit_Assign(self, node: ast.Assign) -> None:
        # Detect parameter subscript assignment: param[k] = v
        if self._current_function:
            for target in node.targets:
                if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
                    if target.value.id in self._current_params:
                        self.issues.append(QualityIssue(
                            file_path=self.file_path,
                            line=node.lineno,
                            symbol=self._current_function,
                            category="anti_pattern",
                            severity="warning",
                            rule="parameter_subscript_mutation",
                            message=f"Parameter '{target.value.id}' mutated in-place via subscript assignment.",
                            suggestion="Create and return a new dictionary/list copy rather than mutating caller's state."
                        ))
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
        self._class_methods: Dict[str, Set[str]] = {}  # method_name -> set of self.attributes used
        self._ignored_demeter_nodes: Set[int] = set()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        prev_class = self._current_class
        self._current_class = node.name
        self._class_methods = {}

        # 1. God Class Detection
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

        # Visit methods and record attribute accesses
        for method in methods:
            self._analyze_method(method)

        # 2. Check Cohesion (LCOM) if class has multiple methods
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

        # Record self.attribute usages for cohesion analysis
        accessed_attrs: Set[str] = set()
        for subnode in ast.walk(node):
            if isinstance(subnode, ast.Attribute) and isinstance(subnode.value, ast.Name):
                if subnode.value.id == "self":
                    accessed_attrs.add(subnode.attr)
        self._class_methods[node.name] = accessed_attrs

        # Also inspect general statements inside method for Law of Demeter
        self.visit(node)

    def _check_class_cohesion(self, node: ast.ClassDef) -> None:
        # Filter out dunder methods
        business_methods = {k: v for k, v in self._class_methods.items() if not (k.startswith("__") and k.endswith("__"))}
        if len(business_methods) >= 4:
            # Check how many methods share at least one attribute with another method
            all_shared_pairs = 0
            keys = list(business_methods.keys())
            for i in range(len(keys)):
                for j in range(i + 1, len(keys)):
                    if business_methods[keys[i]] & business_methods[keys[j]]:
                        all_shared_pairs += 1

            total_pairs = (len(keys) * (len(keys) - 1)) // 2
            if total_pairs > 0 and (all_shared_pairs / total_pairs) < 0.15:
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

        # Law of Demeter: count chained attribute depth (e.g. a.b.c.d)
        depth = 0
        curr = node
        chain_nodes = []
        while isinstance(curr, ast.Attribute):
            depth += 1
            chain_nodes.append(curr)
            curr = curr.value

        # Depth >= 3 means a.b.c.d (4 levels)
        if depth >= 3:
            # Mark inner nodes so they are not double-reported
            for inner in chain_nodes[1:]:
                self._ignored_demeter_nodes.add(id(inner))

            # Skip common fluent builder patterns (e.g., queryset, builder, parser)
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
        returns: List[ast.Return] = []
        for subnode in ast.walk(node):
            if isinstance(subnode, ast.Return):
                returns.append(subnode)

        if len(returns) <= 1:
            return

        has_value_return = any(r.value is not None and not (isinstance(r.value, ast.Constant) and r.value.value is None) for r in returns)
        has_bare_return = any(r.value is None or (isinstance(r.value, ast.Constant) and r.value.value is None) for r in returns)

        # If a function returns a value in one place and bare None in another, check if Optional is declared
        if has_value_return and has_bare_return:
            is_annotated_optional = False
            if node.returns:
                # Check for Optional[...] or ... | None or Union[..., None]
                returns_str = ast.unparse(node.returns) if hasattr(ast, "unparse") else ""
                if "Optional" in returns_str or "None" in returns_str or "|" in returns_str:
                    is_annotated_optional = True

            if not is_annotated_optional:
                self.issues.append(QualityIssue(
                    file_path=self.file_path,
                    line=node.lineno,
                    symbol=node.name,
                    category="contract",
                    severity="warning",
                    rule="inconsistent_return_contract",
                    message=f"Function '{node.name}' returns values in some branches and bare None in others without Optional type annotation.",
                    suggestion=f"Annotate return type as `Optional[...]` or ensure all branches return a consistent domain object/error."
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

    # 1. Complexity
    c_visitor = ComplexityVisitor(file_path=file_path)
    c_visitor.visit(tree)
    issues.extend(c_visitor.issues)

    # 2. Anti-Pattern
    ap_visitor = AntiPatternVisitor(file_path=file_path)
    ap_visitor.visit(tree)
    issues.extend(ap_visitor.issues)

    # 3. OOP & Cohesion
    oop_visitor = OOPVisitor(file_path=file_path)
    oop_visitor.visit(tree)
    issues.extend(oop_visitor.issues)

    # 4. Contract
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
    except Exception as e:
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


def audit_repository(repo_path: str, strict: bool = False) -> QualityReport:
    """
    Recursively scans and audits all Python source files in a repository.
    Computes quality scores and metrics.
    """
    report = QualityReport()
    all_issues: List[QualityIssue] = []
    py_files: List[str] = []

    for root, dirs, files in os.walk(repo_path):
        rel_root = os.path.relpath(root, repo_path)
        if rel_root != "." and is_ignored_path(rel_root):
            dirs.clear()
            continue
        dirs[:] = [d for d in dirs if not is_ignored_path(d if rel_root == "." else os.path.join(rel_root, d))]
        for f in files:
            rel_file = f if rel_root == "." else os.path.join(rel_root, f)
            if f.endswith(".py") and not is_ignored_path(rel_file):
                py_files.append(os.path.join(root, f))

    report.total_files = len(py_files)

    for fpath in py_files:
        issues = audit_file(fpath)
        all_issues.extend(issues)

    report.issues = all_issues
    report.total_issues = len(all_issues)
    report.error_count = sum(1 for i in all_issues if i.severity == "error")
    report.warning_count = sum(1 for i in all_issues if i.severity == "warning")

    # Score calculation normalized per file: 100 - (errors * 10 + warnings * 2) / total_files
    penalty = (report.error_count * 10.0) + (report.warning_count * 2.0)
    penalty_per_file = (penalty / report.total_files) if report.total_files > 0 else 0.0
    report.score = max(0.0, min(100.0, 100.0 - penalty_per_file))

    if strict:
        report.clean = (report.total_issues == 0)
    else:
        report.clean = (report.error_count == 0)

    # Category Breakdown Metrics
    category_counts = {
        "complexity": sum(1 for i in all_issues if i.category == "complexity"),
        "anti_pattern": sum(1 for i in all_issues if i.category == "anti_pattern"),
        "oop_cohesion": sum(1 for i in all_issues if i.category == "oop_cohesion"),
        "contract": sum(1 for i in all_issues if i.category == "contract"),
    }
    clean_files_count = report.total_files - len({i.file_path for i in all_issues})
    clean_pct = (clean_files_count / report.total_files * 100.0) if report.total_files > 0 else 100.0
    report.metrics = {
        "categories": category_counts,
        "clean_files": clean_files_count,
        "affected_files": len({i.file_path for i in all_issues}),
        "clean_files_percentage": round(clean_pct, 1),
        "issues_per_file": round(report.total_issues / max(1, report.total_files), 2),
    }

    return report
