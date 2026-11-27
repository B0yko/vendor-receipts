"""Tests for the generic utilities cache.py and llm.py (offline: a local fake OpenAI-compatible server only)."""
import contextlib, http.server, io, json, os, shutil, sys, tempfile, threading, unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import cache, llm  # noqa: E402


class Fake(http.server.BaseHTTPRequestHandler):
    script = []  # list of (status, headers, body) served in order; the last one repeats
    calls = []

    def log_message(self, *a):
        pass

    def _serve(self):
        n = int(self.headers.get("Content-Length") or 0)
        Fake.calls.append((self.command, self.path, json.loads(self.rfile.read(n) or b"null")))
        status, headers, body = Fake.script[min(len(Fake.calls) - 1, len(Fake.script) - 1)]
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body.encode())

    do_GET = do_POST = _serve


def ok(text):
    return (200, {"Content-Type": "application/json"}, json.dumps({"model": "m", "choices": [{"message": {"content": text}}]}))


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        p = mock.patch.object(cache, "CACHE_DIR", self.dir)
        p.start()
        self.addCleanup(p.stop)
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.base = f"http://127.0.0.1:{self.srv.server_port}"
        Fake.calls = []

    def env(self, **extra):
        e = {"LLM_ORDER": "a,b", "LLM_A_URL": self.base + "/a/v1", "LLM_A_MODEL": "ma", "LLM_A_KEY": "k",
             "LLM_B_URL": self.base + "/b/v1", "LLM_B_MODEL": "mb", "LLM_RETRIES": "1", "LLM_TIMEOUT": "5",
             "LLM_BACKOFF": "0.01"}
        e.update(extra)
        return mock.patch.dict(os.environ, e, clear=False)


class TestCache(Base):
    def test_hit_miss_replay(self):
        calls = []
        f = lambda: calls.append(1) or {"v": 1}
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cache.cached("t", {"a": 1}, f), {"v": 1})
            self.assertEqual(cache.cached("t", {"a": 1}, f), {"v": 1})
        self.assertEqual(len(calls), 1)
        self.assertIn("cache miss t", err.getvalue())
        with mock.patch.dict(os.environ, {"REPLAY": "1"}), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cache.cached("t", {"a": 1}, f), {"v": 1})
            with self.assertRaises(cache.CacheMiss):
                cache.cached("t", {"a": 2}, f)
        self.assertIn("CACHE MISS (REPLAY=1)", err.getvalue())

    def test_http_get_caches_status_not_network_errors(self):
        Fake.script = [(404, {}, "nope"), (200, {"Content-Type": "text/html"}, "<p>hi</p>")]
        with contextlib.redirect_stderr(io.StringIO()):
            r1 = cache.http_get(self.base + "/x", min_gap=0)
            r2 = cache.http_get(self.base + "/x", min_gap=0)
            dead = cache.http_get("http://127.0.0.1:9/never", timeout=1, min_gap=0)
        self.assertEqual((r1["status"], r2["status"], len(Fake.calls)), (404, 404, 1))
        self.assertIsNone(dead["status"])
        with mock.patch.dict(os.environ, {"REPLAY": "1"}), self.assertRaises(cache.CacheMiss):
            cache.http_get("http://127.0.0.1:9/never", min_gap=0)


class TestLLM(Base):
    def test_fallback_after_5xx_and_cache(self):
        Fake.script = [(500, {}, "boom"), (503, {"Retry-After": "0"}, "busy"), ok("from b")]
        with self.env(), contextlib.redirect_stderr(io.StringIO()):
            r = llm.chat([{"role": "user", "content": "hi"}])
            r2 = llm.chat([{"role": "user", "content": "hi"}])
        self.assertEqual((r["text"], r["endpoint"]), ("from b", "b"))
        self.assertEqual(r2, r)
        self.assertEqual([c[1] for c in Fake.calls], ["/a/v1/chat/completions"] * 2 + ["/b/v1/chat/completions"])
        self.assertEqual(Fake.calls[0][2]["model"], "ma")

    def test_quota_429_is_not_retried(self):
        Fake.script = [(429, {}, '{"error": "insufficient_quota"}'), ok("b")]
        with self.env(), contextlib.redirect_stderr(io.StringIO()):
            r = llm.chat([{"role": "user", "content": "q"}])
        self.assertEqual(r["endpoint"], "b")
        self.assertEqual(len(Fake.calls), 2)

    def test_all_fail_raises_and_json(self):
        Fake.script = [(401, {}, "bad key")]
        with self.env(), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(llm.LLMError):
            llm.chat([{"role": "user", "content": "z"}])
        Fake.calls, Fake.script = [], [ok('Sure: {"call": "solid", "n": 2} done')]
        with self.env(), contextlib.redirect_stderr(io.StringIO()):
            obj, _ = llm.chat_json([{"role": "user", "content": "j"}], json_mode=True)
        self.assertEqual(obj, {"call": "solid", "n": 2})
        self.assertEqual(Fake.calls[0][2]["response_format"], {"type": "json_object"})

    def test_no_endpoint(self):
        with mock.patch.dict(os.environ, {"LLM_ORDER": ""}), self.assertRaises(llm.LLMError):
            llm.chat([{"role": "user", "content": "n"}])


if __name__ == "__main__":
    unittest.main()
