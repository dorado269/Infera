#!/usr/bin/env python3
"""A stand-in for `claude -p --output-format json` in tests (agent_sys.yaml cli_path): no model is called.

$AGENT_SYS_FAKE_PARSE is a JSON file saying what to do: {"structured_output": {...}, "cost": 0.01, "sleep": 0,
"sleep_first": 0 (only the first call sleeps), "is_error": false, "result": "...", "stdout": "<raw text instead of
JSON>", "exit": 0}. Every call is appended to $AGENT_SYS_FAKE_LOG (if set) as one JSON line:
{"argv": [...], "stdin": "...", "env": {MAX_THINKING_TOKENS}}.
"""

import json
import os
import sys
import time

spec = json.load(open(os.environ["AGENT_SYS_FAKE_PARSE"], encoding="utf-8"))
stdin = sys.stdin.read()
log = os.environ.get("AGENT_SYS_FAKE_LOG")
first = not (log and os.path.exists(log))
if log:
    with open(log, "a", encoding="utf-8") as f:
        env = {"MAX_THINKING_TOKENS": os.environ.get("MAX_THINKING_TOKENS")}
        call = {"argv": sys.argv[1:], "stdin": stdin, "env": env}
        f.write(json.dumps(call, ensure_ascii=False) + "\n")
time.sleep(spec.get("sleep", 0) + (spec.get("sleep_first", 0) if first else 0))
if "stdout" in spec:
    print(spec["stdout"])
    sys.exit(spec.get("exit", 1))
out = {
    "type": "result",
    "subtype": "success",
    "is_error": bool(spec.get("is_error")),
    "num_turns": 2,
    "duration_ms": 1000,
    "total_cost_usd": spec.get("cost", 0.0123),
    "result": spec.get("result", ""),
}
if "structured_output" in spec:
    out["structured_output"] = spec["structured_output"]
print(
    json.dumps(
        [{"type": "system", "subtype": "init"}, out] if spec.get("verbose") else out,
        ensure_ascii=False,
    )
)
sys.exit(spec.get("exit", 0))
