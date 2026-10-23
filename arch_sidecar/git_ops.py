import os
import subprocess
import shutil
from typing import List, Dict, Set, Optional, Any, Tuple
from pathlib import Path

from .quality import audit_file, QualityIssue, QualityReport
from .guard import check_code_health
from .cluster import (
    build_repository_dependency_graph,
    partition_into_subsystems,
    predict_change_impact,
)


def is_git_repository(repo_path: str = ".") -> bool:
    """Checks whether the specified path is inside a Git working tree."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=3,
        )
        return res.returncode == 0 and res.stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def get_git_root(repo_path: str = ".") -> Optional[str]:
    """Returns absolute path to the top-level directory of the Git repository."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=3,
        )
        if res.returncode == 0 and res.stdout.strip():
            return os.path.abspath(res.stdout.strip())
        return None
    except (OSError, subprocess.SubprocessError):
        return None


def get_staged_files(repo_path: str = ".", extension: str = ".py") -> List[str]:
    """Returns list of staged files in git matching extension."""
    root = get_git_root(repo_path)
    if not root:
        return []

    try:
        res = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode != 0:
            return []

        out = []
        for line in res.stdout.splitlines():
            line = line.strip()
            if line and line.endswith(extension):
                full_p = os.path.join(root, line)
                if os.path.isfile(full_p):
                    out.append(full_p)
        return out
    except (OSError, subprocess.SubprocessError):
        return []


def get_working_tree_modified_files(repo_path: str = ".", extension: str = ".py") -> List[str]:
    """Returns all modified, staged, or untracked files in the working tree."""
    root = get_git_root(repo_path)
    if not root:
        return []

    try:
        res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode != 0:
            return []

        out = []
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            # status format: XY <path> or XY <orig> -> <path>
            parts = line.split(maxsplit=1)
            if len(parts) < 2:
                continue
            rel_path = parts[1]
            if " -> " in rel_path:
                rel_path = rel_path.split(" -> ")[1]

            if rel_path.endswith(extension):
                full_p = os.path.join(root, rel_path)
                if os.path.isfile(full_p):
                    out.append(full_p)
        return sorted(list(set(out)))
    except (OSError, subprocess.SubprocessError):
        return []


def get_branch_diff_files(base_branch: str = "main", repo_path: str = ".", extension: str = ".py") -> List[str]:
    """Returns all files changed between the current branch and base_branch."""
    root = get_git_root(repo_path)
    if not root:
        return []

    try:
        res = subprocess.run(
            ["git", "diff", f"{base_branch}...HEAD", "--name-only", "--diff-filter=ACM"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode != 0:
            # Fallback to single diff without 3 dots if branch ref is detached
            res = subprocess.run(
                ["git", "diff", base_branch, "--name-only", "--diff-filter=ACM"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if res.returncode != 0:
                return []

        out = []
        for line in res.stdout.splitlines():
            line = line.strip()
            if line and line.endswith(extension):
                full_p = os.path.join(root, line)
                if os.path.isfile(full_p):
                    out.append(full_p)
        return out
    except (OSError, subprocess.SubprocessError):
        return []


def _resolve_target_files(root: str, staged_only: bool, base_branch: Optional[str]) -> List[str]:
    """Helper to resolve target files based on git mode."""
    if base_branch:
        return get_branch_diff_files(base_branch, repo_path=root)
    if staged_only:
        return get_staged_files(repo_path=root)
    return get_working_tree_modified_files(repo_path=root)


def audit_git_changeset(
    repo_path: str = ".",
    staged_only: bool = False,
    base_branch: Optional[str] = None,
    strict: bool = False
) -> QualityReport:
    """
    Surgically audits only the files modified in Git (staged, working tree, or branch diff).
    Runs in sub-10ms because it skips untouched repository files.
    """
    root = get_git_root(repo_path) or repo_path
    files = _resolve_target_files(root, staged_only, base_branch)

    report = QualityReport()
    report.total_files = len(files)

    if not files:
        report.clean = True
        report.score = 100.0
        report.metrics = {
            "mode": "git_changeset",
            "files_evaluated": 0,
            "status": "No modified Python files in git changeset"
        }
        return report

    all_issues: List[QualityIssue] = []
    for fpath in files:
        all_issues.extend(audit_file(fpath))

    report.issues = all_issues
    report.total_issues = len(all_issues)
    report.error_count = sum(1 for i in all_issues if i.severity == "error")
    report.warning_count = sum(1 for i in all_issues if i.severity == "warning")

    penalty = (report.error_count * 10.0) + (report.warning_count * 2.0)
    penalty_per_file = penalty / report.total_files if report.total_files > 0 else 0.0
    report.score = max(0.0, min(100.0, 100.0 - penalty_per_file))
    report.clean = (report.total_issues == 0) if strict else (report.error_count == 0)

    category_counts = {
        "complexity": sum(1 for i in all_issues if i.category == "complexity"),
        "anti_pattern": sum(1 for i in all_issues if i.category == "anti_pattern"),
        "oop_cohesion": sum(1 for i in all_issues if i.category == "oop_cohesion"),
        "contract": sum(1 for i in all_issues if i.category == "contract"),
    }
    mode = "git_staged" if staged_only else ("git_branch" if base_branch else "git_working_tree")
    report.metrics = {
        "mode": mode,
        "categories": category_counts,
        "clean_files": report.total_files - len({i.file_path for i in all_issues}),
        "affected_files": len({i.file_path for i in all_issues}),
    }
    return report


def analyze_git_change_impact(
    repo_path: str = ".",
    staged_only: bool = False,
    base_branch: Optional[str] = None
) -> Dict[str, Any]:
    """
    Computes architectural blast radius and cluster impact for git-modified files.
    Identifies affected subsystems and recommends test suites across the repo.
    """
    root = get_git_root(repo_path) or repo_path

    if base_branch:
        files = get_branch_diff_files(base_branch, repo_path=root)
    elif staged_only:
        files = get_staged_files(repo_path=root)
    else:
        files = get_working_tree_modified_files(repo_path=root)

    if not files:
        return {
            "success": True,
            "modified_files": [],
            "affected_clusters": [],
            "transitive_blast_radius": [],
            "recommended_test_files": [],
            "risk_level": "LOW",
            "message": "No modified Python files in git changeset."
        }

    all_blast_radius: Set[str] = set()
    all_callers: Set[str] = set()
    all_affected_clusters: Set[str] = set()
    all_test_files: Set[str] = set()
    max_risk = "LOW"

    risk_hierarchy = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}

    for fpath in files:
        rel_f = os.path.relpath(fpath, root)
        impact = predict_change_impact(rel_f, root_dir=root)
        if not impact.get("success"):
            continue
        all_callers.update(impact.get("direct_callers", []))
        all_blast_radius.update(impact.get("transitive_blast_radius", []))
        all_affected_clusters.update(impact.get("affected_clusters", []))
        all_test_files.update(impact.get("recommended_test_files", []))
        risk_lvl = impact.get("risk_level", "LOW")
        if risk_hierarchy.get(risk_lvl, 1) > risk_hierarchy.get(max_risk, 1):
            max_risk = risk_lvl

    rel_modified = [os.path.relpath(f, root) for f in files]

    return {
        "success": True,
        "modified_files": rel_modified,
        "direct_callers": sorted(list(all_callers)),
        "transitive_blast_radius": sorted(list(all_blast_radius)),
        "affected_clusters": sorted(list(all_affected_clusters)),
        "recommended_test_files": sorted(list(all_test_files)),
        "risk_level": max_risk,
    }


HOOK_SCRIPT = """#!/usr/bin/env bash
# [Devrith Pre-Commit Guard]
# Intercepts circular imports, scope binding errors, silent exception swallows,
# and parameter mutations before git commit completes.

echo "🛡️  Running Devrith pre-commit architectural guard..."

# Run audit on staged files
python3 -m arch_sidecar.cli audit --staged
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    echo "❌ [Devrith Pre-Commit] Commit blocked: Staged files violate architectural or code craft standards."
    echo "💡 Fix the reported issues above or run 'arch-sidecar audit --staged' for details."
    exit 1
fi

echo "✅ [Devrith Pre-Commit] Staged files verified clean."
exit 0
"""


def install_git_hook(repo_path: str = ".") -> Tuple[bool, str]:
    """Installs a Devrith pre-commit hook in .git/hooks/pre-commit."""
    root = get_git_root(repo_path)
    if not root:
        return False, "Not inside a valid Git repository."

    hooks_dir = os.path.join(root, ".git", "hooks")
    if not os.path.isdir(hooks_dir):
        try:
            os.makedirs(hooks_dir, exist_ok=True)
        except OSError as e:
            return False, f"Could not create hooks directory: {e}"

    hook_path = os.path.join(hooks_dir, "pre-commit")

    # If hook already exists and is not devrith, backup
    if os.path.isfile(hook_path):
        with open(hook_path, "r", encoding="utf-8", errors="ignore") as f:
            existing = f.read()
        if "Devrith Pre-Commit Guard" not in existing:
            backup_path = os.path.join(hooks_dir, "pre-commit.backup")
            shutil.copy2(hook_path, backup_path)

    try:
        with open(hook_path, "w", encoding="utf-8") as f:
            f.write(HOOK_SCRIPT)
        # Make executable
        os.chmod(hook_path, 0o755)
        return True, f"Devrith pre-commit hook successfully installed at: {hook_path}"
    except Exception as e:
        return False, f"Failed to write hook file: {e}"


def uninstall_git_hook(repo_path: str = ".") -> Tuple[bool, str]:
    """Uninstalls Devrith pre-commit hook."""
    root = get_git_root(repo_path)
    if not root:
        return False, "Not inside a valid Git repository."

    hook_path = os.path.join(root, ".git", "hooks", "pre-commit")
    if not os.path.isfile(hook_path):
        return True, "No pre-commit hook was found."

    try:
        with open(hook_path, "r", encoding="utf-8", errors="ignore") as f:
            existing = f.read()

        if "Devrith Pre-Commit Guard" in existing:
            os.remove(hook_path)
            # Restore backup if exists
            backup = os.path.join(root, ".git", "hooks", "pre-commit.backup")
            if os.path.isfile(backup):
                shutil.move(backup, hook_path)
                return True, "Devrith hook removed, previous pre-commit hook restored."
            return True, "Devrith pre-commit hook uninstalled."
        else:
            return False, "Existing pre-commit hook was not installed by Devrith; left untouched."
    except Exception as e:
        return False, f"Failed to uninstall hook: {e}"
