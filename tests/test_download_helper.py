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
sys.modules[spec.name] = m
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
    async def test_duplicate_submission_retains_live_progress(self):
        from job_store import JobStore
        store = JobStore(self.root / 'jobs.sqlite3')
        snapshot = {'root': str(self.root), 'folder': self.job['folder'], 'file': self.file, 'host': 'civitai.com', 'modelId': '1'}
        saved, _ = store.reserve(snapshot)
        live = dict(saved, state='downloading', received=3)
        request = types.SimpleNamespace(content_length=100, json=AsyncMock(return_value={'url': 'https://civitai.com/models/1', 'folder': self.job['folder'], 'versionId': 2, 'fileId': 3}))
        with patch.object(m, 'authorize', return_value=({}, self.root)), patch.object(m, 'fresh_config', AsyncMock(return_value={})), patch.object(m, 'get_store', return_value=store), patch.object(m.aiohttp, 'ClientSession', Session), patch.object(m, 'metadata', AsyncMock(return_value={})), patch.object(m, 'model_files', return_value=[self.file]), patch.dict(m.JOBS, {saved['id']: live}):
            response = await m.start(request)
            self.assertIs(m.JOBS[saved['id']], live)
        self.assertEqual(json.loads(response.text)['received'], 3)

    async def test_local_admin_rejects_remote_and_cross_origin(self):
        base = {'remote': '127.0.0.1', 'host': '127.0.0.1:8188', 'scheme': 'http', 'headers': {}}
        m.require_local(types.SimpleNamespace(**base))
        for changed in ({'remote': '192.168.1.2'}, {'host': 'attacker.invalid:8188'}, {'headers': {'Origin': 'https://attacker.invalid'}}, {'headers': {'Sec-Fetch-Site': 'cross-site'}}):
            with self.assertRaises(m.web.HTTPForbidden):
                m.require_local(types.SimpleNamespace(**{**base, **changed}))

    async def test_invalid_request_body_is_rejected(self):
        for value in ([], None, 'not-object'):
            request = types.SimpleNamespace(content_length=20, json=AsyncMock(return_value=value))
            with self.assertRaises(ValueError):
                await m.request_object(request)
        request = types.SimpleNamespace(content_length=20000, json=AsyncMock(return_value={}))
        with self.assertRaises(ValueError):
            await m.request_object(request)
        request.json.assert_not_called()

    async def test_retry_active_does_not_delete_partial(self):
        from job_store import JobStore
        store = JobStore(self.root / 'jobs.sqlite3')
        snapshot = {'root': str(self.root), 'folder': self.job['folder'], 'file': self.file, 'host': 'civitai.com', 'modelId': 1}
        job, _ = store.reserve(snapshot)
        folder = self.root / job['folder']; folder.mkdir()
        partial = folder / ('.' + job['id'] + '.part'); partial.write_bytes(b'ongoing')
        request = types.SimpleNamespace(match_info={'id': job['id']})
        with patch.object(m, 'authorize', return_value=({}, self.root)), patch.object(m, 'fresh_config', AsyncMock(return_value={})), patch.object(m, 'get_store', return_value=store):
            response = await m.retry_job(request)
        self.assertEqual(response.status, 400)
        self.assertEqual(partial.read_bytes(), b'ongoing')

    async def test_cancel_waits_for_worker_cleanup(self):
        from job_store import JobStore
        store = JobStore(self.root / 'jobs.sqlite3')
        job, _ = store.reserve({'root': str(self.root), 'folder': self.job['folder'], 'file': self.file, 'host': 'civitai.com', 'modelId': 1})
        cleaned = []
        async def worker():
            try:
                await asyncio.sleep(100)
            finally:
                cleaned.append(True)
        task = asyncio.create_task(worker()); await asyncio.sleep(0)
        request = types.SimpleNamespace(match_info={'id': job['id']})
        with patch.dict(m.DOWNLOAD_TASKS, {job['id']: task}), patch.object(m, 'authorize', return_value=({}, self.root)), patch.object(m, 'get_store', return_value=store):
            response = await m.cancel_job(request)
        self.assertEqual(response.status, 200)
        self.assertEqual(cleaned, [True])
        self.assertTrue(task.done())
        self.assertEqual(store.get(job['id'])['state'], 'cancelled')

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
    async def test_official_storage_host_and_token_boundary(self):
        self.assertTrue(m.allowed_download('https://b2.civitai.com/file?signature=dummy'))
        self.assertFalse(m.allowed_download('https://b2.civitai.com.attacker.invalid/file'))
        self.assertNotIn('b2.civitai.com', m.HOSTS, 'account token must not be sent to storage')

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

    async def test_missing_permission_rejected(self):
        with self.assertRaises(ValueError): m.store_oauth({'access_token':'a','refresh_token':'r','scope':['1']})

    async def test_refresh_is_serialized(self):
        cfg_path=self.root/'config.json'
        cfg_path.write_text(json.dumps({'oauth_client_id':'our-app','civitai_token':'old-access','oauth_refresh_token':'old-refresh','oauth_expires_at':0}))
        response={'access_token':'new-access','refresh_token':'new-refresh','scope':['5'],'expires_in':3600}
        with patch.object(m,'CONFIG',cfg_path),patch.object(m,'REFRESH_LOCK',asyncio.Lock()),patch.object(m.aiohttp,'ClientSession',Session),patch.object(m,'oauth_post',AsyncMock(return_value=response)) as post:
            results=await asyncio.gather(m.fresh_config(),m.fresh_config())
        self.assertEqual(post.await_count,1)
        self.assertEqual(results[0]['civitai_token'],results[1]['civitai_token'])

    async def test_oauth_refresh_rotation(self):
        cfg_path=self.root/'config.json'
        cfg_path.write_text(json.dumps({'oauth_client_id':'our-app','civitai_token':'old-access','oauth_refresh_token':'old-refresh','oauth_expires_at':0}))
        response={'access_token':'new-access','refresh_token':'new-refresh','scope':['5'],'expires_in':3600}
        with patch.object(m,'CONFIG',cfg_path),patch.object(m.aiohttp,'ClientSession',Session),patch.object(m,'oauth_post',AsyncMock(return_value=response)) as post:
            cfg=await m.fresh_config()
        self.assertEqual(cfg['oauth_refresh_token'],'new-refresh')
        self.assertEqual(post.call_args.args[2]['grant_type'],'refresh_token')


if __name__ == '__main__': unittest.main()
