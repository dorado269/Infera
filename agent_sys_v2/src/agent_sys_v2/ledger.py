"""The ledger (design §5.6): the results a campaign's tasks measured, read into one table with its baseline and best.

A result is a JSON file in a task's workspace, results/*.json (or deeper under results/), with

  {"schema": "infera.result/v1", "label": "c64-spf", "metrics": {"tok_s_gpu": 14120.5, ...},
   "role": "baseline" | "candidate", "time": "2026-10-24T10:00:00Z", "change": "...", "workload": {...},
   "config": {...}, "verdict": {"status": "valid" | "void", "decision": "accept" | "reject" | "noise" | "rerun",
   "vs_baseline_pct": 15.4, "notes": "..."}, "evidence": {...}}

schema, label and metrics (numbers) are required. project.yaml says how to read them:

  ledger: {primary: tok_s_gpu, higher_is_better: true, baseline: experiment/b0}    # baseline: a task or a file

Without a baseline the latest valid result with role baseline is it. delta_pct is the primary metric against the
baseline; best is the best valid result that was not rejected. Files are read again only when they change.

  python -m agent_sys_v2.ledger check <dir or file> ... [--require-verdict]

checks result files (a task's acceptance check): exit 1 and the reasons when one is wrong or there is none.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .model import iso, plain

SCHEMA = "infera.result/v1"
DECISIONS = ("accept", "reject", "noise", "rerun")
STATUSES = ("valid", "void")
_cache = {}  # path -> ((mtime_ns, size), doc, errors)


def number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def validate(doc, require_verdict=False):
    """The reasons a result document is wrong, [] when it is right."""
    if not isinstance(doc, dict):
        return ["not a JSON object"]
    errors = []
    if not str(doc.get("schema") or "").startswith("infera.result/"):
        errors.append(f'schema should be "{SCHEMA}"')
    if not str(doc.get("label") or "").strip():
        errors.append("label is missing")
    metrics = doc.get("metrics")
    if not isinstance(metrics, dict) or not any(number(v) for v in metrics.values()):
        errors.append("metrics should hold at least one number")
    if doc.get("role", "candidate") not in ("baseline", "candidate"):
        errors.append("role should be baseline or candidate")
    verdict = doc.get("verdict")
    if verdict is not None and not isinstance(verdict, dict):
        errors.append("verdict should be an object")
    elif verdict:
        if verdict.get("status", "valid") not in STATUSES:
            errors.append(f"verdict.status should be one of {', '.join(STATUSES)}")
        if verdict.get("decision") not in (None, *DECISIONS):
            errors.append(f"verdict.decision should be one of {', '.join(DECISIONS)}")
    if (
        require_verdict
        and doc.get("role", "candidate") == "candidate"
        and not (verdict or {}).get("decision")
        and (verdict or {}).get("status") != "void"
    ):
        errors.append("verdict.decision is missing (a candidate needs the A/B decision, or void)")
    return errors


def load(path):
    """(doc, errors) of a result file, cached by mtime and size; doc None when it is not a result at all."""
    try:
        st = path.stat()
    except OSError:
        return None, []
    key = (st.st_mtime_ns, st.st_size)
    hit = _cache.get(path)
    if hit and hit[0] == key:
        return hit[1], hit[2]
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        doc, errors = None, [f"not JSON: {e}"]
    else:
        if not (
            isinstance(doc, dict) and str(doc.get("schema") or "").startswith("infera.result/")
        ):
            doc, errors = None, []  # some other JSON in results/: not ours to judge
        else:
            errors = validate(doc)
    _cache[path] = (key, doc, errors)
    return doc, errors


def files(workspace):
    root = workspace / "results"
    return sorted(p for p in root.rglob("*.json") if p.is_file()) if root.is_dir() else []


def settings(project):
    cfg = plain(project.config.get("ledger")) or {}
    cfg = cfg if isinstance(cfg, dict) else {}
    return {
        "primary": str(cfg.get("primary") or ""),
        "higher_is_better": cfg.get("higher_is_better", True) is not False,
        "baseline": str(cfg.get("baseline") or ""),
    }


def rows(project):
    """Every result of the project, oldest first, and the files that look like results but are wrong."""
    out, bad = [], []
    for t in project.tasks.values():
        for path in files(t.workspace):
            doc, errors = load(path)
            rel = str(path.relative_to(t.workspace))
            if errors:
                bad.append({"task": t.id, "file": rel, "errors": errors})
            if doc is None or errors:
                continue
            verdict = doc.get("verdict") if isinstance(doc.get("verdict"), dict) else {}
            out.append(
                {
                    "task": t.id,
                    "title": t.title,
                    "type": t.type,
                    "file": rel,
                    "label": str(doc["label"]),
                    "time": str(doc.get("time") or iso(path.stat().st_mtime)),
                    "role": doc.get("role", "candidate"),
                    "change": str(doc.get("change") or ""),
                    "metrics": {k: v for k, v in doc["metrics"].items() if number(v)},
                    "workload": doc.get("workload")
                    if isinstance(doc.get("workload"), dict)
                    else {},
                    "status": verdict.get("status", "valid"),
                    "decision": verdict.get("decision") or "",
                    "notes": str(verdict.get("notes") or ""),
                    "primary": str(doc.get("primary") or ""),
                }
            )
    return sorted(out, key=lambda r: (r["time"], r["task"], r["file"])), bad


def pick_baseline(table, cfg):
    pinned = cfg["baseline"]
    if pinned:
        named = [
            r
            for r in table
            if pinned
            in (r["task"], f"{r['task']}/{r['file']}", f"workspaces/{r['task']}/{r['file']}")
        ]
        if named:
            return named[-1]
    return next(
        (r for r in reversed(table) if r["role"] == "baseline" and r["status"] == "valid"), None
    )


def ledger(project):
    """The project's ledger: settings, the primary metric, baseline, best, rows with delta_pct, files in error."""
    cfg = settings(project)
    table, bad = rows(project)
    primary = (
        cfg["primary"]
        or next((r["primary"] for r in table if r["primary"]), "")
        or next((k for r in table for k in r["metrics"]), "")
    )
    sign = 1 if cfg["higher_is_better"] else -1
    base = pick_baseline(table, cfg)
    base_value = base["metrics"].get(primary) if base else None
    for r in table:
        r["value"] = r["metrics"].get(primary)
        r["baseline"] = r is base
        r["delta_pct"] = (
            round((r["value"] - base_value) / abs(base_value) * 100, 2)
            if number(r["value"]) and number(base_value) and base_value
            else None
        )
        r["better"] = r["delta_pct"] is not None and r["delta_pct"] * sign > 0
    eligible = [
        r
        for r in table
        if number(r["value"]) and r["status"] == "valid" and r["decision"] != "reject"
    ]
    best = max(eligible, key=lambda r: r["value"] * sign, default=None)
    keys = list(dict.fromkeys(k for r in table for k in r["metrics"]))
    return {
        "primary": primary,
        "higher_is_better": cfg["higher_is_better"],
        "baseline": brief_row(base),
        "best": brief_row(best),
        "count": len(table),
        "void": sum(r["status"] == "void" for r in table),
        "metrics": keys,
        "rows": table,
        "errors": bad,
    }


def brief_row(r):
    if not r:
        return None
    return {k: r[k] for k in ("task", "title", "file", "label", "value", "delta_pct", "time")}


def head(full):
    """What the board's head shows of a ledger: primary metric, baseline, best and counts, without the rows."""
    return {k: v for k, v in full.items() if k not in ("rows", "metrics")}


def task_briefs(full):
    """{task id: the result badge of its card}: how many results, the best delta among the valid ones, the latest
    decision, and how many of its files are wrong."""
    sign = 1 if full["higher_is_better"] else -1
    out = {}
    for r in full["rows"]:
        b = out.setdefault(
            r["task"], {"count": 0, "best_delta_pct": None, "baseline": False, "errors": 0}
        )
        b["count"] += 1
        b["baseline"] = b["baseline"] or r["baseline"]
        b["decision"], b["label"] = r["decision"], r["label"]
        d = r["delta_pct"]
        if (
            d is not None
            and r["status"] == "valid"
            and (b["best_delta_pct"] is None or d * sign > b["best_delta_pct"] * sign)
        ):
            b["best_delta_pct"] = d
    for e in full["errors"]:
        out.setdefault(
            e["task"], {"count": 0, "best_delta_pct": None, "baseline": False, "errors": 0}
        )
        out[e["task"]]["errors"] += 1
    return out


# ---------------------------------------------------------------- python -m agent_sys_v2.ledger check


def check(paths, require_verdict=False):
    """(all well, lines to print): there is at least one result file under paths, and every one is valid."""
    found, bad, lines = 0, 0, []
    for p in map(Path, paths):
        for f in sorted(p.rglob("*.json")) if p.is_dir() else [p] if p.exists() else []:
            try:
                doc = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
                lines.append(f"{f}: not JSON: {e}")
                bad += 1
                continue
            if not (
                isinstance(doc, dict) and str(doc.get("schema") or "").startswith("infera.result/")
            ):
                continue
            found += 1
            errors = validate(doc, require_verdict)
            bad += bool(errors)
            lines += [f"{f}: {e}" for e in errors] or [f"{f}: ok ({doc.get('label')})"]
    if not found:
        lines.append(f"no {SCHEMA} result under {', '.join(map(str, paths))}")
    return found > 0 and bad == 0, lines


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="python -m agent_sys_v2.ledger", description="Check infera.result files."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="exit 1 unless every result file under the paths is valid")
    c.add_argument("paths", nargs="+")
    c.add_argument(
        "--require-verdict", action="store_true", help="a candidate must carry verdict.decision"
    )
    args = ap.parse_args(argv)
    ok, lines = check(args.paths, args.require_verdict)
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
