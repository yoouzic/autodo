import json
import tempfile
import unittest
from pathlib import Path

from dispatcher.__main__ import load_config


class RemoteConfigTests(unittest.TestCase):
    def test_password_relay_requires_secret_and_compatible_tunnel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'data').mkdir()
            (root/'data/relay-proxy.key').write_text('s'*64)
            values = {'enabled': True, 'mode': 'password_proxy', 'origin': 'https://todo.example.com', 'tunnel_enabled': True}
            (root/'config.local.json').write_text(json.dumps({'remote_access': values}))
            config = load_config(root)
            self.assertEqual(config['remote_access']['ssh_alias'], 'autodo-relay')
            self.assertFalse((root/'data/web-auth.db').exists())
            values['mode'] = 'tailscale'
            (root/'config.local.json').write_text(json.dumps({'remote_access': values}))
            with self.assertRaises(ValueError):
                load_config(root)

    def test_remote_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            config = load_config(directory)
            self.assertFalse(config['remote_access']['enabled'])
            self.assertEqual(config['remote_access']['port'], 8766)

    def test_enabled_configuration_requires_separate_port_and_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid = {'enabled': True, 'port': 8766, 'origin': 'https://machine.example.ts.net', 'allowed_users': ['owner@example.com']}
            (root/'config.local.json').write_text(json.dumps({'remote_access': valid}), encoding='utf-8')
            configured = load_config(root)['remote_access']
            self.assertEqual({key: configured[key] for key in valid}, valid)
            for patch in ({'port': 8765}, {'port': True}, {'enabled': 1}, {'allowed_users': []},
                          {'origin': 'http://machine.example.ts.net'}, {'origin': 'https://*.example.ts.net'}):
                with self.subTest(patch=patch):
                    (root/'config.local.json').write_text(json.dumps({'remote_access': dict(valid, **patch)}), encoding='utf-8')
                    with self.assertRaises((TypeError, ValueError)):
                        load_config(root)


if __name__ == '__main__':
    unittest.main()
