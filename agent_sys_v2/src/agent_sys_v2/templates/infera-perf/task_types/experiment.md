---
review: false
checks:
  - test -s "$AGENT_SYS_WORKSPACE/report.md"
  - '"$AGENT_SYS_PYTHON" -m agent_sys_v2.ledger check "$AGENT_SYS_WORKSPACE/results" --require-verdict'
---
## 任务描述

- 假设：（瓶颈是什么，改哪个变量，预期机理和幅度）
- 对照：（基线结果，例如 @baseline/b0）
- 机器：在任务配置里选

## 要做的事

- 按 skill infera-experiment：只改一个变量，dry-run 对比生效配置，证明杠杆生效，再做 A/B。
- 用 `ab_judge.py` 判定，写进结果的 verdict；作废的也要写。

## 交付

- `report.md`：以"## 结论"开头：接受 / 拒绝 / 噪声，数值和机理证据，改动的 diff 或命令。
