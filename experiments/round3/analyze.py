"""Join Round 3 subagent telemetry with objective grades.
Usage: python3 experiments/round3/analyze.py experiments/round3/runs.json experiments/round3/grades.json > experiments/round3/results.json
"""
import json
import statistics as st
import sys
from datetime import datetime

BRAIN = "/home/divyam/.gemini/antigravity-cli/brain"

def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _count_tool_calls(step: dict) -> tuple[int, int]:
    calls, sidecar = 0, 0
    for c in step.get("tool_calls", []) or []:
        calls += 1
        dumped = json.dumps(c)
        if "arch-sidecar" in dumped or "arch_sidecar" in dumped:
            sidecar += 1
    return calls, sidecar


def _tool_result_chars(step: dict) -> int:
    if step.get("type") not in ("PLANNER_RESPONSE", "USER_INPUT") and isinstance(step.get("content"), str):
        return len(step["content"])
    return 0


def telemetry(conv_id):
    path = f"{BRAIN}/{conv_id}/.system_generated/logs/transcript_full.jsonl"
    steps = [json.loads(l) for l in open(path)]
    inp = sum(s.get("input_tokens", 0) for s in steps)
    cache = sum(s.get("cache_read_tokens", 0) for s in steps)
    out = sum(s.get("output_tokens", 0) for s in steps)
    calls, sidecar_calls = 0, 0
    tool_result_chars = 0
    for s in steps:
        c, sc = _count_tool_calls(s)
        calls += c
        sidecar_calls += sc
        tool_result_chars += _tool_result_chars(s)
    times = [ts(s["created_at"]) for s in steps if s.get("created_at")]
    return {
        "input_tokens_uncached": inp,
        "cache_read_tokens": cache,
        "total_input_tokens": inp + cache,
        "output_tokens": out,
        "total_tokens": inp + cache + out,
        "model_turns": sum(1 for s in steps if s.get("type") == "PLANNER_RESPONSE"),
        "tool_calls": calls,
        "sidecar_tool_calls": sidecar_calls,
        "tool_result_chars": tool_result_chars,
        "wall_seconds": round((max(times) - min(times)).total_seconds(), 1) if times else None,
    }

def main(runs_path, grades_path):
    runs = json.load(open(runs_path))
    grades = json.load(open(grades_path))
    rows = {}
    for name, cid in runs.items():
        rows[name] = {"conversation_id": cid, **telemetry(cid), **grades.get(name, {})}
    print(json.dumps(rows, indent=2, default=str))

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
