[English](README.md) | 中文

# agent_sys v2：Infera 性能工程的 agent 工作台

一名工程师在网页上指挥一组 Claude Code agent，找出并消除 Infera 部署的瓶颈。瓶颈可能在拓扑与并行、引擎参数、CPU 路径、通信、KV 传输或 kernel；每个结论都要有实测结果，每个 agent 都看得见、管得住。

- **任务 = prompt + 工作区。** agent 自己决定查什么，skills 提供可靠的工具。任务图在工作中长出来：agent 起草子任务，你一键批准整组计划。
- **核验证据，而不是限定步骤。** agent 说完成后，由系统执行验收检查，失败就把输出发回 agent。结果用统一格式写，进台账，显示基线、最佳值和趋势。
- **看得见，管得住。** 每次运行都有实时输出，注入了什么、花了多少、在等什么都看得到。可以发消息、停止、在终端接管、追加预算、审核。一台机器同一时间只给一个任务。

它和 [`../agent_sys`](../agent_sys)（v1，任务包运行时）并列，不改动 v1。两者可以装在同一个环境里：v1 的命令是 `agent-sys`，v2 的命令是 `agent-sys-v2`。产品、前端、后端设计和迁移计划见 [`docs/design.zh-CN.md`](docs/design.zh-CN.md)。执行模型和网页界面移植自 conch v1.0.1。

## 前提

- Linux、Python 3.10+、`tmux`。
- 能正常使用的 Claude Code 配置 `~/.claude/settings.json`（认证、网关、模型映射）。运行用的是 `claude-agent-sdk`（版本固定）自带的 `claude` CLI，加上你的用户配置，不需要别的设置。

## 安装

在 Infera 仓库根目录，进入任意虚拟环境：

```bash
python3 -m venv ~/.venvs/agent-sys-v2 && . ~/.venvs/agent-sys-v2/bin/activate
pip install -e agent_sys_v2            # 也可以 uv pip install -e agent_sys_v2；要跑测试加 [dev]
agent-sys-v2 --version
```

## 启动

```bash
agent-sys-v2 serve
```

- server 放在 tmux 窗口里（`tmux -L agent_sys` 的 `server` 窗口），**只监听 Unix socket** `/tmp/agent_sys-<uid>/agent_sys.sock`，所在目录只有你能进入。启动时会打印位置和下一步该敲的命令。
- 在你自己的电脑上转发端口，然后用浏览器打开：

  ```bash
  ssh -N -L 8765:/tmp/agent_sys-<uid>/agent_sys.sock <你>@<这台机器>
  # 浏览器打开 http://localhost:8765
  ```

- 看日志：`tmux -L agent_sys attach -t agent_sys:server`。
- 停止：在这个窗口里按 Ctrl-C，或者 `tmux -L agent_sys kill-window -t agent_sys:server`。重启 server 不影响正在跑的任务：每次运行都是同一个 tmux 里的独立进程，server 起来后会从文件里接上它们。
- 其他选项：
  - `--foreground`：在当前终端运行；
  - `--home DIR`（或环境变量 `AGENT_SYS_HOME`）：换一个 home；
  - `--sock PATH`：换一个 socket；
  - `AGENT_SYS_TMUX=<名字>`：换一个 tmux server，用于在正式 server 旁边再开一个。

所有东西都在 `AGENT_SYS_HOME` 里，默认 `~/agent_sys_home`，第一次使用时自动创建：

```
agent_sys.yaml  rules/  knowledge/                     全局设置、规则、经验
campaigns/<c>/project.yaml                            目标、机器、台账主指标、skills、agent 默认值
campaigns/<c>/rules/project.md                        campaign 背景，每次运行都注入
campaigns/<c>/tasks/<类型>/<名字>.md                    任务 prompt（含 frontmatter）
campaigns/<c>/workspaces/<类型>/<名字>/                 progress.md、report.md、results/*.json、.agent_sys/runs/
campaigns/<c>/repos/                                  agent 要改的代码（不扫描）
```

## 第一个 campaign

1. 新建 campaign：在首页**新建项目**，模板选 **Infera 性能优化**（`infera-perf`）；或者用命令行 `agent-sys-v2 new glm52-agentx --template infera-perf --title "GLM-5.2 4P4D AgentX"`。
2. 在 campaign 的**设置**里填目标、机器（每行一台：`主机名 GPU数 备注`）和台账主指标（默认 `tok_s_gpu`）。
3. 写 `campaigns/<c>/rules/project.md`：模型、引擎、kit、负载、用哪个分支。再把 Infera clone 到 `campaigns/<c>/repos/Infera`：agent 在 campaign 目录下工作，skills 里引用的是 `repos/Infera/examples/<kit>`。
4. **新建任务**，类型选 `baseline`，勾上要用的机器，点**创建并排队**，任务会立即开始。
   - 在**运行输出**里看进展；
   - 用底部输入框给 agent 发消息，运行中也可以；
   - 需要时点**停止**，或**在终端中接管**。
5. 基线完成后，新建一个依赖它的 `diagnose` 任务。它会写报告，并起草几个 `experiment` 子任务；在它的详情里点**全部纳入托管**，一次批准。
   - 每个实验占用自己的机器，其他任务等机器空出来再开始。
6. **台账**页显示所有结果相对基线的变化。agent 说完成了但验收检查没过的任务，会出现在**待确认**里，附带检查的输出。

任务文件示例，标出了 v2 新增的字段：

```markdown
---
title: SPF scheduling at C64
depends_on: [baseline/b0]
machines: [smc-a16-19]            # 运行中和待确认时占用
checks:                           # agent 把 progress.md 标为已完成后执行；失败就发回 agent
  - test -s "$AGENT_SYS_WORKSPACE/report.md"
  - '"$AGENT_SYS_PYTHON" -m agent_sys_v2.ledger check "$AGENT_SYS_WORKSPACE/results" --require-verdict'
check_retries: 2
agent: {model: opus, effort: high, budget_usd: 50}
---
## 任务描述
……
```

结果文件是 `results/<标签>.json`，格式为 `infera.result/v1`（见 [`src/agent_sys_v2/skills/infera-bench/SKILL.md`](src/agent_sys_v2/skills/infera-bench/SKILL.md)）。A/B 判定由 `src/agent_sys_v2/skills/infera-experiment/scripts/ab_judge.py` 写入。

## 不调用模型先看看

```bash
python agent_sys_v2/tools/demo.py /tmp/$USER/agent_sys_demo
AGENT_SYS_TMUX=asys-demo agent-sys-v2 serve --home /tmp/$USER/agent_sys_demo --sock /tmp/agent_sys-$(id -u)/demo.sock
ssh -N -L 8766:/tmp/agent_sys-<uid>/demo.sock <你>@<这台机器>     # 在你的电脑上；浏览器开 http://localhost:8766
```

这是一个演示 campaign：3 台机器，任务处在各种状态，有台账，也有一组起草好的计划。它的 `project.yaml` 设置了 `agent.replay`，每次运行都回放一段录好的会话（`tests/fixtures/events/`），不调用模型。用 `tmux -L asys-demo kill-server` 停止。

## 开发

```bash
pip install -e "agent_sys_v2[dev]"
cd agent_sys_v2 && python -m pytest tests -q     # 3–4 分钟，需要 tmux；测试都不调用模型
node --test src/agent_sys_v2/web/test/*.test.js  # 前端的纯函数和英文词典
ruff format --check . && ruff check .            # 仓库的 ruff 配置
```

代码结构见英文版 [README](README.md#develop) 的表格。

## 常见问题

- 提示 *socket 所在的目录其他用户也能进入*：把 `--sock` 指到一个只有你能进入的目录。
- 提示 *已有 server 在运行*：一个 `AGENT_SYS_HOME` 只能有一个 server，否则两个调度器会把同一个任务启动两次。
- 待开始的任务迟迟不开始：卡片上写着原因，可能是上游未完成、机器被别的任务占用、名额已满，或关闭了自动开始。
- 运行用的是 `~/.claude/settings.json`。server 会去掉启动它的 shell 里的 `CLAUDE*` / `ANTHROPIC*` 变量，所以即使在 Claude Code 会话里启动 server，用的也是你的用户配置。
- v1 也读 `AGENT_SYS_HOME`（作为它的状态前缀）。如果你为 v1 设置了它，v2 会在同一个目录下建自己的子目录，两者互不干扰。
