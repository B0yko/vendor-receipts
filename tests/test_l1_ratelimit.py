"""Lane L1-3: the Tavily rate limiter shared across processes, retries within the budget, the credits log.
Offline and instant: urlopen mocked, time.time and time.sleep replaced by a fake clock."""
import datetime, json, os, subprocess, sys, time, unittest
from email.utils import formatdate
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))
from app.research import tools  # noqa: E402
from test_l1_tools import Base as _Base, SEARCH_OK, http_error  # noqa: E402

REAL_TIME = time.time


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now, self.sleeps = now, []

    def time(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


class Base(_Base):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        for p in (mock.patch.object(tools.time, "time", self.clock.time),
                  mock.patch.object(tools.time, "sleep", self.clock.sleep)):
            p.start()
            self.addCleanup(p.stop)
        self.rl = os.environ["TAVILY_RL_DIR"]

    def credits(self):
        if not os.path.exists(tools.CREDITS_LOG):
            return []
        with open(tools.CREDITS_LOG) as f:
            return [json.loads(x) for x in f]


class BucketTests(Base):
    def test_91st_call_in_a_window_blocks(self):
        b = tools.Bucket(self.rl, 90)
        budget = tools._Budget()
        for i in range(90):
            b.acquire(budget)
            self.clock.now += 0.1          # 90 calls spread over 9 s
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(b.in_window(), 90)
        self.assertAlmostEqual(b.try_take(), 51.0, places=3)   # the first call leaves the window at +60 s
        b.acquire(budget)
        self.assertEqual(len(self.clock.sleeps), 1)
        self.assertTrue(51.0 <= self.clock.sleeps[0] <= 51.5, self.clock.sleeps)
        self.assertEqual(b.in_window(), 90)

    def test_two_readers_share_one_state(self):
        a, b = tools.Bucket(self.rl, 90), tools.Bucket(self.rl, 90)
        for _ in range(45):
            self.assertEqual(a.try_take(), 0.0)
            self.assertEqual(b.try_take(), 0.0)
        self.assertGreater(a.try_take(), 0)
        self.assertGreater(b.try_take(), 0)
        self.clock.now += 60.01
        self.assertEqual(b.try_take(), 0.0)
        self.assertEqual(a.in_window(), 1)

    def test_another_process_takes_from_the_same_bucket(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); from app.research import tools; "
                "b = tools.Bucket(sys.argv[2], 90); [b.try_take() for _ in range(3)]")
        subprocess.run([sys.executable, "-c", code, os.path.join(HERE, ".."), self.rl], check=True, timeout=20)
        with mock.patch.object(tools.time, "time", REAL_TIME):
            self.assertEqual(tools.Bucket(self.rl, 90).in_window(), 3)

    def test_limiter_gives_up_at_the_deadline(self):
        b = tools.Bucket(self.rl, 2)
        b.try_take(), b.try_take()
        with self.assertRaisesRegex(tools.ToolError, "rate limiter"):
            b.acquire(tools._Budget(deadline=self.clock.now + 10))
        self.assertEqual(self.clock.sleeps, [])

    def test_search_waits_for_a_slot(self):
        os.environ["TAVILY_RPM"] = "1"
        self.script(SEARCH_OK)
        self.assertIsNone(tools.search("one")["error"])
        self.assertIsNone(tools.search("two")["error"])
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(self.clock.sleeps), 1)
        self.assertTrue(60 <= self.clock.sleeps[0] <= 60.5)
        r = tools.search("three", deadline=self.clock.now + 5)
        self.assertIn("rate limiter", r["error"])
        self.assertEqual(len(self.requests), 2)

    def test_cache_hit_takes_no_slot_and_logs_nothing(self):
        self.script(SEARCH_OK)
        tools.search("q")
        tools.search("q")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(tools.Bucket(self.rl).in_window(), 1)
        self.assertEqual(len(self.credits()), 1)


class RetryTests(Base):
    def test_retry_after_honoured_as_given(self):
        self.script(http_error(429, {"detail": {"error": "excessive requests"}}, {"Retry-After": "45"}), SEARCH_OK)
        r = tools.search("q")
        self.assertIsNone(r["error"])
        self.assertEqual(self.clock.sleeps, [45.0])
        self.assertEqual(tools.Bucket(self.rl).in_window(), 2, "the retry took a slot too")

    def test_http_date_retry_after_on_5xx(self):
        self.script(http_error(503, {"message": "plan service unavailable"},
                               {"Retry-After": formatdate(self.clock.now + 12, usegmt=True)}), SEARCH_OK)
        self.assertIsNone(tools.search("q")["error"])
        self.assertEqual(self.clock.sleeps, [12.0])

    def test_expired_or_imminent_deadline(self):
        self.script({"key": {}})
        self.assertIn("deadline", tools.usage(deadline=self.clock.now)["error"])
        self.assertEqual(self.requests, [])
        self.assertEqual(tools.Bucket().in_window(), 0)
        self.assertIsNone(tools.usage(deadline=self.clock.now + 0.25)["error"])
        self.assertEqual(tools.urllib.request.urlopen.call_args.kwargs["timeout"], 0.25)
        self.assertEqual([(r["endpoint"], r["credits"]) for r in self.credits()], [("usage", 0)])

    def test_six_retries_then_error(self):
        self.script(*[http_error(503, {"detail": {"error": "Service Unavailable"}}) for _ in range(7)])
        r = tools.search("q")
        self.assertEqual(len(self.requests), 7)
        self.assertRegex(r["error"], r"HTTP 503 Service Unavailable after 7 tries")
        waits = self.clock.sleeps
        self.assertEqual(len(waits), 6)
        for w, base in zip(waits, (2, 4, 8, 16, 16, 16)):
            self.assertTrue(base <= w <= base + 1, waits)
        self.assertLessEqual(sum(waits), tools.MAX_WAIT)
        self.assertEqual(self.credits(), [])

    def test_retry_stops_at_the_deadline(self):
        self.script(http_error(429, {"detail": {"error": "excessive requests"}}, {"Retry-After": "30"}))
        r = tools.search("q", deadline=self.clock.now + 40)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.clock.sleeps, [30.0])
        self.assertIn("past the deadline", r["error"])

    def test_no_deadline_caps_total_wait(self):
        self.script(http_error(429, {"detail": {"error": "excessive requests"}}, {"Retry-After": "40"}))
        r = tools.search("q")
        self.assertEqual(self.clock.sleeps, [40.0, 40.0])
        self.assertEqual(len(self.requests), 3)
        self.assertIn("HTTP 429", r["error"])

    def test_plan_limit_429_not_retried(self):
        self.script(http_error(429, {"detail": {"error": "This request exceeds your plan's set usage limit."}}))
        r = tools.search("q")
        self.assertEqual((len(self.requests), self.clock.sleeps), (1, []))
        self.assertIn("HTTP 429", r["error"])
        with open(self.alerts) as f:
            self.assertIn("tavily search HTTP 429 This request exceeds", f.read())

    def test_run_tool_passes_deadline(self):
        self.script(http_error(429, {"detail": {"error": "excessive requests"}}, {"Retry-After": "30"}))
        r = tools.run_tool("search", {"query": "q"}, deadline=self.clock.now + 10)
        self.assertEqual(len(self.requests), 1)
        self.assertIn("past the deadline", r["error"])
        self.assertNotIn("deadline", r["args"])


class CreditTests(Base):
    def test_credit_line_per_call(self):
        self.script(SEARCH_OK)
        tools.search("adv")
        tools.search("basic", depth="basic")
        urls = [f"https://example.com/p{i}" for i in range(7)]
        self.script({"results": [{"url": u, "raw_content": "x"} for u in urls[:5]],
                     "failed_results": [{"url": urls[5], "error": "x"}]})
        tools.extract(urls[:6])
        self.script({"results": [{"url": urls[6], "raw_content": "x"}], "usage": {"credits": 1}})
        tools.extract(urls[6], depth="basic")
        self.assertTrue(self.body()["include_usage"])
        c = self.credits()
        self.assertEqual([(x["endpoint"], x["credits"], x["depth"], x["urls_ok"]) for x in c],
                         [("search", 2, "advanced", 2), ("search", 1, "basic", 2),
                          ("extract", 2.0, "advanced", 5), ("extract", 1, "basic", 1)])
        self.assertTrue(all(set(x) == {"at", "endpoint", "credits", "depth", "urls_ok"} for x in c))

    def test_failed_call_logs_nothing(self):
        self.script(http_error(400, {"detail": {"error": "bad"}}))
        tools.search("q")
        self.assertEqual(self.credits(), [])

    def test_report(self):
        os.makedirs(os.path.dirname(tools.CREDITS_LOG), exist_ok=True)
        with open(tools.CREDITS_LOG, "w") as f:
            for at, c in (("2027-01-15T07:00:00+00:00", 2), ("2027-01-15T07:55:00+00:00", 2),
                          ("2027-01-15T07:58:00+00:00", 0.4)):
                f.write(json.dumps({"at": at, "endpoint": "search", "credits": c, "depth": "advanced", "urls_ok": 1}) + "\n")
            f.write("garbage\n")
        now = datetime.datetime.fromisoformat("2027-01-15T08:00:00+00:00").timestamp()
        out = tools.credits_report(now=now)
        self.assertIn("tavily credits: 4.4 in 3 calls since 2027-01-15T07:00:00+00:00", out)
        self.assertIn("last 10 min: 2.4 credits in 2 calls (0.2 credits/min)", out)
        self.assertIn("rate limiter: 0/90 calls", out)

    def test_cli(self):
        env = {**os.environ, "TAVILY_CREDITS_LOG": os.path.join(self.dir, "none.jsonl")}
        r = subprocess.run([sys.executable, "-m", "app.research.tools", "credits"], cwd=os.path.join(HERE, ".."),
                           env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("tavily credits: 0 in 0 calls", r.stdout)


if __name__ == "__main__":
    unittest.main()


class CorruptStateTest(unittest.TestCase):
    def test_empty_or_torn_state_file_starts_a_fresh_window(self):
        import tempfile
        d = tempfile.mkdtemp()
        b = tools.Bucket(dir=d, rpm=5)
        for junk in ("", "{", '{"a": 1}', '[1, "x"]'):
            with open(os.path.join(d, "bucket.json"), "w") as f:
                f.write(junk)
            self.assertEqual(b.try_take(), 0.0)
