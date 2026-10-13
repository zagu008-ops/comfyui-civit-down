import importlib.util
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from aiohttp.test_utils import TestClient, TestServer

spec = importlib.util.spec_from_file_location('oauth_probe', Path(__file__).resolve().parents[1] / 'tools/verify_oauth_chain.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class TokenResponse:
    status = 200
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def json(self):
        return {'access_token': 'dummy-access', 'refresh_token': 'dummy-refresh', 'scope': ['5'], 'expires_in': 3600}


class TokenSession:
    def __init__(self, **kwargs): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    def post(self, *args, **kwargs): return TokenResponse()


class BrokenSession(TokenSession):
    def post(self, *args, **kwargs): raise TimeoutError()


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name) / 'config.json'
        self.cfg.write_text(json.dumps({'oauth_client_id': 'test-app', 'pairing_key': 'private-pairing-key'}))
        self.client = TestClient(TestServer(probe.create_probe(self.cfg, 18188)))
        await self.client.start_server()
    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()
    async def params(self):
        r = await self.client.get('/start', allow_redirects=False)
        self.assertEqual(r.status, 302)
        return parse_qs(urlsplit(r.headers['Location']).query)
    async def test_pkce_redirect_and_invalid_state(self):
        p = await self.params()
        self.assertEqual(p['redirect_uri'], ['http://127.0.0.1:18188/oauth/callback'])
        self.assertEqual(p['code_challenge_method'], ['S256'])
        self.assertGreaterEqual(len(p['code_challenge'][0]), 43)
        r = await self.client.get('/oauth/callback?state=wrong&code=stolen')
        self.assertEqual(r.status, 400)
        self.assertNotIn('civitai_token', json.loads(self.cfg.read_text()))
    async def test_denial_and_single_use(self):
        p = await self.params()
        r = await self.client.get('/oauth/callback', params={'state': p['state'][0], 'error': 'access_denied'})
        self.assertEqual(r.status, 400)
        r = await self.client.get('/oauth/callback', params={'state': p['state'][0], 'code': 'code'})
        self.assertEqual(r.status, 409)
        r = await self.client.get('/status')
        data = await r.json()
        self.assertEqual(data['state'], 'failed')
        self.assertNotIn('private-pairing-key', json.dumps(data))
    async def test_success_saves_and_hides_credentials(self):
        p = await self.params()
        with patch.object(probe.aiohttp, 'ClientSession', TokenSession):
            r = await self.client.get('/oauth/callback', params={'state': p['state'][0], 'code': 'dummy-code'})
        self.assertEqual(r.status, 200)
        cfg = json.loads(self.cfg.read_text())
        self.assertEqual(cfg['civitai_token'], 'dummy-access')
        self.assertEqual(cfg['pairing_key'], 'private-pairing-key')
        r = await self.client.get('/status')
        data = await r.json()
        self.assertEqual(data, {'state': 'done'})
        self.assertNotIn('dummy-access', await r.text())
    async def test_network_error_classified_without_sensitive_detail(self):
        p = await self.params()
        with patch.object(probe.aiohttp, 'ClientSession', BrokenSession):
            r = await self.client.get('/oauth/callback', params={'state': p['state'][0], 'code': 'dummy-code'})
        self.assertEqual(r.status, 400)
        r = await self.client.get('/status'); data = await r.json()
        self.assertEqual(data['stage'], 'token_exchange')
        self.assertIn('TimeoutError', data['error'])
        self.assertNotIn('dummy-code', json.dumps(data))


if __name__ == '__main__': unittest.main()
