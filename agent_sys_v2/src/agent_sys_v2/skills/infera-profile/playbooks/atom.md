# ATOM playbook

TODO: distil the GLM-5.2 ATOM campaign's ultracode profile (investigate_atom_profile_method: its reports, `dstep_bench.py`,
`kbreak.py` / `kdiff.py`, the trace window control, the time account) into the sections of `README.md`. Until then a
standard profile on ATOM reads that campaign's reports directly and notes what it had to work out itself, so the
distillation has its material.

Known so far (from the design review of that campaign):

- The torch profiler is switched on only by the CLI `--torch-profiler-dir` (`ATOM_TORCH_PROFILER_DIR` is overridden);
  stop blocks the API synchronously; turning it on for a PP prefill crashes the engine (heap corruption).
- Trace directories are `pp{k}_rank_0` / `rank_{r}`; a step with MTP K3 has three graph launches; phases are cut on
  the `ATOM::forward`, `decode[...]`, `propose_eagle[i/K]` labels.
- Metrics: `atom:decode_batch_size`, `atom:gpu_forward_seconds`, `atom:decode_context_tokens`; workers are not
  federated: scrape each worker's `/metrics`.
