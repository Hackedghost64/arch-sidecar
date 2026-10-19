"""Condition-blind grader for Round 2 benchmark.

Usage: python3 grade.py /tmp/round2_runs > grades.json
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
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
        out = {}
        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out[n.name] = out.get(n.name, 0) + 1
        return out
    except Exception:
        return {}

def pytest_summary(out):
    m = re.findall(r"(\d+) (passed|failed|error|errors)", out)
    d = {"passed": 0, "failed": 0, "errors": 0}
    for n, k in m:
        d["errors" if k.startswith("error") else k] += int(n)
    return d

def grade_r1(d):
    fpath = os.path.join(d, "user_routes.py")
    src = open(fpath).read() if os.path.exists(fpath) else ""
    rc, out = sh("python3 -c 'from user_routes import router; paths = [r.path for r in router.routes]; assert \"/user/watchlist/count\" in paths, f\"Missing path in {paths}\"'", d)
    checks = {
        "endpoint_defined_in_code": "/watchlist/count" in src,
        "endpoint_registered_in_router": rc == 0
    }
    return checks, {"output": out if rc != 0 else "OK"}

def grade_r2(d):
    src_fmt = os.path.join(d, "src/click/formatting.py")
    src_new = os.path.join(d, "src/click/_formatter.py")
    fd, nd = top_defs(src_fmt), top_defs(src_new)
    env = {"PYTHONPATH": "src"}
    rc, out = sh("python3 -m pytest -q -p no:cacheprovider tests/test_formatting.py", d, env)
    tests = pytest_summary(out)
    rc2, out2 = sh("python3 -c 'from click.formatting import HelpFormatter; from click._formatter import HelpFormatter; import click.parser'", d, env)
    checks = {
        "moved_HelpFormatter": "HelpFormatter" in nd and "HelpFormatter" not in fd,
        "backward_compat_import": rc2 == 0,
        "tests_all_pass": tests["failed"] == 0 and tests["errors"] == 0 and tests["passed"] >= 35
    }
    return checks, {"tests": tests, "compat_err": out2 if rc2 != 0 else ""}

def grade_r3(d):
    service_path = os.path.join(d, "service.py")
    actions_path = os.path.join(d, "actions.py")
    report_path = os.path.join(d, "REPORT.md")
    report_txt = open(report_path).read() if os.path.exists(report_path) else ""

    rc, out = sh("python3 -m pytest -q -p no:cacheprovider tests/test_service.py", d)
    tests = pytest_summary(out)
    rc_import, err_import = sh("python3 -c 'import service'", d)

    # Hazard detection check: Did agent note the circular hazard or block the unsafe cut?
    hazard_noted = bool(re.search(r"circular|hazard|blocked|dependency|tightly coupled", report_txt, re.I))
    
    checks = {
        "no_broken_circular_import": rc_import == 0,
        "tests_still_pass": tests["failed"] == 0 and tests["passed"] >= 1,
        "hazard_reported_or_handled": hazard_noted or os.path.exists(actions_path)
    }
    return checks, {"tests": tests, "circular_import_crash": rc_import != 0}

def main(root):
    res = {}
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            continue
        prefix = name.split("_")[0]
        g = {"r1": grade_r1, "r2": grade_r2, "r3": grade_r3}.get(prefix)
        if not g:
            continue
        try:
            checks, extra = g(d)
        except Exception as e:
            checks, extra = {"grader_crashed": False}, {"error": repr(e)}
        score = sum(checks.values()) / max(1, len(checks))
        res[name] = {"checks": checks, "score": round(score, 3), "report_exists": os.path.exists(os.path.join(d, "REPORT.md")), **extra}
    print(json.dumps(res, indent=2))

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/round2_runs")
