$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$pythonCommand = Get-Command python -ErrorAction Stop
$null = Get-Command node -ErrorAction Stop
$npmCommand = Get-Command npm.cmd -ErrorAction Stop
& $pythonCommand.Source -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 or newer is required.' }
$toolRoot = Join-Path $projectRoot '.tools'
New-Item -ItemType Directory -Path $toolRoot -Force | Out-Null
& $npmCommand.Source install --prefix $toolRoot --save-exact '@suibiji/dida-cli@0.1.15'
if ($LASTEXITCODE -ne 0) { throw 'Official DIDA CLI installation failed.' }
$localConfig = Join-Path $projectRoot 'config.local.json'
if (-not (Test-Path -LiteralPath $localConfig)) {
    Set-Content -LiteralPath $localConfig -Value '{}' -Encoding UTF8
}
Write-Host 'Setup complete. No login credentials or API keys were read.'
Write-Host 'Next: python -m dispatcher auth'
Write-Host 'Then: python -m dispatcher doctor'
