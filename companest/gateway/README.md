# gateway：消息渠道与控制面连接

输入是渠道消息或 WebSocket 协议帧，输出是任务回复、进度和通知。
Gateway 负责连接、消息关联和格式转换；收到任务后仍经 Orchestrator/JobManager 进入 Substrate。

| 文件 | 做什么 |
| --- | --- |
| [channel.py](channel.py) | ChannelAdapter/TelegramAdapter，解析绑定，将消息上下文交给路由并格式化回复。 |
| [client.py](client.py) | WebSocket `req`/`res`/`event` 客户端、请求关联、鉴权、心跳和重连。 |
| [master.py](master.py) | Companest 控制面对 Gateway 的连接；分发入站任务、返回执行结果和业务通知。 |
| [server.py](server.py) | Gateway 服务端与 Telegram bot，接受控制面连接并转发消息。 |
| [__init__.py](__init__.py) | 包入口。 |

连接状态和待响应请求只存在于进程内；业务任务结果由 jobs.db 保存。
连接超时或客户端断开不能被解释为 actor 已停止，不应自动重发可能已执行的任务。
公司解析使用 channel/chat/user，不能把消息文本中的公司名称当成授权。
生产连接使用 `COMPANEST_MASTER_TOKEN`；修改认证或网络暴露前阅读 [SECURITY.md](../../SECURITY.md)。
