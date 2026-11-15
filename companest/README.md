# Companest 模块说明

Companest 保存公司、团队、任务、记忆和业务计划；Agent Substrate 承担 actor 的创建、运行、暂停、恢复和删除。
执行路径为 CLI/API/Gateway → 控制面 Orchestrator → JobManager → Substrate actor → 团队/Pi → 结果与业务存储。
控制面不直接运行模型，也没有本地 worker 队列或执行失败后的本地回退。

这里把一个职责连贯的 package 视为一个 unit。每个 unit 的 README 逐文件说明职责、输入输出、持久化和边界。

| Unit | 做什么 | 入口文档 |
| --- | --- | --- |
| agents | 团队声明、Pi 模型/工具调用、workspace 操作 | [agents](agents/README.md) |
| companies | 公司配置、成员团队、扩展钩子、CEO 业务结果 | [companies](companies/README.md) |
| gateway | 消息渠道、WebSocket 协议和请求转发 | [gateway](gateway/README.md) |
| jobs | 任务记录、远端执行关联、系统与用户计划 | [jobs 与完整 job 清单](jobs/README.md) |
| knowledge | 外部信息源、公司摘要、公共知识 | [knowledge](knowledge/README.md) |
| memory | 文件记忆、检索、整理与备份 | [memory](memory/README.md) |
| modes | 团队内三种执行方式 | [modes](modes/README.md) |
| orchestration | 公司/团队解析、路由、预算和业务工作流 | [orchestration](orchestration/README.md) |
| runtime | Substrate 适配、单任务 actor、持久存储协调 | [runtime](runtime/README.md) |
| server | 管理 API、内部 actor 回调、事件历史 | [server](server/README.md) |

## 根目录模块

| 文件 | 职责与边界 |
| --- | --- |
| [cli.py](cli.py) | 配置初始化/验证、启动服务和 operator 命令；执行类命令向 API 提交任务。 |
| [config.py](config.py) | Pydantic 配置模型、环境变量和配置发现顺序的唯一实现来源；包含 Substrate scope/template 映射。 |
| [parser.py](parser.py) | 从 Markdown 中解析 YAML/JSON/TOML 配置，处理 include 和环境插值，生成配置模板。 |
| [events.py](events.py) | 进程内业务事件订阅和派发；持久化由 server 的 EventStore 完成。 |
| [exceptions.py](exceptions.py) | 业务异常类型和结构化详情，不控制重试或生命周期。 |
| [utils.py](utils.py) | 阻塞 I/O 的异步封装、同步锁、取消期间完成关键写入及 LLM JSON 提取。 |
| [__init__.py](__init__.py) | 对外导出公共 Python 接口，兼容未安装部分可选依赖的核心导入。 |
| [__main__.py](__main__.py) | `python -m companest` 的 CLI 入口。 |

## 标识和存储

`company_id` 标识公司；`team_id` 在路由、工具、文件记忆与向量检索中使用同一个值。
私有团队沿用 `company/team`，共享团队使用其注册 ID；不把分隔符改成下划线，也不再另设向量集合的团队 ID。
`job_id` 标识一次业务执行，`workspace_id` 标识工作区，`storage_id` 校验实际挂载的是预期存储。
这些 ID 对应不同实体，不能互相替代。

公司数据在公司目录中，跨公司可见的团队必须显式出现在 `shared_teams` 中。
actor 的模板、挂载与回调能力都绑定已接受任务的 scope；嵌套任务继承该公司的用户和 workspace。
完整布局见 [持久化部署](../deploy/substrate/storage.md)，验证范围见 [测试说明](../tests/README.md)。

## 执行链路字段约定

同一实体在请求、服务调用、actor 回调、事件和持久记录中使用同名字段、同一 ID 值。

| 字段 | 贯穿哪些位置 |
| --- | --- |
| `job_id` | 提交/查询/列表/事件、Job、jobs.db、actor task journal；新 actor 的资源名称直接使用该值。 |
| `schedule_id` | 用户计划工具、API/actor 回调、Schedule、user_schedules.db；创建后原样返回并用完整值取消。 |
| `company_id` / `team_id` / `workspace_id` | 执行上下文与所有跨进程服务请求；不替换分隔符、不追加另一层 scope ID。 |
| `model` | 模型配置、预算调用、CostEstimate 和审批响应；不在边界改名为 target_model。 |

外部协议的原生字段（如 Substrate 的 metadata.name/uid、APScheduler 的 id 参数）在适配器中使用；不生成第二份业务 ID。
旧数据库的主键列在启动时一次性改名为 job_id/schedule_id，已有 ID 值保持不变；运行时不同时支持两套字段。
旧客户端需要与本分支同步更新，具体变更见 [部署升级说明](../deploy/substrate/README.md#execution-field-contract)。
