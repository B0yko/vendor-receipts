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
        self.env = mock.patch.dict(os.environ, {"PI_BIN": b, "MODEL_ACCOUNTS": "default,acct3", "RECEIPTS_FAKE": "", "MODEL_THINKING": "xhigh", "MODEL_THINKING_RESEARCH": "high", "MODEL_SLOTS_DIR": os.path.join(self.d, "slots0"),
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

    def test_research_steps_think_high_judge_xhigh(self):
        self.assertEqual(model._thinking("intake"), "high")
        self.assertEqual(model._thinking("round_retry"), "high")
        self.assertEqual(model._thinking("plan"), "high")
        self.assertEqual(model._thinking("judge"), "xhigh")
        self.assertEqual(model._thinking("disconfirm"), "xhigh")

    def test_never_acct2(self):
        with mock.patch.dict(os.environ, {"MODEL_ACCOUNTS": "acct2"}):
            self.assertEqual(model._accounts(), ["default"])

    def test_global_slots_serialize_across_threads(self):
        import threading, time as _t
        from app import model as m
        held, peak, lock = [0], [0], threading.Lock()
        orig = m._global_slot

        def work():
            with orig():
                with lock:
                    held[0] += 1
                    peak[0] = max(peak[0], held[0])
                _t.sleep(0.05)
                with lock:
                    held[0] -= 1
        with mock.patch.dict(os.environ, {"MODEL_GLOBAL_CONC": "2", "MODEL_SLOTS_DIR": os.path.join(self.d, "slots")}):
            ts = [threading.Thread(target=work) for _ in range(6)]
            [t.start() for t in ts]
            [t.join() for t in ts]
        self.assertEqual(peak[0], 2)

    def test_fake_hook(self):
        model.set_fake("judge", lambda s, u: {"x": 1})
        try:
            self.assertEqual(model.ask_json("s", "u", "judge"), {"x": 1})
        finally:
            model.clear_fakes()


if __name__ == "__main__":
    unittest.main()
