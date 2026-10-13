import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('civit_scope_test', Path(__file__).resolve().parents[1] / 'civit_oauth_format.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class ScopeTests(unittest.TestCase):
    def test_numeric_and_array_responses(self):
        for value in (5, '5', ['5'], ['1', '4']):
            self.assertEqual(m.scope_bitmask(value), 5)
    def test_invalid_responses_fail_closed(self):
        for value in (None, [], True, {}, [None], ['invalid'], '-1', -1, '５', 5.0):
            with self.assertRaises(ValueError):
                m.scope_bitmask(value)


if __name__ == '__main__': unittest.main()
