import json
import tempfile
import unittest
from pathlib import Path

from dispatcher.__main__ import load_config


class CollectionConfigTests(unittest.TestCase):
    def test_legacy_config_gains_empty_collections(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'config.local.json').write_text(json.dumps({'workspace_discovery':{'native_agents':False}}))
            self.assertEqual(load_config(root)['workspace_discovery'],{'native_agents':False,'collections':[]})

    def test_valid_collection_config_preserves_other_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);collection=root/'collection';collection.mkdir()
            spec={'path':str(collection),'label':'Codex','agent':'codex','groups':['courses']}
            config={'poll_seconds':20,'workspace_discovery':{'native_agents':False,'collections':[spec]}}
            (root/'config.local.json').write_text(json.dumps(config))
            result=load_config(root)
            self.assertEqual(result['workspace_discovery']['collections'][0]['path'],str(collection))
            self.assertEqual(result['workspace_discovery']['collections'][0]['groups'],['courses'])
            self.assertFalse(result['workspace_discovery']['native_agents'])
            self.assertEqual(result['poll_seconds'],20)

    def test_unknown_discovery_fields_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'config.local.json').write_text(json.dumps({'workspace_discovery':{'deep_scan':True}}))
            with self.assertRaises(ValueError):load_config(root)

    def test_duplicate_collection_labels_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            specs=[{'path':str(root/'one'),'label':'Codex'},{'path':str(root/'two'),'label':'codex'}]
            (root/'config.local.json').write_text(json.dumps({'workspace_discovery':{'collections':specs}}))
            with self.assertRaises(ValueError):load_config(root)


if __name__=='__main__':unittest.main()
