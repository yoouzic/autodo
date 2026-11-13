"""Install only the dispatcher stdio connector; never authenticates or runs tasks.

WorkBuddy watches its MCP configuration. No chat is created, no executor is
enabled, and no permission or automatic approval setting is changed here.
"""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import sys
import uuid


PROJECT_ROOT=Path(__file__).absolute().parent.parent
SERVER_NAME='todo-dispatcher'
MAX_CONFIG_BYTES=1024*1024
_MISSING=object()


class ConnectorInstallError(ValueError):
    pass


def _components(path):
    if not path.is_absolute() or '..' in path.parts:
        raise ConnectorInstallError('Only absolute local paths without traversal are supported.')
    if str(path).startswith(('\\\\','//')):
        raise ConnectorInstallError('Network paths are not supported.')
    for part in [*reversed(path.parents),path]:
        try:info=part.lstat()
        except FileNotFoundError:continue
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
            raise ConnectorInstallError('Links and reparse points are not supported.')
        if part!=path and not stat.S_ISDIR(info.st_mode):
            raise ConnectorInstallError('The configuration parent is not a directory.')


def _directory(path):
    _components(path)
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise ConnectorInstallError('The dispatcher root and code directory must already exist.')
    return path


def _regular(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or
            getattr(info,'st_file_attributes',0)&0x400):
        raise ConnectorInstallError('Configuration and connector files must be ordinary single-link files.')
    return info


def _identity(info):
    # Windows path-stat and handle-stat can report different ctime semantics
    # immediately after rename. File ID, size, mtime and exact bytes stay bound.
    return info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns


def _read(path):
    _components(path)
    try:before=_regular(path.lstat())
    except FileNotFoundError:return None
    if before.st_size>MAX_CONFIG_BYTES:
        raise ConnectorInstallError('The MCP configuration exceeds the size limit.')
    with path.open('rb') as source:
        if _identity(_regular(os.fstat(source.fileno())))!=_identity(before):
            raise ConnectorInstallError('The MCP configuration changed while being read.')
        data=source.read(MAX_CONFIG_BYTES+1)
        after=_regular(os.fstat(source.fileno()))
    _components(path)
    if (len(data)>MAX_CONFIG_BYTES or _identity(before)!=_identity(after) or
            _identity(before)!=_identity(_regular(path.lstat()))):
        raise ConnectorInstallError('The MCP configuration changed while being read.')
    return data,_identity(before)


def _pairs(pairs):
    value={}
    for key,item in pairs:
        if key in value:raise ConnectorInstallError('The MCP configuration contains duplicate keys.')
        value[key]=item
    return value


def _constant(value):
    raise ConnectorInstallError('The MCP configuration contains a non-JSON value.')


def _configuration(data):
    if data is None:return {}
    try:
        value=json.loads(data.decode('utf-8-sig'),object_pairs_hook=_pairs,parse_constant=_constant)
    except (UnicodeError,json.JSONDecodeError,RecursionError):
        raise ConnectorInstallError('The existing MCP configuration is not valid UTF-8 JSON.') from None
    if not isinstance(value,dict) or not isinstance(value.get('mcpServers',{}),dict):
        raise ConnectorInstallError('The MCP configuration requires an object and an object-valued mcpServers.')
    return value


def _path_key(value):
    return os.path.normcase(os.path.normpath(str(value)))


def _known_connector(entry,desired):
    if not isinstance(entry,dict) or set(entry)!=set(desired):return False
    if not isinstance(entry.get('command'),str) or _path_key(entry['command'])!=_path_key(desired['command']):return False
    if not isinstance(entry.get('cwd'),str) or _path_key(entry['cwd'])!=_path_key(desired['cwd']):return False
    args=entry.get('args')
    if not isinstance(args,list):return False
    if len(args)==5 and args[:4]==['-B','-m','dispatcher.workbuddy_mcp','--root']:
        previous_root=args[4]
    elif (len(args)==4 and args[0]=='-B' and isinstance(args[1],str) and
          _path_key(args[1])==_path_key(desired['args'][1]) and args[2]=='--root'):
        previous_root=args[3]
    else:return False
    if not isinstance(previous_root,str) or not previous_root or '\x00' in previous_root:return False
    previous=Path(previous_root)
    return previous.is_absolute() and '..' not in previous.parts and not str(previous).startswith(('\\\\','//'))


def _private_create_windows(path):
    """Create an empty file with a protected DACL before any bytes are written."""
    from ctypes import wintypes
    import msvcrt

    class SecurityAttributes(ctypes.Structure):
        _fields_=[('nLength',wintypes.DWORD),('lpSecurityDescriptor',ctypes.c_void_p),
                  ('bInheritHandle',wintypes.BOOL)]

    advapi=ctypes.WinDLL('advapi32',use_last_error=True)
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetCurrentProcess.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    kernel.CloseHandle.restype=wintypes.BOOL
    kernel.LocalFree.argtypes=[ctypes.c_void_p]
    kernel.LocalFree.restype=ctypes.c_void_p
    advapi.OpenProcessToken.argtypes=[wintypes.HANDLE,wintypes.DWORD,ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype=wintypes.BOOL
    advapi.GetTokenInformation.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,
                                       wintypes.DWORD,ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype=wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes=[ctypes.c_void_p,ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype=wintypes.BOOL
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes=[wintypes.LPCWSTR,
        wintypes.DWORD,ctypes.POINTER(ctypes.c_void_p),ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype=wintypes.BOOL
    kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,
        ctypes.POINTER(SecurityAttributes),wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    kernel.CreateFileW.restype=wintypes.HANDLE
    token=wintypes.HANDLE();needed=wintypes.DWORD()
    sid_text=wintypes.LPWSTR();security=ctypes.c_void_p();handle=None
    error='Could not create a private configuration file; no sensitive bytes were written.'
    try:
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(),8,ctypes.byref(token)):
            raise ConnectorInstallError(error)
        advapi.GetTokenInformation(token,1,None,0,ctypes.byref(needed))
        if not needed.value:raise ConnectorInstallError(error)
        buffer=ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token,1,buffer,needed,ctypes.byref(needed)):
            raise ConnectorInstallError(error)
        sid=ctypes.cast(buffer,ctypes.POINTER(ctypes.c_void_p))[0]
        if not advapi.ConvertSidToStringSidW(sid,ctypes.byref(sid_text)):
            raise ConnectorInstallError(error)
        sddl='D:P(A;;FA;;;'+sid_text.value+')(A;;FA;;;SY)'
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl,1,ctypes.byref(security),None):
            raise ConnectorInstallError(error)
        attributes=SecurityAttributes(ctypes.sizeof(SecurityAttributes),security,False)
        # CREATE_NEW + no sharing: no existing file can be replaced and there
        # is no period with inherited permissions, even for the empty file.
        handle=kernel.CreateFileW(str(path),0xC0000000,0,ctypes.byref(attributes),1,0x00200080,None)
        if handle==ctypes.c_void_p(-1).value:
            handle=None
            raise ConnectorInstallError(error)
        descriptor=msvcrt.open_osfhandle(handle,os.O_WRONLY|os.O_BINARY)
        handle=None  # The CRT descriptor now owns this handle.
        return descriptor
    finally:
        if handle is not None:kernel.CloseHandle(handle)
        if token:kernel.CloseHandle(token)
        if sid_text:kernel.LocalFree(ctypes.cast(sid_text,ctypes.c_void_p))
        if security:kernel.LocalFree(security)


def _new_file(path,data):
    _components(path)
    if os.name=='nt':
        descriptor=_private_create_windows(path)
    else:
        flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_BINARY',0)
        descriptor=os.open(path,flags,0o600)
    with os.fdopen(descriptor,'wb') as output:
        _regular(os.fstat(output.fileno()))
        output.write(data);output.flush();os.fsync(output.fileno())
    _regular(path.lstat())


def _unchanged(path,original):
    if _read(path)!=original:
        raise ConnectorInstallError('The MCP configuration changed; installation was not applied. Retry after checking it.')


def install_connector(*,root=None,config=None,check=False,replace_dispatcher_only=False):
    code=_directory(PROJECT_ROOT.absolute())
    target=_directory(Path(root).absolute() if root is not None else code)
    executable=Path(sys.executable).absolute()
    _components(executable);_regular(executable.lstat())
    module=code/'dispatcher/workbuddy_mcp.py'
    _components(module)
    try:_regular(module.lstat())
    except FileNotFoundError:
        raise ConnectorInstallError('The dispatcher MCP connector module has not been installed yet.') from None
    launcher=code/'scripts/workbuddy-mcp-worker.py'
    _components(launcher)
    try:_regular(launcher.lstat())
    except FileNotFoundError:
        raise ConnectorInstallError('The dispatcher absolute-path launcher has not been installed yet.') from None
    path=Path(config).absolute() if config is not None else Path.home()/'.workbuddy/mcp.json'
    _components(path)
    original=_read(path)
    values=_configuration(original[0] if original is not None else None)
    servers=values.get('mcpServers',{})
    desired={'command':str(executable),'args':['-B',str(launcher),'--root',str(target)],
             'cwd':str(code)}
    existing=servers.get(SERVER_NAME,_MISSING)
    if existing==desired:
        return {'status':'unchanged','config_path':str(path),'backup_path':None}
    replacing=existing is not _MISSING
    if replacing and (not replace_dispatcher_only or not _known_connector(existing,desired)):
        raise ConnectorInstallError('The todo-dispatcher entry conflicts. Only a verified existing dispatcher connector may be replaced with --replace-dispatcher-only.')
    servers=dict(servers);servers[SERVER_NAME]=desired
    values=dict(values,mcpServers=servers)
    payload=(json.dumps(values,ensure_ascii=False,indent=2,allow_nan=False)+'\n').encode('utf-8')
    if len(payload)>MAX_CONFIG_BYTES:
        raise ConnectorInstallError('The updated MCP configuration would exceed the size limit.')
    action='replace' if replacing else 'install'
    if check:
        return {'status':'would_'+action,'config_path':str(path),'backup_path':None}
    # Recheck both content and file identity before publishing anything. Backup
    # and replacement files are exclusive, beside the user's configuration.
    _components(path.parent)
    path.parent.mkdir(parents=True,exist_ok=True)
    _directory(path.parent)
    _unchanged(path,original)
    backup=None
    if original is not None:
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup=path.with_name(path.name+'.backup-'+stamp+'-'+uuid.uuid4().hex+'.json')
        _new_file(backup,original[0])
    temporary=path.with_name('.'+path.name+'.dispatcher-'+uuid.uuid4().hex+'.tmp')
    _new_file(temporary,payload)
    try:
        _unchanged(path,original)
        _components(temporary)
        if _read(temporary)[0]!=payload:
            raise ConnectorInstallError('The prepared connector configuration changed; installation was not applied.')
        os.replace(temporary,path)
    finally:
        # Only clean up this invocation's exact, unchanged temporary file.
        if temporary.exists():
            try:
                if _read(temporary)[0]==payload:temporary.unlink()
            except (OSError,ConnectorInstallError):pass
    if _read(path)[0]!=payload:
        raise ConnectorInstallError('The MCP configuration changed after installation; inspect it locally.')
    return {'status':'replaced' if replacing else 'installed','config_path':str(path),
            'backup_path':str(backup) if backup else None}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,help='Existing dispatcher data root (production or isolated probe).')
    parser.add_argument('--check',action='store_true',help='Read-only preflight; do not create or change files.')
    parser.add_argument('--config',type=Path,help='Alternative configuration path for isolated tests.')
    parser.add_argument('--replace-dispatcher-only',action='store_true',help='Replace only our precisely recognized connector to upgrade its launcher or change its root.')
    args=parser.parse_args(argv)
    try:
        result=install_connector(root=args.root,config=args.config,check=args.check,
                                 replace_dispatcher_only=args.replace_dispatcher_only)
    except ConnectorInstallError as exc:
        print(str(exc),file=sys.stderr);return 1
    except (OSError,ValueError,RecursionError):
        print('Connector configuration could not be safely installed; inspect the local paths and permissions.',file=sys.stderr)
        return 1
    print(json.dumps(result,ensure_ascii=True))
    print('Only the stdio connector is configured. No chat was created and no executor was enabled. Permissions and authentication are unchanged.')
    return 0


if __name__=='__main__':raise SystemExit(main())
