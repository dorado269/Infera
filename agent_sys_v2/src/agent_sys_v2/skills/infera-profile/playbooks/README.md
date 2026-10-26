# Engine playbooks

One file per engine, `<engine>.md` (atom, sglang, vllm). A profile task in standard mode reads the one of its
campaign's engine; an onboarding task writes it. Sections, in this order:

1. **Control plane**: how to start and stop a trace on a live server (URL, body, roles), what blocks or crashes,
   how to tell the trace is on disk.
2. **Adapter**: the metric names and log lines for batch size, step time, queues (waiting / transfer / prealloc),
   KV use, prefix hits, MTP acceptance; how often they are emitted.
3. **Trace layout**: directory and file names per rank and role; how a step is cut (graph launches, ProfilerStep,
   engine labels); verify / draft phases with MTP.
4. **Kernel taxonomy**: the regular expressions of moe / attention / gemm / comm / norm / copy for this engine and
   model family.
5. **Benches**: the decode step bench (fixed batch) and the prefill bench for this engine, as commands.
6. **Known traps**, each with its symptom and the way around it.
7. **Provenance**: which campaign or onboarding task this came from, and when it was last checked.

| Engine | Playbook | State |
|---|---|---|
| ATOM | `atom.md` | TODO: to be distilled from the GLM-5.2 ATOM campaign's ultracode profile |
| SGLang | — | none yet: the first SGLang campaign runs an onboarding |
| vLLM | — | none yet: the first vLLM campaign runs an onboarding |
