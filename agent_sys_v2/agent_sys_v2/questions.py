"""questions.md answered as a form. When the agent has several questions it
writes them as a Markdown table, `# | 问题 | 建议 | 你的意见` (`# | Question | Suggestion | Your answer` under the
English protocol), the last column left for you. The first table with that column is read as rows; your answers go
into it (| escaped, line breaks written as <br>) and the agent is told to go on in the same session. Only this
round's questions.md counts (runs.round_questions): once you answered it, the next round begins and the table is an
old one.
"""

from __future__ import annotations

import re

from . import launch, runs
from .i18n import _, status_word
from .model import AgentSysError, iso, mtime, read_text, write_text

ANSWER = "你的意见"
ANSWER_COLUMN = re.compile(
    r"你的意见|your answer", re.I
)  # the protocol's column in Chinese and in English
ROW = re.compile(r"\s*\|")
RULE = re.compile(r"\s*\|?[\s:|-]*-[\s:|-]*")  # |---|:--| under the header
PIPE = re.compile(r"(?<!\\)\|")
BR = re.compile(r"<br\s*/?>", re.I)


def cells(line):
    """The cells of a table row, stripped: split at the pipes that are not escaped, the outer ones dropped."""
    body = line.strip()
    body = body[1:] if body.startswith("|") else body
    body = body[:-1] if body.endswith("|") and not body.endswith("\\|") else body
    return [c.strip() for c in PIPE.split(body)]


def unescape(cell):
    return BR.sub("\n", cell).replace("\\|", "|")


def escape(text):
    return "<br>".join(line.strip().replace("|", "\\|") for line in text.strip().splitlines())


def parse(text):
    """The first table that has a 你的意见 column, or None: {columns, answer_column, rows, before, after, lines}.
    A row is {key, cells, answer}; key is its first cell (Q1, 1, …), or #<n> where that is empty or repeated."""
    lines = text.split("\n")
    for i in range(len(lines) - 1):
        if not ROW.match(lines[i]) or not RULE.fullmatch(lines[i + 1]):
            continue
        columns = cells(lines[i])
        col = next((j for j, c in enumerate(columns) if ANSWER_COLUMN.search(c)), None)
        if col is None:
            continue
        end = i + 2
        while end < len(lines) and ROW.match(lines[end]):
            end += 1
        rows, keys = [], set()
        for n, line in enumerate(lines[i + 2 : end]):
            c = cells(line)
            c += [""] * (len(columns) - len(c))
            key = c[0] if c[0] and c[0] not in keys else f"#{n + 1}"
            keys.add(key)
            rows.append({"key": key, "cells": c, "answer": unescape(c[col]), "line": i + 2 + n})
        return {
            "columns": columns,
            "answer_column": col,
            "rows": rows,
            "before": "\n".join(lines[:i]).strip("\n"),
            "after": "\n".join(lines[end:]).strip("\n"),
            "lines": lines,
        }
    return None


def fill(text, answers):
    """text with the answers written into its table -> (new text, rows answered, rows). answers: {key: text}; the
    rows not named keep what they had. Only the rows whose answer changed are rewritten."""
    table = parse(text)
    unknown = set(answers) - {r["key"] for r in table["rows"]}
    if unknown:
        raise AgentSysError(
            _(
                "questions.md 中没有「{rows}」这一行，请刷新后重新填写",
                rows=_("、").join(sorted(unknown)),
            )
        )
    lines, col = table["lines"], table["answer_column"]
    filled = 0
    for r in table["rows"]:
        answer = str(answers.get(r["key"], r["answer"]) or "")
        filled += bool(answer.strip())
        if answer.strip() != r["answer"].strip():
            c = list(r["cells"])
            c[col] = escape(answer)
            lines[r["line"]] = f"| {' | '.join(c)} |"
    return "\n".join(lines), filled, len(table["rows"])


def read(t):
    """This round's questions.md as a table (GET …/questions). 404 if the round wrote none, 422 if it has no table
    with a 你的意见 column."""
    q = runs.round_questions(t)
    if not q:
        raise AgentSysError(_("本轮没有 questions.md"), 404)
    path = t.workspace / q["path"]
    table = parse(read_text(path))
    if table is None:
        raise AgentSysError(
            _("{path} 中没有含「{column}」列的表格，请查看原文", path=q["path"], column=ANSWER), 422
        )
    return {
        "path": q["path"],
        "mtime": iso(mtime(path)),
        "columns": table["columns"],
        "answer_column": table["answer_column"],
        "before": table["before"],
        "after": table["after"],
        "rows": [{k: r[k] for k in ("key", "cells", "answer")} for r in table["rows"]],
    }


def submit(home, pdir, tid, answers, base_mtime=""):
    """提交回答 (POST …/questions): write the answers into this round's questions.md and tell the agent, in the same
    session. base_mtime is the file's mtime when you started filling it in: if it changed since, nothing is written."""
    if not isinstance(answers, dict) or not all(isinstance(v, str) for v in answers.values()):
        raise AgentSysError(_("answers 应为「行: 回答」的键值表"))
    project, t = runs.fresh(pdir, tid)
    runs.check_can_run(project, t)
    if t.status != "needs_you":
        raise AgentSysError(
            _("任务当前为「{status}」，只有待确认的任务可以提交回答", status=status_word(t.status)),
            409,
        )
    if runs.budget_left(t) <= 0:
        raise AgentSysError(_("预算已用尽，请先追加预算"), 409)
    q = read(t)
    if base_mtime and base_mtime != q["mtime"]:
        raise AgentSysError(_("{path} 在你填写期间已被修改，请刷新后重新填写", path=q["path"]), 409)
    path = t.workspace / q["path"]
    text, filled, total = fill(read_text(path), answers)
    write_text(path, text)
    runs.message(
        home,
        pdir,
        tid,
        launch.say(
            launch.agent_language(home),
            "已在 {path} 中回答（已填写 {filled} / {total} 项），未填写的项请按你的建议处理。",
            path=q["path"],
            filled=filled,
            total=total,
        ),
    )
    return {"saved": q["path"], "delivery": "resume"}
