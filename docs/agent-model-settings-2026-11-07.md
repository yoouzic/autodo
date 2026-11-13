# Agent 默认值与单次会话模型配置

## 使用

1. 任务看板 → 执行资源 → **配置模型与思考强度**：选择 agent，填模型并选择其可用强度，保存默认值。留空回到项目或厂商默认；保存不派发任务。
2. 打开可分配任务 → 选择具体执行器 → **本次模型 / 本次思考强度**：仅覆盖这次派工。自动分配不接收专属模型参数。
3. 每次运行记录保留 **本次启动配置**。历史记录未记录过配置时显示未知，不用当前默认值补写历史。

默认值在 SQLite 的 `agent_settings` 中，派工覆盖在 `task_controls.execution_overrides`，每次启动快照在 `runs.execution_settings` 和运行目录 `execution-settings.json`。同一任务重新派工会创建新运行；运行中不能通过本表单热切模型。配置保存与派工均使用原有登录、同源、CSRF、命令幂等和版本冲突检查。

项目配置也可写 `agent_settings: {"codex":{"model":"gpt-6.1-sol","effort":"high"}}`。字段只接受 model/effort，不接受任意命令、路径、环境变量或权限设置。前端默认值优先于项目设置，单次覆盖最高；兼容档位可继承，不兼容的继承档位回到新模型默认，显式无效组合直接拒绝。

## 本机能力核查

| 执行器 | 模型与强度传递 | 当前限制 |
|---|---|---|
| Codex | `--model`，`-c model_reasoning_effort` | 使用本机非敏感模型目录的逐模型档位；原难度规则保留；独立验收另用既有配置 |
| DSH | 每次运行的完整 `agent-default-model` patch：provider/model/reasoningEffort | 安装包明确支持 deepseek-flash、deepseek-v4-pro，provider 为 deepseek-official；档位 off/low/high/max；Pro 目录支持不代表已验证账号额度 |
| ZCode 0.17.1 | 独立 app-server 会话的 setModel/setThoughtLevel，实际快照核对 | 本机目录当前为 glm-5.3-flash，low/high/max；原 provider 认证配置不复制、不修改 |
| Qoder 1.1.84 | `--model`、`--reasoning-effort` | Qwen3.8-Max: low/medium/xhigh；GLM-5.3: low/high/max；DeepSeek-V4-Pro: high/max；其余未核实模型仅默认强度 |
| Antigravity 1.5.2 | 模型族解析为具体 slug，`--model` 与匹配的 `--effort` | Gemini 3.8 Flash: low/medium/high；本机其他模型查询为不可调节，不能强加档位 |

模型目录和账号访问权可能变化。Codex 目录从本机 `models_cache.json` 只投影模型名/档位；ZCode 目录从只读 app-server 查询生成并存入 `data/zcode-model-catalog.json`，启动时加载，实际执行时再次向厂商核对。目录缓存不是模型访问权保证；厂商拒绝仍作为失败保留。更换 ZCode 账号/模型目录后应重新执行无推理 catalog 查询并刷新缓存，桥接协议见[ZCode 文档](zcode-session-configuration-2026-11-07.md)。

参考：[官方 Codex 配置](https://learn.chatgpt.com/docs/config-file/config-reference)、[Qoder 模型](https://docs.qoder.com/cli/model)、[Qoder 参数](https://docs.qoder.com/cli/cli-reference)、[Antigravity effort](https://antigravity.google/docs/cli/commands/effort/)。DSH/ZCode 同时核对了当前安装包的配置消费路径，未根据其他版本猜测参数。
