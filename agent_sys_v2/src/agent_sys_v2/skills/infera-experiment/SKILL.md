---
name: infera-experiment
description: Run one hypothesis as a one-variable A/B on Infera — prove the lever is live, measure both arms alike, judge with scripts/ab_judge.py (noise never becomes an accept), and record the verdict
---

# infera-experiment

## Protocol

1. **Write the change down first**: which lever, from what to what, the mechanism you expect and its size, and
   which mechanism metrics should move (queue triple, step time, busiest-rank running count, hit rates, transfer
   queue ...). One variable per experiment.
2. **Dry run**: bring the candidate up and diff its live configuration against the baseline's
   (`infera-deploy`, `effective-config.json`). Only the intended difference may appear.
3. **Prove the lever is live** before measuring: the flag on the live command line or in `/get_server_info`, the
   patched file's hash inside the container plus a runtime marker for a code change. A green static check that
   the instrument has never been seen to fail proves nothing: when in doubt, show it red on a known-bad control.
4. **Measure both arms alike** (`infera-bench`): same load, slice, seed and client version; both fresh or both
   warmed; for a router-only change, A/B on one deployment and repeat in reverse order. Gate the candidate
   (`infera-quality-gate`).
5. **Judge** with the script (rules in its docstring: 2% noise band, 3% accept bar, mechanism must agree):

   ```bash
   "$AGENT_SYS_PYTHON" "$AGENT_SYS_SKILLS/infera-experiment/scripts/ab_judge.py" \
     --metric tok_s_gpu --baseline-files <baseline task>/results/c64.json \
     --candidate-files results/c64-spf.json --mechanism agree --write results/c64-spf.json
   ```

   `--lower-is-better` for latencies. `rerun` means: run another pair (or the mechanism evidence) before deciding.
6. **Void** instead of judging when the window was spoiled: foreign load on the GPUs, HSA out-of-resources,
   preemption, more than 10% failed requests: `--void "<reason>"`. A void result stays in the ledger, marked.

## Report

Start with the decision and the number (`+15.4% tok/s/GPU at C64, accept`), then the mechanism evidence
(`[M]` / `[I]`), the exact change (diff, flag or env), the run directories, and what the next experiment should
be. A rejected or noise result is a finding too: say what it rules out.
