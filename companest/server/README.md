# server：管理 API 与 actor 服务接口

输入是管理员请求、渠道桥接请求或 actor 的受限回调，输出是 JSON、WebSocket 事件和任务结果。
模型任务通过 JobManager 发送到 Substrate；API 进程不执行 Pi。

| 文件 | 做什么 |
| --- | --- |
| [server.py](server.py) | FastAPI 应用、认证、任务/团队/公司/会话/费用接口，以及内部 actor 授权、计划、事件回调；管理服务启动/停止。 |
| [event_store.py](event_store.py) | SQLite 事件日志、序列号、历史读取和恢复；支持 WebSocket 断连后的历史查询。 |
| [__init__.py](__init__.py) | 包入口。 |

## 两种凭据

管理 API 使用 `COMPANEST_API_TOKEN`，是控制面的管理员权限；它不是面向互不信任公司用户的独立租户 token。
生产环境必须配置鉴权，不能把管理员 API 直接作为公司之间的授权边界。

`/internal/jobs/<job_id>/authorize|schedule|events` 使用专属 job capability。
服务从保存的 job 读取 company/user/workspace，不信任 actor 请求体声称的身份；计划列表/取消也检查所属用户与公司。
actor 只获得自己的能力，不能查询其他公司的账本或使用管理 API。

## 结果和故障

jobs.db 与事件数据库位于控制面持久目录。任务列表展示最近一次观察，单个 job 查询更新远端状态。
结果落盘成功后才删除 actor；网络断连、未确认取消和未知 POST 结果不伪装成已完成。
服务退出停止业务触发器、关闭数据库/连接，不终止 Substrate 中正在运行的任务。
公开知识和公司摘要路由由 [knowledge](../knowledge/README.md) 提供，沿用各自的可见性规则。
