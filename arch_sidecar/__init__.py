"""
arch-sidecar: Universal Architectural Sidecar & Context Decoupling Engine
Exposed as an MCP Server + Local CLI Daemon
"""

from .digest import generate_digest
from .context import locate_surgical_context
from .decouple import prefactor_decouple
from .planner import plan_adaptive_retrieval, RetrievalStrategy
from .uncertainty import (
    ConfidenceLevel,
    UncertaintyScanner,
    scan_file_uncertainty,
    scan_codebase_uncertainty,
)
from .workflow import run_workflow, WorkflowStage
from .guard import check_code_health
from .feature_workflow import run_feature_workflow
from .fault_localizer import (
    ASTCanonicalRepairCache,
    localize_pytest_faults,
    parse_pytest_traceback_targets,
    canonicalize_ast_line,
    decanonicalize_ast_line,
    extract_function_params,
)
from .session_logger import (
    record_session,
    load_sessions,
    generate_session_summary,
    format_session_report_markdown,
    format_session_report_terminal,
    clear_sessions,
)

__all__ = [
    "generate_digest",
    "locate_surgical_context",
    "prefactor_decouple",
    "plan_adaptive_retrieval",
    "RetrievalStrategy",
    "ConfidenceLevel",
    "UncertaintyScanner",
    "scan_file_uncertainty",
    "scan_codebase_uncertainty",
    "run_workflow",
    "WorkflowStage",
    "check_code_health",
    "run_feature_workflow",
    "ASTCanonicalRepairCache",
    "localize_pytest_faults",
    "parse_pytest_traceback_targets",
    "canonicalize_ast_line",
    "decanonicalize_ast_line",
    "extract_function_params",
    "record_session",
    "load_sessions",
    "generate_session_summary",
    "format_session_report_markdown",
    "format_session_report_terminal",
    "clear_sessions",
    "__version__",
]

__version__ = "0.4.0"
