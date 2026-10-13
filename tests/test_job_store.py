import importlib.util
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

spec = importlib.util.spec_from_file_location('job_store_test', Path(__file__).resolve().parents[1] / 'job_store.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state/jobs.sqlite3'
        self.store = m.JobStore(self.path)
        self.snapshot = {'root': str(Path(self.tmp.name)), 'folder': '作品', 'modelId': '1', 'host': 'civitai.com',
                         'file': {'sha256': 'a'*64, 'name': 'model.safetensors', 'bytes': 5, 'versionId': 2, 'fileId': 3}}
    def tearDown(self): self.tmp.cleanup()
    def test_reservation_is_atomic_and_idempotent(self):
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: self.store.reserve(self.snapshot), range(2)))
        self.assertEqual(results[0][0]['id'], results[1][0]['id'])
        self.assertEqual(sum(created for _, created in results), 1)
    def test_reboot_interrupts_active_and_retains_completed(self):
        job, _ = self.store.reserve(self.snapshot)
        reopened = m.JobStore(self.path)
        self.assertEqual(reopened.get(job['id'])['state'], 'interrupted')
        job = reopened.retry(job['id']);job.update(state='done', path='作品/model.safetensors');reopened.save(job)
        self.assertEqual(m.JobStore(self.path).get(job['id'])['state'], 'done')
    def test_other_job_blocked_until_terminal_state(self):
        job, _ = self.store.reserve(self.snapshot)
        other = dict(self.snapshot, folder='另一个作品')
        with self.assertRaises(ValueError): self.store.reserve(other)
        job['state'] = 'failed';self.store.save(job)
        self.assertTrue(self.store.reserve(other)[1])
        with self.assertRaises(ValueError): self.store.retry(job['id'])
    def test_snapshot_does_not_contain_credentials(self):
        job, _ = self.store.reserve(self.snapshot)
        self.assertNotIn('token', self.store.snapshot(job['id']))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__': unittest.main()
