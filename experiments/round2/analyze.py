"""Join subagent telemetry (real model-reported tokens) with objective grades.

Usage: python3 analyze.py runs.json grades.json > results.json
runs.json maps run_name -> conversation_id.
"""
import json
import statistics as st
import sys
from datetime import datetime

BRAIN = "/home/divyam/.gemini/antigravity-cli/brain"


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def telemetry(conv_id):
    path = f"{BRAIN}/{conv_id}/.system_generated/logs/transcript_full.jsonl"
    steps = [json.loads(l) for l in open(path)]
    inp = sum(s.get("input_tokens", 0) for s in steps)
    cache = sum(s.get("cache_read_tokens", 0) for s in steps)
    out = sum(s.get("output_tokens", 0) for s in steps)
    calls, sidecar_calls = 0, 0
    tool_result_chars = 0
    for s in steps:
        for c in s.get("tool_calls", []) or []:
            calls += 1
            if "arch-sidecar" in json.dumps(c) or "arch_sidecar" in json.dumps(c):
                sidecar_calls += 1
        if s.get("type") not in ("PLANNER_RESPONSE", "USER_INPUT") and isinstance(s.get("content"), str):
            tool_result_chars += len(s["content"])
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


def agg(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return {"mean": round(st.mean(vals), 1), "min": min(vals), "max": max(vals), "n": len(vals)}


def main(runs_path, grades_path):
    runs = json.load(open(runs_path))
    grades = json.load(open(grades_path))
    rows = {}
    for name, cid in runs.items():
        rows[name] = {"conversation_id": cid, **telemetry(cid), **grades.get(name, {})}
    summary = {}
    for name, r in rows.items():
        task, _, cond, _ = name.split("_")
        summary.setdefault(task, {}).setdefault(cond, []).append(r)
    table = {}
    for task, conds in summary.items():
        table[task] = {}
        for cond, rs in conds.items():
            table[task][cond] = {k: agg([r.get(k) for r in rs]) for k in
                                 ("total_tokens", "total_input_tokens", "output_tokens", "tool_calls",
                                  "model_turns", "wall_seconds", "score")}
    print(json.dumps({"per_run": rows, "summary": table}, indent=2, default=str))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
