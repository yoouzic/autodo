[CmdletBinding()]
param([switch]$ValidateOnly)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$configPath = Join-Path $projectRoot 'data\startup-config.json'

function Assert-NoBroadWrite([string]$Path) {
    $dangerous = [long][Security.AccessControl.FileSystemRights]::Write -bor [long][Security.AccessControl.FileSystemRights]::Delete -bor [long][Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor [long][Security.AccessControl.FileSystemRights]::ChangePermissions -bor [long][Security.AccessControl.FileSystemRights]::TakeOwnership
    foreach ($rule in (Get-Acl -LiteralPath $Path).GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        $sid = $rule.IdentityReference.Value
        if ($rule.AccessControlType -eq 'Allow' -and $sid -in @('S-1-1-0', 'S-1-5-11', 'S-1-5-32-545') -and (([long]$rule.FileSystemRights -band $dangerous) -ne 0)) {
            throw 'Startup stopped: a runtime path permits modification by other local users.'
        }
    }
}

try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if ($identity.User.Value -eq 'S-1-5-18' -or $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'The dispatcher must run as its owner with Limited privileges, never SYSTEM or an elevated administrator.'
    }
    foreach ($path in @($projectRoot, $PSScriptRoot, (Join-Path $projectRoot 'dispatcher'), (Join-Path $projectRoot 'data'), $configPath, (Join-Path $projectRoot 'config.local.json'))) {
        Assert-NoBroadWrite $path
    }
    $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($config.Version -ne 1 -or $config.OwnerSid -ne $identity.User.Value -or -not ([string]$config.ProjectRoot).Equals($projectRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Startup configuration does not match the current owner and project.'
    }
    $rootOwnerSid = (Get-Acl -LiteralPath $projectRoot).GetOwner([Security.Principal.SecurityIdentifier]).Value
    if ($rootOwnerSid -ne $config.OwnerSid) { throw 'Project ownership changed; startup refused.' }
    foreach ($executable in @($config.PythonExecutable, $config.NodeExecutable)) {
        if (-not [IO.Path]::IsPathRooted($executable) -or -not (Test-Path -LiteralPath $executable -PathType Leaf)) { throw 'A configured runtime executable is missing.' }
        Assert-NoBroadWrite $executable
    }
    $paths = @((Split-Path -Parent $config.PythonExecutable), (Split-Path -Parent $config.NodeExecutable))
    if ($config.GitExecutable) {
        if (-not (Test-Path -LiteralPath $config.GitExecutable -PathType Leaf)) { throw 'The configured Git executable is missing.' }
        Assert-NoBroadWrite $config.GitExecutable
        $paths += Split-Path -Parent $config.GitExecutable
    }
    $paths += @((Join-Path $config.UserProfile 'AppData\Roaming\npm'), (Join-Path $env:SystemRoot 'System32'), $env:SystemRoot, (Join-Path $env:SystemRoot 'System32\Wbem'), (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0'), (Join-Path $env:SystemRoot 'System32\OpenSSH'))
    if ($ValidateOnly) {
        $pythonw = Join-Path (Split-Path -Parent $config.PythonExecutable) 'pythonw.exe'
        if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) { throw 'The windowless Python runtime is missing.' }
        Assert-NoBroadWrite $pythonw
        Assert-NoBroadWrite (Join-Path $PSScriptRoot 'autostart-gui.pyw')
        [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
        [ordered]@{PythonExecutable=$config.PythonExecutable;PythonwExecutable=$pythonw;Path=(($paths | Select-Object -Unique) -join ';');ProjectRoot=$projectRoot} | ConvertTo-Json -Compress
        return
    }
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $config.PythonExecutable
    $info.Arguments = '-m dispatcher.startup --root "' + $projectRoot + '"'
    $info.WorkingDirectory = $projectRoot
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.EnvironmentVariables['PATH'] = (($paths | Select-Object -Unique) -join ';')
    foreach ($name in @('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV')) { $info.EnvironmentVariables.Remove($name) }
    $info.EnvironmentVariables['PYTHONUTF8'] = '1'
    $info.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $info
    if (-not $process.Start()) { throw 'Failed to start the dispatcher supervisor.' }
    $process.WaitForExit()
    $exitCode = $process.ExitCode
    $process.Dispose()
    exit $exitCode
} catch {
    # Do not dump environments, credentials or process command lines into logs.
    $record = [ordered]@{Time=[DateTime]::UtcNow.ToString('o'); State='launcher_failed'; Error=$_.Exception.Message}
    try {
        $log = Join-Path $projectRoot 'data\startup-launcher-error.json'
        [IO.File]::WriteAllText($log, ($record | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
    } catch { }
    exit 1
}
