# WorkBuddy 与豆包桌面 MCP

桌面接入使用各客户端已有的登录状态。AuToDo 默认只启用 Codex。使用桌面接入时，将需要的执行器加入 `config.local.json` 的 `enabled_executors`，保留用于独立验收的 `codex`，并将对应 transport 设为 `mcp`。

```json
{
  "enabled_executors": ["codex", "workbuddy"],
  "workbuddy_transport": "mcp"
}
```

## WorkBuddy

在项目根目录运行以下命令。把参数换成 AuToDo 项目的绝对路径。

```powershell
python scripts/install-workbuddy-connector.py --root <项目绝对路径> --check
python scripts/install-workbuddy-connector.py --root <项目绝对路径>
```

`--check` 预检配置。安装器管理 `todo-dispatcher` 条目，备份现有连接器配置并保留其他条目。若需要更新同一项目先前安装的条目，可用 `--replace-dispatcher-only`。安装后在 WorkBuddy 中信任并启用连接器，再重启 AuToDo，按 [桌面会话待命](desktop-worker-standby.md) 操作。

## 豆包

将 `doubao` 加入启用列表并设置 `"doubao_transport": "mcp"`。在豆包客户端按 [连接器配置](doubao-connector-setup.md) 添加独立的 STDIO 连接器。配置完成后重启 AuToDo，并在豆包专用会话等待任务。

## 执行范围

- 支持默认或新建工作区内的有界 UTF-8 文本文件任务。模型和思考强度由桌面会话选择。
- 普通会话可加载连接器并列出工具。只有专用会话实际调用等待工具时才占用对应执行器的派工连接。
- 领取后的文件操作仅在本次租约内有效。提交、取消或断线结束租约，之后可重新开启待命。连接器的限制适用于它提供的工具，客户端原生工具仍由客户端控制。
- 交付由独立 Codex 检查，随后进入 AuToDo 的人工复审。
