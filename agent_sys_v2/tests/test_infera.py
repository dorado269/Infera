"""What agent_sys adds to conch for Infera performance work: acceptance checks run by the harness, machines held by
one task at a time, the results ledger, the skills index, and joining an agent's draft subtasks at once."""

import json

import pytest
from conftest import DONE, T, get, new, ok, replay, run_home, wait, ws
from fastapi.testclient import TestClient

from agent_sys_v2 import checks, launch, ledger, machines, ops, sched, skills
from agent_sys_v2.model import AgentSysError, new_state, write_state
from agent_sys_v2.scan import load_project
from agent_sys_v2.server import create_app

# fails the first time it runs in a workspace, passes after: an agent that fixed what the first attempt showed
FLAKY = (
    'if [ -f "$AGENT_SYS_WORKSPACE/.tried" ]; then echo fixed; '
    'else touch "$AGENT_SYS_WORKSPACE/.tried"; echo "not yet: results missing"; exit 1; fi'
)


# ---------------------------------------------------------------- checks


def test_checks_parse_and_refuse_what_cannot_run():
    assert checks.parse(None) == [] and checks.parse([]) == []
    assert checks.parse("test -s report.md") == [
        {"name": "test -s report.md", "run": "test -s report.md", "timeout": checks.DEFAULT_TIMEOUT}
    ]
    assert checks.parse([{"name": "r", "run": "true", "timeout": 5}]) == [
        {"name": "r", "run": "true", "timeout": 5}
    ]
    for bad in (
        [""],
        [{"name": "no run"}],
        [{"run": "x", "timeout": 0}],
        [{"run": "x", "timeout": True}],
        [3],
    ):
        with pytest.raises(ValueError):
            checks.parse(bad)
    assert (
        checks.retries({}) == checks.DEFAULT_RETRIES and checks.retries({"check_retries": 0}) == 0
    )


def test_run_all_runs_every_check_and_keeps_the_end_of_the_output(tmp_path):
    seen = []
    attempt = checks.run_all(
        checks.parse(
            [
                'echo "$AGENT_SYS_TASK"',
                "python3 -c \"print('x' * 9000); raise SystemExit(3)\"",
                {"name": "slow", "run": "sleep 5", "timeout": 0.5},
            ]
        ),
        tmp_path,
        {"AGENT_SYS_TASK": "develop/a"},
        on_start=lambda n, c: seen.append(("start", n)),
        on_end=lambda n, r: seen.append(("end", n, r["ok"])),
    )
    a, b, c = attempt["results"]
    assert not attempt["ok"] and a["ok"] and a["output"] == "develop/a\n"
    assert (b["exit"], len(b["output"])) == (3, checks.TAIL + 1) and b["output"].startswith("…")
    assert (
        c["timed_out"] and not c["ok"] and c["seconds"] < 3
    )  # the whole group is killed at its timeout
    assert seen == [
        ("start", 1),
        ("end", 1, True),
        ("start", 2),
        ("end", 2, False),
        ("start", 3),
        ("end", 3, False),
    ]
    assert checks.summary(tmp_path) is None
    assert (
        checks.record(tmp_path, attempt) == 1
        and checks.record(tmp_path, {**attempt, "ok": True}) == 2
    )
    s = checks.summary(tmp_path)
    assert (s["ok"], s["attempts"], s["passed"], s["total"]) == (True, 2, 1, 3)
    text = checks.feedback(attempt, 2)
    assert (
        "2/3 条失败" in text and "exit 3" in text and "超时" in text and "还会再检查 2 次" in text
    )
    assert "did not pass (2 of 3 failed)" in checks.feedback(attempt, 1, "en")


def test_a_failed_check_goes_back_to_the_agent_and_the_task_is_done_once_it_passes(tmp_path):
    home = run_home(tmp_path)
    with TestClient(create_app(home)) as c:
        new(
            c, "a", agent=replay("p2_queue", speed=20, writes={"progress.md": DONE}), checks=[FLAKY]
        )
        t = wait(c, "a", lambda t: t["status"] == "done")
        assert t["checks"]["ok"] and t["checks"]["attempts"] == 2
        attempts = t["check_runs"]["attempts"]
        assert [a["ok"] for a in attempts] == [False, True]
        assert "not yet: results missing" in attempts[0]["results"][0]["output"]
        run = t["runs"][0]["id"]
        events = [
            json.loads(line)
            for line in (ws(home, "a") / f".agent_sys/runs/{run}/events.jsonl")
            .read_text()
            .splitlines()
        ]
        sent = [e["text"] for e in events if e.get("subtype") == "send_user"]
        # the recording's own messages, then the failed attempt sent back into the session, once
        assert (
            sent[-1].startswith("验收检查没有通过（1/1 条失败）")
            and "not yet: results missing" in sent[-1]
        )
        assert sum(s.startswith("验收检查没有通过") for s in sent) == 1
        steps = [
            (e["subtype"], e.get("phase"), e.get("ok"))
            for e in events
            if e.get("subtype") in ("check", "checks")
        ]
        assert steps[:4] == [
            ("checks", "start", None),
            ("check", "start", None),
            ("check", "end", False),
            ("checks", "end", False),
        ]
        assert steps[-1] == ("checks", "end", True)


def test_checks_that_keep_failing_leave_the_task_needing_you(tmp_path):
    home = run_home(tmp_path)
    with TestClient(create_app(home)) as c:
        new(
            c,
            "a",
            agent=replay("p2_queue", speed=20, writes={"progress.md": DONE}),
            checks=["false"],
        )
        t = wait(c, "a", lambda t: t["status"] == "needs_you")
        assert t["detail"] == {
            "kind": "checks_failed",
            "failed": ["false"],
            "passed": 0,
            "total": 1,
        }
        assert t["checks"]["attempts"] == 1 + checks.DEFAULT_RETRIES and not t["checks"]["ok"]
        home_page = ok(c.get("/api/home"))
        assert [x["id"] for x in home_page["needs_you"]] == ["develop/a"]


def test_a_task_without_checks_is_done_as_progress_md_says(tmp_path):
    home = run_home(tmp_path)
    with TestClient(create_app(home)) as c:
        new(c, "a", agent=replay("p2_queue", speed=20, writes={"progress.md": DONE}))
        t = wait(c, "a", lambda t: t["status"] == "done")
        assert t["checks"] is None and t["check_runs"] is None


# ---------------------------------------------------------------- machines


def test_machines_are_declared_and_named_by_tasks():
    assert machines.declared(
        {"machines": ["a", {"name": "b", "gpus": 8, "note": "MI355X"}, {"name": "a"}, {}, 3]}
    ) == [
        {"name": "a", "gpus": None, "note": ""},
        {"name": "b", "gpus": 8, "note": "MI355X"},
    ]
    assert machines.task_machines({"machines": "n1"}) == ["n1"]
    assert machines.task_machines({"machines": ["n1", " n2 ", "n1", ""]}) == ["n1", "n2"]
    assert machines.task_machines({}) == []


def test_a_queued_task_waits_for_its_machines_across_campaigns(make_project):
    one = make_project(
        {"develop/a": "---\nmachines: [n1]\n---\n跑 A\n"},
        name="one",
        config="title: one\nmachines: [{name: n1, gpus: 8}]\n",
    )
    two = make_project(
        {
            "develop/b": "---\nmachines: [n1, n2]\n---\n跑 B\n",
            "develop/c": "---\nmachines: n2\n---\n跑 C\n",
        },
        name="two",
        config="title: two\n",
    )
    write_state(
        one / "workspaces/develop/a", new_state("needs_you", {"kind": "asked"})
    )  # it waits for you: still holds
    for tid in ("develop/b", "develop/c"):
        write_state(two / "workspaces" / tid, new_state("queued"))
    projects = [load_project(one), load_project(two)]
    go = sched.plan(projects, 4)
    b, c = projects[1].tasks["develop/b"], projects[1].tasks["develop/c"]
    assert b.detail == {
        "kind": "machines",
        "machines": [{"name": "n1", "project": "one", "task": "develop/a", "title": "a"}],
    }
    assert [t.id for _p, t in go] == ["develop/c"] and c.detail == {}
    with pytest.raises(AgentSysError) as e:
        machines.check_free(projects, projects[1], b)
    assert e.value.status == 409 and "n1" in str(e.value) and "one/develop/a" in str(e.value)
    rows = {m["name"]: m for m in machines.table(projects)}
    assert rows["n1"]["gpus"] == 8 and rows["n1"]["holder"]["task"] == "develop/a"
    assert rows["n1"]["holder"]["idle_minutes"] == 0  # it has only just started waiting
    assert [
        m["name"] for m in machines.table(projects, only=projects[1])
    ] == []  # two declares none, holds none yet
    # once a's wait has gone on for an hour, it holds n1 idle
    write_state(
        one / "workspaces/develop/a",
        {**new_state("needs_you", {"kind": "asked"}), "since": "2026-01-01T00:00:00Z"},
    )
    projects = [load_project(one), load_project(two)]
    assert machines.table(projects)[0]["holder"]["idle_minutes"] >= machines.IDLE_MINUTES
    # done, it lets go: b starts and takes n1 and n2, so c waits for b now
    write_state(one / "workspaces/develop/a", new_state("done"))
    projects = [load_project(one), load_project(two)]
    assert [t.id for _p, t in sched.plan(projects, 4)] == ["develop/b"]
    assert projects[1].tasks["develop/c"].detail["machines"][0]["task"] == "develop/b"


def test_machines_through_the_server(tmp_path):
    home = run_home(tmp_path, "machines: [{name: n1, gpus: 8, note: MI300X}]\n")
    with TestClient(create_app(home)) as c:
        new(
            c, "a", machines=["n1"]
        )  # plays p2_queue and stops without being done: waits for you, holds n1
        wait(c, "a", lambda t: t["status"] == "needs_you")
        new(c, "b", machines=["n1"])
        b = wait(c, "b", lambda t: t["detail"].get("kind") == "machines", 10)
        assert b["detail"]["machines"][0]["task"] == "develop/a" and b["machines"] == ["n1"]
        assert "develop/a" in c.post(f"{T}/b/actions/start").json()["error"]
        board = ok(c.get("/api/projects/r/board"))
        assert board["machines"] == [
            {
                "name": "n1",
                "gpus": 8,
                "note": "MI300X",
                "projects": ["r"],
                "holder": board["machines"][0]["holder"],
            }
        ]
        assert board["machines"][0]["holder"]["task"] == "develop/a"
        assert [m["name"] for m in ok(c.get("/api/home"))["machines"]] == ["n1"]
        assert get(c, "b")["project_machines"] == [{"name": "n1", "gpus": 8, "note": "MI300X"}]
        ok(c.post(f"{T}/a/actions/cancel"))
        wait(c, "b", lambda t: t["status"] in ("running", "needs_you"), 10)


# ---------------------------------------------------------------- ledger


def result(label, value, **more):
    return {
        "schema": ledger.SCHEMA,
        "label": label,
        "metrics": {"tok_s_gpu": value, "p90_interactivity": 30},
        **more,
    }


def write_result(pdir, tid, name, doc, time_=None):
    path = pdir / "workspaces" / tid / "results" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc) if not isinstance(doc, str) else doc, encoding="utf-8")
    return path


def test_ledger_reads_results_against_the_baseline(make_project, capsys):
    pdir = make_project(
        {"baseline/b0": "", "experiment/e1": "", "experiment/e2": ""},
        config="title: t\nledger: {primary: tok_s_gpu}\n",
    )
    write_result(
        pdir,
        "baseline/b0",
        "c64",
        result("c64", 10000.0, role="baseline", time="2026-10-01T00:00:00Z"),
    )
    write_result(
        pdir,
        "experiment/e1",
        "spf",
        result(
            "spf",
            11540.0,
            time="2026-10-02T00:00:00Z",
            verdict={"decision": "accept", "notes": "[M] queue ↓"},
        ),
    )
    write_result(
        pdir,
        "experiment/e1",
        "spf2",
        result(
            "spf2",
            13000.0,
            time="2026-10-03T00:00:00Z",
            verdict={"status": "void", "notes": "foreign load"},
        ),
    )
    write_result(
        pdir,
        "experiment/e2",
        "dcp",
        result("dcp", 12000.0, time="2026-10-04T00:00:00Z", verdict={"decision": "reject"}),
    )
    write_result(
        pdir, "experiment/e2", "bad", {"schema": ledger.SCHEMA, "label": "", "metrics": {}}
    )
    write_result(pdir, "experiment/e2", "other", {"some": "json of another tool"})
    write_result(pdir, "experiment/e2", "broken", "{not json")
    full = ledger.ledger(load_project(pdir))
    assert full["primary"] == "tok_s_gpu" and full["higher_is_better"]
    assert [r["label"] for r in full["rows"]] == ["c64", "spf", "spf2", "dcp"]
    assert full["baseline"]["label"] == "c64" and [r["delta_pct"] for r in full["rows"]] == [
        0.0,
        15.4,
        30.0,
        20.0,
    ]
    assert full["best"]["label"] == "spf"  # spf2 is void, dcp rejected
    assert (full["count"], full["void"]) == (4, 1)
    assert sorted(e["file"] for e in full["errors"]) == ["results/bad.json", "results/broken.json"]
    briefs = ledger.task_briefs(full)
    assert (
        briefs["experiment/e1"]["best_delta_pct"] == 15.4 and briefs["experiment/e2"]["errors"] == 2
    )
    assert briefs["baseline/b0"]["baseline"]
    # a pinned baseline, and a metric where lower is better
    (pdir / "project.yaml").write_text(
        "title: t\nledger: {primary: tok_s_gpu, higher_is_better: false, baseline: experiment/e1}\n"
    )
    full = ledger.ledger(load_project(pdir))
    assert full["baseline"]["label"] == "spf2" and full["best"]["label"] == "c64"
    # the check command: every result valid, a candidate with its decision
    ok_dir = pdir / "workspaces/experiment/e1/results"
    assert (
        ledger.main(["check", str(ok_dir), "--require-verdict"]) == 0
    )  # an accept, and a void without decision
    write_result(pdir, "experiment/e1", "later", result("later", 1.0))
    assert ledger.main(["check", str(ok_dir)]) == 0
    assert (
        ledger.main(["check", str(ok_dir), "--require-verdict"]) == 1
    )  # a candidate nobody judged
    assert "verdict.decision is missing" in capsys.readouterr().out
    assert ledger.main(["check", str(pdir / "workspaces/experiment/e2/results")]) == 1
    assert ledger.main(["check", str(pdir / "workspaces/nothing")]) == 1


def test_ledger_through_the_server(tmp_path):
    home = run_home(tmp_path, "ledger: {primary: tok_s_gpu}\n")
    pdir = home / "campaigns/r"
    with TestClient(create_app(home)) as c:
        new(c, "a", mode="draft")
        write_result(pdir, "develop/a", "base", result("base", 100.0, role="baseline"))
        write_result(pdir, "develop/a", "try", result("try", 110.0, verdict={"decision": "accept"}))
        c.app.state.store.sync(also=[("r", "develop/a")])
        full = ok(c.get("/api/projects/r/ledger"))
        assert [r["delta_pct"] for r in full["rows"]][-1] == 10.0
        board = ok(c.get("/api/projects/r/board"))
        assert board["project"]["ledger"]["best"]["label"] == "try"
        assert get(c, "a")["results"]["best_delta_pct"] == 10.0


# ---------------------------------------------------------------- skills and the plan an agent drafts


def test_skills_index(tmp_path, make_project, monkeypatch):
    shipped = tmp_path / "shipped"
    for name, desc in (
        ("infera-bench", "measure a point"),
        ("infera-deploy", "bring a kit up"),
        ("draft", ""),
    ):
        (shipped / name).mkdir(parents=True)
        (shipped / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {desc}\n---\n# {name}\n"
        )
    monkeypatch.setattr(skills, "SHIPPED", shipped)
    pdir = make_project(
        {"develop/a": ""}, config="title: t\nskills: [infera-deploy, infera-bench, nosuch]\n"
    )
    (pdir / "skills/own").mkdir(parents=True)
    (pdir / "skills/own/SKILL.md").write_text("---\ndescription: this campaign's own\n---\n")
    p = load_project(pdir)
    assert [s["name"] for s in skills.available(p)] == ["infera-deploy", "infera-bench", "own"]
    index = skills.index(p)
    assert (
        index.splitlines()[0]
        == f"- infera-deploy — bring a kit up `{shipped}/infera-deploy/SKILL.md`"
    )
    (pdir / "project.yaml").write_text(
        "title: t\n"
    )  # no list: every shipped skill with a description
    p = load_project(pdir)
    assert [s["name"] for s in skills.available(p)] == ["infera-bench", "infera-deploy", "own"]
    parts, text = launch.system(tmp_path / "home", p, p.tasks["develop/a"])
    assert [x["title"] for x in parts if x["title"] == "skills 索引"] == ["skills 索引"]
    assert "一行一个 skill" in text and "infera-deploy — bring a kit up" in text


def test_the_shipped_skills_are_indexed():
    names = [s["name"] for s in skills.found(skills.SHIPPED)]
    assert {
        "infera-deploy",
        "infera-bench",
        "infera-quality-gate",
        "infera-diagnose",
        "infera-experiment",
        "infera-hygiene",
    } <= set(names)


def test_join_the_drafts_a_task_wrote(tmp_path):
    home = run_home(tmp_path, "autorun: false\n")
    pdir = home / "campaigns/r"
    with TestClient(create_app(home)) as c:
        new(c, "plan", mode="draft")
        for name in ("e1", "e2"):
            (pdir / f"tasks/develop/{name}.md").write_text(
                f"---\nparent: develop/plan\nsummary: 试 {name}\n---\n做 {name}\n"
            )
        (pdir / "tasks/develop/other.md").write_text("与 plan 无关\n")
        c.app.state.store.sync(also=[("r", None)])
        r = ok(c.post(f"{T}/plan/actions/join_children"))
        assert sorted(r["joined"]) == ["develop/e1", "develop/e2"]
        assert get(c, "e1")["status"] == "queued" and get(c, "other")["status"] == "draft"
        assert "没有待纳入托管" in c.post(f"{T}/plan/actions/join_children").json()["error"]
        with pytest.raises(AgentSysError) as e:
            ops.join_children(pdir, "develop/nope")
        assert e.value.status == 404


# ---------------------------------------------------------------- the A/B judge of the infera-experiment skill


def load_judge():
    import importlib.util

    path = skills.SHIPPED / "infera-experiment/scripts/ab_judge.py"
    spec = importlib.util.spec_from_file_location("ab_judge", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ab_judge_never_turns_noise_into_an_accept(tmp_path, capsys):
    j = load_judge()
    d = lambda base, cand, **kw: j.judge(base, cand, **kw)["decision"]  # noqa: E731
    assert d([100], [101.5], mechanism="agree") == "noise"  # inside the 2% band
    assert d([100], [97], mechanism="agree") == "reject"
    assert d([100], [102.5], mechanism="agree") == "rerun"  # between the band and the 3% bar
    assert d([100], [110]) == "rerun"  # a gain without the mechanism shown
    assert d([100], [110], mechanism="disagree") == "rerun"
    assert d([100], [110], mechanism="agree") == "accept"
    # a wide spread raises the floor: +6% on runs that scatter by 10% is noise
    assert d([90, 100, 110], [96, 106, 116], mechanism="agree") == "noise"
    # lower is better (a latency): -8% is the gain
    assert d([2.0], [1.84], higher_is_better=False, mechanism="agree") == "accept"
    assert d([2.0], [2.2], higher_is_better=False) == "reject"
    # from result files, written back into the candidate
    base = write_result(tmp_path, "b", "base", result("base", 10000.0, role="baseline"))
    cand = write_result(tmp_path, "c", "cand", result("cand", 11000.0))
    assert (
        j.main(
            [
                "--metric",
                "tok_s_gpu",
                "--baseline-files",
                str(base),
                "--candidate-files",
                str(cand),
                "--mechanism",
                "agree",
                "--write",
                str(cand),
            ]
        )
        == 0
    )
    doc = json.loads(cand.read_text())
    assert doc["verdict"]["decision"] == "accept" and doc["verdict"]["vs_baseline_pct"] == 10.0
    assert ledger.validate(doc, require_verdict=True) == []
    assert (
        j.main(
            [
                "--baseline",
                "1",
                "--candidate",
                "2",
                "--void",
                "foreign load on GPU 3",
                "--write",
                str(cand),
            ]
        )
        == 0
    )
    assert json.loads(cand.read_text())["verdict"] == {
        "status": "void",
        "notes": "foreign load on GPU 3",
    }
    with pytest.raises(SystemExit):  # result files need --metric
        j.main(["--baseline-files", str(base)])
