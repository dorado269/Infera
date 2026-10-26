"""Files -> Project: tasks, types, edges and record reminders; and change detection by polling stat.

Grown from the prototype's watchboard/build.py: the same progress.md head rule, the same path rule for
dependencies named in prompts, the same lint rules (plus "done without a report").
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

from . import checks, machines
from .i18n import _
from .model import (
    EDGE_KINDS,
    NAME,
    PROJECT_DEFAULTS,
    AgentSysError,
    Edge,
    Progress,
    Project,
    Task,
    TaskType,
    classify_progress,
    cost_today,
    cycle_tasks,
    dep_task,
    iso,
    load_yaml,
    mtime,
    plain,
    read_state,
    read_task_file,
    read_text,
)

STALE_HOURS = 24  # a running task whose progress.md is older than this gets a reminder
OLD_DAYS = (
    7  # reminders about tasks finished longer ago than this are only hints (old news, not to-dos)
)
# Another task named in a prompt: its path tasks/<type>/<name> or workspaces/<type>/<name>, or @<type>/<name> (what
# the new-task form inserts when you type @). A path under another project (campaigns/<other>/…) is not a
# dependency. ASCII names only, as model.NAME; text in any language may come right before it.
TASK_REF = re.compile(
    r"(?<![\w\-])(?:(?:campaigns/([\w.\-]+)/)?(?:tasks|workspaces)/|@)([\w\-]+)/([\w\-]+)", re.ASCII
)
PARENT_REF = re.compile(r"tasks/([\w\-]+)/([\w\-]+)\.md`?\s*的子任务", re.ASCII)


def project_dir(home, pid):
    return Path(home) / "campaigns" / pid


def project_ids(home):
    """Projects = directories under campaigns/ with a project.yaml."""
    camp = Path(home) / "campaigns"
    if not camp.is_dir():
        return []
    return sorted(
        p.name
        for p in camp.iterdir()
        if not p.name.startswith(".") and (p / "project.yaml").is_file()
    )


def subdirs(path):
    return (
        sorted(p for p in path.iterdir() if p.is_dir() and not p.name.startswith("."))
        if path.is_dir()
        else []
    )


def depends_on(fm):
    """frontmatter depends_on as a list (a single entry may be written without the list)."""
    deps = fm.get("depends_on")
    return [] if deps is None else deps if isinstance(deps, list) else [deps]


def lane_order(config):
    """project.yaml's lanes: the order of the board's lanes, as type ids (types left out follow, by name)."""
    lanes = config.get("lanes")
    return [str(x) for x in lanes] if isinstance(lanes, list) else []


def load_types(path, order=()):
    """Types = subdirectories of tasks/, in the order given (project.yaml lanes), the others after it by name. The
    skeleton task_types/<type>.md gives the prompt and the review and checks defaults."""
    types, rank = [], {tid: i for i, tid in enumerate(order)}
    for d in sorted(subdirs(path / "tasks"), key=lambda d: (rank.get(d.name, len(rank)), d.name)):
        try:
            fm, skeleton = read_task_file(path / "task_types" / f"{d.name}.md")
        except ValueError:
            fm, skeleton = {}, ""
        checks = fm.get("checks")
        types.append(
            TaskType(
                d.name,
                review=fm.get("review") is True,
                checks=checks if isinstance(checks, list) else [],
                skeleton=skeleton,
            )
        )
    return types


def work_started(prompt, workspace):
    """When work on an unmanaged task began: the prompt's mtime, or the oldest ctime among the workspace's
    top-level entries if that is earlier (the prototype's rule; ctime, because copies keep their old mtime)."""
    times = [mtime(prompt)]
    if workspace.is_dir():
        for e in os.scandir(workspace):
            try:
                times.append(e.stat(follow_symlinks=False).st_ctime)
            except OSError:  # removed while we looked
                pass
    return min((t for t in times if t), default=None)


def report_files(workspace):
    """Report candidates, best first: report.md, other *report*.md, then numbered reports/*.md, newest first."""
    if not workspace.is_dir():
        return []
    names = sorted(
        (p.name for p in workspace.glob("*.md") if "report" in p.name),
        key=lambda n: (n != "report.md", n.endswith("_en.md"), n),
    )
    numbered = sorted(
        (p for p in workspace.glob("reports/*.md") if re.match(r"\d", p.name)), reverse=True
    )
    return names + [str(p.relative_to(workspace)) for p in numbered]


# The handoff list of a report (the agent_sys protocol): a "## 交接清单" section after the conclusion, one line per item,
# `path` — what it is. The older names in the product discussion are read too.
HANDOFF_HEAD = re.compile(
    r"^##[ \t]*(?:交接清单|交接|产物|Handoff(?: list)?|Artifacts)[ \t]*$", re.M | re.I
)
HANDOFF_MAX = 40  # items read from one report


def handoff(workspace, report):
    """The handoff list of a report as [{text, path, exists, kind, rel}]; None when the report has no such section.
    An item that starts with a `code span` names a path: relative to the workspace (else to the project root), or
    absolute; kind is file / dir, or None when it is not there; rel is the path inside the workspace (the UI opens
    it), '' when it is outside. Other items (a branch, a PR) have path ''."""
    text = read_text(workspace / report)
    m = HANDOFF_HEAD.search(text)
    if not m:
        return None
    end = re.search(r"^#{1,2}[ \t]", text[m.end() :], re.M)
    section = text[m.end() : m.end() + end.start()] if end else text[m.end() :]
    items = []
    for line in re.findall(r"^[ \t]*[-*+][ \t]+(.+)$", section, re.M)[:HANDOFF_MAX]:
        item = {"text": line.strip(), "path": "", "exists": False, "kind": None, "rel": ""}
        code = re.match(
            r"`([^`\s]+)`", line.strip()
        )  # a path has no spaces; a command or a sentence is not one
        if code and "://" not in code[1]:
            item["path"] = code[1]
            try:
                base = Path(item["path"]).expanduser()
                for p in (
                    [base]
                    if base.is_absolute()
                    else [workspace / base, workspace.parent.parent.parent / base]
                ):
                    if p.exists():
                        item.update(exists=True, kind="dir" if p.is_dir() else "file")
                        try:
                            item["rel"] = str(p.resolve().relative_to(workspace.resolve()))
                        except ValueError:
                            pass
                        break
            except (
                OSError,
                RuntimeError,
                ValueError,
            ):  # ~nobody/x, a name too long, a directory you cannot read
                pass
        items.append(item)
    return items


def load_task(path, pid, type_, name, defaults):
    """One task from its prompt file, its workspace's progress.md and .agent_sys/state.json."""
    ws = path.parent.parent.parent / "workspaces" / type_ / name
    t = Task(project=pid, type=type_, name=name, path=path, workspace=ws, fm={}, body="")
    text = read_text(path)
    try:
        t.fm, t.body = read_task_file(path)
    except ValueError as e:
        t.body = text
        t.warnings.append(_("frontmatter 无法解析：{error}", error=e))
    t.title = str(t.fm.get("title") or name)
    t.summary = str(t.fm.get("summary") or "")
    t.review = t.fm.get("review") is True
    t.parent = task_parent(t.fm, t.body)
    agent = t.fm.get("agent") or {}
    t.agent = {**defaults, **(agent if isinstance(agent, dict) else {})}
    t.machines = machines.task_machines(t.fm)
    try:
        t.checks = checks.parse(t.fm.get("checks"))
    except ValueError as e:
        t.warnings.append(str(e))

    if progress_mtime := mtime(ws / "progress.md"):
        t.progress = Progress.parse(read_text(ws / "progress.md"), progress_mtime)
    try:
        t.state = read_state(ws)
    except ValueError as e:
        t.warnings.append(str(e))
        t.status, t.detail = "unknown", {"raw": _("state.json 无法解析")}
    p, s = t.progress, t.state
    if s:
        t.status, t.since = s["status"], s.get("since", "")
        t.cost_today = cost_today(ws)
        t.detail = (
            (s.get("detail") or {}) if t.status == "needs_you" else {}
        )  # the rest: derive_details()
        if s.get("run_id"):
            t.check_result = checks.summary(ws / ".agent_sys" / "runs" / s["run_id"])
        t.created, t.started, t.finished = (
            s.get("created", ""),
            s.get("started", ""),
            s.get("finished", ""),
        )
    else:
        if t.status != "unknown":
            t.status, t.detail = classify_progress(
                p, t.review, str(plain(t.fm.get("approved_at")) or "")
            )
        t.since = iso(p.mtime if p else mtime(path))
        t.created = iso(mtime(path))
        t.started = iso(work_started(path, ws)) if p else ""
        t.finished = p.finished_iso if p and p.done else ""
    t.updated = iso(
        max(
            (x for x in (mtime(path), p and p.mtime, mtime(ws / ".agent_sys" / "state.json")) if x),
            default=None,
        )
    )
    if t.status in ("done", "in_review"):
        t.report = next(iter(report_files(ws)), None)
        t.handoff = handoff(ws, t.report) if t.report else None
    return t


def task_parent(fm, body):
    """The frontmatter parent, else the task named in "这是 tasks/…/X.md 的子任务" in the prompt."""
    fm_parent = fm.get("parent")
    m = PARENT_REF.search(body)
    return (fm_parent if isinstance(fm_parent, str) else None) or (m and f"{m[1]}/{m[2]}") or None


def preview_edges(project, type_, name, prompt, depends_on=()):
    """The dependencies a new task would get from its prompt and depends_on, as the scan will find them once the
    task file is written (the new-task form shows them; the UI does not match paths itself)."""
    fm = {"depends_on": list(depends_on)}
    t = Task(
        project=project.id,
        type=type_ or "_",
        name=name or "_",
        path=Path(),
        workspace=Path(),
        fm=fm,
        body=prompt or "",
        parent=task_parent(fm, prompt or ""),
    )
    return task_edges(t, project.tasks, project.id)


def task_edges(task, ids, project):
    """Edges into one task, in order of precedence (later ones override the kind of earlier ones):
    1. other tasks' paths named in its prompt (input; the task's parent gives subtask), evidence = the line;
    2. its frontmatter parent (subtask);
    3. its frontmatter depends_on (kind and label as written, input by default).
    References to unknown tasks are skipped here and reported by lint()."""
    edges = {}
    for line in task.body.splitlines():
        for camp, type_, name in TASK_REF.findall(line):
            up = f"{type_}/{name}"
            if (camp and camp != project) or up == task.id or up not in ids or up in edges:
                continue
            edges[up] = Edge(
                up,
                task.id,
                "subtask" if up == task.parent else "input",
                evidence=line.strip(" -*"),
                source="prompt",
            )
    fm_parent = task.fm.get("parent") if isinstance(task.fm.get("parent"), str) else None
    if fm_parent in ids and fm_parent != task.id:
        edges[fm_parent] = Edge(
            fm_parent, task.id, "subtask", evidence=edges.get(fm_parent, Edge("", "")).evidence
        )
    for entry in depends_on(task.fm):
        up = dep_task(entry)
        kind = entry.get("kind", "input") if isinstance(entry, dict) else "input"
        if up not in ids or up == task.id or kind not in EDGE_KINDS:
            continue
        edges[up] = Edge(
            up,
            task.id,
            kind,
            label=str(entry.get("label") or "") if isinstance(entry, dict) else "",
            evidence=edges.get(up, Edge("", "")).evidence,
        )
    return list(edges.values())


def derive_details(project):
    """What a managed task waits for or is doing, worked out from the other files rather than stored in state.json
    (it would go stale): why a queued task has not started, the sub-state of a running one (runs.Monitor reads it
    from the run), and in_review's "read the report". A queued task waits, the first that holds: 项目已归档 >
    阻塞类上游 > 上游的子任务 (children edges) > 已关闭自动开始 (and then for its machines and a slot, sched.plan)."""
    for t in project.tasks.values():
        if not t.managed:
            continue
        if t.status == "queued":
            upstream = project.waiting_on(t.id)
            children = project.waiting_on_children(t.id) if not upstream else {}
            t.detail = (
                {"kind": "archived"}
                if project.archived
                else {"kind": "upstream", "tasks": upstream}
                if upstream
                else {
                    "kind": "children",
                    "parents": list(children),
                    "tasks": [c for pending in children.values() for c in pending],
                }
                if children
                else {"kind": "template_review"}
                if project.template_review
                else {"kind": "manual"}
                if project.config.get("autorun", True) is False
                else {}
            )
        elif t.status == "running":
            t.detail = {
                "kind": "working"
            }  # the server refines it from the run's events (runs.Monitor)
        elif t.status == "in_review":
            t.detail = {"kind": "review"}


def lint(project, now):
    """Record reminders (记录提醒): the prototype's four rules, done-without-report, and broken references.
    For a task finished more than OLD_DAYS ago they are hints only."""
    tasks, edges = project.tasks, project.edges
    cyclic = cycle_tasks(edges)
    for t in tasks.values():
        if not (NAME.fullmatch(t.type) and NAME.fullmatch(t.name)):
            t.warnings.append(
                _("名称只能包含字母、数字、_ 和 -：其他任务的 prompt 中写它的路径不会被识别为依赖")
            )
        for entry in depends_on(t.fm):
            up = dep_task(entry)
            if up is None:
                t.warnings.append(
                    _(
                        "depends_on 中的「{entry}」看不懂：应写成 类型/任务名 或 {task, kind, label}",
                        entry=entry,
                    )
                )
            elif up not in tasks:
                t.warnings.append(_("depends_on 中的任务「{task}」不存在", task=up))
            elif isinstance(entry, dict) and entry.get("kind", "input") not in EDGE_KINDS:
                t.warnings.append(
                    _(
                        "depends_on 中「{task}」的 kind「{kind}」不认识（可用 {kinds}）",
                        task=up,
                        kind=entry["kind"],
                        kinds="/".join(EDGE_KINDS),
                    )
                )
        parent = t.fm.get("parent")
        if parent and not isinstance(parent, str):
            t.warnings.append(_("parent「{parent}」看不懂：应写成 类型/任务名", parent=parent))
        elif parent and parent not in tasks:
            t.warnings.append(_("parent「{parent}」不存在", parent=parent))
        if t.id in cyclic:
            t.warnings.append(_("阻塞类依赖（input / code）形成了循环，任务会一直等待"))

        p = t.progress
        if p is None or t.status in ("draft", "queued", "cancelled"):
            continue
        age = (now - p.mtime) / 3600
        warn = t.hints.append if p.done and age > OLD_DAYS * 24 else t.warnings.append
        if not p.status:
            warn(_("progress.md 缺少「状态:」行"))
        if p.done and not p.finished:
            warn(_("已完成，但 progress.md 缺少「完成时间:」行"))
        if (
            t.status == "running"
            and t.detail.get("kind") in (None, "working", "unmanaged")
            and age > STALE_HOURS
        ):
            warn(_("progress.md 已 {hours} 小时未更新", hours=f"{age:.0f}"))
        if p.done:
            reports = report_files(t.workspace)
            if not reports:
                warn(_("已完成，但工作区中没有 report.md"))
            elif reports[0] != "report.md":
                t.hints.append(_("没有 report.md，报告为 {report}", report=reports[0]))
            if reports and t.handoff is None:
                t.hints.append(
                    _('报告中没有交接清单（"## 交接清单"一节），下游任务只能自己翻工作区')
                )
            elif missing := [h["path"] for h in t.handoff or [] if h["path"] and not h["exists"]]:
                warn(
                    _(
                        "交接清单中的路径不存在：{paths}",
                        paths=_("、").join(missing[:5]) + ("…" if len(missing) > 5 else ""),
                    )
                )
            open_checks = p.checks["todo"] + p.checks["doing"]
            if open_checks:
                t.hints.append(_("已完成，但 progress.md 仍有 {n} 个未勾选项", n=open_checks))
        else:
            newer = [
                tasks[e.down]
                for e in edges
                if e.up == t.id
                and e.kind == "subtask"
                and tasks[e.down].progress
                and tasks[e.down].progress.mtime - p.mtime > 3600
            ]
            if newer:
                c = max(newer, key=lambda c: c.progress.mtime)
                t.warnings.append(
                    _(
                        "子任务「{task}」的进展晚于本任务 progress.md，状态和下一步可能未同步",
                        task=c.title,
                    )
                )


def load_project(path):
    """Scan one project. Works on any campaign directory (the prototype has no project.yaml)."""
    path = Path(path)
    try:
        config = load_yaml(read_text(path / "project.yaml"))
    except ValueError as e:
        raise AgentSysError(
            _("{project}/project.yaml 无法解析：{error}", project=path.name, error=e), 422
        ) from None
    if not isinstance(config.get("agent") or {}, dict):
        raise AgentSysError(
            _(
                "{project}/project.yaml 的 agent 应为键值表（例如 agent: {model: opus}）",
                project=path.name,
            ),
            422,
        )
    for key in ("autorun", "template_review"):
        if config.get(key) is not None and not isinstance(config[key], bool):
            raise AgentSysError(
                _(
                    "{project}/project.yaml 的 {key} 应为 true 或 false，而不是「{value}」",
                    project=path.name,
                    key=key,
                    value=config[key],
                ),
                422,
            )
    defaults = {**PROJECT_DEFAULTS["agent"], **(config.get("agent") or {})}
    project = Project(
        id=path.name, path=path, config=config, types=load_types(path, lane_order(config))
    )
    for ty in project.types:
        for f in sorted((path / "tasks" / ty.id).glob("*.md")):
            if not f.name.startswith(".") and f.is_file():
                t = load_task(f, project.id, ty.id, f.stem, defaults)
                project.tasks[t.id] = t
                ty.count += 1
    project.edges = [
        e for t in project.tasks.values() for e in task_edges(t, project.tasks, project.id)
    ]
    derive_details(project)
    lint(project, datetime.now(timezone.utc).timestamp())
    return project


# ---------------------------------------------------------------- change detection


def _stat(path):
    try:
        st = path.stat()
        return st.st_mtime_ns, st.st_size
    except OSError:
        return None


class Watcher:
    """Tells which projects and tasks changed, by polling stat (design §3.2: inotify is unreliable on NFS and a
    workspace can hold hundreds of thousands of files, so nothing is walked recursively).

    Per project it stats project.yaml, rules/*.md, knowledge/INDEX.md and entries/*.md, task_types/*.md and the
    tasks/ directories; per task its prompt file, its workspace directory (top-level entries added or removed),
    progress.md, report.md, issue.md, questions.md, lessons/*.md, results/ (a result file added or removed),
    .agent_sys/state.json and .agent_sys/runs/ (a new run). AGENT_SYS_HOME's own rules/*.md and knowledge/ are key
    ('', None).
    The server calls poll() every 2 s and rescans the projects it names. Running events files are watched
    separately."""

    TASK_FILES = (
        "",
        "progress.md",
        "report.md",
        "issue.md",
        "questions.md",
        "lessons",
        ".agent_sys/state.json",
        "results",
        ".agent_sys/runs",
    )

    def __init__(self, home):
        self.home = Path(home)
        self.snapshot = self.fingerprints()

    def fingerprints(self):
        fp = {("", None): self.rules_and_knowledge(self.home)}
        for pid in project_ids(self.home):
            pdir = project_dir(self.home, pid)
            proj = [_stat(pdir / f) for f in ("project.yaml", "task_types", "tasks")]
            proj += self.rules_and_knowledge(pdir)
            proj += [(f.name, _stat(f)) for f in sorted((pdir / "task_types").glob("*.md"))]
            for tdir in subdirs(pdir / "tasks"):
                proj.append((tdir.name, _stat(tdir)))
                for f in tdir.glob("*.md"):
                    if f.name.startswith("."):
                        continue
                    ws = pdir / "workspaces" / tdir.name / f.stem
                    lessons = tuple(
                        (f.name, _stat(f)) for f in sorted((ws / "lessons").glob("*.md"))
                    )
                    fp[pid, f"{tdir.name}/{f.stem}"] = (
                        (_stat(f),) + tuple(_stat(ws / n) for n in self.TASK_FILES) + lessons
                    )
            fp[pid, None] = tuple(proj)
        return fp

    @staticmethod
    def rules_and_knowledge(base):
        """rules/*.md, knowledge/INDEX.md and knowledge/entries/*.md under AGENT_SYS_HOME or a project."""
        out = [
            _stat(base / "rules"),
            _stat(base / "knowledge" / "INDEX.md"),
            _stat(base / "knowledge" / "entries"),
        ]
        out += [(f.name, _stat(f)) for f in sorted((base / "rules").glob("*.md"))]
        out += [(f.name, _stat(f)) for f in sorted((base / "knowledge" / "entries").glob("*.md"))]
        return out

    def forget(self, pid):
        """The next poll reports the whole project as changed (its scan failed: try again)."""
        self.snapshot = {k: v for k, v in self.snapshot.items() if k[0] != pid}

    def poll(self):
        """[(project, task id)] changed since the last poll; task id None = the project itself changed
        (project.yaml, rules, knowledge, types, or the project was added / removed); ('', None) = AGENT_SYS_HOME's
        rules or knowledge. A removed task is listed too."""
        old, self.snapshot = self.snapshot, self.fingerprints()
        changed = {
            k for k in old.keys() | self.snapshot.keys() if old.get(k) != self.snapshot.get(k)
        }
        return sorted(changed, key=lambda k: (k[0], k[1] or ""))
