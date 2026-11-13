$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $PSScriptRoot 'runtime.ps1')
Assert-DispatcherUserContext
$pythonExecutable = Resolve-DispatcherPython -ProjectRoot $projectRoot
Push-Location -LiteralPath $projectRoot
try {
    $statusText = & $pythonExecutable -m dispatcher status
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read dispatcher status; fix the reported configuration error.' }
    $status = ($statusText -join "`n") | ConvertFrom-Json
    if ($status.running) {
        Write-Host 'Dispatcher already running; no second instance was started.'
        return
    }
    $dataRoot = Join-Path $projectRoot 'data'
    New-Item -ItemType Directory -Path $dataRoot -Force | Out-Null
    $process = Start-Process -FilePath $pythonExecutable -ArgumentList @('-m', 'dispatcher', 'daemon') `
        -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $dataRoot 'daemon.stdout.log') `
        -RedirectStandardError (Join-Path $dataRoot 'daemon.stderr.log')
    Start-Sleep -Milliseconds 1000
    $process.Refresh()
    if ($process.HasExited) {
        throw "Dispatcher exited during startup. Inspect data\daemon.stderr.log."
    }
    Write-Host "Dispatcher process started (PID $($process.Id))."
    Write-Host 'Control center: http://127.0.0.1:8765'
    Write-Host 'Logs: data\daemon.stdout.log and data\daemon.stderr.log'
} finally {
    Pop-Location
}
