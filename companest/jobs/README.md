# jobs：任务记录与业务计划

这里有两种不同对象：Job 是一次已提交的执行；Schedule 是未来何时产生任务的业务规则。
Substrate 管理执行实例的生命周期，Companest 的计划只决定业务触发时间。

| 文件 | 做什么 | 保存在哪里 |
| --- | --- | --- |
| [jobs.py](jobs.py) | Job/JobManager（统一 `job_id`）；持久化提交意图、绑定 actor UID、发送单次任务、重连查询、保存完整结果、确认取消后清理。 | 控制面 `jobs.db`；同一数据库只能有一个 owner。 |
| [scheduler.py](scheduler.py) | 系统 interval 计划；支持启动、停止、立即运行和公司热加载时增删计划。 | 进程内声明；启动时从配置重建。 |
| [user_scheduler.py](user_scheduler.py) | Schedule/UserScheduler（统一 `schedule_id`）；用户 cron/interval/date 计划；保存归属和触发计数，用 APScheduler 触发异步回调。 | `user_schedules.db`；APScheduler 本身只保留内存调度器。 |
| [__init__.py](__init__.py) | 包入口。 | 无。 |

## 每类执行 job 做什么

| 来源/类型 | 目的 | 执行与输出 |
| --- | --- | --- |
| API / CLI / Gateway 任务 | 响应一次用户请求；可指定团队，也可自动路由。 | 一个 actor 执行绑定的任务；完整文本保存在 jobs.db，记忆和文件写入持久挂载。 |
| CEO 周期任务 | 检查公司目标/KPI、向允许的团队委派工作、汇总运营结果。 | `company-<id>` 团队；结果由 OutputSink（默认 MemorySink）保存。 |
| 公司自定义计划 | 执行 company.yaml 中指定的 team、prompt、mode。 | 绑定公司提交 JobManager；适合定时研究、监控和报告。 |
| 信息补全任务 | 更新研究 briefing 或信息 feed，供后续任务使用。 | 调用 research/info-collection；更新共享或授权的团队记忆。 |
| 按需信息刷新 | 自动路由业务请求前，围绕当前问题补充少量信息。 | 在当前 actor 中调用 info-collection，继承公司/用户/workspace；不是另建无归属的任务。 |
| `maintenance=dream` | 评估记忆重要性、压缩、清理并生成概要。 | 按 Substrate 配置的 scope 提交 actor；记录 Dreamer 统计。 |
| `maintenance=consolidate` | 合并长期记忆，归档原条目。 | 按 scope 提交 actor，写入长期记忆和 dream log。 |
| 用户计划任务 | 执行用户创建的提醒、周期分析或一次性工作。 | 继承创建时保存的 company/user/chat/channel/workspace；完成后经通知回调返回结果。 |

## 系统计划名称、周期和用途

当前内置计划均 `run_on_start=False`：启动服务不会立即执行一次。
系统 interval 从上次回调完成后等待下一周期；不是固定墙钟 cron。

| 计划名 | 默认周期/条件 | 工作 |
| --- | --- | --- |
| `company_watcher` | 30 秒 | 控制面检测公司配置变化并热加载；不创建模型任务。 |
| `ceo_<company_id>` | 1800 秒，可由 `ceo.cycle_interval` 修改；公司及 CEO 启用时 | 产生 CEO 周期 job。 |
| `company/<company_id>/<schedule.name>` | 1800 秒，可由 `interval_seconds` 修改；计划启用时 | 产生公司自定义 job。 |
| `enrichment/research` | 1800 秒；已注册 research 时 | 更新 briefing.json；提示词要求保留 24 小时内最多 15 条。 |
| `enrichment/info-collection` | 300 秒；已注册 info-collection 时 | 按 watchlist 拉取信息并更新 feed/digest；提示词要求去重、保留 2 小时内最多 50 条。 |
| `enrichment/<team_id>` | 扩展通过 `register_enrichment_cycle` 指定 | 运行该团队的补全提示词。沿用原始团队 ID，不替换字符。 |
| `dream_nightly` | 86400 秒 | 按 scope 派发 `maintenance=dream`。 |
| `dream_deep` | 604800 秒 | 按 scope 派发 `maintenance=consolidate`。 |
| `daily_spend_report` | 86400 秒 | 控制面汇总最近 24 小时费用，经 CostGate notifier 发送。 |
| `memory_backup` | 14400 秒；配置 S3 bucket 时 | 控制面将记忆快照写入 S3，并记录备份元数据。 |
| 用户 schedule 的 ID | `cron` / `interval` / `date`，由 trigger_args 指定 | 持久化用户规则，触发上述用户计划 job。 |

这些本地计划没有第二套 actor 状态机，也不以本地等待超时来宣告远端执行失败。
同一系统计划串行等待回调；APScheduler 用 `max_instances=1` 和 coalescing 避免同一进程中重复并发触发。

## 状态、重启和不确定结果

新任务通常从 `dispatched` 到 `running`，最后为 `completed`、`failed` 或 `cancelled`。
兼容枚举中的旧 queue/approval 状态不表示仍有本地执行队列。
`execution.phase` 区分创建 actor、准备 POST 和已接受；actor UID 防止误接管同名替换实例。

提交时先保存意图；POST 响应丢失后查询已有 actor，不盲目重放。控制面重启重新关联保存的引用；actor 冷启动发现未完成 journal 时标记结果不确定，不重新执行任务。
终态结果先写 jobs.db，再删除 actor。取消必须等 Substrate 确认删除；网络故障保留取消意图，不能提前宣称已取消。
列表是最近一次观察到的状态，单个 job 查询才刷新远端状态。

同一公司、提交者和 `idempotency_key` 可复用同一个 job；任务内容/上下文不同会拒绝。
一次性用户计划在派发前持久化 claim，重启不自动再触发；claim 与派发之间崩溃可能漏执行，需要先检查 job 记录再显式补发。
周期计划在控制面重启后重建未来触发，不承诺跨进程故障时严格恰好一次执行。

公司计划的 API 名称从 `company_<id>_<name>` 改为 `company/<id>/<name>`，
信息补全计划改为 `enrichment/<team_id>`，避免含下划线/连字符的 ID 相互覆盖。
公司状态按保存的 scope_id 精确筛选计划，不使用名称前缀推断归属。

任务提交、详情、列表和事件均返回 `job_id`，用户计划均返回 `schedule_id`。
新计划使用完整 UUID，取消要求完整 schedule_id，不做前缀解析。
旧 jobs.db / user_schedules.db 的 id 主键列一次性更名，保留已有 ID 和执行引用。
