import os
import re
import json
import shutil
import subprocess
from typing import Set, Optional, List, Dict, Any


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

def is_ignored_path(rel_path: str, ignored_dirs: Set[str] = DEFAULT_IGNORED_DIRS) -> bool:
    """Checks whether a relative file or directory path touches an ignored folder or pattern."""
    normalized = rel_path.replace("\\", "/").strip("/")
    parts = normalized.split("/")
    
    # Check if any parent or self matches an ignored dir
    for part in parts:
        if part in ignored_dirs or part.endswith(".egg-info"):
            return True
        if part.startswith(".") and part not in {".github"}:
            # Ignore hidden directories by default except .github
            if len(parts) > 1 and part != parts[-1]:
                return True

    filename = parts[-1]
    if filename in DEFAULT_IGNORED_FILES:
        return True

    _, ext = os.path.splitext(filename)
    if ext.lower() in DEFAULT_IGNORED_EXTENSIONS:
        return True
    
    # Check for minified patterns like foo.min.js
    if filename.endswith(".min.js") or filename.endswith(".min.css") or ".bundle." in filename:
        return True

    return False

def is_minified_or_binary(filepath: str, max_check_lines: int = 15) -> bool:
    """Detects if a file is minified code (very long lines) or binary content."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= max_check_lines:
                    break
                # If a line exceeds 800 characters, it's overwhelmingly minified bundle code
                if len(line) > 800:
                    return True
        return False
    except (OSError, UnicodeDecodeError):
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
        if not res.stdout.strip():
            return []
        data = json.loads(res.stdout)
        diagnostics = data.get("diagnostics", []) if isinstance(data, dict) else []
        
        parsed: List[Dict[str, Any]] = []
        for diag in diagnostics:
            labels = diag.get("labels", [])
            span = labels[0].get("span", {}) if labels else {}
            parsed.append({
                "code": diag.get("code") or "oxlint",
                "message": diag.get("message") or "",
                "severity": (diag.get("severity") or "warning").upper(),
                "line": span.get("line", 1),
                "column": span.get("column", 1),
                "help": diag.get("help", ""),
            })
        return parsed
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []


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
        if not res.stdout.strip():
            return []
        data = json.loads(res.stdout)
        diagnostics = data.get("diagnostics", []) if isinstance(data, dict) else []
        
        parsed: List[Dict[str, Any]] = []
        for diag in diagnostics:
            location = diag.get("location", {})
            position = location.get("path", {}).get("line", 1) if isinstance(location.get("path"), dict) else 1
            parsed.append({
                "code": diag.get("category") or "biome",
                "message": diag.get("description") or "",
                "severity": (diag.get("severity") or "warning").upper(),
                "line": position,
                "column": 1,
            })
        return parsed
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
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
            except (OSError, subprocess.SubprocessError):
                success = False
    return success

