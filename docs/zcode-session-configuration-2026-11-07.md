# DSH / ZCode 单次模型配置核查与 ZCode 桥接验证

核查时间：2026-11-07，UTC+8。只覆盖本机已安装版本及当前账号的可用模型，不把厂商全部模型列表视为本机权限。

## DSH：使用现有 patch overlay

本机 `@deepseek-ai/dsh` 与 `dsh-headless` 均为 `0.2.0-rc.2`。`headless --help` 没有 `--model` 或 `--effort`，只有任务、JSON 和 session-id 等选项。

在本次运行已有的 `dsh.patch.json` 中覆盖：

```json
{"id":"agent-default-model","config":{"provider":"deepseek-official","model":"deepseek-flash","reasoningEffort":"low"}}
```

本机组合配置当前为 `deepseek-official/deepseek-flash`。`dsh-headless/lib/index.js` 读取 `agentDefaultModel.currentSelection()`，把 provider/model 送入新 agent，并通过 `installModelSelection` 安装完整 selection；本机 `dsh-agent-default-model` 的 schema 确实包含可选 `reasoningEffort`。DeepSeek 官方 adapter 支持 `off/low/high/max`，省略沿用 provider 默认；不能把 Codex 的 medium/xhigh 枚举直接套用。

实际组合验证发现，此 patch 的 `config` 是整段替换，不是逐字段保留。**provider 和 model 必须一起提供**；只写 effort 会丢失必填字段。不能将仅强度覆盖偷偷解释成固定 provider/model，尤其不能覆盖用户将来换过的默认路由。

本机 adapter 目录还明确列有同一 `deepseek-official` provider 下的 `deepseek-v4-pro`，与 `deepseek-flash` 同样声明四个档位。当前组合没有覆盖 models/thinking/reasoningEffort；API-key 插件直接从该本地目录返回可选项。此处确认的是安装版本的目录能力，未实际请求 Pro 模型或确认它的账号额度。

参考 [默认模型插件](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/core/agent-default-model/README.md) 和 [DeepSeek adapter](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/llm/llm-deepseek/README.md)。当前会话覆盖应留在每次运行 patch 中，不调用保存全局默认的接口。

## ZCode：本机 headless 参数不足，使用会话协议

本机 ZCode CLI 为 `0.17.1`，实际 bundle 为安装目录的 `resources/glm/zcode.cjs`。`--prompt` 没有模型/思考强度参数，旧 `ZCODE_MODEL` 也不是新 provider 格式的可靠覆盖入口。[官方配置说明](https://github.com/zai-org/ZCode/blob/main/apps/zcode-cli/docs/usage/03-config.md) 将模型配置与运行配置分开。

桥接器由厂商 CLI 从原位置读取认证配置，只把会话模型标识和档位传入 `app-server` 的 NDJSON 协议。含凭据的 provider 配置留在厂商原目录。

桥接入口：`dispatcher/zcode_session.py`。调用方必须继续使用本项目的 `sandbox_command` 和 `run_process` 硬超时：

```text
python <absolute-project>/dispatcher/zcode_session.py
  --command-json <run>/zcode-command.json
  --request <run>/zcode-request.json
```

- command JSON 是原 CLI argv 数组，桥接器追加 `app-server`，不用 shell。
- request 包含 `workspace/prompt/model/effort/timeout_seconds`；没有任意 endpoint、认证或 shell 字段。
- `--catalog` 只创建无提示词的 deferred session，读取模型元数据后关闭，不执行推理。
- 所有配置、catalog、事件输出均不导出原 provider 配置或原始错误日志。catalog 的 current 仅允许 providerId、modelId、options.reasoningLevel。

新会话限定 edit 模式，固定文件工具 `Read/Write/Edit/Glob/Grep`；模型实际工具注册和 MCP 注册都会接收 allowlist。额外 denylist 保留终端、联网和子智能体禁用项。工作流、后台任务和自动标题关闭，交互权限请求拒绝。

模型以 `providerId/modelId` 为规范 ID；短 modelId 必须在可用目录内唯一，重名或不可用会拒绝。档位按该模型实际返回的 levels 校验。`session/setModel` 自身必须带模型要求的 reasoningLevel，不能指望后面的 setThoughtLevel 补救。设置后的快照和最终快照都核对实际配置，防止被厂商静默忽略。

完成必须同时有同一 turn 的 `turn.completed` 且 `payload.resultType == success`，最终快照无活动/待权限/后台工作，最终 assistant 完整终结且模型匹配。cancelled、预算/次数耗尽、工具失败、result.success=false、未知档位、非零进程退出或断流均不生成成功报告。输出保留非敏感 `effective_configuration` 和白名单工具阶段审计。

桥接器自身使用有界消息队列和响应限额、接收截止时间，并在结束时关闭所拥有的子进程。但管道写入等 OS 阻塞的最终边界由外层 `run_process` 硬期限兜底；不得脱离外层沙箱/硬超时把它当作完整执行隔离器。
