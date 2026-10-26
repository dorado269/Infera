"""What a campaign that goes in rounds needs (the infera-perf template): tasks a template brings, the children
dependency (a round's close waits for its plan and all of the plan's subtasks), joining the drafts a plan wrote at
once, the field checks of intake / survey / plan / close, and the ledger's curves: round, series, references, the
frontier and the working point, and AgentX output turned into results."""

import json
import re
import shutil
from pathlib import Path

import pytest
from conftest import get, ok, run_home
from fastapi.testclient import TestClient

from agent_sys_v2 import launch, ledger, ops, perfcheck, sched, skills
from agent_sys_v2.model import AgentSysError, new_state, read_state, write_state
from agent_sys_v2.scan import load_project
from agent_sys_v2.server import create_app

FIXTURES = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parents[1]


def manage(pdir, *tids, status="queued"):
    for tid in tids:
        write_state(pdir / "workspaces" / tid, new_state(status))


def set_status(pdir, tid, status):
    ws = pdir / "workspaces" / tid
    write_state(ws, {**read_state(ws), "status": status})


# ---------------------------------------------------------------- the children dependency


ROUND = {
    "plan/r1": "---\ntitle: 计划\n---\n",
    "experiment/a": "---\ntitle: 实验 a\nparent: plan/r1\n---\n",
    "experiment/b": "---\ntitle: 实验 b\nparent: plan/r1\n---\n",
    "close/r1": "---\ndepends_on: [{task: plan/r1, kind: children}]\n---\n收尾\n",
}


def test_children_waits_for_the_upstream_and_every_subtask(make_project):
    pdir = make_project(ROUND)
    manage(pdir, *ROUND)
    p = load_project(pdir)
    edge = next(e for e in p.edges if e.down == "close/r1")
    assert (edge.up, edge.kind, edge.blocking) == ("plan/r1", "children", True)
    # the plan itself first: an upstream like any other
    assert p.tasks["close/r1"].detail == {"kind": "upstream", "tasks": ["plan/r1"]}
    set_status(pdir, "plan/r1", "done")
    p = load_project(pdir)
    assert p.tasks["close/r1"].detail == {
        "kind": "children",
        "parents": ["plan/r1"],
        "tasks": ["experiment/a", "experiment/b"],
    }
    assert not p.edge_satisfied(edge) and p.satisfied("plan/r1")
    # a cancelled subtask counts as finished: cancelling the rest is how a round ends early
    set_status(pdir, "experiment/a", "done")
    set_status(pdir, "experiment/b", "cancelled")
    p = load_project(pdir)
    assert p.tasks["close/r1"].detail == {} and p.edge_satisfied(p.upstream("close/r1")[0])
    # a subtask added later (a draft too) holds the close back again
    (pdir / "tasks/experiment/c.md").write_text("---\nparent: plan/r1\n---\n后加的\n")
    p = load_project(pdir)
    assert p.tasks["close/r1"].detail["tasks"] == ["experiment/c"]
    # the scheduler leaves a task waiting for subtasks alone
    assert sched.plan([p], 10) == []


def test_children_leaves_out_the_waiting_task_and_its_downstream(make_project):
    pdir = make_project(
        {
            "plan/r1": "",
            "experiment/a": "---\nparent: plan/r1\n---\n",
            # the close is a subtask of the plan as well, and a follow-up subtask waits for the close
            "close/r1": "---\nparent: plan/r1\ndepends_on: [{task: plan/r1, kind: children}]\n---\n",
            "experiment/after": "---\nparent: plan/r1\ndepends_on: [close/r1]\n---\n",
        }
    )
    manage(pdir, "plan/r1", "experiment/a", "close/r1", "experiment/after")
    set_status(pdir, "plan/r1", "done")
    p = load_project(pdir)
    assert p.tasks["close/r1"].detail["tasks"] == ["experiment/a"]
    assert not p.tasks["close/r1"].warnings  # no cycle reported
    set_status(pdir, "experiment/a", "done")
    assert load_project(pdir).tasks["close/r1"].detail == {}


def test_children_edges_are_checked_for_cycles(make_project):
    pdir = make_project({"plan/r1": "", "close/r1": ""})
    ops.update_task(pdir, "close/r1", {"depends_on": [{"task": "plan/r1", "kind": "children"}]})
    fm = load_project(pdir).tasks["close/r1"].fm
    assert fm["depends_on"][0]["kind"] == "children"
    with pytest.raises(AgentSysError) as e:
        ops.update_task(pdir, "plan/r1", {"depends_on": ["close/r1"]})
    assert e.value.status == 409 and "循环" in str(e.value)


def test_the_close_task_is_told_about_the_plans_subtasks(make_project):
    pdir = make_project(ROUND)
    manage(pdir, *ROUND)
    (pdir / "workspaces/experiment/a/report.md").write_text("## 结论\n\n接受\n")
    p = load_project(pdir)
    assert launch.upstream_lines(p, p.tasks["close/r1"]) == [
        "- 计划（`plan/r1`，等它和它的子任务完成）：工作区 `workspaces/plan/r1/`，还没有 report.md",
        "  它的子任务：",
        "  - 实验 a（`experiment/a`，queued）：报告 `workspaces/experiment/a/report.md`",
        "  - 实验 b（`experiment/b`，queued）：还没有 report.md",
    ]
    en = "\n".join(launch.upstream_lines(p, p.tasks["close/r1"], "en"))
    assert "after it and its subtasks" in en and "Its subtasks:" in en


def test_children_through_the_server(tmp_path):
    home = run_home(tmp_path, "autorun: false\n")
    pdir = home / "campaigns/r"
    with TestClient(create_app(home)) as c:
        for name, text in (
            ("plan", "计划\n"),
            ("e1", "---\nparent: develop/plan\n---\n实验\n"),
            ("close", "---\ndepends_on: [{task: develop/plan, kind: children}]\n---\n收尾\n"),
        ):
            (pdir / f"tasks/develop/{name}.md").write_text(text)
        manage(pdir, "develop/plan", "develop/e1", "develop/close")
        set_status(pdir, "develop/plan", "done")
        c.app.state.store.sync(also=[("r", None)])
        t = get(c, "close")
        assert t["detail"] == {
            "kind": "children",
            "parents": ["develop/plan"],
            "tasks": ["develop/e1"],
        }
        up = t["upstream"][0]
        assert (up["kind"], up["blocking"], up["satisfied"]) == ("children", True, False)
        board = ok(c.get("/api/projects/r/board"))
        assert {"from": "develop/plan", "to": "develop/close", "kind": "children"}.items() <= next(
            e for e in board["edges"] if e["to"] == "develop/close"
        ).items()


# ---------------------------------------------------------------- joining what a plan drafted


def test_join_takes_the_subtasks_and_the_drafts_that_depend_on_the_plan(make_project):
    pdir = make_project(
        {
            **ROUND,
            "review/x": "---\ndepends_on: [{task: plan/r1, kind: review}]\n---\n评审\n",
            "experiment/other": "无关\n",
        },
        types=("experiment",),
    )
    (pdir / "task_types").mkdir()
    (pdir / "task_types/experiment.md").write_text(
        "---\nchecks:\n  - test -s report.md\n---\n## 任务描述\n"
    )
    (pdir / "task_types/close.md").write_text("---\nreview: true\n---\n## 任务描述\n")
    manage(pdir, "plan/r1")
    joined = ops.join_children(pdir, "plan/r1")
    assert sorted(joined) == ["close/r1", "experiment/a", "experiment/b"]
    p = load_project(pdir)
    assert p.tasks["review/x"].status == "draft" and p.tasks["experiment/other"].status == "draft"
    # a draft gets its type's review and checks when it has none of its own
    assert [c["run"] for c in p.tasks["experiment/a"].checks] == ["test -s report.md"]
    assert p.tasks["close/r1"].review and not p.tasks["close/r1"].checks
    assert ops.drafts_of(p, "plan/r1") == []
    with pytest.raises(AgentSysError) as e:
        ops.join_children(pdir, "plan/r1")
    assert e.value.status == 409


def test_join_keeps_what_a_draft_says_itself(make_project):
    pdir = make_project({"experiment/a": "---\nreview: false\nchecks: []\n---\n实验\n"})
    (pdir / "task_types").mkdir()
    (pdir / "task_types/experiment.md").write_text(
        "---\nreview: true\nchecks: [true]\n---\n## 任务描述\n"
    )
    t = ops.join(pdir, "experiment/a")
    assert t.status == "queued" and not t.review and t.checks == []


# ---------------------------------------------------------------- tasks a template brings


def test_a_template_brings_its_tasks_queued(tmp_path):
    templates = tmp_path / "templates"
    shutil.copytree(ops.TEMPLATES / "_common", templates / "_common")
    src = templates / "t"
    for path, text in {
        "project.yaml": "title: T\n",
        "task_types/develop.md": "## 任务描述\n",
        "tasks/develop/first.md": "---\ntitle: 第一步\n---\n做\n",
        "tasks/develop/second.md": "---\ndepends_on: [develop/first]\n---\n再做\n",
        "tasks/other/x.md": "没有骨架的类型\n",
        "tasks/develop/.hidden.md": "不是任务\n",
        "skills/own/SKILL.md": "---\ndescription: 模板自带的 skill\n---\n",
    }.items():
        (src / path).parent.mkdir(parents=True, exist_ok=True)
        (src / path).write_text(text, encoding="utf-8")
    assert ops.list_templates(templates)[0]["tasks"] == [
        "develop/first",
        "develop/second",
        "other/x",
    ]
    pdir = ops.new_project(tmp_path / "home", "c", "t", templates_dir=templates)
    p = load_project(pdir)
    assert {t.id: t.status for t in p.tasks.values()} == {
        "develop/first": "queued",
        "develop/second": "queued",
        "other/x": "queued",
    }
    assert p.tasks["develop/second"].detail == {"kind": "upstream", "tasks": ["develop/first"]}
    assert len({t.state["session_id"] for t in p.tasks.values()}) == 3
    assert [s["name"] for s in skills.found(pdir / "skills")] == ["own"]
    assert not (pdir / "tasks/develop/.hidden.md").exists()


def test_the_infera_perf_round_one(tmp_path):
    pdir = ops.new_project(tmp_path, "glm", "infera-perf")
    p = load_project(pdir)
    assert sorted(p.tasks) == ["baseline/r1", "intake/r1", "plan/r1", "profile/r1", "survey/r1"]
    assert all(t.managed and t.status == "queued" and not t.warnings for t in p.tasks.values())
    # only intake waits for nothing; nothing in a prompt names a later task (that would close a cycle)
    assert sorted((e.up, e.down) for e in p.edges if e.blocking) == [
        ("baseline/r1", "profile/r1"),
        ("intake/r1", "baseline/r1"),
        ("intake/r1", "plan/r1"),
        ("intake/r1", "survey/r1"),
        ("profile/r1", "plan/r1"),
        ("survey/r1", "plan/r1"),
    ]
    assert p.tasks["intake/r1"].detail == {}
    assert {t.id for t in p.tasks.values() if t.review} == {"intake/r1", "profile/r1", "plan/r1"}
    assert p.tasks["intake/r1"].agent["model"] == "sonnet"
    # a preset task carries the checks of its type
    types = {ty.id: ty for ty in p.types}
    for t in p.tasks.values():
        assert t.checks == [dict(c, name=c["run"][:120]) for c in t.checks]
        assert [c["run"] for c in t.checks] == [str(x) for x in types[t.type].checks], t.id
    # the questionnaire of the design's appendix A: 22 rows, the 8 that must be answered marked
    q = (pdir / "skills/infera-round/questionnaire_r1.md").read_text(encoding="utf-8")
    rows = re.findall(r"^\| ([A-D]\d)( ★)? \|", q, re.M)
    assert len(rows) == 22 and sum(bool(star) for _, star in rows) == 8
    assert "| # | 问题 | 建议 | 你的意见 |" in q


# ---------------------------------------------------------------- the field checks


def run_check(kind, pdir, tid):
    ok_, lines = perfcheck.run(kind, tid, pdir)
    return ok_, [x for x in lines if x.startswith("FAIL")]


def fill_intake(pdir):
    cfg = (pdir / "project.yaml").read_text(encoding="utf-8")
    cfg = cfg.replace('goal: ""', "goal: 工作点 Y +10%").replace(
        "machines: []", "machines: [{name: n1, gpus: 8}, n2]"
    )
    cfg = cfg.replace("working_point: {}", "working_point: {x: 30}")
    (pdir / "project.yaml").write_text(cfg, encoding="utf-8")
    rules = pdir / "rules/project.md"
    rules.write_text(rules.read_text().replace("<待 intake 填写>", "已填"), encoding="utf-8")
    (pdir / "workspaces/intake/r1/questions.md").write_text("| # | 问题 | 建议 | 你的意见 |\n")
    for tid in ("baseline/r1", "profile/r1"):
        ops.update_task(pdir, tid, {"machines": ["n1"]})


def test_intake_check(tmp_path):
    pdir = ops.new_project(tmp_path, "glm", "infera-perf")
    passed, failed = run_check("intake", pdir, "intake/r1")
    assert not passed and len(failed) == 7
    fill_intake(pdir)
    assert run_check("intake", pdir, "intake/r1") == (True, [])
    ops.update_task(pdir, "profile/r1", {"machines": ["nosuch"]})
    assert run_check("intake", pdir, "intake/r1")[1] == [
        "FAIL  profile/r1 has its machines: machines not in project.yaml: nosuch"
    ]
    # round 2: its plan has to be drafted, and project.yaml says the round
    (pdir / "tasks/intake/r2.md").write_text("---\ndepends_on: [intake/r1]\n---\n")
    (pdir / "workspaces/intake/r2").mkdir(parents=True)
    (pdir / "workspaces/intake/r2/questions.md").write_text("x\n")
    ops.update_task(pdir, "profile/r1", {"machines": ["n1"]})
    assert run_check("intake", pdir, "intake/r2")[1] == [
        "FAIL  project.yaml round is 2 (it is 1)",
        "FAIL  this round's plan is drafted (tasks/plan/r2.md)",
    ]
    # the command, as a check runs it
    assert perfcheck.main(["intake", "intake/nosuch"]) == 1
    assert perfcheck.main(["bogus"]) == 2


def test_survey_check(tmp_path):
    pdir = ops.new_project(tmp_path, "glm", "infera-perf")
    assert not run_check("survey", pdir, "survey/r1")[0]
    (pdir / "workspaces/survey/r1/report.md").write_text("## 结论\n\n无公开对标。\n")
    assert run_check("survey", pdir, "survey/r1")[0]


PLAN_CHILD = """---
parent: plan/r1
machines: [n1]
expect: {delta_pct: [2, 4], level: L2, falsify: 队列没有下降}
---
"""


def test_plan_check(tmp_path):
    pdir = ops.new_project(tmp_path, "glm", "infera-perf")
    fill_intake(pdir)
    passed, failed = run_check("plan", pdir, "plan/r1")
    assert not passed and failed == [
        "FAIL  a task waits for the plan's subtasks (depends_on {task: plan/r1, kind: children})"
    ]
    (pdir / "tasks/experiment/r1-spf.md").write_text(PLAN_CHILD, encoding="utf-8")
    (pdir / "tasks/experiment/r1-bad.md").write_text(
        "---\nparent: plan/r1\nmachines: [n9]\nexpect: {delta_pct: big, level: L9}\n---\n",
        encoding="utf-8",
    )
    (pdir / "tasks/close/r1.md").write_text(
        "---\ndepends_on: [{task: plan/r1, kind: children}]\nmachines: [n2]\n---\n",
        encoding="utf-8",
    )
    passed, failed = run_check("plan", pdir, "plan/r1")
    assert not passed and failed == [
        "FAIL  experiment/r1-bad has its machines: machines not in project.yaml: n9",
        "FAIL  experiment/r1-bad expect.delta_pct is a number or [low, high]",
        "FAIL  experiment/r1-bad expect.level is one of L0/L1/L2/L3/L4/L5 (the fast check it uses)",
        "FAIL  experiment/r1-bad expect.falsify says what result would refute it",
    ]
    (pdir / "tasks/experiment/r1-bad.md").unlink()
    assert run_check("plan", pdir, "plan/r1") == (True, [])


def write_result(pdir, tid, name, **doc):
    path = pdir / "workspaces" / tid / "results" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": ledger.SCHEMA, "label": name, **doc}), encoding="utf-8")


def test_close_check(tmp_path):
    pdir = ops.new_project(tmp_path, "glm", "infera-perf")
    fill_intake(pdir)
    (pdir / "tasks/close/r1.md").write_text(
        "---\ndepends_on: [{task: plan/r1, kind: children}]\n---\n", encoding="utf-8"
    )
    ws = pdir / "workspaces/close/r1"
    ws.mkdir(parents=True)
    passed, failed = run_check("close", pdir, "close/r1")
    assert failed == [
        'FAIL  stack.json is {"version": ..., "sealed": true|false, ...}',
        "FAIL  the next round's intake is drafted (tasks/intake/r2.md)",
    ]
    # not sealed: a retrospective, no curve needed
    (ws / "stack.json").write_text('{"version": "v0", "sealed": false}')
    (pdir / "tasks/intake/r2.md").write_text("---\ndepends_on: [close/r1]\n---\n")
    assert run_check("close", pdir, "close/r1") == (True, [])
    # sealed: the curve, and project.yaml moved on to it
    (ws / "stack.json").write_text('{"version": "v1", "sealed": true}')
    write_result(pdir, "close/r1", "v1-c64", series="v1", workload={"conc": 64}, metrics={"y": 1.0})
    assert run_check("close", pdir, "close/r1")[1] == [
        "FAIL  results/ has the integration curve of series 'v1' (2+ concurrencies, it has 1)",
        "FAIL  project.yaml version is 'v1'",
        "FAIL  project.yaml ledger.baseline_series is 'v1'",
    ]
    write_result(pdir, "close/r1", "v1-c32", series="v1", workload={"conc": 32}, metrics={"y": 1.2})
    cfg = (pdir / "project.yaml").read_text(encoding="utf-8")
    cfg = cfg.replace("version: v0", "version: v1").replace(
        "baseline_series: v0", "baseline_series: v1"
    )
    (pdir / "project.yaml").write_text(cfg, encoding="utf-8")
    assert run_check("close", pdir, "close/r1") == (True, [])
    (ws / "stack.json").write_text("{broken")
    assert run_check("close", pdir, "close/r1")[1][0].startswith("FAIL  stack.json is JSON")


# ---------------------------------------------------------------- the ledger's curves


def point(series, conc, x, y, role="candidate", **more):
    return {
        "series": series,
        "role": role,
        "round": 1,
        "workload": {"conc": conc},
        "metrics": {"input_tput_per_gpu": y, "p90_intvty": x},
        **more,
    }


CURVES = "title: t\nledger: {primary: input_tput_per_gpu, x: p90_intvty, baseline_series: v0, working_point: %s}\n"


def curve_project(make_project, wp="{x: 40}"):
    pdir = make_project(
        {"baseline/r1": "", "survey/r1": "", "experiment/r1-spf": "", "close/r1": ""},
        config=CURVES % wp,
    )
    for conc, x, y in ((16, 60.0, 10000.0), (32, 50.0, 16000.0), (64, 30.0, 20000.0)):
        write_result(pdir, "baseline/r1", f"v0-c{conc}", **point("v0", conc, x, y, "baseline"))
    # an extra v0 point that the others beat on both axes: not on the frontier
    write_result(pdir, "baseline/r1", "v0-c48", **point("v0", 48, 35.0, 15000.0, "baseline"))
    for conc, x, y in ((16, 70.0, 13000.0), (64, 40.0, 26000.0)):
        write_result(pdir, "survey/r1", f"ix-c{conc}", **point("ix:atom", conc, x, y, "reference"))
    write_result(
        pdir,
        "experiment/r1-spf",
        "spf-c32",
        **point("r1-spf", 32, 51.0, 17600.0, verdict={"decision": "accept"}),
    )
    return pdir


def test_curves_frontier_and_the_working_point(make_project):
    pdir = curve_project(make_project)
    full = ledger.ledger(load_project(pdir))
    assert (full["x"], full["baseline_series"], full["working_point"]) == (
        "p90_intvty",
        "v0",
        {"x": 40},
    )
    series = {c["name"]: c for c in full["series"]}
    assert [c["name"] for c in full["series"]] == ["v0", "r1-spf", "ix:atom"]
    v0 = series["v0"]
    assert (v0["role"], v0["baseline"], v0["round"]) == ("baseline", True, 1)
    assert [(p["conc"], p["frontier"]) for p in v0["points"]] == [
        (64, True),
        (48, False),
        (32, True),
        (16, True),
    ]
    # Y at X = 40: between C64 (30, 20000) and C32 (50, 16000) on the frontier
    assert v0["wp"] == {"x": 40, "y": 18000.0, "conc": None, "interpolated": True, "delta_pct": 0.0}
    ix = series["ix:atom"]
    assert ix["role"] == "reference" and ix["wp"]["y"] == 26000.0 and ix["wp"]["delta_pct"] == 44.44
    # a series that does not reach the working point has no value there
    assert series["r1-spf"]["wp"] == {
        "x": 51.0,
        "y": 17600.0,
        "conc": 32,
        "interpolated": False,
        "delta_pct": -2.22,
    }
    # rows: compared with the baseline series at the same concurrency; references are never the best
    rows = {r["label"]: r for r in full["rows"]}
    assert rows["spf-c32"]["delta_pct"] == 10.0 and rows["ix-c64"]["delta_pct"] == 30.0
    assert full["best"]["label"] == "v0-c64"  # the highest Y of ours (ix-c64 is higher)
    assert full["wp_best"]["series"] == "r1-spf"
    assert "series" not in ledger.head(full)
    assert ledger.task_briefs(full)["survey/r1"]["best_delta_pct"] is None


def test_the_working_point_at_a_concurrency(make_project):
    pdir = curve_project(make_project, "{conc: 32}")
    write_result(pdir, "baseline/r1", "v0-c32-again", **point("v0", 32, 52.0, 16400.0, "baseline"))
    series = {c["name"]: c for c in ledger.ledger(load_project(pdir))["series"]}
    assert series["v0"]["wp"] == {
        "x": 51.0,
        "y": 16200.0,
        "conc": 32,
        "n": 2,
        "interpolated": False,
        "delta_pct": 0.0,
    }
    assert series["r1-spf"]["wp"]["delta_pct"] == 8.64
    assert series["ix:atom"]["wp"] is None  # it has no C32 point


def test_at_x_beyond_and_before_the_curve():
    front = [{"x": 30.0, "y": 20.0, "conc": 64}, {"x": 50.0, "y": 16.0, "conc": 32}]
    assert ledger.at_x(front, 60, 1, 1) is None
    assert ledger.at_x(front, 10, 1, 1)["y"] == 20.0
    assert ledger.at_x(front, 50, 1, 1)["conc"] == 32
    # lower X is better (a latency on X): the same rule the other way round
    assert ledger.at_x(front, 40, -1, 1)["y"] == 18.0


def test_round_series_and_role_are_validated():
    base = {"schema": ledger.SCHEMA, "label": "x", "metrics": {"y": 1}}
    assert ledger.validate({**base, "role": "reference", "round": 0, "series": "ix:a"}) == []
    assert ledger.validate({**base, "role": "theirs", "round": True, "series": " "}) == [
        "role should be one of baseline, candidate, reference",
        "round should be a whole number (the campaign's round)",
        "series should be a name (the curve the point belongs to)",
    ]
    # a reference needs no verdict
    assert ledger.validate({**base, "role": "reference"}, require_verdict=True) == []


def test_agentx_output_becomes_a_result(tmp_path, capsys):
    src = FIXTURES / "agentx/agentx_conc40.json"
    agg = json.loads(src.read_text())
    doc = ledger.from_agentx(agg, src, "v0", 1, "baseline", time="2026-10-20T00:00:00Z")
    assert ledger.validate(doc) == []
    assert (doc["label"], doc["series"], doc["round"], doc["role"]) == (
        "v0-c40",
        "v0",
        1,
        "baseline",
    )
    m = doc["metrics"]
    # the InferenceX App's definitions: interactivity is 1 / p90 of the full-response ITL
    assert m["p90_intvty"] == round(1 / 0.01415, 6) and m["input_tput_per_gpu"] == 23873.55907
    assert m["p90_e2e_norm_intvty"] == 39.11896 and m["p90_ttft"] == 2.69414
    assert (m["requests"], m["errors"]) == (4561, 0)
    assert doc["workload"]["conc"] == 40 and doc["workload"]["num_gpus"] == 8
    assert doc["config"]["topology"] == "P TP1·PP4 → D TP4·DCP4"
    with pytest.raises(ValueError):
        ledger.from_agentx({"conc": 1}, src, "v0")
    # the command: a run directory in, result files out, checked like any
    run = tmp_path / "run/agentx"
    run.mkdir(parents=True)
    shutil.copy(src, run / src.name)
    (run / "agentx_conc8.json").write_text("{}")
    out = tmp_path / "results"
    argv = [
        "agentx",
        str(tmp_path / "run"),
        "--out",
        str(out),
        "--series",
        "r1-spf",
        "--round",
        "1",
    ]
    assert ledger.main(argv) == 1  # the broken one is reported
    printed = capsys.readouterr().out
    assert "wrote" in printed and "agentx_conc8.json: not an AgentX result" in printed
    assert ledger.main(["check", str(out)]) == 0
    written = json.loads((out / "r1-spf-c40.json").read_text())
    assert written["role"] == "candidate" and written["evidence"]["agentx"].endswith(
        "agentx_conc40.json"
    )
    assert (
        ledger.main(["agentx", str(tmp_path / "nothing"), "--out", str(out), "--series", "x"]) == 1
    )


# ---------------------------------------------------------------- packaging


def test_every_template_and_skill_file_is_package_data():
    """pyproject's package-data globs reach every file of the templates and skills (a wheel install has them)."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    block = text[text.index("agent_sys_v2 = [", text.index("[tool.setuptools.package-data]")) :]
    globs = re.findall(r'"([^"]+)"', block[: block.index("]\n")])
    pat = [re.compile(re.escape(g).replace(r"\*", "[^/]*")) for g in globs]
    pkg = ROOT / "src/agent_sys_v2"
    missing = [
        str(f.relative_to(pkg))
        for sub in ("templates", "skills")
        for f in (pkg / sub).rglob("*")
        if f.is_file()
        and "__pycache__" not in f.parts
        and not any(p.fullmatch(str(f.relative_to(pkg))) for p in pat)
    ]
    assert missing == []


def test_the_demo_campaign(tmp_path):
    """tools/demo.py: the template's round one, a close waiting for the plan's subtasks, the ledger's curves."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("demo", ROOT / "tools/demo.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    p = load_project(demo.main(tmp_path / "home"))
    assert p.tasks["close/r1"].detail["kind"] == "children"
    assert ops.drafts_of(p, "plan/r1") == ["experiment/r1-router"]
    full = ledger.ledger(p)
    assert [c["name"] for c in full["series"]][0] == "v0" and full["wp_best"]["series"] == "r1-spf"
