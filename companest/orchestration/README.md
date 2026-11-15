# orchestration：业务决策与任务入口

控制面接收任务上下文，解析公司/团队/workspace，检查配置后提交 JobManager。
真正的团队执行方法在 actor harness 中；这里不保留旧本地 worker、实例缓存或回退运行路径。

| 文件 | 做什么 |
| --- | --- |
| [orchestrator.py](orchestrator.py) | 初始化团队、记忆和公司；维护业务注册/热加载；准备提交上下文；对外 run_team/run_auto 提交并等待远端 job。 |
| [router.py](router.py) | 确定性绑定、显式团队、关键词及模型路由；输出带允许团队、模式、策略和置信度的 RoutingDecision。 |
| [cost_gate.py](cost_gate.py) | 成本估算、预算判定、审批通知与支出记录；中央账本由控制面持有。 |
| [background.py](background.py) | 信息补全、按需刷新、费用报告和用户计划的业务回调。 |
| [multi_team.py](multi_team.py) | 按 RoutingDecision 在当前任务内顺序或并行调用获授权团队。 |
| [decomposition.py](decomposition.py) | 有界拆解、逐步执行与综合；不实现自动修复/重试运行时。 |
| [delegation.py](delegation.py) | lead 在允许团队之间委派，限制步骤和嵌套深度，汇总结果。 |
| [__init__.py](__init__.py) | 包入口。 |

## 控制面和 actor 的边界

`prepare_submission` 确定公司 scope 和 workspace 存储身份，JobManager 选择 operator 配置的模板。
actor 的 [harness](../runtime/harness.py) 执行路由/模式/Pi；嵌套调用继承原始公司、用户及 workspace，不能扩大权限。
全局绑定可在提交前解析成明确团队，actor 的结果仍保留 RoutingDecision，供 run_auto 调用者读取。

CostGate 在每次团队调用前由 actor 回调控制面。控制面依据当前团队配置和公司预算作决定，按估算预留支出；这不是模型供应商最终账单。
任务能力不能自升到高于提交时的优先级；等待审批后再次检查取消/公司停用。
所有分支都经过预算检查，旧 skip_cost_check 参数仅为兼容保留。

业务周期在 [jobs 清单](../jobs/README.md) 中列出。新工作流应组合已有团队调用，不要另写 scheduler、进程恢复或持久化执行状态机。
