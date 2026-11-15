# runtime：Companest 与 Agent Substrate 的边界

输入是控制面持久化的 Job 和 operator 配置的 actor template；输出是 actor 记录、完整结果和存储副作用。
Substrate 负责实例生命周期，Companest 只适配调用和执行自身业务 harness。

| 文件 | 做什么 |
| --- | --- |
| [substrate.py](substrate.py) | 调用固定版本的官方 kubectl-ate，创建/查询/删除 actor、设置公司出站策略，并通过本地 proxy 访问 actor HTTP；不自行实现快照调度或恢复。 |
| [actor.py](actor.py) | 单任务 HTTP workload，提供 ready/task 接口；校验 scope、持久化 journal、持有 workspace 锁并调用 harness。 |
| [harness.py](harness.py) | ActorOrchestrator；绑定原任务身份，运行路由、团队、模式和有限工作流。 |
| [services.py](services.py) | 使用当前 job capability 回调中央预算、用户计划和业务事件接口；不携带管理 API token。 |
| [storage.py](storage.py) | 共享卷原子文件锁、原生控制进程锁与 storage identity 校验；协调跨 sandbox 的记忆和 workspace 写入。 |
| [__init__.py](__init__.py) | 包入口。 |

## 一次执行的持久状态

控制面 jobs.db 保存提交意图、actor 引用/UID、能力凭据和最终结果。
actor 的独立持久卷保存 task journal，业务记忆与 workspace 则使用长期共享挂载。
这三类数据的生命周期不同，删除完成的 actor 不应删除业务存储。
actor 将已接受请求的 job_id/company_id 注入记忆来源上下文，Pi 追加 pi_id；
嵌套任务和后台整理沿用这个 job_id，不从模型工具参数读取来源身份。
团队内的 memory pending journal 只恢复文件提交一致性，不重跑 actor、模型或工具。

接受请求时先写 journal 再启动协程；客户端在写入期间断开也必须完成接受步骤。
相同任务重复请求只能得到已有记录，不能再次执行。
冷进程重启不能恢复未完成 Python 协程，因此标记 outcome unknown/failed；Substrate 的进程快照恢复则由其自身负责。

## 隔离

模板与公司绑定，启动时拒绝暴露其他公司的配置/目录/工作区。
共享团队也必须属于当前公司允许的集合。workspace 的公司与 storage_id 同时校验，任务持有单写锁。
所有嵌套 CEO/委派/刷新调用继承同一公司、用户、聊天渠道和 workspace。
控制面每次检查 job capability 与公司是否仍启用；终态或取消任务的能力失效。
每个 scope 的 egress_rules 由 operator 配置并随 job 持久化，在唤醒前创建；
恢复时核对已有策略，拒绝覆盖不同策略。模板策略不会自动继承给新 actor。
提交前检查 readyz，启动失败可继续准备同一 actor；POST 一旦发出就只查询结果，不自动重放。
共享锁用原子 hard link 发布完整 owner，避免 gVisor 的 flock 只在 sandbox 内生效。
锁不按年龄过期；Substrate 确认 actor 已删除后才清理该 job 的遗留锁。
未知 owner 或控制进程异常退出的锁保守保留，恢复步骤见 storage.md。

部署配置、固定版本与挂载示例见 [Substrate 部署](../../deploy/substrate/README.md)。
已在本地 Docker + kind 上运行真实控制面、gVisor actor 和 CSI 持久卷，模型 HTTP 使用 mock。
临时检查脚本不在仓库；结果范围见部署文档，不代表云端部署或真实模型质量验收。
