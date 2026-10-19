import os
import re
import time
from typing import Dict, List, Optional, Set, Any

from .planner import plan_adaptive_retrieval, RetrievalStrategy
from .uncertainty import scan_file_uncertainty, ConfidenceLevel
from .decouple import prefactor_decouple
from .utils import estimate_tokens

class WorkflowStage:
    RETRIEVAL_PLAN = "retrieval_plan"
    UNCERTAINTY_AUDIT = "uncertainty_audit"
    DEPENDENCY_PREFLIGHT = "dependency_preflight"
    TRANSFORMATION = "transformation"
    VERIFICATION = "verification"

def run_workflow(
    task: str,
    project_root: str = ".",
    from_file: Optional[str] = None,
    to_file: Optional[str] = None,
    symbols: Optional[List[str]] = None,
    test_command: Optional[str] = None,
    dry_run: bool = False,
    token_budget: int = 3000
) -> Dict[str, Any]:
    """
    End-to-End Orchestrated Workflow:
    Task Request -> Context Plan -> Reflection Audit -> Preflight Checks -> Deterministic AST Transformation -> Test Verification & Rollback
    
    Dynamically routes between inspection-only vs structural refactoring pipelines.
    Tracks exact latency, token consumption per stage, diffs, and verification safety.
    """
    start_total_time = time.perf_counter()
    root_abs = os.path.abspath(project_root)

    stages_executed: List[str] = []
    telemetry = {
        "tokens_by_stage": {},
        "latency_ms_by_stage": {},
        "status": "INITIALIZED"
    }

    # =========================================================================
    # STAGE 1: Task-Aware Context Retrieval Planning
    # =========================================================================
    t0 = time.perf_counter()
    plan_res = plan_adaptive_retrieval(
        task=task,
        root_dir=root_abs,
        target_symbols=symbols,
        token_budget=token_budget
    )
    planning_time_ms = round((time.perf_counter() - t0) * 1000, 2)
    stages_executed.append(WorkflowStage.RETRIEVAL_PLAN)

    plan_tokens = plan_res["estimated_tokens"]
    telemetry["tokens_by_stage"][WorkflowStage.RETRIEVAL_PLAN] = plan_tokens
    telemetry["latency_ms_by_stage"][WorkflowStage.RETRIEVAL_PLAN] = planning_time_ms

    # Infer symbols, from_file, to_file if not explicitly passed
    resolved_symbols = symbols or plan_res.get("selected_symbols", [])
    if not from_file and plan_res.get("ranking"):
        # Select file from top candidate
        top_candidates = [c for c in plan_res["ranking"] if c.get("included")]
        if top_candidates:
            from_file = top_candidates[0]["file"]

    if not to_file:
        # Check task for pattern like 'into <file>.py' or 'to <file>.py'
        to_match = re.search(r"\b(?:into|to)\s+([\w\-./]+\.py)\b", task, re.IGNORECASE)
        if to_match:
            to_file = to_match.group(1)

    # Check if task requires code transformation
    refactor_keywords = {"decouple", "extract", "move", "split", "refactor into", "separate"}
    is_refactor_task = any(kw in task.lower() for kw in refactor_keywords) or (from_file and to_file and resolved_symbols)

    if not is_refactor_task or not (from_file and to_file and resolved_symbols):
        # Task is inspection, query, or small-file edit only. Stop pipeline cleanly without forced AST transforms.
        total_time_ms = round((time.perf_counter() - start_total_time) * 1000, 2)
        telemetry["status"] = "COMPLETED_RETRIEVAL"
        telemetry["total_latency_ms"] = total_time_ms
        telemetry["total_tokens"] = plan_tokens

        return {
            "success": True,
            "pipeline": "retrieval_only",
            "task": task,
            "stages_executed": stages_executed,
            "strategy": plan_res["strategy"],
            "strategy_reason": plan_res["strategy_reason"],
            "context": plan_res["context"],
            "telemetry": telemetry,
            "message": "Task evaluated as informational/inspection. Context plan generated without code modification."
        }

    # =========================================================================
    # STAGE 2: Dynamic Reflection & Uncertainty Audit
    # =========================================================================
    t1 = time.perf_counter()
    from_file_full = os.path.join(root_abs, from_file)
    findings = scan_file_uncertainty(from_file_full, from_file)
    stages_executed.append(WorkflowStage.UNCERTAINTY_AUDIT)
    audit_time_ms = round((time.perf_counter() - t1) * 1000, 2)
    telemetry["latency_ms_by_stage"][WorkflowStage.UNCERTAINTY_AUDIT] = audit_time_ms

    # Check if any UNKNOWN dynamic reflection directly touches the targeted symbols
    critical_uncertainties = [
        f for f in findings 
        if f.confidence == ConfidenceLevel.UNKNOWN and any(sym in f.code for sym in resolved_symbols)
    ]

    # =========================================================================
    # STAGE 3 & 4: Dependency Preflight & Deterministic Transformation
    # =========================================================================
    t2 = time.perf_counter()
    decouple_res = prefactor_decouple(
        symbol=resolved_symbols,
        from_file=from_file,
        to_file=to_file,
        project_root=root_abs,
        run_tests=test_command,
        dry_run=dry_run
    )
    stages_executed.append(WorkflowStage.DEPENDENCY_PREFLIGHT)
    if not dry_run:
        stages_executed.append(WorkflowStage.TRANSFORMATION)
    transform_time_ms = round((time.perf_counter() - t2) * 1000, 2)
    telemetry["latency_ms_by_stage"][WorkflowStage.TRANSFORMATION] = transform_time_ms

    # Verification status
    test_verification = decouple_res.get("test_verification")
    if test_verification:
        stages_executed.append(WorkflowStage.VERIFICATION)
        telemetry["latency_ms_by_stage"][WorkflowStage.VERIFICATION] = test_verification.get("duration_ms", 0)

    total_time_ms = round((time.perf_counter() - start_total_time) * 1000, 2)
    telemetry["total_latency_ms"] = total_time_ms

    diff_str = decouple_res.get("diff", "")
    diff_tokens = estimate_tokens(diff_str) if diff_str else 0
    telemetry["tokens_by_stage"][WorkflowStage.TRANSFORMATION] = diff_tokens
    telemetry["total_tokens"] = plan_tokens + diff_tokens

    if decouple_res.get("success"):
        telemetry["status"] = "VERIFIED" if (test_verification and test_verification.get("passed")) else "TRANSFORMED"
        return {
            "success": True,
            "pipeline": "full_refactor",
            "task": task,
            "stages_executed": stages_executed,
            "extracted_symbols": resolved_symbols,
            "from_file": from_file,
            "to_file": to_file,
            "diff": diff_str,
            "uncertainty_findings": [f.to_dict() for f in findings],
            "critical_uncertainties": [f.to_dict() for f in critical_uncertainties],
            "test_verification": test_verification,
            "telemetry": telemetry
        }
    else:
        telemetry["status"] = "ROLLED_BACK" if decouple_res.get("rolled_back") else "PREFLIGHT_BLOCKED"
        return {
            "success": False,
            "pipeline": "full_refactor",
            "task": task,
            "stages_executed": stages_executed,
            "error": decouple_res.get("error"),
            "rolled_back": decouple_res.get("rolled_back", False),
            "test_verification": test_verification,
            "uncertainty_findings": [f.to_dict() for f in findings],
            "telemetry": telemetry
        }
