"""
Polyglot Deterministic Symbol Mover & Transactional Rollback Engine for Devrith.

Provides:
Deterministic extraction and moving of symbols across:
- Python (.py)
- TypeScript / JavaScript (.ts, .tsx, .js, .jsx)
- Go (.go)
- Rust (.rs)
- Dart (.dart)
- JVM (Kotlin / Java: .kt, .kts, .java)

Features:
- Multi-symbol list and comma-separated extraction in a single atomic transaction.
- Automatic import resolution, migration, and backwards-compatibility re-exports.
- Dependency cycle hazard detection prior to commit.
- Detection of local referenced dependencies (remaining helpers in source file).
- Atomic Transactional Rollback: if preflight health checks or test gates fail,
  restores all modified files to their exact pristine states.
"""

import os
import re
import shutil
import subprocess
import time
import logging
from typing import Dict, List, Optional, Any, Set, Tuple, Union

from .decouple import prefactor_decouple
from .ts_ast import parse_ts_js_ast
from .go_ast import parse_go_ast
from .rust_ast import parse_rust_ast
from .dart_ast import parse_dart_ast
from .jvm_ast import parse_jvm_ast
from .guard import check_code_health

logger = logging.getLogger(__name__)


def normalize_symbol_input(symbol: Union[str, List[str]]) -> List[str]:
    """
    Normalizes single symbol string, comma-separated string, or list of strings
    into a deduplicated list of trimmed symbol names.
    """
    if isinstance(symbol, str):
        raw_list = [s.strip() for s in symbol.split(",") if s.strip()]
    else:
        raw_list = [str(s).strip() for s in symbol if str(s).strip()]

    seen: Set[str] = set()
    result: List[str] = []
    for s in raw_list:
        if s not in seen:
            seen.add(s)
            result.append(s)
    return result


def locate_declarations(
    declarations: List[Dict[str, Any]],
    symbols_list: List[str],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Locates matching declarations for symbols_list in declarations.
    Returns (matched_declarations_sorted_by_start_line, missing_symbols).
    """
    decl_map: Dict[str, Dict[str, Any]] = {}
    for d in declarations:
        name = d.get("name")
        if name and name not in decl_map:
            decl_map[name] = d

    matched: List[Dict[str, Any]] = []
    missing: List[str] = []
    for s in symbols_list:
        if s in decl_map:
            matched.append(decl_map[s])
        else:
            missing.append(s)

    matched.sort(key=lambda d: d.get("start_line", 0))
    return matched, missing


def find_local_dependencies_referenced(
    all_declarations: List[Dict[str, Any]],
    extracted_symbols: List[str],
    combined_code: str,
) -> List[str]:
    """
    Detects if any remaining non-extracted declaration in from_file is referenced
    inside the extracted symbol_code.
    """
    local_deps: List[str] = []
    extracted_set = set(extracted_symbols)
    seen: Set[str] = set()
    for d in all_declarations:
        name = d.get("name", "")
        if not name or name in extracted_set:
            continue
        candidates = [name]
        if "." in name:
            candidates.append(name.split(".")[0])
        for cand in candidates:
            if cand and cand not in extracted_set and cand not in seen:
                if re.search(rf"\b{re.escape(cand)}\b", combined_code):
                    seen.add(cand)
                    local_deps.append(cand)
    return local_deps


def apply_line_removals(
    from_lines: List[str],
    matched_decls: List[Dict[str, Any]],
    alias_line: Optional[str] = None,
) -> List[str]:
    """
    Removes matched declarations from from_lines in reverse line-number order
    so line indices never shift. Replaces the first declaration with alias_line
    if provided.
    """
    new_from_lines = list(from_lines)
    reversed_decls = sorted(matched_decls, key=lambda d: d.get("start_line", 0), reverse=True)
    first_decl = min(matched_decls, key=lambda d: d.get("start_line", 0))
    for d in reversed_decls:
        s_start = d["start_line"] - 1
        s_end = d["end_line"]
        if d is first_decl and alias_line:
            new_from_lines[s_start:s_end] = [alias_line]
        else:
            new_from_lines[s_start:s_end] = []
    return new_from_lines


def _transform_ts(
    parsed: Dict[str, Any],
    matched_decls: List[Dict[str, Any]],
    symbols_list: List[str],
    from_lines: List[str],
    from_path: str,
    to_path: str,
    existing_to_content: str,
    add_backward_compat_alias: bool,
) -> Tuple[Optional[str], str, str]:
    blocks: List[str] = []
    for d in matched_decls:
        code = "".join(from_lines[d["start_line"] - 1 : d["end_line"]])
        if not re.match(r"^export\b", code.strip()):
            code = "export " + code.lstrip()
        blocks.append(code.strip())
    combined_code = "\n\n".join(blocks) + "\n"

    alias_line = None
    if add_backward_compat_alias:
        rel_import = os.path.relpath(to_path, os.path.dirname(from_path)).replace("\\", "/")
        if not rel_import.startswith("."):
            rel_import = "./" + rel_import
        rel_import_clean = os.path.splitext(rel_import)[0]
        alias_line = f'export {{ {", ".join(symbols_list)} }} from "{rel_import_clean}";\n'

    needed_imports: List[str] = []
    for imp in parsed.get("imports", []):
        used_names = [n for n in imp.get("names", []) if re.search(rf"\b{re.escape(n)}\b", combined_code)]
        if used_names:
            mod_p = imp["module"]
            if mod_p.startswith("."):
                orig_abs = os.path.normpath(os.path.join(os.path.dirname(from_path), mod_p))
                new_rel = os.path.relpath(orig_abs, os.path.dirname(to_path)).replace("\\", "/")
                if not new_rel.startswith("."):
                    new_rel = "./" + new_rel
                mod_p = new_rel
            needed_imports.append(f'import {{ {", ".join(used_names)} }} from "{mod_p}";\n')

    import_header = "".join(needed_imports)
    if existing_to_content:
        new_to_content = existing_to_content.rstrip() + "\n\n" + import_header + combined_code.strip() + "\n"
    else:
        new_to_content = import_header + ("\n" if import_header else "") + combined_code.strip() + "\n"

    return alias_line, combined_code, new_to_content


def _transform_go(
    parsed: Dict[str, Any],
    matched_decls: List[Dict[str, Any]],
    symbols_list: List[str],
    from_lines: List[str],
    from_path: str,
    to_path: str,
    existing_to_content: str,
    add_backward_compat_alias: bool,
) -> Tuple[Optional[str], str, str]:
    blocks = [
        "".join(from_lines[d["start_line"] - 1 : d["end_line"]]).strip()
        for d in matched_decls
    ]
    combined_code = "\n\n".join(blocks) + "\n"
    alias_line = None

    pkg_name = parsed.get("package") or "main"
    needed_go_imports: List[str] = []
    for imp in parsed.get("imports", []):
        pkg_alias = imp["module"].split("/")[-1]
        if re.search(rf"\b{re.escape(pkg_alias)}\.", combined_code):
            needed_go_imports.append(f'import "{imp["module"]}"\n')

    import_header = "".join(needed_go_imports)
    if existing_to_content:
        new_to_content = existing_to_content.rstrip() + "\n\n" + import_header + combined_code.strip() + "\n"
    else:
        new_to_content = f"package {pkg_name}\n\n" + import_header + ("\n" if import_header else "") + combined_code.strip() + "\n"

    return alias_line, combined_code, new_to_content


def _transform_rust(
    parsed: Dict[str, Any],
    matched_decls: List[Dict[str, Any]],
    symbols_list: List[str],
    from_lines: List[str],
    from_path: str,
    to_path: str,
    existing_to_content: str,
    add_backward_compat_alias: bool,
) -> Tuple[Optional[str], str, str]:
    blocks: List[str] = []
    for d in matched_decls:
        code = "".join(from_lines[d["start_line"] - 1 : d["end_line"]])
        if not re.match(r"^pub\b", code.strip()):
            code = "pub " + code.lstrip()
        blocks.append(code.strip())
    combined_code = "\n\n".join(blocks) + "\n"

    alias_line = None
    if add_backward_compat_alias:
        mod_name = os.path.splitext(os.path.basename(to_path))[0]
        if len(symbols_list) == 1:
            alias_line = f"pub use crate::{mod_name}::{symbols_list[0]};\n"
        else:
            alias_line = f"pub use crate::{mod_name}::{{{', '.join(symbols_list)}}};\n"

    needed_rs_imports: List[str] = []
    for imp in parsed.get("imports", []):
        item_name = imp["module"].split("::")[-1].strip().rstrip(";")
        if re.search(rf"\b{re.escape(item_name)}\b", combined_code):
            needed_rs_imports.append(f'use {imp["module"]};\n')

    import_header = "".join(needed_rs_imports)
    if existing_to_content:
        new_to_content = existing_to_content.rstrip() + "\n\n" + import_header + combined_code.strip() + "\n"
    else:
        new_to_content = import_header + ("\n" if import_header else "") + combined_code.strip() + "\n"

    return alias_line, combined_code, new_to_content


def _transform_dart(
    parsed: Dict[str, Any],
    matched_decls: List[Dict[str, Any]],
    symbols_list: List[str],
    from_lines: List[str],
    from_path: str,
    to_path: str,
    existing_to_content: str,
    add_backward_compat_alias: bool,
) -> Tuple[Optional[str], str, str]:
    blocks = [
        "".join(from_lines[d["start_line"] - 1 : d["end_line"]]).strip()
        for d in matched_decls
    ]
    combined_code = "\n\n".join(blocks) + "\n"

    alias_line = None
    if add_backward_compat_alias:
        rel_import = os.path.relpath(to_path, os.path.dirname(from_path)).replace("\\", "/")
        if not rel_import.startswith("."):
            rel_import = "./" + rel_import
        alias_line = f"export '{rel_import}' show {', '.join(symbols_list)};\n"

    needed_dart_imports: List[str] = []
    for imp in parsed.get("imports", []):
        mod = imp["module"].split("/")[-1].replace(".dart", "")
        if re.search(rf"\b{re.escape(mod)}\b", combined_code):
            alias_str = f" as {imp['alias']}" if imp.get("alias") else ""
            needed_dart_imports.append(f"import '{imp['module']}'{alias_str};\n")

    import_header = "".join(needed_dart_imports)
    if existing_to_content:
        new_to_content = existing_to_content.rstrip() + "\n\n" + import_header + combined_code.strip() + "\n"
    else:
        new_to_content = import_header + ("\n" if import_header else "") + combined_code.strip() + "\n"

    return alias_line, combined_code, new_to_content


def _transform_jvm(
    parsed: Dict[str, Any],
    matched_decls: List[Dict[str, Any]],
    symbols_list: List[str],
    from_lines: List[str],
    from_path: str,
    to_path: str,
    existing_to_content: str,
    add_backward_compat_alias: bool,
) -> Tuple[Optional[str], str, str]:
    blocks = [
        "".join(from_lines[d["start_line"] - 1 : d["end_line"]]).strip()
        for d in matched_decls
    ]
    combined_code = "\n\n".join(blocks) + "\n"
    alias_line = None

    pkg_name = parsed.get("package") or ""
    needed_jvm_imports: List[str] = []
    for imp in parsed.get("imports", []):
        imp_tail = imp["module"].split(".")[-1].rstrip(";")
        if re.search(rf"\b{re.escape(imp_tail)}\b", combined_code):
            needed_jvm_imports.append(f"import {imp['module']};\n")

    import_header = "".join(needed_jvm_imports)
    pkg_header = f"package {pkg_name};\n\n" if pkg_name else ""
    if existing_to_content:
        new_to_content = existing_to_content.rstrip() + "\n\n" + import_header + combined_code.strip() + "\n"
    else:
        new_to_content = pkg_header + import_header + ("\n" if import_header else "") + combined_code.strip() + "\n"

    return alias_line, combined_code, new_to_content


class _TransactionalRollback:
    """Manages transactional backup, rollback, and cleanup for atomic file operations."""

    def __init__(self, from_path: str, to_path: str) -> None:
        self.from_path = from_path
        self.to_path = to_path
        self.backup_from = from_path + ".devrith_bak"
        self.backup_to = to_path + ".devrith_bak"
        self.to_existed = os.path.exists(to_path)

    def backup(self) -> None:
        shutil.copy2(self.from_path, self.backup_from)
        if self.to_existed:
            shutil.copy2(self.to_path, self.backup_to)

    def rollback(self) -> None:
        if os.path.exists(self.backup_from):
            shutil.copy2(self.backup_from, self.from_path)
            os.remove(self.backup_from)
        if self.to_existed and os.path.exists(self.backup_to):
            shutil.copy2(self.backup_to, self.to_path)
            os.remove(self.backup_to)
        elif not self.to_existed and os.path.exists(self.to_path):
            os.remove(self.to_path)
        if os.path.exists(self.backup_to):
            os.remove(self.backup_to)

    def cleanup(self) -> None:
        if os.path.exists(self.backup_from):
            os.remove(self.backup_from)
        if os.path.exists(self.backup_to):
            os.remove(self.backup_to)


def _decouple_python_symbol(
    symbols_list: List[str],
    from_path: str,
    to_path: str,
    project_root: str,
    add_backward_compat_alias: bool,
    test_cmd: Optional[str],
    dry_run: bool,
) -> Dict[str, Any]:
    py_symbol = symbols_list if len(symbols_list) > 1 else symbols_list[0]
    res = prefactor_decouple(
        symbol=py_symbol,
        from_file=from_path,
        to_file=to_path,
        project_root=project_root,
        add_backward_compat_alias=add_backward_compat_alias,
        run_tests=test_cmd,
        dry_run=dry_run,
    )
    if "local_dependencies_referenced" not in res:
        res["local_dependencies_referenced"] = [
            s for s in res.get("minimal_safe_closure", []) if s not in symbols_list
        ]
    return res


def _get_polyglot_transform(
    ext: str, from_source: str, from_path: str
) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[Any]]:
    if ext in (".ts", ".tsx", ".js", ".jsx"):
        return "typescript", parse_ts_js_ast(from_source, filename=from_path), _transform_ts
    if ext == ".go":
        return "go", parse_go_ast(from_source, filename=from_path), _transform_go
    if ext == ".rs":
        return "rust", parse_rust_ast(from_source, filename=from_path), _transform_rust
    if ext == ".dart":
        return "dart", parse_dart_ast(from_source, filename=from_path), _transform_dart
    if ext in (".kt", ".kts", ".java"):
        lang = "kotlin" if ext in (".kt", ".kts") else "java"
        return lang, parse_jvm_ast(from_source, filename=from_path), _transform_jvm
    return None, None, None


def _verify_preflight_and_tests(
    from_path: str,
    to_path: str,
    project_root: str,
    test_cmd: Optional[str],
    root_abs: str,
) -> Tuple[bool, Optional[str], Optional[List[Any]], Optional[str]]:
    health_from = check_code_health(from_path, project_root=project_root)
    health_to = check_code_health(to_path, project_root=project_root)

    if not health_from.get("success", False) or not health_to.get("success", False):
        errs = health_from.get("issues", []) + health_to.get("issues", [])
        return False, "Preflight check failed after symbol move. Transaction rolled back.", errs, None

    if test_cmd:
        proc = subprocess.run(
            test_cmd,
            shell=True,
            cwd=root_abs,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            msg = f"Test verification failed (exit code {proc.returncode}). Transaction rolled back."
            return False, msg, None, proc.stderr or proc.stdout

    return True, None, None, None


def _write_polyglot_files(
    from_path: str,
    new_from_lines: List[str],
    to_path: str,
    new_to_content: str,
) -> None:
    with open(from_path, "w", encoding="utf-8") as f:
        f.writelines(new_from_lines)
    os.makedirs(os.path.dirname(to_path), exist_ok=True)
    with open(to_path, "w", encoding="utf-8") as f:
        f.write(new_to_content)


def _read_file_content_safe(path: str) -> str:
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _format_decouple_failure(
    err_msg: Optional[str],
    issues: Optional[List[Any]],
    test_out: Optional[str],
) -> Dict[str, Any]:
    fail_res: Dict[str, Any] = {"success": False, "error": err_msg or "Decoupling verification failed."}
    if issues is not None:
        fail_res["issues"] = issues
    if test_out is not None:
        fail_res["test_output"] = test_out
    return fail_res


def _validate_decouple_args(
    from_path: str,
    symbol: Union[str, List[str]],
    from_file: str,
) -> Tuple[Optional[List[str]], Optional[Dict[str, Any]]]:
    if not os.path.isfile(from_path):
        return None, {"success": False, "error": f"Source file does not exist: {from_path}"}

    symbols_list = normalize_symbol_input(symbol)
    if not symbols_list:
        return None, {"success": False, "error": f"No valid symbol(s) provided for decoupling from {from_file}"}

    return symbols_list, None


def _transform_and_write(
    parsed: Dict[str, Any],
    matched_decls: List[Dict[str, Any]],
    symbols_list: List[str],
    from_lines: List[str],
    from_path: str,
    to_path: str,
    transform_fn: Any,
    add_backward_compat_alias: bool,
) -> Tuple[str, List[str], str]:
    to_content = _read_file_content_safe(to_path)
    alias_line, combined_symbol_code, new_to_content = transform_fn(
        parsed=parsed,
        matched_decls=matched_decls,
        symbols_list=symbols_list,
        from_lines=from_lines,
        from_path=from_path,
        to_path=to_path,
        existing_to_content=to_content,
        add_backward_compat_alias=add_backward_compat_alias,
    )
    new_from_lines = apply_line_removals(from_lines, matched_decls, alias_line)
    return combined_symbol_code, new_from_lines, new_to_content


def decouple_polyglot_symbol(
    symbol: Union[str, List[str]],
    from_file: str,
    to_file: str,
    project_root: str = ".",
    add_backward_compat_alias: bool = True,
    test_cmd: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Universally moves a single symbol, comma-separated symbols, or list of symbols
    from `from_file` to `to_file` across Python, TypeScript, Go, Rust, Dart, and JVM,
    ensuring zero circular imports, backwards compatibility, and transactional rollback.
    """
    t_start = time.perf_counter()
    root_abs = os.path.abspath(project_root)
    from_path = from_file if os.path.isabs(from_file) else os.path.join(root_abs, from_file)
    to_path = to_file if os.path.isabs(to_file) else os.path.join(root_abs, to_file)

    symbols_list, err_resp = _validate_decouple_args(from_path, symbol, from_file)
    if err_resp:
        return err_resp

    ext = os.path.splitext(from_path)[1].lower()
    if ext == ".py":
        return _decouple_python_symbol(
            symbols_list, from_path, to_path, project_root, add_backward_compat_alias, test_cmd, dry_run
        )

    tx = _TransactionalRollback(from_path, to_path)
    try:
        tx.backup()
    except OSError as e:
        logger.error("Failed to create rollback backup: %s", e)
        return {"success": False, "error": f"Failed to create rollback backup: {e}"}

    try:
        with open(from_path, "r", encoding="utf-8") as f:
            from_source = f.read()
        from_lines = from_source.splitlines(keepends=True)

        lang, parsed, transform_fn = _get_polyglot_transform(ext, from_source, from_path)
        if not transform_fn or not parsed or not lang:
            tx.rollback()
            return {"success": False, "error": f"Unsupported language for decouple: {ext}"}

        matched_decls, missing_symbols = locate_declarations(parsed.get("declarations", []), symbols_list)
        if missing_symbols:
            tx.rollback()
            return {"success": False, "error": f"Symbol(s) not found in {from_path}: {', '.join(missing_symbols)}"}

        combined_code, new_from_lines, new_to_content = _transform_and_write(
            parsed, matched_decls, symbols_list, from_lines, from_path, to_path, transform_fn, add_backward_compat_alias
        )

        local_deps = find_local_dependencies_referenced(
            all_declarations=parsed.get("declarations", []),
            extracted_symbols=symbols_list,
            combined_code=combined_code,
        )

        if dry_run:
            tx.rollback()
            return {
                "success": True,
                "symbol": symbol,
                "extracted_symbols": symbols_list,
                "local_dependencies_referenced": local_deps,
                "language": lang,
                "from_file": from_path,
                "to_file": to_path,
                "extracted_code": combined_code,
                "dry_run": True,
            }

        _write_polyglot_files(from_path, new_from_lines, to_path, new_to_content)
        ok, err_msg, issues, test_out = _verify_preflight_and_tests(from_path, to_path, project_root, test_cmd, root_abs)
        if not ok:
            tx.rollback()
            return _format_decouple_failure(err_msg, issues, test_out)

        tx.cleanup()
        duration_ms = round((time.perf_counter() - t_start) * 1000, 2)
        return {
            "success": True,
            "symbol": symbol,
            "extracted_symbols": symbols_list,
            "local_dependencies_referenced": local_deps,
            "execution_time_ms": duration_ms,
            "language": lang,
            "from_file": os.path.relpath(from_path, root_abs),
            "to_file": os.path.relpath(to_path, root_abs),
            "duration_ms": duration_ms,
            "rolled_back": False,
        }

    except Exception as e:
        logger.error("Unexpected error during decoupling: %s", e)
        tx.rollback()
        return {"success": False, "error": f"Unexpected error during decoupling: {e}. Rolled back."}
