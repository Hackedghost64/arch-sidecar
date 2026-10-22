import pytest
from arch_sidecar.quality import (
    audit_code_string,
    audit_repository,
    QualityIssue,
    QualityReport,
)


def test_clean_code_audit():
    code = '''
from typing import Optional

def calculate_discount(price: float, is_vip: bool) -> float:
    """Calculates customer discount with early return guard."""
    if price <= 0:
        return 0.0
    if is_vip:
        return price * 0.2
    return price * 0.05
'''
    issues = audit_code_string(code)
    assert len(issues) == 0


def test_complexity_and_guard_clause():
    code = '''
def process_data(data):
    if data is not None:
        a = 1
        b = 2
        c = 3
        d = 4
        return a + b + c + d
'''
    issues = audit_code_string(code)
    guard_issues = [i for i in issues if i.rule == "missing_guard_clause"]
    assert len(guard_issues) == 1
    assert "Invert condition" in guard_issues[0].suggestion


def test_deep_nesting_detection():
    code = '''
def deeply_nested():
    if True:
        for i in range(10):
            while False:
                if True:
                    print("too deep")
'''
    issues = audit_code_string(code)
    nesting_issues = [i for i in issues if "nesting" in i.rule]
    assert len(nesting_issues) >= 1
    assert nesting_issues[0].category == "complexity"


def test_silent_exception_swallowing():
    code = '''
def unsafe_call():
    try:
        dangerous_operation()
    except Exception:
        pass
'''
    issues = audit_code_string(code)
    silent_issues = [i for i in issues if i.rule == "silent_exception_swallowing"]
    assert len(silent_issues) == 1
    assert silent_issues[0].severity == "error"


def test_mutable_default_argument():
    code = '''
def append_to_list(val, target=[]):
    target.append(val)
    return target
'''
    issues = audit_code_string(code)
    mutable_issues = [i for i in issues if i.rule == "mutable_default_argument"]
    assert len(mutable_issues) == 1
    assert mutable_issues[0].severity == "error"

    # Also catches parameter mutation target.append(val)
    mutation_issues = [i for i in issues if i.rule == "parameter_mutation"]
    assert len(mutation_issues) == 1


def test_law_of_demeter_detection():
    code = '''
def get_user_zip(order):
    return order.customer.profile.address.zipcode
'''
    issues = audit_code_string(code)
    demeter_issues = [i for i in issues if i.rule == "law_of_demeter_violation"]
    assert len(demeter_issues) == 1
    assert demeter_issues[0].category == "oop_cohesion"


def test_inconsistent_return_contract():
    unannotated_code = '''
def find_item(items, target):
    for item in items:
        if item == target:
            return item
    return None
'''
    issues = audit_code_string(unannotated_code)
    contract_issues = [i for i in issues if i.rule == "inconsistent_return_contract"]
    assert len(contract_issues) == 1

    annotated_code = '''
from typing import Optional

def find_item_fixed(items, target) -> Optional[str]:
    for item in items:
        if item == target:
            return item
    return None
'''
    issues_fixed = audit_code_string(annotated_code)
    contract_issues_fixed = [i for i in issues_fixed if i.rule == "inconsistent_return_contract"]
    assert len(contract_issues_fixed) == 0


def test_audit_repository_scoring(tmp_path):
    dirty_file = tmp_path / "dirty.py"
    dirty_file.write_text('''
def bad_func(x=[]):
    try:
        do_something()
    except Exception:
        pass
''')

    clean_file = tmp_path / "clean.py"
    clean_file.write_text('''
def add(a: int, b: int) -> int:
    return a + b
''')

    report = audit_repository(str(tmp_path), strict=True)
    assert report.total_files == 2
    assert report.error_count >= 2  # mutable default + silent exception
    assert report.clean is False
    assert report.score < 100.0
