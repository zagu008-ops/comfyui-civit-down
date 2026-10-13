import asyncio
import hashlib
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

folder_paths = types.ModuleType('folder_paths')
folder_paths.get_folder_paths = lambda _: []
folder_paths.filename_list_cache = {}
sys.modules['folder_paths'] = folder_paths
server = types.ModuleType('server')
routes = types.SimpleNamespace(get=lambda _: lambda f: f, post=lambda _: lambda f: f)
server.PromptServer = types.SimpleNamespace(instance=types.SimpleNamespace(routes=routes))
sys.modules['server'] = server
spec = importlib.util.spec_from_file_location('download_helper', Path(__file__).resolve().parents[1] / '__init__.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class Response:
    def __init__(self, data, status=200, headers=None):
        self.status = status
        self.headers = headers or {}
        self.content = self
        self.data = data
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def iter_chunked(self, size):
        yield self.data


class Session:
    response = None
    calls = []
    def __init__(self, **kwargs): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class HelperTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.file = {'versionId': 2, 'fileId': 3, 'name': 'test.safetensors', 'sha256': hashlib.sha256(b'model').hexdigest(), 'bytes': 5}
        self.job = {'id': 'job', 'state': 'queued', 'folder': '恋上换装娃娃', 'received': 0}
        Session.calls = []
    def tearDown(self): self.tmp.cleanup()
    async def run_download(self, response, file=None):
        Session.response = response
        with patch.object(m.aiohttp, 'ClientSession', Session):
            await m.download(self.job, {'civitai_token': 'test-secret'}, self.root, 'civitai.red', file or self.file)
    async def test_verified_download_and_reuse(self):
        await self.run_download(Response(b'model'))
        self.assertEqual(self.job['state'], 'done')
        self.assertEqual((self.root / self.job['path']).read_bytes(), b'model')
        self.assertFalse(list(self.root.rglob('*.part')))
        self.job.update(state='queued', received=0)
        with patch.object(m.aiohttp, 'ClientSession', side_effect=AssertionError('reuse must not download')):
            await m.download(self.job, {}, self.root, 'civitai.red', self.file)
        self.assertTrue(self.job['reused'])
    async def test_bad_hash_cleans_partial(self):
        await self.run_download(Response(b'wrong'))
        self.assertEqual(self.job['state'], 'failed')
        self.assertFalse(list(self.root.rglob('*.safetensors')))
        self.assertFalse(list(self.root.rglob('*.part')))
    async def test_auth_failure_no_file_or_token_disclosure(self):
        await self.run_download(Response(b'', status=401))
        self.assertIn('401', self.job['error'])
        self.assertNotIn('test-secret', json.dumps(self.job))
        self.assertFalse(list(self.root.rglob('*.part')))
    async def test_conflict_does_not_overwrite(self):
        folder = self.root / self.job['folder']; folder.mkdir()
        existing = folder / self.file['name']; existing.write_bytes(b'old')
        await self.run_download(Response(b'model'))
        self.assertEqual(existing.read_bytes(), b'old')
        self.assertNotEqual(self.job['path'], str(existing.relative_to(self.root)))
    async def test_symlink_escape_rejected(self):
        (self.root / self.job['folder']).symlink_to(self.root.parent, target_is_directory=True)
        await self.run_download(Response(b'model'))
        self.assertEqual(self.job['state'], 'failed')
        self.assertIn('目录', self.job['error'])
    async def test_redirect_to_private_host_rejected(self):
        await self.run_download(Response(b'', status=302, headers={'Location': 'https://127.0.0.1/private'}))
        self.assertEqual(len(Session.calls), 1)
        self.assertEqual(self.job['state'], 'failed')
    async def test_pairing_and_paths(self):
        cfg={'pairing_key':'x'*32,'allowed_origins':['http://phone:8000'],'lora_root':str(self.root)}
        request=types.SimpleNamespace(headers={'Authorization':'Bearer '+ 'x'*32,'Origin':'http://phone:8000'})
        with patch.object(m, 'config', return_value=cfg), patch.object(folder_paths, 'get_folder_paths', return_value=[str(self.root)]):
            self.assertEqual(m.authorize(request)[1], self.root)
            request.headers['Origin']='http://other:8000'
            with self.assertRaises(m.web.HTTPForbidden): m.authorize(request)
            request.headers.pop('Origin');request.headers['Authorization']='Bearer wrong'
            with self.assertRaises(m.web.HTTPUnauthorized): m.authorize(request)
        for value in ['../escape', '/absolute', '..', 'folder\\file', 'bad\x00']:
            with self.assertRaises(ValueError): m.safe_part(value)
        self.assertEqual(m.parse_link('https://civitai.red/models/1505483/character')[1], '1505483')
        with self.assertRaises(ValueError): m.parse_link('https://localhost/models/1')

    async def test_oauth_wait_backoff_and_save_without_exposing_tokens(self):
        auth={'id':'auth-test','state':'pending','clientId':'our-app'}
        cfg_path=self.root/'config.json'
        cfg_path.write_text(json.dumps({'pairing_key':'x'*32,'oauth_client_id':'our-app'}))
        responses=[{'error':'authorization_pending'},{'error':'rate_limited'},
                   {'access_token':'private-access','refresh_token':'private-refresh','expires_in':3600,'scope':'5'}]
        with patch.object(m,'CONFIG',cfg_path), patch.object(m.aiohttp,'ClientSession',Session), patch.object(m,'oauth_post',AsyncMock(side_effect=responses)), patch.object(m.asyncio,'sleep',AsyncMock()) as sleep:
            await m.poll_auth(auth,{'device_code':'private-device-code','interval':5,'expires_in':900})
        self.assertEqual(auth['state'],'done')
        self.assertEqual([call.args[0] for call in sleep.call_args_list],[5,5,10])
        saved=json.loads(cfg_path.read_text())
        self.assertEqual(saved['oauth_refresh_token'],'private-refresh')
        self.assertNotIn('private-access',json.dumps(auth))
        self.assertEqual(cfg_path.stat().st_mode & 0o777,0o600)

    async def test_oauth_denial_and_missing_permission(self):
        auth={'id':'auth-test','state':'pending','clientId':'our-app'}
        with patch.object(m.aiohttp,'ClientSession',Session),patch.object(m,'oauth_post',AsyncMock(return_value={'error':'access_denied'})),patch.object(m.asyncio,'sleep',AsyncMock()):
            await m.poll_auth(auth,{'device_code':'code','expires_in':900})
        self.assertEqual(auth['state'],'failed')
        with self.assertRaises(ValueError):m.store_oauth({'access_token':'a','refresh_token':'r','scope':'1'})

    async def test_oauth_refresh_rotation(self):
        cfg_path=self.root/'config.json'
        cfg_path.write_text(json.dumps({'oauth_client_id':'our-app','civitai_token':'old-access','oauth_refresh_token':'old-refresh','oauth_expires_at':0}))
        response={'access_token':'new-access','refresh_token':'new-refresh','scope':'5','expires_in':3600}
        with patch.object(m,'CONFIG',cfg_path),patch.object(m.aiohttp,'ClientSession',Session),patch.object(m,'oauth_post',AsyncMock(return_value=response)) as post:
            cfg=await m.fresh_config()
        self.assertEqual(cfg['oauth_refresh_token'],'new-refresh')
        self.assertEqual(post.call_args.args[2]['grant_type'],'refresh_token')


if __name__ == '__main__': unittest.main()
