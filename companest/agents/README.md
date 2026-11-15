# agents：执行模型与工具的业务层

输入是团队声明、任务和已绑定的公司/workspace 上下文，输出是任务文本及工具产生的业务副作用。
这些模块在 actor 内构建执行对象；TeamRegistry 在控制面也用于发现声明，不持有常驻 agent 进程。

| 文件 | 做什么 |
| --- | --- |
| [team.py](team.py) | 解析 `team.md`、发现私有和共享团队；每次 `create` 构建新的 AgentTeam/Pi，避免请求之间共享运行上下文。 |
| [pi.py](pi.py) | 构建 system prompt、检索任务记忆、组装工具，运行 LangChain agent，并实施 max_turns、模型策略和调用期限。 |
| [tools.py](tools.py) | 工具定义/注册表、预设与 deny 策略；提供记忆、会话、用户计划、CEO 委派、workspace 和外部 MCP 接口。 |
| [builtin_tools.py](builtin_tools.py) | 文件、搜索、命令等内置工具的执行实现和参数约束。 |
| [git_tools.py](git_tools.py) | 对指定 workspace 执行 Git status/diff/log/branch/commit/push；输出 Git 结果，不负责创建 actor。 |
| [workspace.py](workspace.py) | 读取 `workspaces.json`，校验归属与存储标记，限制路径在工作区内，提供文件上下文。 |
| [model_routing.py](model_routing.py) | 将模型声明映射到原生或代理端点，解析相应凭据，构建正确协议的模型客户端。 |
| [cascade.py](cascade.py) | 按成本、任务复杂度、语言和历史质量选择模型并升级；这是模型调用策略，不是独立生命周期或执行模式。 |
| [__init__.py](__init__.py) | 包入口。 |

记忆通过 [memory](../memory/README.md) 落盘；代码修改写入持久 workspace。
workspace 单写锁由 [ActorWorkload](../runtime/actor.py) 在任务开始时持有，锁文件在独立共享锁目录，不能放进可能被 Git 清理的工作区。
工具可见性还要经过 Pi 的 allow/deny 与公司共享团队检查。CEO 工具只能在当前任务的公司、用户和工作区中委派。
新增工具时同时检查默认权限、外部 MCP 的公司作用域和路径逃逸；相关契约在 `test_pi_runtime.py`、`test_coding.py`、`test_sessions_security.py`。

会话发送和会话读取都使用 `team_id` 参数；用户计划工具使用 `schedule_id`，
不将计划误称为已执行的 job。新调用方应按这些统一字段构造请求。

团队声明创建时即确定完整 ID；配置加载后不再修改 TeamConfig.id，
也不再保存 local_id 与完整 ID 两套身份。source_dir 只表示实际配置/记忆目录。
