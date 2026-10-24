"""The REST API on the fixture AGENT_SYS_HOME (M1a api_contract.md), through FastAPI's test client."""

import os
import time

import pytest
from fastapi.testclient import TestClient

from agent_sys_v2.model import read_state, split_frontmatter
from agent_sys_v2.server import create_app

TASK = "/api/projects/demo/tasks"


@pytest.fixture
def client(home):
    with TestClient(create_app(home)) as c:
        yield c


def ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def error(response, status):
    assert response.status_code == status, response.text
    return response.json()["error"]


def test_home_board_and_task(client):
    home = ok(client.get("/api/home"))
    assert [t["id"] for t in home["in_review"]] == [
        "develop/m0_poc",
        "develop/m1a_ui",
    ]  # longest waiting first
    assert [t["id"] for t in home["needs_you"]] == [
        "develop/m1d_lessons"
    ]  # unmanaged, progress 阻塞
    assert [t["id"] for t in home["running"]] == ["develop/m1b_core"]
    assert home["projects"][0]["counts"]["total"] == 7 and home["archived_projects"] == []
    assert [(t["id"], t["types"]) for t in home["templates"]] == [
        ("blank", ["develop"]),
        ("infera-perf", ["baseline", "develop", "diagnose", "experiment", "review"]),
    ]

    board = ok(client.get("/api/projects/demo/board"))
    assert [ty["id"] for ty in board["types"]] == ["develop", "review", "todo"] and len(
        board["edges"]
    ) == 4
    assert board["running"] == {"count": 0, "limit": 3, "global_count": 0, "global_limit": 4}

    t = ok(client.get(f"{TASK}/develop/m1c_server"))
    assert (
        t["status"] == "draft"
        and t["prompt"]["path"] == "campaigns/demo/tasks/develop/m1c_server.md"
    )
    assert [(u["task"], u["satisfied"], u["blocking"]) for u in t["upstream"]] == [
        ("develop/m1a_ui", False, True),
        ("develop/m1b_core", False, True),
    ]
    assert t["runs"] == [] and t["questions"] is None
    assert error(client.get(f"{TASK}/develop/nope"), 404) == "任务「develop/nope」不存在"
    assert error(client.get("/api/projects/nope/board"), 404) == "项目「nope」不存在"
    assert "接口不存在" in error(client.get("/api/nope"), 404)


def test_new_projects_from_both_templates(client, home):
    p = ok(
        client.post(
            "/api/projects", json={"name": "perf-q4", "template": "infera-perf", "title": "Q4"}
        )
    )
    assert (p["id"], p["title"], p["template"], p["counts"]["total"]) == (
        "perf-q4",
        "Q4",
        "infera-perf",
        0,
    )
    assert [ty["id"] for ty in ok(client.get("/api/projects/perf-q4/board"))["types"]] == [
        "baseline",
        "diagnose",
        "experiment",
        "develop",
        "review",
    ]
    ok(client.post("/api/projects", json={"name": "blank1", "template": "blank"}))
    assert [p["id"] for p in ok(client.get("/api/home"))["projects"]] == [
        "blank1",
        "demo",
        "perf-q4",
    ]
    assert "已存在" in error(client.post("/api/projects", json={"name": "blank1"}), 409)
    assert "不合法" in error(client.post("/api/projects", json={"name": "中文"}), 400)
    assert not (home / "campaigns/perf-q4/rules/protocol.md").exists()


def test_new_type_and_tasks(client, home):
    ty = ok(client.post("/api/projects/demo/types", json={"id": "perf", "review": True}))
    assert ty == {
        "id": "perf",
        "review": True,
        "checks": [],
        "skeleton": ty["skeleton"],
        "count": 0,
    }
    # the form's preview: prompt paths and chosen upstream tasks, as the scan will see them
    prompt = "## 任务描述\n\n- 按workspaces/develop/m1b_core/report.md 补测\n"
    pv = ok(
        client.post(
            f"{TASK}/preview",
            json={
                "type": "perf",
                "name": "c128",
                "prompt": prompt,
                "depends_on": ["review/pm_review"],
            },
        )
    )
    assert pv["name_error"] == "" and [(u["task"], u["source"]) for u in pv["upstream"]] == [
        ("develop/m1b_core", "prompt"),
        ("review/pm_review", "frontmatter"),
    ]
    assert (
        "不合法"
        in ok(client.post(f"{TASK}/preview", json={"type": "perf", "name": "c.v2"}))["name_error"]
    )
    assert (
        "已存在"
        in ok(client.post(f"{TASK}/preview", json={"type": "develop", "name": "m0_poc"}))[
            "name_error"
        ]
    )

    t = ok(
        client.post(
            TASK,
            json={
                "type": "perf",
                "name": "c128",
                "mode": "queue",
                "prompt": prompt,
                "depends_on": [{"task": "review/pm_review", "kind": "input"}],
                "review": True,
                "agent": {"budget_usd": 150},
            },
        )
    )
    assert (t["status"], t["detail"], t["managed"]) == (
        "queued",
        {"kind": "upstream", "tasks": ["develop/m1b_core"]},
        True,
    )
    assert t["agent"]["budget_usd"] == 150 and t["agent"]["model"] == "opus" and t["session_id"]
    text = (home / "campaigns/demo/tasks/perf/c128.md").read_text()
    assert text.startswith(
        "---\ndepends_on:\n  - review/pm_review\nreview: true\nagent: {budget_usd: 150}\n---\n"
    )
    d = ok(
        client.post(TASK, json={"type": "perf", "name": "later", "mode": "draft", "prompt": "x\n"})
    )
    assert d["status"] == "draft" and not (home / "campaigns/demo/workspaces/perf/later").exists()
    assert "已存在" in error(
        client.post(TASK, json={"type": "perf", "name": "later", "mode": "draft"}), 409
    )


def test_update_rename_and_delete(client, home):
    t = ok(client.get(f"{TASK}/todo/role_injection"))
    base = t["prompt"]["mtime"]
    t = ok(
        client.put(
            f"{TASK}/todo/role_injection",
            json={
                "frontmatter": {"summary": None, "review": True},
                "prompt": "新正文\n",
                "base_mtime": base,
            },
        )
    )
    assert (t["summary"], t["review"], t["prompt"]["body"]) == ("", True, "新正文\n")
    path = home / "campaigns/demo/tasks/todo/role_injection.md"
    os.utime(path, (time.time() + 5,) * 2)  # someone else wrote the file after we read it
    assert "已被修改" in error(
        client.put(f"{TASK}/todo/role_injection", json={"prompt": "x", "base_mtime": base}), 409
    )

    r = ok(
        client.post(f"{TASK}/todo/role_injection/rename", json={"type": "develop", "name": "roles"})
    )
    assert r["id"] == "develop/roles" and not path.exists()
    assert "正在运行" in error(
        client.post(f"{TASK}/develop/m1b_core/rename", json={"name": "m1b"}), 409
    )
    status = ok(client.get(f"{TASK}/develop/m0_poc"))["status"]
    r = ok(
        client.post(f"{TASK}/develop/m0_poc/rename", json={"type": "review"})
    )  # finished: may change type
    assert r["id"] == "review/m0_poc" and r["status"] == status
    assert (home / "campaigns/demo/workspaces/review/m0_poc/progress.md").is_file()

    lanes = ok(client.put("/api/projects/demo/lanes", json={"order": ["todo", "develop"]}))["types"]
    assert [ty["id"] for ty in lanes] == ["todo", "develop", "review"]
    assert [ty["id"] for ty in ok(client.get("/api/projects/demo/board"))["types"]] == [
        "todo",
        "develop",
        "review",
    ]

    d = ok(client.post(f"{TASK}/develop/roles/actions/delete"))
    assert d["deleted"] == "develop/roles" and d["trash"].startswith("campaigns/demo/.trash/")
    assert (home / d["trash"] / "tasks/develop/roles.md").is_file()


def test_join_approve_cancel_and_refusals(client, home):
    # approving an unmanaged task keeps it unmanaged and lets its downstream go
    t = ok(client.post(f"{TASK}/develop/m0_poc/actions/approve"))["task"]
    assert (t["status"], t["managed"]) == ("done", False)
    fm, _ = split_frontmatter((home / "campaigns/demo/tasks/develop/m0_poc.md").read_text())
    assert fm["approved_at"].endswith("Z")
    assert ok(client.get(f"{TASK}/develop/m1b_core"))["upstream"][0]["satisfied"]

    t = ok(client.post(f"{TASK}/develop/m1c_server/actions/join"))["task"]  # a draft -> queued
    assert (t["status"], t["managed"], t["detail"]["kind"]) == ("queued", True, "upstream")
    t = ok(client.post(f"{TASK}/develop/m1d_lessons/actions/join"))[
        "task"
    ]  # blocked -> needs_you / joined
    assert (t["status"], t["detail"]) == ("needs_you", {"kind": "joined"})
    assert "已纳入托管" in error(client.post(f"{TASK}/develop/m1d_lessons/actions/join"), 409)
    assert (
        ok(client.post(f"{TASK}/develop/m1c_server/actions/cancel"))["task"]["status"]
        == "cancelled"
    )
    assert (
        read_state(home / "campaigns/demo/workspaces/develop/m1c_server")["status"] == "cancelled"
    )
    assert (
        error(client.post(f"{TASK}/develop/m1d_lessons/actions/start"), 409)
        == "任务当前为「待确认」，无法执行「开始」"
    )
    assert (
        error(client.post(f"{TASK}/develop/m1d_lessons/messages", json={"text": " "}), 400)
        == "消息内容不能为空"
    )
    assert "未托管" in error(
        client.post(f"{TASK}/develop/m1b_core/messages", json={"text": "x"}), 409
    )
    assert "不认识" in error(client.post(f"{TASK}/develop/m1d_lessons/actions/fly"), 404)


def test_archive(client, home):
    p = ok(client.post("/api/projects/demo/archive"))
    assert p["archived"] and p["archived_at"].endswith("Z")
    home_data = ok(client.get("/api/home"))
    assert home_data["projects"] == [] and home_data["in_review"] == []
    assert [p["id"] for p in home_data["archived_projects"]] == ["demo"]
    assert "已归档" in error(client.post(TASK, json={"type": "develop", "name": "x"}), 409)
    assert not ok(client.post("/api/projects/demo/unarchive"))["archived"]
    assert (
        "# 上游完成后自动启动下游" in (home / "campaigns/demo/project.yaml").read_text()
    )  # comments kept


def test_broken_project_yaml_only_hides_that_project(client, home):
    ok(client.post("/api/projects", json={"name": "other"}))
    (home / "campaigns/demo/project.yaml").write_text("title: [unclosed\n")
    client.app.state.store.sync()
    projects = {p["id"]: p for p in ok(client.get("/api/home"))["projects"]}
    assert "无法解析" in projects["demo"]["error"] and "counts" in projects["other"]
    assert "无法解析" in error(client.get("/api/projects/demo/board"), 422)


def test_files(client, home):
    base = f"{TASK}/develop/m0_poc"
    root = ok(client.get(f"{base}/files"))
    assert [(e["name"], e["type"]) for e in root["entries"]] == [
        ("log", "dir"),
        ("progress.md", "file"),
        ("report.md", "file"),
    ]
    assert (
        ok(client.get(f"{base}/files", params={"dir": "log"}))["entries"][0]["path"]
        == "log/run.log"
    )
    f = ok(client.get(f"{base}/file", params={"path": "report.md"}))
    assert f["kind"] == "md" and f["content"].startswith("## 结论")
    saved = ok(
        client.put(
            f"{base}/file",
            params={"path": "report.md"},
            json={"content": "## 结论\n\n- 改了\n", "base_mtime": f["mtime"]},
        )
    )
    assert saved["path"] == "report.md"
    ws = home / "campaigns/demo/workspaces/develop/m0_poc"
    os.utime(ws / "report.md", (time.time() + 5,) * 2)
    assert "已被修改" in error(
        client.put(
            f"{base}/file",
            params={"path": "report.md"},
            json={"content": "x", "base_mtime": saved["mtime"]},
        ),
        409,
    )
    # out of the workspace, agent_sys's own files, and through a symlink: refused
    assert "不在任务工作区内" in error(
        client.get(f"{base}/file", params={"path": "../m1a_ui/report.md"}), 400
    )
    assert ".agent_sys/" in error(
        client.put(f"{base}/file", params={"path": ".agent_sys/state.json"}, json={"content": ""}),
        400,
    )
    (ws / "outside").symlink_to(home / "campaigns/demo/workspaces/develop/m1a_ui")
    assert ok(client.get(f"{base}/file", params={"path": "outside/report.md"}))[
        "content"
    ]  # reading follows
    assert "软链接" in error(
        client.put(f"{base}/file", params={"path": "outside/report.md"}, json={"content": ""}), 400
    )
    (ws / ".agent_sys").mkdir()
    (ws / "state").symlink_to(ws / ".agent_sys")
    assert ".agent_sys/" in error(
        client.put(f"{base}/file", params={"path": "state/x.json"}, json={"content": ""}), 400
    )
    # long text: its tail; images and markup through /raw, sandboxed
    (ws / "log/big.log").write_text("".join(f"line {i} " + "x" * 80 + "\n" for i in range(5000)))
    big = ok(client.get(f"{base}/file", params={"path": "log/big.log"}))
    assert big["truncated"] and big["content"].splitlines()[-1].startswith("line 4999")
    (ws / "page.html").write_text('<script>fetch("/api/home")</script>')
    raw = client.get(f"{base}/raw", params={"path": "page.html"})
    assert (
        raw.headers["content-security-policy"] == "sandbox"
        and raw.headers["x-content-type-options"] == "nosniff"
    )
    (ws / "curve.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    img = ok(client.get(f"{base}/file", params={"path": "curve.png"}))
    assert img["kind"] == "image" and img["url"] == f"{base}/raw?path=curve.png"


def test_settings(client, home):
    s = ok(client.get("/api/settings", params={"project": "demo"}))
    assert s["global"]["values"] == {
        "max_running": 4,
        "language": "en",
        "protocol_language": "zh",
    }
    assert s["project"]["path"] == "campaigns/demo/project.yaml"
    assert s["project"]["values"]["agent"]["budget_usd"] == 200
    out = ok(
        client.put(
            "/api/settings",
            json={
                "scope": "project",
                "project": "demo",
                "values": {"max_running": 5, "agent": {"model": "sonnet"}},
            },
        )
    )
    assert out["values"]["max_running"] == 5 and out["values"]["agent"]["effort"] == "high"
    text = (home / "campaigns/demo/project.yaml").read_text()
    assert "model: sonnet" in text and "# 任务默认值" in text
    ok(client.put("/api/settings", json={"scope": "global", "values": {"max_running": 6}}))
    assert (home / "agent_sys.yaml").read_text() == "max_running: 6\n"
    assert ok(client.get("/api/projects/demo/board"))["running"]["global_limit"] == 6
    assert "正整数" in error(
        client.put("/api/settings", json={"scope": "global", "values": {"max_running": 0}}), 400
    )
    assert "语言应为" in error(
        client.put(
            "/api/settings", json={"scope": "global", "values": {"protocol_language": "fr"}}
        ),
        400,
    )
    ok(client.put("/api/settings", json={"scope": "global", "values": {"protocol_language": "en"}}))
    assert ok(client.get("/api/rules"))["protocol"]["language"] == "en"
    assert ok(client.get("/api/settings"))["projects"][0]["id"] == "demo"


def test_changes_reach_listeners(client, home):
    store, seen = client.app.state.store, []
    store.listeners.add(seen.append)
    ok(client.post(f"{TASK}/develop/m1c_server/actions/join"))
    assert {"project": "demo", "task": "develop/m1c_server"} in seen
    (home / "campaigns/demo/workspaces/develop/m1b_core/progress.md").write_text("状态: 已完成\n")
    store.sync()  # what the 2 s poll does
    assert seen[-1] == {"project": "demo", "task": "develop/m1b_core"}
    assert ok(client.get(f"{TASK}/develop/m1b_core"))["status"] == "done"


def test_a_scan_that_fails_unexpectedly_shows_on_its_row(client, home, monkeypatch):
    from agent_sys_v2 import server

    ok(client.post("/api/projects", json={"name": "other"}))
    real = server.load_project

    def broken(pdir):
        if pdir.name == "demo":
            raise PermissionError("denied")
        return real(pdir)

    monkeypatch.setattr(server, "load_project", broken)
    (home / "campaigns/demo/project.yaml").write_text("title: 换个标题\n")
    client.app.state.store.sync()
    projects = {p["id"]: p for p in ok(client.get("/api/home"))["projects"]}
    assert "扫描项目失败" in projects["demo"]["error"] and "counts" in projects["other"]
    monkeypatch.setattr(server, "load_project", real)
    client.app.state.store.sync()  # scanned again at the next poll, without a new change
    assert ok(client.get("/api/projects/demo/board"))["project"]["title"] == "换个标题"


def test_a_file_needs_a_name(client):
    for path in ("", "."):
        assert "请指定" in error(
            client.put(f"{TASK}/develop/m0_poc/file", params={"path": path}, json={"content": "x"}),
            400,
        )
