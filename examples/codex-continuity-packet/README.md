# Codex continuity packet example

这个目录演示自建后端怎样把 Serein 的结构化续接资料预装到一个新的 Codex thread。

它支持两种输入：

- `--serein-url`：用后端环境变量中的 Gateway Key 请求 Serein，并自动读完分页。
- `--packet`：读取本目录的虚构响应，适合离线检查和临时 smoke test。

先确认已安装并登录 Codex CLI。离线测试：

```powershell
python .\examples\codex-continuity-packet\prepare_codex_window.py `
  --packet .\examples\codex-continuity-packet\packet.example.json `
  --cwd . `
  --ephemeral
```

连接自己的 Serein 实例时，只在后端设置密钥：

```powershell
$env:SEREIN_GATEWAY_KEY = "替换成自己的 Gateway Key"
python .\examples\codex-continuity-packet\prepare_codex_window.py `
  --serein-url https://你的域名 `
  --window-id new-window-001 `
  --source-session-id old-window-017 `
  --cwd D:\你的项目
```

成功后脚本只输出 JSON，其中包含新 `thread_id` 和注入条数。正式接入时，把相同的组装和 App Server 调用放进自己的后端，并在注入成功后才更新前端的活动窗口。

原文前的元数据行会同时保留：

- `id`：Serein 原文 ID，例如 `raw:42`；可用 `source_message_read` 精确读回。
- `source_message_id`：原聊天来源提供的消息 ID；用于追溯来源。

不要删掉 ID 后只传正文，也不要用 `source_message_id` 冒充 Serein 的 `raw:…` ID。更完整的边界与协议说明见 [`docs/codex-continuity-packet.md`](../../docs/codex-continuity-packet.md)。
