"""What one run of the agent is given, written into its run directory by the server:

  .agent_sys/runs/<run_id>/system.md     agent_sys protocol + global rules + project rules + skills index +
                                     knowledge indexes, appended to Claude Code's own system prompt
                                     (--append-system-prompt-file)
  .agent_sys/runs/<run_id>/launch.json   what the runner reads: claude options, the message to send, the cost baseline
  .agent_sys/runs/<run_id>/launch.md     for people: where each injected part came from and its size, the knowledge
                                     indexes, the whole system.md (folded), the message, the equivalent claude command
                                     and the environment
  .agent_sys/runs/<run_id>/inbox.jsonl   empty; the server appends your messages and stop commands to it

The same options make the interactive claude of a terminal takeover (terminal_argv).
"""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path

from . import checks, i18n, machines, skills
from .i18n import _
from .model import iso, load_config, mtime, now_iso, read_text, write_text
from .scan import report_files

# The agent_sys protocol in each language of agent_sys.yaml protocol_language (zh by default). The agent works in the
# language of the task's prompt whichever it gets (the protocol says so).
PROTOCOL_DIR = Path(__file__).resolve().parent / "templates" / "_common" / "rules"
PROTOCOLS = {"zh": PROTOCOL_DIR / "protocol.md", "en": PROTOCOL_DIR / "protocol.en.md"}
# The worker's environment on top of the server's cleaned one: background commands may run 24 h and are
# not reaped under memory pressure, the CLI reports session_state_changed (how a round's end is seen), and the
# worker does not write into the user's auto memory.
ENV = {
    "BASH_MAX_TIMEOUT_MS": "86400000",
    "CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP": "1",
    "CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
}
SETTINGS = '{"cleanupPeriodDays": 365}'  # keep session records for a year: a task may wait long before a resume


def settings_json(cfg):
    """--settings of a run: SETTINGS, plus ultracode (agent.ultracode: true). Ultracode is Claude Code's switch for
    multi-agent workflows (the Workflow tool on every substantial task, at a much higher cost); in a headless run it is
    turned on by this setting, with any effort."""
    return (
        json.dumps({**json.loads(SETTINGS), "ultracode": True})
        if cfg.get("ultracode")
        else SETTINGS
    )


KIND_ZH = {"input": "产出依赖", "code": "代码基线", "review": "评审覆盖", "subtask": "父任务"}
# What agent_sys tells the agent (system.md, the first message, messages into its session) in protocol_language: written
# in Chinese and looked up here for en, as i18n.EN does for what people see.
AGENT_EN = {
    "# 这个项目的工作约定\n\n下面是我给这个项目的所有任务定的约定和规则，每次运行都会附上。\n\n": "# This project's working conventions\n\nThe conventions and rules I set for every task of this project, "
    "attached to every run.\n\n",
    "- 项目目录（也是当前目录）：`{project}`\n": "- Project directory (also the current directory): `{project}`\n",
    "- AGENT_SYS_HOME：`{home}`（全局规则在其中的 `rules/`，全局经验库在 `knowledge/`）\n": "- AGENT_SYS_HOME: `{home}` (the global rules are in its `rules/`, the global knowledge library in `knowledge/`)\n",
    "- 本任务：prompt 是 `tasks/{task}.md`，工作区是 `workspaces/{task}/`\n": "- This task: the prompt is `tasks/{task}.md`, the workspace `workspaces/{task}/`\n",
    "- 规则里写的相对路径先按项目目录找；找不到时，按该规则文件所在的目录（下面每段开头注明了来源）理解。\n": "- A relative path in the rules is looked up from the project directory first; if it is not there, read it "
    "from the directory of the rule file it is in (each part below starts with its source).\n",
    "agent_sys 约定": "agent_sys conventions",
    "全局规则": "Global rules",
    "项目规则": "Project rules",
    "项目经验索引": "Project knowledge index",
    "skills 索引": "Skills index",
    "{title}：一行一个 skill（名字、用途、SKILL.md 的路径）；用得上时先读它的 SKILL.md，里面有步骤和脚本。\n\n": "{title}: one skill per line (name, what it is for, the path of its SKILL.md); when one is of use, read its "
    "SKILL.md first: it has the steps and the scripts.\n\n",
    "本任务可以使用的机器：{machines}。任务运行期间和等我回复期间，其他任务不会被安排到这些机器上；不要使用别的机器。": "The machines this task may use: {machines}. While it runs and while it waits for my reply, no other task is "
    "scheduled on them; do not use other machines.",
    "（{gpus} GPU）": " ({gpus} GPUs)",
    "全局经验索引": "Global knowledge index",
    "{title}：一行一条经验，正文在 `{dir}/entries/<name>.md`，用得上时再读。\n\n": "{title}: one lesson per line, the text in `{dir}/entries/<name>.md`; read it when it is of use.\n\n",
    "产出依赖": "output dependency",
    "代码基线": "code baseline",
    "评审覆盖": "review coverage",
    "父任务": "parent task",
    "报告 `workspaces/{task}/{report}`": "report `workspaces/{task}/{report}`",
    "还没有 report.md": "no report.md yet",
    "- {title}（`{task}`，{kind}）：工作区 `workspaces/{task}/`，{report}": "- {title} (`{task}`, {kind}): workspace `workspaces/{task}/`, {report}",
    "；progress 的下一步：{next}": "; Next in its progress: {next}",
    "交接清单：": "Handoff:",
    "……（共 {n} 条，其余见报告）": "... ({n} in all; the rest are in the report)",
    "请完成任务 `tasks/{task}.md`，工作区是 `workspaces/{task}/`。": "Please do the task `tasks/{task}.md`; its workspace is `workspaces/{task}/`.",
    "上游任务（需要时读它们的 report.md 和 progress.md）：": "Upstream tasks (read their report.md and progress.md when you need them):",
    "任务说明（`tasks/{task}.md` 的正文）：": "The task (the body of `tasks/{task}.md`):",
    "（空）": "(empty)",
    "工作区里已有进展，请先读 `workspaces/{task}/progress.md`，接着做。": "The workspace already has progress: read `workspaces/{task}/progress.md` first and carry on from there.",
    "我的回复：{reply}": "My reply: {reply}",
    "（agent_sys 规则有更新，请先读 `{path}`，之后按新规则做。）": "(The agent_sys rules have changed: read `{path}` first, then follow the new rules.)",
    "（上次运行被中断，后台等待的命令已经结束。请先检查在等的条件，需要的话重新起后台等待。）": "(The last run was interrupted and the background wait has ended. Check the condition you were waiting for "
    "first, and start the background wait again if needed.)",
    "（这个任务已从 `tasks/{old}.md` 移到 `tasks/{new}.md`，工作区现在是 `workspaces/{new}/`，旧路径已失效。）": "(This task has moved from `tasks/{old}.md` to `tasks/{new}.md`; its workspace is now `workspaces/{new}/`, and "
    "the old paths no longer hold.)",
    "继续": "Continue",
    "预算已追加 ${usd}，请继续。": "The budget has been raised by ${usd}; please go on.",
    "任务 prompt 已更新（`tasks/{task}.md`），请重新阅读并按新要求调整。": "The task prompt has changed (`tasks/{task}.md`): read it again and adjust to what it now asks.",
    "已在 {path} 中回答（已填写 {filled} / {total} 项），未填写的项请按你的建议处理。": "Answered in {path} ({filled} / {total} filled in); for the rows left empty, follow your suggestion.",
}


def agent_language(home):
    """agent_sys.yaml protocol_language: the language of the protocol and of what agent_sys tells the agent (zh or en)."""
    lang = load_config(home).get("protocol_language")
    return lang if lang in PROTOCOLS else "zh"


def say(lang, text, **params):
    """text (Chinese) in the agent's language, with {params} filled in."""
    return (AGENT_EN.get(text, text) if lang == "en" else text).format(**params)


def protocol(home):
    """The protocol file a run of this AGENT_SYS_HOME gets."""
    return PROTOCOLS[agent_language(home)]


def bundled_cli():
    """The claude executable that comes with the pinned claude-agent-sdk."""
    import claude_agent_sdk

    return str(Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude")


# The models offered where one is chosen (agent of a task, project defaults, the parse), [(id, label)]. An alias
# (opus, sonnet, haiku) is resolved by Claude Code: ANTHROPIC_DEFAULT_<X>_MODEL in ~/.claude/settings.json or the
# environment, else the CLI's own default for it; any other name can be typed in.
MODELS = [
    ("opus", "Opus"),
    ("sonnet", "Sonnet"),
    ("haiku", "Haiku"),
    ("claude-opus-5-5", "Opus 5.5"),
    ("claude-sonnet-5-5", "Sonnet 5.5"),
    ("claude-haiku-5-5", "Haiku 5.5"),
    ("claude-haiku-4-5", "Haiku 4.5"),
]


def user_settings():
    try:
        settings = json.loads(read_text(Path.home() / ".claude" / "settings.json") or "{}")
    except json.JSONDecodeError:
        return {}
    return settings if isinstance(settings, dict) else {}


def models():
    """MODELS as [{id, label, resolves}]: resolves is the model an alias stands for, or the name modelOverrides
    sends a model id to, as far as ~/.claude/settings.json and the environment say ('' when they do not)."""
    settings = user_settings()
    env = {**os.environ, **{k: str(v) for k, v in (settings.get("env") or {}).items()}}
    overrides = (
        settings.get("modelOverrides") if isinstance(settings.get("modelOverrides"), dict) else {}
    )
    out = []
    for mid, label in MODELS:
        resolves = env.get(f"ANTHROPIC_DEFAULT_{mid.upper()}_MODEL", "") if "-" not in mid else ""
        resolves = str(overrides.get(resolves or mid) or resolves)
        out.append({"id": mid, "label": label, "resolves": "" if resolves == mid else resolves})
    return out


# ---------------------------------------------------------------- system.md


def system_parts(home, project):
    """The parts of system.md in order, [{title, path, text}]; missing or empty files are left out.
    project None: only what every project gets (the global rules page)."""
    home = Path(home)
    files = [("agent_sys 约定", protocol(home))]
    files += [("全局规则", p) for p in sorted((home / "rules").glob("*.md"))]
    if project:
        files += [("项目规则", p) for p in sorted((project.path / "rules").glob("*.md"))]
        files.append(("skills 索引", None))
        files.append(("项目经验索引", project.path / "knowledge" / "INDEX.md"))
    files.append(("全局经验索引", home / "knowledge" / "INDEX.md"))
    parts = []
    for title, path in files:
        text = skills.index(project).strip() if path is None else read_text(path).strip()
        if text:
            parts.append({"title": title, "path": str(path or skills.SHIPPED), "text": text})
    return parts


def system_md(home, project, task, parts, lang="zh"):
    """system.md: a head that says where things are (rules written with relative paths do not hold for a worker
    whose cwd is the project root), then each part with its source; agent_sys's own words in lang."""
    head = (
        say(
            lang,
            "# 这个项目的工作约定\n\n下面是我给这个项目的所有任务定的约定和规则，每次运行都会附上。\n\n",
        )
        + say(lang, "- 项目目录（也是当前目录）：`{project}`\n", project=project.path)
        + say(
            lang,
            "- AGENT_SYS_HOME：`{home}`（全局规则在其中的 `rules/`，全局经验库在 `knowledge/`）\n",
            home=home,
        )
        + say(
            lang,
            "- 本任务：prompt 是 `tasks/{task}.md`，工作区是 `workspaces/{task}/`\n",
            task=task.id,
        )
        + say(
            lang,
            "- 规则里写的相对路径先按项目目录找；找不到时，按该规则文件所在的目录（下面每段开头注明了来源）理解。\n",
        )
    )
    out = [head]
    colon = ": " if lang == "en" else "："
    for p in parts:
        title, intro = say(lang, p["title"]), ""
        if p["title"] == "skills 索引":
            intro = say(
                lang,
                "{title}：一行一个 skill（名字、用途、SKILL.md 的路径）；用得上时先读它的 SKILL.md，里面有步骤和脚本。\n\n",
                title=title,
            )
        if p["title"].endswith("经验索引"):
            intro = say(
                lang,
                "{title}：一行一条经验，正文在 `{dir}/entries/<name>.md`，用得上时再读。\n\n",
                title=title,
                dir=Path(p["path"]).parent,
            )
        out.append(f"<!-- {title}{colon}{p['path']} -->\n{intro}{p['text']}\n")
    return "\n---\n\n".join(out)


def system(home, project, task):
    """(parts, system.md text) for a run of task."""
    parts = system_parts(home, project)
    return parts, system_md(home, project, task, parts, agent_language(home))


def known_system_md(workspace):
    """The system.md the session last got to know, '' if none: Claude Code keeps the system prompt a session was
    created with, on every resume, so it is the one of the run that created the session, or of a later run
    whose message told the agent to read it."""
    runs = workspace / ".agent_sys" / "runs"
    for run in sorted(runs.glob("*/launch.json"), reverse=True):
        try:
            cfg = json.loads(read_text(run))
        except json.JSONDecodeError:
            continue
        if cfg.get("system_known", not cfg.get("resume")):
            return read_text(run.parent / "system.md")
    return ""


# ---------------------------------------------------------------- the message

HANDOFF_LINES = 15  # items of an upstream's handoff list in the first message


def upstream_lines(project, task, lang="zh"):
    """One line per upstream task: title, kind, workspace, report and the 下一步 of its progress; then
    the handoff list of its report, when it has one."""
    lines = []
    for e in project.upstream(task.id):
        up = project.tasks[e.up]
        reports = report_files(up.workspace)
        report = (
            say(lang, "报告 `workspaces/{task}/{report}`", task=up.id, report=reports[0])
            if reports
            else say(lang, "还没有 report.md")
        )
        line = say(
            lang,
            "- {title}（`{task}`，{kind}）：工作区 `workspaces/{task}/`，{report}",
            title=up.title,
            task=up.id,
            kind=say(lang, KIND_ZH.get(e.kind, e.kind)),
            report=report,
        )
        if up.progress and up.progress.next:
            line += say(lang, "；progress 的下一步：{next}", next=up.progress.next)
        lines.append(line)
        if (
            up.handoff
        ):  # the upstream's own list of what it leaves for you: read before its workspace
            lines.append("  " + say(lang, "交接清单："))
            lines += [f"  - {h['text']}" for h in up.handoff[:HANDOFF_LINES]]
            if len(up.handoff) > HANDOFF_LINES:
                lines.append("  - " + say(lang, "……（共 {n} 条，其余见报告）", n=len(up.handoff)))
    return lines


def first_message(project, task, reply="", lang="zh"):
    """The message a task's session starts with: which task, its upstream tasks, the prompt. A task joined with work
    already done (纳入托管) is told to read its progress.md first, with your reply."""
    out = [say(lang, "请完成任务 `tasks/{task}.md`，工作区是 `workspaces/{task}/`。", task=task.id)]
    ups = upstream_lines(project, task, lang)
    if ups:
        out.append(
            say(lang, "上游任务（需要时读它们的 report.md 和 progress.md）：")
            + "\n"
            + "\n".join(ups)
        )
    if task.machines:
        info = {m["name"]: m for m in machines.declared(project.config)}
        names = [
            name
            + (
                say(lang, "（{gpus} GPU）", gpus=info[name]["gpus"])
                if info.get(name, {}).get("gpus")
                else ""
            )
            for name in task.machines
        ]
        out.append(
            say(
                lang,
                "本任务可以使用的机器：{machines}。任务运行期间和等我回复期间，其他任务不会被安排到这些机器上；不要使用别的机器。",
                machines=", ".join(names),
            )
        )
    out.append(
        say(lang, "任务说明（`tasks/{task}.md` 的正文）：", task=task.id)
        + "\n\n"
        + (task.body.strip() or say(lang, "（空）"))
    )
    if task.progress is not None:
        out.append(
            say(
                lang,
                "工作区里已有进展，请先读 `workspaces/{task}/progress.md`，接着做。",
                task=task.id,
            )
        )
    if reply.strip():
        out.append(say(lang, "我的回复：{reply}", reply=reply.strip()))
    return "\n\n".join(out)


def resume_message(text, rules_updated="", wait_interrupted=False, moved=None, lang="zh"):
    """Your message to an existing session, after what agent_sys has to tell first. moved: (old id, new id) when the task
    was renamed or changed type since the session last ran."""
    out = []
    if moved:
        old, new = moved
        out.append(
            say(
                lang,
                "（这个任务已从 `tasks/{old}.md` 移到 `tasks/{new}.md`，工作区现在是 `workspaces/{new}/`，"
                "旧路径已失效。）",
                old=old,
                new=new,
            )
        )
    if rules_updated:
        out.append(
            say(
                lang,
                "（agent_sys 规则有更新，请先读 `{path}`，之后按新规则做。）",
                path=rules_updated,
            )
        )
    if wait_interrupted:
        out.append(
            say(
                lang,
                "（上次运行被中断，后台等待的命令已经结束。请先检查在等的条件，需要的话重新起后台等待。）",
            )
        )
    out.append(text.strip())
    return "\n\n".join(out)


# ---------------------------------------------------------------- claude options


def parse_extra_args(args):
    """The task's extra_args (each item as written: '--strict-mcp-config', '--max-turns 5', '--x=y') -> the SDK's
    {flag: value or None}."""
    tokens = [tok for a in args or [] for tok in shlex.split(str(a))]
    out, i = {}, 0
    while i < len(tokens):
        tok = tokens[i].lstrip("-")
        if "=" in tok:
            key, value = tok.split("=", 1)
            out[key] = value
        elif i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
            out[tok] = tokens[i + 1]
            i += 1
        else:
            out[tok] = None
        i += 1
    return out


def sdk_options(cfg, **extra):
    """ClaudeAgentOptions for a run from its launch.json (M0 report: the preset system prompt, bypassPermissions on
    every start since a resume does not keep it, user settings only, --session-id first and --resume after)."""
    from claude_agent_sdk import ClaudeAgentOptions

    sid = cfg["session_id"]
    return ClaudeAgentOptions(
        cli_path=cfg["cli_path"],
        model=cfg.get("model") or None,
        effort=cfg.get("effort") or None,
        cwd=cfg["cwd"],
        system_prompt={"type": "preset", "preset": "claude_code"},
        permission_mode="bypassPermissions",
        setting_sources=["user"],
        session_id=None if cfg["resume"] else sid,
        resume=sid if cfg["resume"] else None,
        max_budget_usd=cfg["max_budget_usd"],
        settings=settings_json(cfg),
        env=dict(cfg["env"]),
        extra_args={
            "append-system-prompt-file": cfg["system_md"],
            **parse_extra_args(cfg.get("extra_args")),
        },
        **extra,
    )


def sdk_command(cfg):
    """The claude command line the SDK runs for these options (the equivalent command in launch.md)."""
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    return SubprocessCLITransport(prompt="", options=sdk_options(cfg))._build_command()


def terminal_argv(cfg):
    """Interactive claude for a terminal takeover: the runner's permission mode, setting sources and system.md; an
    existing session is resumed, a new one starts with the first message. --max-budget-usd only works
    with --print, so the budget does not hold in the terminal."""
    sid = cfg["session_id"]
    argv = [cfg["cli_path"], *(["--resume", sid] if cfg["resume"] else ["--session-id", sid])]
    for flag, value in (("--model", cfg.get("model")), ("--effort", cfg.get("effort"))):
        if value:
            argv += [flag, str(value)]
    argv += [
        "--permission-mode",
        "bypassPermissions",
        "--setting-sources",
        "user",
        "--settings",
        settings_json(cfg),
        "--append-system-prompt-file",
        cfg["system_md"],
    ]
    for flag, value in parse_extra_args(cfg.get("extra_args")).items():
        argv += [f"--{flag}"] + ([value] if value is not None else [])
    if not cfg["resume"] and cfg.get("message"):
        argv.append(cfg["message"])
    return argv


def shell_command(cwd, env, argv):
    lines = [
        f"cd {shlex.quote(str(cwd))}",
        "env " + " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items()) + " \\",
    ]
    return "\n".join(lines) + "\n  " + shlex.join(argv)


# ---------------------------------------------------------------- the run directory


def kb(n):
    return f"{n / 1024:.1f} KB"


def launch_md(cfg, task, parts, system_text):
    """launch.md, for people (shown in the run's 启动信息): in the language of agent_sys.yaml; the system.md and the
    message it quotes are what the agent got."""
    kind = {
        "sdk": _("首次运行") if not cfg["resume"] else "resume",
        "replay": _("回放（不调用模型）"),
        "terminal": _("在终端中接管"),
    }[cfg["kind"]]
    size = kb(len(system_text.encode()))
    out = [
        _("# 运行 {id}", id=cfg["id"]),
        "",
        _("- 任务：`{task}`（{title}）", task=task.id, title=task.title),
        _(
            "- 类型：{kind}；会话 `{session}`（{flag}）",
            kind=kind,
            session=cfg["session_id"],
            flag="--resume" if cfg["resume"] else "--session-id",
        ),
        _("- 开始：{started}；cwd：`{cwd}`", started=cfg["started"], cwd=cfg["cwd"]),
        _(
            "- model：{model}；effort：{effort}；本次预算上限 ${budget}（任务预算减去已花费）",
            model=cfg.get("model") or _("默认"),
            effort=(cfg.get("effort") or _("默认"))
            + (" + ultracode" if cfg.get("ultracode") else ""),
            budget=f"{cfg['max_budget_usd']:.2f}",
        ),
        "",
        _("## 注入的 system.md（{size}）", size=size),
        "",
        _(
            "文件：`{path}`。Claude Code 在会话创建时保存 system prompt，resume 时沿用那一份；"
            "规则或经验索引更新后，resume 的消息开头会提示 agent 读新的 system.md。",
            path=cfg["system_md"],
        ),
        "",
        _("| # | 内容 | 来源 | 大小 |"),
        "|---|---|---|---|",
    ]
    out += [
        f"| {i} | {_(p['title'])} | `{p['path']}` | {kb(len(p['text'].encode()))} |"
        for i, p in enumerate(parts, 1)
    ]
    for p in parts:
        if p["title"].endswith("经验索引"):
            out += [
                "",
                _("### {title}（{n} 行）", title=_(p["title"]), n=len(p["text"].splitlines())),
                "",
                "```",
                p["text"],
                "```",
            ]
    fence = "`" * max(
        [3, *(len(m) + 1 for m in re.findall(r"`+", system_text))]
    )  # longer than any run inside
    out += [
        "",
        _("<details><summary>system.md 全文（规则和索引，{size}）</summary>", size=size),
        "",
        fence + "markdown",
        system_text.rstrip("\n"),
        fence,
        "",
        "</details>",
    ]
    if cfg["kind"] == "replay":
        r = cfg["replay"]
        gap = _("，间隔最长 {gap} 秒", gap=r["max_gap"]) if r.get("max_gap") else ""
        out += [
            "",
            _("## 回放"),
            "",
            _(
                "回放 `{events}`（速度 ×{speed}{gap}），不调用模型；发出的消息只记录，"
                "由回放给出一条说明作为回复。",
                events=r["events"],
                speed=r.get("speed") or 1,
                gap=gap,
            ),
        ]
    out += [
        "",
        _("## 本次发送的消息") if cfg["resume"] else _("## 首条消息"),
        "",
        cfg["message"] or _("（无）"),
    ]
    if cfg["kind"] == "terminal":
        out += [
            "",
            _("## 终端中的命令"),
            "",
            "```",
            shell_command(cfg["cwd"], cfg["env"], terminal_argv(cfg)),
            "```",
            "",
            _(
                "在 tmux 窗口 `{window}` 中运行：`tmux -L {tmux} attach -t agent_sys:{window}`。"
                "退出 claude 后，agent_sys 读回会话累计花费，并按 progress.md 判断任务状态。",
                window=cfg["window"],
                tmux=cfg["tmux"],
            ),
        ]
    else:
        how = (_("回放没有启动 claude；真实运行时，") if cfg["kind"] == "replay" else "") + _(
            "runner 用 Claude Agent SDK 以下面的参数启动 claude，消息从标准输入以 stream-json 发送："
        )
        out += [
            "",
            _("## 等价命令"),
            "",
            how,
            "",
            "```",
            shell_command(cfg["cwd"], cfg["env"], sdk_command(cfg)),
            "```",
        ]
    out += [
        "",
        _("## 环境变量"),
        "",
        _("在 agent_sys serve 清理过的环境（去掉 `CLAUDE*`、`ANTHROPIC*`、`IS_SANDBOX`）上加："),
        "",
    ]
    out += [f"- `{k}={v}`" for k, v in cfg["env"].items()]
    return "\n".join(out) + "\n"


def write_run(run_dir, cfg, task, parts, system_text):
    """Create the run directory: system.md, launch.json, launch.md and an empty inbox.jsonl."""
    run_dir.mkdir(parents=True)
    write_text(run_dir / "system.md", system_text)
    write_text(run_dir / "launch.json", json.dumps(cfg, ensure_ascii=False, indent=1) + "\n")
    write_text(run_dir / "launch.md", launch_md(cfg, task, parts, system_text))
    (run_dir / "inbox.jsonl").touch()


def session_exists(session_id):
    """Claude Code has a record of this session (~/.claude/projects/<cwd>/<sid>.jsonl): a first run that died before
    claude started left none, and must start the session again rather than resume it."""
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return any((config / "projects").glob(f"*/{session_id}.jsonl"))


def prepare(
    home,
    project,
    task,
    *,
    run_dir,
    kind,
    parts,
    system_text,
    message,
    resume,
    max_budget_usd,
    cost_start_usd,
    window,
    tmux,
    replay=None,
    cli_path=None,
    system_known=None,
):
    """Everything a run needs, written into run_dir; returns launch.json as a dict. parts / system_text from system(),
    message as it is to be sent (first_message / resume_message make it). system_known: this run's system.md reaches
    the agent (a new session; or a resume whose message says the rules changed)."""
    agent = task.agent
    report = next(iter(report_files(task.workspace)), None)
    cfg = {
        "id": run_dir.name,
        "kind": kind,
        "task": task.id,
        "project": project.id,
        "cwd": str(project.path),
        "session_id": task.state["session_id"],
        "resume": resume,
        "model": agent.get("model") or "",
        "effort": agent.get("effort") or "",
        "ultracode": agent.get("ultracode") is True,
        "max_budget_usd": round(max_budget_usd, 4),
        "extra_args": list(agent.get("extra_args") or []),
        "cli_path": cli_path or bundled_cli(),
        "system_md": str(run_dir / "system.md"),
        "env": {
            **ENV,
            **checks.env(project.path, task.id, task.workspace, task.machines, skills.SHIPPED),
        },
        "checks": task.checks,
        "check_retries": checks.retries(task.fm),
        "message": message,
        "window": window,
        "tmux": tmux,
        "started": now_iso(),
        "cost_start_usd": cost_start_usd,
        "replay": replay,
        "language": i18n.LANG,
        "agent_language": agent_language(home),
        "system_known": (not resume) if system_known is None else system_known,
        # what the task was before this run: a done task that only answered a question stays done
        "before": {
            "status": task.status,
            "detail": task.detail,
            "report": report,
            "report_mtime": iso(mtime(task.workspace / report)) if report else "",
        },
    }
    write_run(run_dir, cfg, task, parts, system_text)
    return cfg
