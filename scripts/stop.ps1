$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $PSScriptRoot 'runtime.ps1')
$pythonExecutable = Resolve-DispatcherPython -ProjectRoot $projectRoot
Push-Location -LiteralPath $projectRoot
try {
    & $pythonExecutable -m dispatcher stop
    if ($LASTEXITCODE -ne 0) { throw 'Cannot write graceful-stop request.' }
} finally {
    Pop-Location
}
