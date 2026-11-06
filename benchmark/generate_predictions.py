import ast
import glob
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

BENCHMARK_DIR = os.path.abspath(os.path.dirname(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(BENCHMARK_DIR, ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from arch_sidecar.decouple import prefactor_decouple

SEEN_SYMBOLS = {
    'click': {'echo', 'Context', 'ShellComplete', '_split_opt', 'get_current_context', 'isatty', 'wrap_text'},
    'requests': {'Session', 'SessionRedirectMixin', '_basic_auth_str', 'merge_setting', 'super_len', 'to_key_val_list', 'where'},
    'django': {'AdminSite', 'QuerySet', 'RANDOM_STRING_CHARS', 'RegexValidator', 'ReverseManyToOneDescriptor', 'capfirst', 'get_random_string', 'slugify'}
}


def _is_valid_constant_name(name: str, seen: Set[str]) -> bool:
    return bool(name and not name.startswith('_') and name.isupper() and name not in seen)


def _extract_assign_candidates(node: ast.AST, rel: str, seen: Set[str]) -> List[Tuple[str, str, str]]:
    results: List[Tuple[str, str, str]] = []
    if isinstance(node, ast.Assign):
        for t in node.targets:
            target_id = getattr(t, "id", "")
            if isinstance(t, ast.Name) and _is_valid_constant_name(target_id, seen):
                results.append((rel, target_id, 'Constant'))
    elif isinstance(node, ast.AnnAssign):
        target = node.target
        target_name = getattr(target, "id", "")
        if isinstance(target, ast.Name) and _is_valid_constant_name(target_name, seen):
            results.append((rel, target_name, 'Constant'))
    return results


def _extract_file_candidates(path: str, repo_path: str, seen: Set[str]) -> List[Tuple[str, str, str]]:
    rel = os.path.relpath(path, repo_path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except (SyntaxError, OSError) as e:
        logger.debug("Failed parsing %s: %s", path, e)
        return []

    candidates: List[Tuple[str, str, str]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
            if not name.startswith('_') and name not in seen:
                stype = 'Class' if isinstance(node, ast.ClassDef) else 'Function'
                candidates.append((rel, name, stype))
        else:
            candidates.extend(_extract_assign_candidates(node, rel, seen))
    return candidates


def _sample_candidates(candidates: List[Tuple[str, str, str]], max_count: Optional[int]) -> List[Tuple[str, str, str]]:
    if not max_count or len(candidates) <= max_count:
        return candidates
    step = len(candidates) / max_count
    return [candidates[int(i * step)] for i in range(max_count)]


def extract_candidates(repo_path: str, prefix: str, repo_name: str, max_count: Optional[int] = None) -> List[Tuple[str, str, str]]:
    candidates: List[Tuple[str, str, str]] = []
    seen = SEEN_SYMBOLS.get(repo_name, set())
    pattern = os.path.join(repo_path, prefix, '**/*.py')
    for p in sorted(glob.glob(pattern, recursive=True)):
        if '__pycache__' in p or 'test' in p:
            continue
        candidates.extend(_extract_file_candidates(p, repo_path, seen))
    return _sample_candidates(candidates, max_count)


def _collect_hazards(res: Dict[str, Any]) -> List[Any]:
    hazards: List[Any] = []
    for h_key in ('circular_hazards', 'global_state_hazards', 'state_desync_hazards', 'framework_hazards'):
        hazards.extend(res.get(h_key, []) or [])
    return hazards


def _classify_prediction(res: Dict[str, Any], hazards: List[Any]) -> Tuple[str, str]:
    err_str = res.get("error", "")
    if "Refactoring blocked" in err_str or len(hazards) > 0:
        reason = err_str if err_str else f"Detected {len(hazards)} architectural hazard(s)"
        return "REJECT", reason
    if res.get("success") is True:
        return "ACCEPT", "Safe DAG extraction closure verified"
    return "ERROR", err_str or "Unknown preflight error"


def _predict_candidate(repo_name: str, repo_path: str, candidate: Tuple[str, str, str]) -> Dict[str, Any]:
    rel_file, symbol_name, stype = candidate
    dir_name = os.path.dirname(rel_file)
    target_file = os.path.join(dir_name, f"_extracted_{symbol_name.lower()}.py")

    t0 = time.perf_counter()
    try:
        res = prefactor_decouple(
            symbol=symbol_name,
            from_file=rel_file,
            to_file=target_file,
            project_root=repo_path,
            dry_run=True,
            add_backward_compat_alias=True
        )
        dt_ms = (time.perf_counter() - t0) * 1000.0
        hazards = _collect_hazards(res)
        pred, reason = _classify_prediction(res, hazards)
    except Exception as e:
        logger.debug("prefactor_decouple prediction failed for %s: %s", symbol_name, e)
        dt_ms = (time.perf_counter() - t0) * 1000.0
        pred = "ERROR"
        hazards = []
        reason = str(e)

    return {
        "repo": repo_name,
        "from_file": rel_file,
        "to_file": target_file,
        "symbol": symbol_name,
        "symbol_type": stype,
        "predicted_label": pred,
        "prediction_reason": reason,
        "hazards_count": len(hazards),
        "hazards": hazards,
        "preflight_duration_ms": round(dt_ms, 2)
    }


def main():
    repos = {
        'flask': ('/tmp/bench_repos/flask', 'src/flask', None),
        'click': ('/tmp/bench_repos/click', 'src/click', 30),
        'requests': ('/tmp/bench_repos/requests', 'src/requests', 30),
    }

    manifest = {
        "metadata": {
            "freeze_commit": "509f131",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pre_registered_thresholds": {
                "recall_lower_bound_target": 0.70,
                "false_rejection_target": 0.10,
                "max_accepted_regressions": 0,
                "minimum_hazard_gate": 10,
                "inconclusive_rule": "If N_hazard < 10, recall is marked INCONCLUSIVE DUE TO INSUFFICIENT HAZARD BASE RATE"
            },
            "eval_datasets": {
                "headline": "flask (all 98 public symbols, 100% untuned)",
                "secondary": "click & requests (unseen symbols only, reported separately)",
                "speed_only": "django (time comparison only, slow test suite)"
            }
        },
        "predictions": []
    }

    print("Generating Devrith preflight predictions at freeze commit 509f131...")

    for repo_name, (repo_path, prefix, max_count) in repos.items():
        candidates = extract_candidates(repo_path, prefix, repo_name, max_count)
        print(f"Loaded {len(candidates)} candidates for {repo_name}...")
        for cand in candidates:
            item = _predict_candidate(repo_name, repo_path, cand)
            manifest["predictions"].append(item)

    output_path = os.path.join(BENCHMARK_DIR, "predictions_509f131.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"Successfully generated {len(manifest['predictions'])} preflight predictions!")
    print(f"Manifest written to: {output_path}")


if __name__ == '__main__':
    main()
