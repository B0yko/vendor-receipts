"""app/model.py against a fake pi binary (offline)."""
import json, os, stat, sys, tempfile, unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import cache  # noqa: E402
from app import model  # noqa: E402

FAKE = """#!/bin/sh
cat > /dev/null
if [ "$PI_CLAUDE_TETHER_ACCOUNT" = "default" ] && [ -n "$FAIL_DEFAULT" ]; then echo "usage limit reached" >&2; exit 1; fi
echo '```json'
echo '{"ok": true, "acct": "'$PI_CLAUDE_TETHER_ACCOUNT'"}'
echo '```'
"""


class ModelTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        b = os.path.join(self.d, "pi")
        with open(b, "w") as f:
            f.write(FAKE)
        os.chmod(b, os.stat(b).st_mode | stat.S_IEXEC)
        self.env = mock.patch.dict(os.environ, {"PI_BIN": b, "MODEL_ACCOUNTS": "default,acct3", "RECEIPTS_FAKE": "",
                                                "REPLAY": ""})
        self.env.start()
        self.cd = mock.patch.object(cache, "CACHE_DIR", os.path.join(self.d, "c"))
        self.cd.start()
        self.cwd = os.getcwd()
        os.chdir(self.d)

    def tearDown(self):
        os.chdir(self.cwd)
        self.env.stop()
        self.cd.stop()

    def test_parse_fenced_json_and_cache(self):
        a = model.ask_json("sys", "user one", "t")
        self.assertTrue(a["ok"])
        self.assertEqual(model.ask_json("sys", "user one", "t"), a)  # cached
        with open("runs/model_calls.jsonl") as f:
            self.assertEqual(len(f.readlines()), 1)

    def test_limit_fails_over_to_next_account(self):
        with mock.patch.dict(os.environ, {"FAIL_DEFAULT": "1", "MODEL_ACCOUNTS": "default,acct3"}):
            seen = {model.ask_json("sys", f"u{i}", "t")["acct"] for i in range(3)}
        self.assertEqual(seen, {"acct3"})
        with open("runs/alerts.log") as f:
            self.assertIn("limit", f.read())

    def test_never_acct2(self):
        with mock.patch.dict(os.environ, {"MODEL_ACCOUNTS": "acct2"}):
            self.assertEqual(model._accounts(), ["default"])

    def test_fake_hook(self):
        model.set_fake("judge", lambda s, u: {"x": 1})
        try:
            self.assertEqual(model.ask_json("s", "u", "judge"), {"x": 1})
        finally:
            model.clear_fakes()


if __name__ == "__main__":
    unittest.main()
