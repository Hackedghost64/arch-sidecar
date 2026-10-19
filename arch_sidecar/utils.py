import os
import re
from typing import Set

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
    except Exception:
        return True

def estimate_tokens(text: str) -> int:
    """Quick and reliable heuristic for token count (~4 characters per token)."""
    return max(1, len(text) // 4)
