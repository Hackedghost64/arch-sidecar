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
]

__version__ = "0.3.0"
