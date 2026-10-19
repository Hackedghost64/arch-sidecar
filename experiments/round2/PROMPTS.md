# Round 2 Prompts (Devrith vs Vanilla Baseline)

## Shared Instructions
You are a coding agent in a controlled experiment. Work ONLY inside WORKDIR={dir}. Never read or modify any other directory under /tmp/round2_runs. Do not ask questions, do not use phone / agent-calling / browser tools. Work autonomously until the task is complete, then write WORKDIR/REPORT.md and reply with one line: DONE.

## TOOLING — baseline
You must NOT use arch-sidecar (do not run the `arch-sidecar` command or import `arch_sidecar`). Use normal tools only: shell commands (ls, grep, find, sed, python), file viewing, and file editing.

## TOOLING — Devrith sidecar
The `arch-sidecar` CLI (Devrith engine) is installed. Use it to orient, plan, and execute transformations:
- `arch-sidecar plan "<task>" <path>` (task-aware context: detects compact files, rankings, dependencies, and dynamic reflection hazards)
- `arch-sidecar workflow "<task>" <path> --from F --to T --symbols A,B --test "<cmd>" [--dry-run]` (safe transformation pipeline with preflight and test gate)
- `arch-sidecar decouple`, `arch-sidecar locate`, `arch-sidecar digest`, `arch-sidecar uncertainty`
You may also use normal shell and file tools to inspect or edit files.

## Task R1: Compact File Endpoint (Negative Control)
Task: In {dir}/user_routes.py (an 86-line FastAPI routes module), add a new GET endpoint:
`@router.get("/watchlist/count")` that counts the items in the user's watchlist by calling `await db.get_watchlist()` and returns `{"ok": True, "count": len(items)}`.
Verify with: `cd {dir} && python3 -c "from user_routes import router; assert '/user/watchlist/count' in [r.path for r in router.routes]"`
REPORT.md: files changed, endpoint added, verification result.

## Task R2: Pallets Click Refactoring (Complex Cross-Repo)
Task: In {dir} (Click source repo), move the class `HelpFormatter` (along with any helper functions it directly requires, such as `measure_table`, `wrap_text`, `iter_rows`) out of `src/click/formatting.py` into `src/click/_formatter.py`.
`from click.formatting import HelpFormatter` must still work (backward compatibility alias).
Verify with: `cd {dir} && PYTHONPATH=src python3 -m pytest -q -p no:cacheprovider tests/test_formatting.py` (all 212 tests must pass).
REPORT.md: symbols moved, target file, verification result.

## Task R3: Hazard Prevention (The Crash Gate)
Task: In {dir}, investigate decoupling `dispatch_action` from `service.py` into a new module `actions.py`.
Notice: `dispatch_action` is tightly coupled to `get_state()` and `AppState` in `service.py`.
Your goal: Safely refactor or determine if the cut introduces an unsafe circular import hazard. If unsafe, document the circular dependency in REPORT.md and ensure `python3 -m pytest -q tests/test_service.py` continues to pass without introducing a broken circular import.
REPORT.md: refactoring outcome, dependency analysis, verification result.
