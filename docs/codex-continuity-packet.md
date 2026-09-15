# Codex 换窗包接入示例（自建前后端）

这份示例面向自己维护聊天前端、后端和会话映射的 Serein 使用者。目标是在用户主动换窗时，由后端读取 Serein 已选定的续接资料，新建一个 Codex thread，并在第一条真实新消息之前预装最新窗影、Scene、Event 和原话。

“换窗包”是本文对这组 Serein 连续性材料的称呼，不是 Codex 的官方功能名。Codex 侧使用官方 App Server 的 `thread/start` 和 `thread/inject_items`。示例不会让 Serein 网页直接控制 Codex，也不会读取、覆盖或伪造 Codex 的会话 JSONL 文件。

可运行代码与虚构数据位于 [`examples/codex-continuity-packet/`](../examples/codex-continuity-packet/README.md)。Codex 协议本身以 [Codex App Server 官方文档](https://developers.openai.com/codex/app-server) 和当前安装版本生成的 schema 为准。

## 前后端边界

1. 自建前端只把“换窗”和来源会话标识发给自己的后端。
2. 后端持有 Gateway Key，请求 `POST /v1/extensions/resume`，跟随 `next_cursor` 读完每一页。
3. 后端校验材料属于当前用户可访问的实例与会话，并限制条数和总字符数。
4. 后端启动 Codex App Server，新建 thread，再调用 `thread/inject_items`。
5. 只有注入成功、且新 Codex thread ID 已保存到自己的会话表后，前端才切到新窗口。

Gateway Key 和 Codex 进程都留在后端，不要放进浏览器。若任何一步失败，保留旧窗口为活动窗口。

## Serein 材料怎样进入 Codex

| Serein `kind` | Codex history role | 保留的身份 |
| --- | --- | --- |
| `shadow` | `developer` | `id`、标题、正文、revision |
| `scene` | `developer` | Scene `id`、标题、正文、revision |
| `event` | `developer` | Event `id`、标题、正文、revision |
| `raw` | 原来的 `user` / `assistant` | 原文 `id`、`source_message_id`、来源、会话、时间和未改写正文 |

每条原文至少保留响应中的 `id`，例如 `raw:42`。它是这个 Serein 实例中可交给 `source_message_read` 精确读回的原文 ID。`source_message_id` 是导入来源提供的上游消息 ID，用于追溯来源；两者不要互相替代，也不要只保留正文。

这些 ID 无法作为 Codex message 的独立字段发送，所以示例把它们写在原文前的清晰元数据行中，同时保持原文原来的 role 和正文。结构化材料前还会加入一条 developer 边界，说明后续内容是历史资料、不是当前指令，当前用户消息优先。

## 读取续接资料

先在 Serein 的“设置 → 功能”开启开窗续接并选择要带入的区块。后端调用：

```http
POST /v1/extensions/resume
Authorization: Bearer <Gateway Key>
Content-Type: application/json

{
  "window_id": "new-window-001",
  "source_session_id": "old-window-017"
}
```

`source_session_id` 用来把“最近原话”或“尚未整理的原话”限定到来源会话；没有这个边界时不要随意把整个实例的近期原话带入某个用户窗口。响应中的 `injected: false` 表示 Serein 只准备了资料，尚未替你的宿主注入 Codex。

当 `has_more` 为 `true` 时，用同样的参数加上返回的 `next_cursor` 继续请求。必须读完全部页面；不要把“拿到第一页”当作换窗包完整。示例会按 `body_offset` 合并被分页切开的长正文，并要求同一次读取的 `collection_id` 保持一致。

## Codex App Server 顺序

App Server 使用 JSON-RPC 风格的 JSONL 消息。最小顺序是：

1. `initialize`，随后发送 `initialized` notification。
2. `thread/start` 创建新 thread。
3. `thread/inject_items` 注入已经校验和组装的 history items。
4. 保存新 thread ID，并把它绑定到自建前端的新窗口。
5. 用户真正发出下一条消息时，再调用 `turn/start`。

若只是继续同一个 Codex thread，应保存并恢复原 thread ID，不需要另建换窗包。

## 版本与验收

App Server 的 schema 随 Codex 版本发布。接入时记录已验证的 Codex CLI 版本，并从本机版本生成 schema：

```powershell
codex app-server generate-json-schema --out .codex-app-server-schema
```

升级 Codex 后先重新生成 schema，再用示例的 `--ephemeral` 模式跑一次临时 thread。临时 smoke test 成功，只证明 Codex 已接收这些 history items；自建后端仍应单独测试“注入成功后才切窗”、失败回滚、权限边界和 thread ID 持久化。
