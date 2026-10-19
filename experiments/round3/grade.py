"""Objective grader for Round 3 Feature Workflow Benchmark.
Usage: python3 grade.py > grades.json
"""
import json
import os
import re
import subprocess
import sys

BASE = "/tmp/round3_runs"

def run_cmd(cmd, cwd, timeout=30):
    try:
        res = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, text=True, timeout=timeout)
        out = (res.stdout + "\n" + res.stderr).strip()
        passed_m = re.search(r"(\d+)\s+passed", out)
        passed = int(passed_m.group(1)) if passed_m else 0
        failed_m = re.search(r"(\d+)\s+failed", out)
        failed = int(failed_m.group(1)) if failed_m else 0
        return {"returncode": res.returncode, "passed": passed, "failed": failed, "output": out[-1000:]}
    except Exception as e:
        return {"returncode": -1, "passed": 0, "failed": 0, "error": str(e)}

def grade_f1_click(run_dir):
    report_exists = os.path.exists(os.path.join(run_dir, "REPORT.md"))
    fmt_path = os.path.join(run_dir, "src/click/formatting.py")
    test_path = os.path.join(run_dir, "tests/test_formatting.py")
    if not (os.path.exists(fmt_path) and os.path.exists(test_path)):
        return {"score": 0.0, "reason": "Missing files"}

    fmt_code = open(fmt_path).read()
    test_code = open(test_path).read()

    has_method = "def write_table" in fmt_code
    has_test = "test_formatting_write_table" in test_code

    test_res = run_cmd("PYTHONPATH=src python3 -m pytest -q -p no:cacheprovider tests/test_formatting.py", run_dir)
    passed_ok = test_res["returncode"] == 0 and test_res["passed"] >= 37

    checks = {
        "report_exists": report_exists,
        "write_table_defined": has_method,
        "test_defined": has_test,
        "formatting_tests_passed": passed_ok,
    }
    score = sum(1.0 for v in checks.values() if v) / len(checks)
    return {"score": round(score, 3), "checks": checks, "test_res": test_res}

def grade_f2_requests(run_dir):
    report_exists = os.path.exists(os.path.join(run_dir, "REPORT.md"))
    utils_path = os.path.join(run_dir, "src/requests/utils.py")
    test_path = os.path.join(run_dir, "tests/test_utils.py")
    if not (os.path.exists(utils_path) and os.path.exists(test_path)):
        return {"score": 0.0, "reason": "Missing files"}

    utils_code = open(utils_path).read()
    test_code = open(test_path).read()

    has_func = "def redact_url_auth" in utils_code
    has_test = "test_redact_url_auth" in test_code

    test_res = run_cmd("PYTHONPATH=src python3 -m pytest -q -p no:cacheprovider tests/test_utils.py", run_dir)
    passed_ok = test_res["returncode"] == 0 and test_res["passed"] >= 216

    # Test direct functionality
    eval_res = run_cmd(
        'PYTHONPATH=src python3 -c "from requests.utils import redact_url_auth; '
        'assert redact_url_auth(\'http://user:pass@example.com:8080/p#f\') == \'http://***@example.com:8080/p#f\'; '
        'assert redact_url_auth(\'http://example.com/p\') == \'http://example.com/p\'"',
        run_dir
    )
    func_ok = (eval_res["returncode"] == 0)

    checks = {
        "report_exists": report_exists,
        "redact_url_auth_defined": has_func,
        "test_defined": has_test,
        "utils_tests_passed": passed_ok,
        "redact_behavior_verified": func_ok,
    }
    score = sum(1.0 for v in checks.values() if v) / len(checks)
    return {"score": round(score, 3), "checks": checks, "test_res": test_res}

def main():
    runs = json.load(open("experiments/round3/runs.json"))
    grades = {}
    for name in runs:
        run_dir = os.path.join(BASE, name)
        if name.startswith("f1_click"):
            grades[name] = grade_f1_click(run_dir)
        elif name.startswith("f2_requests"):
            grades[name] = grade_f2_requests(run_dir)
    print(json.dumps(grades, indent=2))

if __name__ == "__main__":
    main()
