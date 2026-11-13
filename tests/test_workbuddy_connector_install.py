"""Installer tests use temporary JSON files only, with no WorkBuddy process."""
import contextlib
import ctypes
import importlib.util
import io
import json
import os
import re
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


class WorkBuddyConnectorInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve()
        self.code=self.base/'code';(self.code/'dispatcher').mkdir(parents=True)
        (self.code/'dispatcher/workbuddy_mcp.py').write_text('# Not executed\n')
        (self.code/'scripts').mkdir()
        (self.code/'scripts/workbuddy-mcp-worker.py').write_text('# Not executed\n')
        self.root=self.base/'data-root';self.root.mkdir()
        self.config=self.base/'profile/.workbuddy/mcp.json'
        path=Path(__file__).resolve().parents[1]/'scripts/install-workbuddy-connector.py'
        spec=importlib.util.spec_from_file_location('test_connector_installer',path)
        self.installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.installer)
        self.installer.PROJECT_ROOT=self.code

    def write(self,value):
        self.config.parent.mkdir(parents=True,exist_ok=True)
        self.config.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')

    def read(self):return json.loads(self.config.read_text(encoding='utf-8'))

    def install(self,**kwargs):
        return self.installer.install_connector(root=self.root,config=self.config,**kwargs)

    def test_creates_only_standard_stdio_connector(self):
        result=self.install()
        entry=self.read()['mcpServers']['todo-dispatcher']
        self.assertEqual(result['status'],'installed')
        self.assertEqual(entry,{'command':str(Path(self.installer.sys.executable).absolute()),
            'args':['-B',str(self.code/'scripts/workbuddy-mcp-worker.py'),'--root',str(self.root)],'cwd':str(self.code)})
        self.assertIsNone(result['backup_path'])
        self.assertFalse((self.root/'data').exists())

    def test_preserves_all_existing_servers_root_keys_and_exact_backup_bytes(self):
        original={'mcpServers':{'user-server':{'command':'user.exe','env':{'TOKEN':'private-token'}}},
                  'custom':{'message':'保留我的配置'},'disabled':False}
        self.write(original)
        before=self.config.read_bytes()
        result=self.install();after=self.read()
        self.assertEqual(after['mcpServers']['user-server'],original['mcpServers']['user-server'])
        self.assertEqual(after['custom'],original['custom'])
        self.assertEqual(after['disabled'],False)
        self.assertEqual(Path(result['backup_path']).read_bytes(),before)
        self.assertEqual(Path(result['backup_path']).parent,self.config.parent)

    def test_check_does_not_create_directories_or_files(self):
        result=self.install(check=True)
        self.assertEqual(result['status'],'would_install')
        self.assertFalse(self.config.parent.exists())

    def test_default_configuration_is_home_workbuddy(self):
        with patch.object(self.installer.Path,'home',return_value=self.base/'home'):
            result=self.installer.install_connector(root=self.root)
        self.assertEqual(Path(result['config_path']),self.base/'home/.workbuddy/mcp.json')

    def test_repeated_install_does_not_backup_or_change_existing_file(self):
        self.install();before=self.config.read_bytes();info=self.config.stat()
        result=self.install()
        self.assertEqual(result['status'],'unchanged')
        self.assertEqual(self.config.read_bytes(),before)
        self.assertEqual(self.config.stat().st_mtime_ns,info.st_mtime_ns)
        self.assertEqual(list(self.config.parent.glob('*.backup-*.json')),[])

    def test_different_entry_is_never_overwritten_even_with_replace_flag(self):
        for entry in ({'command':'private-tool','args':[]},None,{},'some-value'):
            with self.subTest(entry=entry):
                self.write({'mcpServers':{'todo-dispatcher':entry}})
                before=self.config.read_bytes()
                with self.assertRaises(self.installer.ConnectorInstallError):self.install(replace_dispatcher_only=True)
                self.assertEqual(self.config.read_bytes(),before)

    def test_only_known_connector_can_change_root_with_explicit_flag(self):
        self.install();original=self.read()
        alternative=self.base/'probe';alternative.mkdir()
        with self.assertRaises(self.installer.ConnectorInstallError):
            self.installer.install_connector(root=alternative,config=self.config)
        preview=self.installer.install_connector(root=alternative,config=self.config,
                                               check=True,replace_dispatcher_only=True)
        self.assertEqual(preview['status'],'would_replace')
        self.assertEqual(self.read(),original)
        result=self.installer.install_connector(root=alternative,config=self.config,replace_dispatcher_only=True)
        self.assertEqual(result['status'],'replaced')
        self.assertEqual(self.read()['mcpServers']['todo-dispatcher']['args'][-1],str(alternative))

    def test_known_name_with_extra_permissions_is_not_our_replaceable_entry(self):
        self.install();value=self.read()
        value['mcpServers']['todo-dispatcher']['alwaysAllow']=['anything']
        self.write(value)
        with self.assertRaises(self.installer.ConnectorInstallError):self.install(replace_dispatcher_only=True)
        self.assertEqual(self.read(),value)

    def test_old_module_entry_migrates_only_explicitly_with_exact_backup(self):
        old={'command':str(Path(self.installer.sys.executable).absolute()),
             'args':['-B','-m','dispatcher.workbuddy_mcp','--root',str(self.root)],'cwd':str(self.code)}
        original={'mcpServers':{'todo-dispatcher':old,'unrelated':{'command':'keep.exe'}},'custom':True}
        self.write(original)
        before=self.config.read_bytes()
        with self.assertRaises(self.installer.ConnectorInstallError):self.install()
        check=self.install(check=True,replace_dispatcher_only=True)
        self.assertEqual(check['status'],'would_replace')
        self.assertEqual(self.config.read_bytes(),before)
        result=self.install(replace_dispatcher_only=True)
        self.assertEqual(result['status'],'replaced')
        self.assertEqual(Path(result['backup_path']).read_bytes(),before)
        after=self.read()
        self.assertEqual(after['mcpServers']['unrelated'],original['mcpServers']['unrelated'])
        self.assertTrue(after['custom'])
        self.assertEqual(after['mcpServers']['todo-dispatcher']['args'],
                         ['-B',str(self.code/'scripts/workbuddy-mcp-worker.py'),'--root',str(self.root)])

    def test_old_and_new_entries_with_another_executable_or_source_are_not_migrated(self):
        valid={'command':str(Path(self.installer.sys.executable).absolute()),
               'args':['-B','-m','dispatcher.workbuddy_mcp','--root',str(self.root)],'cwd':str(self.code)}
        variants=[dict(valid,command='other-python.exe'),dict(valid,cwd=str(self.base/'elsewhere')),
                  dict(valid,args=['-B','-m','dispatcher.workbuddy_mcp','--root',str(self.root),'--extra']),
                  dict(valid,args=['-B',str(self.base/'other-worker.py'),'--root',str(self.root)])]
        for value in variants:
            with self.subTest(value=value):
                self.write({'mcpServers':{'todo-dispatcher':value}})
                before=self.config.read_bytes()
                with self.assertRaises(self.installer.ConnectorInstallError):self.install(replace_dispatcher_only=True)
                self.assertEqual(self.config.read_bytes(),before)

    def test_invalid_json_and_schema_are_not_modified(self):
        self.config.parent.mkdir(parents=True)
        for text in ('[]','{"mcpServers":null}','{"mcpServers":{},"mcpServers":{}}',
                     '{"private":NaN}','not-json','{"x":"\ud800"}'):
            with self.subTest(text=text):
                self.config.write_bytes(text.encode('utf-8',errors='surrogatepass'))
                before=self.config.read_bytes()
                with self.assertRaises((self.installer.ConnectorInstallError,UnicodeError)):self.install()
                self.assertEqual(self.config.read_bytes(),before)

    def test_utf8_bom_config_is_supported_and_backup_is_exact(self):
        self.write({'mcpServers':{},'message':'中文'})
        self.config.write_bytes(b'\xef\xbb\xbf'+self.config.read_bytes());before=self.config.read_bytes()
        result=self.install()
        self.assertEqual(self.read()['message'],'中文')
        self.assertEqual(Path(result['backup_path']).read_bytes(),before)

    def test_oversized_configuration_is_rejected_before_parsing(self):
        self.config.parent.mkdir(parents=True)
        self.config.write_bytes(b' '*(self.installer.MAX_CONFIG_BYTES+1))
        with self.assertRaises(self.installer.ConnectorInstallError):self.install(check=True)

    def test_hardlinked_configuration_is_rejected(self):
        self.write({'mcpServers':{}})
        os.link(self.config,self.base/'linked.json')
        with self.assertRaises(self.installer.ConnectorInstallError):self.install()

    def test_reparse_parent_is_rejected(self):
        source=self.base/'ordinary';source.mkdir()
        link=self.base/'linked-parent'
        if os.name=='nt':
            # Junction setup changes only this owned temporary test directory.
            subprocess.run(['cmd','/c','mklink','/J',str(link),str(source)],check=True,capture_output=True)
            self.addCleanup(lambda:os.rmdir(link) if link.exists() else None)
        else:link.symlink_to(source,target_is_directory=True)
        with self.assertRaises(self.installer.ConnectorInstallError):
            self.installer.install_connector(root=self.root,config=link/'mcp.json')

    def test_concurrent_edit_is_not_overwritten(self):
        self.write({'mcpServers':{}})
        real=self.installer._new_file
        changed={'mcpServers':{'new-user-server':{'command':'new-user-command'}}}
        def write(path,data):
            real(path,data)
            if path.suffix=='.tmp':self.write(changed)
        with patch.object(self.installer,'_new_file',side_effect=write):
            with self.assertRaises(self.installer.ConnectorInstallError):self.install()
        self.assertEqual(self.read(),changed)

    def test_cli_never_prints_existing_configuration_or_secrets(self):
        self.write({'mcpServers':{'private-server':{'env':{'PRIVATE_TOKEN':'secret-contents'}}}})
        stdout,stderr=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
            result=self.installer.main(['--root',str(self.root),'--config',str(self.config),'--check'])
        self.assertEqual(result,0)
        for forbidden in ('private-server','PRIVATE_TOKEN','secret-contents'):
            self.assertNotIn(forbidden,stdout.getvalue()+stderr.getvalue())
        self.assertFalse(list(self.config.parent.glob('*.backup-*.json')))

    def test_missing_connector_module_refuses_install(self):
        with patch.object(self.installer,'PROJECT_ROOT',self.root):
            with self.assertRaisesRegex(self.installer.ConnectorInstallError,'not been installed'):
                self.install()
        self.assertFalse(self.config.exists())

    def windows_acl(self,path,*,set_sddl=None):
        """Inspect actual on-disk Windows DACLs, independent of installer code."""
        from ctypes import wintypes
        advapi=ctypes.WinDLL('advapi32',use_last_error=True)
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.LocalFree.argtypes=[ctypes.c_void_p];kernel.LocalFree.restype=ctypes.c_void_p
        if set_sddl is not None:
            descriptor=ctypes.c_void_p()
            advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes=[
                wintypes.LPCWSTR,wintypes.DWORD,ctypes.POINTER(ctypes.c_void_p),ctypes.c_void_p]
            advapi.SetFileSecurityW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,ctypes.c_void_p]
            self.assertTrue(advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(set_sddl,1,ctypes.byref(descriptor),None))
            try:self.assertTrue(advapi.SetFileSecurityW(str(path),4|0x80000000,descriptor))
            finally:kernel.LocalFree(descriptor)
        advapi.GetFileSecurityW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,ctypes.c_void_p,
                                         wintypes.DWORD,ctypes.POINTER(wintypes.DWORD)]
        needed=wintypes.DWORD()
        advapi.GetFileSecurityW(str(path),4,None,0,ctypes.byref(needed))
        self.assertGreater(needed.value,0)
        buffer=ctypes.create_string_buffer(needed.value)
        self.assertTrue(advapi.GetFileSecurityW(str(path),4,buffer,needed,ctypes.byref(needed)))
        text=wintypes.LPWSTR()
        advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes=[
            ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,ctypes.POINTER(wintypes.LPWSTR),ctypes.c_void_p]
        self.assertTrue(advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(buffer,1,4,ctypes.byref(text),None))
        try:return text.value
        finally:kernel.LocalFree(ctypes.cast(text,ctypes.c_void_p))

    def assert_private_windows_acl(self,path):
        identity=subprocess.check_output(['whoami','/user','/fo','csv','/nh'],encoding='utf-8',errors='replace')
        user_sid=re.search(r'S-1-5-21-(?:\d+-){2}\d+-\d+',identity).group(0)
        sddl=self.windows_acl(path)
        self.assertTrue(sddl.startswith('D:P'))
        self.assertEqual(set(re.findall(r'\(A;;FA;;;([^)]*)\)',sddl)),{user_sid,'SY'})
        self.assertEqual(sddl.count('('),2)

    @unittest.skipUnless(os.name=='nt','Windows DACL verification')
    def test_real_windows_acl_protects_backup_and_replacement_despite_broad_parent(self):
        self.write({'mcpServers':{'existing':{'env':{'TOKEN':'test-only-secret'}}}})
        # Broad inheritance is confined to this test's owned temporary folder.
        self.windows_acl(self.config.parent,set_sddl='D:P(A;OICI;FA;;;WD)')
        self.assertIn(';;;WD)',self.windows_acl(self.config.parent))
        result=self.install()
        self.assert_private_windows_acl(Path(result['backup_path']))
        self.assert_private_windows_acl(self.config)
        self.assertEqual(self.read()['mcpServers']['existing']['env']['TOKEN'],'test-only-secret')

    @unittest.skipUnless(os.name=='nt','Windows DACL verification')
    def test_real_windows_acl_protects_first_install(self):
        self.install()
        self.assert_private_windows_acl(self.config)

    @unittest.skipUnless(os.name=='nt','Windows private creation failure')
    def test_windows_acl_failure_never_writes_sensitive_backup_or_replaces_config(self):
        self.write({'mcpServers':{'existing':{'env':{'TOKEN':'test-only-secret'}}}})
        before=self.config.read_bytes()
        failure=self.installer.ConnectorInstallError('test ACL failure')
        with patch.object(self.installer,'_private_create_windows',side_effect=failure):
            with self.assertRaises(self.installer.ConnectorInstallError):self.install()
        self.assertEqual(self.config.read_bytes(),before)
        self.assertEqual([path.name for path in self.config.parent.iterdir()],['mcp.json'])


if __name__=='__main__':unittest.main()
