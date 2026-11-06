import json
import subprocess
import sys
from unittest.mock import patch, MagicMock

import pytest

import arch_sidecar
from arch_sidecar.cli import (
    parse_semver,
    _clean_tag_ref,
    parse_git_tags,
    fetch_remote_tags,
    get_update_status,
    find_local_repo_root,
    run_self_update,
    main,
)


def test_package_version_is_0_4_0():
    assert arch_sidecar.__version__ == "0.4.0"
    from arch_sidecar.mcp_server import SERVER_VERSION
    assert SERVER_VERSION == "0.4.0"


def test_parse_semver():
    assert parse_semver("v0.1.0") == (0, 1, 0)
    assert parse_semver("0.4.0") == (0, 4, 0)
    assert parse_semver("v1.2.3") == (1, 2, 3)
    assert parse_semver("v0.4.0-rc1") == (0, 4, 0, 1)

    tags = ["v0.3.0", "v0.1.0", "v0.4.0", "v0.2.1"]
    sorted_tags = sorted(tags, key=parse_semver)
    assert sorted_tags == ["v0.1.0", "v0.2.1", "v0.3.0", "v0.4.0"]


def test_clean_tag_ref():
    assert _clean_tag_ref("1234\trefs/tags/v0.4.0") == "v0.4.0"
    assert _clean_tag_ref("1234\trefs/tags/v0.4.0^{}") == "v0.4.0"
    assert _clean_tag_ref("1234\trefs/heads/main") is None
    assert _clean_tag_ref("") is None


def test_parse_git_tags():
    raw_output = """
    962f924f3460 refs/tags/v0.1.0
    1a2b3c4d5e6f refs/tags/v0.2.0
    1a2b3c4d5e6f refs/tags/v0.2.0^{}
    a1b2c3d4e5f6 refs/tags/v0.4.0
    f6e5d4c3b2a1 refs/tags/v0.3.0
    """
    tags = parse_git_tags(raw_output)
    assert tags == ["v0.1.0", "v0.2.0", "v0.3.0", "v0.4.0"]

    # Empty output
    assert parse_git_tags("") == []
    assert parse_git_tags("no tags here") == []


def test_fetch_remote_tags_success():
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = "abc refs/tags/v0.3.0\ndef refs/tags/v0.4.0\n"

    with patch("subprocess.run", return_value=fake_proc) as mock_run:
        tags = fetch_remote_tags()
        assert tags == ["v0.3.0", "v0.4.0"]
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert args[0][:3] == ["git", "ls-remote", "--tags"]


def test_fetch_remote_tags_failure():
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="git", timeout=10)):
        tags = fetch_remote_tags()
        assert tags == []


def test_get_update_status_no_update():
    with patch("arch_sidecar.cli.fetch_remote_tags", return_value=["v0.3.0", "v0.4.0"]):
        status = get_update_status()
        assert status["current_version"] == "0.4.0"
        assert status["latest_version"] == "0.4.0"
        assert status["update_available"] is False
        assert status["remote_reachable"] is True


def test_get_update_status_update_available():
    with patch("arch_sidecar.cli.fetch_remote_tags", return_value=["v0.3.0", "v0.5.0"]):
        status = get_update_status()
        assert status["current_version"] == "0.4.0"
        assert status["latest_version"] == "0.5.0"
        assert status["update_available"] is True
        assert status["remote_reachable"] is True


def test_get_update_status_offline():
    with patch("arch_sidecar.cli.fetch_remote_tags", return_value=[]):
        status = get_update_status()
        assert status["current_version"] == "0.4.0"
        assert status["latest_version"] == "0.4.0"
        assert status["update_available"] is False
        assert status["remote_reachable"] is False


def test_run_self_update_check_only(capsys):
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = "abc refs/tags/v0.5.0\n"

    with patch("subprocess.run", return_value=fake_proc):
        # Plain terminal output
        res = run_self_update(check_only=True, as_json=False)
        captured = capsys.readouterr()
        assert "Devrith Self-Update Check" in captured.out
        assert "Update Available: Yes" in captured.out
        assert res["update_available"] is True
        assert res["latest_version"] == "0.5.0"

        # JSON output
        res_json = run_self_update(check_only=True, as_json=True)
        captured_json = capsys.readouterr()
        payload = json.loads(captured_json.out)
        assert payload["update_available"] is True
        assert payload["latest_version"] == "0.5.0"
        assert res_json == payload


def test_run_self_update_local_git_success(capsys):
    # Mock offline tag discovery so latest_version is 0.4.0
    with patch("arch_sidecar.cli.fetch_remote_tags", return_value=["v0.4.0"]):
        with patch("arch_sidecar.cli.find_local_repo_root", return_value="/mock/repo"):
            with patch("arch_sidecar.cli.install_via_local_git", return_value=(True, "Local git pull updated")):
                res = run_self_update(check_only=False, as_json=False)
                captured = capsys.readouterr()
                assert "✅ [Devrith Self-Update] Local git pull updated" in captured.out
                assert res["success"] is True


def test_run_self_update_pip_remote_with_tag(capsys):
    with patch("arch_sidecar.cli.fetch_remote_tags", return_value=["v0.4.0"]):
        with patch("arch_sidecar.cli.find_local_repo_root", return_value=None):
            with patch("arch_sidecar.cli.install_via_pip", return_value=(True, "Installed git URL")) as mock_pip:
                res = run_self_update(check_only=False, tag="v0.5.0", as_json=True)
                captured = capsys.readouterr()
                payload = json.loads(captured.out)
                assert payload["success"] is True
                assert payload["target_version"] == "v0.5.0"
                mock_pip.assert_called_once_with("git+https://github.com/Hackedghost64/arch-sidecar.git@v0.5.0")


def test_run_self_update_failure(capsys):
    with patch("arch_sidecar.cli.fetch_remote_tags", return_value=["v0.4.0"]):
        with patch("arch_sidecar.cli.find_local_repo_root", return_value=None):
            with patch("arch_sidecar.cli.install_via_pip", return_value=(False, "Connection refused")):
                res = run_self_update(check_only=False, as_json=False)
                captured = capsys.readouterr()
                assert "❌ [Devrith Self-Update] Connection refused" in captured.err
                assert res["success"] is False


def test_cli_update_command(capsys):
    with patch.object(sys, "argv", ["arch-sidecar", "update", "--check", "--json"]):
        with patch("arch_sidecar.cli.fetch_remote_tags", return_value=["v0.4.0"]):
            main()
            captured = capsys.readouterr()
            payload = json.loads(captured.out)
            assert payload["current_version"] == "0.4.0"
            assert payload["update_available"] is False


def test_cli_version_flag(capsys):
    with patch.object(sys, "argv", ["arch-sidecar", "--version"]):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "arch-sidecar 0.4.0 (Devrith Engine)" in captured.out
