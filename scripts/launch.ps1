[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [ValidateRange(1, 120)][int]$WaitSeconds = 45
)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$dashboardUrl = 'http://127.0.0.1:8765'

function Test-DispatcherRunning {
    # Probe the existing daemon lock without opening the task database or writing files.
    $lockPath = Join-Path $projectRoot 'data\daemon.lock'
    if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) { return $false }
    $stream = [IO.File]::Open($lockPath, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::ReadWrite)
    try {
        try { $stream.Lock(0, 1) }
        catch [IO.IOException] {
            if (($_.Exception.HResult -band 0xffff) -eq 33) { return $true }
            throw
        }
        $stream.Unlock(0, 1)
        return $false
    } finally { $stream.Dispose() }
}

function Test-DispatcherHealth {
    $response = $null
    $reader = $null
    try {
        $request = [Net.HttpWebRequest]::Create($dashboardUrl + '/api/status')
        $request.Proxy = $null
        $request.AllowAutoRedirect = $false
        $request.Timeout = 2000
        $request.ReadWriteTimeout = 2000
        $response = $request.GetResponse()
        if ([int]$response.StatusCode -ne 200) { return $false }
        $reader = [IO.StreamReader]::new($response.GetResponseStream())
        $status = $reader.ReadToEnd() | ConvertFrom-Json
        return ($status.running -is [bool] -and $status.running)
    } catch { return $false }
    finally {
        if ($reader) { $reader.Dispose() }
        if ($response) { $response.Dispose() }
    }
}

function Get-InstalledDispatcherTask {
    # A failed task query must not silently become an uninstalled/manual fallback.
    $task = Get-ScheduledTask -TaskPath '\' -ErrorAction Stop |
        Where-Object { $_.TaskName -eq 'AuToDo' }
    if (-not $task) { return $null }
    $ownerSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $marker = "TodoDispatcher.Autostart/v1|$projectRoot|$ownerSid|"
    if (-not ([string]$task.Description).StartsWith($marker, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'The installed dispatcher task belongs to another project or user; it was not started.'
    }
    $taskUser = [string]$task.Principal.UserId
    if (-not $taskUser.StartsWith('S-1-')) {
        $taskUser = ([Security.Principal.NTAccount]::new($taskUser)).Translate([Security.Principal.SecurityIdentifier]).Value
    }
    if ($taskUser -ne $ownerSid -or [string]$task.Principal.LogonType -ne 'Interactive' -or [string]$task.Principal.RunLevel -ne 'Limited') {
        throw 'The installed task must belong to this user and use Interactive + Limited privileges.'
    }
    $actions = @($task.Actions)
    $expectedShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $expectedRunner = Join-Path $projectRoot 'scripts\autostart-run.ps1'
    $expectedArguments = '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy RemoteSigned -File "' + $expectedRunner + '"'
    $legacyAction = $actions.Count -eq 1 -and $actions[0].Execute -eq $expectedShell -and $actions[0].Arguments -eq $expectedArguments
    $startupConfig = Get-Content -LiteralPath (Join-Path $projectRoot 'data\startup-config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $expectedGui = Join-Path (Split-Path -Parent $startupConfig.PythonExecutable) 'pythonw.exe'
    $guiArguments = '-I -B "' + (Join-Path $projectRoot 'scripts\autostart-gui.pyw') + '"'
    $guiAction = $actions.Count -eq 1 -and $actions[0].Execute -eq $expectedGui -and $actions[0].Arguments -eq $guiArguments
    if ((-not $legacyAction -and -not $guiAction) -or $actions[0].WorkingDirectory -ne $projectRoot) {
        throw 'The installed task action differs from this project installer; inspect it before starting.'
    }
    if (-not $task.Settings.Enabled -or [string]$task.State -eq 'Disabled') {
        throw 'The installed dispatcher task is disabled. Enable it explicitly before using this launcher.'
    }
    return $task
}

try {
    . (Join-Path $PSScriptRoot 'runtime.ps1')
    Assert-DispatcherUserContext
    if (Test-DispatcherRunning) {
        Write-Host 'Dispatcher is already running; keeping the existing process.'
    } else {
        $task = Get-InstalledDispatcherTask
        if ($task) {
            if ([string]$task.State -in @('Running', 'Queued')) {
                Write-Host 'The existing startup task is active; waiting for the control center.'
            } else {
                Start-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -ErrorAction Stop
                Write-Host 'Started the installed Interactive + Limited dispatcher task.'
            }
        } else {
            Write-Host 'No installed dispatcher task found; using scripts\start.ps1.'
            & (Join-Path $PSScriptRoot 'start.ps1')
        }
    }
    $deadline = [DateTime]::UtcNow.AddSeconds($WaitSeconds)
    do {
        if ((Test-DispatcherRunning) -and (Test-DispatcherHealth)) {
            Write-Host "Control center is ready: $dashboardUrl"
            if (-not $NoBrowser) {
                # Opening the user's browser is intentionally visible.
                Start-Process -FilePath $dashboardUrl -ErrorAction Stop
            }
            exit 0
        }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "The control center did not become healthy within $WaitSeconds seconds. The existing process was not stopped or restarted."
} catch {
    Write-Host ("Launch failed: " + $_.Exception.Message) -ForegroundColor Red
    Write-Host "Project: $projectRoot"
    Write-Host 'Check data\startup-status.json, data\startup-launcher-error.json and data\daemon.stderr.log.'
    Write-Host 'Use a normal Windows account window. Keep the existing permission and execution-policy checks enabled.'
    exit 1
}
