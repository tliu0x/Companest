# companies：公司配置与业务扩展

公司是业务授权边界。输入是 `companies/<company_id>/company.yaml`、团队文件或进程内注册的组件；输出是公司注册信息、业务计划和 CEO 结果。

| 文件 | 做什么 |
| --- | --- |
| [company.py](company.py) | 公司/绑定/偏好/CEO/计划的数据模型；扫描、保存、删除配置，按 channel/chat/user 解析公司，检测文件变化。 |
| [component.py](component.py) | CompanyComponent 协议、初始化/清理钩子、线程与事件循环桥接，以及限制到公司的记忆视图。 |
| [ceo_engine.py](ceo_engine.py) | 根据目标、KPI 和可访问团队生成 CEO soul 与周期任务提示词。 |
| [output.py](output.py) | OutputSink 接口与 MemorySink；保存 CEO 周期结果供后续任务读取。 |
| [__init__.py](__init__.py) | 包入口。 |

## 归属规则

私有团队使用 `company/team`；CEO 使用 `company-<company_id>`，仅所属公司可调用。
共享团队必须在公司的 `shared_teams` 白名单中。未知或停用公司不能提交新任务，已存在 actor 的后续服务授权也会被拒绝。
停用不等同于撤回已经完成的工具副作用；需要终止正在运行的任务时使用任务取消接口。

控制面 Orchestrator 负责应用公司配置、刷新路由、注册计划、建立记忆路径以及发出初始化事件。
公司计划和 CEO 周期通过 JobManager 执行，详见 [job 清单](../jobs/README.md)。

组件对象和 Python 回调是当前进程的扩展，不会序列化并发送给远端 actor。
actor 需要的团队、模型、MCP 与记忆配置必须存在于该 scope 的挂载/镜像中；不能只在控制面的内存注册表里修改它们。
组件钩子用于业务注册和输出，不应自行启动执行 worker、保持 agent 实例或替代 Substrate 生命周期。

CompanyMemoryNamespace 的运行时方法只接收完整 team_id（例如 `acme/research`），
不会把短名称自动加上公司前缀。旧 manifest 的目录名仅在配置初始化时解析一次；
TeamConfig 创建后，其 ID 经路由、工具和存储原样传递，物理目录保存在 source_dir 中。
