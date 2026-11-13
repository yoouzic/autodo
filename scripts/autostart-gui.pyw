"""Windowless task entry; validate the same owner/ACL policy before importing app code."""
import json
import os
from pathlib import Path
import subprocess
import sys


def validated_config(root):
    shell=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
    # A pwsh 7 module path must not leak into the Windows PowerShell 5.1 preflight.
    env={k:v for k,v in os.environ.items() if k.upper() not in {'PSMODULEPATH','PYTHONHOME','PYTHONPATH','VIRTUAL_ENV'}}
    checked=subprocess.run([str(shell),'-NoLogo','-NoProfile','-NonInteractive',
        '-ExecutionPolicy','RemoteSigned','-File',str(root/'scripts/autostart-run.ps1'),'-ValidateOnly'],
        cwd=root,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,timeout=60,check=False)
    if checked.returncode:
        raise RuntimeError('Startup owner, runtime or ACL preflight failed; see startup-launcher-error.json')
    config=json.loads(checked.stdout.decode('utf-8-sig'))
    if Path(config['ProjectRoot']).resolve()!=root or Path(config['PythonwExecutable']).resolve()!=Path(sys.executable).resolve():
        raise RuntimeError('Windowless runtime does not match the installed dispatcher')
    return config


def main():
    root=Path(__file__).resolve().parents[1]
    try:
        if sys.argv[1:] not in ([],['--check']):raise ValueError('Unsupported startup arguments')
        config=validated_config(root)
        if sys.argv[1:]==['--check']:
            import ctypes
            receipt={'validated':True,'console_attached':bool(ctypes.windll.kernel32.GetConsoleWindow()),'pid':os.getpid()}
            (root/'data/startup-gui-check.json').write_text(json.dumps(receipt),encoding='utf-8')
            return 0
        for key in list(os.environ):
            if key.upper() in {'PYTHONHOME','PYTHONPATH','VIRTUAL_ENV'}:del os.environ[key]
        os.environ.update(PATH=config['Path'],PYTHONUTF8='1',PYTHONIOENCODING='utf-8')
        os.chdir(root)
        sys.path.insert(0,str(root))
        from dispatcher.startup import Supervisor
        return Supervisor(root,python_executable=config['PythonExecutable']).run()
    except Exception as exc:
        # pythonw has no stderr. Never dump arguments, environments or credentials.
        path=root/'data/startup-gui-error.json'
        if path.resolve().parent==(root/'data').resolve():
            path.write_text(json.dumps({'state':'launcher_failed','error_type':type(exc).__name__}),encoding='utf-8')
        return 1


if __name__=='__main__':raise SystemExit(main())
