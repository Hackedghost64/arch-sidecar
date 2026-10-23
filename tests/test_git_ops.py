import os
import subprocess
import pytest
from arch_sidecar.git_ops import (
    is_git_repository,
    get_git_root,
    get_staged_files,
    get_working_tree_modified_files,
    audit_git_changeset,
    analyze_git_change_impact,
    install_git_hook,
    uninstall_git_hook,
)


def test_is_git_repository():
    assert is_git_repository(".") is True
    root = get_git_root(".")
    assert root is not None
    assert os.path.isdir(os.path.join(root, ".git"))


def test_git_hook_lifecycle(tmp_path):
    # Initialize a temporary git repository
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "TestUser"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(tmp_path), check=True)

    # 1. Install Hook
    ok, msg = install_git_hook(str(tmp_path))
    assert ok is True
    hook_file = tmp_path / ".git" / "hooks" / "pre-commit"
    assert hook_file.is_file()
    assert os.access(str(hook_file), os.X_OK)
    assert "Devrith Pre-Commit Guard" in hook_file.read_text()

    # 2. Uninstall Hook
    uninstalled, un_msg = uninstall_git_hook(str(tmp_path))
    assert uninstalled is True
    assert not hook_file.exists()


def test_audit_git_changeset_staged(tmp_path):
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "TestUser"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(tmp_path), check=True)

    bad_file = tmp_path / "bad.py"
    bad_file.write_text("""
def buggy_func(x=[]):
    try:
        do_work()
    except Exception:
        pass
""")

    # Stage the file
    subprocess.run(["git", "add", "bad.py"], cwd=str(tmp_path), check=True)

    staged = get_staged_files(str(tmp_path))
    assert len(staged) == 1
    assert os.path.basename(staged[0]) == "bad.py"

    report = audit_git_changeset(str(tmp_path), staged_only=True, strict=True)
    assert report.total_files == 1
    assert report.error_count >= 2
    assert report.clean is False


def test_analyze_git_change_impact(tmp_path):
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "TestUser"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(tmp_path), check=True)

    f1 = tmp_path / "core.py"
    f1.write_text("def helper(): return 42\n")
    f2 = tmp_path / "app.py"
    f2.write_text("import core\ndef main(): return core.helper()\n")

    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True)

    # Modify core.py
    f1.write_text("def helper(): return 100\n")

    impact = analyze_git_change_impact(str(tmp_path))
    assert impact["success"] is True
    assert "core.py" in impact["modified_files"]
