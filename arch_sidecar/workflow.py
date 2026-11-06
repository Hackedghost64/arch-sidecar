import os
import re
import time
import logging
from typing import Dict, List, Optional, Set, Any, Tuple

from .planner import plan_adaptive_retrieval, RetrievalStrategy
from .uncertainty import scan_file_uncertainty, ConfidenceLevel
from .decouple import prefactor_decouple
from .utils import estimate_tokens
from .session_logger import record_session

logger = logging.getLogger(__name__)

REFACTOR_KEYWORDS = frozenset({"decouple", "extract", "move", "split", "refactor into", "separate"})


class WorkflowStage:
    RETRIEVAL_PLAN = "retrieval_plan"
    UNCERTAINTY_AUDIT = "uncertainty_audit"
    DEPENDENCY_PREFLIGHT = "dependency_preflight"
    TRANSFORMATION = "transformation"
    VERIFICATION = "verification"


def _matches_file(candidate_file: str, target_norm: str, target_base: str) -> bool:
    c_norm = os.path.normpath(candidate_file)
    return c_norm == target_norm or os.path.basename(c_norm) == target_base


def _is_exact_match(reason: str) -> bool:
    return "exact name match" in reason or "case-insensitive word match" in reason


def _resolve_target_symbols(
    symbols: Optional[List[str]],
    from_file: Optional[str],
    top_candidates: List[Dict[str, Any]],
    default_symbols: List[str],
) -> List[str]:
    if symbols:
        return list(symbols)
    if not from_file or not top_candidates:
        return list(default_symbols)

    target_norm = os.path.normpath(from_file)
    target_base = os.path.basename(target_norm)
    file_candidates = [
        c for c in top_candidates
        if _matches_file(c.get("file", ""), target_norm, target_base)
    ]
    exact_matches = [c for c in file_candidates if _is_exact_match(c.get("reason", ""))]
    chosen = exact_matches or file_candidates
    return list(dict.fromkeys(c["name"] for c in chosen))


def _resolve_to_file(to_file: Optional[str], task: str) -> Optional[str]:
    if to_file:
        return to_file
    to_match = re.search(r"\b(?:into|to)\s+([\w\-./]+\.py)\b", task, re.IGNORECASE)
    if to_match:
        return to_match.group(1)
    return None


def _is_refactor_task(task: str, has_explicit_targets: bool) -> bool:
    task_lower = task.lower()
    has_keyword = any(kw in task_lower for kw in REFACTOR_KEYWORDS)
    return has_keyword or has_explicit_targets


def _resolve_initial_from_file(
    from_file: Optional[str],
    top_candidates: List[Dict[str, Any]],
) -> Optional[str]:
    if from_file:
        return from_file
    if top_candidates:
        return top_candidates[0].get("file")
    return None


def _has_all_targets(from_f: Optional[str], to_f: Optional[str], symbols: Any) -> bool:
    if not from_f:
        return False
    if not to_f:
        return False
    return bool(symbols)


def _should_bypass(is_refactor: bool, explicit_refactor: bool, recommendation: Optional[str]) -> bool:
    if not is_refactor:
        return False
    if explicit_refactor:
        return False
    return recommendation == "BYPASS_DECOUPLING"


def _build_bypass_response(
    task: str,
    root_abs: str,
    from_file: Optional[str],
    resolved_symbols: List[str],
    plan_res: Dict[str, Any],
    elapsed_ms: float,
) -> Dict[str, Any]:
    plan_tokens = plan_res["estimated_tokens"]
    telemetry = {
        "tokens_by_stage": {WorkflowStage.RETRIEVAL_PLAN: plan_tokens},
        "latency_ms_by_stage": {WorkflowStage.RETRIEVAL_PLAN: elapsed_ms},
        "status": "BYPASS_RECOMMENDED",
        "total_latency_ms": elapsed_ms,
        "total_tokens": plan_tokens,
    }
    session_id = record_session(
        command="workflow",
        task=task,
        project_root=root_abs,
        success=True,
        duration_ms=elapsed_ms,
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
        "stages_executed": [WorkflowStage.RETRIEVAL_PLAN],
        "strategy": plan_res["strategy"],
        "strategy_reason": plan_res["strategy_reason"],
        "recommendation": "BYPASS_DECOUPLING",
        "context": plan_res["context"],
        "telemetry": telemetry,
        "message": (
            "Devrith Recommendation: Target file is compact. Direct file modification "
            "is more token-efficient than automated AST codemods. Decoupling bypassed."
        ),
    }


def _build_retrieval_response(
    task: str,
    root_abs: str,
    from_file: Optional[str],
    resolved_symbols: List[str],
    plan_res: Dict[str, Any],
    elapsed_ms: float,
) -> Dict[str, Any]:
    plan_tokens = plan_res["estimated_tokens"]
    telemetry = {
        "tokens_by_stage": {WorkflowStage.RETRIEVAL_PLAN: plan_tokens},
        "latency_ms_by_stage": {WorkflowStage.RETRIEVAL_PLAN: elapsed_ms},
        "status": "COMPLETED_RETRIEVAL",
        "total_latency_ms": elapsed_ms,
        "total_tokens": plan_tokens,
    }
    session_id = record_session(
        command="workflow",
        task=task,
        project_root=root_abs,
        success=True,
        duration_ms=elapsed_ms,
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
        "stages_executed": [WorkflowStage.RETRIEVAL_PLAN],
        "strategy": plan_res["strategy"],
        "strategy_reason": plan_res["strategy_reason"],
        "context": plan_res["context"],
        "telemetry": telemetry,
        "message": "Task evaluated as informational/inspection. Context plan generated without code modification.",
    }


def _run_uncertainty_stage(
    root_abs: str,
    from_file: str,
    resolved_symbols: List[str],
) -> Tuple[List[Any], List[Any], float]:
    t1 = time.perf_counter()
    from_file_full = os.path.join(root_abs, from_file)
    findings = scan_file_uncertainty(from_file_full, from_file)
    audit_time_ms = round((time.perf_counter() - t1) * 1000, 2)
    critical = [
        f for f in findings
        if f.confidence == ConfidenceLevel.UNKNOWN and any(sym in f.code for sym in resolved_symbols)
    ]
    return findings, critical, audit_time_ms


def _execute_decouple(
    root_abs: str,
    refactor_args: Dict[str, Any],
) -> Tuple[Dict[str, Any], float]:
    t2 = time.perf_counter()
    res = prefactor_decouple(
        symbol=refactor_args["symbols"],
        from_file=refactor_args["from_file"],
        to_file=refactor_args["to_file"],
        project_root=root_abs,
        run_tests=refactor_args.get("test_command"),
        dry_run=refactor_args.get("dry_run", False),
        share_state=refactor_args.get("share_state", False),
    )
    transform_time_ms = round((time.perf_counter() - t2) * 1000, 2)
    return res, transform_time_ms


def _determine_status(
    is_success: bool,
    test_verif: Optional[Dict[str, Any]],
    rolled_back: bool,
) -> str:
    if is_success:
        if test_verif and test_verif.get("passed"):
            return "VERIFIED"
        return "TRANSFORMED"
    if rolled_back:
        return "ROLLED_BACK"
    return "PREFLIGHT_BLOCKED"


def _build_stages_list(dry_run: bool, has_test_verification: bool) -> List[str]:
    stages = [
        WorkflowStage.RETRIEVAL_PLAN,
        WorkflowStage.UNCERTAINTY_AUDIT,
        WorkflowStage.DEPENDENCY_PREFLIGHT,
    ]
    if not dry_run:
        stages.append(WorkflowStage.TRANSFORMATION)
    if has_test_verification:
        stages.append(WorkflowStage.VERIFICATION)
    return stages


def _record_refactor_session(
    task_info: Dict[str, Any],
    refactor_params: Dict[str, Any],
    session_metrics: Dict[str, Any],
) -> str:
    is_success = session_metrics["is_success"]
    mode = "dry_run" if refactor_params.get("dry_run") else "refactor"
    error_msg = session_metrics.get("error") if not is_success else None
    target_files = [f for f in [refactor_params["from_file"], refactor_params["to_file"]] if f]

    return record_session(
        command="workflow",
        task=task_info["task"],
        project_root=task_info["root_abs"],
        success=is_success,
        duration_ms=session_metrics["total_time_ms"],
        mode=mode,
        target_files=target_files,
        target_symbols=refactor_params["symbols"],
        estimated_tokens=session_metrics["total_tokens"],
        test_run=session_metrics.get("test_verification"),
        error=error_msg,
    )


def _build_success_payload(
    task_env: Dict[str, Any],
    decouple_res: Dict[str, Any],
    findings: List[Any],
    critical_uncertainties: List[Any],
    session_id: str,
) -> Dict[str, Any]:
    resolved_symbols = task_env["resolved_symbols"]
    return {
        "session_id": session_id,
        "success": True,
        "pipeline": "full_refactor",
        "task": task_env["task"],
        "stages_executed": task_env["stages_executed"],
        "extracted_symbols": decouple_res.get("extracted_symbols", resolved_symbols),
        "from_file": task_env["from_file"],
        "to_file": task_env["to_file"],
        "import_target": decouple_res.get("import_target"),
        "share_state_bound": decouple_res.get("share_state_bound", []),
        "ruff_gate": decouple_res.get("ruff_gate"),
        "diff": decouple_res.get("diff", ""),
        "secret_warnings": decouple_res.get("secret_warnings", []),
        "uncertainty_findings": [f.to_dict() for f in findings],
        "critical_uncertainties": [f.to_dict() for f in critical_uncertainties],
        "test_verification": decouple_res.get("test_verification"),
        "telemetry": task_env["telemetry"],
    }


def _build_failure_payload(
    task_env: Dict[str, Any],
    decouple_res: Dict[str, Any],
    findings: List[Any],
    session_id: str,
) -> Dict[str, Any]:
    return {
        "session_id": session_id,
        "success": False,
        "pipeline": "full_refactor",
        "task": task_env["task"],
        "stages_executed": task_env["stages_executed"],
        "error": decouple_res.get("error"),
        "rolled_back": decouple_res.get("rolled_back", False),
        "circular_hazards": decouple_res.get("circular_hazards", []),
        "global_state_hazards": decouple_res.get("global_state_hazards", []),
        "state_desync_hazards": decouple_res.get("state_desync_hazards", []),
        "framework_hazards": decouple_res.get("framework_hazards", []),
        "secret_warnings": decouple_res.get("secret_warnings", []),
        "minimal_safe_closure": decouple_res.get("minimal_safe_closure", []),
        "remediation": decouple_res.get("remediation", []),
        "test_verification": decouple_res.get("test_verification"),
        "uncertainty_findings": [f.to_dict() for f in findings],
        "telemetry": task_env["telemetry"],
    }


def _run_refactor_pipeline(
    task_info: Dict[str, Any],
    refactor_params: Dict[str, Any],
    plan_tokens: int,
    start_total_time: float,
) -> Dict[str, Any]:
    root_abs = task_info["root_abs"]
    from_file = refactor_params["from_file"]
    resolved_symbols = refactor_params["symbols"]

    findings, critical, audit_ms = _run_uncertainty_stage(root_abs, from_file, resolved_symbols)
    decouple_res, transform_ms = _execute_decouple(root_abs, refactor_params)

    test_verif = decouple_res.get("test_verification")
    stages_executed = _build_stages_list(bool(refactor_params.get("dry_run")), bool(test_verif))

    total_time_ms = round((time.perf_counter() - start_total_time) * 1000, 2)
    diff_str = decouple_res.get("diff", "")
    diff_tokens = estimate_tokens(diff_str) if diff_str else 0
    total_tokens = plan_tokens + diff_tokens

    is_success = bool(decouple_res.get("success"))
    rolled_back = bool(decouple_res.get("rolled_back"))
    status_label = _determine_status(is_success, test_verif, rolled_back)

    telemetry = {
        "tokens_by_stage": {
            WorkflowStage.RETRIEVAL_PLAN: plan_tokens,
            WorkflowStage.TRANSFORMATION: diff_tokens,
        },
        "latency_ms_by_stage": {
            WorkflowStage.RETRIEVAL_PLAN: task_info["planning_time_ms"],
            WorkflowStage.UNCERTAINTY_AUDIT: audit_ms,
            WorkflowStage.TRANSFORMATION: transform_ms,
        },
        "status": status_label,
        "total_latency_ms": total_time_ms,
        "total_tokens": total_tokens,
    }
    if test_verif:
        telemetry["latency_ms_by_stage"][WorkflowStage.VERIFICATION] = test_verif.get("duration_ms", 0)

    session_metrics = {
        "is_success": is_success,
        "total_time_ms": total_time_ms,
        "total_tokens": total_tokens,
        "test_verification": test_verif,
        "error": decouple_res.get("error"),
    }
    session_id = _record_refactor_session(task_info, refactor_params, session_metrics)

    task_env = {
        "task": task_info["task"],
        "stages_executed": stages_executed,
        "from_file": from_file,
        "to_file": refactor_params["to_file"],
        "resolved_symbols": resolved_symbols,
        "telemetry": telemetry,
    }

    if is_success:
        return _build_success_payload(task_env, decouple_res, findings, critical, session_id)
    return _build_failure_payload(task_env, decouple_res, findings, session_id)


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
    """
    start_total_time = time.perf_counter()
    root_abs = os.path.abspath(project_root)

    t0 = time.perf_counter()
    plan_res = plan_adaptive_retrieval(
        task=task,
        root_dir=root_abs,
        target_symbols=symbols,
        token_budget=token_budget,
    )
    planning_time_ms = round((time.perf_counter() - t0) * 1000, 2)
    plan_tokens = plan_res["estimated_tokens"]

    top_candidates = [c for c in plan_res.get("ranking", []) if c.get("included")]
    resolved_from = _resolve_initial_from_file(from_file, top_candidates)
    resolved_symbols = _resolve_target_symbols(
        symbols, resolved_from, top_candidates, plan_res.get("selected_symbols", [])
    )
    resolved_to = _resolve_to_file(to_file, task)

    explicit_refactor = _has_all_targets(from_file, to_file, symbols)
    targets_ready = _has_all_targets(resolved_from, resolved_to, resolved_symbols)
    is_refactor = _is_refactor_task(task, targets_ready)

    total_time_ms = round((time.perf_counter() - start_total_time) * 1000, 2)

    if _should_bypass(is_refactor, explicit_refactor, plan_res.get("recommendation")):
        return _build_bypass_response(task, root_abs, resolved_from, resolved_symbols, plan_res, total_time_ms)

    if not is_refactor or not targets_ready:
        return _build_retrieval_response(task, root_abs, resolved_from, resolved_symbols, plan_res, total_time_ms)

    task_info = {
        "task": task,
        "root_abs": root_abs,
        "planning_time_ms": planning_time_ms,
    }
    refactor_params = {
        "from_file": resolved_from,
        "to_file": resolved_to,
        "symbols": resolved_symbols,
        "test_command": test_command,
        "dry_run": dry_run,
        "share_state": share_state,
    }
    return _run_refactor_pipeline(task_info, refactor_params, plan_tokens, start_total_time)
