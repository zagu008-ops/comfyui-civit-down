import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.script = self.root / 'setup_config.py'
        shutil.copy2(Path(__file__).resolve().parents[1] / 'setup_config.py', self.script)
        self.loras = self.root / '恋上换装娃娃'
        self.loras.mkdir()
    def tearDown(self): self.tmp.cleanup()
    def run_setup(self, origin='http://192.168.1.10:8000'):
        return subprocess.run([sys.executable, str(self.script), '--origin', origin,
                               '--lora-root', str(self.loras.resolve())], capture_output=True, text=True)
    def test_generate_and_preserve_private_credentials(self):
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        p = self.root / 'config.json'
        cfg = json.loads(p.read_text(encoding='utf-8'))
        self.assertGreaterEqual(len(cfg['pairing_key']), 32)
        self.assertNotIn(cfg['pairing_key'], result.stdout)
        cfg['civitai_token'] = 'private-token'
        p.write_text(json.dumps(cfg), encoding='utf-8')
        result = self.run_setup('https://tavern.example.com')
        saved = json.loads(p.read_text(encoding='utf-8'))
        self.assertEqual(saved['civitai_token'], 'private-token')
        self.assertEqual(saved['pairing_key'], cfg['pairing_key'])
        self.assertEqual(saved['allowed_origins'], ['https://tavern.example.com'])
        self.assertNotIn('private-token', result.stdout)
    def test_reject_non_origin_url(self):
        result = self.run_setup('https://tavern.example.com/path?secret=value')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'config.json').exists())


if __name__ == '__main__': unittest.main()
