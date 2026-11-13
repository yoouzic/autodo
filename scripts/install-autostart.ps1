[CmdletBinding()]
param(
    [ValidateSet('Logon', 'Boot')][string]$Mode = 'Logon',
    [string]$PythonExecutable,
    [string]$NodeExecutable,
    [switch]$ValidateOnly
)
$ErrorActionPreference = 'Stop'
if ($Mode -eq 'Boot') {
    throw 'Boot installation is not supported for this administrator-owned workspace: batch logon may run elevated despite Limited. Use -Mode Logon. Before-sign-in startup requires a separately provisioned standard account.'
}
$taskName = 'AuToDo'
$taskPath = '\'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$ownerSid = $identity.User.Value
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$isElevated = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($ownerSid -eq 'S-1-5-18') { throw 'Do not install this task as SYSTEM. Use the project owner account.' }

function Assert-NoBroadWrite([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $dangerous = [long][Security.AccessControl.FileSystemRights]::Write -bor [long][Security.AccessControl.FileSystemRights]::Delete -bor [long][Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor [long][Security.AccessControl.FileSystemRights]::ChangePermissions -bor [long][Security.AccessControl.FileSystemRights]::TakeOwnership
    foreach ($rule in (Get-Acl -LiteralPath $Path).GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        $sid = $rule.IdentityReference.Value
        if ($rule.AccessControlType -eq 'Allow' -and $sid -in @('S-1-1-0', 'S-1-5-11', 'S-1-5-32-545') -and (([long]$rule.FileSystemRights -band $dangerous) -ne 0)) {
            throw "Unsafe shared write permission on $Path. Restrict this project to its owner before installing autostart."
        }
    }
}

$rootOwnerSid = (Get-Acl -LiteralPath $projectRoot).GetOwner([Security.Principal.SecurityIdentifier]).Value
if ($rootOwnerSid -ne $ownerSid) { throw 'Run this installer as the project owner, not another administrator account.' }
$dataRoot = Join-Path $projectRoot 'data'
foreach ($path in @($projectRoot, $PSScriptRoot, (Join-Path $projectRoot 'dispatcher'), $dataRoot, (Join-Path $projectRoot 'config.local.json'), (Join-Path $dataRoot 'startup-config.json'))) {
    Assert-NoBroadWrite $path
}
if (-not $PythonExecutable) { $PythonExecutable = (Get-Command python.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source }
if (-not $NodeExecutable) { $NodeExecutable = (Get-Command node.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source }
$PythonExecutable = (Resolve-Path -LiteralPath $PythonExecutable).Path
$NodeExecutable = (Resolve-Path -LiteralPath $NodeExecutable).Path
if ((Split-Path -Leaf $PythonExecutable) -ne 'python.exe' -or (Split-Path -Leaf $NodeExecutable) -ne 'node.exe') { throw 'Select the actual python.exe and node.exe files.' }
Assert-NoBroadWrite $PythonExecutable
Assert-NoBroadWrite $NodeExecutable
$git = Get-Command git.exe -ErrorAction SilentlyContinue
$gitExecutable = if ($git) { $git.Source } else { $null }
if ($gitExecutable) { Assert-NoBroadWrite $gitExecutable }
$guiExecutable = Join-Path (Split-Path -Parent $PythonExecutable) 'pythonw.exe'
$runner = Join-Path $PSScriptRoot 'autostart-gui.pyw'
if (-not (Test-Path -LiteralPath $runner) -or -not (Test-Path -LiteralPath $guiExecutable -PathType Leaf)) { throw 'Missing the windowless startup entry or pythonw.exe.' }
Assert-NoBroadWrite $guiExecutable
Assert-NoBroadWrite $runner
$marker = "TodoDispatcher.Autostart/v1|$projectRoot|$ownerSid"
$existing = Get-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction SilentlyContinue
if ($existing -and -not ([string]$existing.Description).StartsWith($marker + '|', [StringComparison]::OrdinalIgnoreCase)) {
    throw "A different task already owns $taskPath$taskName; it will not be overwritten."
}
if ($ValidateOnly) {
    [PSCustomObject]@{Validated=$true;Mode=$Mode;OwnerSid=$ownerSid;Python=$PythonExecutable;Node=$NodeExecutable;Git=$gitExecutable;TaskExists=[bool]$existing} | ConvertTo-Json
    return
}

$backupRoot = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'TodoDispatcher\backups'
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
Assert-NoBroadWrite $backupRoot
$stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
if ($existing) {
    $xml = Export-ScheduledTask -TaskName $taskName -TaskPath $taskPath
    [IO.File]::WriteAllText((Join-Path $backupRoot "$taskName-$stamp.xml"), $xml, [Text.UTF8Encoding]::new($false))
}
New-Item -ItemType Directory -Path $dataRoot -Force | Out-Null
$configPath = Join-Path $dataRoot 'startup-config.json'
if (Test-Path -LiteralPath $configPath) {
    Copy-Item -LiteralPath $configPath -Destination (Join-Path $backupRoot "startup-config-$stamp.json")
}
$config = [ordered]@{
    Version = 1; TaskName = $taskName; OwnerSid = $ownerSid; UserName = $identity.Name
    UserProfile = [Environment]::GetFolderPath('UserProfile'); ProjectRoot = $projectRoot
    PythonExecutable = $PythonExecutable; NodeExecutable = $NodeExecutable; GitExecutable = $gitExecutable
    Mode = $Mode; InstalledAt = [DateTime]::UtcNow.ToString('o')
}
$action = New-ScheduledTaskAction -Execute $guiExecutable -Argument ('-I -B "' + $runner + '"') -WorkingDirectory $projectRoot
$logon = New-ScheduledTaskTrigger -AtLogOn -User $identity.Name
$logon.Delay = 'PT20S'
$triggers = @($logon)
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $ownerSid -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -DisallowHardTerminate -Hidden
$definition = New-ScheduledTask -Action $action -Trigger $triggers -Principal $taskPrincipal -Settings $settings -Description ($marker + '|Mode=' + $Mode)
Register-ScheduledTask -TaskName $taskName -TaskPath $taskPath -InputObject $definition -Force | Out-Null
[IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json -Depth 4), [Text.UTF8Encoding]::new($false))
Assert-NoBroadWrite $configPath
$registered = Get-ScheduledTask -TaskName $taskName -TaskPath $taskPath
if ([string]$registered.Principal.RunLevel -ne 'Limited') {
    Disable-ScheduledTask -TaskName $taskName -TaskPath $taskPath | Out-Null
    throw 'Unexpected privilege level; the task was disabled for safety.'
}
Write-Host "Installed $taskPath$taskName in $Mode mode with Limited privileges."
Write-Host "Python: $PythonExecutable"
Write-Host 'The installer did not launch the dispatcher. Start the task explicitly to verify it, then test a reboot when convenient.'
if ($Mode -eq 'Logon') { Write-Host 'Logon mode starts only after you sign into Windows; it is not unattended cold-boot startup.' }
