"""Objective, condition-blind grader for the A/B experiment.

Usage: python3 grade.py /tmp/ab_runs > grades.json
Grades each run directory purely from the files on disk (no knowledge of which agent produced it).
"""
import ast
import json
import os
import re
import subprocess
import sys


def sh(cmd, cwd, env=None):
    e = dict(os.environ)
    e.update(env or {})
    p = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, text=True, timeout=300, env=e)
    return p.returncode, (p.stdout + p.stderr)


def top_defs(path):
    if not os.path.exists(path):
        return {}
    tree = ast.parse(open(path, encoding="utf-8").read())
    out = {}
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[n.name] = out.get(n.name, 0) + 1
    return out


def unused_imports(path):
    """Count imported names never referenced in the module (simple static check)."""
    if not os.path.exists(path):
        return None
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    imported = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported += [(a.asname or a.name).split(".")[0] for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            imported += [a.asname or a.name for a in n.names if a.name != "*"]
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    used |= {n.value.id for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
    # names referenced inside string annotations
    strs = " ".join(n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str))
    return sorted({i for i in imported if i not in used and not re.search(rf"\b{re.escape(i)}\b", strs)})


def pytest_summary(out):
    m = re.findall(r"(\d+) (passed|failed|error|errors)", out)
    d = {"passed": 0, "failed": 0, "errors": 0}
    for n, k in m:
        d["errors" if k.startswith("error") else k] += int(n)
    return d


def grade_t1(d):
    cli, sysm = os.path.join(d, "cli.py"), os.path.join(d, "system.py")
    sd, cd = top_defs(sysm), top_defs(cli)
    rc, out = sh("timeout 200 python3 -m pytest -q -p no:cacheprovider tests", d)
    tests = pytest_summary(out)
    rc2, _ = sh("python3 -c 'from cli import SleepInhibitor, start_cloudflared_tunnel; import system'", d)
    checks = {
        "moved_SleepInhibitor": "SleepInhibitor" in sd and "SleepInhibitor" not in cd,
        "moved_start_cloudflared_tunnel": "start_cloudflared_tunnel" in sd and "start_cloudflared_tunnel" not in cd,
        "backward_compat_import": rc2 == 0,
        "tests_all_pass": tests["failed"] == 0 and tests["errors"] == 0 and tests["passed"] >= 15,
    }
    return checks, {"tests": tests, "unused_imports_new_module": unused_imports(sysm)}


def grade_t2(d):
    utils, kv = os.path.join(d, "src/requests/utils.py"), os.path.join(d, "src/requests/_kv.py")
    kd, ud = top_defs(kv), top_defs(utils)
    env = {"PYTHONPATH": "src"}
    rc, out = sh("python3 -m pytest -q -p no:cacheprovider tests/test_utils.py", d, env)
    tests = pytest_summary(out)
    rc2, _ = sh("python3 -c 'from requests.utils import to_key_val_list, from_key_val_list; import requests.sessions, requests.models; "
                "import requests; assert requests.__file__.startswith(\"src\") or \"/src/\" in requests.__file__'", d, env)
    checks = {
        "moved_to_key_val_list_with_3_overloads": kd.get("to_key_val_list", 0) == 3 and "to_key_val_list" not in ud,
        "moved_from_key_val_list": "from_key_val_list" in kd and "from_key_val_list" not in ud,
        "backward_compat_import": rc2 == 0,
        "tests_all_pass": tests["failed"] == 0 and tests["errors"] == 0 and tests["passed"] >= 216,
    }
    return checks, {"tests": tests, "unused_imports_new_module": unused_imports(kv)}


T3_FACTS = {
    "wsgi_app_entry": r"wsgi_app",
    "request_context_creates_ctx": r"request_context",
    "from_environ": r"from_environ",
    "AppContext_push": r"AppContext\.push|ctx\.push|\.push\(\)",
    "full_dispatch_request": r"full_dispatch_request",
    "dispatch_request": r"(?<!full_)dispatch_request",
    "helpers_url_for_delegates_to_current_app": r"current_app\.url_for|Flask\.url_for|app\.url_for",
    "RequestContext_merged_into_AppContext": r"RequestContext.{0,120}(merged|alias|deprecat)|(merged|alias|deprecat).{0,120}RequestContext",
    "cites_app_py_wsgi_line": r"app\.py[:#L ]+15(5\d|6\d|7\d)",
    "cites_ctx_py_push_line": r"ctx\.py[:#L ]+4(0\d|1\d|2\d)",
}
T3_FALSE = {
    "claims_RequestContext_class_defined": r"class RequestContext\b(?!.{0,80}(merged|alias|deprecat|no longer|removed))",
}


def grade_t3(d):
    p = os.path.join(d, "REPORT.md")
    txt = open(p, encoding="utf-8").read() if os.path.exists(p) else ""
    flat = re.sub(r"\s+", " ", txt)
    facts = {k: bool(re.search(v, flat, re.I | re.S)) for k, v in T3_FACTS.items()}
    false = {k: bool(re.search(v, flat, re.S)) for k, v in T3_FALSE.items()}
    return facts, {"false_claims": false, "report_chars": len(txt)}


def main(root):
    res = {}
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            continue
        g = {"t1": grade_t1, "t2": grade_t2, "t3": grade_t3}[name.split("_")[0]]
        try:
            checks, extra = g(d)
        except Exception as e:  # syntax errors etc. count as failure
            checks, extra = {"grader_crashed": False}, {"error": repr(e)}
        score = sum(checks.values()) / max(1, len(checks))
        if "false_claims" in extra:
            score -= 0.1 * sum(extra["false_claims"].values())
        res[name] = {"checks": checks, "score": round(score, 3),
                     "report_exists": os.path.exists(os.path.join(d, "REPORT.md")), **extra}
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/ab_runs")
