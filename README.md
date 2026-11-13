# AuToDo

AuToDo 把滴答清单里明确委派的待办交给 Windows 本机的 AI 智能体，按任务类型和难度派工。完成后可在看板上查看交付结果，经 Codex 独立复核与人工批准后回写滴答清单。Python 标准库与 SQLite 保存队列，使用各执行器自身的登录配置。默认只有 **`AI` 标签**的未完成任务会自动执行；其余任务展示在待办栏，可从前端拖动或在详情中明确委派。也可通过 `project_ids` 明确授权整个清单。

默认配置仅启用 Codex CLI；可配置 DSH、ZCode、Qoder CLI、Antigravity CLI。`agents` 命令显示本机探测与启用状态，发现可执行文件不代表登录、任务执行或恢复已通过真实验证。Qoder 桌面启动器不算独立 CLI。

看板卡片按任务日期显示，例如 **11.5 整理资料**；具体时刻按滴答任务时区显示，全天任务不显示午夜，跨年和起止区间保留完整含义。没有日期时标“未设日期”，不会用创建时间代替。高/中/低优先级分别用红/黄/蓝色边线与标签表示；卡片不再显示 `#短编号`，完整任务编号仍保留在详情中供诊断。

## 模型与思考强度

前端“执行资源”下面展开 **配置模型与思考强度**，可以分别保存 Codex、DSH、ZCode、Qoder、Antigravity 的默认设置。任务详情的 **本次模型 / 本次思考强度** 可覆盖本次派工；先选择具体执行器，留空继承。自动分配使用最终选中执行器的默认值。

优先级为本次覆盖 → 前端保存的 agent 默认值 → `config.local.json` 的 `agent_settings` → 原有项目/CLI 默认。换模型时，兼容的强度继续继承，不兼容则使用新模型默认；显式提交不支持的组合会报错。Codex 未固定强度时仍按任务难度选择。

运行开始前会冻结本次启动参数，详情中的 **本次启动配置** 和历史记录均展示该快照。修改默认值只影响之后启动的任务，不会改变正在执行的会话或独立验收配置。厂商桌面端之外创建的会话不由此面板修改。

各模型支持的强度不同；例如 DSH 为 off/low/high/max，ZCode 的 GLM-5.3-Flash 为 low/high/max，Antigravity 的 Gemini 3.8 Flash 为 low/medium/high。未知模型可输入标识，但只有能力已核实的模型才开放强度；DSH 还需要已核实的 provider/model 配对。详细支持范围和验证记录见[模型配置说明](docs/agent-model-settings-2026-11-07.md)。

## 安装与授权

需要 Windows、Python 3.11+、Node.js/npm，以及已安装并登录的 Codex CLI。代码类任务还需要 Git。

在项目目录的 PowerShell 中运行：

```powershell
.\scripts\setup.ps1
python -m dispatcher auth
python -m dispatcher doctor
```

安装脚本把官方 `@suibiji/dida-cli@0.1.15` 固定安装到项目 `.tools`。`auth` 调用官方 CLI 的 OAuth 浏览器登录，由用户在官方页面完成授权。程序不读取认证文件，不要求粘贴 access token 或 API key。Codex 的登录由自身管理；需要时先在可见终端运行 `codex login`。

`doctor` 仅调用 `dida auth status` 和 `codex login status`，过滤官方 CLI 输出中的 Token 行；成功的登录状态不等于真实任务已经完成端到端验证。官方 CLI 的收集箱覆盖若无法核实，服务会记录警告，不会把读取失败当作空清单。

## 运行

日常使用可双击项目根目录的 **`AuToDo.cmd`**：已有服务时直接复用；需要启动时优先使用同项目、同用户的 `Interactive + Limited` 计划任务，未安装计划任务才调用 `scripts/start.ps1`。确认本机接口健康后自动打开派工台，启动失败会显示原因并保留窗口。请从普通用户窗口运行；入口不提权、不修改执行策略，也不新增自启或桌面快捷方式。重复点击不会重启已有服务。

```powershell
python -m dispatcher sync       # 仅同步，不执行
python -m dispatcher status     # 查看本地队列与执行器状态
python -m dispatcher run-once   # 同步、重试待回写结果，再最多执行一个新任务
.\scripts\start.ps1            # 隐藏后台运行
.\scripts\status.ps1
.\scripts\stop.ps1             # 请求优雅停止
```

本机派工台：[http://127.0.0.1:8765](http://127.0.0.1:8765)。六列看板每 2 秒更新：待办、排队、处理中、待复审、审核通过、已完成。可拖动任务到 Codex / DSH / ZCode / Qoder / Antigravity 或自动分配区；也可在详情中选择执行器及已配置仓库。执行中的任务不能改派。页面只在 daemon 运行时提供；日志位于 `data/daemon.stdout.log` 和 `data/daemon.stderr.log`。

复审抽屉提供交付文件预览/下载、真实检查结果、执行与复核日志和运行历史。自动检查通过后停在 `awaiting_review`，只有你点击「批准并完成滴答」才允许回写。退回时必须填写意见，任务暂停，重新分配后意见进入下一轮提示词；旧工作区和产物继续保留。

工作区页支持打开目录、归档和恢复；归档仅调整展示元数据，不删除文件。当前正在运行或等待批准的工作区不可归档。界面使用 SWU TIC 原始纸纹与 YouziAuth 的配色参考，素材来源见 `dispatcher/static/assets/README.md`。

控制请求绑定本机 Host、同源 Origin 和随机令牌，通过 SQLite 持久命令队列交给 daemon 执行；重复请求幂等，陈旧任务版本会返回冲突。源内容、当前运行与产物 SHA-256 均参与批准校验。浏览器不能提交任意 shell 命令或任意文件路径。

停止脚本只写入 `data/stop.request`，不根据 PID 杀进程。当前执行中的任务会完成或超时后退出；再次启动会清除旧的停止请求。系统锁防止重复 daemon，并阻止运行期间用 `sync`、`retry` 或 `run-once` 修改队列。启动脚本检查系统锁，已运行时不再启动。

可显式运行 `scripts/install-autostart.ps1 -Mode Logon`，登记 Windows **登录后自启**：使用当前用户的 `Interactive + Limited` 计划任务，登录后延迟 20 秒启动监督进程，保持派工台和 SSH 隧道运行。安装器不会自行启动服务；`-ValidateOnly` 仅做只读预检。此模式需要先登录 Windows，不设置自动登录，也不支持停在登录界面的无人值守启动。安装、停止和卸载步骤见 [Windows 登录自启](docs/windows-autostart.md)。

## 手机远程访问

AuToDo 可以通过自备的 HTTPS 域名、nginx 中继和 SSH 反向隧道在手机上访问。队列、浏览器会话、执行器与工作区保存在运行 AuToDo 的 Windows 电脑上。手机浏览器登录后可以派工、查看进度与交付物、批准或退回结果。

配置 `remote_access.mode` 为 `password_proxy`，由 `proxy_secret_file` 引用本机私有的反代密钥文件。启用 `tunnel_enabled` 后，隧道随服务启停。用 `python -m dispatcher mobile-setup` 生成 30 分钟有效的一次性设置链接。完整步骤见 [手机远程访问](docs/mobile-remote-access-2026-11-06.md)。

## 配置与任务标签

默认配置来自 `config.example.json`，个人修改写入未跟踪的 `config.local.json`；嵌套对象按字段合并，数组整体替换。示例：

```json
{
  "max_concurrent": 2,
  "project_ids": [],
  "repositories": {
    "myapp": "D:/projects/myapp"
  }
}
```

- `AI`：将任务纳入自动派工范围。加入此标签后，daemon 下次同步即可执行。
- `difficulty:easy` / `difficulty:medium` / `difficulty:hard`，或 `简单` / `中等` / `复杂`：显式指定难度；多个难度取最高。没有标签时根据任务说明长度、步骤与架构/重构等关键词估级。
- `repo:myapp`：代码任务选择配置中的仓库；使用默认工作区且无有效仓库映射时进入 `needs_input`。默认/新建模式会 clone 仓库，未提交的本地修改不会自动带入；明确选择已有工作区时直接修改原目录。Qoder 的代码任务仍需配置可信验证命令。
- `agent:codex` / `agent:dsh` / `agent:zcode` / `agent:qoder` / `agent:antigravity` / `agent:codebuddy`：显式指定执行器，可省略以使用自动路由。指定的执行器不可用、未启用，或多个 `agent:` 标签冲突时进入 `needs_input`，不会静默改派。CodeBuddy 的独立 CLI、模型与单次强度配置见 [接入说明](docs/codebuddy-agent.md)。

优先级决定排队顺序，难度决定推理强度，两者互不替代。相同优先级按截止时间和入队时间排序。默认模型 `gpt-6.1-sol`；简单/中等/复杂使用 `low` / `medium` / `high`，复核使用 `medium`。可在 `model`、`efforts`、`review_effort` 修改；`models` 可按推理强度覆盖模型，如 `{"high":"gpt-6.1-sol"}`。

并发 `max_concurrent` 范围 1–4，默认 1；同步间隔 `poll_seconds` 至少 10 秒，默认 20；每次执行器调用超时 `task_timeout_seconds` 至少 30 秒，默认 1800。`daily_max_runs` 默认 10，`max_attempts` 默认 2，两者至少 1。CLI `retry` 保留尝试次数；前端重新分配是一次明确的新委派，重置本次尝试预算，历史运行及每日次数仍保留。

手机在滴答勾选完成后，任务会从未完成列表消失。调度器会对本地缺失项补查任务详情，只有明确确认 `status=2` 才移到“已完成”，标为 **滴答已完成**；读取失败或列表缺失不会直接视为完成。外部完成会停止本次委派并保留原产物、运行和复审记录；重新打开后需明确重新派工，旧 worker 或批准不能复活。原本已获确认的派工成功历史继续保留。

界面的两秒刷新是派工台状态轮询，源任务默认每二十秒同步；顶部显示本轮耗时和未确认提示，“同步滴答”可立即请求核对。缺失补查每轮最多二十项、六秒额外预算，未知项退避重试且轮转避免饥饿；这不是整轮同步的硬期限，清单读取和网络耗时仍需另外计算。详见[源状态同步修复](docs/source-sync-2026-11-07.md)。

Antigravity 的 `--print-timeout` 使用上述执行时限，外层进程另留 60 秒退出宽限，以收集厂商终结事件；达到外层期限仍会终止该次进程树。超时、非零退出或无有效终结结果时，运行目录保存 `execution-diagnostics.json`，仅含期限、会话编号及事件统计，不复制原始错误消息。厂商诊断日志位于工作区 `.runtime/antigravity.log`，属于私有运行数据，不作为交付物或前端预览内容。只有文件存在、缺少完整结束证据的运行仍为失败。

Antigravity 返回 `SUCCESS` 时优先校验官方 `structured_output`；缺失或为 null 才兼容 `response`。说明文字混在 response 中不会覆盖合法结构化结果。普通成功解析仍拒绝厂商 `ERROR`；另有受限的 EOF 产物恢复分支，只在以下证据全部满足时返回内部执行状态 **`recovered`**，并保留 **`vendor_status: ERROR`**：

- 实际进程正常退出且退出码为 0、stderr 为空；任务是文档，模型为 `gemini-3.8-flash-medium`，并且启用人工复审。
- NDJSON 事件流完整、会话一致、只有一个首位用户输入回合；只有一次无附加错误信息的错误步骤，终结错误严格匹配已知 EOF 白名单。
- 只出现允许的文件工具及 `finish`；全部步骤结束、没有工具失败或拒绝，文件路径限于本次工作区。最后 `finish` 已完成，其参数与完整结构化报告一致；若有 `response`，也必须是相同的严格 JSON。
- 交付文件真实、非空、路径安全且不经过链接或重解析点，由调度器读取实际字节并记录 SHA-256；独立验收必须覆盖同一组未变化的文件。

`recovered` 只允许进入 Codex 独立验收。验收通过后仍停在 `awaiting_review`，显示 EOF/ERROR 警告；只有本人批准，才允许按回写配置完成滴答。批准摘要同时绑定产物哈希与恢复证据，后来将 `require_human_review` 改为 false 也不能绕过此门槛。历史文件导入不能把恢复结果追认为已完成或已获人工批准。其他错误、超时、非零退出、重复 EOF 或证据不完整仍失败。这项改动不表示 Google 生成流的网络 EOF 已修复。

可选 `antigravity_proxy_url` 默认空；只接受不含凭据、路径或参数的本机 HTTP 代理，如 `http://127.0.0.1:12334`。它仅为该执行器注入代理环境变量，不改全局代理或其他智能体，也不关闭 TLS 校验；需要该本机代理本身运行。指定厂商模型可在 `executor_commands.antigravity` 的 argv 后添加 `--model` 和通过 `agy models` 核实的模型名称。

默认 `require_human_review: true`、`write_comments: true` 和 `auto_complete: true`。独立复核通过后先等你复审，批准后才在滴答写入总结和本机交付目录并标记完成。只需同步或暂时不希望自动标记完成时，可设置 `auto_complete: false`；本地 `done` 表示本地流程结束，不代表已在滴答勾选完成。发布、部署、发送消息、购买、删除外部数据不属于默认自动执行范围；需要这些动作时先生成可审阅产物并等待输入。

## 多智能体调度配置

默认的工作分配策略如下。这是可调整的任务分工，不是执行器能力排行；优先级只决定排队先后。

| 任务类型 | 简单 | 中等 | 复杂 |
| --- | --- | --- | --- |
| 文档 | DSH | ZCode | Codex |
| 代码 | 配有验证命令时 Qoder，否则 Codex | 配有验证命令时 Qoder，否则 Codex | Codex |

中等文档的候选顺序为 ZCode、Qoder、Codex；高难度任务优先 Codex，Antigravity 为后备，也可通过 `agent:antigravity` 指定。首选未启用、未发现或缺少必要能力配置时，只在启动前回退，并记录原因。示例配置仅启用 Codex。已经启动的任务失败不会自动换一个执行器重跑，显式 `agent:` 标签也不会静默回退。此策略是可调整的初始配置，不是模型能力排名。

以下是配置示例，启用前应先完成对应工具的安装、登录和本机验证：

```json
{
  "enabled_executors": ["codex", "dsh", "zcode", "qoder", "antigravity"],
  "executor_commands": {
    "qoder": ["D:/tools/qoder/qodercli.exe"]
  },
  "executor_routes": {
    "code:easy": ["qoder", "codex"],
    "document:medium": ["zcode", "codex"]
  }
}
```

`executor_commands` 默认 `{}`，按名称覆盖自动探测到的启动参数，值必须是非空 argv 字符串数组；路径中有空格时仍是数组中的一个元素，不填写整条 shell 命令。`executor_routes` 默认 `{}`，可覆盖 `easy`、`medium`、`hard`，以及 `code:easy` / `code:medium` / `code:hard`、`document:easy` / `document:medium` / `document:hard`；每项必须是非空执行器名称列表。任务类型与难度都匹配的自定义路由优先，其次是仅按难度的自定义路由，最后采用默认策略。自定义路由如需 Codex 后备，应把 `codex` 放入列表。

DSH、ZCode 当前只用于工作目录内的纯文件任务，不负责运行终端命令或代码验证；产物仍需通过 Codex 独立复核。执行器状态、选择原因和各厂商的会话 ID 会显示在派工台与项目索引中，复核的 Codex 线程单独记录。

Qoder 的 Git Bash 在当前 Windows 受限令牌环境中无法创建 MSYS signal pipe，因此保留文件编辑工具，不让它尝试终端指令。代码任务只有配置静态验证命令后才自动派给 Qoder；调度器在同一工作目录直接运行这些命令并写真实报告，然后交给 Codex 独立复核。命令来自本地配置，不从任务正文或模型输出执行。例如：

```json
{
  "repositories": {"myapp": "D:/projects/myapp"},
  "repository_checks": {
    "myapp": {
      "commands": [["python", "-B", "-m", "unittest", "discover", "-s", "tests", "-v"]],
      "report": "REPORT.md"
    }
  }
}
```

报告文件会由调度器用实际 stdout/stderr 覆盖，配置时应选择专用报告文件；验证失败不会生成成功报告或完成滴答任务。未配置验证命令的普通代码任务自动回退 Codex。Antigravity 文档任务只使用文件工具，避免无人响应的终端权限问询。

外部四家 CLI 均包在 Codex Windows 工作目录沙箱内，并额外允许各自必要的运行状态目录写入（DSH headless profile、ZCode CLI 数据库、Qoder cache/log/tmp 等、Antigravity CLI 状态）。该沙箱提供写入边界，不能描述成凭据读取隔离或网络目的地白名单。登录由厂商官方 CLI 管理，本项目不复制密钥。

## 工作目录、恢复与状态

派工时可以选择 **默认工作区 / 新建工作区 / 已有工作区**。默认保留每次运行独立建目录的规则；新建可填写名称，生成按智能体分类的持久项目目录；已有工作区优先列出所选智能体的目录，确认派工后直接在原目录修改。切换智能体、模型或刷新目录不会清空尚未提交的选择。

目录列表优先按配置的桌面汇总目录分组，如 `Codex / 文档 / 示例项目`，并合并智能体最近保存的项目路径、派工台历史和配置仓库。只枚举汇总根及显式分类的直接项目，不扫描整块磁盘或深入项目实现目录。当前支持 Codex、DSH、ZCode、Qoder 的本地会话索引；Antigravity 可发现 `workspace_gravity` 的桌面项目，其原生会话索引尚未接入。`workspace_discovery.native_agents: false` 仅关闭最近会话发现；桌面根由 `collections` 配置。目录映射和边界见 [桌面工作区归类](docs/desktop-workspace-collections.md)。路径不存在、涉及认证/运行数据、是链接/重解析点或被派工任务占用时禁止选择。

新建和原地续做都会另存本次验收快照，复审、预览、下载读取快照。复用旧版派工台目录前，先保存原有产物并绑定原运行记录，避免后续修改影响历史审核。原地执行失败不会自动撤销已修改文件；派工台的目录锁覆盖本系统的同时执行任务，不能阻止你在其他编辑器或原生智能体里同时修改同一目录。

快照保留普通项目文件，跳过已知凭据、运行缓存、依赖目录和版本控制内部目录；这不是任意文件内容的秘密扫描。复制前后核对内容，遇到链接、源文件持续变化或超过资源限额会停止验收，保留失败记录。上限为 10,000 个文件、总计 512 MiB、单文件 64 MiB。详细设计与验证见 [工作区选择](docs/workspace-selection-2026-11-07.md)。

```text
data/
  tasks.db                       SQLite 队列、尝试次数与事件
  status.json                    daemon 最近快照
  daemon.stdout.log
  daemon.stderr.log
workspaces/
  <agent>/                       codex / dsh / zcode / qoder / antigravity
    <任务摘要>--<任务短标识>/
      runs/<UTC-timestamp>/
        task.json
        routing.json             本次执行器与路由原因
        workspace-layout.json    目录版本、摘要和执行器归属
        execution.events.jsonl
        execution-resume.txt     执行会话的恢复说明（可用时）
        verification.events.jsonl
        workspace/               本次独立产物与代码仓库
```

新目录摘要优先取任务标题，缺少标题时取内容首句；清理 Windows 非法字符和保留设备名，最多保留 36 个字符。任务键的稳定短哈希区分同名任务；同一任务每次执行都有独立运行目录。自动派工先选执行器，再创建其分类目录。已有 `<project>/<task-id>-<version>` 目录保留，前端与历史扫描仍读取原路径；改派或更名后的新运行进入新的对应目录。根目录使用当前项目的 `workspaces`。

任务状态包括 `queued`、`running`、`verifying`、`awaiting_review`、`approved`、`writeback_pending`、`done`、`revision_requested`、`needs_input`、`needs_review`、`failed`、`ignored`、`cancelled`。`recovered` 是保存在执行报告中的恢复状态，不是任务完成状态；其厂商原始 ERROR 与警告随独立验收结果保留。历史自动完成任务保留其原状态并标记历史验收，不追认成人工批准；恢复运行的历史文件导入为 `needs_review`。异常中断的运行在恢复时进入 `needs_review`，不会自动重复执行。回写失败只重试回写，`run-once` 即使达到每日新任务上限也会先处理全部待回写结果。源任务变更、移除授权或已完成时，旧结果不会直接完成新版本。

```powershell
python -m dispatcher retry "projectId:taskId"
python -m dispatcher resume "projectId:taskId"
python -m dispatcher agents
```

`retry` 仅把失败、需输入、需复核任务重新入队，使用前先停止 daemon 并检查已有产物，避免重复动作。唯一的短 taskId 也可使用。`resume` 必须由用户在可见终端运行：Codex 任务调用 `codex resume <thread_id>` 并保留工作目录，进入 TUI 后输入 `/app`，再按 Codex 的界面交接到 App；其他执行器只显示该次运行的 `execution-resume.txt`，由用户按厂商入口恢复，缺失说明时明确报错。DSH 等厂商的 session ID 不会传给 Codex，复核线程也不会替代执行会话。**后台 CLI 创建的会话并不会由本项目自动添加到真实 Codex App 的侧边栏**，也没有创建模拟 App 聊天窗口。

所有命令可使用 `--root <项目根目录>`，省略时默认当前包所在项目。更换目录时一起保留 `data` 和 `workspaces`；状态文件包含绝对路径，已有产物移动后需相应核对。

## 本地验证

```powershell
python -B -m unittest discover -s tests -v
$tests = (Get-ChildItem tests -Filter *.test.cjs).FullName
node --test $tests
```

在 Windows 上，请从退出虚拟环境的终端运行 Python 测试，或将测试命令中的 `python` 换成 CPython 安装目录里的 `python.exe`。豆包进程测试会验证专用控制台的 Ctrl+C 行为，虚拟环境的重定向启动器会增加控制台进程。

测试采用临时目录和假适配器覆盖排队、恢复、锁、配置、回写与只读页面安全边界，不调用真实滴答任务。这些测试通过并不代表 OAuth、真实同步、真实回写或候选执行器已验证。

也可运行 `python -m tests.serve_dashboard_fixture --port 8877`，使用临时模拟数据预览看板。
