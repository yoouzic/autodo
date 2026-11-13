[CmdletBinding()]
param([ValidateRange(1, 900)][int]$DrainTimeoutSeconds = 120)
$ErrorActionPreference = 'Stop'
$taskName = 'AuToDo'
$taskPath = '\'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$ownerSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$marker = "TodoDispatcher.Autostart/v1|$projectRoot|$ownerSid"
$task = Get-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction SilentlyContinue
if (-not $task) { Write-Host 'This project has no registered startup task.'; return }
if (-not ([string]$task.Description).StartsWith($marker + '|', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The named task does not belong to this project and owner; it will not be changed.'
}
Disable-ScheduledTask -TaskName $taskName -TaskPath $taskPath | Out-Null
if ([string]$task.State -eq 'Running') {
    $stopFile = Join-Path $projectRoot 'data\stop.request'
    [IO.File]::WriteAllText($stopFile, '', [Text.UTF8Encoding]::new($false))
    $deadline = [DateTime]::UtcNow.AddSeconds($DrainTimeoutSeconds)
    do {
        Start-Sleep -Milliseconds 500
        $task = Get-ScheduledTask -TaskName $taskName -TaskPath $taskPath
    } while ([string]$task.State -eq 'Running' -and [DateTime]::UtcNow -lt $deadline)
    if ([string]$task.State -eq 'Running') {
        throw 'The task is disabled but still draining current work. No process was killed. Run this script again after it stops.'
    }
}
Unregister-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Confirm:$false
Write-Host 'Removed this project startup task. Runtime files, workspaces and credentials were retained.'
