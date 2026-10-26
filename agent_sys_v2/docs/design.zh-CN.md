# agent_sys v2 设计：产品、前端与后端

| | |
|---|---|
| 状态 | 设计稿 + MVP（分支 `dev/yaoc/agent_system`） |
| 范围 | 产品定位、核心循环、前端、后端、Infera 性能工作包、MVP、迁移 |
| 来源 | 前端和执行模型移植自 conch v1.0.1（同一作者），按 Infera 性能工作的需要增删 |

## 0. 一页概览

agent_sys v2 是 Infera 的**性能工程 agent 工作台**。一名工程师在网页上指挥一组 Claude Code agent，在 GPU 集群上找出并消除 Infera 部署的瓶颈。瓶颈可以在任何一层：拓扑与并行、引擎参数、CPU 路径、通信、KV 传输、kernel。每个结论都要有实测证据，每个 agent 都看得见、管得住。

```
 浏览器 ──ssh -L──► agent-sys-v2 serve（FastAPI，Unix socket，tmux 窗口）
                     │  扫描文件 → 内存模型 → REST + SSE
                     │  调度：依赖、并发名额、机器占用
                     │  跟踪：每 0.5 s 读运行中任务的 events.jsonl
                     ▼
              每次运行一个 runner 进程（tmux 窗口）
                     │  claude-agent-sdk → claude CLI（每个任务一个会话，多轮 resume）
                     │  与 server 只通过 run 目录里的文件通信
                     │  agent 报告完成后：执行验收检查，失败就把输出发回同一会话
                     ▼
     campaigns/<c>/tasks/*.md  ·  workspaces/<任务>/{progress.md, report.md, results/*.json, .agent_sys/}
```

和旧 agent_sys（任务包运行时）相比：

| | 旧（任务包） | v2 |
|---|---|---|
| 工作流 | 事先声明好的静态 DAG（YAML，5 类对象） | 一个任务就是一份 prompt。图由人和 agent 在过程中长出来：agent 起草子任务，人批准 |
| agent 的角色 | 照着 brief 执行步骤（"do not invent a method"） | 自己决定查什么、试什么；skills 提供可靠的工具和方法 |
| 质量保证 | handoff schema + validator，管的是格式 | 实测结果台账 + A/B 判定规则 + 验收检查，管的是结论 |
| 可观测 | 状态变化时打一行；agent 消息丢弃 | 每个 agent 的实时输出、花费、当前命令、后台等待、机器占用、实验趋势 |
| 可控 | 只能 Ctrl-C 结束整个 run | 对单个任务发消息、停止、终端接管、追加预算、审核、回答问题 |
| 规模 | 32.5K 行 Python（可执行 14.3K），25.5K 行文档，56.5K 行测试 | 7.5K 行 Python（可执行约 5.8K），3.4K 行 JS（另有 0.8K 行英文词典、0.7K 行 CSS），4.9K 行测试 |

## 1. 定位与原则

**定位**：一名工程师加一群 agent 的工作台，人只做决策和验收。它不是给人用的项目管理工具，也不是自动驾驶的优化器。

**原则**：

1. **用户体验优先。** 需要人处理的事集中在一个页面，一行一件，带好主按钮。新建任务就是写一段话，不用学一套 YAML 本体论。界面和文件始终一致；不开界面，也能照同样的约定工作。
2. **信任 agent，核验证据。** 不限定 agent 的步骤；限定的是证据的标准，包括结果格式、A/B 判定规则和验收检查，并且由 harness 而不是 agent 自己去执行检查。保留旧系统里两条站得住的洞见：昂贵的 gate 前先放便宜的检查；有意思的失败不是崩溃。
3. **可观测。** 运行的每一帧都落盘（`events.jsonl`），界面实时展示。注入了什么、花了多少、在等什么，都写在 `launch.md` 和卡片上。
4. **可控。** 任何一个 agent 都能单独发消息、停止、接管、加预算、审核。机器占用由调度器管，不靠口头约定。
5. **简单。** 文件就是状态，server 可以随时重启。一个进程模型，一套状态词汇。新功能先问：能不能让产出更可信？能不能省人的时间？

## 2. 用户与核心循环

**用户**：
- **性能工程师**（主要用户）：给定模型 × 硬件 × 引擎 × 拓扑 × 负载，把 goodput 提上去。节点稀缺，希望几个实验并行；不想守着屏幕，早上来看结果，而不是看到一台闲置了一夜的机器。
- **评审者**：看结论和证据，决定哪些改动进 overlay 补丁或 PR。
- **团队**：skills、经验库和基线在 campaign 之间共享。

**核心循环**（模板 `infera-perf`，按轮推进；第 1 轮的 5 个任务由模板在建项目时预置）：

```
 ① intake/rN       预填问卷 → 你回答 → 写 campaign 的规则、目标、机器、工作点 → brief（审核）
        │
 ② survey/rN ∥ baseline/r1   对标曲线进台账（reference）、配置差异表 ∥ v0 曲线、噪声、快速验证标定
        │
 ③ profile/rN      从便宜的层做起：时间账 → 资源利用 → 只对瓶颈层做 kernel / CPU → candidates.yaml（审核）
        │
 ④ plan/rN         候选排序、GPU 预算和机器分配（审核）→ 起草 experiment 子任务和 close/rN
        │
 ⑤ experiment × N  一个大杠杆或一组同层小杠杆：快速验证迭代 → 工作点单点 A/B → 判定；按机器并行
        │
 ⑥ close/rN        depends_on {plan/rN, kind: children}：子任务全部完成或取消后开始；集成 → 整条曲线 →
                   封版 vN（不劣于 v(N−1) 时）→ 复盘 → 起草 intake/r(N+1)（带建议的增量问卷）
```

和固定流水线的区别：③决定④⑤做什么，⑤⑥的结果又决定下一轮。kernel 优化只是③的一种，跟 tokenizer workers、调度策略、P:D 比、Mooncake 参数一视同仁，由②的证据决定先做哪个。

## 3. 概念模型

一共 7 个概念，每个都落在文件上：

| 概念 | 是什么 | 文件 |
|---|---|---|
| **Campaign**（项目） | 一次优化活动：目标、机器、台账配置、规则、经验 | `campaigns/<c>/project.yaml`、`rules/`、`knowledge/` |
| **Task** | 一份 prompt 加一个工作区；任务类型就是看板上的泳道 | `tasks/<类型>/<名字>.md`、`workspaces/<类型>/<名字>/` |
| **Run** | 任务会话的一轮：启动参数、注入内容、全部事件、结束记录 | `workspaces/…/.agent_sys/runs/<run>/` |
| **Check** | agent 报告完成后由 harness 执行的验收命令 | task frontmatter 的 `checks`；结果在 `runs/<run>/checks.json` |
| **Result** | 一个测量点，统一 schema | `workspaces/…/results/**/*.json`（`schema: infera.result/v1`） |
| **Machine** | campaign 可用的节点；任务声明要用哪些，调度器保证互斥 | `project.yaml` 的 `machines`；task frontmatter 的 `machines` |
| **Skill** | 可复用的操作手册和脚本，例如部署、测量、分诊、A/B | `src/agent_sys_v2/skills/<名字>/SKILL.md`；campaign 自己的放 `campaigns/<c>/skills/` |

另外两个是从 conch 继承的约定：**规则**（每次运行都注入）和**经验**（任务写出候选，人批准后进经验库，经验库的索引会注入）。

### 3.1 任务状态

沿用 conch 的词汇。每个状态都用形状、文字、颜色三样同时表示。

| 状态 | 含义 |
|---|---|
| 草稿 draft | 有 prompt，未托管（agent 起草的子任务也在这里） |
| 待开始 queued | 等上游、等你审核模板、等名额，或**等机器**（卡片上写明被谁占着） |
| 运行中 running | 执行中 / 等待中（后台命令在等作业，不花 token）/ 终端接管 |
| 待确认 needs_you | agent 提问、`questions.md`、被停止、运行中断、预算用完，或**验收未通过** |
| 待审核 in_review | 完成了，且设置了 `review: true` |
| 已完成 done | 新完成的任务带"未查看"标记 |
| 已取消 cancelled | |

"已完成"由两件事共同决定：agent 在 `progress.md` 里写了已完成，并且 `checks` 全部通过。没有配置检查的任务只看第一条。

### 3.2 机器占用规则

- 托管任务从开始运行起占用它声明的机器，直到进入待审核、已完成或已取消。
- 在运行中和待确认时都算占用：等人回复期间，部署往往还在节点上。
- 一个待开始的任务，只要它要的机器有一台被占用，就不会被启动；卡片写明"等机器：node-a（被 experiment/e3 占用）"。手动"开始"也会被拒绝并说明原因。要越过这条规则，就改任务的机器列表。
- 占着机器又停在待确认的任务，首页和看板会标出"闲置占用 2 小时"。这是对"agent 睡着、节点空转一夜"的直接回应。

## 4. 前端设计

技术：Preact + htm，不需要构建，ES module 直接加载（移植自 conch）。同源，CSP 只放行 `'self'`。只有一条 SSE 推送变化，运行输出按通知去取增量。中英双语，默认暗色。

### 4.1 页面

| 页面 | 路由 | 内容 |
|---|---|---|
| **首页** | `#/` | 与 conch 一致：顶部三张卡片——项目列表（一行一个，带待确认、待审核、**待审核模板**等标记；"新建项目"收在卡片标题旁的按钮里）、新建任务（选项目后在首页打开新建任务抽屉，快捷键 n）、运行中（附各项目今日花费）；下面是**机器**（所有 campaign 的占用和闲置告警），然后是待确认卡片（一行一件，按原因给主按钮：回复 / 回答 / 查看检查 / 追加预算 / 继续）、待审核、最近完成，空的队列折叠成一行 |
| **看板** | `#/p/<c>` | 头部：标题、**目标**、**基线和当前最佳（Δ%）**、自动开始、名额、新建任务。模板预置的任务还没开始时，头部下面是**待审核模板**：按执行顺序列出这些任务、项目规则和设置的入口，以及"开始运行"。**机器条**：每台机器显示被哪个任务占着、占了多久、占用者的状态。待处理摘要、搜索和筛选。泳道 × 日期的依赖图（按执行顺序从左上到右下：上游总在下游之上，同时创建的任务按依赖深度和泳道排；依赖线是只走泳道之间和行之间空隙的直角折线，同一空隙里的线各占一条轨道并按少交叉排序，可由其他依赖推出的产出、代码基线依赖默认不画，"视图"里或图上方可切换为全部显示），卡片上有状态、机器、检查 ✓/✗、结果 Δ%。下方是任务列表 |
| **台账** | `#/p/<c>/ledger` | 头部：主指标及方向、基线值、最佳值和 Δ%、结果数（有效/作废）。趋势图：主指标随时间变化，基线虚线，最佳值阶梯线，点的颜色表示判定（接受 / 拒绝 / 噪声 / 作废）。表格：时间、任务、标签、改动、主指标、Δ、其他指标、判定。点一行打开任务抽屉，定位到这个结果文件 |
| **任务抽屉** | `?t=<c>/<类型>/<名字>` | 头部：状态和原因、标题、花费/预算、模型、进度（已勾选/全部）、**机器**、**检查摘要**。三个 tab：**运行输出**（实时，检查的过程和结果也在流里）；**工作区文件**（报告审核、questions.md 表单、交接清单、结果文件渲染成指标表）；**任务配置**（prompt、上游任务、机器、检查、agent 配置）。底部一个输入框：运行中发的消息在当前命令后读到，其他状态发消息就接着跑。另有停止、在终端中接管、追加预算 |
| **新建任务** | `?new=1` | 选类型（prompt 按类型骨架预填，检查也一起预填），选上游任务，选机器，设审核、预算、模型和 effort |
| **规则 / 经验 / 设置** | `#/rules` 等 | 规则页显示每次注入的各段及大小，包括 **skills 索引**；经验页处理候选的采纳和丢弃；设置页 |

### 4.2 交互要点

- **agent 起草的子任务**：显示为草稿卡片，挂在父任务下。父任务抽屉里有"子任务草稿 N 个，全部纳入托管"，一次批准一组；也可以逐个修改后再批准。
- **验收未通过**：卡片主按钮是"查看检查"，打开运行输出并定位到检查结果。回复"继续"后，agent 在同一会话里接着修。
- **机器冲突**：卡片的等待原因直接链接到占用者。占用者在待确认时，首页的那一行带"闲置占用"标签。
- **台账和看板互通**：卡片上的 Δ% 链到台账的对应行，台账的行打开任务抽屉。
- 不做：燃尽图、故事点、多用户权限。有价值的指标是人的等待时间、返工率、花费，见第 10 节。

### 4.3 视觉

沿用 conch 的 token，logo 和动效是自己的：
- 页面底色带一点冷绿；
- 状态色：待确认橙、运行中蓝、待审核紫、完成绿；
- logo：圆角方块里一条带节点的折线逐级上升（一串任务把指标推高），也是 favicon；
- 卡片左侧色条标状态；唯一会动的是运行中图标（一段弧绕着中心点转）；首屏加载时 logo 的折线自己画出来，页面内加载是一条扫过的细光带；遵守 `prefers-reduced-motion`。

台账的颜色只有四种：接受用完成绿，拒绝用警告红，噪声用灰，作废是空心点。

## 5. 后端设计

### 5.1 进程

| 进程 | 职责 | 生命周期 |
|---|---|---|
| `agent-sys-v2 serve` | 扫描、调度、跟踪运行、REST + SSE、提供前端 | 在 tmux `-L agent_sys` 的 server 窗口里。可随时重启，正在跑的任务不受影响 |
| runner（`python -m agent_sys_v2.runner <run_dir>`） | 一轮会话：启动或恢复 claude 会话，转发消息和停止指令，记录事件；会话空闲且 agent 报告完成时执行检查 | 每轮一个，各自在一个 tmux 窗口里；会话空闲、没有后台命令、收件箱已读完时退出 |
| 终端接管 | 在 tmux 窗口里对同一会话开交互式 claude | 用户退出 claude 后，server 按 progress.md 接回 |

server 和 runner 只通过 run 目录里的文件交换信息：

| 文件 | 写入方 | 内容 |
|---|---|---|
| `launch.json` | server | 启动参数，以及要执行的检查 |
| `system.md` | server | 注入内容 |
| `launch.md` | server | 给人看的注入清单和等价命令 |
| `inbox.jsonl` | server 追加 | 用户消息、停止指令 |
| `events.jsonl` | runner | CLI 的原始帧，加上 runner 自己的事件 |
| `run.json` | runner | 开始记录和结束记录 |
| `checks.json` | runner | 每一轮检查的结果 |

### 5.2 文件布局

```
$AGENT_SYS_HOME/                       默认 ~/agent_sys_home
  agent_sys.yaml                       全局配置（语言、并发上限、claude 路径）
  rules/*.md                           全局规则
  knowledge/{INDEX.md,entries/}        全局经验库
  campaigns/<c>/
    project.yaml                       目标、机器、台账、skills、agent 默认值、自动开始、并发、泳道顺序
    rules/*.md  knowledge/  skills/    campaign 的规则、经验、自带 skills
    task_types/<类型>.md                prompt 骨架（frontmatter 可带默认的 review、checks）
    tasks/<类型>/<任务>.md               任务 prompt（frontmatter：title, summary, depends_on, parent, review, agent, machines, checks）
    workspaces/<类型>/<任务>/
      progress.md report.md issue.md questions.md lessons/ log/ results/
      .agent_sys/state.json            托管状态（只有系统写）
      .agent_sys/runs/<run>/           一轮的完整记录
    repos/                             代码仓库，不扫描
```

`project.yaml` 示例：

```yaml
title: GLM-5.2 4P4D AgentX
template: infera-perf
goal: "tok/s/GPU ↑ at AgentX C64, P90 interactivity ≥ 30 tok/s/user"
machines:
  - {name: smc-a16-19, gpus: 8, note: MI300X}
  - smc-a16-20
ledger: {primary: tok_s_gpu, higher_is_better: true}
skills: [infera-deploy, infera-bench, infera-diagnose, infera-experiment, infera-quality-gate, infera-hygiene]
autorun: true
max_running: 3
agent: {model: opus, effort: high, budget_usd: 200}
```

### 5.3 模块

一个 Python 包 `agent_sys_v2`（目录 `Infera/agent_sys_v2/`，与 v1 的 `agent_sys/` 并列），从 conch 移植。

| 模块 | 职责 | 来源 |
|---|---|---|
| `model.py` | 数据类型（Task、Project、Edge）、frontmatter 读写、state.json、状态推导 | conch，加 machines/checks 字段 |
| `scan.py` | 扫描一个 campaign：任务、依赖边、等待原因、记录提醒；Watcher 按 stat 发现变化 | conch，加"等机器"原因 |
| `ops.py` | 写操作：新建项目、类型、任务，改名，删除，审核，纳入托管 | conch，加"批量纳入子任务草稿" |
| `launch.py` | system.md（约定、规则、经验索引、**skills 索引**）、首条消息、SDK 参数、launch.md | conch，加 skills 和机器说明 |
| `runner.py` | 一轮会话；**检查的执行和反馈** | conch，加检查 |
| `runs.py` | 开始、消息、停止、接管、预算、一轮结束后的状态迁移；Monitor | conch，加"验收未通过" |
| `sched.py` | 启动待开始的任务：依赖、名额、**机器** | conch，加机器 |
| `checks.py` | 解析 checks、执行（超时、输出截断）、汇总 | 新增 |
| `ledger.py` | 扫描结果文件、校验 schema、算 Δ 和最佳值；`python -m agent_sys_v2.ledger check <dir>` 也可以当作检查命令用 | 新增 |
| `machines.py` | 解析机器列表；占用表；闲置占用 | 新增 |
| `files.py`、`questions.py`、`knowledge.py`、`i18n.py` | 工作区文件、questions.md 表单、规则与经验、提示信息的英文 | conch |
| `server.py`、`cli.py` | REST + SSE；`agent-sys-v2 serve / new / --version` | conch，加 ledger 和 machines |

从 conch 去掉的：
- 新建任务时用 Haiku 解析依赖建议（`parse.py`）：准确度不够，README 里也写了"只作参考"；
- 版本与里程碑（`milestone.py`）：campaign 本身就是一个边界；后续需要时再加。

### 5.4 一轮运行和验收检查

```
start_run → 写 run 目录 → tmux 开 runner
runner：connect / resume → 发首条消息或你的消息 → 转发 inbox → 记录帧
  会话空闲、没有后台命令、收件箱已读完：
    progress.md 没写"已完成"         → 退出（server 判为待确认：提问 / 有问题待答）
    写了"已完成"，且有检查            → 逐条执行检查（事件 check_start / check_end 流进运行输出）
        全部通过                      → 退出（server 判为已完成或待审核）
        有失败，且还有重试次数         → 把失败的检查和输出尾部作为一条消息发回同一会话，继续等它下一轮空闲
        有失败，且重试次数用完         → 退出（server 判为待确认 · 验收未通过）
```

- 检查用 `bash -lc` 执行，cwd 是 campaign 目录。环境变量有 `AGENT_SYS_TASK`、`AGENT_SYS_WORKSPACE`、`AGENT_SYS_PROJECT`、`AGENT_SYS_SKILLS`、`AGENT_SYS_MACHINES`。默认超时 600 s；输出最多留 4000 字符。
- 重试次数：frontmatter 的 `check_retries`，默认 2。发回 agent 的消息写明哪条检查失败、退出码和输出尾部，并要求修好后再标记完成。
- 检查在 runner 里执行，不占 server 的锁，过程也在运行输出里。结束记录写在 `checks.json`，任务抽屉和卡片都读它。
- 终端接管结束时，如果 progress 已完成，就由 server 用同一个函数执行一次检查，不重试。

写法：

```yaml
checks:
  - test -s "$AGENT_SYS_WORKSPACE/report.md"
  - {name: results, run: 'python -m agent_sys_v2.ledger check "$AGENT_SYS_WORKSPACE/results" --require-verdict', timeout: 60}
check_retries: 2
```

### 5.5 调度

每 2 s，扫描完之后执行一次。待开始的任务按以下顺序取第一个成立的等待原因：

1. 项目已归档
2. 阻塞类上游未完成
3. 自动开始已关闭
4. **机器被占用**
5. 项目名额已满
6. 全局名额已满

没有等待原因的任务，按等待时间从长到短启动。运行中的任务占一个名额，在后台等待和终端接管期间也算。

### 5.6 台账

- 扫描 `workspaces/*/*/results/**/*.json` 中 `schema` 以 `infera.result/` 开头的文件，按 mtime 缓存，请求时才算。
- 基线：`project.yaml` 的 `ledger.baseline`（某个结果文件的路径）；没有配置时，取最新的一个 `role: baseline` 且有效的结果。
- Δ = 主指标相对基线的百分比，方向由 `higher_is_better` 决定。有基线系列时，带并发（`workload.conc`）的结果和基线系列同一并发的点比。最佳值只在我们自己（不含 `role: reference`）有效且未被拒绝的结果里取。
- **曲线**：结果带 `round`（轮次）和 `series`（曲线名：`v0`、`r2-spf`、`ix:<条目>`）；`role: reference` 是别人的数（对标）。`ledger.x` 给出曲线的 X（模板默认 `p90_intvty`，Y 是主指标 `input_tput_per_gpu`），每个系列按 X 排序，标出前沿（同系列里没有别的点两个轴都不差）。`ledger.working_point` 是判 A/B 的工作点：`{conc: C}` 取该并发的点（多次取平均），`{x: X}` 取 X 不低于该值时的最大 Y（在前沿上相邻两点之间插值）；各系列在工作点的值相对基线系列（`ledger.baseline_series`）的 Δ 显示在台账页和看板头部。
- AgentX 的 `agentx_conc<C>.json` 用 `python -m agent_sys_v2.ledger agentx <文件或目录> --out results --series v0 --round 1 --role baseline` 转成结果文件，指标名和算法与 InferenceX App 入库一致（交互性 = 1/p90 full-response ITL）。
- schema 不对的文件不进表，在台账底部和任务卡片上列为"格式错误"。

`infera.result/v1`（必填：`schema`、`label`、`metrics`）：

```json
{
  "schema": "infera.result/v1",
  "label": "c64-spf",
  "time": "2026-10-24T10:00:00Z",
  "role": "candidate",
  "round": 1,
  "series": "r1-spf",
  "change": "--schedule-policy shortest-prefill-first (decode/prefill both)",
  "metrics": {"tok_s_gpu": 14120.5, "p90_interactivity": 31.2, "ttft_p90_s": 2.1, "requests": 3120, "errors": 0},
  "workload": {"client": "agentx", "conc": 64, "duration_s": 3600, "seed": 42},
  "config": {"kit": "atom_4p4d_glm5.2", "image": "…", "extra_args": "…"},
  "verdict": {"status": "valid", "decision": "accept", "vs_baseline_pct": 15.4, "notes": "[M] queue-req ↓ 80%"},
  "evidence": {"run_dir": "…", "logs": "…"}
}
```

### 5.7 skills 注入

- system.md 加一段"可用的 skills"，一行一个：名字、描述、SKILL.md 的绝对路径。agent 用得上时自己去读。用法和经验索引相同，增加的上下文很小。
- 注入哪些：`project.yaml` 的 `skills` 列表（不写则注入全部内置 skills），再加 campaign 自己 `skills/` 目录下的全部。
- SKILL.md 的 frontmatter 和 Claude Code 原生 skill 的格式一致（`name`、`description`），以后可以改成原生加载（第 10 节）。

### 5.8 API（与 conch 一致的部分省略细节）

| 方法与路径 | 说明 |
|---|---|
| `GET /api/home` | 跨 campaign 的待确认、待审核、未查看、运行中、今日花费，**机器占用** |
| `GET /api/projects/{c}/board` | 类型、任务、依赖边、名额，**机器表**，**目标与台账摘要**（基线、最佳、Δ） |
| `GET /api/projects/{c}/ledger` | 台账：主指标、基线、最佳、行、格式错误的文件 |
| `GET /api/projects/{c}/tasks/{类型}/{名字}` | 任务详情，加**检查**（最近一轮 checks.json）和**机器** |
| `POST …/actions/{start,stop,resume,takeover,end_takeover,add_budget,cancel,join,approve,mark_read,delete,join_children}` | 操作；`join_children` 批量纳入子任务草稿 |
| `POST …/messages` | 你的消息 |
| `GET …/runs/{run}/events?after=&before=&tail=` | 精简过的事件（长内容截断，点开再取全文） |
| `GET/PUT …/file`、`GET …/files`、`GET …/raw` | 工作区文件 |
| `GET/POST …/questions` | questions.md 表单 |
| `GET/PUT /api/rules`、`/api/knowledge…`、`/api/settings`、`/api/models` | 规则、经验、设置、模型 |
| `GET /api/events` | SSE：`changed {project, task}`、`run {project, task, run, seq}` |

### 5.9 安全与边界

- 只监听 Unix socket（目录权限 0700）。浏览器通过 `ssh -L` 转发访问。
- agent 以 `bypassPermissions` 运行，靠规则、检查和人的审核约束。不做 OS 隔离，因为旧系统的经验表明 AI 任务做不到有效隔离。
- 工作区文件的读写都限定在工作区内；`/raw` 一律放进沙箱（CSP sandbox）。
- 发给 agent 的消息里不带任何凭据。

## 6. Infera 性能工作包

### 6.1 模板 `infera-perf`

| 类型 | 用途 | 默认检查 |
|---|---|---|
| `intake` | 每轮开头的问卷和 brief（`review: true`）；可写 campaign 的 `rules/project.md`、`project.yaml`、集群 skill | report.md 非空；`perfcheck intake` |
| `survey` | 对标调研：InferenceX 参考曲线（`role: reference`）、配置差异表、合规清单；不占 GPU | report.md 非空；`perfcheck survey` |
| `baseline` | 部署、过质量门、测 v0 曲线（`role: baseline`），噪声和快速验证标定，栈清单 | report.md、results、calibration.json、stack.json |
| `profile` | 从便宜的层做起的瓶颈分析，产出 `candidates.yaml`（skill infera-profile；默认不用 ultracode） | report.md、candidates.yaml、profile.json |
| `plan` | 候选排序、预算和机器分配，起草 experiment 子任务和 close（`review: true`） | report.md 非空；`perfcheck plan` |
| `experiment` | 一个大杠杆或一组同层小杠杆：快速验证迭代，工作点单点 A/B，写带判定的结果 | results 校验通过，且有判定 |
| `close` | 集成、整条曲线、封版、复盘，起草下一轮 intake；可写 `project.yaml` 的 version 和基线系列 | report.md；results；`perfcheck close` |
| `develop` | 代码改动：补丁、router、kernel，出 PR 或 overlay | report.md 非空 |
| `review` | 评审一个 PR、分支或结论（`review: true`） | — |

`perfcheck`（`python -m agent_sys_v2.perfcheck <类型> <任务>`）是这个模板的字段检查：intake 查 project.yaml 的目标、机器、轮次、台账口径和工作点，rules/project.md 的占位，问卷，本轮要上机任务的机器；plan 查每个子任务的机器和 `expect`（预期 Δ、快速验证级别 L0–L5、证伪条件），以及等它子任务的 close；close 查 `stack.json`、封版时的曲线和 project.yaml，以及下一轮 intake 的草稿。一轮的做法、文件格式和问卷在模板自带的 campaign skill `infera-round` 里。

两个通用机制让模板跑得起来：
- **模板自带任务**：`templates/<模板>/tasks/<类型>/<名字>.md` 在建项目时原样复制并排队（`skills/` 也复制，成为 campaign 自己的 skill）。它们不会自己开始：project.yaml 写入 `template_review: true`，调度器不启动这个项目的任务，第一个任务的等待原因是"等待你审核模板"。你审核后在看板上点"开始运行"（`POST /api/projects/<c>/start`），或手动开始其中一个任务，这一行就删掉，之后和普通项目一样按依赖自动推进。
- **`children` 依赖**：`{task: X, kind: children}` 要等 X 完成，并且 `parent: X` 的任务全部完成或取消（草稿也算没完成；等待者自己和它的下游不计入）。看板上画成青色点划线，等待原因写"等待 X 的子任务：…"，agent 的首条消息列出 X 的子任务和它们的报告。"全部纳入托管"同时纳入 X 的子任务草稿和直接依赖 X 的草稿；草稿纳入托管时，frontmatter 没写的 `review` 和 `checks` 取类型的默认值。

项目规则 `rules/project.md` 写这个 campaign 的背景（模型、引擎、kit、节点、负载、SLA），以及实验纪律：
- 一次只改一个变量；
- 证据标 `[M]` 实测或 `[I]` 推断；
- 作废规则；
- 判定规则：小于 2% 算噪声，大于等于 3% 要机理一致；
- 不动别人的容器和 GPU；
- 长任务在远端以自包含的执行链运行，并带守护。

### 6.2 内置 skills（MVP 版）

| skill | 内容 | 主要依据 |
|---|---|---|
| `infera-deploy` | kit 生命周期；VRAM 等待和外来容器检查；生效配置审计（`docker inspect`、`/get_server_info`），找出没被转发的 knob；守护规则 | `examples/atom_4p4d_glm5.2` 的 run 目录约定；旧 flow 的 kit 契约 |
| `infera-bench` | AgentX 全量和 fast 模式、`bench_serving`；每个点的固定产物；输出 `infera.result/v1`；测量陷阱 | `engine/agentx/*`；campaign 的结果文件约定 |
| `infera-quality-gate` | 拒绝在 forced acceptance 下跑；smoke、GSM8K、乱码探针、MTP 真实接受、RDMA 不回退 TCP、PD 交接 | 仓库的 smoke/eval；旧 flow 的探针集 |
| `infera-diagnose` | 瓶颈分类决策树（prefill 饱和、decode 步长、KV 天花板、传输、CPU、JIT、正确性）；每一类的观测手段和候选杠杆 | campaign §9.6；#209 |
| `infera-experiment` | 单变量 A/B 协议、作废和判定规则、`ab_judge.py`（噪声底，噪声只能导向拒绝）、结果写法 | 旧 flow 的 `compare.py`/`eval_stats.py`；campaign 判定规则 |
| `infera-hygiene` | 共享节点卫生：不删别人的容器，NFS、TMPDIR、HOME 陷阱，VRAM 释放，抢占 | 旧 flow 的 `lib/` 和 m1 brief；campaign 运维痛点 |

skills 的原则：
- 确定性的事交给脚本，判断交给 agent；
- 每个 skill 都写明"怎么证明它真的生效了"；
- 不复制仓库里已有的脚本，而是指过去（例如 `repos/Infera/examples/<kit>/engine/up.sh`）。

## 7. 可观测与可控清单

| 你想知道或做的事 | 在哪里 |
|---|---|
| 现在哪些事需要我 | 首页待确认（含验收未通过、闲置占用）；标签页标题上的数字 |
| 某个 agent 正在干什么 | 卡片上的"正在执行：<命令>"；运行输出实时流 |
| 它在等什么 | 运行中 · 等待中：后台命令的描述、已等多久 |
| 它拿到了哪些规则、skills、经验 | 运行输出顶部"本次注入内容"（launch.md） |
| 花了多少 | 卡片和抽屉里的花费/预算；首页今日花费 |
| 哪台机器被谁占着、是否闲置 | 看板机器条；首页机器卡片 |
| 优化走到哪了 | 台账趋势图：基线、最佳、每个实验的判定 |
| 结论可信吗 | 检查 ✓/✗；结果的判定和 `[M]/[I]` 证据；报告审核 |
| 让 agent 改方向 | 底部输入框（运行中也可以） |
| 立刻停下 | 停止（打断当前命令） |
| 亲自上手 | 在终端中接管（同一会话），退出后自动接回 |
| 控制花费 | 每个任务的预算；追加预算 |
| 批准 agent 起草的计划 | 父任务抽屉：全部纳入托管 |

## 8. MVP

### 8.1 范围

- **移植 conch 核心**：后端和前端，改名，去掉 parse 和 milestone，符合仓库的 ruff 配置（100 列、双引号），支持 Python 3.10+。
- **新增**：
  - 验收检查（runner 执行并反馈；抽屉和卡片展示）；
  - 机器（调度互斥、看板机器条、首页闲置占用）；
  - 台账（API、页面、趋势图、看板头部的基线和最佳）；
  - skills 注入；
  - 批量纳入子任务草稿；
  - `infera-perf` 模板；
  - 6 个 skills，其中 `ab_judge.py` 可执行、有测试。
- **不做**：
  - 子任务全部完成后自动唤醒父任务；
  - 原生 skills 加载；
  - 通知（webhook）；
  - 多用户；
  - 删除旧代码。

### 8.2 验收

1. `agent-sys-v2 serve` 起来后，首页、看板、台账、任务抽屉、规则页在中英文下都能打开，浏览器控制台没有报错。
2. 用 `infera-perf` 模板新建一个 campaign。一个真实的 agent 任务能实时输出，能接收消息，完成后状态正确。
3. 检查：先让检查失败，agent 收到反馈并修好，最终显示 ✓。一直失败的任务进入待确认 · 验收未通过。
4. 机器：同一台机器上的两个任务串行；等待原因写明占用者；占用者完成后释放。
5. 台账：工作区里的结果显示在表和图里，Δ 正确；格式错误的文件被列出。
6. launch.md 里能看到 skills 索引。
7. 测试：
   - 移植的 conch 测试和新功能的测试，在 Python 3.10 和 3.12 下都通过；
   - node 测试通过；
   - ruff format 和 ruff check 通过；
   - 旧 agent_sys 的测试照常通过。

### 8.3 MVP 现状（本分支）

- **代码**：独立目录 `Infera/agent_sys_v2/`，和 v1 的 `agent_sys/` 并列。
  - `src/agent_sys_v2/`：Python 包（src 布局），包括后端、`web/`、`templates/`、`skills/`；
  - `tests/`：测试；
  - `tools/demo.py`：生成不调用模型的演示 campaign；
  - `docs/`：本文；
  - `README.md` / `README.zh-CN.md`：安装和运行说明；
  - `pyproject.toml`：独立发布（`infera-agent-sys-v2`，命令 `agent-sys-v2`）。
- **对 v1 和仓库其他部分的改动**：
  - v1 的 `agent_sys/` 一行未改；
  - CI 另加一个 `agent_sys_v2` job：装 tmux，跑 pytest 和 node 测试。
- **验证结果**：
  - pytest：v2 全部通过（Python 3.10 和 3.12）；在 Python 3.10 下连同旧 agent_sys 的测试一起跑（CI 的方式），也全部通过。
  - node 单测：32 个，全部通过。
  - ruff 和 pre-commit：通过。
  - 无头浏览器截图自查：首页、看板、台账、任务抽屉、设置页、新建任务、规则页，中英文均无控制台报错。
  - 两次真实 agent 运行（Sonnet，合计 $0.15）：
    1. 第一次：agent 从注入的索引找到并读了 skills，用 `$AGENT_SYS_PYTHON` 和 `ab_judge.py` 写出带判定的结果；3 条检查第一次就通过；结果进了台账。
    2. 第二次：故意加了一条 prompt 没提的检查（报告要有"## 回滚"）。第一次检查失败，失败信息发回同一会话；agent 补上这一节，第二次检查通过，任务完成。运行输出和"验收检查"视图里，两次尝试都看得到。

## 9. 迁移计划

| 步骤 | 内容 | 前提 |
|---|---|---|
| 1（本分支） | `agent_sys_v2/` 和 v1 的 `agent_sys/` 并列，互不改动；两者可以装在同一个环境里（命令 `agent-sys-v2` 和 `agent-sys`） | — |
| 2 | 用新系统真实跑一个 campaign（例如 GLM-5.2 4P4D AgentX），按实际使用打磨 skills；把旧 flow 剩下的领域资产（探针集、trace 分类、launcher 归因）迁进 skills；把 todo.md 里的经验写成经验条目 | 你确认设计 |
| 3 | 删除 v1 的 `agent_sys/`（9 个包、examples、tests、docs）和它的 CI job；`Dockerfile.agent-sys` 换成新系统的镜像 | 和旧系统作者对齐 |
| 4 | 是否把 `agent_sys_v2/` 改名回 `agent_sys/`（包名、命令随之改） | 第 3 步之后 |

## 10. 开放问题与路线图

**开放问题**（需要你决定）：

1. 什么时候删旧代码？我的建议是第 2 步之后。
2. skills 用注入索引（MVP），还是走 Claude Code 原生加载（`--plugin-dir` 或项目 `.claude/skills`）？原生方式的体验更好：Skill 工具、按需展开。代价是要加载项目级设置。
3. 和 conch 的关系：分叉，按需 cherry-pick；还是抽出共享的核心库？模块名和结构目前和 conch 保持一致，方便同步。
4. 单人 server（和 conch 一样），还是团队共用一个 server？后者需要身份认证和权限，复杂度会明显上升。

**路线图**：

| 优先级 | 内容 |
|---|---|
| P1 | 子任务全部完成后唤醒父任务（滚动规划；目前由另一个任务用 `children` 依赖等待子任务，例如一轮的 close）；通知（webhook / Slack），"有任务需要你"时推送；人的等待时间、返工率、按类型统计花费 |
| P1 | skills 补全：`infera-sim`（模拟器作先验和剪枝）、`infera-patch`（补丁出货）、`infera-env`（预检）；引擎日志 parser 和 CPU profiling 脚本；profile 工具（步长台架、kernel 分类、时间账、trace 开窗）和 `check_profile.py`；ATOM 的 profile playbook（从 ultracode profile 蒸馏，`skills/infera-profile/playbooks/atom.md` 已留位置）；SGLang/vLLM 的 AgentX 运行器 |
| P2 | 远端执行链加守护和事件唤醒的标准件（`run_chain.sh`）；台账的多指标 Pareto 视图；任务图模板（建项目时预置第 1 轮已做；之后各轮由 intake 起草） |
