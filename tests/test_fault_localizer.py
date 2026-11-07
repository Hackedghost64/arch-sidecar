import os
import tempfile
from arch_sidecar.fault_localizer import (
    ASTCanonicalRepairCache,
    canonicalize_ast_line,
    decanonicalize_ast_line,
    extract_function_params,
)
from arch_sidecar.feature_workflow import run_feature_workflow


def test_ast_canonical_repair_cache_cross_repo_transfer():
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_file = os.path.join(tmpdir, "repair_cache.json")
        cache = ASTCanonicalRepairCache(cache_path=cache_file)

        repo_a_func = (
            "def calculate_net_invoice(subtotal: float, promo_rate: float) -> float:\n"
            "    return round(subtotal * (1.0 + promo_rate), 2)\n"
        )
        entry = cache.record_repair(
            func_code=repo_a_func,
            func_name="calculate_net_invoice",
            buggy_line="return round(subtotal * (1.0 + promo_rate), 2)",
            fixed_line="return round(subtotal * (1.0 - promo_rate), 2)",
        )
        assert entry["canonical_bug"] == "return round($ARG0 * (1.0 + $ARG1), 2)"
        assert entry["canonical_fix"] == "return round($ARG0 * (1.0 - $ARG1), 2)"

        # Reload from disk and apply to a completely renamed function from another repo
        reloaded = ASTCanonicalRepairCache(cache_path=cache_file)
        repo_b_func = (
            "def compute_discounted_price(unit_cost: float, markdown_pct: float) -> float:\n"
            "    return round(unit_cost * (1.0 + markdown_pct), 2)\n"
        )
        hit = reloaded.lookup_repair(repo_b_func, "compute_discounted_price", start_line=10)
        assert hit is not None
        assert hit["line_number"] == 11
        assert hit["suggested_line"] == "    return round(unit_cost * (1.0 - markdown_pct), 2)"


def test_feature_workflow_pytest_fault_localization_and_repair_cache(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_path = os.path.join(tmpdir, "ast_repair_cache.json")
        monkeypatch.setenv("DEVRITH_HOME", tmpdir)

        cache = ASTCanonicalRepairCache(cache_path=cache_path)
        cache.record_repair(
            func_code="def calc(a, b):\n    return round(a * (1.0 + b), 2)\n",
            func_name="calc",
            buggy_line="return round(a * (1.0 + b), 2)",
            fixed_line="return round(a * (1.0 - b), 2)",
        )

        # Create a distractors file and a target file with a failing pytest test
        with open(os.path.join(tmpdir, "alpha_noise.py"), "w", encoding="utf-8") as f:
            f.write("def unrelated_helper(x: int) -> int:\n    return x + 1\n")

        with open(os.path.join(tmpdir, "zeta_pricing.py"), "w", encoding="utf-8") as f:
            f.write(
                "def apply_markdown(base_price: float, markdown_ratio: float) -> float:\n"
                "    return round(base_price * (1.0 + markdown_ratio), 2)\n"
            )

        tests_dir = os.path.join(tmpdir, "tests")
        os.makedirs(tests_dir, exist_ok=True)
        with open(os.path.join(tests_dir, "test_pricing.py"), "w", encoding="utf-8") as f:
            f.write(
                "from zeta_pricing import apply_markdown\n\n"
                "def test_apply_markdown():\n"
                "    assert apply_markdown(100.0, 0.2) == 80.0\n"
            )

        # Vague bug prompt with zero mention of 'zeta_pricing' or 'apply_markdown'
        res = run_feature_workflow(
            task="Fix the broken calculation bug in the repo",
            project_root=tmpdir,
        )
        assert res["primary_file"] == "zeta_pricing.py"
        assert res["anchor_symbols"][0]["name"] == "apply_markdown"
        assert len(res["canonical_repair_suggestions"]) == 1
        suggestion = res["canonical_repair_suggestions"][0]
        assert suggestion["suggested_line"].strip() == "return round(base_price * (1.0 - markdown_ratio), 2)"
