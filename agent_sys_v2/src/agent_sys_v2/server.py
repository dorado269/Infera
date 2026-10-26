"""agent_sys server: the REST API and the change stream for the web UI, which it serves too.

  agent-sys-v2 serve    (starts uvicorn on /tmp/agent_sys-$UID/agent_sys.sock in tmux -L agent_sys; see cli.py)

Every project is scanned once into memory and rescanned when it changes: the Watcher polls stat every 2 s, and
each write through the API syncs right away. Reads (home page, board, task detail) come from memory. Writes go
through one lock, because an operation checks on a fresh scan and then writes: two interleaved requests would
get round the check. Paths in the API are relative to AGENT_SYS_HOME, errors are {"error": <text for the user>}.
Runs are started, stopped and taken over through runs.py; a 0.5 s loop (runs.Monitor) follows the running tasks'
events and moves a task on when its run is over. The scheduler (sched.py) runs after each 2 s scan and starts the
queued tasks that wait for nothing, their machines free. Each rescan reads the project's ledger (ledger.py) for the
board's head and the cards' result badges; the rules and knowledge pages are knowledge.py, and a knowledge library's
INDEX.md is made again whenever its entries change.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import Body, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from . import checks, files, i18n, knowledge, launch, ledger, machines, ops, questions, runs, sched
from .i18n import _
from .model import AgentSysError, Project, check_name, iso, load_config, mtime, now_iso, read_text
from .scan import Watcher, load_project, preview_edges, project_dir, project_ids

WEB = Path(__file__).resolve().parent / "web"
POLL_SECONDS = 2
COALESCE_SECONDS = 0.15  # changes this close together go out as one push
# The UI's scripts come only from here; components set some styles inline; images may be data: or blob:.
CSP = "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'"
RUN_POLL = 0.5  # running tasks' events and run records
log = logging.getLogger("agent_sys")
Json = Annotated[dict, Body()]  # a request's JSON body
OptionalJson = Annotated[dict | None, Body()]


class Store:
    """The scans of all projects, kept current. listeners get {"project", "task"} for each change
    (task None = the project itself), and {"project", "task", "run", "seq"} when a running task's run has new events
    (seq: the last); they may be called from any thread."""

    def __init__(self, home):
        self.home = Path(home)
        self.lock = threading.RLock()
        self.projects: dict[
            str, Project | AgentSysError
        ] = {}  # a project.yaml that cannot be read: its error
        self.listeners = set()
        self.watcher = Watcher(self.home)
        self.monitor = runs.Monitor(self)
        for pid in [
            "",
            *project_ids(self.home),
        ]:  # entries written while no server ran (M1d writes them by hand)
            knowledge.refresh_index(self.knowledge_dir(pid))
        for pid in project_ids(self.home):
            self.rescan(pid)
        sched.annotate(self.live(), sched.global_limit(self.home))

    def knowledge_dir(self, pid):
        return knowledge.knowledge_dir(self.home, pid or None)

    def rescan(self, pid):
        pdir = project_dir(self.home, pid)
        if not pid:  # AGENT_SYS_HOME's rules and knowledge: nothing to scan
            return
        if not (pdir / "project.yaml").is_file():
            self.projects.pop(pid, None)
            return
        try:
            p = self.projects[pid] = load_project(pdir)
            self.monitor.annotate(p)  # running tasks: what their runs are doing
            full = ledger.ledger(p)  # the board's head and the cards' result badges
            p.ledger = ledger.head(full)
            for tid, brief in ledger.task_briefs(full).items():
                p.tasks[tid].results = brief
        except AgentSysError as e:
            self.projects[pid] = e
        except (
            Exception
        ) as e:  # a file that changed under the scan, a bad moment on NFS: shown on the
            # project's row, and scanned again at the next poll
            error = AgentSysError(_("扫描项目失败：{error}", error=repr(e)), 500)
            if str(self.projects.get(pid)) != str(error):
                log.exception("scanning project %s failed", pid)
            self.projects[pid] = error
            self.watcher.forget(pid)

    def sync(self, also=()):
        """Rescan the projects that changed on disk, plus `also` (changes the Watcher does not look at, such as
        any file of a workspace written through the API), and tell the listeners."""
        with self.lock:
            changes = set(self.watcher.poll()) | set(also)
            for pid in {pid for pid, _ in changes}:
                self.rescan(pid)
            for pid in {
                pid for pid, tid in changes if tid is None
            }:  # entries edited by hand, or added: a new index
                knowledge.refresh_index(self.knowledge_dir(pid))
            if changes:  # a slot taken or freed anywhere: which queued tasks wait for one
                changes |= set(sched.annotate(self.live(), sched.global_limit(self.home)))
        self.notify(changes)

    def poll(self):
        """Every 2 s: rescan what changed on disk, then start the queued tasks that can start."""
        self.sync()
        sched.tick(self)

    def notify(self, changes):
        """Tell the listeners, without a rescan (a running task's sub-state changed in memory)."""
        for pid, tid in sorted(set(changes), key=lambda c: (c[0], c[1] or "")):
            for fn in list(self.listeners):
                fn({"project": pid, "task": tid})

    def tell(self, change):
        for fn in list(self.listeners):
            fn(change)

    def write(self, fn, *args, also=(), **kw):
        """Run a write operation under the lock, then sync, so the reply and the next read agree with the disk."""
        with self.lock:
            result = fn(*args, **kw)
            self.sync(also)
        return result

    def project(self, pid) -> Project:
        p = self.projects.get(pid)
        if p is None:
            raise AgentSysError(_("项目「{project}」不存在", project=pid), 404)
        if isinstance(p, AgentSysError):
            raise p
        return p

    def live(self):
        return [p for p in self.projects.values() if isinstance(p, Project)]


# ---------------------------------------------------------------- JSON


def task_summary(t):
    """TaskSummary; a managed task to review also says what its last run cost (本轮花费)."""
    out = {**t.to_json(), "last_message": runs.last_message(t)}
    if t.managed and t.status == "in_review":
        out["round_cost_usd"] = runs.round_cost(t)
    return out


def edge_end(p, e, other):
    o = p.tasks[other]
    return {
        "task": other,
        "title": o.title,
        "status": o.status,
        "kind": e.kind,
        "label": e.label,
        "evidence": e.evidence,
        "source": e.source,
        "blocking": e.blocking,
        "satisfied": p.edge_satisfied(e),
    }


def task_detail(store, p, t):
    started = t.started and iso(mtime(t.path)) > t.started
    run_id = t.managed and t.state.get("run_id")
    follow = store.monitor.follows.get(runs.runs_dir(t) / run_id) if run_id else None
    run_list = runs.list_runs(t, follow) if t.managed else []
    return {
        **t.detail_json(),
        "last_message": runs.last_message(t),
        "upstream": [edge_end(p, e, e.up) for e in p.upstream(t.id)],
        "downstream": [edge_end(p, e, e.down) for e in p.downstream(t.id)],
        "questions": runs.round_questions(t),
        "check_runs": checks.load(runs.runs_dir(t) / run_id) if run_id else None,
        "project_machines": machines.declared(p.config),
        "prompt_edited_after_start": bool(started),
        "runs": run_list,
    }


def project_json(store, pid):
    p = store.projects[pid]
    if isinstance(p, AgentSysError):
        return {
            "id": pid,
            "title": pid,
            "path": f"campaigns/{pid}",
            "archived": False,
            "error": str(p),
        }
    return p.to_json()


def templates_brief():
    return [{**t, "types": [ty["id"] for ty in t["types"]]} for t in ops.list_templates()]


def home_json(store):
    """Everything that needs you, across the projects that are not archived."""
    live = [p for p in store.live() if not p.archived]
    tasks = [t for p in live for t in p.tasks.values()]
    pick = lambda status: sorted((t for t in tasks if t.status == status), key=lambda t: t.since)
    archived = sorted(
        (p for p in store.live() if p.archived),
        key=lambda p: str(p.to_json()["archived_at"]),
        reverse=True,
    )
    return {
        "now": now_iso(),
        "needs_you": [task_summary(t) for t in pick("needs_you")],  # longest waiting first
        "in_review": [task_summary(t) for t in pick("in_review")],
        "unread": [
            task_summary(t)
            for t in sorted((t for t in tasks if t.unread), key=lambda t: t.finished, reverse=True)
        ],
        "running": [task_summary(t) for t in pick("running")],
        "queued": sum(t.status == "queued" for t in tasks),
        "cost_today_usd": round(
            sum(t.cost_today for p in store.live() for t in p.tasks.values()), 4
        ),
        "projects": [
            project_json(store, pid)
            for pid in sorted(store.projects)
            if pid not in {p.id for p in archived}
        ],
        "archived_projects": [p.to_json() for p in archived],
        "machines": [m for m in machines.table(live) if m["holder"]],
        "templates": templates_brief(),
    }


def board_json(store, p):
    config = load_config(store.home)
    return {
        "project": p.to_json(),
        "types": [ty.to_json() for ty in p.types],
        "tasks": [task_summary(t) for t in p.tasks.values()],
        "edges": [e.to_json() for e in p.edges],
        "machines": machines.table(store.live(), only=p),
        "running": {
            "count": p.running(),
            "limit": p.to_json()["max_running"],
            "global_count": sum(q.running() for q in store.live()),
            "global_limit": config["max_running"],
        },
    }


# ---------------------------------------------------------------- the app


def create_app(home):
    i18n.use(home)
    store = Store(home)

    @asynccontextmanager
    async def lifespan(app):
        async def poll(fn, seconds):
            while True:
                await asyncio.sleep(seconds)
                try:
                    await run_in_threadpool(fn)
                except Exception:  # a bad moment on NFS must not end the polling
                    log.exception("polling failed")

        tasks = [
            asyncio.create_task(poll(store.poll, POLL_SECONDS)),
            asyncio.create_task(poll(store.monitor.poll, RUN_POLL)),
        ]
        yield
        for task in tasks:
            task.cancel()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store = store

    @app.exception_handler(AgentSysError)
    async def refused(request, e: AgentSysError):
        return JSONResponse({"error": str(e)}, status_code=e.status)

    @app.exception_handler(HTTPException)
    async def http_error(request, e: HTTPException):
        text = (
            _("接口不存在：{method} {path}", method=request.method, path=request.url.path)
            if e.status_code == 404
            else str(e.detail)
        )
        return JSONResponse({"error": text}, status_code=e.status_code)

    @app.exception_handler(RequestValidationError)
    async def bad_request(request, e: RequestValidationError):
        where = _("；").join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
        return JSONResponse({"error": _("请求格式不正确（{where}）", where=where)}, status_code=400)

    # the run output, boards and scripts are text that packs 3-6 times smaller: what makes a page slow over a tunnel
    # is its size (the SSE streams are left as they are: text/event-stream is not compressed)
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("Cache-Control", "no-cache")
        # one line per page load and per server error, to see which browser reached this server and what failed
        if request.url.path == "/" or response.status_code >= 500:
            ua = request.headers.get("user-agent", "-")
            print(
                f"{time.strftime('%H:%M:%S')} {response.status_code} {request.method} {request.url.path} {ua}",
                flush=True,
            )
        return response

    def task_of(pid, type_, name):
        p = store.project(pid)
        return p, p.task(f"{type_}/{name}")

    # ---- home and projects (§3.1, §3.2)

    @app.get("/api/home")
    def home():
        return home_json(store)

    def each_task(body, fn):
        """Run fn(project, task) for each {"project", "task"} in body["tasks"] (the home page's batch actions): the
        ones it did, and the ones refused with why."""
        done, errors = [], []
        for item in body.get("tasks") or []:
            pid, tid = str(item.get("project") or ""), str(item.get("task") or "")
            try:
                p = store.project(pid)
                if fn(p, p.task(tid)):
                    done.append(f"{pid}/{tid}")
            except AgentSysError as e:
                errors.append({"task": f"{pid}/{tid}", "error": str(e)})
        return done, errors

    @app.post("/api/resume")
    def resume_all(body: Json):
        """继续全部中断任务: every listed task whose run was interrupted goes on (继续); others are
        left as they are."""
        done, errors = each_task(
            body,
            lambda p, t: store.write(
                runs.resume_interrupted, store.home, p.path, t.id, also=[(p.id, t.id)]
            ),
        )
        return {"resumed": done, "errors": errors}

    @app.post("/api/mark_read")
    def mark_all_read(body: Json):
        """全部标为已查看: clear the unread mark of every listed task; one write, one rescan."""

        def mark():
            return each_task(
                body, lambda p, t: t.unread and ops.mark_read(p.path, t.id) is not None
            )

        done, errors = store.write(
            mark,
            also=[
                (str(i.get("project") or ""), str(i.get("task") or ""))
                for i in body.get("tasks") or []
            ],
        )
        return {"marked": done, "errors": errors}

    @app.get("/api/projects")
    def projects():
        return [project_json(store, pid) for pid in sorted(store.projects)]

    @app.post("/api/projects")
    def create_project(body: Json):
        name = str(body.get("name") or "").strip()
        store.write(
            ops.new_project,
            store.home,
            name,
            body.get("template") or "blank",
            body.get("title") or None,
        )
        return project_json(store, name)

    @app.get("/api/templates")
    def templates():
        return ops.list_templates()

    @app.post("/api/projects/{pid}/types")
    def create_type(pid: str, body: Json):
        p = store.project(pid)
        return store.write(
            ops.new_type, p.path, str(body.get("id") or "").strip(), bool(body.get("review"))
        ).to_json()

    @app.put("/api/projects/{pid}/lanes")
    def set_lanes(pid: str, body: Json):
        """The board's lanes from left to right: {"order": [type id, ...]} (project.yaml lanes)."""
        p = store.write(ops.set_lanes, store.project(pid).path, body.get("order"))
        return {"types": [ty.to_json() for ty in p.types]}

    @app.post("/api/projects/{pid}/archive")
    def archive(pid: str):
        store.write(ops.set_archived, store.project(pid).path, True)
        return project_json(store, pid)

    @app.post("/api/projects/{pid}/unarchive")
    def unarchive(pid: str):
        store.write(ops.set_archived, store.project(pid).path, False)
        return project_json(store, pid)

    @app.get("/api/projects/{pid}/board")
    def board(pid: str):
        return board_json(store, store.project(pid))

    @app.get("/api/projects/{pid}/ledger")
    def get_ledger(pid: str):
        """The campaign's measured results: primary metric, baseline, best, every row and the files in error."""
        return ledger.ledger(store.project(pid))

    # ---- tasks (§3.4, §3.5)

    @app.post("/api/projects/{pid}/tasks")
    def create_task(pid: str, body: Json):
        p = store.project(pid)
        type_, name = str(body.get("type") or ""), str(body.get("name") or "").strip()
        title = str(body.get("title") or "").strip()

        def create():
            ops.new_task(
                p.path,
                type_,
                name,
                body.get("prompt") or "",
                title=title,
                depends_on=body.get("depends_on") or [],
                review=bool(body.get("review")),
                agent=body.get("agent") or None,
                queue=body.get("mode") != "draft",
                machines=body.get("machines") or None,
                checks=body.get("checks"),
            )

        store.write(create)
        p = store.project(pid)
        return task_detail(store, p, p.task(f"{type_}/{name}"))

    @app.post("/api/projects/{pid}/tasks/preview")
    def preview(pid: str, body: Json):
        """What the new-task form shows while you type: the dependencies the prompt and the chosen upstream tasks
        give, and whether the name can be used."""
        p = store.project(pid)
        type_, name = str(body.get("type") or ""), str(body.get("name") or "").strip()
        error = ""
        if name:
            try:
                check_name(name)
            except AgentSysError as e:
                error = str(e)
            if f"{type_}/{name}" in p.tasks or (p.path / "workspaces" / type_ / name).exists():
                error = _("{task} 已存在", task=f"{type_}/{name}")
        edges = preview_edges(
            p, type_, name, body.get("prompt") or "", body.get("depends_on") or []
        )
        return {"name_error": error, "upstream": [edge_end(p, e, e.up) for e in edges]}

    @app.get("/api/projects/{pid}/tasks/{type_}/{name}")
    def get_task(pid: str, type_: str, name: str):
        return task_detail(store, *task_of(pid, type_, name))

    @app.put("/api/projects/{pid}/tasks/{type_}/{name}")
    def put_task(pid: str, type_: str, name: str, body: Json):
        """Only the fields given; '' or null removes a frontmatter key. send_to_agent: a task that has run is told
        about the new prompt by a message."""
        p, t = task_of(pid, type_, name)
        store.write(
            ops.update_task,
            p.path,
            t.id,
            body.get("frontmatter"),
            body.get("prompt"),
            body.get("base_mtime") or "",
        )
        t = store.project(pid).task(t.id)
        if body.get("send_to_agent") and t.managed and t.state.get("runs"):
            store.write(
                runs.message,
                store.home,
                p.path,
                t.id,
                launch.say(
                    launch.agent_language(store.home),
                    "任务 prompt 已更新（`tasks/{task}.md`），请重新阅读并按新要求调整。",
                    task=t.id,
                ),
                also=[(pid, t.id)],
            )
        return task_detail(store, *task_of(pid, type_, name))

    @app.post("/api/projects/{pid}/tasks/{type_}/{name}/rename")
    def rename(pid: str, type_: str, name: str, body: Json):
        p, t = task_of(pid, type_, name)
        new = store.write(
            ops.rename, p.path, t.id, body.get("type") or t.type, str(body.get("name") or t.name)
        )
        return task_detail(store, store.project(pid), store.project(pid).task(new.id))

    @app.post("/api/projects/{pid}/tasks/{type_}/{name}/actions/{action}")
    def act(pid: str, type_: str, name: str, action: str, body: OptionalJson = None):
        p, t = task_of(pid, type_, name)
        body = body or {}
        home, out, also = store.home, {}, [(pid, t.id)]
        if action == "delete":
            trash = store.write(ops.delete, p.path, t.id)
            return {"deleted": t.id, "trash": str(trash.relative_to(store.home))}
        if action == "takeover":
            out["takeover"] = store.write(runs.takeover, home, p.path, t.id, also=also)
        elif action == "resume":
            text = str(body.get("text") or launch.say(launch.agent_language(home), "继续"))
            out["delivery"] = store.write(runs.message, home, p.path, t.id, text, also=also)
        elif action == "add_budget":
            out["budget_usd"] = store.write(
                runs.add_budget,
                home,
                p.path,
                t.id,
                body.get("usd"),
                str(body.get("text") or ""),
                also=also,
            )
        elif action in ("start", "stop", "end_takeover", "cancel"):
            if (
                action == "start"
            ):  # by hand too, a task does not start on machines another one holds
                with store.lock:
                    machines.check_free(store.live(), p, t)
            store.write(getattr(runs, action), home, p.path, t.id, also=also)
        elif action == "join_children":
            out["joined"] = store.write(
                ops.join_children, p.path, t.id, also=[(pid, c.id) for c in p.tasks.values()]
            )
        else:
            do = {
                "join": ops.join,
                "approve": ops.approve,
                "mark_read": ops.mark_read,
            }.get(action)
            if do is None:
                raise AgentSysError(_("不认识的操作「{action}」", action=action), 404)
            store.write(do, p.path, t.id)
        return {**out, "task": task_summary(store.project(pid).task(t.id))}

    @app.post("/api/projects/{pid}/tasks/{type_}/{name}/messages")
    def message(pid: str, type_: str, name: str, body: Json):
        """Your message: into the running session, or a new round of it (resume)."""
        p, t = task_of(pid, type_, name)
        delivery = store.write(
            runs.message, store.home, p.path, t.id, str(body.get("text") or ""), also=[(pid, t.id)]
        )
        return {"delivery": delivery, "task": task_summary(store.project(pid).task(t.id))}

    # ---- questions.md (§3.7)

    @app.get("/api/projects/{pid}/tasks/{type_}/{name}/questions")
    def get_questions(pid: str, type_: str, name: str):
        return questions.read(task_of(pid, type_, name)[1])

    @app.post("/api/projects/{pid}/tasks/{type_}/{name}/questions")
    def answer_questions(pid: str, type_: str, name: str, body: Json):
        p, t = task_of(pid, type_, name)
        out = store.write(
            questions.submit,
            store.home,
            p.path,
            t.id,
            body.get("answers") or {},
            str(body.get("base_mtime") or ""),
            also=[(pid, t.id)],
        )
        return {**out, "task": task_summary(store.project(pid).task(t.id))}

    # ---- run output (§3.9)

    def run_dir_of(pid, type_, name, run):
        t = task_of(pid, type_, name)[1]
        path = runs.runs_dir(t) / run
        if not t.managed or run.startswith(".") or "/" in run or not path.is_dir():
            raise AgentSysError(_("运行记录「{run}」不存在", run=run), 404)
        return t, path

    @app.get("/api/projects/{pid}/tasks/{type_}/{name}/runs/{run}/events")
    def run_events(
        pid: str,
        type_: str,
        name: str,
        run: str,
        after: int = -1,
        before: int | None = None,
        tail: int | None = None,
    ):
        """The events the run view shows, slim (runs.slim): those after seq `after` (what is new), or the last
        `tail` before seq `before` (a long run opens at its end; earlier pages when you scroll up)."""
        t, path = run_dir_of(pid, type_, name, run)
        return {
            "run": runs.run_json(t, runs.record(path)),
            **runs.read_events(path, after, before, tail),
        }

    @app.get("/api/projects/{pid}/tasks/{type_}/{name}/runs/{run}/events/{seq}")
    def run_event(pid: str, type_: str, name: str, run: str, seq: int):
        """One event whole: what an opened tool line or thinking block shows of what was cut."""
        event = runs.read_event(run_dir_of(pid, type_, name, run)[1], seq)
        if event is None:
            raise AgentSysError(_("运行记录「{run}」中没有第 {seq} 条事件", run=run, seq=seq), 404)
        return event

    @app.get("/api/projects/{pid}/tasks/{type_}/{name}/runs/{run}/launch")
    def run_launch(pid: str, type_: str, name: str, run: str):
        return {"markdown": read_text(run_dir_of(pid, type_, name, run)[1] / "launch.md")}

    # ---- workspace files (§3.8)

    def raw_url(pid, tid, rel):
        return f"/api/projects/{pid}/tasks/{tid}/raw?path={quote(rel)}"

    @app.get("/api/projects/{pid}/tasks/{type_}/{name}/files")
    def list_files(pid: str, type_: str, name: str, dir: str = ""):
        return files.list_dir(task_of(pid, type_, name)[1].workspace, dir)

    @app.get("/api/projects/{pid}/tasks/{type_}/{name}/file")
    def read_file(pid: str, type_: str, name: str, path: str):
        t = task_of(pid, type_, name)[1]
        return files.read_file(t.workspace, path, raw_url(pid, t.id, path))

    @app.put("/api/projects/{pid}/tasks/{type_}/{name}/file")
    def write_file(pid: str, type_: str, name: str, path: str, body: Json):
        t = task_of(pid, type_, name)[1]
        return store.write(
            files.write_file,
            t.workspace,
            path,
            str(body.get("content") or ""),
            body.get("base_mtime") or "",
            also=[(pid, t.id)],
        )

    @app.get("/api/projects/{pid}/tasks/{type_}/{name}/raw")
    def raw(pid: str, type_: str, name: str, path: str):
        target = files.resolve(task_of(pid, type_, name)[1].workspace, path)
        if not target.is_file():
            raise AgentSysError(_("文件「{path}」不存在", path=path), 404)
        return FileResponse(target, headers=files.raw_headers(target))

    # ---- rules and knowledge (§3.11)

    def scope_of(body):
        """(scope, project id or None) of a rules / knowledge write; a project must exist."""
        scope, pid = body.get("scope"), str(body.get("project") or "") or None
        if scope not in ("global", "project") or (scope == "project" and not pid):
            raise AgentSysError(_("scope 应为 global，或 project 并给出 project"))
        if scope == "project":
            store.project(pid)
        return scope, pid if scope == "project" else None

    @app.get("/api/rules")
    def get_rules(project: str = ""):
        return knowledge.rules(store.home, store.project(project) if project else None)

    @app.put("/api/rules")
    def put_rule(body: Json):
        _scope, pid = scope_of(body)
        return store.write(
            knowledge.save_rule,
            store.home,
            pid,
            str(body.get("name") or ""),
            str(body.get("content") or ""),
            str(body.get("base_mtime") or ""),
            also=[(pid or "", None)],
        )

    @app.get("/api/knowledge")
    def get_knowledge(project: str = ""):
        """Candidates of one project (all projects that are not archived on the global page) and the libraries."""
        with store.lock:
            projects = (
                [store.project(project)] if project else [p for p in store.live() if not p.archived]
            )
            return {
                "candidates": knowledge.candidates(store.home, projects),
                "libraries": [
                    *([knowledge.library(store.home, project)] if project else []),
                    knowledge.library(store.home),
                ],
                "types": list(knowledge.ENTRY_TYPES),
            }

    @app.post("/api/knowledge/candidates/{action}")
    def handle_candidate(action: str, body: Json):
        def do():
            p, t, path = knowledge.find_candidate(
                store.home, store.project, str(body.get("id") or "")
            )
            scope = body.get("scope") or "global"
            if action == "adopt":
                knowledge.adopt(
                    store.home,
                    p,
                    t,
                    path,
                    scope,
                    body.get("name"),
                    body.get("description"),
                    body.get("type"),
                    body.get("body"),
                )
            elif action == "to_rule":
                knowledge.to_rule(
                    store.home,
                    p,
                    path,
                    scope,
                    str(body.get("file") or ""),
                    str(body.get("text") or ""),
                )
            elif action == "discard":
                knowledge.discard(path)
            else:
                raise AgentSysError(_("不认识的操作「{action}」", action=action), 404)
            return p, t, scope

        p, t, scope = store.write(do)
        store.sync(also=[(p.id, t.id), (p.id if scope == "project" else "", None)])
        return {"ok": True}

    @app.put("/api/knowledge/entries")
    def put_entry(body: Json):
        _scope, pid = scope_of(body)
        store.write(
            knowledge.edit_entry,
            store.home,
            pid,
            str(body.get("name") or ""),
            str(body.get("description") or ""),
            str(body.get("type") or ""),
            str(body.get("body") or ""),
            str(body.get("base_mtime") or ""),
            also=[(pid or "", None)],
        )
        return knowledge.library(store.home, pid)

    @app.post("/api/knowledge/entries/delete")
    def delete_entry(body: Json):
        _scope, pid = scope_of(body)
        store.write(
            knowledge.delete_entry,
            store.home,
            pid,
            str(body.get("name") or ""),
            str(body.get("base_mtime") or ""),
            also=[(pid or "", None)],
        )
        return knowledge.library(store.home, pid)

    # ---- settings (§3.11)

    @app.get("/api/settings")
    def get_settings(project: str = ""):
        if project:
            store.project(project)
        return ops.settings(store.home, project or None)

    @app.put("/api/settings")
    def put_settings(body: Json):
        scope, pid = body.get("scope"), body.get("project") or None
        if scope == "project":
            store.project(pid)
        values = body.get("values") or {}
        out = store.write(ops.save_settings, store.home, scope, values, pid)
        if (
            scope == "global" and "language" in values
        ):  # the reminders on the cards are made at scan time
            i18n.use(store.home)
            with store.lock:
                for p in list(store.projects):
                    store.rescan(p)
                sched.annotate(store.live(), sched.global_limit(store.home))
            store.notify([(p, None) for p in list(store.projects)])
        return out

    @app.get("/api/models")
    def get_models():
        """The models offered in the forms, and what the aliases stand for here (launch.models)."""
        return {"models": launch.models()}

    # ---- changes (§3.12)

    @app.get("/api/events")
    async def events(request: Request):
        queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        listener = lambda change: loop.call_soon_threadsafe(queue.put_nowait, change)  # noqa: E731
        store.listeners.add(listener)

        async def stream():
            try:
                yield "retry: 3000\n\n"
                while True:
                    try:
                        first = await asyncio.wait_for(queue.get(), 15)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    await asyncio.sleep(COALESCE_SECONDS)
                    batch = [first] + [queue.get_nowait() for _ in range(queue.qsize())]
                    for change, c in {json.dumps(c, ensure_ascii=False): c for c in batch}.items():
                        yield f"event: {'run' if 'run' in c else 'changed'}\ndata: {change}\n\n"
            finally:
                store.listeners.discard(listener)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/")
    @app.get("/index.html")
    def index():
        """The page in the language of agent_sys.yaml: the scripts read it from <html lang> before they draw anything.
        It names every module up front, so the browser asks for them all at once instead of one import level per
        round trip."""
        page = read_text(WEB / "index.html").replace(
            '<html lang="en">', f'<html lang="{i18n.LANG}">', 1
        )
        modules = sorted(
            p.relative_to(WEB).as_posix()
            for p in [*WEB.glob("*.js"), *WEB.glob("vendor/*.*js")]
            if p.name != "boot.js"
        )
        links = "".join(f'<link rel="modulepreload" href="{m}">\n' for m in modules)
        return HTMLResponse(page.replace("</head>", f"{links}</head>", 1))

    app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
    return app
