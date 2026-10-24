---
review: false
checks:
  - test -s "$AGENT_SYS_WORKSPACE/report.md"
  - '"$AGENT_SYS_PYTHON" -m agent_sys_v2.ledger check "$AGENT_SYS_WORKSPACE/results"'
---
## 任务描述

- 部署：（kit、拓扑、镜像和分支，或"按项目规则"）
- 负载：（例如 AgentX C64，fast 模式先冒烟再跑全量）
- 机器：在任务配置里选

## 要做的事

- 按 skill infera-deploy 部署并审计生效配置，按 infera-quality-gate 过质量门，再按 infera-bench 测量。
- 写 `results/<标签>.json`，role 为 baseline；同一配置复测一次，给出噪声范围。

## 交付

- `report.md`：以"## 结论"开头，写基线数值、噪声范围、生效配置和复现命令。
