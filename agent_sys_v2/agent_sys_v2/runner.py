"""agent_sys runner: one run of the agent, a process of its own in a tmux window.

  python -m agent_sys_v2.runner <run_dir>                     the agent, through the Claude Agent SDK
  python -m agent_sys_v2.runner <run_dir> --replay <events.jsonl> [--speed N] [--max-gap S]
                                                       a recorded events.jsonl played back, no model (UI work,
                                                       tests, demos); run_dir is created if it does not exist
  python -m agent_sys_v2.runner <run_dir> --terminal          interactive claude in this terminal (在终端中接管)

The runner and the server only share files in run_dir. The runner reads launch.json (the server writes it, see
launch.py) and the new lines of inbox.jsonl: {"type": "user", "text"} is your message, sent into the session at
once; {"type": "stop", "reason"} is 停止 = interrupt(), after which the runner exits. It writes events.jsonl, one
JSON per line: every raw frame of the CLI except thinking_tokens / ui_status / ui_panes, plus its own `runner`
events (connected, send_user, inbox, exit); and run.json: the record of its start, then of its end (model.py).

A round is over when the session is idle, the latest background_tasks_changed is empty, the inbox is
read and the CLI owes no round: a message sent while the session was idle, or a background command that ended
while it was idle, starts one a moment later (M0 p5_main: 0.1 s between the empty list and `running`). With a
background command still running the runner stays (运行中 · 等待中) and Claude Code starts the next round itself.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

from . import checks, i18n
from .i18n import _
from .launch import terminal_argv
from .model import Progress, iso, read_text, write_text

DROP = (
    "thinking_tokens",
    "ui_status",
    "ui_panes",
)  # dozens per round, or a plugin's periodic status
OWED_SECONDS = (
    10  # how long a round the CLI owes may take to start before the runner stops waiting for it
)
STOP_SECONDS = (
    10  # after interrupt(), how long the round may take to end before the runner exits anyway
)
POLL = 0.3  # inbox and exit checks
CUT = 20000  # longer strings (tool output, file contents) are cut in events.jsonl


def cut(x):
    if isinstance(x, str):
        return x if len(x) <= CUT else x[:CUT] + _("…[共 {n} 字符]", n=len(x))
    if isinstance(x, list):
        return [cut(v) for v in x]
    if isinstance(x, dict):
        return {k: cut(v) for k, v in x.items()}
    return x


class Events:
    """events.jsonl, appended one whole line at a time."""

    def __init__(self, path):
        self.file = open(path, "a", encoding="utf-8")
        self.lock = threading.Lock()  # the checks write from a thread of their own

    def emit(self, frame):
        if frame.get("subtype") in DROP:
            return
        with self.lock:
            self._write(frame)

    def _write(self, frame):
        self.file.write(json.dumps({"ts": time.time(), **cut(frame)}, ensure_ascii=False) + "\n")
        self.file.flush()

    def note(self, subtype, **fields):
        self.emit({"type": "runner", "subtype": subtype, **fields})


class Record:
    """run.json: written when the run starts and when it ends."""

    def __init__(self, run_dir, cfg):
        self.path = run_dir / "run.json"
        self.data = {
            "version": 1,
            "id": run_dir.name,
            "kind": cfg["kind"],
            "resume": cfg["resume"],
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "session_id": cfg["session_id"],
            "model": cfg.get("model") or "",
            "window": cfg.get("window") or "",
            "started": iso(time.time()),
            "ended": None,
            "cost_start_usd": cfg.get("cost_start_usd") or 0.0,
            "cost_end_usd": None,
            "exit": None,
            "exit_detail": "",
            "inbox_read": 0,
        }
        self.save()

    def save(self):
        write_text(self.path, json.dumps(self.data, ensure_ascii=False, indent=1) + "\n")

    def end(self, exit, cost_end, detail="", inbox_read=0):
        self.data.update(
            ended=iso(time.time()),
            exit=exit,
            exit_detail=detail,
            inbox_read=inbox_read,
            cost_end_usd=self.data["cost_start_usd"] if cost_end is None else cost_end,
        )
        self.save()


class Inbox:
    """New complete lines of inbox.jsonl since the last read."""

    def __init__(self, path):
        self.path, self.pos = path, 0
        path.touch()

    def read(self):
        with open(self.path, "rb") as f:
            f.seek(self.pos)
            data = f.read()
        end = data.rfind(b"\n") + 1  # a line the server is still writing waits for the next read
        self.pos += end
        cmds = []
        for line in data[:end].decode("utf-8", "replace").splitlines():
            try:
                cmds.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return cmds


class Session:
    """What the runner follows in the frames: session state, background commands, cost, rounds the CLI owes."""

    def __init__(self):
        self.state = None  # running | idle | requires_action (session_state_changed)
        self.bg = []  # latest background_tasks_changed
        self.owed_until = None  # the CLI owes a round that should start before this time
        self.cost = None  # latest total_cost_usd: the session's total, across resumes
        self.budget = False  # the last round ended on the budget

    def feed(self, frame):
        t, sub = frame.get("type"), frame.get("subtype")
        if t == "system" and sub == "session_state_changed":
            self.state = frame.get("state")
            if self.state == "running":
                self.owed_until = None
        elif t == "system" and sub == "background_tasks_changed":
            was, self.bg = self.bg, frame.get("tasks") or []
            if was and not self.bg and self.state == "idle":
                self.owe()  # a background command ended: Claude Code wakes the agent up
        elif t == "result":
            if isinstance(frame.get("total_cost_usd"), (int, float)):
                self.cost = frame["total_cost_usd"]
            self.budget = sub == "error_max_budget_usd"

    def owe(self):
        self.owed_until = time.time() + OWED_SECONDS

    def sent(self):
        if (
            self.state != "running"
        ):  # a message sent mid-round is taken in the same round, without an idle between
            self.owe()

    def round_over(self):
        return (
            self.state == "idle"
            and not self.bg
            and (self.owed_until is None or time.time() > self.owed_until)
        )


# ---------------------------------------------------------------- where the frames come from


class Agent:
    """The CLI through the SDK. Frames are read raw (client._query.receive_messages(), a private interface): the
    typed one drops frames it does not know, such as tool_progress."""

    def __init__(self, cfg, run_dir):
        from claude_agent_sdk import ClaudeSDKClient

        from .launch import sdk_options

        log = open(run_dir / "stderr.log", "a", encoding="utf-8")
        self.stderr = []

        def on_stderr(line):
            log.write(line.rstrip() + "\n")
            log.flush()
            self.stderr = (self.stderr + [line.rstrip()])[-5:]

        self.client = ClaudeSDKClient(sdk_options(cfg, stderr=on_stderr))

    async def start(self):
        await self.client.connect()
        return {"claude_pid": self.client._transport._process.pid}

    def frames(self):
        return self.client._query.receive_messages()

    async def send(self, text):
        await self.client.query(text)

    async def interrupt(self):
        await self.client.interrupt()

    async def close(self):
        await self.client.disconnect()


class Replay:
    """A recorded events.jsonl played back as if the CLI sent it, at the recorded pace divided by `speed`, each gap
    at most `max_gap` seconds. The recording's own runner events are left out (this runner writes its own), and so
    is the message it opened with (this run's message takes its place); messages recorded later stay. Messages you
    send get a short reply marked as a replay, where the CLI would read them: after the command in
    flight, at the end of the round, or at once while the session waits in the background. stop() ends the round
    the way interrupt() does (M0 p2_interrupt). When the recording is over the source stays open, as the CLI
    would: the runner then exits by its usual rule, or waits (a recording cut mid-round, like p4_k9). `writes`
    ({absolute path: text}) are written just before the recording's last frame, as the agent would have written
    progress.md, report.md or questions.md by the end of its round; a stopped replay writes nothing."""

    def __init__(self, path, speed=1.0, max_gap=None, session_id=None, writes=None):
        if not Path(path).is_file():
            raise FileNotFoundError(_("回放文件不存在：{path}", path=path))
        self.frames_left, opened = [], False
        for line in read_text(Path(path)).splitlines():
            e = json.loads(line)
            if e.get("subtype") in DROP:
                continue
            if e.get("type") == "runner":
                if e.get("subtype") not in ("send_user", "inbox") or not opened:
                    continue
            else:
                opened = True
            if session_id and "session_id" in e:
                e["session_id"] = session_id
            self.frames_left.append(e)
        self.speed, self.max_gap, self.writes = speed or 1.0, max_gap, writes or {}
        self.queue = asyncio.Queue()  # frames to give out now (replies, the interrupt)
        self.pending = []  # your messages: [(text, when)]
        self.fg = set()  # foreground tool_use ids in flight
        self.state, self.bg, self.started, self.stopped = None, [], False, False

    async def start(self):
        return {"replay": True}

    async def frames(self):
        last = None
        while True:
            if not self.queue.empty():
                yield self.follow(self.queue.get_nowait())
                continue
            if self.stopped or not self.frames_left:
                yield self.follow(await self.queue.get())
                continue
            e = self.frames_left[0]
            if last is not None:
                gap = max(0.0, e["ts"] - last) / self.speed
                gap = min(gap, self.max_gap) if self.max_gap else gap
                try:  # a reply or stop() may come during the gap
                    yield self.follow(await asyncio.wait_for(self.queue.get(), gap))
                    continue
                except asyncio.TimeoutError:
                    pass
            if self.stopped:
                continue
            self.frames_left.pop(0)
            last = e["ts"]
            if not self.frames_left:
                for path, text in self.writes.items():
                    write_text(Path(path), text)
            if e.get("subtype") == "session_state_changed" and e.get("state") == "idle":
                for frame in self.reply(
                    "next_turn"
                ):  # a message sent mid-round is answered before the round ends
                    yield self.follow(frame)
            yield self.follow({k: v for k, v in e.items() if k != "ts"})
            if e.get("type") == "user" and not self.fg:
                for frame in self.reply("after_tool"):
                    yield self.follow(frame)

    def follow(self, e):
        self.started = True
        if e.get("type") == "assistant":
            for c in e.get("message", {}).get("content") or []:
                if c.get("type") == "tool_use" and not (c.get("input") or {}).get(
                    "run_in_background"
                ):
                    self.fg.add(c["id"])
        elif e.get("type") == "user":
            for c in e.get("message", {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    self.fg.discard(c.get("tool_use_id"))
        elif e.get("subtype") == "session_state_changed":
            self.state = e.get("state")
        elif e.get("subtype") == "background_tasks_changed":
            self.bg = e.get("tasks") or []
        return e

    def reply(self, when):
        take = ("next_turn", "after_tool") if when == "next_turn" else (when,)
        ready = [t for t, w in self.pending if w in take]
        self.pending = [(t, w) for t, w in self.pending if w not in take]
        if not ready:
            return []
        text = "\n\n".join(
            _("（回放模式，没有调用模型）收到你的消息：「{text}」。", text=t) for t in ready
        )
        frames = [
            {
                "type": "assistant",
                "replay": True,
                "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
            }
        ]
        if when != "after_tool":
            frames.append(
                {"type": "result", "subtype": "success", "replay": True, "is_error": False}
            )
        if when == "now":
            frames = [
                {"type": "system", "subtype": "session_state_changed", "state": "running"},
                *frames,
                {"type": "system", "subtype": "session_state_changed", "state": "idle"},
            ]
        return frames

    async def send(self, text):
        if not self.started:
            return  # the recording answers this run's first message
        when = "after_tool" if self.fg else "next_turn" if self.state == "running" else "now"
        self.pending.append((text, when))
        if when == "now":
            for frame in self.reply("now"):
                self.queue.put_nowait(frame)

    async def interrupt(self):
        self.stopped, self.pending = True, []
        for tid in sorted(self.fg):
            self.queue.put_nowait(
                {
                    "type": "system",
                    "subtype": "task_notification",
                    "tool_use_id": tid,
                    "status": "stopped",
                    "replay": True,
                }
            )
            self.queue.put_nowait(
                {
                    "type": "user",
                    "replay": True,
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tid,
                                "is_error": True,
                                "content": "The user doesn't want to proceed with this tool use. The tool use was rejected.",
                            }
                        ],
                    },
                }
            )
        if self.state == "running":
            self.queue.put_nowait(
                {
                    "type": "user",
                    "replay": True,
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "[Request interrupted by user for tool use]"}
                        ],
                    },
                }
            )
            self.queue.put_nowait(
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "terminal_reason": "aborted_tools",
                    "replay": True,
                }
            )
            self.queue.put_nowait(
                {"type": "system", "subtype": "session_state_changed", "state": "idle"}
            )

    async def close(self):
        pass


# ---------------------------------------------------------------- one run


async def drive(make_source, cfg, run_dir):
    events = Events(run_dir / "events.jsonl")
    record = Record(run_dir, cfg)
    inbox = Inbox(run_dir / "inbox.jsonl")
    s = Session()
    reason, detail, source = None, "", None
    try:
        source = make_source()
        info = await source.start()
        events.note("connected", **info)

        async def read():
            async for frame in source.frames():
                events.emit(frame)
                s.feed(frame)

        reader = asyncio.create_task(read())

        async def send(text):
            events.note("send_user", text=text)
            s.sent()
            await source.send(text)

        if cfg.get("message"):
            await send(cfg["message"])
        stopping, stop_by = None, 0
        while reason is None:
            await asyncio.sleep(POLL)
            for cmd in inbox.read():
                events.note("inbox", cmd=cmd)
                if (
                    cmd.get("type") == "user"
                    and str(cmd.get("text") or "").strip()
                    and not stopping
                ):
                    await send(cmd["text"])
                elif cmd.get("type") == "stop" and not stopping:
                    stopping, stop_by = cmd.get("reason") or "stopped", time.time() + STOP_SECONDS
                    await source.interrupt()
            if reader.done():
                reason = "error"
                err = reader.exception()
                detail = _("claude 已退出：{error}", error=err) if err else _("claude 进程已退出")
                stderr = getattr(source, "stderr", [])
                detail += _("（stderr：{stderr}）", stderr=" / ".join(stderr)) if stderr else ""
            elif stopping:
                if s.state == "idle" or time.time() > stop_by:
                    reason = stopping
            elif s.budget and s.state == "idle":
                reason = "budget"  # no more rounds: a background command still running is ended with the session
            elif s.round_over():
                if cfg.get("checks") and progress_done(cfg):
                    attempt = await asyncio.to_thread(run_checks, cfg, run_dir, events)
                    left = cfg.get("check_retries", checks.DEFAULT_RETRIES) + 1 - attempt["n"]
                    if not attempt["ok"] and left > 0:
                        await send(
                            checks.feedback(attempt, left, cfg.get("agent_language") or "zh")
                        )
                        continue  # the agent fixes it; the checks run again when it says done again
                reason = "idle"
        reader.cancel()
    except Exception as e:  # noqa: BLE001 - whatever went wrong goes into the run's end record
        reason, detail = "error", f"{type(e).__name__}: {e}"
        traceback.print_exc()
    finally:
        try:
            if source:
                await asyncio.wait_for(source.close(), 15)
        except Exception:  # noqa: BLE001 - the session's processes go away with this one anyway
            traceback.print_exc()
        reason = reason or "error"
        events.note(
            "exit",
            reason=reason,
            detail=detail,
            bg={b.get("task_id"): b.get("description") for b in s.bg},
        )
        record.end(reason, s.cost, detail, inbox.pos)
    return reason


def progress_done(cfg):
    """The task's progress.md says 已完成 (the round ended done: time for the checks)."""
    path = Path(cfg["cwd"]) / "workspaces" / cfg["task"] / "progress.md"
    return path.is_file() and Progress.parse(read_text(path), 0).done


def run_checks(cfg, run_dir, events):
    """One attempt at the task's checks, its steps written as runner events and the outcome into checks.json."""
    total = len(cfg["checks"])
    events.note("checks", phase="start", total=total)
    attempt = checks.run_all(
        cfg["checks"],
        cfg["cwd"],
        cfg["env"],
        on_start=lambda n, c: events.note(
            "check", phase="start", n=n, total=total, name=c["name"], run=c["run"]
        ),
        on_end=lambda n, r: events.note("check", phase="end", n=n, total=total, **r),
    )
    attempt["n"] = checks.record(run_dir, attempt)
    passed = sum(r["ok"] for r in attempt["results"])
    events.note(
        "checks", phase="end", ok=attempt["ok"], passed=passed, total=total, attempt=attempt["n"]
    )
    return attempt


def terminal(cfg, run_dir):
    """在终端中接管: interactive claude on the session, here in the tmux window. When it exits, the end record gets
    the session's total cost from its record in ~/.claude/projects (the CLI writes cost-state there)."""
    record = Record(run_dir, cfg)
    if cfg.get("replay"):  # a replay task has no claude session: a shell stands in for it
        argv = ["bash", "--norc", "-i"]
        print(
            _(
                "回放任务：这里是一个 shell，代替交互式 claude（不调用模型）。\n"
                "可以编辑 workspaces/{task}/progress.md 模拟 agent 的进展，然后 exit 结束接管。\n",
                task=cfg["task"],
            ),
            flush=True,
        )
    else:
        argv = terminal_argv(cfg)
    code = subprocess.call(argv, cwd=cfg["cwd"], env={**os.environ, **cfg["env"]})
    if cfg.get("checks") and progress_done(
        cfg
    ):  # once, shown here; no retries without the runner's session
        print(_("运行验收检查……"), flush=True)
        attempt = run_checks(cfg, run_dir, Events(run_dir / "events.jsonl"))
        for r in attempt["results"]:
            print(f"{'✓' if r['ok'] else '✗'} {r['name']}\n{r['output']}", flush=True)
    record.end("terminal_exit", session_cost(cfg["session_id"]), _("退出码 {code}", code=code))


def session_cost(session_id):
    """The session's total_cost_usd from the last cost-state line of its record, None if there is none."""
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    cost = None
    for path in (config / "projects").glob(f"*/{session_id}.jsonl"):
        for line in read_text(path).splitlines():
            if '"cost-state"' in line:
                try:
                    cost = json.loads(line).get("totalCostUSD", cost)
                except json.JSONDecodeError:
                    pass
    return cost


def main(argv=None):
    from .cli import clean_own_env

    ap = argparse.ArgumentParser(
        prog="python -m agent_sys_v2.runner", description=__doc__.split("\n\n")[0]
    )
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--replay", type=Path, help="replay a recorded events.jsonl, no model")
    ap.add_argument(
        "--speed", type=float, help="replay speed factor (default 1, the recorded pace)"
    )
    ap.add_argument("--max-gap", type=float, help="longest gap between replayed events, in seconds")
    ap.add_argument(
        "--terminal", action="store_true", help="run interactive claude in this terminal (takeover)"
    )
    args = ap.parse_args(argv)
    # The parent Claude Code session's variables would override the gateway settings in ~/.claude; the server
    # cleans its own environment, but tmux may hand a new window an older one.
    clean_own_env()
    if (
        not args.terminal
    ):  # an agent's bare `tmux kill-server` would otherwise reach agent_sys's tmux, every run in it
        for name in ("TMUX", "TMUX_PANE"):
            os.environ.pop(name, None)
    run_dir = args.run_dir.resolve()
    if (run_dir / "launch.json").is_file():
        cfg = json.loads(read_text(run_dir / "launch.json"))
    else:  # a replay by hand: python -m agent_sys_v2.runner /tmp/r1 --replay events.jsonl
        run_dir.mkdir(parents=True, exist_ok=True)
        cfg = {
            "kind": "replay",
            "session_id": str(uuid.uuid4()),
            "resume": False,
            "message": "",
            "replay": {},
        }
    i18n.set_language(cfg.get("language"))  # what the server spoke when it wrote launch.json
    if args.terminal:
        return terminal(cfg, run_dir)
    if args.replay or cfg["kind"] == "replay":
        r = cfg.get("replay") or {}

        def source():
            return Replay(
                args.replay or r["events"],
                args.speed or r.get("speed") or 1,
                args.max_gap or r.get("max_gap"),
                cfg["session_id"],
                r.get("writes"),
            )
    else:
        sys.stderr = open(
            run_dir / "stderr.log", "a", encoding="utf-8"
        )  # tracebacks outlive the window

        def source():
            return Agent(cfg, run_dir)

    asyncio.run(drive(source, cfg, run_dir))


if __name__ == "__main__":
    main()
