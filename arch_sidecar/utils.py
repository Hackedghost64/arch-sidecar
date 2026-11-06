import os
import re
import json
import shutil
import subprocess
import logging
from typing import Set, Optional, List, Dict, Any

logger = logging.getLogger(__name__)

# Default directories that contain generated assets, caches, dependencies, or dist bundles
DEFAULT_IGNORED_DIRS: Set[str] = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "venv",
    ".venv",
    "env",
    ".env",
    "__pycache__",
    "build",
    "dist",
    ".dart_tool",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".next",
    ".nuxt",
    "out",
    "target",
    "coverage",
    ".idea",
    ".vscode",
    ".agents",
    ".superpowers",
    "egg-info",
    "site-packages",
    "tmp",
    "temp",
}

# Ignored extensions or filenames
DEFAULT_IGNORED_EXTENSIONS: Set[str] = {
    ".pyc", ".pyo", ".pyd",
    ".min.js", ".min.css",
    ".bundle.js", ".chunk.js",
    ".map",
    ".lock",
    ".apk", ".aab", ".dex", ".ipa",
    ".exe", ".dll", ".so", ".dylib", ".bin",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".webp",
    ".mp3", ".mp4", ".wav", ".ogg", ".webm",
    ".ttf", ".woff", ".woff2", ".eot",
    ".zip", ".tar", ".gz", ".7z", ".rar",
    ".pdf",
}

DEFAULT_IGNORED_FILES: Set[str] = {
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.lock",
    "poetry.lock",
    "composer.lock",
}


def _is_hidden_dir(part: str) -> bool:
    return part.startswith(".") and part not in {".", "..", ".github"}


def _is_ignored_dir_part(part: str, is_parent: bool, ignored_dirs: Set[str]) -> bool:
    if part in ignored_dirs or part.endswith(".egg-info"):
        return True
    if is_parent and _is_hidden_dir(part):
        return True
    return False


def _is_minified_filename(filename: str) -> bool:
    if filename.endswith(".min.js") or filename.endswith(".min.css"):
        return True
    return ".bundle." in filename


def _is_ignored_filename(filename: str) -> bool:
    if filename in DEFAULT_IGNORED_FILES:
        return True
    _, ext = os.path.splitext(filename)
    if ext.lower() in DEFAULT_IGNORED_EXTENSIONS:
        return True
    return _is_minified_filename(filename)


def is_ignored_path(rel_path: str, ignored_dirs: Set[str] = DEFAULT_IGNORED_DIRS) -> bool:
    """Checks whether a relative file or directory path touches an ignored folder or pattern."""
    normalized = rel_path.replace("\\", "/").strip("/")
    parts = normalized.split("/")

    total_parts = len(parts)
    for idx, part in enumerate(parts):
        is_parent = total_parts > 1 and idx < total_parts - 1
        if _is_ignored_dir_part(part, is_parent, ignored_dirs):
            return True

    return _is_ignored_filename(parts[-1])


def _has_long_line(file_obj: Any, max_lines: int) -> bool:
    for i, line in enumerate(file_obj):
        if i >= max_lines:
            break
        if len(line) > 800:
            return True
    return False


def is_minified_or_binary(filepath: str, max_check_lines: int = 15) -> bool:
    """Detects if a file is minified code (very long lines) or binary content."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            return _has_long_line(f, max_check_lines)
    except (OSError, UnicodeDecodeError) as e:
        logger.debug("Failed reading %s: %s", filepath, e)
        return True


def estimate_tokens(text: str) -> int:
    """Quick and reliable heuristic for token count (~4 characters per token)."""
    return max(1, len(text) // 4)


def find_ruff_binary() -> Optional[str]:
    """Locates the ruff binary on PATH or in ~/.local/bin/ruff."""
    found = shutil.which("ruff")
    if found:
        return found
    fallback = os.path.expanduser("~/.local/bin/ruff")
    if os.path.isfile(fallback) and os.access(fallback, os.X_OK):
        return fallback
    return None


def find_oxlint_binary() -> Optional[str]:
    """Locates the oxlint (Rust TS/JS linter) binary on PATH."""
    found = shutil.which("oxlint")
    if found:
        return found
    fallback = os.path.expanduser("~/.nvm/versions/node/v22.22.2/bin/oxlint")
    if os.path.isfile(fallback) and os.access(fallback, os.X_OK):
        return fallback
    return None


def find_biome_binary() -> Optional[str]:
    """Locates the biome (Rust TS/JS toolchain) binary on PATH."""
    found = shutil.which("biome")
    if found:
        return found
    fallback = os.path.expanduser("~/.nvm/versions/node/v22.22.2/bin/biome")
    if os.path.isfile(fallback) and os.access(fallback, os.X_OK):
        return fallback
    return None


def _extract_oxlint_span(labels: Any) -> Dict[str, Any]:
    if labels and isinstance(labels, list) and isinstance(labels[0], dict):
        return labels[0].get("span", {}) or {}
    return {}


def _parse_oxlint_entry(diag: Dict[str, Any]) -> Dict[str, Any]:
    labels = diag.get("labels", [])
    span = _extract_oxlint_span(labels)
    code = diag.get("code") or "oxlint"
    msg = diag.get("message") or ""
    sev = diag.get("severity") or "warning"
    return {
        "code": code,
        "message": msg,
        "severity": sev.upper(),
        "line": span.get("line", 1),
        "column": span.get("column", 1),
        "help": diag.get("help", ""),
    }


def _parse_oxlint_data(raw_stdout: str) -> List[Dict[str, Any]]:
    if not raw_stdout.strip():
        return []
    data = json.loads(raw_stdout)
    if not isinstance(data, dict):
        return []
    diagnostics = data.get("diagnostics", [])
    return [_parse_oxlint_entry(diag) for diag in diagnostics if isinstance(diag, dict)]


def run_oxlint_check(file_path: str, cwd: str = ".") -> List[Dict[str, Any]]:
    """
    Executes oxlint on a TypeScript/JavaScript file and parses JSON diagnostics.
    """
    oxlint_bin = find_oxlint_binary()
    if not oxlint_bin or not os.path.isfile(file_path):
        return []

    try:
        res = subprocess.run(
            [oxlint_bin, "--format", "json", file_path],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return _parse_oxlint_data(res.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as e:
        logger.debug("Oxlint execution failed on %s: %s", file_path, e)
        return []


def _extract_biome_line(location: Any) -> int:
    if isinstance(location, dict):
        path_info = location.get("path")
        if isinstance(path_info, dict):
            return path_info.get("line", 1)
    return 1


def _parse_biome_entry(diag: Dict[str, Any]) -> Dict[str, Any]:
    location = diag.get("location", {})
    line_num = _extract_biome_line(location)
    code = diag.get("category") or "biome"
    msg = diag.get("description") or ""
    sev = diag.get("severity") or "warning"
    return {
        "code": code,
        "message": msg,
        "severity": sev.upper(),
        "line": line_num,
        "column": 1,
    }


def _parse_biome_data(raw_stdout: str) -> List[Dict[str, Any]]:
    if not raw_stdout.strip():
        return []
    data = json.loads(raw_stdout)
    if not isinstance(data, dict):
        return []
    diagnostics = data.get("diagnostics", [])
    return [_parse_biome_entry(diag) for diag in diagnostics if isinstance(diag, dict)]


def run_biome_check(file_path: str, cwd: str = ".") -> List[Dict[str, Any]]:
    """
    Executes biome lint on a TypeScript/JavaScript file and parses JSON diagnostics.
    """
    biome_bin = find_biome_binary()
    if not biome_bin or not os.path.isfile(file_path):
        return []

    try:
        res = subprocess.run(
            [biome_bin, "lint", "--reporter", "json", file_path],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return _parse_biome_data(res.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as e:
        logger.debug("Biome execution failed on %s: %s", file_path, e)
        return []


def run_ruff_autofix(file_paths: List[str], project_root: str = ".") -> bool:
    """
    Executes ruff import cleanup and formatting deterministically ($0.00 LLM cost):
    1. `ruff check --fix --select I,F401`: cleans dead imports and organizes isort imports.
    2. `ruff format`: standardizes code layout.
    """
    ruff_bin = find_ruff_binary()
    if not ruff_bin:
        return False

    success = True
    for p in file_paths:
        if os.path.isfile(p):
            try:
                subprocess.run(
                    [ruff_bin, "check", "--fix", "--select=I,F401", p],
                    cwd=project_root,
                    capture_output=True,
                    timeout=5,
                )
                subprocess.run(
                    [ruff_bin, "format", p],
                    cwd=project_root,
                    capture_output=True,
                    timeout=5,
                )
            except (OSError, subprocess.SubprocessError) as e:
                logger.debug("Ruff autofix failed on %s: %s", p, e)
                success = False
    return success
