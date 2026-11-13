import importlib.machinery,importlib.util,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch

SOURCE=Path(__file__).resolve().parents[1]/'scripts/autostart-gui.pyw'
loader=importlib.machinery.SourceFileLoader('startup_gui_test',str(SOURCE))
spec=importlib.util.spec_from_loader(loader.name,loader)
gui=importlib.util.module_from_spec(spec);loader.exec_module(gui)

@unittest.skipUnless(os.name=='nt','Windows GUI launcher')
class StartupGuiTests(unittest.TestCase):
    def test_preflight_is_hidden_captures_output_and_preserves_acl_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            config={'ProjectRoot':str(root),'PythonwExecutable':sys.executable}
            result=subprocess.CompletedProcess([],0,json.dumps(config).encode(),b'')
            with patch.dict(os.environ,{'PSModulePath':'foreign-modules','PYTHONPATH':'foreign-python'}),patch.object(gui.subprocess,'run',return_value=result) as run:
                self.assertEqual(gui.validated_config(root),config)
            args,options=run.call_args
            self.assertIn('-ValidateOnly',args[0]);self.assertIn('RemoteSigned',args[0])
            self.assertEqual(options['creationflags'],subprocess.CREATE_NO_WINDOW)
            self.assertEqual(options['stdin'],subprocess.DEVNULL)
            self.assertFalse({'PSMODULEPATH','PYTHONPATH'} & {k.upper() for k in options['env']})

    def test_rejected_preflight_and_wrong_interpreter_cannot_start_supervisor(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            for result in (subprocess.CompletedProcess([],1,b'secret-test',b'secret-test'),
                subprocess.CompletedProcess([],0,json.dumps({'ProjectRoot':str(root),'PythonwExecutable':str(root/'other.exe')}).encode(),b'')):
                with patch.object(gui.subprocess,'run',return_value=result),self.assertRaises(RuntimeError) as caught:
                    gui.validated_config(root)
                self.assertNotIn('secret-test',str(caught.exception))

if __name__=='__main__':unittest.main()
