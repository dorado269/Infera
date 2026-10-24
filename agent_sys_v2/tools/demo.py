"""Make a demo AGENT_SYS_HOME: one infera-perf campaign with three machines, tasks in most states, results in the
ledger and a plan of drafted experiments, so the console can be looked at without running anything.

  python tools/demo.py <home>          # replaces <home>
  AGENT_SYS_TMUX=asys-demo agent-sys-v2 serve --home <home> --sock /tmp/agent_sys-$(id -u)/demo.sock

The numbers are made up. Every run of the campaign replays a recorded session (tests/fixtures/events) instead of
calling a model: starting a task here costs nothing.
"""

import json
import shutil
import sys
from pathlib import Path

from agent_sys_v2 import ops
from agent_sys_v2.model import new_state, write_state

EVENTS = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "events"
DONE = (
    "状态: 已完成\n下一步: 无\n完成时间: 2026-10-21 10:00\n\n- [x] 部署\n- [x] 质量门\n- [x] 测量\n"
)


def main(home):
    home = Path(home)
    shutil.rmtree(home, ignore_errors=True)
    ops.init_home(home)
    pdir = ops.new_project(home, "glm52-agentx", "infera-perf", title="GLM-5.2 4P4D AgentX")
    cfg = (pdir / "project.yaml").read_text(encoding="utf-8")
    cfg = cfg.replace(
        'goal: ""', 'goal: "AgentX C64 的 tok/s/GPU 提升 20%，P90 interactivity ≥ 30 tok/s/user"'
    )
    cfg = cfg.replace(
        "machines: []",
        "machines:\n"
        + "".join(
            f"  - {{name: smc-a16-{n}, gpus: 8, note: MI300X}}\n" for n in (19, 20, 21)
        ).rstrip(),
    )
    cfg += f"  replay: {{events: {EVENTS / 'p2_queue.jsonl'}, speed: 5}}   # demo: runs replay, no model\n"
    (pdir / "project.yaml").write_text(cfg, encoding="utf-8")

    def task(tid, body, fm="", state=None, progress=None, report=None, results=()):
        path = pdir / "tasks" / f"{tid}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\n{fm}---\n{body}\n" if fm else f"{body}\n", encoding="utf-8")
        ws = pdir / "workspaces" / tid
        if state:
            write_state(ws, state)
        if progress:
            ws.mkdir(parents=True, exist_ok=True)
            (ws / "progress.md").write_text(progress, encoding="utf-8")
        if report:
            (ws / "report.md").write_text(report, encoding="utf-8")
        for name, doc in results:
            (ws / "results").mkdir(parents=True, exist_ok=True)
            (ws / "results" / f"{name}.json").write_text(
                json.dumps(doc, ensure_ascii=False, indent=1)
            )

    def st(status, since, detail=None, **more):
        s = new_state(status, detail)
        s.update(since=since, created="2026-10-20T01:00:00Z", **more)
        s["started"] = since if status != "queued" else ""
        return s

    def res(
        label, value, t, p90, role="candidate", decision=None, status="valid", change="", notes=""
    ):
        doc = {
            "schema": "infera.result/v1",
            "label": label,
            "time": t,
            "role": role,
            "change": change,
            "metrics": {
                "tok_s_gpu": value,
                "p90_interactivity": p90,
                "requests": 3120,
                "errors": 0,
            },
            "workload": {"client": "agentx", "conc": 64, "duration_s": 3600},
        }
        if decision or status == "void":
            doc["verdict"] = {
                "status": status,
                **({"decision": decision} if decision else {}),
                "notes": notes,
            }
        return doc

    seen = {"seen_at": "2026-10-23T00:00:00Z"}
    task(
        "baseline/b0",
        "## 任务描述\n\n- 部署：atom_4p4d_glm5.2，main\n- 负载：AgentX C64\n",
        "title: 基线 C64\nmachines: [smc-a16-19]\n",
        st("done", "2026-10-20T06:00:00Z", finished="2026-10-20T06:00:00Z", cost_usd=11.2, **seen),
        DONE,
        "## 结论\n\n基线 tok/s/GPU 9,850（噪声 ±1.6%），P90 interactivity 29.1。\n",
        [
            ("c64", res("c64", 9850.0, "2026-10-20T05:40:00Z", 29.1, role="baseline")),
            (
                "c64-repeat",
                res("c64-repeat", 9790.0, "2026-10-20T05:55:00Z", 29.3, decision="noise"),
            ),
        ],
    )
    task(
        "diagnose/d1",
        "## 任务描述\n\n- 对象：@baseline/b0\n- 问题：瓶颈在哪一层\n",
        "title: 第一轮诊断\ndepends_on: [baseline/b0]\nreview: true\n",
        st("done", "2026-10-21T02:00:00Z", finished="2026-10-21T02:00:00Z", cost_usd=8.4, **seen),
        DONE,
        "## 结论\n\nprefill 饱和（FCFS 队头阻塞）[M]；decode 最忙 rank 决定步长 [M]；API 进程 gen-2 GC [I]。\n",
    )
    experiments = [
        (
            "spf",
            "SPF 调度",
            "smc-a16-19",
            "done",
            11350.0,
            30.2,
            "accept",
            "--schedule-policy shortest-prefill-first",
        ),
        (
            "tokenizer",
            "tokenizer workers ×4",
            "smc-a16-20",
            "in_review",
            11140.0,
            30.0,
            "accept",
            "--tokenizer-worker-num 4",
        ),
        ("dcp4", "DCP4 decode", "smc-a16-21", "done", 8190.0, 26.0, "reject", "decode DCP4"),
    ]
    for i, (name, title, machine, status, value, p90, decision, change) in enumerate(experiments):
        when = f"2026-10-21T{9 + 3 * i:02d}:00:00Z"
        task(
            f"experiment/{name}",
            f"## 任务描述\n\n- 假设：{change}\n",
            f"title: {title}\nparent: diagnose/d1\nmachines: [{machine}]\n",
            st(
                status,
                when,
                {"kind": "review"} if status == "in_review" else None,
                finished=when,
                cost_usd=10.0 + i,
                **(seen if status == "done" else {}),
            ),
            DONE,
            f"## 结论\n\n{decision}。\n",
            [
                (
                    f"c64-{name}",
                    res(f"c64-{name}", value, when, p90, decision=decision, change=change),
                )
            ],
        )
    task(
        "experiment/hicache",
        "## 任务描述\n\n- 假设：HiCache write-back\n",
        "title: HiCache write-back\nparent: diagnose/d1\nmachines: [smc-a16-21]\n",
        st(
            "needs_you",
            "2026-10-22T03:00:00Z",
            {"kind": "checks_failed", "failed": ["results"], "passed": 1, "total": 2},
            cost_usd=10.2,
        ),
        "状态: 已完成\n下一步: 无\n",
        results=[
            (
                "c64-hicache",
                res(
                    "c64-hicache",
                    12100.0,
                    "2026-10-22T02:30:00Z",
                    30.8,
                    status="void",
                    notes="测量窗口内有别人的 sweep 跑在 GPU 3",
                ),
            )
        ],
    )
    task(
        "experiment/gc",
        "## 任务描述\n\n- 假设：API 进程 gen-2 GC 停顿\n",
        "title: API 进程 GC\nparent: diagnose/d1\nmachines: [smc-a16-21]\n",
        st("queued", "2026-10-22T05:00:00Z"),
    )
    task(
        "experiment/router",
        "## 任务描述\n\n- 假设：decode 请求均衡\n",
        "title: decode request balance\nparent: diagnose/d1\nmachines: [smc-a16-19]\n",
    )
    task(
        "experiment/mooncake",
        "## 任务描述\n\n- 假设：Mooncake QP 数\n",
        "title: Mooncake QP 与发送线程\nparent: diagnose/d1\n",
    )
    task(
        "diagnose/d2",
        "## 任务描述\n\n- 对象：第一轮实验（@experiment/spf、@experiment/tokenizer）\n",
        "title: 第二轮诊断\ndepends_on: [experiment/spf, experiment/tokenizer]\n",
        st("queued", "2026-10-22T05:00:00Z"),
    )
    return pdir


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    print(main(sys.argv[1]))
