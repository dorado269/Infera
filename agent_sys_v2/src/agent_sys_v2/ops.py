"""Write operations on projects and tasks. The server calls these; each one
checks what it needs on a fresh scan, writes files, and returns the result read back from disk.
Refusals raise AgentSysError with a message for the user."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from .checks import parse as parse_checks
from .i18n import LANGUAGES, _, status_word
from .model import (
    BLOCKING,
    EDGE_KINDS,
    GLOBAL_DEFAULTS,
    NAME,
    PROJECT_DEFAULTS,
    AgentSysError,
    blocking_graph,
    check_name,
    cycle_tasks,
    dep_task,
    dump_yaml,
    iso,
    load_config,
    load_yaml,
    mtime,
    new_state,
    now_iso,
    plain,
    reaches,
    read_task_file,
    read_text,
    split_frontmatter,
    write_state,
    write_task_file,
    write_text,
)
from .scan import TASK_REF, depends_on, load_project, project_dir, project_ids

TEMPLATES = Path(__file__).resolve().parent / "templates"
DEFAULT_SKELETON = '## 任务描述\n\n- \n\n## 交付\n\n- `report.md`：以"## 结论"开头。\n'
AGENT_SYS_YAML = f"""# agent_sys's global settings. A key left out takes its default; saving on the settings page keeps these comments.
language: {GLOBAL_DEFAULTS["language"]}                     # en or zh: the language of the web UI and of agent_sys's messages
protocol_language: {GLOBAL_DEFAULTS["protocol_language"]}            # zh or en: the agent_sys protocol and what agent_sys tells the agent (it works in the prompt's language)
max_running: {GLOBAL_DEFAULTS["max_running"]}                   # tasks running at once across all projects (background waits and terminal takeovers count)
# cli_path: /path/to/claude      # default: the claude bundled with claude-agent-sdk in agent_sys's venv
"""


def update_frontmatter(path, change):
    """Rewrite a task file's frontmatter with change(fm); the body and unknown fields are kept."""
    if not path.is_file():
        raise AgentSysError(_("任务文件 {file} 已不存在", file=path.name), 404)
    try:
        fm, body = read_task_file(path)
    except ValueError as e:
        raise AgentSysError(
            _("{file} 的 frontmatter 无法解析：{error}", file=path.name, error=e), 422
        ) from None
    change(fm)
    write_task_file(path, fm, body)


def edit_yaml(path, change):
    """Rewrite a YAML file (project.yaml, agent_sys.yaml) with change(data); comments and key order are kept.
    A missing file starts empty."""
    try:
        data = load_yaml(read_text(path))
    except ValueError as e:
        raise AgentSysError(_("{file} 无法解析：{error}", file=path.name, error=e), 422) from None
    change(data)
    write_text(path, dump_yaml(data))


def flow(mapping):
    """A mapping written on one line, like `agent: {budget_usd: 300}` in design §4.3."""
    m = CommentedMap(mapping)
    m.fa.set_flow_style()
    return m


def flow_list(items):
    """A list written on one line, like `lanes: [develop, bug_fix]`."""
    seq = CommentedSeq(items)
    seq.fa.set_flow_style()
    return seq


def dep_entry(up, kind="input", label=""):
    """depends_on entry: the short form for a plain input, else {task, kind, label} on one line."""
    if kind == "input" and not label:
        return up
    return flow({"task": up, "kind": kind, **({"label": label} if label else {})})


def dep_list(fm):
    """fm's depends_on as a list that can be edited in place (created if missing)."""
    deps = fm.get("depends_on")
    if not isinstance(deps, list):
        fm["depends_on"] = deps = [] if deps is None else [deps]
    return deps


def edit_deps(fm, fn):
    """Apply fn(entry) -> entry or None (drop) to each depends_on entry, in place so that the comments of the
    other entries stay; an emptied depends_on is removed."""
    if fm.get("depends_on") is None:
        return
    deps = dep_list(fm)
    for i in reversed(range(len(deps))):
        entry = fn(deps[i])
        if entry is None:
            del deps[i]
        else:
            deps[i] = entry
    if not deps:
        del fm["depends_on"]


# project.yaml keys the settings page may write: the defaults, and what a campaign says about itself.
PROJECT_SETTINGS = PROJECT_DEFAULTS | {"title": "", "goal": "", "machines": [], "ledger": {}}


def check_not_archived(project):
    if project.archived:
        raise AgentSysError(_("项目「{project}」已归档，请先取消归档", project=project.title), 409)


def check_no_cycle(project, up, down, kind):
    """Adding a blocking edge up -> down is refused if down is already (indirectly) upstream of up."""
    edges = [e for e in project.edges if (e.up, e.down) != (up, down)]
    if kind in BLOCKING and reaches(blocking_graph(edges), down, up):
        raise AgentSysError(
            _(
                "会形成循环依赖：「{down}」已经（间接）是「{up}」的上游",
                down=project.tasks[down].title,
                up=project.tasks[up].title,
            ),
            409,
        )


def new_cycle(pdir, tid, before):
    """After a write that may add edges into tid: the upstream task that now closes a cycle through tid, if any
    (`before` = the tasks on cycles before the write)."""
    project = load_project(pdir)
    cyc = cycle_tasks(project.edges)
    if tid not in cyc or tid in before:
        return None
    return next(project.tasks[e.up] for e in project.upstream(tid) if e.blocking and e.up in cyc)


# ---------------------------------------------------------------- types and tasks


def new_type(pdir, type_id, review=False):
    """New type = tasks/<type>/; also task_types/<type>.md with a generic skeleton unless one exists."""
    check_name(type_id, _("类型名"))
    pdir = Path(pdir)
    if (pdir / "tasks" / type_id).exists():
        raise AgentSysError(_("类型「{type}」已存在", type=type_id), 409)
    skeleton = pdir / "task_types" / f"{type_id}.md"
    if not skeleton.exists():
        write_task_file(skeleton, CommentedMap(review=True) if review else {}, DEFAULT_SKELETON)
    (pdir / "tasks" / type_id).mkdir(parents=True)
    return next(t for t in load_project(pdir).types if t.id == type_id)


def set_lanes(pdir, order):
    """The order of the board's lanes: project.yaml lanes, the type ids from left to right. Types left out follow
    by name; ids of types that are gone are dropped."""
    project = load_project(pdir)
    if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
        raise AgentSysError(_("泳道顺序应为类型名的列表"))
    types = [ty.id for ty in project.types]
    lanes = list(dict.fromkeys(x for x in order if x in types))

    def change(config):
        if lanes:
            config["lanes"] = flow_list(lanes)
        else:
            config.pop("lanes", None)

    edit_yaml(project.path / "project.yaml", change)
    return load_project(pdir)


def new_task(
    pdir,
    type_,
    name,
    prompt,
    *,
    title="",
    summary="",
    depends_on=(),
    review=False,
    agent=None,
    queue=True,
    machines=None,
    checks=None,
):
    """创建并排队 (queue=True): the task file and a state.json in 待开始, with a new session_id.
    保存为草稿 (queue=False): the task file only. depends_on: ids or {task, kind, label}. agent: only what differs
    from the project's defaults, so that later changes to project.yaml still reach the task. machines: names from
    project.yaml. checks: the acceptance checks as frontmatter writes them; None = the type skeleton's."""
    project = load_project(pdir)
    check_not_archived(project)
    check_name(name, _("任务名"))
    if type_ not in {t.id for t in project.types}:
        raise AgentSysError(_("类型「{type}」不存在", type=type_), 404)
    tid = f"{type_}/{name}"
    path, ws = (
        project.path / "tasks" / type_ / f"{name}.md",
        project.path / "workspaces" / type_ / name,
    )
    if path.exists() or ws.exists():
        raise AgentSysError(_("任务「{task}」已存在", task=tid), 409)

    fm = CommentedMap()
    if title:
        fm["title"] = title
    if summary:
        fm["summary"] = summary
    if deps := dep_entries(project, tid, list(depends_on), {}):
        fm["depends_on"] = deps
    if review:
        fm["review"] = True
    if machines:
        fm["machines"] = flow_list([str(m) for m in machines])
    skeleton = next(ty for ty in project.types if ty.id == type_)
    if checks := skeleton.checks if checks is None else checks:
        try:
            parse_checks(checks)
        except ValueError as e:
            raise AgentSysError(str(e)) from None
        fm["checks"] = checks
    if agent:
        fm["agent"] = flow(agent)
    write_task_file(path, fm, prompt)

    # Other prompts may already name this task's path, so check cycles on the scan that includes it.
    if up := new_cycle(pdir, tid, cycle_tasks(project.edges)):
        path.unlink()
        raise AgentSysError(
            _("会形成循环依赖：「{up}」已经（间接）依赖这个新任务", up=up.title), 409
        )
    if queue:
        write_state(ws, new_state("queued"))
    return load_project(pdir).tasks[tid]


def dep_entries(project, tid, deps, fm):
    """A depends_on list from the UI ([id or {task, kind, label}]) as it is written: short form for a plain input.
    An entry already in the file may name a task that is gone (it stays a record reminder); a new one may not."""
    if not isinstance(deps, list):
        raise AgentSysError(_("depends_on 应为列表"))
    known = {dep_task(e) for e in depends_on(fm)}
    out, seen = [], set()
    for d in deps:
        d = {"task": d} if isinstance(d, str) else d
        up = dep_task(d)
        if up is None:
            raise AgentSysError(
                _(
                    "depends_on 中的「{entry}」看不懂：应写成 类型/任务名 或 {task, kind, label}",
                    entry=d,
                )
            )
        if up == tid:
            raise AgentSysError(_("任务不能依赖自己"))
        if up not in known:
            project.task(up)
        kind = d.get("kind") or "input"
        if kind not in EDGE_KINDS:
            raise AgentSysError(
                _("依赖类型「{kind}」不认识（可用 {kinds}）", kind=kind, kinds="/".join(EDGE_KINDS))
            )
        if up not in seen:
            seen.add(up)
            out.append(dep_entry(up, kind, str(d.get("label") or "")))
    return out


def update_task(pdir, tid, frontmatter=None, prompt=None, base_mtime=""):
    """修改任务配置: set the frontmatter keys given ('' / None / {} / [] removes the key; a mapping is written on one
    line) and / or replace the prompt. base_mtime is the task file's mtime when you started editing: if the file
    changed since (an agent or an editor wrote it), nothing is written. A prompt that now names a task which
    (indirectly) depends on this one is refused, as in new_task."""
    project = load_project(pdir)
    t = project.task(tid)
    if base_mtime and base_mtime != iso(mtime(t.path)):
        raise AgentSysError(_("任务文件在你编辑期间已被修改，请载入新版本后再保存"), 409)
    old = read_text(t.path)
    try:
        fm, body = read_task_file(t.path)
    except ValueError as e:
        raise AgentSysError(
            _("{file} 的 frontmatter 无法解析：{error}", file=t.path.name, error=e), 422
        ) from None
    for key, value in (frontmatter or {}).items():
        if key == "depends_on" and value:
            value = dep_entries(project, tid, value, fm)
        if key == "checks" and value:
            try:
                parse_checks(value)
            except ValueError as e:
                raise AgentSysError(str(e)) from None
        if value is None or value == "" or value == {} or value == []:
            fm.pop(key, None)
        else:
            fm[key] = flow(value) if isinstance(value, dict) else value
    write_task_file(t.path, fm, body if prompt is None else prompt)
    if up := new_cycle(pdir, tid, cycle_tasks(project.edges)):
        write_text(t.path, old)
        raise AgentSysError(_("会形成循环依赖：「{up}」已经（间接）依赖这个任务", up=up.title), 409)
    return load_project(pdir).tasks[tid]


def join(pdir, tid):
    """纳入托管: 草稿 -> 待开始; progress 已完成 -> 已完成 (review: true -> 待审核);
    any other progress -> 待确认 (detail joined: confirm the terminal session has ended first).
    A draft without review or checks in its frontmatter gets its type's defaults, as a task made in the UI does:
    a draft an agent wrote (a plan's experiments) is held to the same acceptance checks."""
    project = load_project(pdir)
    check_not_archived(project)
    t = project.task(tid)
    if t.managed:
        raise AgentSysError(_("任务「{task}」已纳入托管", task=t.title), 409)
    p = t.progress
    if p is None:
        ty = next((ty for ty in project.types if ty.id == t.type), None)
        if ty and (("review" not in t.fm and ty.review) or ("checks" not in t.fm and ty.checks)):

            def defaults(fm):
                if "review" not in fm and ty.review:
                    fm["review"] = True
                if "checks" not in fm and ty.checks:
                    fm["checks"] = ty.checks

            try:
                update_frontmatter(t.path, defaults)
            except (
                AgentSysError
            ):  # a frontmatter that cannot be parsed: joined as it is (it shows a reminder)
                pass
        state = new_state("queued", created=t.created)
    elif p.done:
        status = "in_review" if t.status == "in_review" else "done"
        state = new_state(
            status,
            created=t.created,
            started=t.started,
            finished=t.finished,
            seen_at=now_iso() if status == "done" else "",
        )
    else:
        state = new_state("needs_you", {"kind": "joined"}, created=t.created, started=t.started)
    write_state(t.workspace, state)
    return load_project(pdir).tasks[tid]


def drafts_of(project, tid):
    """The drafts tid wrote: those whose parent it is, and those that depend on it directly (a plan's close task,
    which waits for the plan's subtasks without being one). Review edges do not count."""
    direct = {e.down for e in project.downstream(tid) if e.kind != "review"}
    return [
        t.id
        for t in project.tasks.values()
        if t.status == "draft" and (t.parent == tid or t.id in direct)
    ]


def join_children(pdir, tid):
    """全部纳入托管: join every draft tid wrote (drafts_of: the plan an agent wrote as drafts, approved at once).
    Returns the ids joined."""
    project = load_project(pdir)
    project.task(tid)
    drafts = drafts_of(project, tid)
    if not drafts:
        raise AgentSysError(_("没有待纳入托管的草稿（子任务或直接依赖它的任务）"), 409)
    for child in drafts:
        join(pdir, child)
    return drafts


def approve(pdir, tid):
    """审核通过: 待审核 -> 已完成, which lets downstream tasks start. An unmanaged task (progress 已完成,
    review: true) stays unmanaged: approved_at goes into its frontmatter, and if progress.md later reports a
    newer finish (you went on in the terminal) it is back in 待审核."""
    project = load_project(pdir)
    t = project.task(tid)
    if t.status != "in_review":
        raise AgentSysError(
            _("任务当前为「{status}」，无法执行「审核通过」", status=status_word(t.status)), 409
        )
    now = now_iso()
    if t.managed:
        write_state(
            t.workspace, {**t.state, "status": "done", "detail": {}, "since": now, "seen_at": now}
        )
    else:

        def stamp(fm):
            fm["approved_at"] = max(now, t.finished)

        update_frontmatter(t.path, stamp)
    return load_project(pdir).tasks[tid]


def cancel(pdir, tid):
    """取消: a managed task that has not finished -> 已取消. Its downstream tasks keep waiting."""
    t = load_project(pdir).task(tid)
    if not t.managed or t.status in ("done", "cancelled"):
        raise AgentSysError(
            _(
                "任务当前为「{status}」{unmanaged}，无法取消",
                status=status_word(t.status),
                unmanaged="" if t.managed else _("（未托管）"),
            ),
            409,
        )
    if t.status == "running":
        raise AgentSysError(_("任务「{task}」正在运行，请先停止再取消", task=t.title), 409)
    write_state(t.workspace, {**t.state, "status": "cancelled", "detail": {}, "since": now_iso()})
    return load_project(pdir).tasks[tid]


def mark_read(pdir, tid):
    """标为已查看: opening a finished task clears its unread mark."""
    t = load_project(pdir).task(tid)
    if t.unread:
        write_state(t.workspace, {**t.state, "seen_at": now_iso()})
    return t


def retarget_paths(text, pid, old, new):
    """Task paths and @ mentions in a prompt (tasks/<old>, workspaces/<old>, also under campaigns/<pid>/, @<old>) pointed
    at new."""

    def sub(m):
        if (m[1] and m[1] != pid) or f"{m[2]}/{m[3]}" != old:
            return m[0]
        return m[0][: m.start(2) - m.start()] + new

    return TASK_REF.sub(sub, text)


def _rewrite_refs(project, old, new, moved, backup):
    """Point every task at new instead of old: depends_on / parent in the frontmatter and the task paths in the
    prompt. moved: where the renamed task's own file is now. backup gets {path: text before} of each file written.
    A file whose frontmatter cannot be parsed is left as it is (it shows a record reminder already)."""

    def retarget(entry):
        if dep_task(entry) != old:
            return entry
        if not isinstance(entry, dict):
            return new
        entry["task"] = new
        return entry

    for t in project.tasks.values():
        path = moved if t.id == old else t.path
        text = read_text(path)
        try:
            fm, body = split_frontmatter(text)
        except ValueError:
            continue
        new_body = retarget_paths(body, project.id, old, new)
        if old in [dep_task(e) for e in depends_on(fm)]:
            edit_deps(fm, retarget)
        elif fm.get("parent") != old and new_body == body:
            continue
        if fm.get("parent") == old:
            fm["parent"] = new
        backup[path] = text
        write_task_file(path, fm, new_body)


def rename(pdir, tid, new_type, new_name):
    """改名 / 改类型 (also 拖到另一条泳道): moves the prompt and the workspace (with .agent_sys/: state, runs, session) and
    points the other tasks at the new id, in their frontmatter and in the task paths of their prompts, so that every
    dependency stays. A running task has to stop first: its agent works in the workspace.
    The session goes on: its cwd is the project directory; the next message to it says where the task went."""
    project = load_project(pdir)
    t = project.task(tid)
    if t.status == "running":
        raise AgentSysError(_("任务「{task}」正在运行，请先停止再改名或改类型", task=t.title), 409)
    if t.detail.get("kind") == "joined":  # its terminal session may still work in the workspace
        raise AgentSysError(
            _(
                "任务「{task}」刚纳入托管，可能仍在终端中运行：请先确认终端会话已结束并回复它，再改名或改类型",
                task=t.title,
            ),
            409,
        )
    check_name(new_name, _("任务名"))
    if new_type not in {ty.id for ty in project.types}:
        raise AgentSysError(_("类型「{type}」不存在", type=new_type), 404)
    new_id = f"{new_type}/{new_name}"
    if new_id == tid:
        return t
    path, ws = (
        project.path / "tasks" / new_type / f"{new_name}.md",
        project.path / "workspaces" / new_type / new_name,
    )
    if path.exists() or ws.exists():
        raise AgentSysError(_("任务「{task}」已存在", task=new_id), 409)
    backup, moved = (
        {},
        [],
    )  # files rewritten in place: their text before; (from, to) of what was moved
    try:
        if t.workspace.exists():  # first: a workspace that cannot move leaves nothing half done
            ws.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(t.workspace, ws)
            moved.append((t.workspace, ws))
        shutil.move(t.path, path)
        moved.append((t.path, path))
        _rewrite_refs(project, tid, new_id, path, backup)
        # Other prompts may name the new path already, so check cycles on the scan that includes it (a cycle the
        # task was on before, under its old id, is not a new one).
        before = {new_id if x == tid else x for x in cycle_tasks(project.edges)}
        if new_cycle(pdir, new_id, before):
            raise AgentSysError(
                _(
                    "改为「{task}」会形成循环依赖：别的任务的 prompt 里已经写了这个路径",
                    task=new_id,
                ),
                409,
            )
    except BaseException:
        for f, text in backup.items():
            write_text(f, text)
        for src, dst in reversed(moved):
            shutil.move(dst, src)
        raise
    if t.managed and t.state.get("runs"):
        # the session knows the old paths: the next message to it says where the task is now (runs.start_run)
        state = {**t.state, "moved_from": t.state.get("moved_from") or tid}
        if state["moved_from"] == new_id:
            del state["moved_from"]
        write_state(ws, state)
    return load_project(pdir).tasks[new_id]


def delete(pdir, tid):
    """删除: the prompt and the workspace move to .trash/<time>-<type>-<name>/{tasks,workspaces}/…; moving them
    back restores the task as it was. Other tasks are not touched: a depends_on / parent naming the deleted task
    makes no edge and shows as a record reminder. Returns the trash directory."""
    project = load_project(pdir)
    t = project.task(tid)
    if t.managed and t.status == "running":
        raise AgentSysError(_("任务「{task}」正在运行，请先停止再删除", task=t.title), 409)
    trash = project.path / ".trash" / f"{datetime.now():%Y%m%d-%H%M%S}-{t.type}-{t.name}"
    while trash.exists():
        trash = trash.with_name(trash.name + "_")
    (trash / "tasks" / t.type).mkdir(parents=True)
    shutil.move(t.path, trash / "tasks" / t.type / t.path.name)
    if t.workspace.exists():
        (trash / "workspaces" / t.type).mkdir(parents=True)
        shutil.move(t.workspace, trash / "workspaces" / t.type / t.name)
    return trash


# ---------------------------------------------------------------- dependencies (stored in the downstream's frontmatter)


def set_archived(pdir, archived):
    """归档 / 取消归档: archived_at in project.yaml. Archiving is refused while managed tasks are running (including
    waiting in the background and terminal takeovers): what they ask afterwards would not show on the home page."""
    project = load_project(pdir)
    if archived == project.archived:
        return project
    if archived and (n := project.running()):
        raise AgentSysError(
            _(
                "项目「{project}」中还有 {n} 个运行中的任务（含后台等待和终端接管），请先停止或等待其结束后再归档",
                project=project.title,
                n=n,
            ),
            409,
        )

    def change(config):
        if archived:
            config["archived_at"] = now_iso()
        else:
            config.pop("archived_at", None)

    edit_yaml(project.path / "project.yaml", change)
    return load_project(pdir)


def settings(home, pid=None):
    """agent_sys.yaml and project.yaml with defaults filled in. Without a project, the
    defaults of every project are listed too (the global settings page shows them)."""

    def project_values(pdir):
        config = plain(load_project(pdir).config)
        return {
            **PROJECT_DEFAULTS,
            **config,
            "title": config.get("title") or pdir.name,
            "agent": {**PROJECT_DEFAULTS["agent"], **(config.get("agent") or {})},
        }

    out = {"global": {"path": "agent_sys.yaml", "values": load_config(home)}, "project": None}
    if pid:
        out["project"] = {
            "path": f"campaigns/{pid}/project.yaml",
            "values": project_values(project_dir(home, pid)),
        }
    else:
        out["projects"] = []
        for p in project_ids(home):
            try:
                values = project_values(project_dir(home, p))
            except AgentSysError:
                continue
            out["projects"].append(
                {
                    "id": p,
                    "path": f"campaigns/{p}/project.yaml",
                    "values": values,
                    "archived": bool(values.get("archived_at")),
                }
            )
    return out


def save_settings(home, scope, values, pid=None):
    """Write values into agent_sys.yaml (scope global) or a project's project.yaml (scope project), keeping the
    comments; a mapping value (agent) is merged key by key."""
    known = GLOBAL_DEFAULTS if scope == "global" else PROJECT_SETTINGS
    if scope not in ("global", "project") or (scope == "project" and not pid):
        raise AgentSysError(_("scope 应为 global，或 project 并给出 project"))
    for key, value in values.items():
        if key not in known:
            raise AgentSysError(_("不认识的设置「{key}」", key=key))
        if key == "max_running" and not (isinstance(value, int) and value >= 1):
            raise AgentSysError(_("并发上限应为正整数"))
        if key in ("language", "protocol_language") and value not in LANGUAGES:
            raise AgentSysError(_("语言应为 {languages}", languages=" / ".join(LANGUAGES)))
        if key == "machines" and not isinstance(value, list):
            raise AgentSysError(_("machines 应为机器的列表"))
        if key == "ledger" and not isinstance(value, dict):
            raise AgentSysError(_("ledger 应为键值表（primary、higher_is_better、baseline）"))
    path = (
        Path(home) / "agent_sys.yaml"
        if scope == "global"
        else project_dir(home, pid) / "project.yaml"
    )
    if scope == "project" and not path.is_file():
        raise AgentSysError(_("项目「{project}」不存在", project=pid), 404)

    def change(config):
        for key, value in values.items():
            if isinstance(value, dict) and isinstance(config.get(key), dict):
                config[key].update(value)  # in place: the comments inside agent: stay
            else:
                config[key] = value

    edit_yaml(path, change)
    return settings(home, pid if scope == "project" else None)[scope]


def init_home(home):
    """Make what a AGENT_SYS_HOME lacks: campaigns/, rules/, knowledge/entries/ (the server writes its
    INDEX.md) and agent_sys.yaml with the defaults written out. Nothing that exists is touched. Returns what was made."""
    home, made = Path(home), []
    for sub in ("campaigns", "rules", "knowledge/entries"):
        if not (home / sub).is_dir():
            (home / sub).mkdir(parents=True)
            made.append(f"{sub}/")
    if not (home / "agent_sys.yaml").exists():
        write_text(home / "agent_sys.yaml", AGENT_SYS_YAML)
        made.append("agent_sys.yaml")
    return made


def template_tasks(src):
    """The tasks a template brings, tasks/<type>/<name>.md, as [type/name]."""
    return sorted(
        f"{f.parent.name}/{f.stem}"
        for f in (Path(src) / "tasks").glob("*/*.md")
        if not f.name.startswith(".") and NAME.fullmatch(f.parent.name) and NAME.fullmatch(f.stem)
    )


def list_templates(templates_dir=TEMPLATES):
    """[{id, title, description, types: [{id, review, skeleton}], tasks: [type/name]}] for templates/<id>/
    (templates/_common is shared, not a template)."""
    out = []
    for d in sorted(Path(templates_dir).iterdir()):
        if d.is_dir() and not d.name.startswith(("_", ".")):
            config = load_yaml(read_text(d / "project.yaml"))
            types = []
            for f in sorted((d / "task_types").glob("*.md")):
                fm, skeleton = read_task_file(f)
                types.append(
                    {"id": f.stem, "review": fm.get("review") is True, "skeleton": skeleton}
                )
            out.append(
                {
                    "id": d.name,
                    "title": config.get("title") or d.name,
                    "description": str(config.get("description") or ""),
                    "types": types,
                    "tasks": template_tasks(d),
                }
            )
    return out


def new_project(home, name, template="blank", title=None, templates_dir=TEMPLATES):
    """New project = copy the template's rules/, knowledge/, task_types/, skills/ + project.yaml with the
    title filled in + tasks/<type>/ for every type. The tasks the template brings (tasks/<type>/<name>.md, e.g. the
    first round of a campaign) are copied too and queued, each with a state.json of its own: their depends_on keeps
    all but the first waiting. The agent_sys protocol is not copied: M2 injects it from the code at every run, so
    that it follows agent_sys's version.
    Built in a hidden directory and renamed into place, so a failure leaves nothing half made."""
    check_name(name, _("项目名"))
    templates_dir = Path(templates_dir)
    src = templates_dir / template
    if template.startswith(("_", ".")) or not src.is_dir():
        raise AgentSysError(_("模板「{template}」不存在", template=template), 404)
    dst = Path(home) / "campaigns" / name
    if dst.exists():
        raise AgentSysError(_("项目「{project}」已存在", project=name), 409)
    tmp = dst.with_name(f".{name}.new")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    for sub in ("rules", "knowledge", "task_types", "skills"):
        if (src / sub).is_dir():
            shutil.copytree(src / sub, tmp / sub)
    (tmp / "rules").mkdir(exist_ok=True)
    (tmp / "knowledge" / "entries").mkdir(parents=True, exist_ok=True)
    if not (tmp / "knowledge" / "INDEX.md").exists():
        write_text(tmp / "knowledge" / "INDEX.md", "")
    config = load_yaml(read_text(src / "project.yaml"))
    config.pop("description", None)  # describes the template, not the project
    if "title" in config:
        config["title"] = title or name
    else:
        config.insert(0, "title", title or name)
    if "template" not in config:
        config.insert(1, "template", template)
    write_text(tmp / "project.yaml", dump_yaml(config))
    for skeleton in (tmp / "task_types").glob("*.md"):
        (tmp / "tasks" / skeleton.stem).mkdir(parents=True)
    (tmp / "workspaces").mkdir()
    for tid in template_tasks(src):
        (tmp / "tasks" / tid).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src / "tasks" / f"{tid}.md", tmp / "tasks" / f"{tid}.md")
        write_state(tmp / "workspaces" / tid, new_state("queued"))
    tmp.rename(dst)
    return dst
