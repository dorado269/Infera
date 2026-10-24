"""Runs through the server (runs.py + server.py): real runner processes in replay mode in a tmux of their own, no
model. Start -> running -> done / in_review / needs_you, messages, 停止, a killed runner, a restarted server,
terminal takeover, budget, cleaning up processes, and the run's events as the run view gets them."""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import DONE, EVENTS, T, get, new, ok, replay, run_home, wait, ws
from fastapi.testclient import TestClient

from agent_sys_v2 import runs
from agent_sys_v2.model import read_state, split_frontmatter, write_state
from agent_sys_v2.scan import load_project
from agent_sys_v2.server import create_app


@pytest.fixture
def home(tmp_path):
    """Project r with autorun off: these tests start their tasks by hand (the scheduler is test_sched.py's)."""
    return run_home(tmp_path, "autorun: false\n")


@pytest.fixture
def client(home):
    with TestClient(create_app(home)) as c:
        yield c


def test_round_end_by_progress(client, home):
    for name in ("asked", "done", "review", "questions"):
        new(
            client,
            name,
            review=name == "review",
            **({"agent": replay("p2_queue", speed=4)} if name == "questions" else {}),
        )
    for name in ("done", "review"):
        (ws(home, name) / "progress.md").write_text(DONE, encoding="utf-8")
        (ws(home, name) / "report.md").write_text("## 结论\n", encoding="utf-8")
    for name in ("asked", "done", "review", "questions"):
        t = ok(client.post(f"{T}/{name}/actions/start"))["task"]
        assert t["status"] == "running"
    time.sleep(1)  # the agent of `questions` writes questions.md during its round
    (ws(home, "questions") / "questions.md").write_text(
        "| # | 问题 | 建议 | 你的意见 |\n|---|---|---|---|\n| Q1 | a | b | |\n| Q2 | c | d | |\n",
        encoding="utf-8",
    )
    asked = wait(client, "asked", lambda t: t["status"] != "running")
    assert (asked["status"], asked["detail"]) == ("needs_you", {"kind": "asked"})
    assert asked["last_message"] == "got it" and asked["cost_usd"] == pytest.approx(
        0.01712215, abs=1e-5
    )
    run = asked["runs"][0]
    assert (run["kind"], run["exit"], run["live"], run["resume"]) == (
        "replay",
        "idle",
        False,
        False,
    )
    done = wait(client, "done", lambda t: t["status"] != "running")
    assert (done["status"], done["unread"], done["finished"] != "") == ("done", True, True)
    assert wait(client, "review", lambda t: t["status"] != "running")["status"] == "in_review"
    q = wait(client, "questions", lambda t: t["status"] != "running")
    assert q["detail"] == {"kind": "questions", "count": 2, "path": "questions.md"}
    assert q["questions"]["count"] == 2
    home_page = ok(client.get("/api/home"))
    assert home_page["cost_today_usd"] == pytest.approx(4 * 0.01712215, abs=1e-4)
    assert [t["id"] for t in home_page["needs_you"]] == ["develop/asked", "develop/questions"]


def test_resume_with_rules_update_and_a_done_task_that_stays_done(client, home):
    new(client, "a", agent=replay(["p2_queue", "p4_k9_resume"], speed=20))
    ok(client.post(f"{T}/a/actions/start"))
    wait(client, "a", lambda t: t["status"] == "needs_you")
    (home / "rules").mkdir()
    (home / "rules/new.md").write_text("新规则", encoding="utf-8")
    r = ok(client.post(f"{T}/a/messages", json={"text": "继续"}))
    assert r["delivery"] == "resume" and r["task"]["status"] == "running"
    t = get(client, "a")
    cfg = json.loads(
        (ws(home, "a") / ".agent_sys/runs" / t["runs"][0]["id"] / "launch.json").read_text()
    )
    assert (
        cfg["resume"]
        and cfg["message"].startswith("（agent_sys 规则有更新，请先读 `")
        and cfg["message"].endswith("继续")
    )
    assert cfg["replay"]["events"].endswith("p4_k9_resume.jsonl")
    assert cfg["cost_start_usd"] == pytest.approx(0.01712215, abs=1e-5)
    (ws(home, "a") / "progress.md").write_text(DONE, encoding="utf-8")
    t = wait(client, "a", lambda t: t["status"] != "running")
    assert t["status"] == "done" and len(t["runs"]) == 2
    ok(client.post(f"{T}/a/actions/mark_read"))
    # a follow-up question: progress still done, no report change -> stays done, not unread again
    ok(client.post(f"{T}/a/messages", json={"text": "再问一句"}))
    t = wait(client, "a", lambda t: t["status"] != "running")
    assert (t["status"], t["unread"]) == ("done", False)
    cfg = json.loads(
        (ws(home, "a") / ".agent_sys/runs" / t["runs"][0]["id"] / "launch.json").read_text()
    )
    assert cfg["message"] == "再问一句"  # the rules are what the session saw last time: no hint


def test_a_moved_task_resumes_its_session_and_is_told_where_it_went(client, home):
    new(client, "a", agent=replay(["p2_queue", "p4_k9_resume"], speed=20))
    ok(client.post(f"{T}/a/actions/start"))
    sid = wait(client, "a", lambda t: t["status"] == "needs_you")["session_id"]
    (home / "campaigns/r/tasks/bug_fix").mkdir()
    assert ok(client.post(f"{T}/a/rename", json={"type": "bug_fix"}))["id"] == "bug_fix/a"
    r = ok(client.post("/api/projects/r/tasks/bug_fix/a/messages", json={"text": "继续"}))
    assert r["delivery"] == "resume"
    t = ok(client.get("/api/projects/r/tasks/bug_fix/a"))
    assert t["session_id"] == sid and len(t["runs"]) == 2
    cfg = json.loads(
        (
            home
            / "campaigns/r/workspaces/bug_fix/a/.agent_sys/runs"
            / t["runs"][0]["id"]
            / "launch.json"
        ).read_text()
    )
    assert cfg["resume"] and cfg["message"].startswith(
        "（这个任务已从 `tasks/develop/a.md` 移到 `tasks/bug_fix/a.md`"
    )
    assert "请先读 `" in cfg["message"] and cfg["message"].endswith(
        "继续"
    )  # the new system.md names the new paths
    assert "moved_from" not in json.loads(
        (home / "campaigns/r/workspaces/bug_fix/a/.agent_sys/state.json").read_text()
    )


def test_messages_while_running_and_stop(client, home):
    new(client, "a", agent=replay("p2_queue", speed=1))  # a 20 s command first
    ok(client.post(f"{T}/a/actions/start"))
    t = wait(client, "a", lambda t: t["detail"].get("current"))
    assert (
        t["detail"]["current"].startswith("Bash sleep 20") and t["runs"][0]["activity"] == "working"
    )
    assert ok(client.post(f"{T}/a/messages", json={"text": "只看 C96"}))["delivery"] == "after_tool"
    t = ok(client.post(f"{T}/a/actions/stop"))["task"]
    assert (t["status"], t["detail"]) == ("needs_you", {"kind": "stopped"})
    assert get(client, "a")["runs"][0]["exit"] == "stopped"

    new(client, "w", agent=replay("p5_main", speed=10))  # then waits 2.5 h in the background
    ok(client.post(f"{T}/w/actions/start"))
    t = wait(client, "w", lambda t: t["detail"].get("kind") == "waiting")
    assert "p5_main_flag" in t["detail"]["waiting_for"] and t["runs"][0]["background"]
    assert ok(client.post(f"{T}/w/messages", json={"text": "进展如何？"}))["delivery"] == "now"
    run = t["runs"][0]["id"]
    end = time.time() + 10
    while not any(
        "进展如何" in json.dumps(e, ensure_ascii=False) and e["type"] == "assistant"
        for e in ok(client.get(f"{T}/w/runs/{run}/events"))["events"]
    ):
        assert time.time() < end
        time.sleep(0.2)
    assert get(client, "w")["detail"]["kind"] == "waiting"  # the background command is still there
    ok(client.post(f"{T}/w/actions/stop"))
    assert get(client, "w")["detail"] == {"kind": "stopped"}


def test_killed_runner_and_resume(client, home):
    new(client, "w", agent=replay(["p5_main", "p2_queue"], speed=10))
    ok(client.post(f"{T}/w/actions/start"))
    t = wait(client, "w", lambda t: t["detail"].get("kind") == "waiting")
    rec = json.loads(
        (ws(home, "w") / ".agent_sys/runs" / t["runs"][0]["id"] / "run.json").read_text()
    )
    os.kill(rec["pid"], signal.SIGKILL)  # the machine went down, say
    t = wait(client, "w", lambda t: t["status"] != "running")
    assert (
        t["detail"]["kind"] == "wait_interrupted" and "p5_main_flag" in t["detail"]["waiting_for"]
    )
    assert t["runs"][0]["exit"] == "interrupted"
    ok(client.post(f"{T}/w/actions/resume", json={"text": "继续"}))
    cfg = json.loads(
        (
            ws(home, "w") / ".agent_sys/runs" / get(client, "w")["runs"][0]["id"] / "launch.json"
        ).read_text()
    )
    assert "后台等待的命令已经结束" in cfg["message"]
    wait(client, "w", lambda t: t["status"] != "running")


def test_server_restart_picks_up_runs(home):
    with TestClient(create_app(home)) as c:
        new(c, "a", agent=replay("p2_queue", speed=4))
        new(c, "b", agent=replay("p2_queue", speed=2))
        ok(c.post(f"{T}/a/actions/start"))
        ok(c.post(f"{T}/b/actions/start"))
    # no server: a's runner ends meanwhile; b's is still running when the next server starts
    a_run = sorted((ws(home, "a") / ".agent_sys/runs").iterdir())[-1]
    end = time.time() + 30
    while not runs.read_json(a_run / "run.json").get("ended"):
        assert time.time() < end
        time.sleep(0.2)
    assert read_state(ws(home, "a"))["status"] == "running"  # nobody moved it on yet
    with TestClient(create_app(home)) as c:
        assert wait(c, "a", lambda t: t["status"] != "running", 5)["detail"] == {"kind": "asked"}
        t = get(c, "b")
        assert t["status"] == "running" and t["runs"][0]["live"]
        assert wait(c, "b", lambda t: t["status"] != "running")["runs"][0]["exit"] == "idle"


def test_takeover_and_end_takeover(client, home):
    new(client, "a")
    ok(client.post(f"{T}/a/actions/start"))
    wait(client, "a", lambda t: t["status"] == "needs_you")
    r = ok(client.post(f"{T}/a/actions/takeover"))
    assert r["takeover"] == {
        "window": "agent_sys:a",
        "attach": f"tmux -L {runs.TMUX} attach -t agent_sys:a",
    }
    t = r["task"]
    assert (t["status"], t["detail"]["kind"]) == ("running", "terminal")
    t = wait(client, "a", lambda t: t["runs"][0]["live"])
    assert t["runs"][0]["kind"] == "terminal" and t["runs"][0]["attach"] == r["takeover"]["attach"]
    assert "a" in runs.window_names()
    msg = client.post(f"{T}/a/messages", json={"text": "x"})
    assert msg.status_code == 409 and "终端" in msg.json()["error"]
    (ws(home, "a") / "progress.md").write_text(
        DONE, encoding="utf-8"
    )  # what you did in the terminal
    t = ok(client.post(f"{T}/a/actions/end_takeover"))["task"]
    assert t["status"] == "done"
    assert get(client, "a")["runs"][0]["exit"] == "terminal_exit"


def test_budget(client, home):
    new(client, "a", agent={"budget_usd": 0.01, **replay("p2_queue", speed=20)})
    ok(client.post(f"{T}/a/actions/start"))
    t = wait(client, "a", lambda t: t["status"] != "running")
    assert t["cost_usd"] > t["budget_usd"]
    state = read_state(ws(home, "a"))
    write_state(
        ws(home, "a"), {**state, "detail": {"kind": "budget"}}
    )  # as the CLI's budget stop leaves it
    client.app.state.store.sync(also=[("r", "develop/a")])
    r = client.post(f"{T}/a/messages", json={"text": "继续"})
    assert r.status_code == 409 and "追加预算" in r.json()["error"]
    r = ok(client.post(f"{T}/a/actions/add_budget", json={"usd": 1}))
    assert r["budget_usd"] == 1.01 and r["task"]["status"] == "running"
    fm, _ = split_frontmatter((home / "campaigns/r/tasks/develop/a.md").read_text())
    assert fm["agent"]["budget_usd"] == 1.01
    wait(client, "a", lambda t: t["status"] != "running")


def test_cancel_a_running_task(client, home):
    new(client, "a", agent=replay("p2_queue", speed=1))
    ok(client.post(f"{T}/a/actions/start"))
    t = wait(client, "a", lambda t: t["runs"] and t["runs"][0]["live"])
    rec = json.loads(
        (ws(home, "a") / ".agent_sys/runs" / t["runs"][0]["id"] / "run.json").read_text()
    )
    assert ok(client.post(f"{T}/a/actions/cancel"))["task"]["status"] == "cancelled"
    assert runs.wait_gone([rec["pid"]], 5) == []


def test_clean_kills_only_what_it_should(home, tmp_path):
    pdir = home / "campaigns/r"
    (pdir / "tasks/develop/a.md").write_text("x", encoding="utf-8")
    write_state(
        ws(home, "a"),
        {
            **json.loads("{}"),
            "version": 1,
            "status": "needs_you",
            "detail": {"kind": "asked"},
            "session_id": "sid-a",
            "runs": 1,
            "run_id": "r1",
            "cost_usd": 0,
            "session_cost_usd": 0,
        },
    )
    t = load_project(pdir).tasks["develop/a"]
    sleep = [sys.executable, "-c", "import time; time.sleep(60)"]
    run1, other = runs.runs_dir(t) / "r1", tmp_path / "other_run"
    procs = {
        "runner": subprocess.Popen(
            sleep + ["-m", "agent_sys_v2.runner", str(run1)]
        ),  # this run's runner: KILL
        "claude": subprocess.Popen(sleep + ["--resume=sid-a"]),  # the session: TERM
        "claude2": subprocess.Popen(sleep + ["--session-id", "sid-a"]),
        "other_runner": subprocess.Popen(
            sleep + ["-m", "agent_sys_v2.runner", str(other)]
        ),  # another run's runner
        "other_claude": subprocess.Popen(sleep + ["--resume=sid-b"]),  # another session
        "reused": subprocess.Popen(sleep),  # a reused pid
    }
    run1.mkdir(parents=True)
    run2 = runs.runs_dir(t) / "r0"
    run2.mkdir()
    host = runs.HOST
    (run1 / "run.json").write_text(
        json.dumps({"pid": procs["runner"].pid, "host": host, "ended": None})
    )
    (run2 / "run.json").write_text(
        json.dumps({"pid": procs["reused"].pid, "host": host, "ended": None})
    )
    try:
        runs.clean(t)
        for name in ("runner", "claude", "claude2"):
            assert procs[name].wait(5) is not None, name
        for name in ("other_runner", "other_claude", "reused"):
            assert procs[name].poll() is None, name
    finally:
        for p in procs.values():
            p.kill()
            p.wait()


def test_run_events_come_in_pages_and_the_page_is_told_of_new_ones(client, home):
    told = []
    client.app.state.store.listeners.add(lambda c: "run" in c and told.append(c))
    new(client, "a")
    ok(client.post(f"{T}/a/actions/start"))
    t = wait(client, "a", lambda t: t["status"] != "running")
    run = t["runs"][0]["id"]
    lines = (ws(home, "a") / f".agent_sys/runs/{run}/events.jsonl").read_text().splitlines()
    # while it ran, /api/events said which run grew, up to its last line (one SSE per page, no stream per run)
    assert told and {(c["project"], c["task"], c["run"]) for c in told} == {("r", "develop/a", run)}
    assert [c["seq"] for c in told] == sorted({c["seq"] for c in told}) and told[-1]["seq"] <= len(
        lines
    ) - 1
    page = ok(client.get(f"{T}/a/runs/{run}/events"))
    shown = [s for s, line in enumerate(lines) if runs.slim(json.loads(line)) is not None]
    assert ([e["seq"] for e in page["events"]], page["upto"], page["first"]) == (
        shown,
        len(lines) - 1,
        0,
    )
    assert [e["seq"] for e in ok(client.get(f"{T}/a/runs/{run}/events?after=5"))["events"]] == [
        s for s in shown if s > 5
    ]
    # the end first, then earlier pages; a page that starts in the middle carries the session's state
    end = ok(client.get(f"{T}/a/runs/{run}/events?tail=4"))
    assert [e["seq"] for e in end["events"]] == shown[-4:] and end["first"] == shown[-4]
    assert {e["subtype"] for e in end["carry"]} <= {
        "session_state_changed",
        "background_tasks_changed",
    }
    before = ok(client.get(f"{T}/a/runs/{run}/events?tail=4&before={end['first']}"))
    assert [e["seq"] for e in before["events"]] == shown[-8:-4]
    assert ok(client.get(f"{T}/a/runs/{run}/events?tail=1000"))["events"] == page["events"]
    # one event whole
    assert ok(client.get(f"{T}/a/runs/{run}/events/{shown[1]}")) == runs.slim(
        {"seq": shown[1], **json.loads(lines[shown[1]])}, None
    )
    assert client.get(f"{T}/a/runs/{run}/events/{len(lines)}").status_code == 404
    assert ok(client.get(f"{T}/a/runs/{run}/launch"))["markdown"].startswith(f"# 运行 {run}")
    assert client.get(f"{T}/a/runs/nope/events").status_code == 404
    # packed: the browser asks for gzip
    r = client.get(f"{T}/a/runs/{run}/events", headers={"Accept-Encoding": "gzip"})
    assert r.headers.get("content-encoding") == "gzip" or len(r.content) < 1024


def test_slim_keeps_what_the_run_view_shows():
    big = "x" * (runs.PREVIEW + 10)
    e = {
        "seq": 3,
        "type": "user",
        "ts": 1.0,
        "uuid": "u",
        "session_id": "s",
        "tool_use_result": {"stdout": big},
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": [
                        {"type": "text", "text": big},
                        {"type": "image", "source": {"data": "AAAA"}},
                    ],
                },
                {"type": "tool_result", "tool_use_id": "t2", "content": "short", "is_error": True},
            ],
        },
    }
    s = runs.slim(e)
    assert set(s) == {"seq", "type", "ts", "message"} and list(s["message"]) == ["content"]
    long, short = s["message"]["content"]
    assert (long["content"], long["clipped"], long["chars"]) == (
        big[: runs.PREVIEW],
        True,
        len(big) + len("\n[image]"),
    )
    assert short == {
        "type": "tool_result",
        "tool_use_id": "t2",
        "is_error": True,
        "content": "short",
        "chars": 5,
    }
    assert runs.slim(e, None)["message"]["content"][0]["content"] == big + "\n[image]"
    a = runs.slim(
        {
            "type": "assistant",
            "ts": 2.0,
            "message": {
                "id": "m",
                "usage": {},
                "content": [
                    {"type": "thinking", "thinking": "hmm", "signature": "sig"},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Write",
                        "input": {"file_path": "a.py", "content": big},
                    },
                ],
            },
        }
    )
    assert a["message"]["content"] == [
        {"type": "thinking", "thinking": "", "chars": 3, "clipped": True},
        {
            "type": "tool_use",
            "id": "t1",
            "name": "Write",
            "input": {"file_path": "a.py", "content": big[: runs.PREVIEW]},
            "clipped": True,
        },
    ]
    assert runs.slim({"type": "result", "subtype": "success", "replay": True, "usage": {}}) == {
        "type": "result",
        "subtype": "success",
        "replay": True,
    }  # tagged 回放 in the view
    assert (
        runs.slim({"type": "assistant", "replay": True, "message": {"content": []}})["replay"]
        is True
    )
    assert (
        runs.slim(
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t"}]}}
        )["message"]["content"][0]["content"]
        == ""
    )
    assert runs.slim({"type": "system", "subtype": "task_progress"}) is None
    assert runs.slim(
        {"type": "system", "subtype": "init", "model": "m", "tools": ["Bash"], "cwd": "/"}
    ) == {"type": "system", "subtype": "init", "model": "m"}


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_slim_events_fold_into_the_same_view(tmp_path):
    """web/run.js folds the slim events of M0's recordings into the items it folds of the whole ones: the same
    lines, and once opened (nothing cut) the same texts."""
    script = tmp_path / "check.mjs"
    script.write_text(f"""
import {{ fold }} from {json.dumps((Path(__file__).resolve().parents[1] / "agent_sys_v2/web/run.js").as_uri())};
import {{ setLang }} from {json.dumps((Path(__file__).resolve().parents[1] / "agent_sys_v2/web/i18n.js").as_uri())};
setLang('zh');
const data = JSON.parse(await new Response(process.stdin).text());
const strip = (items, cut) => items.map(({{ key, whole, wholeIn, wholeOut, chars, ...it }}) => cut
  ? {{ ...it, text: it.kind === 'thinking' ? '' : it.kind === 'note' ? it.text.slice(0, 20) : it.text, input: undefined,
      output: undefined, summary: undefined }} : it);  // a note may quote a cut command
const out = {{}};
for (const [name, r] of Object.entries(data)) {{
  const whole = fold(r.raw).items;
  out[name] = [JSON.stringify(strip(fold(r.full).items)) === JSON.stringify(strip(whole)),
               JSON.stringify(strip(fold(r.slim).items, true)) === JSON.stringify(strip(whole, true))];
}}
console.log(JSON.stringify(out));
""")
    data = {}
    for path in sorted(EVENTS.glob("*.jsonl")):
        raw = [
            {"seq": s, **json.loads(line)} for s, line in enumerate(path.read_text().splitlines())
        ]
        data[path.stem] = {
            "raw": raw,
            "full": [e for e in (runs.slim(e, None) for e in raw) if e],
            "slim": [e for e in (runs.slim(e, 20) for e in raw) if e],
        }
    out = subprocess.run(
        ["node", str(script)], input=json.dumps(data), capture_output=True, text=True, check=True
    )
    assert json.loads(out.stdout) == {name: [True, True] for name in data}


def test_a_run_on_another_host_is_only_shown(client, home):
    new(client, "a")
    state = read_state(ws(home, "a"))
    run_dir = ws(home, "a") / ".agent_sys/runs/20261023-090000"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "id": run_dir.name,
                "kind": "sdk",
                "pid": os.getpid(),
                "host": "elsewhere",
                "started": "2026-10-23T01:00:00Z",
                "ended": None,
            }
        )
    )
    write_state(ws(home, "a"), {**state, "status": "running", "run_id": run_dir.name, "runs": 1})
    client.app.state.store.sync(also=[("r", "develop/a")])
    time.sleep(1.2)  # a few rounds of the Monitor: it must not finish or kill anything
    t = get(client, "a")
    assert (t["status"], t["detail"]["host"], t["runs"][0]["live"], t["runs"][0]["host"]) == (
        "running",
        "elsewhere",
        True,
        "elsewhere",
    )
    for action in ("stop", "takeover"):
        r = client.post(f"{T}/a/actions/{action}")
        assert r.status_code == 409 and "在 elsewhere 上运行" in r.json()["error"]


def test_prompt_change_sent_to_the_agent(client, home):
    new(client, "a")
    ok(client.post(f"{T}/a/actions/start"))
    t = wait(client, "a", lambda t: t["status"] == "needs_you")
    t = ok(
        client.put(
            f"{T}/a",
            json={"prompt": "新的要求", "base_mtime": t["prompt"]["mtime"], "send_to_agent": True},
        )
    )
    assert t["status"] == "running" and t["prompt"]["body"] == "新的要求"
    cfg = json.loads(
        (ws(home, "a") / ".agent_sys/runs" / t["runs"][0]["id"] / "launch.json").read_text()
    )
    assert (
        cfg["message"] == "任务 prompt 已更新（`tasks/develop/a.md`），请重新阅读并按新要求调整。"
    )
    wait(client, "a", lambda t: t["status"] != "running")


def test_a_message_with_line_separators_is_not_lost(tmp_path):
    from agent_sys_v2.runner import Inbox

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    inbox = Inbox(run_dir / "inbox.jsonl")
    texts = ["从网页粘贴的\u2028一段", "另一段\u2029", "NEL\x85", "普通"]
    for text in texts:
        runs.append_inbox(run_dir, {"type": "user", "text": text})
    assert [c["text"] for c in inbox.read()] == texts
    assert runs.unread_messages(run_dir, {}) == texts


def test_window_names_are_tmux_targets(monkeypatch):
    from types import SimpleNamespace

    task, project = SimpleNamespace(name="release_v1.0.0"), SimpleNamespace(id="p.1")
    monkeypatch.setattr(runs, "window_names", lambda: set())
    assert runs.window_name(project, task) == "release_v1_0_0"
    monkeypatch.setattr(runs, "window_names", lambda: {"release_v1_0_0", "release_v1_0_0-p_1"})
    assert runs.window_name(project, task) == "release_v1_0_0-p_1-2"


def test_a_new_session_counts_its_cost_from_zero(client, home):
    """A task whose session record is gone (cleaned up by Claude Code) starts a new session: its cost starts at 0,
    not at the old session's total."""
    new(client, "a", agent={"replay": False})  # not a replay: /bin/false for claude, no model
    w = ws(home, "a")
    write_state(
        w,
        {
            **read_state(w),
            "status": "needs_you",
            "detail": {"kind": "asked"},
            "runs": 2,
            "session_cost_usd": 5.0,
            "cost_usd": 0.5,
        },
    )  # budget $1
    ok(client.post(f"{T}/a/messages", json={"text": "继续"}))
    run = sorted((w / ".agent_sys/runs").iterdir())[-1]
    cfg = json.loads((run / "launch.json").read_text())
    assert (cfg["resume"], cfg["cost_start_usd"]) == (False, 0.0)
