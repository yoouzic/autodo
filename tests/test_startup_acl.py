"""Exercise startup ACL policy in Windows PowerShell 5.1, without registration."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(os.name == 'nt', 'Windows ACL integration test')
class StartupAclTests(unittest.TestCase):
    def test_package_sids_are_read_directly_and_shared_write_is_rejected(self):
        shell = Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, STARTUP_TEST_ROOT=directory,
                       STARTUP_SOURCE_ROOT=str(Path(__file__).resolve().parents[1]))
            # A parent pwsh 7 module path is incompatible with Windows PowerShell 5.1.
            env = {key: value for key, value in env.items() if key.upper() != 'PSMODULEPATH'}
            script = r'''
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$root=(Resolve-Path -LiteralPath $env:STARTUP_TEST_ROOT).Path
$safe=Join-Path $root 'package-readonly'
$unsafe=Join-Path $root 'shared-write'
$owner=[Security.Principal.WindowsIdentity]::GetCurrent().User
foreach($path in @($safe,$unsafe)) {
    New-Item -ItemType Directory -Path $path | Out-Null
    $acl=Get-Acl -LiteralPath $path
    $acl.SetAccessRuleProtection($true,$false)
    foreach($rule in @($acl.Access)){[void]$acl.RemoveAccessRuleSpecific($rule)}
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($owner,'FullControl','ContainerInherit,ObjectInherit','None','Allow'))
    foreach($id in @('S-1-15-2-1','S-1-15-2-2')) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($id),'ReadAndExecute','ContainerInherit,ObjectInherit','None','Allow'))
    }
    if($path -eq $unsafe) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new('S-1-5-11'),'Modify','ContainerInherit,ObjectInherit','None','Allow'))
    }
    Set-Acl -LiteralPath $path -AclObject $acl
}
$results=@(foreach($name in @('install-autostart.ps1','autostart-run.ps1')) {
    $tokens=$null;$errors=$null
    $source=Join-Path $env:STARTUP_SOURCE_ROOT ('scripts\'+$name)
    $ast=[Management.Automation.Language.Parser]::ParseFile($source,[ref]$tokens,[ref]$errors)
    if($errors.Count){throw $errors[0]}
    $function=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Assert-NoBroadWrite'},$true)
    . ([ScriptBlock]::Create($function.Extent.Text))
    Assert-NoBroadWrite $safe
    $rejected=$false
    try { Assert-NoBroadWrite $unsafe } catch { $rejected=$true }
    [PSCustomObject]@{Script=$name;PackageReadAllowed=$true;SharedWriteRejected=$rejected}
})
ConvertTo-Json -InputObject $results
'''
            import base64
            encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
            result = subprocess.run([str(shell), '-NoLogo', '-NoProfile', '-NonInteractive',
                                     '-EncodedCommand', encoded], env=env, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
            rows = json.loads(result.stdout.decode('utf-8-sig'))
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(row['PackageReadAllowed'] and row['SharedWriteRejected'] for row in rows))


if __name__ == '__main__':
    unittest.main()
