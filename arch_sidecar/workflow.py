import os
import re
import time
from typing import Dict, List, Optional, Set, Any

from .planner import plan_adaptive_retrieval, RetrievalStrategy
from .uncertainty import scan_file_uncertainty, ConfidenceLevel
from .decouple import prefactor_decouple
from .utils import estimate_tokens
from .session_logger import record_session

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
    token_budget: int = 3000,
    share_state: bool = False,
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
    top_candidates = [c for c in plan_res.get("ranking", []) if c.get("included")]
    if not from_file and top_candidates:
        from_file = top_candidates[0]["file"]

    if symbols:
        resolved_symbols = list(symbols)
    elif from_file and top_candidates:
        from_rel_norm = os.path.normpath(from_file)
        file_candidates = [
            c for c in top_candidates
            if os.path.normpath(c["file"]) == from_rel_norm or os.path.basename(c["file"]) == os.path.basename(from_rel_norm)
        ]
        exact_matches = [c for c in file_candidates if "exact name match" in c.get("reason", "") or "case-insensitive word match" in c.get("reason", "")]
        chosen = exact_matches if exact_matches else file_candidates
        resolved_symbols = list(dict.fromkeys(c["name"] for c in chosen))
    else:
        resolved_symbols = plan_res.get("selected_symbols", [])

    if not to_file:
        # Check task for pattern like 'into <file>.py' or 'to <file>.py'
        to_match = re.search(r"\b(?:into|to)\s+([\w\-./]+\.py)\b", task, re.IGNORECASE)
        if to_match:
            to_file = to_match.group(1)

    # Check if task requires code transformation
    refactor_keywords = {"decouple", "extract", "move", "split", "refactor into", "separate"}
    explicit_refactor_request = bool(from_file and to_file and symbols)
    is_refactor_task = any(kw in task.lower() for kw in refactor_keywords) or bool(from_file and to_file and resolved_symbols)

    # If refactoring requested on a compact file without explicit caller override, recommend bypass
    if is_refactor_task and not explicit_refactor_request and plan_res.get("recommendation") == "BYPASS_DECOUPLING":
        total_time_ms = round((time.perf_counter() - start_total_time) * 1000, 2)
        telemetry["status"] = "BYPASS_RECOMMENDED"
        telemetry["total_latency_ms"] = total_time_ms
        telemetry["total_tokens"] = plan_tokens

        session_id = record_session(
            command="workflow",
            task=task,
            project_root=root_abs,
            success=True,
            duration_ms=total_time_ms,
            mode="bypass_recommended",
            target_files=[from_file] if from_file else [],
            target_symbols=resolved_symbols,
            estimated_tokens=plan_tokens,
        )

        return {
            "session_id": session_id,
            "success": True,
            "pipeline": "bypass_recommended",
            "task": task,
            "stages_executed": stages_executed,
            "strategy": plan_res["strategy"],
            "strategy_reason": plan_res["strategy_reason"],
            "recommendation": "BYPASS_DECOUPLING",
            "context": plan_res["context"],
            "telemetry": telemetry,
            "message": "Devrith Recommendation: Target file is compact. Direct file modification is more token-efficient than automated AST codemods. Decoupling bypassed."
        }

    if not is_refactor_task or not (from_file and to_file and resolved_symbols):
        # Task is inspection, query, or read-only audit. Stop pipeline cleanly without forced AST transforms.
        total_time_ms = round((time.perf_counter() - start_total_time) * 1000, 2)
        telemetry["status"] = "COMPLETED_RETRIEVAL"
        telemetry["total_latency_ms"] = total_time_ms
        telemetry["total_tokens"] = plan_tokens

        session_id = record_session(
            command="workflow",
            task=task,
            project_root=root_abs,
            success=True,
            duration_ms=total_time_ms,
            mode="retrieval_only",
            target_files=[from_file] if from_file else [],
            target_symbols=resolved_symbols,
            estimated_tokens=plan_tokens,
        )

        return {
            "session_id": session_id,
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
        dry_run=dry_run,
        share_state=share_state,
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
        session_id = record_session(
            command="workflow",
            task=task,
            project_root=root_abs,
            success=True,
            duration_ms=total_time_ms,
            mode="dry_run" if dry_run else "refactor",
            target_files=[f for f in [from_file, to_file] if f],
            target_symbols=resolved_symbols,
            estimated_tokens=plan_tokens + diff_tokens,
            test_run=test_verification,
        )
        return {
            "session_id": session_id,
            "success": True,
            "pipeline": "full_refactor",
            "task": task,
            "stages_executed": stages_executed,
            "extracted_symbols": decouple_res.get("extracted_symbols", resolved_symbols),
            "from_file": from_file,
            "to_file": to_file,
            "import_target": decouple_res.get("import_target"),
            "share_state_bound": decouple_res.get("share_state_bound", []),
            "ruff_gate": decouple_res.get("ruff_gate"),
            "diff": diff_str,
            "secret_warnings": decouple_res.get("secret_warnings", []),
            "uncertainty_findings": [f.to_dict() for f in findings],
            "critical_uncertainties": [f.to_dict() for f in critical_uncertainties],
            "test_verification": test_verification,
            "telemetry": telemetry
        }
    else:
        telemetry["status"] = "ROLLED_BACK" if decouple_res.get("rolled_back") else "PREFLIGHT_BLOCKED"
        session_id = record_session(
            command="workflow",
            task=task,
            project_root=root_abs,
            success=False,
            duration_ms=total_time_ms,
            mode="dry_run" if dry_run else "refactor",
            target_files=[f for f in [from_file, to_file] if f],
            target_symbols=resolved_symbols,
            estimated_tokens=plan_tokens + diff_tokens,
            test_run=test_verification,
            error=decouple_res.get("error"),
        )
        return {
            "session_id": session_id,
            "success": False,
            "pipeline": "full_refactor",
            "task": task,
            "stages_executed": stages_executed,
            "error": decouple_res.get("error"),
            "rolled_back": decouple_res.get("rolled_back", False),
            "circular_hazards": decouple_res.get("circular_hazards", []),
            "global_state_hazards": decouple_res.get("global_state_hazards", []),
            "state_desync_hazards": decouple_res.get("state_desync_hazards", []),
            "framework_hazards": decouple_res.get("framework_hazards", []),
            "secret_warnings": decouple_res.get("secret_warnings", []),
            "minimal_safe_closure": decouple_res.get("minimal_safe_closure", []),
            "remediation": decouple_res.get("remediation", []),
            "test_verification": test_verification,
            "uncertainty_findings": [f.to_dict() for f in findings],
            "telemetry": telemetry
        }
