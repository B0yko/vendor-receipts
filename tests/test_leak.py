"""Dataset-leak test (SPEC § Dataset policy): no product name, vendor name or registrable domain of the labelled CSV
in the agent's files (evalkit/leak.py SCAN). Skipped when the CSV is absent; the scanner itself is tested on
invented data in tests/test_l4_leak.py. A hit prints file:line:term: rename it, or, for a generic word only, add it
to tests/leak_allow.txt with a '# reason'."""
import os, sys, unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
from evalkit import labels, leak  # noqa: E402

ALLOW = os.path.join(ROOT, "tests", "leak_allow.txt")


@unittest.skipUnless(labels.exists(), "no labelled CSV (LABELS)")
class LeakTest(unittest.TestCase):
    def test_allow_list_has_reasons(self):
        self.assertEqual(leak.load_allow(ALLOW)[1], [])

    def test_no_label_terms_in_agent_files(self):
        rows = labels.load()
        self.assertGreater(len(rows), 0)
        ts = leak.terms(rows)
        self.assertGreater(len(ts), 0)
        hits = leak.scan(ROOT, ts, leak.load_allow(ALLOW)[0])
        self.assertEqual(hits, [], "label terms in the agent's files:\n" + "\n".join(hits))


if __name__ == "__main__":
    unittest.main()
