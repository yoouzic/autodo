import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dispatcher.__main__ import load_config
from dispatcher.external import invocation


class AntigravityProxyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()

    def config(self, value):
        (self.root / 'config.local.json').write_text(
            json.dumps({'antigravity_proxy_url': value}), encoding='utf-8')
        return load_config(self.root)

    def provider_env(self, name, config):
        return invocation(name, [name], self.workspace, self.root,
                          'write a file', config)[1]

    def test_proxy_is_disabled_by_default(self):
        config = load_config(self.root)
        self.assertEqual(config.get('antigravity_proxy_url'), '')
        self.assertEqual(self.provider_env('antigravity', config), {'CI': '1'})

    def test_empty_proxy_keeps_default_environment(self):
        self.assertEqual(self.provider_env('antigravity', self.config('')), {'CI': '1'})

    def test_loopback_urls_and_port_boundaries_are_accepted(self):
        for proxy in ['http://127.0.0.1:12334', 'http://localhost:1', 'http://[::1]:65535']:
            with self.subTest(proxy=proxy):
                config = self.config(proxy)
                self.assertEqual(config['antigravity_proxy_url'], proxy)
                self.assertEqual(self.provider_env('antigravity', config), {
                    'CI': '1', 'HTTP_PROXY': proxy, 'HTTPS_PROXY': proxy,
                    'NO_PROXY': 'localhost,127.0.0.1,::1'})

    def test_untrusted_urls_and_nonstring_values_are_rejected_without_echo(self):
        invalid = [None, False, 12334, [], {},
            'http://example.com:12334', 'http://127.0.0.2:12334',
            'http://0.0.0.0:12334', 'http://[::]:12334',
            'http://127.1:12334', 'http://2130706433:12334',
            'http://localhost.example.com:12334', 'http://localhost.:12334',
            'http://127.0.0.1:0', 'http://127.0.0.1:65536',
            'http://127.0.0.1:-1', 'http://127.0.0.1:abc', 'http://127.0.0.1',
            'http://user:TOP_SECRET@127.0.0.1:12334', 'http://@127.0.0.1:12334',
            'http://127.0.0.1:12334/', 'http://127.0.0.1:12334/a',
            'http://127.0.0.1:12334?', 'http://127.0.0.1:12334?x=1',
            'http://127.0.0.1:12334#', 'http://127.0.0.1:12334#fragment',
            'https://127.0.0.1:12334', 'socks5://127.0.0.1:12334',
            ' http://127.0.0.1:12334', 'http://127.0.0.1:12334\n',
            'http://local\thost:12334', 'http://127.0.0.1:12334\\other',
            'http://[::1%25zone]:12334', 'http://[::1:12334',
            'http://%6cocalhost:12334']
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, 'antigravity_proxy_url') as raised:
                    self.config(value)
                self.assertNotIn('TOP_SECRET', str(raised.exception))

    def test_direct_invocation_revalidates_proxy_before_launch(self):
        with self.assertRaisesRegex(ValueError, 'antigravity_proxy_url'):
            self.provider_env('antigravity', {'antigravity_proxy_url': 'http://external.example:8080'})

    def test_proxy_override_is_only_for_antigravity_and_never_global(self):
        config = self.config('http://127.0.0.1:12334')
        inherited = {'HTTP_PROXY': 'http://old-proxy.example:8080',
                     'HTTPS_PROXY': 'http://old-proxy.example:8080', 'NO_PROXY': 'old.example'}
        with patch.dict(os.environ, inherited):
            agy_env = self.provider_env('antigravity', config)
            self.assertEqual(agy_env['HTTPS_PROXY'], config['antigravity_proxy_url'])
            for name in ['dsh', 'zcode', 'qoder']:
                with self.subTest(name=name):
                    env = self.provider_env(name, config)
                    self.assertTrue(set(env).isdisjoint({'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY'}))
            self.assertEqual({key: os.environ[key] for key in inherited}, inherited)


if __name__ == '__main__':
    unittest.main()
