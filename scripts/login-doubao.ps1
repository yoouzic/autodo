$ErrorActionPreference = 'Stop'
# The installed integration bundle's QR flow asked this user for an enterprise
# account. Keep the consumer-account path closed until compatibility is verified.
throw 'Login paused: the current QR flow requests an enterprise account. Consumer-account compatibility is unverified. Do not repeat QR scans.'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$DoubaoCli = Join-Path $ProjectRoot '.tools\providers\doubao\2.31.3\doubao-cli-2.31.3-win-x64\doubao.exe'
if (-not (Test-Path -LiteralPath $DoubaoCli -PathType Leaf)) {
    throw 'Verified Doubao CLI 2.31.3 is missing. Do not use the desktop Doubao.exe in its place.'
}
Write-Host 'Doubao official CLI login. Scan the code in this window with the Doubao app.'
Write-Host 'Keep passwords, QR codes and tokens out of chat. Existing login is preserved.'
& $DoubaoCli login
if ($LASTEXITCODE -ne 0) { throw "Doubao login did not finish (exit $LASTEXITCODE)." }
Write-Host 'Login command finished. Return to the dispatcher setup for the isolated task check.'
