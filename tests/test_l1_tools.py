"""Lane L1 tool layer: Tavily tools, fetch, run_tool (offline: urlopen and cache.http_get are mocked)."""
import email.message, http.server, io, json, os, shutil, sys, tempfile, threading, unittest, urllib.error
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import cache  # noqa: E402
from app.research import tools  # noqa: E402


class Resp:
    def __init__(self, body, status=200, url="https://api.tavily.com/x"):
        self.body, self.status, self.url = (body if isinstance(body, bytes) else json.dumps(body).encode()), status, url

    def read(self, *a):
        return self.body

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, body, headers=None):
    h = email.message.Message()
    for k, v in (headers or {}).items():
        h[k] = v
    return urllib.error.HTTPError("https://api.tavily.com/x", code, "err", h, io.BytesIO(json.dumps(body).encode()))


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.alerts = os.path.join(self.dir, "runs", "alerts.log")
        for p in (mock.patch.object(cache, "CACHE_DIR", os.path.join(self.dir, "cache")),
                  mock.patch.object(tools, "ALERTS", self.alerts),
                  mock.patch.dict(os.environ, {"TAVILY_API_KEY": "test-key"}),
                  mock.patch("sys.stderr", io.StringIO())):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("REPLAY", None)
        self.requests = []

    def script(self, *answers):
        """urlopen answers in order (a Resp, an exception or a dict); records each Request."""
        answers = list(answers)

        def urlopen(req, timeout=None):
            self.requests.append(req)
            a = answers.pop(0) if len(answers) > 1 else answers[0]
            if isinstance(a, BaseException):
                raise a
            return a if isinstance(a, Resp) else Resp(a)
        p = mock.patch("urllib.request.urlopen", side_effect=urlopen)
        p.start()
        self.addCleanup(p.stop)

    def body(self, i=-1):
        return json.loads(self.requests[i].data)


SEARCH_OK = {"query": "q", "results": [
    {"url": "https://example.com/a", "title": "A", "content": "alpha [...] beta", "score": 0.9,
     "published_date": "Tue, 11 Mar 2025 17:00:00 GMT", "raw_content": None},
    {"url": "https://example.org/b", "title": "B", "content": "gamma", "score": 0.5}]}


class TavilyTests(Base):
    def test_search_shape_request_and_cache(self):
        self.script(SEARCH_OK)
        r = tools.search("acme breach", include_domains="example.com")
        self.assertIsNone(r["error"])
        self.assertEqual(r["query"], "acme breach")
        self.assertEqual([x["url"] for x in r["results"]], ["https://example.com/a", "https://example.org/b"])
        self.assertEqual(set(r["results"][0]), {"url", "title", "content", "published_date", "score"})
        self.assertIsNone(r["results"][1]["published_date"])
        req = self.requests[0]
        self.assertEqual(req.full_url, "https://api.tavily.com/search")
        self.assertEqual(req.get_header("Authorization"), "Bearer test-key")
        b = self.body()
        self.assertEqual((b["search_depth"], b["chunks_per_source"], b["max_results"], b["topic"]),
                         ("advanced", 3, 8, "general"))
        self.assertEqual(b["include_domains"], ["example.com"])
        self.assertNotIn("time_range", b)
        self.assertEqual(tools.search("acme breach", include_domains="example.com"), r)
        self.assertEqual(len(self.requests), 1, "second identical search must come from the cache")
        cached = open(next(os.path.join(d, f) for d, _, fs in os.walk(cache.CACHE_DIR) for f in fs)).read()
        self.assertNotIn("test-key", cached)

    def test_news_defaults(self):
        self.script(SEARCH_OK)
        tools.news("acme layoffs")
        b = self.body()
        self.assertEqual((b["topic"], b["time_range"]), ("news", "year"))

    def test_error_not_cached(self):
        self.script(http_error(500, {"detail": {"error": "Internal Server Error"}}), SEARCH_OK)
        r = tools.search("q")
        self.assertEqual(r["results"], [])
        self.assertIn("HTTP 500", r["error"])
        self.assertIn("Internal Server Error", r["error"])
        r2 = tools.search("q")
        self.assertIsNone(r2["error"])
        self.assertEqual(len(r2["results"]), 2)
        self.assertEqual(len(self.requests), 2)

    def test_network_error_returns_dict(self):
        self.script(urllib.error.URLError("no route"))
        r = tools.search("q")
        self.assertIn("URLError", r["error"])

    def test_no_key(self):
        os.environ.pop("TAVILY_API_KEY")
        self.script(SEARCH_OK)
        self.assertIn("TAVILY_API_KEY", tools.search("q")["error"])
        self.assertEqual(self.requests, [])

    def test_replay_miss_is_an_error(self):
        os.environ["REPLAY"] = "1"
        self.addCleanup(os.environ.pop, "REPLAY", None)
        self.script(SEARCH_OK)
        r = tools.search("never cached")
        self.assertIn("REPLAY=1", r["error"])
        self.assertEqual(self.requests, [])

    def test_alert_on_432(self):
        msg = "This request exceeds your plan's set usage limit. Please upgrade your plan."
        self.script(http_error(432, {"detail": {"error": msg}}))
        r = tools.search("q")
        self.assertIn("HTTP 432", r["error"])
        self.assertEqual(len(self.requests), 1, "a plan limit is never retried")
        lines = open(self.alerts).read().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00 tavily search HTTP 432 This request exceeds")
        self.assertIn("ALERT", sys.stderr.getvalue())

    def test_alert_on_433_and_on_credit_message(self):
        self.script(http_error(433, {"detail": {"error": "exceeds the pay-as-you-go limit"}}))
        tools.extract(["https://example.com"])
        self.script(http_error(429, {"detail": {"error": "Out of credits for this month"}}))
        tools.search("q2")
        lines = open(self.alerts).read().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("tavily extract HTTP 433", lines[0])
        self.assertIn("tavily search HTTP 429 Out of credits", lines[1])

    def test_retry_after_honoured(self):
        busy = {"detail": {"error": "Your request has been blocked due to excessive requests."}}
        self.script(http_error(429, busy, {"Retry-After": "7"}), SEARCH_OK)
        with mock.patch.object(tools.time, "sleep") as sl:
            r = tools.search("q")
        self.assertIsNone(r["error"])
        sl.assert_called_once_with(7.0)
        self.assertEqual(len(self.requests), 2)
        self.assertFalse(os.path.exists(self.alerts), "a plain rate limit is no alert")

    def test_retry_after_capped_and_three_tries(self):
        busy = {"detail": {"error": "excessive requests"}}
        self.script(http_error(429, busy, {"Retry-After": "600"}))
        with mock.patch.object(tools.time, "sleep") as sl:
            r = tools.search("q")
        self.assertIn("HTTP 429", r["error"])
        self.assertEqual(len(self.requests), 3)
        self.assertEqual([c.args[0] for c in sl.call_args_list], [20.0, 20.0])

    def test_extract_batches_and_failed(self):
        urls = [f"https://example.com/p{i}" for i in range(25)]
        self.script({"results": [{"url": urls[0], "raw_content": "page zero"}],
                     "failed_results": [{"url": urls[1], "error": "Failed to retrieve content"}]})
        r = tools.extract(urls + [urls[0]])
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(self.body(0)["urls"]), 20)
        self.assertEqual(len(self.body(1)["urls"]), 5)
        self.assertEqual((self.body(0)["extract_depth"], self.body(0)["format"]), ("advanced", "text"))
        self.assertEqual(r["results"][0], {"url": urls[0], "text": "page zero", "published_date": None})
        self.assertEqual(r["failed"][0], {"url": urls[1], "error": "Failed to retrieve content"})
        self.assertIsNone(r["error"])

    def test_extract_single_string_and_batch_error(self):
        self.script(http_error(400, {"detail": {"error": "All URLs failed validation."}}))
        r = tools.extract("https://example.com")
        self.assertEqual(self.body()["urls"], ["https://example.com"])
        self.assertEqual(r["results"], [])
        self.assertEqual(r["failed"][0]["url"], "https://example.com")
        self.assertIn("HTTP 400", r["error"])
        self.assertIn("no urls", tools.extract([])["error"])

    def test_usage_not_cached(self):
        self.script({"key": {"usage": 10}, "account": {"current_plan": "Researcher"}})
        self.assertEqual(tools.usage()["key"], {"usage": 10})
        self.assertIsNone(tools.usage()["error"])
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[0].get_method(), "GET")
        self.assertTrue(self.requests[0].full_url.endswith("/usage"))


PAGE = """<!doctype html><html><head><title> Acme  Widgets </title>
<meta name="generator" content="WordPress 6.4"><style>.x{color:red}</style>
<script>var copyright = "Copyright 1999";</script></head>
<body><nav><a href="/about">About us</a> <a href="https://example.org/x#frag">Partner</a>
<a href="mailto:a@example.com">Mail</a><a href="javascript:void(0)">JS</a><a href="/about">dup</a>
<a href="/logo"><img alt="Acme logo"></a><a href="/aria" aria-label="Aria label"></a></nav>
<h1>Widgets   for
 teams</h1><p>We ship <b>fast</b> widgets.</p>
<ul><li>One</li><li>Two &amp; three</li></ul>
<noscript>Enable JS</noscript><svg><title>icon</title><text>svgtext</text></svg>
<table><tr><td>a</td><td>b</td></tr></table>
<footer>&copy; 2015&ndash;2024 Acme GmbH. Copyright Acme Inc. 2019</footer></body></html>"""


class FetchTests(Base):
    def http(self, text, status=200, error=None, final_url="https://example.com/"):
        p = mock.patch.object(cache, "http_get", return_value={"url": "x", "status": status, "final_url": final_url,
                                                                 "text": text, "error": error})
        m = p.start()
        self.addCleanup(p.stop)
        return m

    def test_html_to_text(self):
        m = self.http(PAGE)
        r = tools.fetch("example.com")
        m.assert_called_once_with("https://example.com")
        self.assertIsNone(r["error"])
        self.assertEqual((r["status"], r["final_url"], r["title"]), (200, "https://example.com/", "Acme Widgets"))
        lines = r["text"].splitlines()
        for want in ("Widgets for teams", "We ship fast widgets.", "One", "Two & three", "a b"):
            self.assertIn(want, lines)
        for gone in ("color:red", "var copyright", "Enable JS", "svgtext", "icon"):
            self.assertNotIn(gone, r["text"])
        self.assertEqual(r["links"], [{"url": "https://example.com/about", "text": "About us"},
                                      {"url": "https://example.org/x", "text": "Partner"},
                                      {"url": "https://example.com/logo", "text": "Acme logo"},
                                      {"url": "https://example.com/aria", "text": "Aria label"}])
        self.assertEqual(r["meta"], {"generator": "WordPress 6.4", "copyright_years": [2015, 2019, 2024]})

    def test_caps(self):
        big = "<p>" + "x " * 60_000 + "</p>" + "".join(f'<a href="/p{i}">p{i}</a>' for i in range(500))
        self.http(big)
        r = tools.fetch("https://example.com")
        self.assertEqual(len(r["text"]), tools.TEXT_CAP)
        self.assertEqual(len(r["links"]), tools.LINK_CAP)

    def test_http_error_passthrough(self):
        self.http("", status=404, error="HTTP 404")
        r = tools.fetch("https://example.com/missing")
        self.assertEqual((r["status"], r["error"], r["text"]), (404, "HTTP 404", ""))

    def test_bad_url_and_raising_http(self):
        self.assertIn("not an http", tools.fetch("ftp://example.com")["error"])
        with mock.patch.object(cache, "http_get", side_effect=cache.CacheMiss("http_get abc not cached and REPLAY=1")):
            self.assertIn("REPLAY=1", tools.fetch("https://example.com")["error"])

    def test_fetch_through_real_http_get(self):
        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write("<title>Example Domain</title><p>Hello © 2023</p>".encode())
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        r = tools.fetch(f"http://127.0.0.1:{srv.server_port}/")
        self.assertEqual((r["title"], r["text"], r["meta"]["copyright_years"], r["error"]),
                         ("Example Domain", "Hello © 2023", [2023], None))


class RunToolTests(Base):
    def test_registry_shape(self):
        for name in ("search", "news", "extract", "fetch", "wayback", "wayback_get", "dns", "crtsh", "registry",
                     "github"):
            t = tools.TOOLS[name]
            self.assertTrue(callable(t["fn"]) and t["doc"] and isinstance(t["args"], list), name)
            self.assertNotIn("\n", t["doc"])
        self.assertIn("- search(", tools.tool_docs())

    def test_unknown_tool_and_args(self):
        r = tools.run_tool("shell", {"cmd": "ls"})
        self.assertIn("unknown tool", r["error"])
        self.assertEqual((r["tool"], r["args"]), ("shell", {"cmd": "ls"}))
        with mock.patch.dict(tools.TOOLS["search"], fn=mock.Mock()) as _:
            r = tools.run_tool("search", {"query": "q", "api_key": "x"})
            self.assertIn("unknown args ['api_key']", r["error"])
            tools.TOOLS["search"]["fn"].assert_not_called()
        self.assertIn("missing args ['query']", tools.run_tool("search", {})["error"])
        self.assertIn("args must be an object", tools.run_tool("search", ["q"])["error"])

    def test_ok_and_raising_fn(self):
        self.script(SEARCH_OK)
        r = tools.run_tool("search", {"query": "q", "max_results": 3})
        self.assertIsNone(r["error"])
        self.assertEqual((r["tool"], r["args"], len(r["results"])), ("search", {"query": "q", "max_results": 3}, 2))
        self.assertIsInstance(r["seconds"], float)
        self.assertEqual(self.body()["max_results"], 3)
        with mock.patch.dict(tools.TOOLS["fetch"], fn=mock.Mock(side_effect=ValueError("boom"))):
            r = tools.run_tool("fetch", {"url": "https://example.com"})
        self.assertEqual((r["error"], r["tool"]), ("ValueError: boom", "fetch"))


if __name__ == "__main__":
    unittest.main()
