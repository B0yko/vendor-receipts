"""The leak scanner (evalkit/leak.py) on an invented tree: terms, whole-word matching, allow list, file:line:term."""
import os, shutil, sys, tempfile, unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
from evalkit import leak  # noqa: E402

ROWS = [{"product": "Acme Widgets", "vendor": "Acmecorp GmbH", "url": "https://www.acmewidgets.example.org/x"},
        {"product": "Zed", "vendor": "Bolt Labs Inc.", "url": "bolt.co.uk"}]


class LeakScanTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)

    def put(self, rel, text, mode="w"):
        p = os.path.join(self.d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, mode) as f:
            f.write(text)

    def test_terms(self):
        ts = leak.terms(ROWS)
        self.assertIn("acme widgets", ts)
        self.assertIn("acmecorp gmbh", ts)
        self.assertIn("acmecorp", ts)            # legal suffix stripped
        self.assertIn("example.org", ts)         # registrable domain
        self.assertIn("bolt labs", ts)
        self.assertIn("bolt.co.uk", ts)          # co.uk-style suffix keeps three labels
        self.assertNotIn("zed", ts)              # shorter than 4 characters

    def test_scan_whole_word_case_insensitive(self):
        self.put("app/prompts.py", "x = 1\nWe compare ACME  Widgets here\nacmecorporation is fine\n")
        self.put("app/sub/notes.md", "see https://acmewidgets.example.org/a\n")
        self.put("tests/fixtures/fake/judge.json", '{"vendor": "Bolt Labs"}\n')
        self.put("README.md", "AcmeCorp\n")
        self.put("app/__pycache__/x.pyc", "Acmecorp")      # skipped dir
        self.put("app/blob.bin", b"\0Acmecorp", "wb")      # binary skipped
        self.put("evalkit/labels.py", "Acmecorp\n")         # not an agent path
        hits = leak.scan(self.d, leak.terms(ROWS))
        self.assertEqual(sorted(hits), sorted([
            "app/prompts.py:2:acme widgets",
            "app/sub/notes.md:1:example.org",
            "tests/fixtures/fake/judge.json:1:bolt labs",
            "README.md:1:acmecorp"]))

    def test_allow_list(self):
        self.put("app/a.py", "bolt labs\n")
        self.put("allow.txt", "# header\n\nbolt labs  # generic in this test\nnoreason\n")
        allowed, problems = leak.load_allow(os.path.join(self.d, "allow.txt"))
        self.assertEqual(allowed, {"bolt labs", "noreason"})
        self.assertEqual(len(problems), 1)
        self.assertIn("noreason", problems[0])
        self.assertEqual(leak.scan(self.d, leak.terms(ROWS), allowed), [])

    def test_clean_tree(self):
        self.put("app/a.py", "print('hello')\n")
        self.assertEqual(leak.scan(self.d, leak.terms(ROWS)), [])
        self.assertEqual(leak.scan(self.d, []), [])


if __name__ == "__main__":
    unittest.main()
