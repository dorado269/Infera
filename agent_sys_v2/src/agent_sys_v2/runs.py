"""Runs as the server handles them: start a runner or a terminal takeover in a tmux window,
pass it your messages and 停止 through its inbox, follow its events, and when a run is over decide the task's next
status from the run's end record and progress.md. The server never holds a runner: everything here
works from the files in the run directory, so a restarted server picks up where the last one was. Only runs on
this host are touched; a run on another host is only shown.

The functions that change a task take (home, project dir, task id), read the project fresh and are called under the
server's write lock; Monitor is the server's 0.5 s loop over the running tasks.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import checks, files, launch
from .cli import clean_env
from .i18n import _, status_word
from .model import AgentSysError, Progress, iso, load_config, mtime, now_iso, read_text, write_state
from .ops import flow, update_frontmatter
from .scan import load_project, report_files

log = logging.getLogger("agent_sys")
TMUX = os.environ.get(
    "AGENT_SYS_TMUX", "agent_sys"
)  # tmux -L <TMUX>: the runners' windows; tests use their own
SESSION = "agent_sys"
HOST = socket.gethostname()
START_SECONDS = (
    30  # a run with no run.json this long after it was started: its runner never came up
)
STOP_WAIT = 12  # how long a stop or a takeover waits for the runner to end its round and exit
TERM_WAIT = 10  # claude processes of the session get TERM, then KILL after this long


# ---------------------------------------------------------------- processes


def cmdline(pid):
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace").split("\0")[:-1]
    except OSError:
        return None


def is_runner(pid, run_dir):
    """pid is still the runner of run_dir (a pid may be reused after a reboot: check before trusting or killing)."""
    args = cmdline(pid) if pid else None
    return bool(args) and "agent_sys_v2.runner" in args and str(run_dir) in args


def session_pids(session_id):
    """claude processes of a session: their command line has --session-id / --resume with the sid."""
    marks = {f"--session-id={session_id}", f"--resume={session_id}"}
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        args = cmdline(entry.name) or []
        pairs = {f"{a}={b}" for a, b in zip(args, args[1:])}
        if marks & (set(args) | pairs):
            pids.append(int(entry.name))
    return pids


def children(pid):
    out = []
    for entry in Path("/proc").iterdir():
        try:
            if entry.name.isdigit() and (entry / "stat").read_text().rsplit(")", 1)[1].split()[
                1
            ] == str(pid):
                out.append(int(entry.name))
        except (OSError, IndexError):
            pass
    return out


def signal_all(pids, sig):
    for pid in pids:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def wait_gone(pids, seconds):
    end = time.time() + seconds
    while time.time() < end:
        pids = [p for p in pids if Path(f"/proc/{p}").exists() and not zombie(p)]
        if not pids:
            return []
        time.sleep(0.2)
    return pids


def zombie(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
    except OSError:
        return True


def end_session(session_id, also=()):
    """TERM the session's claude processes (and `also`), KILL what is left after TERM_WAIT."""
    pids = sorted(set(session_pids(session_id)) | set(also))
    signal_all(pids, signal.SIGTERM)
    signal_all(wait_gone(pids, TERM_WAIT), signal.SIGKILL)


def clean(t):
    """Before a resume or a takeover: KILL the last run's runner if it is still that process, then end
    whatever claude still runs the session. Claude Code has no session lock: two processes on one session fork its
    record."""
    for run in sorted(runs_dir(t).glob("*/run.json")):
        rec = read_json(run)
        if (
            rec.get("host") == HOST
            and not rec.get("ended")
            and is_runner(rec.get("pid"), run.parent)
        ):
            os.kill(rec["pid"], signal.SIGKILL)
    end_session(t.state["session_id"])


# ---------------------------------------------------------------- tmux


def tmux(*args):
    return subprocess.run(
        ["tmux", "-L", TMUX, *args], env=clean_env(), capture_output=True, text=True
    )


def window_names():
    r = tmux("list-windows", "-t", SESSION, "-F", "#W")
    return set(r.stdout.split()) if r.returncode == 0 else set()


def window_name(project, task):
    """The task's name, made unique among the session's windows (attach -t agent_sys:<name> must find one). Only
    letters, digits, _ and -: tmux reads a "." in a target as the start of a pane index."""
    taken = window_names()
    base = re.sub(r"[^A-Za-z0-9_-]", "_", task.name)
    name = base if base not in taken else re.sub(r"[^A-Za-z0-9_-]", "_", f"{base}-{project.id}")
    stem, n = name, 2
    while name in taken:
        name, n = f"{stem}-{n}", n + 1
    return name


def open_window(project, name, argv):
    """A tmux window `name` running argv in the project directory."""
    cmd = shlex.join(argv)
    if tmux("has-session", "-t", SESSION).returncode:
        r = tmux("new-session", "-d", "-s", SESSION, "-n", name, "-c", str(project.path), cmd)
    else:
        r = tmux("new-window", "-d", "-t", f"{SESSION}:", "-n", name, "-c", str(project.path), cmd)
    if r.returncode:
        raise AgentSysError(_("无法在 tmux 中启动运行：{error}", error=r.stderr.strip()), 500)


def attach(window):
    return f"tmux -L {TMUX} attach -t {SESSION}:{window}"


# ---------------------------------------------------------------- run files


def read_json(path):
    try:
        data = json.loads(read_text(path))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def runs_dir(t):
    return t.workspace / ".agent_sys" / "runs"


def new_run_dir(t):
    """Runs are named after their start, local time (today's cost groups them by day)."""
    base = runs_dir(t) / time.strftime("%Y%m%d-%H%M%S")
    path, n = base, 2
    while path.exists():
        path, n = base.with_name(f"{base.name}-{n}"), n + 1
    return path


def record(run_dir):
    """run.json; before the runner wrote it, what launch.json says (a run that is starting)."""
    rec = read_json(run_dir / "run.json")
    if rec:
        return rec
    cfg = read_json(run_dir / "launch.json")
    if not cfg:
        return {}
    return {
        "id": run_dir.name,
        "kind": cfg.get("kind"),
        "resume": cfg.get("resume"),
        "pid": None,
        "host": HOST,
        "session_id": cfg.get("session_id"),
        "model": cfg.get("model"),
        "window": cfg.get("window"),
        "started": cfg.get("started"),
        "ended": None,
        "cost_start_usd": cfg.get("cost_start_usd") or 0.0,
        "cost_end_usd": None,
        "exit": None,
        "starting": True,
    }


def is_live(run_dir, rec=None):
    """The run has not ended: it has no end record and its runner is still there (on another host: no end record)."""
    rec = rec if rec is not None else record(run_dir)
    if not rec or rec.get("ended"):
        return False
    if rec.get("host") != HOST:
        return True
    if rec.get("starting"):
        return time.time() - (mtime(run_dir / "launch.json") or 0) < START_SECONDS
    return is_runner(rec.get("pid"), run_dir)


# ---------------------------------------------------------------- events as the run view gets them

PREVIEW = 2000  # characters of a tool's input or result that the run view gets at first; the rest when you open it
TAIL = 400  # events a run view gets at first, and per "load earlier"
_UNREAD = {
    "uuid",
    "session_id",
    "timestamp",
    "usage",
    "tool_use_result",
    "tool_result_meta",
    "request_id",
    "prompt",
    "run_id",
}  # fields of the other events that the run view does not read


def result_text(content):
    """A tool result as the run view shows it: its text, an image as [image]."""
    if isinstance(content, str) or content is None:
        return content or ""
    if isinstance(content, list):
        return "\n".join(
            c.get("text", "") if c.get("type") != "image" else "[image]"
            for c in content
            if isinstance(c, dict)
        )
    return json.dumps(content, ensure_ascii=False)


def _clip(value, limit, cut):
    """value with every string longer than limit cut to it; cut[0] says whether one was."""
    if isinstance(value, str) and limit is not None and len(value) > limit:
        cut[0] = True
        return value[:limit]
    if isinstance(value, dict):
        return {k: _clip(v, limit, cut) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip(v, limit, cut) for v in value]
    return value


def _block(c, limit):
    """One content block of a message, slim; the blocks keep their places, so a block is found again by index."""
    if not isinstance(c, dict):
        return c
    kind, cut = c.get("type"), [False]
    if kind == "thinking":  # folded in the view: only its length until you open it
        text = c.get("thinking") or ""
        out = {
            "type": kind,
            "thinking": _clip(text, 0 if limit is not None else None, cut),
            "chars": len(text),
        }
    elif kind == "tool_use":
        out = {
            "type": kind,
            "id": c.get("id"),
            "name": c.get("name"),
            "input": _clip(c.get("input") or {}, limit, cut),
        }
    elif kind == "tool_result":
        text = result_text(c.get("content"))
        out = {
            "type": kind,
            "tool_use_id": c.get("tool_use_id"),
            "is_error": bool(c.get("is_error")),
            "content": _clip(text, limit, cut),
            "chars": len(text),
        }
    elif kind in ("image", "redacted_thinking"):
        out = {"type": kind}
    else:
        out = c
    return {**out, "clipped": True} if cut[0] else out


def slim(e, limit=PREVIEW):
    """What the run view (web/run.js, Folder) reads of an event: no ids, usage or copies it does not show, long texts
    cut to `limit` characters (None: whole) and marked clipped; None for an event it ignores."""
    kind, sub = e.get("type"), e.get("subtype")
    pick = lambda *keys: {
        k: e[k] for k in ("seq", "type", "subtype", "ts", "replay", *keys) if k in e
    }  # noqa: E731
    if kind == "system" and sub == "task_progress":
        return None
    if kind == "system" and sub == "init":
        return pick("model", "claude_code_version", "permissionMode")
    if kind == "result":
        return pick("total_cost_usd", "is_error", "terminal_reason", "origin")
    if kind in ("assistant", "user"):
        out = {
            k: e[k]
            for k in ("seq", "type", "ts", "replay", "isSynthetic", "parent_tool_use_id")
            if e.get(k) is not None
        }
        content = (e.get("message") or {}).get("content")
        out["message"] = {
            "content": [_block(c, limit) for c in content] if isinstance(content, list) else content
        }
        return out
    return {k: v for k, v in e.items() if k not in _UNREAD}


class Lines:
    """Where each whole line of an events.jsonl starts (a line's seq is its number from 0), kept for the files read
    lately and extended as a file grows: a page of events, or one, is read without going through the whole file."""

    KEEP = 32

    def __init__(self):
        self.lock = threading.Lock()
        self.files = {}  # path -> [start of line 0, of line 1, ..., end of the last whole line]

    def starts(self, path):
        with self.lock:
            starts = self.files.pop(path, None) or [0]
            try:
                size = path.stat().st_size
                if size < starts[-1]:  # the file was written again
                    starts = [0]
                with open(path, "rb") as f:
                    f.seek(starts[-1])
                    data = f.read(size - starts[-1])
            except OSError:
                return [0]
            base, i = starts[-1], data.find(b"\n")
            while i >= 0:  # a line without its \n is still being written
                starts.append(base + i + 1)
                i = data.find(b"\n", i + 1)
            self.files[path] = starts
            while len(self.files) > self.KEEP:
                self.files.pop(next(iter(self.files)))
            return starts

    @staticmethod
    def read(path, starts, lo, hi):
        """Lines lo..hi-1 as (seq, bytes)."""
        if hi <= lo:
            return []
        with open(path, "rb") as f:
            f.seek(starts[lo])
            data = f.read(starts[hi] - starts[lo])
        return list(zip(range(lo, hi), data.splitlines()))


LINES = Lines()


def _parse(seq, line, limit=PREVIEW):
    try:
        e = json.loads(line)
    except json.JSONDecodeError:
        return None
    return slim({"seq": seq, **e}, limit) if isinstance(e, dict) else None


def read_events(run_dir, after=-1, before=None, tail=None):
    """The run view's events (slim): those after seq `after`; or with `tail`, the last `tail` before seq `before`
    (None: the end), with `carry`: the session's state and background commands as they were before the first
    (a view that starts there needs them). `upto` = the last seq in the file, `first` = where the page starts."""
    path = run_dir / "events.jsonl"
    try:
        starts = LINES.starts(path)
        n = len(starts) - 1
        if tail is None:
            lo = max(after + 1, 0)
            events = [e for s, line in Lines.read(path, starts, lo, n) if (e := _parse(s, line))]
            return {"events": events, "upto": n - 1, "first": lo, "carry": []}
        tail, hi = max(tail, 1), n if before is None else max(0, min(before, n))
        events, lo = [], hi
        while (
            lo > 0 and len(events) < tail
        ):  # back a block at a time: the events it ignores do not count
            block = Lines.read(path, starts, max(0, lo - tail), lo)
            lo = block[0][0]
            events[:0] = [e for s, line in block if (e := _parse(s, line))]
        if len(events) > tail:
            events = events[-tail:]
            lo = events[0]["seq"]
        return {"events": events, "upto": n - 1, "first": lo, "carry": _carry(path, starts, lo)}
    except OSError:
        return {"events": [], "upto": -1, "first": 0, "carry": []}


def _carry(path, starts, lo):
    """The last session_state_changed and background_tasks_changed before seq lo."""
    want, found = [b'"session_state_changed"', b'"background_tasks_changed"'], []
    while lo > 0 and want:
        block = Lines.read(path, starts, max(0, lo - 500), lo)
        lo = block[0][0]
        for s, line in reversed(block):
            for w in [w for w in want if w in line]:
                if (e := _parse(s, line)) and e.get("subtype") == w.strip(b'"').decode():
                    want.remove(w)
                    found.append(e)
    return sorted(found, key=lambda e: e["seq"])


def read_event(run_dir, seq):
    """One event whole (slim, nothing cut): what a tool line or a thinking block shows when you open it."""
    path = run_dir / "events.jsonl"
    starts = LINES.starts(path)
    if not 0 <= seq < len(starts) - 1:
        return None
    return next((_parse(s, line, None) for s, line in Lines.read(path, starts, seq, seq + 1)), None)


class Follow:
    """A run as its events tell it, read incrementally: session state, background commands, the foreground command
    in flight, the agent's last words, the session's cost."""

    def __init__(self, run_dir):
        self.path = run_dir / "events.jsonl"
        self.pos, self.seq = 0, -1  # seq: of the last line read
        self.state, self.bg, self.fg = None, [], {}
        self.idle_since = None
        self.last_text, self.cost = "", None
        self.checking = None  # the acceptance check being run, as a foreground command

    def poll(self):
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read()
        except OSError:
            return self
        end = data.rfind(b"\n") + 1
        self.pos += end
        for line in data[:end].splitlines():
            self.seq += 1
            try:
                self.feed(json.loads(line))
            except json.JSONDecodeError:
                pass
        return self

    def feed(self, e):
        t, sub = e.get("type"), e.get("subtype")
        if t == "system" and sub == "session_state_changed":
            self.state = e.get("state")
            self.idle_since = e.get("ts") if self.state == "idle" else None
        elif t == "system" and sub == "background_tasks_changed":
            self.bg = e.get("tasks") or []
        elif t == "assistant":
            for c in (e.get("message") or {}).get("content") or []:
                if c.get("type") == "text" and c.get("text", "").strip():
                    self.last_text = c["text"].strip()
                elif c.get("type") == "tool_use" and not (c.get("input") or {}).get(
                    "run_in_background"
                ):
                    i = c.get("input") or {}
                    text = (
                        i.get("command")
                        or i.get("file_path")
                        or i.get("pattern")
                        or i.get("description")
                        or ""
                    )
                    self.fg[c.get("id")] = {
                        "tool": c.get("name"),
                        "text": str(text)[:200],
                        "since": iso(e.get("ts")),
                    }
        elif t == "system" and sub == "task_started" and e.get("is_backgrounded"):
            self.fg.pop(e.get("tool_use_id"), None)
        elif t == "user":
            content = (e.get("message") or {}).get("content")
            for c in content if isinstance(content, list) else []:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    self.fg.pop(c.get("tool_use_id"), None)
        elif t == "result" and isinstance(e.get("total_cost_usd"), (int, float)):
            self.cost = e["total_cost_usd"]
        elif t == "runner" and sub == "check":
            self.checking = (
                {"tool": "Check", "text": str(e.get("name") or "")[:200], "since": iso(e.get("ts"))}
                if e.get("phase") == "start"
                else None
            )

    @property
    def activity(self):
        if self.checking:
            return "working"
        if self.state == "idle":
            return "waiting" if self.bg else "idle"
        return "working"

    @property
    def current(self):
        if self.checking:
            return self.checking
        return next(iter(self.fg.values()), None) if self.state != "idle" else None

    def delivery(self):
        """When a message sent now is read."""
        return (
            "after_tool" if self.current else "next_turn" if self.activity == "working" else "now"
        )


_last_text = {}


def last_message(t):
    """The agent's last words in the task's latest run that has events (a terminal takeover has none), cached by the
    events file's size and mtime."""
    run_id = (t.state or {}).get("run_id")
    if not run_id:
        return ""
    path = runs_dir(t) / run_id / "events.jsonl"
    if not path.is_file():
        path = next(
            (
                d / "events.jsonl"
                for d in sorted(runs_dir(t).iterdir(), reverse=True)
                if (d / "events.jsonl").is_file()
            ),
            path,
        )
    try:
        st = path.stat()
    except OSError:
        return ""
    key = (st.st_size, st.st_mtime_ns)
    if _last_text.get(path, (None,))[0] != key:
        follow = Follow(path.parent)
        follow.pos = max(0, st.st_size - 262144)
        if follow.pos:  # start at a line boundary
            with open(path, "rb") as f:
                f.seek(follow.pos)
                follow.pos += len(f.readline())
        _last_text[path] = (key, follow.poll().last_text)
    return _last_text[path][1]


def round_start(t):
    """When the task's current round began: the time its run's launch.json was written, to the sub-second. (By the
    second, a questions.md you answered right before a new run started would still count as this round's.) None
    before the first run."""
    run_id = (t.state or {}).get("run_id")
    return mtime(runs_dir(t) / run_id / "launch.json") if run_id else None


def round_questions(t):
    """questions.md written in this round (its mtime is after the round started): {path, count, mtime}, else None.
    Once you answered it and the next round began, it is an old one."""
    path = t.workspace / "questions.md"
    m, since = mtime(path), round_start(t)
    if not m or not since or m <= since:
        return None
    rows = [line for line in read_text(path).splitlines() if line.strip().startswith("|")]
    count = max(0, len(rows) - 2)  # the header and the |---| line
    return {"path": "questions.md", "count": count, "mtime": iso(m)}


def run_json(t, rec, follow=None):
    """Run from its record; a live sdk run adds what it is doing."""
    run_dir = runs_dir(t) / rec["id"]
    live = rec.get("id") == (t.state or {}).get("run_id") and is_live(run_dir, rec)
    end = rec.get("cost_end_usd") if rec.get("ended") else (follow or Follow(run_dir).poll()).cost
    out = {
        "id": rec["id"],
        "kind": rec.get("kind"),
        "resume": rec.get("resume"),
        "started": rec.get("started"),
        "ended": rec.get("ended"),
        "exit": rec.get("exit") or (None if live else "interrupted"),
        "exit_detail": rec.get("exit_detail") or "",
        "model": rec.get("model"),
        "live": live,
        "cost_usd": round(max(0.0, end - (rec.get("cost_start_usd") or 0)), 6)
        if end is not None
        else None,
    }
    if rec.get("host") and rec["host"] != HOST:
        out["host"] = rec["host"]
    if rec.get("kind") == "terminal" and rec.get("window"):
        out["attach"] = attach(rec["window"])
    if live and rec.get("kind") != "terminal":
        f = follow or Follow(run_dir).poll()
        out.update(
            activity=f.activity,
            current=f.current,
            background=[b.get("description", "") for b in f.bg],
        )
    return out


def list_runs(t, follow=None):
    """The task's runs, newest first; the current one may still be starting (launch.json only)."""
    recs = (
        [record(d) for d in sorted(runs_dir(t).iterdir(), reverse=True)]
        if runs_dir(t).is_dir()
        else []
    )
    return [
        run_json(t, r, follow if r["id"] == (t.state or {}).get("run_id") else None)
        for r in recs
        if r
    ]


# ---------------------------------------------------------------- starting runs


def replay_spec(t, n):
    """agent.replay (project.yaml or the task's frontmatter): a recorded events.jsonl to play back instead of calling
    the model, or a list of them (run n plays the n-th, the last one after that), or {events, speed, max_gap, writes}.
    writes: the files run n writes into the workspace when its recording is over, as an agent would (progress.md,
    report.md, questions.md: {path relative to the workspace: text}); a list picked like events, or one mapping for
    every run. With them a chain of tasks, questions and reviews can be played without a model."""
    spec = t.agent.get("replay")
    if not spec:
        return None
    spec = dict(spec) if isinstance(spec, dict) else {"events": spec}
    pick = (
        lambda items: (items[min(n, len(items) - 1)] if items else None)
        if isinstance(items, list)
        else items
    )  # noqa: E731
    path = Path(
        str(pick(spec["events"] if isinstance(spec.get("events"), list) else [spec.get("events")]))
    )
    if not path.is_absolute():
        path = t.workspace.parent.parent.parent / path  # relative to the project directory
    writes = pick(spec.get("writes")) or {}
    if not isinstance(writes, dict):
        raise AgentSysError(
            _("agent.replay.writes 应为「工作区内的路径: 内容」的键值表，或者它们的列表")
        )
    writes = {
        str(files.resolve(t.workspace, str(rel), write=True)): str(text)
        for rel, text in writes.items()
    }
    return {
        "events": str(path),
        "speed": spec.get("speed") or 1,
        "max_gap": spec.get("max_gap"),
        "writes": writes,
    }


def check_can_run(project, t):
    if not t.managed:
        raise AgentSysError(
            _("该任务未托管，agent_sys 没有它的会话：请在原终端中回复，或先将任务纳入托管"), 409
        )
    if project.archived:
        raise AgentSysError(_("项目「{project}」已归档，请先取消归档", project=project.title), 409)


def budget_left(t):
    try:
        budget = float(t.agent.get("budget_usd") or 0)
    except (TypeError, ValueError):
        budget = 0.0
    return budget - float(t.state.get("cost_usd") or 0)


def start_run(home, project, t, text="", kind="sdk"):
    """A new run of the task: write its run directory, mark the task running, open the tmux window. text is your
    message (the first run sends the task's first message, with text as your reply)."""
    state = t.state
    clean(t)
    replay = replay_spec(t, state.get("runs") or 0)
    resume = (state.get("runs") or 0) > 0 if replay else launch.session_exists(state["session_id"])
    parts, system_text = launch.system(home, project, t)
    run_dir, updated, lang = new_run_dir(t), "", launch.agent_language(home)
    if resume:
        known = launch.known_system_md(t.workspace)
        updated = (
            str(run_dir / "system.md") if known and known != system_text and text.strip() else ""
        )
        wait = t.status == "needs_you" and t.detail.get("kind") == "wait_interrupted"
        moved = (state["moved_from"], t.id) if state.get("moved_from") else None
        message = launch.resume_message(text, updated, wait, moved, lang) if text.strip() else ""
    else:
        message = launch.first_message(project, t, text, lang)
    left = budget_left(t)
    if kind != "terminal" and left <= 0:
        raise AgentSysError(
            _(
                "预算已用尽（${cost} / ${budget}），请先追加预算",
                cost=f"{state.get('cost_usd', 0):.2f}",
                budget=f"{float(t.agent.get('budget_usd') or 0):.2f}",
            ),
            409,
        )
    kind = "replay" if replay and kind == "sdk" else kind
    window = window_name(project, t)
    cfg = launch.prepare(
        home,
        project,
        t,
        run_dir=run_dir,
        kind=kind,
        parts=parts,
        system_text=system_text,
        message=message,
        resume=resume,
        max_budget_usd=max(left, 0.0),
        # a new session (its record was cleaned up) counts its cost from 0
        cost_start_usd=float(state.get("session_cost_usd") or 0) if resume else 0.0,
        window=window,
        tmux=TMUX,
        replay=replay,
        cli_path=load_config(home).get("cli_path"),
        system_known=not resume or bool(updated),
    )
    now = now_iso()
    told = {
        k: v for k, v in state.items() if k != "moved_from" or (resume and not message)
    }  # the move is told once
    write_state(
        t.workspace,
        {
            **told,
            "status": "running",
            "detail": {},
            "since": now,
            "run_id": run_dir.name,
            "runs": (state.get("runs") or 0) + 1,
            "started": state.get("started") or now,
        },
    )
    argv = [sys.executable, "-m", "agent_sys_v2.runner", str(run_dir)] + (
        ["--terminal"] if kind == "terminal" else []
    )
    try:
        open_window(project, window, argv)
    except (
        AgentSysError
    ):  # no run started: the task is as it was, and no run directory is left half made
        write_state(t.workspace, state)
        shutil.rmtree(run_dir, ignore_errors=True)
        raise
    # Until the runner wrote run.json there is no pid to stop or check; no other action may come in between (the
    # caller holds the write lock). A runner that never comes up is found by the Monitor after START_SECONDS.
    wait_for(lambda: (run_dir / "run.json").is_file(), 10)
    return cfg


def wait_for(cond, seconds):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.2)
    return cond()


def append_inbox(run_dir, cmd):
    """One line per command, ASCII only: U+2028 and the like in a pasted message would split the line for its reader
    (str.splitlines) and the message would be lost."""
    with open(run_dir / "inbox.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(cmd) + "\n")


def current_run(t):
    run_id = (t.state or {}).get("run_id")
    if not run_id:
        return None, {}
    run_dir = runs_dir(t) / run_id
    return run_dir, record(run_dir)


def check_local(rec):
    if rec.get("host") and rec["host"] != HOST:
        raise AgentSysError(
            _("该任务在 {host} 上运行，请在那台机器上的 agent_sys 中操作", host=rec["host"]), 409
        )


def stop_runner(t, reason="stopped"):
    """Ask the live runner to interrupt and exit, wait for it; KILL it if it does not go."""
    run_dir, rec = current_run(t)
    if run_dir and is_live(run_dir, rec):
        append_inbox(run_dir, {"type": "stop", "reason": reason})
        if not wait_for(lambda: not is_live(run_dir), STOP_WAIT):
            clean(t)
    return run_dir


# ---------------------------------------------------------------- actions


def fresh(pdir, tid):
    project = load_project(pdir)
    return project, project.task(tid)


def start(home, pdir, tid):
    """开始: a queued task's first run, whether or not its upstream tasks are done."""
    project, t = fresh(pdir, tid)
    check_can_run(project, t)
    if t.status != "queued":
        raise AgentSysError(
            _("任务当前为「{status}」，无法执行「开始」", status=status_word(t.status)), 409
        )
    start_run(home, project, t)


def message(home, pdir, tid, text):
    """Your message -> when it is read: after_tool / next_turn / now into the live run; resume starts a
    new round of the session."""
    if not str(text or "").strip():
        raise AgentSysError(_("消息内容不能为空"))
    project, t = fresh(pdir, tid)
    check_can_run(project, t)
    if t.status == "running":
        run_dir, rec = current_run(t)
        check_local(rec)
        if rec.get("kind") == "terminal":
            raise AgentSysError(_("该任务正在终端中接管，请在终端中回复"), 409)
        if not run_dir or not is_live(run_dir, rec):
            raise AgentSysError(_("runner 已退出，agent_sys 正在更新任务状态，请稍后再发送"), 409)
        delivery = Follow(run_dir).poll().delivery()
        append_inbox(run_dir, {"type": "user", "text": text})
        return delivery
    if t.status not in ("needs_you", "in_review", "done"):
        raise AgentSysError(
            _("任务当前为「{status}」，尚未开始，暂时不能发送消息", status=status_word(t.status)),
            409,
        )
    if t.detail.get("kind") == "budget" and budget_left(t) <= 0:
        raise AgentSysError(_("预算已用尽，请先追加预算"), 409)
    start_run(home, project, t, text)
    return "resume"


def resume_interrupted(home, pdir, tid):
    """继续, for 继续全部中断任务: only a task whose run was interrupted, its background wait too.
    False if the task is not one of those."""
    _p, t = fresh(pdir, tid)
    if t.status != "needs_you" or t.detail.get("kind") not in ("interrupted", "wait_interrupted"):
        return False
    message(home, pdir, tid, launch.say(launch.agent_language(home), "继续"))
    return True


def stop(home, pdir, tid):
    """停止 = interrupt(): the command in flight is killed, the runner exits, the task goes to 待确认 (stopped)."""
    project, t = fresh(pdir, tid)
    if not t.managed or t.status != "running":
        raise AgentSysError(
            _("任务当前为「{status}」，没有正在运行的 runner", status=status_word(t.status)), 409
        )
    run_dir, rec = current_run(t)
    check_local(rec)
    if rec.get("kind") == "terminal":
        raise AgentSysError(_("该任务正在终端中接管，请使用「结束接管」"), 409)
    stop_runner(t)
    finish(home, pdir, tid)


def takeover(home, pdir, tid):
    """在终端中接管: stop the runner, clean the session's processes, start interactive claude in tmux."""
    project, t = fresh(pdir, tid)
    check_can_run(project, t)
    run_dir, rec = current_run(t)
    if t.status == "running":
        check_local(rec)
        if rec.get("kind") == "terminal":
            raise AgentSysError(_("该任务已在终端中接管"), 409)
        stop_runner(t, "takeover")
        state = account(t.state, run_dir)  # the runner's cost; its run is not finished as a round
        write_state(t.workspace, state)
        project, t = fresh(pdir, tid)
    elif t.status not in ("needs_you", "in_review", "done"):
        raise AgentSysError(
            _("任务当前为「{status}」，无法在终端中接管", status=status_word(t.status)), 409
        )
    cfg = start_run(home, project, t, kind="terminal")
    return {"window": f"{SESSION}:{cfg['window']}", "attach": attach(cfg["window"])}


def end_takeover(home, pdir, tid):
    """结束接管: end the interactive claude (the session's record stays); its wrapper writes the end record and the
    round ends as usual: progress.md decides."""
    project, t = fresh(pdir, tid)
    run_dir, rec = current_run(t)
    if t.status != "running" or rec.get("kind") != "terminal":
        raise AgentSysError(_("该任务当前未处于终端接管状态"), 409)
    check_local(rec)
    wrapper = rec.get("pid") if is_runner(rec.get("pid"), run_dir) else None
    inner = children(wrapper) if wrapper else []
    signal_all(
        inner, signal.SIGHUP
    )  # an interactive shell (a replay task's stand-in for claude) ignores TERM
    end_session(t.state["session_id"], inner)
    if wrapper and not wait_for(lambda: not is_live(run_dir), 5):
        os.kill(wrapper, signal.SIGKILL)
    finish(home, pdir, tid)


def add_budget(home, pdir, tid, usd, text=""):
    """追加预算: the task's budget (frontmatter agent.budget_usd, a setting of the task) grows by usd; a task that
    stopped on its budget goes on."""
    try:
        usd = float(usd)
    except (TypeError, ValueError):
        usd = 0
    if usd <= 0:
        raise AgentSysError(_("追加的金额应为正数"))
    project, t = fresh(pdir, tid)
    check_can_run(project, t)
    total = round(float(t.agent.get("budget_usd") or 0) + usd, 2)

    def change(fm):
        agent = fm.get("agent")
        if isinstance(agent, dict):
            agent["budget_usd"] = total
        else:
            fm["agent"] = flow({"budget_usd": total})

    update_frontmatter(t.path, change)
    project, t = fresh(pdir, tid)
    if t.status == "needs_you":
        start_run(
            home,
            project,
            t,
            text
            or launch.say(
                launch.agent_language(home), "预算已追加 ${usd}，请继续。", usd=f"{usd:g}"
            ),
        )
    return total


def cancel(home, pdir, tid):
    """取消, also of a running task: its runner and the session's processes are ended first."""
    project, t = fresh(pdir, tid)
    if not t.managed or t.status in ("done", "cancelled"):
        raise AgentSysError(
            _(
                "任务当前为「{status}」{unmanaged}，无法取消",
                status=status_word(t.status),
                unmanaged="" if t.managed else _("（未托管）"),
            ),
            409,
        )
    state = t.state
    if t.status == "running":
        run_dir, rec = current_run(t)
        check_local(rec)
        clean(t)
        state = account(state, run_dir)
    write_state(t.workspace, {**state, "status": "cancelled", "detail": {}, "since": now_iso()})


# ---------------------------------------------------------------- the end of a run


def round_cost(t):
    """What the task's latest run cost (本轮花费, shown with a report to review); None while it is not known."""
    _run, rec = current_run(t)
    end = rec.get("cost_end_usd") if rec.get("ended") else None
    return (
        round(max(0.0, end - float(rec.get("cost_start_usd") or 0)), 6)
        if isinstance(end, (int, float))
        else None
    )


def run_end_cost(run_dir, rec):
    """The session's total cost when the run ended: the end record; for a run that ended without one, the last
    result in its events (a terminal run: the session's record)."""
    if rec.get("ended") and rec.get("cost_end_usd") is not None:
        return rec["cost_end_usd"]
    if rec.get("kind") == "terminal":
        from .runner import session_cost

        return session_cost(rec.get("session_id"))
    return Follow(run_dir).poll().cost


def account(state, run_dir):
    """state with the run's cost added: the session total is cumulative, so a run costs end - start."""
    rec = record(run_dir)
    end = run_end_cost(run_dir, rec)
    if end is None:
        return state
    cost = max(0.0, end - float(rec.get("cost_start_usd") or 0))
    return {
        **state,
        "cost_usd": round(float(state.get("cost_usd") or 0) + cost, 6),
        "session_cost_usd": end,
    }


def round_end(t, cfg, run_dir):
    """A round ended normally: progress.md says done and the run's checks (if it had any) passed -> done (review: true
    -> in_review); done with checks that failed every attempt -> needs_you (checks_failed); else needs_you (asked, or
    questions when the round wrote questions.md). A done task that only answered a follow-up question (progress
    still done, report.md unchanged) stays done and is not unread again."""
    path = t.workspace / "progress.md"
    progress = Progress.parse(read_text(path), mtime(path)) if path.is_file() else None
    before = cfg.get("before") or {}
    if progress and progress.done and cfg.get("checks"):
        result = checks.summary(run_dir) or {"failed": [], "passed": 0, "total": len(cfg["checks"])}
        if not result.get("ok"):
            detail = {k: result[k] for k in ("failed", "passed", "total")}
            return "needs_you", {"kind": "checks_failed", **detail}, False
    if progress and progress.done:
        report = next(iter(report_files(t.workspace)), None)
        now = (report, iso(mtime(t.workspace / report)) if report else "")
        if before.get("status") == "done" and now == (
            before.get("report"),
            before.get("report_mtime") or "",
        ):
            return "done", {}, False
        return ("in_review" if t.review else "done"), {}, True
    questions = round_questions(t)
    if questions:
        return (
            "needs_you",
            {"kind": "questions", "count": questions["count"], "path": questions["path"]},
            False,
        )
    return "needs_you", {"kind": "asked"}, False


def unread_messages(run_dir, rec):
    """Your messages the runner had not read when it exited (sent in the moment it decided to)."""
    try:
        with open(run_dir / "inbox.jsonl", "rb") as f:
            f.seek(rec.get("inbox_read") or 0)
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            cmd = json.loads(line)
        except json.JSONDecodeError:
            continue
        if cmd.get("type") == "user" and str(cmd.get("text") or "").strip():
            out.append(cmd["text"])
    return out


def over(t):
    """The task's current run is over and on this host: it has an end record, or its runner is gone (or never came
    up) without one."""
    run_dir, rec = current_run(t)
    return bool(run_dir) and rec.get("host", HOST) == HOST and not is_live(run_dir, rec)


def finish(home, pdir, tid):
    """If the task's current run is over, move the task on. Returns True if it did."""
    project, t = fresh(pdir, tid)
    if not t.managed or t.status != "running":
        return False
    if not over(t):
        return False
    run_dir, rec = current_run(t)
    cfg = read_json(run_dir / "launch.json")
    exit = rec.get("exit") if rec.get("ended") else None
    finished = t.state.get("finished") or ""
    if exit in ("idle", "terminal_exit") or (rec.get("kind") == "terminal" and not exit):
        status, detail, new = round_end(t, cfg, run_dir)
        finished = now_iso() if new else finished
    elif exit in ("stopped", "takeover"):  # takeover: the terminal never came up
        status, detail = "needs_you", {"kind": "stopped"}
    elif exit == "budget":
        status, detail = "needs_you", {"kind": "budget"}
    elif exit == "error":
        status, detail = "needs_you", {"kind": "interrupted", "error": rec.get("exit_detail") or ""}
    else:  # the runner died: kill -9, a closed window, a reboot (or a takeover that had to KILL it)
        bg = Follow(run_dir).poll().bg
        progress = t.progress
        status, detail = (
            "needs_you",
            (
                {
                    "kind": "wait_interrupted",
                    "waiting_for": (progress.next if progress else "")
                    or bg[0].get("description", ""),
                }
                if bg
                else {"kind": "interrupted"}
            ),
        )
    state = account(t.state, run_dir)
    write_state(
        t.workspace,
        {**state, "status": status, "detail": detail, "since": now_iso(), "finished": finished},
    )
    later = unread_messages(run_dir, rec) if exit == "idle" else []
    if later:  # sent while the runner was exiting: they start the next round
        project, t = fresh(pdir, tid)
        try:
            start_run(home, project, t, "\n\n".join(later))
        except (
            AgentSysError
        ):  # e.g. the budget is used up: the task stays in 待确认, the messages are in the inbox
            pass
    return True


# ---------------------------------------------------------------- the server's view of running tasks


class Monitor:
    """Every 0.5 s: the events of each running task's current run, read incrementally, give its
    sub-state (执行中 / 等待中 / 终端接管) and what it runs; a run that is over moves its task on (finish)."""

    def __init__(self, store):
        self.store = store
        self.follows = {}  # run dir -> Follow
        self.told = {}  # run dir -> the last seq the listeners were told of

    def follow(self, t):
        run_dir = runs_dir(t) / t.state["run_id"]
        f = self.follows.get(run_dir)
        if f is None:
            f = self.follows[run_dir] = Follow(run_dir)
        return run_dir, f.poll()

    def annotate(self, project):
        """Set the running tasks' detail (and since, while waiting) from their runs. Called after every rescan."""
        for t in project.tasks.values():
            if t.managed and t.status == "running" and t.state.get("run_id"):
                self.annotate_task(t)

    def annotate_task(self, t):
        run_dir, f = self.follow(t)
        rec = record(run_dir)
        if rec.get("kind") == "terminal":
            detail = {
                "kind": "terminal",
                "attach": attach(rec.get("window") or ""),
                "window": rec.get("window"),
            }
        elif f.activity == "waiting":
            detail = {"kind": "waiting", "waiting_for": f.bg[0].get("description", "")}
            t.since = iso(f.idle_since) or t.since
        else:
            cur = f.current
            detail = {
                "kind": "working",
                **({"current": f"{cur['tool']} {cur['text']}"[:160]} if cur else {}),
            }
        if rec.get("host") and rec["host"] != HOST:
            detail["host"] = rec["host"]
        t.detail = detail
        if f.cost is not None:  # the run's cost so far counts in the task's cost while it runs
            t.cost_running = max(0.0, f.cost - float(rec.get("cost_start_usd") or 0))
        return run_dir, f

    def poll(self):
        """One round over the running tasks: finish the runs that are over, push sub-state changes."""
        finished, changed, grown = [], [], []
        with self.store.lock:
            running = [
                (p, t)
                for p in self.store.live()
                for t in p.tasks.values()
                if t.managed and t.status == "running" and t.state.get("run_id")
            ]
        for p, t in running:
            with self.store.lock:
                try:
                    if over(t):
                        if finish(self.store.home, p.path, t.id):
                            finished.append((p.id, t.id))
                        continue
                    before = (dict(t.detail), t.cost_running)
                    run_dir, f = self.annotate_task(t)
                    if (t.detail, t.cost_running) != before:
                        changed.append((p.id, t.id))
                    if f.seq != self.told.get(run_dir, -1):
                        self.told[run_dir] = f.seq
                        grown.append(
                            {"project": p.id, "task": t.id, "run": run_dir.name, "seq": f.seq}
                        )
                except AgentSysError:
                    pass
                except (
                    Exception
                ):  # one task's files (NFS, permissions) must not hold up the other runs
                    log.exception("following the run of %s/%s failed", p.id, t.id)
        live = {runs_dir(t) / t.state["run_id"] for _, t in running}
        self.follows = {k: v for k, v in self.follows.items() if k in live}
        self.told = {k: v for k, v in self.told.items() if k in live}
        if finished:
            self.store.sync(also=finished)
        if changed:
            self.store.notify(changed)
        for run in (
            grown
        ):  # the run views of these runs fetch what is new (one SSE per page, not one per run)
            self.store.tell(run)
