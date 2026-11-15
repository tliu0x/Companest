# memory：持久记忆、检索与整理

文件记忆是业务数据的事实来源。actor 的进程快照不能替代这些文件、workspace 或业务备份。
控制面与相应 scope 的 actor 必须挂载相同的持久数据和锁目录，并校验 storage_id。

| 文件 | 做什么 |
| --- | --- |
| [manager.py](manager.py) | 团队/公司记忆读写、索引、归档、缓存失效、system prompt 和信息补全；使用共享文件锁协调进程间更新。 |
| [backend.py](backend.py) | MemoryBackend 接口与 FileBackend 委派，保持文件业务操作一致。 |
| [qdrant_backend.py](qdrant_backend.py) | 文件存储加派生向量检索；沿用原始 team_id 做过滤，可从文件重建团队索引。 |
| [search.py](search.py) | exact/semantic/hybrid 检索、结果去重和排序；为任务选取有限预算的记忆片段。 |
| [dreamer.py](dreamer.py) | 评分、压缩、GC、概要、长期合并；失败时保留/恢复原数据，记录 dream log。 |
| [archiver.py](archiver.py) | S3 记忆快照上传与恢复，约束归档路径，避免恢复越界。 |
| [__init__.py](__init__.py) | 导出统一的记忆接口。 |

## 一个 team_id 贯穿所有后端

Qdrant 使用一个 `companest_memory_v2` 集合，payload 中直接保存原始 team_id。
例如 `acme/research` 和 `acme_research` 保持不同值，不再替换分隔符，也不生成第二个团队 ID。
所有语义查询带精确 team_id 过滤，返回值再校验归属。
每条记忆的 Qdrant 原生 UUID 由 `(team_id, memory_key)` 确定；这是记录主键，确保不同团队的同名文件互不覆盖或删除。

旧的按团队拼接集合名可能发生碰撞，因此新实现不回退读取旧集合。
已有 Qdrant 部署需要使用配置一致的 `backend.reindex_team(team_id)` 从文件重建派生索引；错误会向调用者报告。
升级步骤见 [部署说明](../../deploy/substrate/README.md#existing-qdrant-indexes)。
actor 仅应获得所在公司允许的数据服务权限；共享 Qdrant 的管理员凭据不能作为公司的隔离凭据。

## 后台工作

`dream_nightly` 和 `dream_deep` 在 actor 中运行，并通过控制面授权模型费用。
公司 actor 的 dream log 保存在本公司目录，不能写全局公司汇总文件。
`memory_backup` 是控制面到 S3 的业务备份，无需模型；周期和启用条件见 [job 清单](../jobs/README.md)。
