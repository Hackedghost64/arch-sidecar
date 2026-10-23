"""
Polyglot Deterministic Symbol Mover & Transactional Rollback Engine for Devrith.

Provides:
Deterministic extraction and moving of symbols across:
- Python (.py)
- TypeScript / JavaScript (.ts, .tsx, .js, .jsx)
- Go (.go)
- Rust (.rs)

Features:
- Automatic import resolution and backwards-compatibility re-exports.
- Dependency cycle hazard detection prior to commit.
- Atomic Transactional Rollback: if preflight health checks or test gates fail,
  restores all modified files to their exact pristine states.
"""

import os
import re
import shutil
import subprocess
import time
from typing import Dict, List, Optional, Any, Set

from .decouple import prefactor_decouple
from .ts_ast import parse_ts_js_ast
from .go_ast import parse_go_ast
from .rust_ast import parse_rust_ast
from .guard import check_code_health


def decouple_polyglot_symbol(
    symbol: str,
    from_file: str,
    to_file: str,
    project_root: str = ".",
    add_backward_compat_alias: bool = True,
    test_cmd: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Universally moves a symbol from `from_file` to `to_file` across Python, TypeScript,
    Go, and Rust, ensuring zero circular imports, backwards compatibility, and transactional rollback.
    """
    t_start = time.perf_counter()
    root_abs = os.path.abspath(project_root)
    from_path = from_file if os.path.isabs(from_file) else os.path.join(root_abs, from_file)
    to_path = to_file if os.path.isabs(to_file) else os.path.join(root_abs, to_file)

    if not os.path.isfile(from_path):
        return {"success": False, "error": f"Source file does not exist: {from_path}"}

    ext = os.path.splitext(from_path)[1].lower()

    # 1. Python: delegate to prefactor_decouple
    if ext == ".py":
        return prefactor_decouple(
            symbol=symbol,
            from_file=from_path,
            to_file=to_path,
            project_root=project_root,
            add_backward_compat_alias=add_backward_compat_alias,
            run_tests=test_cmd,
            dry_run=dry_run,
        )

    # Backup files for Transactional Rollback
    backup_from = from_path + ".devrith_bak"
    backup_to = to_path + ".devrith_bak"
    to_existed = os.path.exists(to_path)

    try:
        shutil.copy2(from_path, backup_from)
        if to_existed:
            shutil.copy2(to_path, backup_to)
    except Exception as e:
        return {"success": False, "error": f"Failed to create rollback backup: {e}"}

    def rollback():
        if os.path.exists(backup_from):
            shutil.copy2(backup_from, from_path)
            os.remove(backup_from)
        if to_existed and os.path.exists(backup_to):
            shutil.copy2(backup_to, to_path)
            os.remove(backup_to)
        elif not to_existed and os.path.exists(to_path):
            os.remove(to_path)
        if os.path.exists(backup_to):
            os.remove(backup_to)

    def cleanup_backups():
        if os.path.exists(backup_from):
            os.remove(backup_from)
        if os.path.exists(backup_to):
            os.remove(backup_to)

    try:
        with open(from_path, "r", encoding="utf-8") as f:
            from_source = f.read()
        from_lines = from_source.splitlines(keepends=True)

        symbol_code = ""
        symbol_start = -1
        symbol_end = -1
        lang = ""

        # 2. TypeScript / JavaScript
        if ext in (".ts", ".tsx", ".js", ".jsx"):
            lang = "typescript"
            parsed = parse_ts_js_ast(from_source, filename=from_path)
            matched = [d for d in parsed.get("declarations", []) if d["name"] == symbol]
            if not matched:
                rollback()
                return {"success": False, "error": f"Symbol '{symbol}' not found in {from_path}"}
            target_decl = matched[0]
            symbol_start = target_decl["start_line"] - 1
            symbol_end = target_decl["end_line"]
            symbol_code = "".join(from_lines[symbol_start:symbol_end])

            # Compute relative import path from from_file to to_file
            rel_import = os.path.relpath(to_path, os.path.dirname(from_path)).replace("\\", "/")
            if not rel_import.startswith("."):
                rel_import = "./" + rel_import
            # Strip extension for ts import
            rel_import_clean = os.path.splitext(rel_import)[0]

            # Rewrite from_file
            new_from_lines = from_lines[:symbol_start]
            if add_backward_compat_alias:
                new_from_lines.append(f'export {{ {symbol} }} from "{rel_import_clean}";\n')
            new_from_lines.extend(from_lines[symbol_end:])

            # Migrate dependent imports referenced in symbol_code
            needed_imports: List[str] = []
            for imp in parsed.get("imports", []):
                used_names = [n for n in imp.get("names", []) if re.search(rf"\b{re.escape(n)}\b", symbol_code)]
                if used_names:
                    mod_p = imp["module"]
                    if mod_p.startswith("."):
                        orig_abs = os.path.normpath(os.path.join(os.path.dirname(from_path), mod_p))
                        new_rel = os.path.relpath(orig_abs, os.path.dirname(to_path)).replace("\\", "/")
                        if not new_rel.startswith("."):
                            new_rel = "./" + new_rel
                        mod_p = new_rel
                    needed_imports.append(f'import {{ {", ".join(used_names)} }} from "{mod_p}";\n')

            # Prepare to_file content
            to_content = ""
            if os.path.exists(to_path):
                with open(to_path, "r", encoding="utf-8") as f:
                    to_content = f.read()

            # Ensure symbol_code is exported if not already
            if not symbol_code.strip().startswith("export "):
                symbol_code = "export " + symbol_code.lstrip()

            import_header = "".join(needed_imports)
            if to_content:
                new_to_content = to_content.rstrip() + "\n\n" + import_header + symbol_code.strip() + "\n"
            else:
                new_to_content = import_header + ("\n" if import_header else "") + symbol_code.strip() + "\n"

            if not dry_run:
                with open(from_path, "w", encoding="utf-8") as f:
                    f.writelines(new_from_lines)
                os.makedirs(os.path.dirname(to_path), exist_ok=True)
                with open(to_path, "w", encoding="utf-8") as f:
                    f.write(new_to_content)

        # 3. Go
        elif ext == ".go":
            lang = "go"
            parsed = parse_go_ast(from_source, filename=from_path)
            matched = [d for d in parsed.get("declarations", []) if d["name"] == symbol]
            if not matched:
                rollback()
                return {"success": False, "error": f"Symbol '{symbol}' not found in {from_path}"}
            target_decl = matched[0]
            symbol_start = target_decl["start_line"] - 1
            symbol_end = target_decl["end_line"]
            symbol_code = "".join(from_lines[symbol_start:symbol_end])

            new_from_lines = from_lines[:symbol_start] + from_lines[symbol_end:]

            pkg_name = parsed.get("package") or "main"
            needed_go_imports: List[str] = []
            for imp in parsed.get("imports", []):
                pkg_alias = imp["module"].split("/")[-1]
                if re.search(rf"\b{re.escape(pkg_alias)}\.", symbol_code):
                    needed_go_imports.append(f'import "{imp["module"]}"\n')

            to_content = ""
            if os.path.exists(to_path):
                with open(to_path, "r", encoding="utf-8") as f:
                    to_content = f.read()

            import_header = "".join(needed_go_imports)
            if to_content:
                new_to_content = to_content.rstrip() + "\n\n" + import_header + symbol_code.strip() + "\n"
            else:
                new_to_content = f"package {pkg_name}\n\n" + import_header + ("\n" if import_header else "") + symbol_code.strip() + "\n"

            if not dry_run:
                with open(from_path, "w", encoding="utf-8") as f:
                    f.writelines(new_from_lines)
                os.makedirs(os.path.dirname(to_path), exist_ok=True)
                with open(to_path, "w", encoding="utf-8") as f:
                    f.write(new_to_content)

        # 4. Rust
        elif ext == ".rs":
            lang = "rust"
            parsed = parse_rust_ast(from_source, filename=from_path)
            matched = [d for d in parsed.get("declarations", []) if d["name"] == symbol]
            if not matched:
                rollback()
                return {"success": False, "error": f"Symbol '{symbol}' not found in {from_path}"}
            target_decl = matched[0]
            symbol_start = target_decl["start_line"] - 1
            symbol_end = target_decl["end_line"]
            symbol_code = "".join(from_lines[symbol_start:symbol_end])

            new_from_lines = from_lines[:symbol_start]
            if add_backward_compat_alias:
                # Compute crate path
                mod_name = os.path.splitext(os.path.basename(to_path))[0]
                new_from_lines.append(f"pub use crate::{mod_name}::{symbol};\n")
            new_from_lines.extend(from_lines[symbol_end:])

            # Ensure symbol is pub in to_file
            if not symbol_code.strip().startswith("pub "):
                symbol_code = "pub " + symbol_code.lstrip()

            needed_rs_imports: List[str] = []
            for imp in parsed.get("imports", []):
                item_name = imp["module"].split("::")[-1].strip().rstrip(";")
                if re.search(rf"\b{re.escape(item_name)}\b", symbol_code):
                    needed_rs_imports.append(f'use {imp["module"]};\n')

            to_content = ""
            if os.path.exists(to_path):
                with open(to_path, "r", encoding="utf-8") as f:
                    to_content = f.read()

            import_header = "".join(needed_rs_imports)
            if to_content:
                new_to_content = to_content.rstrip() + "\n\n" + import_header + symbol_code.strip() + "\n"
            else:
                new_to_content = import_header + ("\n" if import_header else "") + symbol_code.strip() + "\n"

            if not dry_run:
                with open(from_path, "w", encoding="utf-8") as f:
                    f.writelines(new_from_lines)
                os.makedirs(os.path.dirname(to_path), exist_ok=True)
                with open(to_path, "w", encoding="utf-8") as f:
                    f.write(new_to_content)

        else:
            rollback()
            return {"success": False, "error": f"Unsupported language for decouple: {ext}"}

        if dry_run:
            rollback()
            return {
                "success": True,
                "symbol": symbol,
                "language": lang,
                "from_file": from_path,
                "to_file": to_path,
                "extracted_code": symbol_code,
                "dry_run": True
            }

        # Preflight Verification Gate
        health_from = check_code_health(from_path, project_root=project_root)
        health_to = check_code_health(to_path, project_root=project_root)

        if not health_from.get("success", False) or not health_to.get("success", False):
            # Preflight failed: trigger Transactional Rollback!
            rollback()
            errs = health_from.get("issues", []) + health_to.get("issues", [])
            return {
                "success": False,
                "error": "Preflight check failed after symbol move. Transaction rolled back.",
                "issues": errs
            }

        # Headless Test Gate Verification
        if test_cmd:
            proc = subprocess.run(
                test_cmd,
                shell=True,
                cwd=root_abs,
                capture_output=True,
                text=True,
                timeout=60
            )
            if proc.returncode != 0:
                # Tests failed: trigger Transactional Rollback!
                rollback()
                return {
                    "success": False,
                    "error": f"Test verification failed (exit code {proc.returncode}). Transaction rolled back.",
                    "test_output": proc.stderr or proc.stdout
                }

        # All checks passed: clean up backups
        cleanup_backups()
        duration_ms = round((time.perf_counter() - t_start) * 1000, 2)

        return {
            "success": True,
            "symbol": symbol,
            "language": lang,
            "from_file": os.path.relpath(from_path, root_abs),
            "to_file": os.path.relpath(to_path, root_abs),
            "duration_ms": duration_ms,
            "rolled_back": False
        }

    except Exception as e:
        rollback()
        return {"success": False, "error": f"Unexpected error during decoupling: {e}. Rolled back."}
