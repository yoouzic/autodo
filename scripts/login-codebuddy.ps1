$ErrorActionPreference = 'Stop'
$NodeCommand = (Get-Command node.exe -ErrorAction Stop).Source
$CliEntry = Join-Path $env:APPDATA 'npm\node_modules\@tencent-ai\codebuddy-code\bin\codebuddy'
if (-not (Test-Path -LiteralPath $CliEntry -PathType Leaf)) { throw 'Independent CodeBuddy CLI is not installed.' }
$LoginDirectory = Join-Path $env:TEMP ('todo-codebuddy-login-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $LoginDirectory | Out-Null
Set-Location -LiteralPath $LoginDirectory
$env:CODEBUDDY_CONFIG_DIR = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.codebuddy'
Write-Host 'CodeBuddy CLI official login. Use /login if a login menu does not appear.'
Write-Host 'Choose the China mainland service for your current CN account.'
Write-Host 'After signing in, tell the dispatcher setup. Do not send passwords, codes or tokens in chat.'
& $NodeCommand $CliEntry --setting-sources none --settings '{"disableAllHooks":true,"autoUpdates":false}'
