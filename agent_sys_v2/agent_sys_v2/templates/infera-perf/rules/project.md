# Campaign 说明

<!-- 新建 campaign 后填写。每次运行都会带给 agent；相对固定的背景写在这里，不用在每个任务里重复。 -->

## 目标与部署

- 目标：（指标、负载、SLA，例如 AgentX C64 的 tok/s/GPU，P90 interactivity ≥ 30）
- 模型与引擎：（模型、引擎 SGLang / vLLM / ATOM、镜像）
- 拓扑：（例如 4P4D；kit：`repos/Infera/examples/<kit>`）
- 代码：（`repos/Infera` 的分支；需要的其他仓库）
- 机器：在 project.yaml 的 machines 里登记；任务只用分给它的机器

## 实验纪律

- 先诊断再选杠杆：瓶颈可能在调度、CPU 路径、KV/内存配方、传输、路由、并行策略或 kernel，按证据决定先做哪个，不默认从 kernel 下手。
- 一次只改一个变量；改动前写清预期机理和预期幅度。
- 以实际运行的配置为准（`docker inspect`、`/get_server_info`、引擎日志），不信脚本和 README；证明杠杆真的生效了再测。
- 证据标注 `[M]`（实测）或 `[I]`（推断）。
- 判定：差异 < 2% 视为噪声；≥ 3% 且机理指标一致才接受；介于两者之间加跑一轮（skill infera-experiment）。
- 作废：测量期间有别人的负载、HSA out-of-resources、被抢占、失败请求 > 10%。作废的结果也写进 results/，verdict.status 写 void。
- 每个测量点写一个 `results/<标签>.json`（infera.result/v1，skill infera-bench），台账据此画趋势；原始数据和命令放在 `results/<标签>/` 或 `log/`。
- 不动别人的容器和 GPU，不删不是自己创建的文件；长任务在远端以自包含的执行链运行，并带守护（skill infera-hygiene）。
