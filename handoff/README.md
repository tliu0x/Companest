# Agent Substrate 迁移交接

## 从这里开始

本分支 `feat/agent-substrate-migration` 从已合并 #15–#17 的 `main` 创建，起点是 `bef101b8e160bb284d2ab7aa78edb504bc9edde2`。本次提交只整理交接材料；**尚未安装、启动或接入 Substrate，没有迁移运行时代码**。下一 session 在本分支继续实现，不要回到旧 PR 分支。

用户已确定的目标：使用 [agent-substrate/substrate](https://github.com/agent-substrate/substrate) 承担底层执行和生命周期管理；能用上游替换的自研代码要删除，不要在适配层重新实现同一套运行时。先跑通实际 launch，再逐步切换、验证、删旧代码。不要停在接口桩、mock 或设计文档。

先读根目录 `AGENTS.md`、`CLAUDE.md`、`SECURITY.md`、本文件及 `inventory.json`。本目录是临时交接：迁移完成后将必要运维说明归入正式文档，最后删除 `handoff/`。

## 已完成的上下文

- #15：删除旧控制台等未使用功能，合并 knowledge 相关模块。
- #16：Pi 统一到 LangChain/LangGraph，旧的双 SDK runner 已移除。
- #17：目录重构、删冗余注释/测试、删除公共知识写入接口、Dreamer 日志、S3 备份恢复、CostGate 边界测试，以及 CLI/session 接入和阻塞 I/O 修复。
- 最新 main 的代码树与此前通过验证的 PR #17 相同：完整测试 **523 passed / 3 live-network deselected**，编译、目标 flake8、GitHub CI 和 Docker 构建/认证/入口检查通过。这是继承的基线，不是 Substrate 验证结果。
- 最近又跑过模型路由、默认模型、Pi、Dreamer、CostGate 回归：116 passed。
- 旧 `console-phase-0b-frontend-scaffold`、`console-phase-0c-deployment-ci`、`low-cost-models` 远端分支已核对并删除，不要恢复它们。
- 公开分支/标签的历史脱敏已发布；禁止合并旧 clone 的历史。GitHub 保留的旧 PR 引用及缓存清理仍是独立未完成事项。
- 运行时代码约 15,579 行，测试 6,280 行，部署/示例脚本与 Terraform 440 行。不要为了行数目标删除必要业务能力。

## 当前执行链与已知限制

`CLI / HTTP API -> JobManager -> run_team / run_auto -> mode -> Pi -> LangChain agent -> tools/model`

- `companest/jobs/jobs.py`：内存队列、async worker、任务状态、SQLite、重启恢复、结果保存。
- `companest/orchestration/orchestrator.py`：团队/公司初始化与销毁、路由、预算、工作区、执行入口。
- `companest/agents/team.py`：团队配置、Pi 实例及 idle eviction；`agents/pi.py`：模型调用、工具循环、上下文、usage。
- `jobs/scheduler.py`、`jobs/user_scheduler.py`、`orchestration/background.py`：周期任务、用户调度、后台工作。
- `server/server.py`、`server/event_store.py`、`events.py`：认证、API、事件历史、WebSocket。
- `companies/`、`memory/`、`knowledge/`、`orchestration/cost_gate.py`：公司规则、记忆/知识、预算等业务能力。

当前服务是单进程 asyncio，阻塞工作进入线程。公司配置写入/热更新采用完整快照发布；生命周期写入串行化，同步组件钩子在线程运行，工具/事件/路由注册回到事件循环。迁移后应让上游承担执行实例生命周期，避免长期保留这套机制和另一套 actor 管理机制。

Claude session **当前只是 CLI/API 客户端**，不是已经接入的远程执行器。`examples/claude-session.md` 记录现有接入方式。任务取消目前不能保证打断已经开始的工具调用；重启可能重试，提交没有幂等键，也没有 exactly-once 保证。真实模型服务、真实 S3 调用尚未验证。

## 上游事实与待核对边界

2026-11-15 查询上游 main 得到 `a46464663fa076221ae059e1fd4a273edd2d8b09`。这是调查记录，不是已经验证的兼容版本；下一 session 要读取所选提交实际文件，固定 release/commit，并核对 API/proto，不要臆造 Python SDK、REST 路由或 launch 参数。

上游资料入口：

- [项目及开发 quickstart](https://github.com/agent-substrate/substrate#quickstart-development)
- [架构](https://github.com/agent-substrate/substrate/blob/main/docs/concepts/architecture.md)
- [API / WorkerPool / ActorTemplate 配置](https://github.com/agent-substrate/substrate/blob/main/docs/reference/api-guide.md)
- [Claude Code multiplex 示例](https://github.com/agent-substrate/substrate/blob/main/demos/claude-code-multiplex/README.md)
- [Agent Executor / AX](https://github.com/google/ax)：上游关联项目，**不是 substrate core 本身**；只有实际需要其能力时再评估。

已查阅的官方说明将 Substrate 定位为 actor 与 worker 分离的沙箱执行基础设施，提供放置、休眠/恢复和流量路由；开发 quickstart 使用 Go、kubectl、Docker 和 kind。它支持承载 OCI 工作负载，因此现有 agent harness 可以先作为工作负载迁入，不必同时重写业务。具体平台、权限、存储和恢复语义必须实测；不要把 Linux/Kubernetes 前提当成纯 Python 包安装问题。

**actor 恢复不等于业务 job 恢复；网络入口不等于聊天网关；计算资源调度不等于 cron；沙箱快照不等于知识库备份。** 为每个拟删模块找到真实替代能力、接口和验证证据。上游 core 未覆盖的业务协议只保留最薄必要部分，不要再写一个通用运行时。`inventory.json` 是候选清单，不是直接删除脚本。

## 已同意的产品方向

1. CLI/API 是主要接入方式，Telegram 网关不是必需依赖。已有网关的业务路由不要无关改动；若确实被迁移替代，说明替代关系并清理消费者。
2. 身份、任务和状态不绑定单个客户端会话或 Python 进程；执行由 Substrate 管理，Companest 保留业务控制面及薄适配。
3. 模式收敛已经得到用户同意，**尚未实现**：对外只保留单人执行 `default`、顺序协作 `collaborative`、并行会审 `council`；`cascade` 是模型策略开关；`loop` 的“拆解→逐项执行→汇总”和 `conditional` 转派属于上层工作流。优先复用选定上游已有能力，不要新写一个 WorkflowRegistry 来制造另一层框架。
4. 旧模式配置需迁移或保留明确、短期的兼容映射；不把旧 `loop` 描述为具备反馈/自我修正闭环。条件转派应知道允许的团队，并有预算、步骤和权限边界。
5. 之前“不拆自研多进程”不是阻止采用 Substrate worker 的要求。现在用户明确要求交给上游运行时，不再自研进程池/actor 调度器。
6. 每个有意义阶段汇报、验证、提交和推送；迁移从新的 main 分支开新 PR，不再改已合并的 #17。

## 实施顺序与验收

### 1. 锁定能力与启动环境

- 核对可用 Docker、Kubernetes context、kubectl、Go、架构及上游支持情况。没有现成集群时优先评估上游本地 quickstart；不要假设 Docker 可用就代表 gVisor/microVM 可运行。
- 明确 actor 粒度（会话、workspace 或任务）、company 到 atespace/身份/权限的映射、镜像及持久化位置。记录选择及理由，不能把 job_id 直接等同 actor_id。
- 固定上游版本；先运行其最小有状态示例，验证创建、请求、状态、休眠/恢复及销毁。这里列的是所需能力，不是未经核对的 API 名称。
- 若真实环境缺必要权限/资源，报告具体阻断；不要以 mock 成功声称完成 launch。优先继续完成不依赖该阻断的代码/文档。

### 2. 打通一个完整任务

- 使用上游提供的协议/工具，加入最小配置和适配代码；不要复制上游 control plane、worker pool 或快照系统。
- 将一个现有 agent harness 打包成 actor 工作负载。CLI/API 提交任务，经过 Companest 的公司/权限/预算检查，由 Substrate 启动或定位执行环境，返回可重连查询的完整结果。
- 明确 job 状态与 actor 状态、终止与取消、超时与恢复的区别；状态以一个权威执行来源为准，本地只保留必要业务记录或可重建投影。
- 先做无外部副作用的真实 actor 测试，再做有授权凭据的模型/tool 集成。生产凭据不写进镜像、参数日志、prompt、示例或仓库。

### 3. 切换并删旧实现

- 按 `inventory.json` 逐个确认替代，再删除本地 worker loop、实例缓存/驱逐、重复生命周期/恢复等已被替代代码及依赖。
- 不长期保留两套生产执行后端；Substrate 不可用时给出明确错误，不静默回退自研 runtime。过渡兼容层只做配置/协议转换，有清晰删除终点。
- 模型调用、预算、公司配置、记忆、知识、周期触发等只有在验证等价替代后才删；不能为了“去自研”误删上游没提供的业务能力。
- 同步更新 CLI/API、部署示例、可选依赖、测试和正式文档；根 AGENTS/CLAUDE 若修改须保持一致。保留 .dockerignore 的隐私边界。

### 4. 真实故障与边界验证

- 客户端断开/重连仍能查询原任务；同一任务/会话不会产生两个无协调写入者。
- actor 休眠/恢复、worker 失败或控制面重启后，按实际保证恢复；不得凭快照功能宣称外部副作用恰好执行一次。
- 取消与销毁的行为明确，重复提交/重试不会造成不受控的重复执行；结果、错误及终态不会被迟到响应覆盖。
- 跨公司任务、workspace、记忆和凭据隔离；权限/预算不会被 actor 入口绕过；provider/tool egress 能按预期工作。
- 执行面不可用时 API 保持可响应，错误清晰；部署产物确实可 launch，不止通过单元测试。
- 全量测试、静态检查、镜像/启动检查；记录替换/删除代码量及仍保留的业务职责。列出未验证项目，不掩盖环境阻断。

## 本地验证入口

```bash
python -m pip install -e '.[dev]'
python -m pytest tests/ -q -p no:cacheprovider
python -m compileall companest tests -q
python -m flake8 companest tests --select=E9,F63,F7,F82 --count
git diff --check
```

当前环境若已存在虚拟环境，可使用 `.venv/bin/python`。新 session 应根据其系统安装依赖；不要复制另一台机器的虚拟环境。`live_network` 的 3 项默认排除，只在有合适网络/凭据时显式运行。Substrate 的真实集成测试需另加，不能用这 523 项旧测试替代。

优先保护的现有测试：`test_jobs.py`、`test_session_api.py`、`test_cli.py`、`test_company_api.py`、`test_company_async.py`、`test_sessions_security.py`、`test_event_api.py`、`test_pi_runtime.py`、`test_coding.py`、`test_async_io.py`、`test_cost_gate.py`、`test_memory_backend_safety.py`。旧实现被删后，删掉只验证其内部实现的测试，保留/迁移用户契约和隔离测试。

## 公开仓库与交接纪律

不要写入个人姓名/邮箱、机器绝对路径、私有 session 链接或任何凭据。提交使用非个人 Git identity，推送前检查 author/committer 与 diff。不要恢复历史脱敏前的提交或已删除旧分支。只引用公开仓库及上游来源。

当前未知、由下一 session 核实：Substrate 选定版本的精确 SDK/API、可用启动环境、凭据注入与持久卷配置、actor 粒度，以及是否确实需要 AX。除阻断性信息外按最小可行方案推进；不要反复要求用户确认已经同意的迁移方向。

新 session 开场：

> 在 `feat/agent-substrate-migration` 分支读取 `handoff/README.md` 和 `handoff/inventory.json`。从已合并 #15–#17 的 main 开始，把底层执行与生命周期交给 agent-substrate/substrate；先验证上游实际 launch 和完整任务链路，再删除被替代的自研实现。不要新造运行时，不要静默回退旧后端；保留公司、预算、记忆及 CLI/API 契约，落实三种执行模式与独立模型策略。每步验证、提交、推送，完成后删除临时 handoff。
