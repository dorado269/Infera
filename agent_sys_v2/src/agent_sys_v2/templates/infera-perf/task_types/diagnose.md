---
review: true
checks:
  - test -s "$AGENT_SYS_WORKSPACE/report.md"
---
## 任务描述

- 对象：（基线任务，例如 @baseline/b0，或上一轮的实验）
- 问题：瓶颈在哪一层，下一轮最值得做的实验是什么

## 要做的事

- 按 skill infera-diagnose 多层分诊：指标与驻留、引擎日志、GPU trace、CPU profile；给瓶颈分类，结论带 `[M]`/`[I]`。
- 读台账（`workspaces/*/*/results/`）和已完成实验的报告，不重复已否决的方向。
- 列出按预期收益排序的假设；为前几个假设各起草一个 experiment 子任务（`tasks/experiment/<名字>.md`，frontmatter 写 parent、summary 和 machines），写明要改的变量、预期机理、预期幅度和判定方式。

## 交付

- `report.md`：以"## 结论"开头，写瓶颈分类、证据、假设清单和起草的子任务。
