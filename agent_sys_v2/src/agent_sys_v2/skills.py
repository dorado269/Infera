"""Skills (design §5.7): procedures and scripts an agent reads when it needs them, offered through an index.

  agent_sys_v2/skills/<name>/SKILL.md          shipped with agent_sys; project.yaml `skills: [name, ...]` picks some
                                            (no `skills:` = all of them)
  campaigns/<c>/skills/<name>/SKILL.md      the campaign's own, always offered

SKILL.md starts with the frontmatter of a Claude Code skill (name, description). The index goes into system.md as one
line per skill, like a knowledge index: the agent opens SKILL.md when a task calls for it, so a skill costs a line of
context until it is used.
"""

from __future__ import annotations

from pathlib import Path

from .model import plain, read_task_file

SHIPPED = Path(__file__).resolve().parent / "skills"


def read_skill(path):
    """{name, description, path} of a SKILL.md; None if it has no description (not a skill yet)."""
    try:
        fm, _body = read_task_file(path)
    except ValueError:
        return None
    description = " ".join(str(fm.get("description") or "").split())
    if not description:
        return None
    return {
        "name": str(fm.get("name") or path.parent.name),
        "description": description,
        "path": str(path),
    }


def found(root):
    return [s for p in sorted(Path(root).glob("*/SKILL.md")) if (s := read_skill(p))]


def available(project):
    """The skills offered to the tasks of project: the shipped ones it picks, then its own."""
    wanted = plain(project.config.get("skills")) if project else None
    shipped = found(SHIPPED)
    if isinstance(wanted, list):
        names = [str(x) for x in wanted]
        shipped = sorted(
            (s for s in shipped if s["name"] in names), key=lambda s: names.index(s["name"])
        )
    own = found(project.path / "skills") if project else []
    return shipped + own


def index(project):
    """The index text: one line per skill."""
    return "\n".join(
        f"- {s['name']} — {s['description']} `{s['path']}`" for s in available(project)
    )
