# memory：持久记忆、检索与整理

文件记忆是业务数据的事实来源。actor 的进程快照不能替代这些文件、workspace 或业务备份。
控制面与相应 scope 的 actor 必须挂载相同的持久数据和锁目录，并校验 storage_id。

| 文件 | 做什么 |
| --- | --- |
| [manager.py](manager.py) | 团队/公司记忆读写、索引、归档、缓存失效、system prompt 和信息补全；使用共享文件锁协调进程间更新。 |
| [history.py](history.py) | 不可覆盖的版本快照、来源上下文、版本冲突检查和文件提交日志；中断后恢复正文、索引与归档的一致状态。 |
| [backend.py](backend.py) | MemoryBackend 接口与 FileBackend 委派，保持文件业务操作一致。 |
| [search.py](search.py) | 按 key、摘要和标签做关键词检索，包含可选归档查询、去重和排序；为任务选取有限预算的记忆片段。 |
| [dreamer.py](dreamer.py) | 评分、按分类去重/合并、过期归档和概要；只提交仍然有效的输入版本，保留原文与来源链，记录 dream log。 |
| [archiver.py](archiver.py) | S3 记忆快照上传与恢复，约束归档路径，避免恢复越界。 |
| [__init__.py](__init__.py) | 导出统一的记忆接口。 |

## FileBackend 与原始 team_id

配置默认为 `memory_backend: file`，只支持现有 FileBackend。它把原始 `team_id` 直接传给 MemoryManager，
继续使用已经注册的团队目录。例如 `acme/research` 和 `acme_research` 保持不同值，
不替换分隔符，也不生成第二个团队 ID。已有记忆文件、路径和元数据保持原样。

`memory/.index.json` 记录文件的重要性、摘要、标签等元数据；检索按 key、摘要和标签做关键词匹配。
`memory_search` 不再接受 mode 参数；解释结果中的 `_mode` 保持为 `exact`。
任务检索仍可读取命中文件生成有字符预算的上下文，支持分类过滤。没有向量服务、embedding 下载或 FTS5。
旧的 `memory_config` 必须从配置中删除，其他后端名称会在配置校验时被拒绝。
升级步骤见 [部署说明](../../deploy/substrate/README.md#file-memory)。

公司访问权限由调用边界和公司命名空间校验；actor 只挂载所属公司及获准共享团队的数据。
FileBackend 是存储适配器，不能替代这些权限检查和挂载隔离。

## 分类、来源与版本

`category` 使用 `fact`（长期事实/决策）、`task`（任务过程）、`experience`（可复用经验）、
`unclassified`（旧数据或尚未分类）。新条目默认 `task`；显式写入 fact/experience 默认进入 long tier，
task/unclassified 默认 short tier。分类与 tier 分别表示用途和整理周期，不替换任何业务 ID。
旧文件无需批量迁移；第一次受管理的修改前会保留 legacy 基线，不编造历史 job 或创建来源。
Python 写入/元数据接口可设置 `pinned=True`，使运行数据跳过 Dreamer 的评分、概要、合并与归档。
级联统计、收件箱、费用/备份日志、CEO 周期结果以及新导入的 manifest seed 会设置该标记；
已有其他运行数据可由管理方显式标记。模型记忆工具不能解除这个标记。

每次写入、元数据修改、归档、删除和恢复都产生递增 `version`，快照位于团队目录下的
`.memory-history/<key>/<version>.json`。快照保存正文、分类、原始 team_id/key、操作、时间以及
`provenance`；后者记录最近修改的 kind、实际 job_id、company_id 和 pi_id（存在时）。
早期来源通过历史版本查询；Dreamer 合并和恢复用 `derived_from: [{key, version}]` 指向同一团队的来源版本。
`updated_at` 表示内容更新时间，`modified_at` 表示该版本的修改时间；读取计数不创建新版本。
actor 从已接受的请求注入 job_id，模型工具参数不能指定或覆盖来源身份。

写入工具用 `expected_version: 0` 创建；修改必须先读取并传回当前版本。版本不符会拒绝写入，
调用方应重新读取再决定如何合并。Python 业务写入接口保留可选 expected_version；省略时在锁内串行提交。
恢复旧版本会创建新的 active 版本，不删除后来的历史。删除表示移出 active，历史内容仍然保留。
历史不会自动裁剪，备份和存储容量规划应包含 `.memory-history`。

一次文件变更先持久化团队内的 `.memory-pending.json`，再写版本、正文、索引和归档投影。
中断后，下次受管理的读写在同一共享锁内完成日志中的文件变更；它不会重跑模型或工具。
因此遇到写盘错误后应查询当前版本，不能假设变更一定未提交。应用读写必须经过 MemoryManager/FileBackend；
直接编辑内部索引、历史或日志不受这个协议支持。普通文件丢失的索引可从历史恢复元数据。
源文件仍保存在原目录；拒绝越界路径、符号链接和内部元数据 key。

## 按需读取与工具

system prompt 只包含简短目录（最多 20 个 key、800 字符）和 overview（最多 2400 字符），
不再自动注入全部长期摘要。Pi 另按当前任务检索有限片段；摘要标出 key、version 和 category。
记忆是参考数据，不能覆盖系统指令。

| 工具 | 用途与返回 |
| --- | --- |
| `memory_overview` | 读取短概要，随后按需检索。 |
| `memory_search` | 按 key/摘要/标签检索，可过滤 category、包含归档；返回元数据与版本，不返回完整正文。 |
| `memory_index` / `memory_list` | 用 offset/limit 浏览目录；返回 entries、next_offset、total。 |
| `memory_read` | 读取 active/archive 或指定 version；返回 text、version、source、category、provenance 和 next_offset。默认每页 4000 字符，最多 16000；翻页时固定 version。 |
| `memory_write` | 写入 JSON，可指定 category、summary、tags；用 expected_version 防止覆盖新内容，返回 key/team_id/version。 |
| `memory_history` | 按 before_version/limit 浏览不含正文的历史；正文通过 memory_read(version) 读取。 |
| `memory_restore` | 指定 version 和当前 expected_version，恢复为一个新版本。 |

工具固定绑定当前团队，不接受可切换公司的 team_id 参数；公司命名空间的历史、读取和恢复方法也验证完整 team_id。
archiver preset 包含 overview/history/restore；其他显式工具列表按需增加工具名，原有 tools_deny 仍然有效。
旧的工具消费者应适配 memory_read/list/index/write 的分页/版本响应；Python `read_team_memory` 仍返回原 JSON 内容。

## 后台工作

`dream_nightly` 和 `dream_deep` 在 actor 中运行，并通过控制面授权模型费用。
公司 actor 的 dream log 保存在本公司目录，不能写全局公司汇总文件。
Dreamer 会给未评分的内容生成摘要，按同一分类合并；完全相同的条目可直接去重，无需模型。
模型合并提示要求保留分歧和来源，不能猜测哪个事实正确。原条目与合并结果在一次提交中归档/写入，
每个输入版本都要仍然匹配，否则丢弃过时结果。低价值、过期且少访问的短期内容进入 archive；
未评分内容不会因为模型返回无效结果而被当作垃圾归档。
一次整理失败会保留之前已提交的批次，不再用旧索引覆盖并发写入。概要也会校验来源版本；
记忆变更会使旧概要失效，后续 Dreamer 再生成。归档可检索、可按版本读取和恢复。
`memory_backup` 是控制面到 S3 的业务备份，无需模型；周期和启用条件见 [job 清单](../jobs/README.md)。
