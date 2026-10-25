#!/usr/bin/env python3
"""Judge an A/B on one metric, the way the campaign rules say, and write the verdict into a result file.

  ab_judge.py --baseline 12010 11950 --candidate 12650 12700 --mechanism agree [--lower-is-better]
  ab_judge.py --metric tok_s_gpu --baseline-files b0.json --candidate-files c1.json c2.json --write c1.json

Rules (infera-experiment SKILL.md): the noise floor is the larger of a fixed band (--noise-pct, 2%) and the
run-to-run spread (1.96 x the relative standard error of the difference). A difference inside the floor is noise,
and noise never becomes an accept. A worse result beyond the floor is a reject. A gain between the floor and the
accept bar (--accept-pct, 3%) asks for another run; so does a gain whose median disagrees with its mean, or whose
mechanism metrics were not shown to agree (--mechanism). Only a gain past the bar with the mechanism agreeing is an
accept. --void REASON records a voided measurement (foreign load, HSA out-of-resources, preemption, > 10% failed
requests) without a decision. Prints the verdict as JSON; exit 0.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

DECISIONS = ("accept", "reject", "noise", "rerun")


def rsd_pct(xs):
    """Relative standard deviation in percent; 0 for a single value."""
    return statistics.stdev(xs) / abs(statistics.mean(xs)) * 100 if len(xs) > 1 else 0.0


def judge(base, cand, higher_is_better=True, mechanism="unknown", noise_pct=2.0, accept_pct=3.0):
    if not base or not cand:
        raise ValueError("both arms need at least one value")
    mb, mc = statistics.mean(base), statistics.mean(cand)
    if mb == 0:
        raise ValueError("the baseline's mean is 0: no relative difference")
    delta = (mc - mb) / abs(mb) * 100
    gain = delta if higher_is_better else -delta
    spread = 1.96 * math.sqrt(rsd_pct(base) ** 2 / len(base) + rsd_pct(cand) ** 2 / len(cand))
    floor = max(noise_pct, spread)
    median_agrees = (
        len(base) < 2
        or len(cand) < 2
        or (statistics.median(cand) - statistics.median(base)) * (mc - mb) > 0
    )
    notes = []
    if abs(gain) < floor:
        decision = "noise"
        notes.append(f"|{gain:+.2f}%| is inside the noise floor {floor:.2f}%")
    elif gain < 0:
        decision = "reject"
        notes.append(f"worse by {-gain:.2f}% (floor {floor:.2f}%)")
    elif gain < max(accept_pct, floor):
        decision = "rerun"
        notes.append(
            f"{gain:+.2f}% is between the noise floor and the {accept_pct:g}% bar: run again"
        )
    elif not median_agrees:
        decision = "rerun"
        notes.append("the medians disagree with the means: run again")
    elif mechanism != "agree":
        decision = "rerun"
        notes.append(
            "the mechanism metrics disagree: suspect the measurement"
            if mechanism == "disagree"
            else "show the mechanism metrics agree (queues, step time, hit rates ...) before accepting"
        )
    else:
        decision = "accept"
        notes.append(
            f"{gain:+.2f}% past the {accept_pct:g}% bar and the floor {floor:.2f}%, mechanism agrees"
        )
    return {
        "status": "valid",
        "decision": decision,
        "vs_baseline_pct": round(delta, 2),
        "noise_floor_pct": round(floor, 2),
        "n_baseline": len(base),
        "n_candidate": len(cand),
        "mean_baseline": mb,
        "mean_candidate": mc,
        "notes": "; ".join(notes),
    }


def values(files, metric):
    out = []
    for f in files:
        doc = json.loads(Path(f).read_text(encoding="utf-8"))
        v = (doc.get("metrics") or {}).get(metric)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise ValueError(f"{f}: metrics.{metric} is not a number")
        out.append(float(v))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--baseline", nargs="+", type=float, default=[])
    ap.add_argument("--candidate", nargs="+", type=float, default=[])
    ap.add_argument("--baseline-files", nargs="+", default=[])
    ap.add_argument("--candidate-files", nargs="+", default=[])
    ap.add_argument("--metric", help="the metric to read from result files (metrics.<name>)")
    ap.add_argument("--lower-is-better", action="store_true")
    ap.add_argument("--mechanism", choices=("agree", "disagree", "unknown"), default="unknown")
    ap.add_argument("--noise-pct", type=float, default=2.0)
    ap.add_argument("--accept-pct", type=float, default=3.0)
    ap.add_argument(
        "--void", metavar="REASON", help="the measurement is void: record why, decide nothing"
    )
    ap.add_argument("--write", metavar="RESULT", help="write the verdict into this result file")
    args = ap.parse_args(argv)
    if (args.baseline_files or args.candidate_files) and not args.metric:
        ap.error("--metric names the metric to read from the result files")
    try:
        base = args.baseline + values(args.baseline_files, args.metric)
        cand = args.candidate + values(args.candidate_files, args.metric)
        verdict = (
            {"status": "void", "notes": args.void}
            if args.void
            else judge(
                base,
                cand,
                not args.lower_is_better,
                args.mechanism,
                args.noise_pct,
                args.accept_pct,
            )
        )
    except ValueError as e:
        print(f"ab_judge: {e}", file=sys.stderr)
        return 2
    if args.write:
        path = Path(args.write)
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["verdict"] = verdict
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(verdict, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
