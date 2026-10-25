"""A task's workspace as the 工作区文件 tab sees it: list one directory, read a file, write a file (M1a
api_contract §3.8).

Paths are relative to the workspace and may not leave it: no absolute paths, no `..`. Reading follows symlinks
(workspaces often link to data elsewhere, and only you can reach the server's socket); writing does not
leave the workspace even through a symlink, and never touches .agent_sys/ (agent_sys's own state).
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from .i18n import _
from .model import AgentSysError, iso, mtime, write_text

MAX_ENTRIES = 300  # per directory listed (a prototype workspace held 100,000 files)
MAX_COUNT = 1000  # entries counted in a subdirectory
TAIL_BYTES = 64 * 1024  # text above this is shown as its last TAIL_LINES lines
TAIL_LINES = 200
MAX_MD = 2 * 1024 * 1024  # markdown is shown whole up to this size
IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
LOGS = {".log", ".out", ".err"}


def resolve(workspace, rel, write=False):
    """The path `rel` names inside the workspace; refused if it leaves the workspace (or, for writing, is in
    .agent_sys/ or resolves outside the workspace through a symlink)."""
    parts = PurePosixPath(rel or "").parts
    if (rel or "").startswith("/") or ".." in parts:
        raise AgentSysError(_("路径「{path}」不在任务工作区内", path=rel), 400)
    path = workspace.joinpath(*parts)
    if write:
        real, root = path.resolve(), workspace.resolve()
        if not real.is_relative_to(root):
            raise AgentSysError(_("路径「{path}」经软链接指向工作区以外，不能修改", path=rel), 400)
        if real.relative_to(root).parts[:1] == (".agent_sys",):
            raise AgentSysError(_(".agent_sys/ 由 agent_sys 维护，不能通过界面修改"), 400)
    return path


def kind_of(path):
    suffix = path.suffix.lower()
    return (
        "image"
        if suffix in IMAGES
        else "md"
        if suffix == ".md"
        else "log"
        if suffix in LOGS
        else "text"
    )


def list_dir(workspace, rel=""):
    """One level of a directory: {dir, entries, total, truncated}. Directories first; hidden entries are left out.
    A workspace that does not exist yet lists as empty."""
    path = resolve(workspace, rel)
    if not path.is_dir():
        if rel:
            raise AgentSysError(_("目录「{path}」不存在", path=rel), 404)
        return {"dir": "", "entries": [], "total": 0, "truncated": False}
    items = sorted(
        (e for e in path.iterdir() if not e.name.startswith(".")),
        key=lambda e: (not e.is_dir(), e.name),
    )
    entries = []
    for e in items[:MAX_ENTRIES]:
        sub = f"{rel}/{e.name}" if rel else e.name
        if e.is_dir():
            count = sum(1 for _, __ in zip(range(MAX_COUNT), e.iterdir()))
            entries.append(
                {"name": e.name, "path": sub, "type": "dir", "count": count, "mtime": iso(mtime(e))}
            )
        else:
            size = e.stat().st_size if e.exists() else 0
            entries.append(
                {
                    "name": e.name,
                    "path": sub,
                    "type": "file",
                    "size": size,
                    "kind": kind_of(e),
                    "mtime": iso(mtime(e)),
                }
            )
    return {
        "dir": rel,
        "entries": entries,
        "total": len(items),
        "truncated": len(items) > MAX_ENTRIES,
    }


def tail(path, lines=TAIL_LINES):
    with path.open("rb") as f:
        f.seek(max(0, path.stat().st_size - 4 * TAIL_BYTES))
        return b"\n".join(f.read().splitlines()[-lines:]).decode("utf-8", "replace")


def read_file(workspace, rel, raw_url):
    """{path, kind, size, mtime, content | url}: markdown whole, images by url, long text by its tail,
    binary files by size only."""
    path = resolve(workspace, rel)
    if not path.is_file():
        raise AgentSysError(_("文件「{path}」不存在", path=rel), 404)
    size, kind = path.stat().st_size, kind_of(path)
    out = {"path": rel, "kind": kind, "size": size, "mtime": iso(mtime(path))}
    if kind == "image":
        return {**out, "url": raw_url}
    with path.open("rb") as f:
        head = f.read(8192)
    if b"\0" in head:
        return {**out, "kind": "binary"}
    if size > (MAX_MD if kind == "md" else TAIL_BYTES):
        return {
            **out,
            "kind": "log",
            "content": tail(path),
            "truncated": True,
            "tail_lines": TAIL_LINES,
        }
    return {**out, "content": path.read_text(encoding="utf-8", errors="replace")}


def write_file(workspace, rel, content, base_mtime=""):
    """Save a text file of the workspace. base_mtime is the file's mtime when you started editing: if it changed
    since (the agent wrote it meanwhile), nothing is written."""
    if not PurePosixPath(rel or "").parts:  # '' or '.': the workspace itself
        raise AgentSysError(_("请指定要保存的文件"), 400)
    path = resolve(workspace, rel, write=True)
    if path.is_dir():
        raise AgentSysError(_("「{path}」是目录", path=rel), 400)
    if base_mtime and path.exists() and iso(mtime(path)) != base_mtime:
        raise AgentSysError(_("{path} 在你编辑期间已被修改，请载入新版本后再保存", path=rel), 409)
    write_text(path, content)
    return {"path": rel, "mtime": iso(mtime(path))}


def raw_headers(path: Path):
    """Response headers for /raw: never sniff a type, and whatever the file is, it runs sandboxed (opaque origin, no
    scripts): markup the agent wrote cannot call agent_sys's API from agent_sys_v2's origin, under any suffix."""
    return {
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-cache",
        "Content-Security-Policy": "sandbox",
    }
