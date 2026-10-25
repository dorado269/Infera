"""Machines (design §3.2): the nodes a campaign may use, which task holds which, and who holds one idle.

  project.yaml   machines: [{name: smc-a16-19, gpus: 8, note: MI300X}, smc-a16-20]
  task           machines: [smc-a16-19]

A managed task holds the machines in its frontmatter from its first run until it is in review, done or cancelled:
while it runs, and while it waits for you, since its deployment is usually still up. The scheduler starts no task
whose machines another task holds (sched.plan), and 开始 by hand is refused (check_free). Machines are named by host
name, so the rule holds across campaigns. A holder that has waited for you longer than IDLE_MINUTES is shown as
holding its machines idle: the node is up and nobody is using it.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .i18n import _
from .model import AgentSysError, plain

HOLDING = ("running", "needs_you")
IDLE_MINUTES = 30


def declared(config):
    """project.yaml machines -> [{name, gpus, note}], names unique, in the order written."""
    items = plain(config.get("machines")) or []
    out, seen = [], set()
    for item in items if isinstance(items, list) else []:
        m = {"name": item} if isinstance(item, str) else item if isinstance(item, dict) else {}
        name = str(m.get("name") or "").strip()
        if name and name not in seen:
            seen.add(name)
            gpus = m.get("gpus")
            out.append(
                {
                    "name": name,
                    "gpus": gpus if isinstance(gpus, int) and not isinstance(gpus, bool) else None,
                    "note": str(m.get("note") or ""),
                }
            )
    return out


def task_machines(fm):
    """A task's frontmatter machines -> [name]: a list, or one name."""
    value = plain(fm.get("machines"))
    items = value if isinstance(value, list) else [value] if value else []
    return list(dict.fromkeys(str(x).strip() for x in items if str(x or "").strip()))


def holds(t):
    return t.managed and t.status in HOLDING and bool(t.machines)


def holders(projects):
    """{machine name: (project, task)} over the tasks that hold machines."""
    out = {}
    for p in projects:
        for t in p.tasks.values():
            if holds(t):
                for name in t.machines:
                    out.setdefault(name, (p, t))
    return out


def minutes_since(iso_time, now=None):
    try:
        then = datetime.strptime(iso_time or "", "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return 0
    return max(0, int(((now or datetime.now(timezone.utc)) - then).total_seconds() // 60))


def holder_json(p, t, now=None):
    waited = minutes_since(t.since, now) if t.status == "needs_you" else 0
    return {
        "project": p.id,
        "task": t.id,
        "title": t.title,
        "status": t.status,
        "detail": t.detail,
        "since": t.since,
        "idle_minutes": waited if waited >= IDLE_MINUTES else 0,
    }


def table(projects, only=None, now=None):
    """The machines as the board (one project: only) or the home page (all projects) shows them:
    [{name, gpus, note, projects, holder}], holder None when free. A held machine nobody declared is listed too."""
    held = holders(projects)
    rows = {}
    for p in projects:
        if only is not None and p.id != only.id:
            continue
        for m in declared(p.config):
            row = rows.setdefault(m["name"], {**m, "projects": []})
            row["projects"].append(p.id)
    for name, (p, _t) in held.items():
        if only is None or p.id == only.id:
            rows.setdefault(name, {"name": name, "gpus": None, "note": "", "projects": [p.id]})
    for name, row in rows.items():
        row["holder"] = holder_json(*held[name], now) if name in held else None
    return list(rows.values())


def check_free(projects, project, t):
    """Refuse to start t while another task holds one of its machines (开始 by hand; the scheduler waits)."""
    held = holders(projects)
    busy = [(name, held[name]) for name in t.machines if name in held and held[name][1] is not t]
    busy = [(name, (p, h)) for name, (p, h) in busy if (p.id, h.id) != (project.id, t.id)]
    if busy:
        raise AgentSysError(
            _(
                "机器 {machines} 正被 {tasks} 占用：等它完成或取消，或修改本任务的机器",
                machines="、".join(name for name, _h in busy),
                tasks="、".join(sorted({f"{p.id}/{h.id}" for _n, (p, h) in busy})),
            ),
            409,
        )


def wait_detail(t, held):
    """The detail of a queued task whose machines are held: {kind: machines, machines: [{name, project, task, title}]}
    or None when they are all free."""
    busy = [
        {
            "name": name,
            "project": held[name][0].id,
            "task": held[name][1].id,
            "title": held[name][1].title,
        }
        for name in t.machines
        if name in held and held[name][1] is not t
    ]
    return {"kind": "machines", "machines": busy} if busy else None
