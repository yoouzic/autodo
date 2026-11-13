# CodeBuddy 独立执行器

安装并登录官方 `@tencent-ai/codebuddy-code` CLI 后，将 `codebuddy` 加入 `config.local.json` 的 `enabled_executors`。AuToDo 以非交互方式调用 CodeBuddy CLI，WorkBuddy 桌面 MCP 是独立接入方式。

## 在派工台使用

1. 刷新派工台，在“执行资源”中选择 `codebuddy`，或将任务拖到它的卡片。
2. 在“配置模型与思考强度”中选择 `codebuddy`，保存该执行器的默认值。
3. 单项任务详情选择 `codebuddy` 后，可覆盖这一次运行的模型和思考强度。覆盖值在启动时冻结，不修改默认值或其他运行。
4. 当前默认模型是 `glm-5.3-flash`，默认思考强度为 `low`。模型访问权限由 CodeBuddy 账号决定。
5. 交付物先由独立 Codex 验收，再进入“待我复审”。本人点通过后才完成滴答任务。

目录继续按 `workspaces/codebuddy/<任务摘要--短哈希>/runs/<运行时间>/workspace` 整理。单次运行不保存可恢复的厂商会话；诊断 session ID 不能被当成可 resume 的会话。后续任务可以通过派工台的工作区选择复用文件。

## 模型目录与登录

当前目录通过已登录 CLI 的标准 ACP 查询读取，共 17 个 CLI 模型。只调用 initialize 与空 session/new，不调用 session/prompt，不修改默认模型；从 availableModels 的真实 modelId 与 effortOptions 提取名称和思考档位。目录包括 hy4free、hy4std、space-bunny、minimax-m3.1-flash、kimi-k3.1-pro 等当前条目。此前安装包静态 product.internal.json 中的部分 ID 和档位已落后，因此不再作为默认刷新来源。未知模型 ID 仍可填写，但不提供未验证的强度选项。

升级 CLI、账号或模型目录变化后，重新查询并平滑重启派工台。认证仍由官方 CLI 管理，脚本不读取账号文件；查询失败时不会替换旧目录：

```powershell
python -X utf8 -B scripts/refresh-codebuddy-catalog.py --source live
```

需要重新登录时运行 `scripts/login-codebuddy.ps1`，在官方 CLI 中使用 `/login`。脚本使用空临时目录和关闭 hooks 的配置，凭据仍由厂商 CLI 管理，不复制到派工台。

如需离线排障，可显式使用 `--source installed --region internal` 导出安装包快照；这不能代表账号当前目录。前端在默认配置及单次分配输入框下提供“选择模型（全部 N 个）”完整下拉列表，模型 ID 输入框继续支持手动填写。卡片标注“当前默认”和可选数量，避免把默认值误认为唯一模型。

AuToDo 的默认值与客户端全局默认值分别保存。在原生客户端切换模型后，仍可在 AuToDo 中为后续任务单独设置默认值。

## 权限与已知范围

- 仅开放已验证的 Read、Write、Edit 和纯结果工具 StructuredOutput；不开放搜索子进程、终端、网络工具、子智能体或其他 MCP。隔离复测发现 Grep 的厂商子进程启动被当前 Windows 沙箱拒绝，因此没有把它继续列为可用工具。
- 保留外层 Windows workspace-write 沙箱。原生读工具保持可信工作区限制；写入规则只匹配本次绝对工作区，路径中的 glob 字符做字面转义，并通过 JSON 传递，避免标题里的逗号拆成额外许可。
- 不使用跳过权限参数，不给整个账号目录写权限。关闭全局 hooks、定时/后台任务、会话持久化以及新版 CLI 的额外 trace/file log 写入；进程输出由派工台归档。
- Windows 只额外允许官方 CLI 在 `AppData/Local/CodeBuddyExtension/Data/Public/auth` 内完成 OAuth 刷新所需的锁和原子写入。该目录不会加入模型工具的可信工作区，模型读写规则不放行它；派工台不读取或复制令牌。整份 `.codebuddy` 配置目录仍不授予写权限。
- 使用 `dontAsk`，需要额外批准时明确失败。代码任务还需有可信仓库检查命令，不能把执行器自己写的“测试通过”当成验证。
- 本次实测范围是默认新建文本工作区。已有目录原地执行、命令工具、二进制交付和厂商原会话恢复没有在本次完成端到端验证。外部 runtime 路径含特殊 glob 字符时仍可能被当前外层沙箱拒绝；不得扩大目录权限来绕过。

官方参考：[CLI 参数](https://www.codebuddy.ai/docs/cli/cli-reference)、[权限规则](https://www.codebuddy.ai/docs/cli/permissions)、[认证说明](https://www.codebuddy.ai/docs/cli/iam)。
