# knowledge：外部信息、公司摘要与公共知识

输入是外部 feed 响应、公司摘要或已发布公共文档；输出是结构化信息、私有记忆和供工具读取的公开检索结果。
这不是执行引擎，也不把公司私有数据自动发布到公共知识库。

| 文件 | 做什么 |
| --- | --- |
| [feeds.py](feeds.py) | Brave、RSS、Reddit、HN、X、OpenBB 及预测市场等数据适配；统一响应、缓存和限速。 |
| [digests.py](digests.py) | 校验市场、仓位、PnL、风险、结算和审批摘要；按 company/schema/idempotency_key 摄取到公司私有存储。 |
| [public.py](public.py) | 公共文档和索引模型，按新鲜度/标签/来源检索；API 与 agent 工具只读公开内容。 |
| [s3.py](s3.py) | 上述知识存储共用的 S3 JSON 读写封装。 |
| [__init__.py](__init__.py) | 包入口。 |

Feed 缓存是进程内优化；feed.json、briefing.json 和 digest 业务状态由 memory/公司命名空间持久化。
S3 对象是知识数据，不是 actor 内存快照。
公司摘要和公开知识使用不同边界；仅因为 doc ID 存在，不能绕过 visibility 检查。
后台收集的触发规则见 [jobs](../jobs/README.md)，实际网络接入需要相应凭据和部署出站权限。
模拟检查使用 `test_feeds.py`、`test_digests.py`、`test_public_knowledge.py`；带 `live_network` 的检查默认排除。
