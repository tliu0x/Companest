# 测试范围

默认测试验证业务契约，模型、Substrate 传输和外部服务使用模拟对象。
`live_network` 用例默认排除，只有明确进行外部集成验收时才运行。
Qdrant 文件委派测试不下载 embedding 模型。

```bash
python -m pytest tests/ -q
python -m compileall companest tests -q
python -m flake8 companest tests --select=E9,F63,F7,F82
```

| 保留的检查 | 对应测试 |
| --- | --- |
| API 鉴权、任务/会话/事件契约、完整结果与历史 | `test_server.py`、`test_session_api.py`、`test_event_api.py`、`test_jobs.py` |
| 公司绑定、私有记忆、动态注册、异步钩子和权限 | `test_company_api.py`、`test_company_async.py`、`test_component.py`、`test_sessions_security.py` |
| 工具实际可见性、workspace 路径约束、并发运行上下文 | `test_pi_runtime.py`、`test_coding.py`、`test_tools.py` |
| 预算、模型端点协议与凭据解析 | `test_cost_gate.py`、`test_model_routing.py`、`test_cascade.py` |
| 模式和工作流的实际执行、分支失败、允许的团队 | `test_modes.py`、`test_team_mode.py`、`test_council.py`、`test_collaborative.py`、`test_delegation.py` |
| 记忆读取/索引/缓存、归档恢复、Dreamer 事务与容量限制 | `test_memory_*`、`test_archiver.py`、`test_dreamer.py` |
| Feed 协议、摘要幂等写入、公开知识边界 | `test_feeds.py`、`test_info_collection.py`、`test_digests.py`、`test_public_knowledge.py` |

本次精简删除了 39 个重复或过于贴近实现的测试函数（68 个参数化用例），
包括工具名称常量、重复 JSON 围栏解析、重复 getter/注册表检查，以及已由
端到端行为覆盖的级联和记忆示例。保留默认测试从 521 项变为 453 项。
委派测试从旧的 `test_conditional.py` 更名为 `test_delegation.py`，与业务工作流命名一致。

迁移专项故障模拟放在仓库外临时脚本中，覆盖 actor 断连/重启/取消、
共享记忆竞争、workspace 锁、能力撤销、公司范围及向量集合隔离。
这些临时脚本不随仓库分发，也不代表真实模型或集群部署验收。
