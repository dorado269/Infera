---
name: infera-bench
description: Measure one point (AgentX or a fixed ISL/OSL load) on a live deployment and write it as an infera.result/v1 file the ledger reads
---

# infera-bench

## Pick the load

| Load | How | Cost |
|---|---|---|
| InferenceX AgentX (the campaign's main metric) | `engine/agentx/agentx.sh` of the ATOM kit; client pinned (InferenceX `918524ff`, AIPerf 0.12.0, `constraints.txt`) | warm-up 13-25 min + `DURATION=3600` |
| AgentX fast (screening) | same, `AIPERF_EXPERIMENTAL_FAST=1` (1200 s, one warm-up per lane) | ~25 min |
| fixed ISL/OSL | `python -m sglang.bench_serving --backend sglang-oai --dataset-name random --random-range-ratio 1.0 --num-prompts 10xC --request-rate inf`; vLLM: `bench/serve_standard/bench_vllm_serve.sh` | minutes |

Screen with the cheap one, confirm with the real one. Sweep concurrency from high to low (C176 -> C128 -> C96 ...,
about x0.7) so the most informative point lands first.

## Traps that produce plausible wrong numbers

- AIPerf exits 0 on an empty window: check the request count; count errors per request, not from the summary.
- Streaming must be on, or there is no TTFT/ITL. `--stream-interval > 1` makes ITL = interval x TPOT: read TPOT.
- A fresh stack measures ~10% below a warm one: compare like with like (both fresh, or warm-up then score).
- Reuse the same trace slice and seed for both arms; flush caches (`POST /flush_cache`, decode first, retry while
  prefill answers 400) only when nothing is in flight.
- The random dataset has no prefix reuse: it cannot show kv-aware routing or KV tiers.
- Performance runs may use forced MTP acceptance (`SGLANG_SIMULATE_ACC_LEN`, `MTP_AL`): their output is wrong by
  design; correctness is `infera-quality-gate`'s job on a stack with real acceptance.
- TP > 1: sum engine counters across ranks.

## Record the point

Per point keep: the run directory (`engine-config.env`, `workers.json`, `effective-config.json`), the client's
command (`benchmark_command.txt` / `replay-command.txt`), the raw client output (`agentx_conc<C>.json`,
`profile_export_aiperf.json`), `/metrics` before and after. Then write **one result file**, flat in the task's
`results/` (the ledger notices new files there):

```json
{"schema": "infera.result/v1", "label": "c64-spf", "time": "<ISO, end of the window>",
 "role": "baseline | candidate",
 "change": "one line: what differs from the baseline",
 "metrics": {"tok_s_gpu": 14120.5, "p90_interactivity": 31.2, "ttft_p90_s": 2.1, "itl_p90_ms": 32.0,
             "requests": 3120, "errors": 0},
 "workload": {"client": "agentx", "client_version": "InferenceX@918524ff/aiperf 0.12.0", "conc": 64,
              "duration_s": 3600, "seed": 42, "forced_acceptance": 3.61},
 "config": {"kit": "atom_4p4d_glm5.2", "image": "<tag@digest>", "run_dir": "<path>"},
 "evidence": {"run_dir": "<path>", "metrics_pre": "<path>", "metrics_post": "<path>"}}
```

`tok_s_gpu` is the InferenceX definition: (input tokens including cache hits + output tokens) / duration / all P
and D GPUs. `p90_interactivity` is 1 / p90 ITL (tok/s/user). Use the campaign's `ledger.primary` as the key of the
main metric. Check the file before you report: `"$AGENT_SYS_PYTHON" -m agent_sys_v2.ledger check results/`.
A candidate's `verdict` comes from `infera-experiment`.
