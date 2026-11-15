# memory：持久记忆、检索与整理

文件记忆是业务数据的事实来源。actor 的进程快照不能替代这些文件、workspace 或业务备份。
控制面与相应 scope 的 actor 必须挂载相同的持久数据和锁目录，并校验 storage_id。

| 文件 | 做什么 |
| --- | --- |
| [manager.py](manager.py) | 团队/公司记忆读写、索引、归档、缓存失效、system prompt 和信息补全；使用共享文件锁协调进程间更新。 |
| [backend.py](backend.py) | MemoryBackend 接口与 FileBackend 委派，保持文件业务操作一致。 |
| [search.py](search.py) | 按 key、摘要和标签做关键词检索，包含可选归档查询、去重和排序；为任务选取有限预算的记忆片段。 |
| [dreamer.py](dreamer.py) | 评分、压缩、GC、概要、长期合并；失败时保留/恢复原数据，记录 dream log。 |
| [archiver.py](archiver.py) | S3 记忆快照上传与恢复，约束归档路径，避免恢复越界。 |
| [__init__.py](__init__.py) | 导出统一的记忆接口。 |

## FileBackend 与原始 team_id

配置默认为 `memory_backend: file`，只支持现有 FileBackend。它把原始 `team_id` 直接传给 MemoryManager，
继续使用已经注册的团队目录。例如 `acme/research` 和 `acme_research` 保持不同值，
不替换分隔符，也不生成第二个团队 ID。已有记忆文件、路径和元数据保持原样。

`memory/.index.json` 记录文件的重要性、摘要、标签等元数据；检索按 key、摘要和标签做关键词匹配。
`memory_search` 不再接受 mode 参数；解释结果中的 `_mode` 保持为 `exact`。
任务检索仍可读取命中文件生成有字符预算的上下文。没有向量服务、embedding 下载或 FTS5。
旧的 `memory_config` 必须从配置中删除，其他后端名称会在配置校验时被拒绝。
升级步骤见 [部署说明](../../deploy/substrate/README.md#file-memory)。

公司访问权限由调用边界和公司命名空间校验；actor 只挂载所属公司及获准共享团队的数据。
FileBackend 是存储适配器，不能替代这些权限检查和挂载隔离。

## 后台工作

`dream_nightly` 和 `dream_deep` 在 actor 中运行，并通过控制面授权模型费用。
公司 actor 的 dream log 保存在本公司目录，不能写全局公司汇总文件。
`memory_backup` 是控制面到 S3 的业务备份，无需模型；周期和启用条件见 [job 清单](../jobs/README.md)。
