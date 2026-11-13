# Windows 登录自启

当前支持的安装方式是 **登录后自启**：以项目所属用户登记 `\AuToDo`，使用 `Interactive` 登录类型、`Limited` 权限和 `AtLogon` 触发器，登录后延迟 20 秒运行。

电脑重启后需要先登录 Windows。脚本不设置自动登录、不关闭 UAC、不索取 Windows 密码；停留在登录界面、关机、休眠或断网期间，不能保证手机访问。锁屏与注销不同：登录后仅锁屏可保留当前会话，注销则不属于本方案的持续运行保证。当前安装器拒绝 `-Mode Boot`；未登录运行需要另行配置专用普通账户，并验证执行器认证和文件权限。

## 安装前只读预检

在项目目录以项目所属用户运行：

```powershell
.\scripts\install-autostart.ps1 -Mode Logon -ValidateOnly
```

预检读取用户 SID、项目所有权、关键路径 ACL、固定 Python/Node/Git 路径和已有计划任务的归属标记；不登记任务、不写启动配置、不启动服务。`Validated: true` 只表示这些条件通过，不代表任务已安装或实际运行成功。

运行时路径由 `data/startup-config.json` 固定，不依赖任意 PATH 中的同名 Python。可用 `-PythonExecutable`、`-NodeExecutable` 指定实际 exe 文件。关键路径若允许 Everyone、Authenticated Users 或 Users 写入，检查会拒绝继续；应先核对并收紧具体路径权限，不要关闭检查。

## 显式安装

```powershell
.\scripts\install-autostart.ps1 -Mode Logon
```

安装器只更新名称、项目路径和所有者标记匹配的任务；同名但属于其他项目的任务不会被覆盖。原任务 XML 和启动配置备份在当前用户的 `%LOCALAPPDATA%\TodoDispatcher\backups`。Logon 安装流程不传入账户密码。

如果已有任务由管理员登记，普通窗口可能因权限不足无法更新。此时需要用户在 Windows 中确认，以**同一个项目所属账户**打开管理员 PowerShell 后执行安装命令；不要改用其他管理员账户。管理员权限只用于登记任务，运行中的派工器仍必须是普通权限。

出现 `Installed` 仅表示登记完成。安装器不会自动启动派工器，也不会执行电脑重启。

## 实际验证

先确认没有正在执行且不宜中断的任务。需要验证计划任务完整启动链路时，应让现有手动 daemon 优雅退出，再启动计划任务；已有 daemon 会被监督进程观察并复用，不能据此证明新 daemon 是由计划任务启动的。

```powershell
.\scripts\stop.ps1
.\scripts\status.ps1
# 等待 status 中 running 为 false；任务执行结束或超时后才退出。
Start-ScheduledTask -TaskName 'AuToDo'
Get-ScheduledTask -TaskName 'AuToDo' |
    Select-Object TaskName, State, Principal, Triggers
.\scripts\status.ps1
Get-Content -LiteralPath .\data\startup-status.json -Encoding UTF8
```

继续核对本机派工台、手机 HTTPS 登录入口及 SSH 隧道状态。`RunLevel=Limited` 是任务配置，不能单独证明实际进程权限：PowerShell 运行器和 Python 监督进程都会检查实际 Windows 令牌，遇到 SYSTEM 或提升后的管理员权限会拒绝启动。不要删除这两层检查来处理启动失败。

只有实际完成重启、登录并从手机访问，才能记录“重启后登录自启通过”；没有进行该步骤时，应保留为未验证。验证服务启动不需要派发真实任务或批准滴答任务。

## 运行、重连和停止

计划任务通过 `scripts/autostart-gui.pyw` 启动 `dispatcher.startup` 监督进程。监督进程使用固定解释器启动 daemon；daemon 自身持有单实例锁，并管理 SSH 反向隧道。重复触发不会创建第二个监督进程或 daemon。

daemon 异常退出时，监督进程以 5 至 60 秒退避重试；SSH 进程退出时由隧道管理器以 5 至 30 秒退避重连。重试不能修复失效的登录凭据、服务器故障或长期断网，仍需查看具体错误。

```powershell
.\scripts\stop.ps1
.\scripts\status.ps1
```

停止只写 `data/stop.request`。监督进程等待当前任务排空，让 daemon 关闭其拥有的隧道后退出，不强杀 agent。明确停止后不会立即自动拉起；需要再次运行时可执行 `Start-ScheduledTask -TaskName 'AuToDo'`。下次登录仍会触发已启用的计划任务；需要永久取消自启应卸载任务。

双击项目根目录的 `AuToDo.cmd` 可启动服务并打开看板。`scripts/start.ps1` 可从普通用户 PowerShell 直接启动 daemon。

## 日志与卸载

主要诊断文件均在项目私有的 `data` 目录：

| 文件 | 用途 |
| --- | --- |
| `startup-config.json` | 固定运行时路径、所属用户和安装模式，不包含账户密码 |
| `startup-status.json` | 监督进程状态、子进程 PID、重试时间和更新时间 |
| `startup.log` | 监督启动、观察已有服务、异常退出与重试记录；上限约 1 MiB，保留一份轮换 |
| `daemon.stdout.log` / `daemon.stderr.log` | 监督进程捕获的 daemon 输出；各上限约 5 MiB，保留一份轮换 |
| `relay-tunnel.log` | SSH 隧道诊断和重连记录；上限约 256 KiB，保留一份轮换 |
| `startup-launcher-error.json` | PowerShell 启动失败记录；读取时注意时间，旧错误不等于当前状态 |

```powershell
.\scripts\uninstall-autostart.ps1
```

卸载器检查任务归属，先禁用任务，再等待已运行的任务优雅退出，最后注销任务。默认等待 120 秒，可通过 `-DrainTimeoutSeconds` 调整为 1 至 900 秒；超时只保留禁用状态并报错，不强杀进程，待退出后可再次卸载。原任务若由管理员登记，更新或卸载可能需要同账户管理员窗口。工作区、数据库、运行时文件及登录凭据会保留。
