# modes：团队内如何执行一次任务

输入是已授权的 AgentTeam、任务、上下文和进度回调；输出是文本结果。
模式只编排当前 actor 中的 Pi 调用，不创建远端实例、不保存进程，也不管理任务重启。

| 文件/模式 | 做什么 |
| --- | --- |
| [default.py](default.py) / `default` | 由团队 lead Pi 完成任务。 |
| [collaborative.py](collaborative.py) / `collaborative` | 团队成员顺序工作，后续成员利用前面的结果。 |
| [council.py](council.py) / `council` | 独立观点、可选评分与筛选，最后综合；处理部分成员失败。 |
| [__init__.py](__init__.py) | ExecutionMode 接口、ModeRegistry、默认注册和旧配置标准化。 |

`cascade: true` 是独立模型策略，可与模式组合。
`workflow: decompose` 和 `workflow: delegate` 在 [orchestration](../orchestration/README.md) 中，是有界的业务步骤，不是额外执行模式。
旧的 `mode: cascade/loop/conditional` 只保留配置翻译兼容；新配置应使用 mode、cascade、workflow 三个明确字段。
每次执行使用独立模式实例/配置，防止并发任务相互修改 cascade 或工作流设置。
