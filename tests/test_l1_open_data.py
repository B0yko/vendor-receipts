"""Lane L1 open-data tools: wayback, wayback_get, dns, crtsh, registry, github, research (offline: urlopen mocked)."""
import json, os, sys, unittest, urllib.parse
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import cache  # noqa: E402
from app.research import tools  # noqa: E402
from test_l1_tools import Base as _Base, Resp, http_error  # noqa: E402


class Base(_Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(cache, "_polite")
        self.polite = p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(tools.time, "sleep")
        self.sleep = p.start()
        self.addCleanup(p.stop)

    def route(self, fn):
        """fn(url, query dict) -> Resp | dict | list | Exception; records each Request."""
        def urlopen(req, timeout=None):
            self.requests.append(req)
            u = urllib.parse.urlsplit(req.full_url)
            a = fn(req.full_url, dict(urllib.parse.parse_qsl(u.query)))
            if isinstance(a, BaseException):
                raise a
            return a if isinstance(a, Resp) else Resp(a, url=req.full_url)
        p = mock.patch("urllib.request.urlopen", side_effect=urlopen)
        p.start()
        self.addCleanup(p.stop)


CDX = [["timestamp", "original", "statuscode"], ["20150301000000", "http://example.com/", "200"],
       ["20190710120000", "https://example.com/", "301"], ["20241105083000", "https://example.com/", "200"]]


class WaybackTests(Base):
    def test_wayback_shape_params_cache(self):
        self.route(lambda url, q: CDX)
        r = tools.wayback("example.com")
        self.assertIsNone(r["error"])
        self.assertEqual((r["first"], r["last"], r["count"]), ("2015-03-01", "2024-11-05", 3))
        self.assertEqual(r["snapshots"][0], {"timestamp": "20150301000000", "status": "200",
                                             "url": "https://web.archive.org/web/20150301000000/http://example.com/"})
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.requests[0].full_url).query))
        self.assertEqual(q, {"url": "example.com", "output": "json", "fl": "timestamp,original,statuscode",
                             "collapse": "timestamp:6", "limit": "200"})
        self.assertTrue(self.requests[0].full_url.startswith("https://web.archive.org/cdx/search/cdx?"))
        self.polite.assert_called_with("web.archive.org", 1.0)
        tools.wayback("example.com")
        self.assertEqual(len(self.requests), 1)

    def test_wayback_truncated_fetches_latest(self):
        self.route(lambda url, q: [CDX[0], ["20260101000000", "https://example.com/", "200"]]
                   if q.get("limit") == "-1" else CDX[:3])
        r = tools.wayback("example.com", limit=2)
        self.assertEqual((r["count"], r["last"], r["truncated"]), (2, "2026-01-01", True))
        self.assertEqual(len(self.requests), 2)
        self.assertNotIn("collapse", self.requests[1].full_url)

    def test_wayback_empty_and_errors(self):
        self.route(lambda url, q: Resp(b"[]"))
        r = tools.wayback("example.org")
        self.assertEqual((r["count"], r["first"], r["last"], r["error"]), (0, None, None, None))
        calls = []

        def flaky(url, q):
            calls.append(url)
            return http_error(503, {"detail": "busy"}, {"Retry-After": "3"}) if len(calls) <= 2 else CDX
        self.route(flaky)
        r = tools.wayback("example.net")
        self.assertIn("HTTP 503", r["error"])
        self.sleep.assert_called_once_with(3.0)
        self.assertEqual(tools.wayback("example.net")["count"], 3, "the error was not cached")
        self.assertIn("no url", tools.wayback("")["error"])

    def test_wayback_get_nearest(self):
        page = "<html><title>Terms</title><script>x()</script><h2>Data use</h2><p>We never sell data.</p></html>"
        final = "https://web.archive.org/web/20230102030405id_/https://example.com/terms"
        self.route(lambda url, q: Resp(page.encode(), url=final))
        r = tools.wayback_get("https://example.com/terms", "2023-01-01")
        self.assertEqual(self.requests[0].full_url, "https://web.archive.org/web/20230101id_/https://example.com/terms")
        self.assertEqual(r, {"url": "https://web.archive.org/web/20230102030405/https://example.com/terms",
                             "timestamp": "20230102030405", "title": "Terms", "text": "Data use\nWe never sell data.",
                             "error": None})
        self.assertIn("need url and timestamp", tools.wayback_get("https://example.com", "")["error"])

    def test_wayback_get_404(self):
        self.route(lambda url, q: http_error(404, {"detail": "not archived"}))
        r = tools.wayback_get("https://example.com/x", "2020")
        self.assertIn("HTTP 404", r["error"])
        self.assertEqual(r["text"], "")


def doh(answers, status=0):
    return {"Status": status, "Answer": answers} if answers else {"Status": status}


class DnsTests(Base):
    def test_dns_records_cname_and_apex_ns(self):
        table = {("app.example.com", "A"): doh([{"name": "app.example.com.", "type": 5, "data": "edge.example.net."},
                                                {"name": "edge.example.net.", "type": 1, "data": "93.184.215.14"}]),
                 ("app.example.com", "AAAA"): doh([{"type": 28, "data": "2606:2800:21f:cb07:6820:80da:af6b:8b2c"}]),
                 ("app.example.com", "NS"): doh([]),
                 ("example.com", "NS"): doh([{"type": 2, "data": "a.iana-servers.net."},
                                             {"type": 2, "data": "b.iana-servers.net."}])}
        self.route(lambda url, q: table[(q["name"], q["type"])])
        r = tools.dns("https://app.example.com/login")
        self.assertEqual(r, {"host": "app.example.com", "a": ["93.184.215.14"],
                             "aaaa": ["2606:2800:21f:cb07:6820:80da:af6b:8b2c"], "cname": ["edge.example.net"],
                             "ns": ["a.iana-servers.net", "b.iana-servers.net"], "rcode": "NOERROR", "error": None})
        self.polite.assert_called_with("dns.google", 0.2)
        n = len(self.requests)
        tools.dns("app.example.com")
        self.assertEqual(len(self.requests), n, "cached")

    def test_nxdomain_is_an_answer_servfail_an_error(self):
        self.route(lambda url, q: doh([], status=3))
        r = tools.dns("nope.example.com")
        self.assertEqual((r["rcode"], r["a"], r["error"]), ("NXDOMAIN", [], None))
        self.route(lambda url, q: doh([], status=2))
        self.assertIn("SERVFAIL", tools.dns("flaky.example.org")["error"])
        self.route(lambda url, q: doh([{"type": 1, "data": "192.0.2.1"}]))
        self.assertEqual(tools.dns("flaky.example.org")["a"], ["192.0.2.1"], "SERVFAIL was not cached")


class CrtshTests(Base):
    def test_crtsh_names_first_seen(self):
        rows = [{"name_value": "example.com\nwww.example.com", "not_before": "2019-03-01T00:00:00"},
                {"name_value": "*.example.com\napi.example.com", "not_before": "2017-06-15T12:00:00"},
                {"name_value": "API.example.com\nexample.org", "not_before": "2024-01-01T00:00:00"}]
        self.route(lambda url, q: rows)
        r = tools.crtsh("www.example.com")
        self.assertIn("q=%25.example.com", self.requests[0].full_url)
        self.assertIn("output=json", self.requests[0].full_url)
        self.assertEqual(r, {"domain": "example.com", "names": ["api.example.com", "example.com", "www.example.com"],
                             "first_seen": "2017-06-15", "count": 3, "certs": 3, "error": None})

    def test_crtsh_cap_and_bad_json(self):
        self.route(lambda url, q: [{"name_value": f"h{i}.example.com", "not_before": "2020-01-01"} for i in range(350)])
        r = tools.crtsh("example.com")
        self.assertEqual((len(r["names"]), r["count"]), (300, 350))
        self.route(lambda url, q: Resp(b"<html>502 Bad Gateway</html>"))
        self.assertIn("not JSON", tools.crtsh("example.org")["error"])


REG = {
    "pypi.org": {"info": {"version": "2.1.0"}, "releases": {
        "1.0.0": [{"upload_time_iso_8601": "2016-02-01T10:00:00Z"}], "2.1.0": [{"upload_time_iso_8601":
                                                                            "2025-09-09T09:00:00Z"}], "0.1": []}},
    "registry.npmjs.org": {"dist-tags": {"latest": "3.0.0"},
                           "time": {"created": "2014-05-05T00:00:00Z", "3.0.0": "2025-10-10T00:00:00Z"}},
    "crates.io": {"crate": {"max_stable_version": "0.9.1", "created_at": "2020-02-02T00:00:00Z"},
                  "versions": [{"num": "0.9.1", "created_at": "2025-03-03T00:00:00Z"}]},
    "packagist.org": {"package": {"time": "2012-01-01T00:00:00+00:00", "versions": {
        "dev-main": {"version": "dev-main", "time": "2026-01-01T00:00:00+00:00"},
        "v2.0.0-RC1": {"version": "v2.0.0-RC1", "time": "2025-12-01T00:00:00+00:00"},
        "v1.5.0": {"version": "v1.5.0", "time": "2025-06-01T00:00:00+00:00"},
        "v1.0.0": {"version": "v1.0.0", "time": "2013-04-04T00:00:00+00:00"}}}},
    "rubygems.org": [{"number": "5.0.0.beta", "prerelease": True, "created_at": "2025-11-01T00:00:00Z"},
                     {"number": "4.2.0", "prerelease": False, "created_at": "2025-08-08T00:00:00Z"},
                     {"number": "0.1.0", "prerelease": False, "created_at": "2011-01-01T00:00:00Z"}],
}


class RegistryTests(Base):
    def test_each_ecosystem(self):
        self.route(lambda url, q: REG[urllib.parse.urlsplit(url).hostname])
        want = {"pypi": ("2.1.0", "2025-09-09", "2016-02-01", "https://pypi.org/project/acme-widgets/"),
                "npm": ("3.0.0", "2025-10-10", "2014-05-05", "https://www.npmjs.com/package/acme-widgets"),
                "crates": ("0.9.1", "2025-03-03", "2020-02-02", "https://crates.io/crates/acme-widgets"),
                "rubygems": ("4.2.0", "2025-08-08", "2011-01-01", "https://rubygems.org/gems/acme-widgets")}
        for eco, (latest, ld, fd, url) in want.items():
            r = tools.registry("acme-widgets", eco)
            self.assertEqual((r["latest"], r["latest_date"], r["first_date"], r["url"], r["error"]),
                             (latest, ld, fd, url, None), eco)
        r = tools.registry("acme/widgets", "composer")
        self.assertEqual((r["ecosystem"], r["latest"], r["latest_date"], r["first_date"]),
                         ("packagist", "v1.5.0", "2025-06-01", "2013-04-04"))
        self.assertTrue(self.requests[-1].full_url.endswith("/packages/acme/widgets.json"))

    def test_npm_scoped_name_and_errors(self):
        self.route(lambda url, q: REG["registry.npmjs.org"])
        tools.registry("@acme/widgets", "npm")
        self.assertEqual(self.requests[0].full_url, "https://registry.npmjs.org/@acme%2Fwidgets")
        self.assertIn("unknown ecosystem", tools.registry("x", "maven")["error"])
        self.route(lambda url, q: http_error(404, {"message": "Not Found"}))
        r = tools.registry("no-such-acme", "pypi")
        self.assertIn("HTTP 404", r["error"])
        self.assertIsNone(r["latest"])


class GithubTests(Base):
    REPO = {"full_name": "acme/widgets", "pushed_at": "2025-11-01T10:00:00Z", "created_at": "2018-01-01T00:00:00Z",
            "stargazers_count": 1234, "archived": False, "license": {"spdx_id": "Apache-2.0"},
            "open_issues_count": 56, "html_url": "https://github.com/acme/widgets"}

    def test_github_shape_and_url_forms(self):
        self.route(lambda url, q: self.REPO)
        r = tools.github("https://github.com/acme/widgets/tree/main")
        self.assertEqual(r, {"repo": "acme/widgets", "pushed_at": "2025-11-01", "created_at": "2018-01-01",
                             "stars": 1234, "archived": False, "license": "Apache-2.0", "open_issues": 56,
                             "url": "https://github.com/acme/widgets", "error": None})
        self.assertEqual(self.requests[0].full_url, "https://api.github.com/repos/acme/widgets")
        self.assertIsNone(self.requests[0].get_header("Authorization"))
        for form in ("acme/widgets", "git@github.com:acme/widgets.git", "github.com/Acme/Widgets"):
            self.assertIsNone(tools.github(form)["error"], form)
        self.assertEqual(len(self.requests), 1, "all forms hit the same cache entry")
        self.assertIn("not owner/repo", tools.github("acme")["error"])

    def test_rate_limit_alert(self):
        self.route(lambda url, q: http_error(403, {"message": "API rate limit exceeded for 192.0.2.1."}))
        r = tools.github("acme/gadgets")
        self.assertIn("HTTP 403", r["error"])
        self.assertIn("github /repos/acme/gadgets HTTP 403 API rate limit exceeded", open(self.alerts).read())
        self.assertEqual(len(self.requests), 1)

    def test_token_used_when_set(self):
        self.route(lambda url, q: self.REPO)
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "gh-test"}):
            tools.github("acme/tokened")
        self.assertEqual(self.requests[0].get_header("Authorization"), "Bearer gh-test")


class MiscTests(Base):
    def test_research_stub(self):
        self.assertEqual(tools.research("anything"), {"report": "", "sources": [], "error": "not available"})

    def test_run_tool_dispatch(self):
        self.route(lambda url, q: doh([{"type": 1, "data": "192.0.2.7"}]))
        r = tools.run_tool("dns", {"host": "example.com"})
        self.assertEqual((r["a"], r["tool"], r["error"]), (["192.0.2.7"], "dns", None))
        self.assertIn("unknown args", tools.run_tool("wayback", {"domain": "example.com"})["error"])

    def test_day(self):
        self.assertEqual([tools._day(v) for v in ("2023-05-01T00:00:00Z", "20230501123456", None, "garbage", 0)],
                         ["2023-05-01", "2023-05-01", "", "", "1970-01-01"])


if __name__ == "__main__":
    unittest.main()
