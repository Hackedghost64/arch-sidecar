# Devrith Round 3 Benchmark Report: Feature Workflow
**Date:** October 19, 2026  
**Subject:** Empirical evaluation of `feature_workflow` (`arch-sidecar feature`) against an unassisted baseline coding agent implementing new features in real repositories.  
**Core Principle:** *Understand before changing. Check before trusting. Verify before claiming success.*

---

## 1. Executive Summary

| Task & Repository | Metric | Baseline Agent | Devrith `feature_workflow` | Absolute Change | % Change | Correctness |
|---|---|---:|---:|---:|---:|:---:|
| **F1 — Click**<br>`HelpFormatter.write_table` + tests | Total Tokens<br>Input Tokens<br>Output Tokens<br>Model Turns<br>Tool Calls<br>Wall Time | 1,117,272<br>1,099,536<br>17,736<br>23<br>21<br>230.0s | **314,739**<br>**304,673**<br>**10,066**<br>**8**<br>**6**<br>**115.0s** | **−802,533**<br>−794,863<br>−7,670<br>−15<br>−15<br>−115.0s | **−71.8%**<br>−72.3%<br>−43.2%<br>−65.2%<br>−71.4%<br>**−50.0% (2.0x faster)** | Both 1.0/1.0<br>(38/38 tests passed) |
| **F2 — Requests**<br>`redact_url_auth` + tests | Total Tokens<br>Input Tokens<br>Output Tokens<br>Model Turns<br>Tool Calls<br>Wall Time | 736,491<br>729,483<br>7,008<br>20<br>19<br>142.0s | **247,273**<br>**242,041**<br>**5,232**<br>**7**<br>**6**<br>**70.0s** | **−489,218**<br>−487,442<br>−1,776<br>−13<br>−13<br>−72.0s | **−66.4%**<br>−66.8%<br>−25.3%<br>−65.0%<br>−68.4%<br>**−50.7% (2.0x faster)** | Both 1.0/1.0<br>(217/217 tests passed) |
| **TOTALS (Combined)** | **Tokens**<br>**Turns**<br>**Wall Time** | **1,853,763**<br>**43 turns**<br>**372.0s** | **562,012**<br>**15 turns**<br>**185.0s** | **−1,291,751 tokens**<br>**−28 turns**<br>**−187.0s** | **−69.7% tokens**<br>**−65.1% turns**<br>**−50.3% time** | **100% Pass** |

---

## 2. Why Did Devrith Win Decisively?

### Root Cause of Baseline Waste
On both tasks, the unassisted baseline agent followed an expensive exploratory loop:
1. `grep` or `find` across directories to find where similar functions live.
2. `view_file` on large sections of the target source module (`src/click/formatting.py` or `src/requests/utils.py`) to understand formatting/signatures.
3. `view_file` on `tests/test_formatting.py` or `tests/test_utils.py` to see how tests are structured.
4. Second `view_file` call on the tail of the test file to find the line numbers needed for `replace_file_content`.
5. Iterative edits and trial `pytest` commands.
This inflated turn count to **20–23 turns** and consumed over **1.85 million tokens**.

### How `feature_workflow` Eliminated 65% of Turns
Devrith compacted the entire orientation and impact mapping phase into **ONE call**:
`arch-sidecar feature "<task>" <path>`

In **~150–350ms**, it delivered:
1. **Target Identification:** Pinned the exact module (`src/click/formatting.py` or `src/requests/utils.py`).
2. **Anchor Implementation:** Sliced the closest matching existing function (`HelpFormatter.write_dl` or `urldefragauth`) with **exact 1-indexed line numbers**.
3. **Cross-File Dependency Signatures:** Displayed helper definitions (`measure_table`, `iter_rows`, `term_len`) so the agent did not need to search for them.
4. **Fast-Path Test Context:** Included the **exact 1-indexed tail of the test file** (`tests/test_formatting.py` or `tests/test_utils.py`).

Because the agent had exact 1-indexed line numbers for **both the source file and the test file from turn 1**, it made its edits immediately without any intermediate `view_file` or `grep` turns.

---

## 3. The Evidence Ledger: "Verify Before Claiming Success"

Instead of an agent saying "I think this works", `arch-sidecar feature ... --verify` runs compiler-grade preflight (<15ms) followed by targeted test execution and outputs a structured **Verification & Evidence Ledger**:

```text
📋 Verification & Evidence Ledger:
  [Statically Verified]
    ✓ AST syntax valid across 2 file(s): src/requests/utils.py, tests/test_utils.py
    ✓ Two-pass lexical scope binding passed: 0 undefined or hallucinated symbols
    ✓ Cross-module local import resolution passed: all imported local symbols exist
    ✓ Module import DAG check passed: 0 circular import cycles detected
  [Runtime Verified]
    ✓ Test suite passed: `PYTHONPATH=src python3 -m pytest -q -p no:cacheprovider tests/test_utils.py` (exit 0, 217 passed in 1338.03ms)
  [Unverified / Residual Risks]
    ✓ None — all static preflight and runtime test checks passed.
```

If an agent introduces a hallucinated identifier or a broken import, preflight catches it in milliseconds before wasting time on test execution.
