"""launch.py: what a run is given (system.md, the message, the claude options) and what launch.md records."""

import ast
import inspect
import json
import re

from conftest import DONE

from agent_sys_v2 import launch, questions, runs, server, skills
from agent_sys_v2.ops import new_task
from agent_sys_v2.scan import load_project


def project_with_rules(home, make_project):
    pdir = make_project(
        {
            "develop/a": "---\ntitle: 上游 A\n---\n做 A\n",
            "develop/b": "---\ntitle: 任务 B\n---\n## 任务描述\n\n- 按 workspaces/develop/a/report.md 做 B\n",
        },
        progress={"develop/a": DONE},
    )
    (pdir / "workspaces/develop/a/report.md").write_text("## 结论\n", encoding="utf-8")
    for d, files in (
        (home / "rules", {"b.md": "全局 b", "a.md": "全局 a，参考 clusters 中的集群说明"}),
        (pdir / "rules", {"z.md": "项目 z", "project.md": "项目背景"}),
    ):
        d.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (d / name).write_text(text, encoding="utf-8")
    (pdir / "knowledge").mkdir()
    (pdir / "knowledge/INDEX.md").write_text("- p1 — 项目经验\n", encoding="utf-8")
    (home / "knowledge").mkdir()
    (home / "knowledge/INDEX.md").write_text("", encoding="utf-8")  # empty: left out
    return pdir


def test_system_md_parts_in_order_with_sources(tmp_path, make_project):
    home = tmp_path / "home"
    pdir = project_with_rules(home, make_project)
    project = load_project(pdir)
    parts, text = launch.system(home, project, project.tasks["develop/b"])
    assert [(p["title"], p["path"]) for p in parts] == [
        ("agent_sys 约定", str(launch.PROTOCOLS["zh"])),
        ("全局规则", str(home / "rules/a.md")),
        ("全局规则", str(home / "rules/b.md")),
        ("项目规则", str(pdir / "rules/project.md")),
        ("项目规则", str(pdir / "rules/z.md")),
        ("skills 索引", str(skills.SHIPPED)),
        ("项目经验索引", str(pdir / "knowledge/INDEX.md")),
    ]
    head = text.split("---")[0]
    assert (
        f"`{pdir}`" in head
        and f"AGENT_SYS_HOME：`{home}`" in head
        and "`workspaces/develop/b/`" in head
    )
    # each part says where it came from, in order
    positions = [text.index(f"<!-- {p['title']}：{p['path']} -->") for p in parts]
    assert positions == sorted(positions)
    assert "# 任务约定" in text and "全局 a，参考 clusters" in text and "- p1 — 项目经验" in text


def test_first_and_resume_messages(tmp_path, make_project):
    home = tmp_path / "home"
    pdir = project_with_rules(home, make_project)
    project = load_project(pdir)
    b = project.tasks["develop/b"]
    msg = launch.first_message(project, b)
    assert msg.startswith("请完成任务 `tasks/develop/b.md`，工作区是 `workspaces/develop/b/`。")
    assert (
        "- 上游 A（`develop/a`，产出依赖）：工作区 `workspaces/develop/a/`，报告 `workspaces/develop/a/report.md`；"
        "progress 的下一步：无"
    ) in msg
    assert msg.endswith(
        "任务说明（`tasks/develop/b.md` 的正文）：\n\n## 任务描述\n\n- 按 workspaces/develop/a/report.md 做 B"
    )
    # a task joined with work already done: read progress.md first, then your reply
    joined = launch.first_message(project, project.tasks["develop/a"], "接着做")
    assert "还没有" not in joined and joined.endswith(
        "请先读 `workspaces/develop/a/progress.md`，接着做。\n\n我的回复：接着做"
    )
    assert launch.resume_message("继续") == "继续"
    assert (
        launch.resume_message("继续", moved=("develop/a", "bug_fix/a"))
        == "（这个任务已从 `tasks/develop/a.md` 移到 `tasks/bug_fix/a.md`，工作区现在是 `workspaces/bug_fix/a/`，旧路径已失效。）\n\n继续"
    )
    r = launch.resume_message("继续", "/x/system.md", wait_interrupted=True)
    assert (
        r.startswith("（agent_sys 规则有更新，请先读 `/x/system.md`")
        and "重新起后台等待" in r
        and r.endswith("\n\n继续")
    )


def test_protocol_language_en(tmp_path, make_project):
    """agent_sys.yaml protocol_language: en gives the English protocol, and agent_sys's own words to the agent in English;
    the parts keep their Chinese titles (launch.md and the rules page translate them for people)."""
    home = tmp_path / "home"
    pdir = project_with_rules(home, make_project)
    project = load_project(pdir)
    b = project.tasks["develop/b"]
    assert launch.agent_language(home) == "zh" and launch.protocol(home) == launch.PROTOCOLS["zh"]
    (home / "agent_sys.yaml").write_text("protocol_language: en\n", encoding="utf-8")
    parts, text = launch.system(home, project, b)
    assert parts[0] == {
        "title": "agent_sys 约定",
        "path": str(launch.PROTOCOLS["en"]),
        "text": launch.PROTOCOLS["en"].read_text(encoding="utf-8").strip(),
    }
    assert (
        text.startswith("# This project's working conventions")
        and f"AGENT_SYS_HOME: `{home}`" in text
    )
    assert f"<!-- agent_sys conventions: {launch.PROTOCOLS['en']} -->\n# Task conventions" in text
    assert (
        f"<!-- Project knowledge index: {pdir / 'knowledge/INDEX.md'} -->\nProject knowledge index: one lesson"
        in text
    )
    msg = launch.first_message(project, b, lang="en")
    assert msg.startswith(
        "Please do the task `tasks/develop/b.md`; its workspace is `workspaces/develop/b/`."
    )
    assert (
        "- 上游 A (`develop/a`, output dependency): workspace `workspaces/develop/a/`, report "
        "`workspaces/develop/a/report.md`; Next in its progress: 无"
    ) in msg
    assert msg.endswith(
        "The task (the body of `tasks/develop/b.md`):\n\n## 任务描述\n\n- 按 workspaces/develop/a/report.md 做 B"
    )
    r = launch.resume_message("go on", "/x/system.md", lang="en")
    assert (
        r
        == "(The agent_sys rules have changed: read `/x/system.md` first, then follow the new rules.)\n\ngo on"
    )
    assert launch.resume_message("go on", moved=("develop/a", "bug_fix/a"), lang="en").startswith(
        "(This task has moved from `tasks/develop/a.md` to `tasks/bug_fix/a.md`; its workspace is now `workspaces/bug_fix/a/`"
    )
    (home / "agent_sys.yaml").write_text(
        "protocol_language: fr\n", encoding="utf-8"
    )  # unknown: Chinese
    assert launch.agent_language(home) == "zh"


def test_agent_texts_have_english_with_the_same_placeholders():
    used = set()
    for mod in (launch, questions, runs, server):
        for n in ast.walk(ast.parse(inspect.getsource(mod))):
            f = getattr(n, "func", None)
            if getattr(f, "id", getattr(f, "attr", "")) == "say" and isinstance(
                n.args[1], ast.Constant
            ):
                used.add(n.args[1].value)
    assert len(used) > 20
    used |= set(launch.KIND_ZH.values()) | {
        "agent_sys 约定",
        "全局规则",
        "项目规则",
        "项目经验索引",
        "全局经验索引",
    }
    assert used - set(launch.AGENT_EN) == set()
    for zh, en in launch.AGENT_EN.items():
        assert sorted(re.findall(r"\{\w+\}", zh)) == sorted(re.findall(r"\{\w+\}", en)), zh


def test_options_command_and_launch_md(tmp_path, make_project):
    home = tmp_path / "home"
    pdir = project_with_rules(home, make_project)
    (pdir / "project.yaml").write_text(
        "title: t\nagent: {model: claude-haiku-4-5, effort: low, budget_usd: 2, "
        'extra_args: ["--strict-mcp-config", "--max-turns 5"]}\n',
        encoding="utf-8",
    )
    new_task(pdir, "develop", "c", "做 C", queue=True)
    project = load_project(pdir)
    c = project.tasks["develop/c"]
    parts, text = launch.system(home, project, c)
    run_dir = c.workspace / ".agent_sys/runs/20261023-100000"
    cfg = launch.prepare(
        home,
        project,
        c,
        run_dir=run_dir,
        kind="sdk",
        parts=parts,
        system_text=text,
        message=launch.first_message(project, c),
        resume=False,
        max_budget_usd=1.5,
        cost_start_usd=0.0,
        window="c",
        tmux="agent_sys",
        cli_path="/x/claude",
    )
    assert json.loads((run_dir / "launch.json").read_text()) == cfg
    assert (run_dir / "system.md").read_text() == text and (
        run_dir / "inbox.jsonl"
    ).read_text() == ""
    assert cfg["before"] == {"status": "queued", "detail": {}, "report": None, "report_mtime": ""}

    sid = c.state["session_id"]
    cmd = launch.sdk_command(cfg)
    assert (
        cmd[0] == "/x/claude" and f"--session-id={sid}" in cmd and "--resume" not in " ".join(cmd)
    )
    for flag, value in (
        ("--model", "claude-haiku-4-5"),
        ("--effort", "low"),
        ("--permission-mode", "bypassPermissions"),
        ("--max-budget-usd", "1.5"),
        ("--append-system-prompt-file", str(run_dir / "system.md")),
        ("--settings", launch.SETTINGS),
        ("--max-turns", "5"),
    ):
        assert cmd[cmd.index(flag) + 1] == value
    assert "--setting-sources=user" in cmd and "--strict-mcp-config" in cmd
    env = launch.sdk_options(cfg).env
    assert (
        launch.ENV.items() <= env.items()
    )  # the worker's environment, and where it is: task, workspace, skills
    assert env["AGENT_SYS_TASK"] == "develop/c" and env["AGENT_SYS_WORKSPACE"].endswith(
        "workspaces/develop/c"
    )
    assert (
        env["AGENT_SYS_SKILLS"].endswith("agent_sys_v2/skills") and env["AGENT_SYS_MACHINES"] == ""
    )

    term = launch.terminal_argv(
        cfg
    )  # a new session: --session-id and the first message as the prompt
    assert term[:3] == ["/x/claude", "--session-id", sid] and term[-1] == cfg["message"]
    assert (
        "--max-budget-usd" not in term
        and term[term.index("--permission-mode") + 1] == "bypassPermissions"
    )
    assert launch.terminal_argv({**cfg, "resume": True})[1:3] == ["--resume", sid]

    md = (run_dir / "launch.md").read_text()
    for part in parts:
        assert f"`{part['path']}`" in md
    assert (
        "### 项目经验索引（1 行）" in md and "## 首条消息\n\n请完成任务 `tasks/develop/c.md`" in md
    )
    assert (
        "## 等价命令" in md
        and f"--session-id={sid}" in md
        and "CLAUDE_CODE_DISABLE_AUTO_MEMORY=1" in md
    )


def test_parse_extra_args():
    assert launch.parse_extra_args(
        ["--strict-mcp-config", "--max-turns 5", "--add-dir=/x", "fallback-model", "sonnet"]
    ) == {"strict-mcp-config": None, "max-turns": "5", "add-dir": "/x", "fallback-model": "sonnet"}
    assert launch.parse_extra_args([]) == {}


def test_xhigh_and_ultracode(tmp_path, make_project):
    """effort xhigh goes to --effort as it is; ultracode (Claude Code's multi-agent workflows) goes into --settings,
    for the runner and for a terminal takeover alike."""
    home = tmp_path / "home"
    pdir = project_with_rules(home, make_project)
    (pdir / "project.yaml").write_text(
        "title: t\nagent: {model: opus, effort: xhigh, ultracode: true}\n", encoding="utf-8"
    )
    new_task(pdir, "develop", "c", "做 C", queue=True)
    new_task(pdir, "develop", "d", "做 D", queue=True, agent={"effort": "max", "ultracode": False})
    project = load_project(pdir)
    parts, text = launch.system(home, project, project.tasks["develop/c"])

    def cfg_of(tid):
        t = project.tasks[tid]
        return launch.prepare(
            home,
            project,
            t,
            run_dir=t.workspace / ".agent_sys/runs/20261023-100000",
            kind="sdk",
            parts=parts,
            system_text=text,
            message="m",
            resume=False,
            max_budget_usd=1,
            cost_start_usd=0.0,
            window="c",
            tmux="agent_sys",
            cli_path="/x/claude",
        )

    c, d = cfg_of("develop/c"), cfg_of("develop/d")
    assert (c["effort"], c["ultracode"], d["effort"], d["ultracode"]) == (
        "xhigh",
        True,
        "max",
        False,
    )
    cmd = launch.sdk_command(c)
    assert cmd[cmd.index("--effort") + 1] == "xhigh"
    assert json.loads(cmd[cmd.index("--settings") + 1]) == {
        "cleanupPeriodDays": 365,
        "ultracode": True,
    }
    term = launch.terminal_argv(c)
    assert json.loads(term[term.index("--settings") + 1])["ultracode"] is True
    cmd = launch.sdk_command(d)
    assert (
        cmd[cmd.index("--settings") + 1] == launch.SETTINGS
        and cmd[cmd.index("--effort") + 1] == "max"
    )
    md = (
        project.tasks["develop/c"].workspace / ".agent_sys/runs/20261023-100000/launch.md"
    ).read_text()
    assert "effort：xhigh + ultracode" in md


def test_first_message_has_the_upstream_handoff_and_the_machines(
    tmp_path, make_project, monkeypatch
):
    home = tmp_path / "home"
    pdir = project_with_rules(home, make_project)
    items = "".join(f"- `log/{i}.log` — 第 {i} 份日志\n" for i in range(launch.HANDOFF_LINES + 2))
    (pdir / "workspaces/develop/a/report.md").write_text(
        f"## 结论\n\n好\n\n## 交接清单\n\n{items}\n## 其他\n- 不算\n", encoding="utf-8"
    )
    project = load_project(pdir)
    msg = launch.first_message(project, project.tasks["develop/b"])
    assert (
        "；progress 的下一步：无\n  交接清单：\n  - `log/0.log` — 第 0 份日志\n  - `log/1.log`"
        in msg
    )
    assert (
        f"  - `log/{launch.HANDOFF_LINES - 1}.log`" in msg
        and f"`log/{launch.HANDOFF_LINES}.log`" not in msg
    )
    assert (
        f"  - ……（共 {launch.HANDOFF_LINES + 2} 条，其余见报告）\n\n" in msg and "不算" not in msg
    )
    en = launch.first_message(project, project.tasks["develop/b"], lang="en")
    assert "\n  Handoff:\n  - `log/0.log`" in en and "(17 in all; the rest are in the report)" in en
    assert "机器" not in msg
    # a task with machines is told which ones it may use (with their GPUs, as project.yaml lists them)
    with open(pdir / "project.yaml", "a", encoding="utf-8") as f:
        f.write("machines:\n  - {name: n1, gpus: 8}\n  - n2\n")
    text = (pdir / "tasks/develop/b.md").read_text(encoding="utf-8")
    (pdir / "tasks/develop/b.md").write_text(
        "---\nmachines: [n1, n2]\n---\n" + text, encoding="utf-8"
    )
    project = load_project(pdir)
    msg = launch.first_message(project, project.tasks["develop/b"])
    assert "本任务可以使用的机器：n1（8 GPU）, n2。" in msg
    en = launch.first_message(project, project.tasks["develop/b"], lang="en")
    assert "The machines this task may use: n1 (8 GPUs), n2." in en
