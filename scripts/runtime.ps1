function Resolve-DispatcherPython {
    param([Parameter(Mandatory=$true)][string]$ProjectRoot)
    $configFile = Join-Path $ProjectRoot 'data\startup-config.json'
    if (Test-Path -LiteralPath $configFile) {
        $config = Get-Content -LiteralPath $configFile -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($config.ProjectRoot -ne $ProjectRoot) { throw 'Startup configuration belongs to another project.' }
        $candidate = [string]$config.PythonExecutable
    } else {
        $candidate = (Get-Command python.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    }
    if (-not [IO.Path]::IsPathRooted($candidate) -or -not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        throw 'The pinned Python runtime is missing. Run install-autostart.ps1 with -PythonExecutable.'
    }
    return (Resolve-Path -LiteralPath $candidate).Path
}

function Assert-DispatcherUserContext {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if ($identity.User.Value -eq 'S-1-5-18' -or $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run the dispatcher from your normal Windows account, not an elevated or SYSTEM session.'
    }
}
