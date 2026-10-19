# A/B Prompts (identical except the TOOLING block)

## Shared header
You are a coding agent in a controlled experiment. Work ONLY inside WORKDIR={dir}. Never read or modify any other directory under /tmp/ab_runs. Do not ask questions, do not use phone / agent-calling / browser tools. Work autonomously until the task is complete, then write WORKDIR/REPORT.md and reply with one line: DONE.

## TOOLING — baseline
You must NOT use arch-sidecar (do not run the `arch-sidecar` command or import `arch_sidecar`). Use normal tools only: shell commands (ls, grep, find, sed, python), file viewing, and file editing.

## TOOLING — sidecar
The `arch-sidecar` CLI is installed. Use it as your primary way to orient and, where applicable, to transform code:
- `arch-sidecar plan "<task>" <path> [--budget N]` (task-aware context: whole-file vs symbols vs callers, plus uncertainty report)
- `arch-sidecar workflow "<task>" <path> --from F --to T --symbols A,B --test "<cmd>" [--dry-run]` (plan, preflight, deterministic move, test gate, rollback)
- `arch-sidecar locate`, `arch-sidecar decouple`, `arch-sidecar digest`
You may also use normal shell and file tools to verify or fix its output.

## T1 anime-cli (refactor)
Task: In {dir}, move the class `SleepInhibitor` and the function `start_cloudflared_tunnel` out of `cli.py` into a new module `system.py` at the repo root. `cli.py` must keep working and `from cli import SleepInhibitor, start_cloudflared_tunnel` must still work (backward compatibility). `system.py` must import only what it needs. Verify with: `cd {dir} && python3 -m pytest -q -p no:cacheprovider tests` (all tests must pass).
REPORT.md: files changed, verification command and result.

## T2 requests (refactor)
Task: In {dir}, move the functions `to_key_val_list` (including all its `@overload` stubs) and `from_key_val_list` out of `src/requests/utils.py` into a new module `src/requests/_kv.py`. `from requests.utils import to_key_val_list, from_key_val_list` must keep working, as must `requests.sessions` and `requests.models`. `_kv.py` must import only what it needs. IMPORTANT: the system has another `requests` installed; always test the local source with: `cd {dir} && PYTHONPATH=src python3 -m pytest -q -p no:cacheprovider tests/test_utils.py` (all tests must pass).
REPORT.md: files changed, verification command and result.

## T3 flask (audit, read-only)
Task: In {dir} (Flask source under src/flask), do NOT modify code. Produce an accurate audit in REPORT.md answering:
1. Trace the request-handling path: which method is the WSGI entry point, how the request context object is created and pushed, and which methods dispatch to the view function. Give file:line for each.
2. Is there a separate `RequestContext` class in this version? Explain precisely what `RequestContext` is here.
3. Where is `flask.url_for` implemented and what does it delegate to? Give file:line.
Only state things you verified in the source.
