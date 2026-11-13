import json
import tempfile
import unittest
from pathlib import Path
from dispatcher.__main__ import load_config


class RepositoryConfigTests(unittest.TestCase):
    def test_repository_checks_require_argv_and_known_repository(self):
        cases=[{'x':{'commands':['python -m unittest'],'report':'REPORT.md'}},
               {'x':{'commands':[],'report':'REPORT.md'}},
               {'unknown':{'commands':[['python','-m','unittest']],'report':'REPORT.md'}}]
        for checks in cases:
            with self.subTest(checks=checks), tempfile.TemporaryDirectory() as d:
                p=Path(d);(p/'config.local.json').write_text(json.dumps({'repositories':{'x':d},'repository_checks':checks}))
                with self.assertRaises(ValueError):load_config(p)

    def test_valid_checks_retained(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);checks={'x':{'commands':[['python','-B','-m','unittest']],'report':'REPORT.md'}}
            (p/'config.local.json').write_text(json.dumps({'repositories':{'x':d},'repository_checks':checks}))
            self.assertEqual(load_config(p)['repository_checks'],checks)


if __name__=='__main__':unittest.main()
