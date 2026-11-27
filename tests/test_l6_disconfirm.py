"""Lane L6: disconfirmation (app/check/disconfirm.py). Offline: model fakes for disconfirm_plan, disconfirm and
says_it; the search tool is patched (dc._run_tool). Vendor on the reserved .example TLD (RFC 2606)."""
import copy, json, os, sys, threading, time, unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import ledger, model  # noqa: E402
from app.check import disconfirm as dc  # noqa: E402
from app.check import receipts as rc  # noqa: E402

FINE_URL = "https://news.example.org/acme-fined"
FINE_PAGE = ("The data protection authority fined Acme Analytics 2 million euros in March 2025 for tracking users "
             "without consent.")
APPEAL_URL = "https://court.example.org/acme-appeal"
APPEAL_TEXT = ("On 12 June 2026 the court overturned the fine against Acme Analytics, ruling that the consent "
               "banner met the legal standard.")


def ev(i, url, quote, **kw):
    e = {"id": f"e{i}", "category": 9, "text": "Acme Analytics was fined for tracking without consent.", "url": url,
         "quote": quote, "date": "2025-03", "author_type": "major_press_research", "severity": "critical",
         "polarity": "negative", "promise_link": "direct", "hypothesis": "regulator action", "source_title": "News",
         "round": 2}
    e.update(kw)
    return e


def research(evidence, sources):
    r = ledger.new_research("https://acme.example/")
    r["identity"].update({"product": "Acme Analytics", "vendor": "Acme", "legal_entity": "Acme Data GmbH",
                          "domains": ["acme.example"], "country": "DE"})
    r["evidence"], r["sources"] = evidence, sources
    r["rounds"] = [{"n": 1}, {"n": 2}]
    r["stats"] = {"tool_calls": 10, "llm_calls": 3, "capped": False}
    return r


def src(text, title="News"):
    return {"text": text, "kind": "extract", "title": title, "published_date": "", "fetched_at": "2026-11-27T15:00:00Z"}


def targets_in(user):
    out, on = [], False
    for line in user.splitlines():
        if line in ("TARGETS:", "RESULTS:"):
            on = line == "TARGETS:"
        elif on and line.startswith("{"):
            out.append(json.loads(line)["target"])
    return out


def results_in(user):
    on, out = False, []
    for line in user.splitlines():
        if line == "RESULTS:":
            on = True
        elif on and line.startswith("{"):
            out.append(json.loads(line))
    return out


class Base(unittest.TestCase):
    def setUp(self):
        model.clear_fakes()
        self.calls = {"plan": [], "check": [], "tools": []}
        self.plan_answer = None   # None -> two queries per target
        self.check_answer = {"evidence": [], "outcomes": []}
        self.search_results = {}  # query -> [results]
        self.lock = threading.Lock()
        model.set_fake("says_it", lambda s, u: {"results": [{"id": json.loads(x)["id"], "supports": True, "why": ""}
                                                            for x in u.splitlines() if x.startswith("{")]})
        model.set_fake("disconfirm_plan", self._plan)
        model.set_fake("disconfirm", self._check)
        self.tool = mock.patch.object(dc, "_run_tool", self._tool)
        self.tool.start()

    def tearDown(self):
        self.tool.stop()
        model.clear_fakes()

    def _plan(self, system, user):
        self.calls["plan"].append(user)
        if self.plan_answer is not None:
            return self.plan_answer
        return {"queries": [{"target": t, "tool": tool, "query": f"{t} {tool} query"}
                            for t in targets_in(user) for tool in ("news", "search")]}

    def _check(self, system, user):
        self.calls["check"].append(user)
        return self.check_answer

    def _tool(self, name, args):
        with self.lock:
            self.calls["tools"].append((name, dict(args)))
        return {"query": args["query"], "results": self.search_results.get(args["query"], []), "error": None}

    def fined(self, **kw):
        e = ev(7, FINE_URL, "fined Acme Analytics 2 million euros in March 2025", **kw)
        res = research([e], {FINE_URL: src(FINE_PAGE)})
        return res, rc.check(res)


class TestRefuted(Base):
    def test_refuted_target_adds_checked_evidence(self):
        res, recs = self.fined()
        before = copy.deepcopy(res)
        self.search_results["e7 news query"] = [{"url": APPEAL_URL, "title": "Court overturns Acme fine",
                                                 "content": APPEAL_TEXT, "published_date": "2026-06-12",
                                                 "score": 0.9}]
        self.check_answer = {
            "evidence": [{"id": "n1", "target": "e7", "category": 9, "text": "A court overturned the fine.",
                          "url": APPEAL_URL, "quote": "the court overturned the fine against Acme Analytics",
                          "date": "2026-06-12", "author_type": "authority", "severity": "critical",
                          "polarity": "positive", "promise_link": "direct",
                          "hypothesis": "disconfirmation of e7: was the fine overturned?"}],
            "outcomes": [{"target": "e7", "outcome": "refuted", "why": "the fine was overturned on appeal",
                          "evidence": ["n1"]}]}
        r, out, rec = dc.disconfirm(res, recs, deadline=time.time() + 1200)
        self.assertEqual(res, before, "the input research is never mutated")
        self.assertEqual(rec["mode"], "negatives")
        self.assertEqual([t["target"] for t in rec["targets"]], ["e7"])
        self.assertEqual(len(self.calls["tools"]), 2)
        self.assertIn(("search", {"query": "e7 news query", "max_results": dc.MAX_RESULTS, "topic": "news"}),
                      self.calls["tools"])
        self.assertEqual([q["results"] for q in rec["queries"]], [1, 0])
        self.assertEqual(r["sources"][APPEAL_URL]["kind"], "search")
        self.assertEqual(results_in(self.calls["check"][0])[0]["url"], APPEAL_URL)
        new = r["evidence"][-1]
        self.assertEqual(new["id"], "e8", "ids continue the research's numbering")
        self.assertEqual(new["round"], 3)
        self.assertTrue(new["hypothesis"].startswith("disconfirmation of e7: "), new["hypothesis"])
        self.assertEqual(ledger.validate_evidence(new), [])
        self.assertEqual(len(out), 2)
        self.assertEqual(out[-1]["id"], "e8")
        self.assertTrue(out[-1]["kept"], out[-1]["why"])
        self.assertTrue(out[-1]["independent"])
        o = rec["outcomes"][0]
        self.assertEqual((o["target"], o["outcome"], o["evidence"], o["kept"]), ("e7", "refuted", ["e8"], ["e8"]))
        self.assertEqual(r["disconfirmation"], rec)
        self.assertEqual(r["stats"]["tool_calls"], 12)
        self.assertEqual(r["stats"]["llm_calls"], 5)
        self.assertEqual(rec["new_evidence"], 1)
        self.assertEqual(rec["kept"], 1)

    def test_refuted_without_a_kept_receipt_is_recorded_as_survived(self):
        res, recs = self.fined()
        self.search_results["e7 news query"] = [{"url": APPEAL_URL, "title": "Court", "content": APPEAL_TEXT}]
        self.check_answer = {
            "evidence": [{"id": "n1", "target": "e7", "url": APPEAL_URL, "category": 9, "author_type": "authority",
                          "severity": "critical", "polarity": "positive", "text": "Overturned.",
                          "quote": "the fine against Acme Analytics was cancelled by the ministry"}],
            "outcomes": [{"target": "e7", "outcome": "refuted", "why": "overturned", "evidence": ["n1"]}]}
        r, out, rec = dc.disconfirm(res, recs)
        o = rec["outcomes"][0]
        self.assertEqual(o["outcome"], "survived")
        self.assertEqual(o["model_outcome"], "refuted")
        self.assertIn("no kept receipt", o["why"])
        self.assertFalse(out[-1]["kept"])
        self.assertEqual(out[-1]["why"], "quote not found on the page")
        self.assertEqual(o["kept"], [])


class TestSurvived(Base):
    def test_survived_target(self):
        res, recs = self.fined()
        self.search_results["e7 search query"] = [{"url": "https://blog.example.net/acme", "title": "Acme blog",
                                                   "content": "Acme Analytics paid the fine and changed its banner."}]
        self.check_answer = {"evidence": [], "outcomes": [{"target": "e7", "outcome": "survived",
                                                           "why": "no appeal or reversal found", "evidence": []}]}
        r, out, rec = dc.disconfirm(res, recs)
        self.assertEqual(rec["outcomes"], [{"target": "e7", "outcome": "survived", "why": "no appeal or reversal found",
                                            "evidence": [], "kept": [], "model_outcome": "survived"}])
        self.assertEqual(out, recs)
        self.assertEqual(r["evidence"], res["evidence"])
        self.assertIn("https://blog.example.net/acme", r["sources"])

    def test_missing_outcome_is_no_verdict(self):
        res, recs = self.fined()
        self.search_results["e7 news query"] = [{"url": APPEAL_URL, "title": "Court", "content": APPEAL_TEXT}]
        self.check_answer = {"evidence": "junk", "outcomes": [{"target": "e99", "outcome": "refuted"}]}
        r, out, rec = dc.disconfirm(res, recs)
        self.assertEqual(rec["outcomes"][0]["outcome"], "survived")
        self.assertEqual(rec["outcomes"][0]["why"], "no verdict")

    def test_snippet_on_a_known_page_is_appended(self):
        res, recs = self.fined()
        self.search_results["e7 news query"] = [
            {"url": "http://www." + FINE_URL.removeprefix("https://") + "/", "title": "News",
             "content": "Acme Analytics said it will appeal the fine."},
            {"url": FINE_URL, "title": "News", "content": "fined Acme Analytics 2 million euros"}]
        r, _, _ = dc.disconfirm(res, recs)
        self.assertEqual(r["sources"][FINE_URL]["text"],
                         FINE_PAGE + "\n\nAcme Analytics said it will appeal the fine.")
        self.assertEqual(res["sources"][FINE_URL]["text"], FINE_PAGE)
        self.assertEqual(len(results_in(self.calls["check"][0])), 1, "results deduped by url")


class TestTargets(Base):
    def test_selection_order_cap_and_exclusions(self):
        evs, srcs = [], {}
        for i, (sev, pol, link) in enumerate([("moderate", "negative", "none"), ("critical", "negative", "adjacent"),
                                              ("minor", "negative", "direct"), ("critical", "positive", "direct"),
                                              ("moderate", "negative", "direct"), ("critical", "negative", "direct"),
                                              ("moderate", "negative", "none"), ("moderate", "negative", "none"),
                                              ("critical", "negative", "direct")], start=1):
            u = f"https://n{i}.example.org/p"
            srcs[u] = src(FINE_PAGE)
            evs.append(ev(i, u, "fined Acme Analytics 2 million euros", severity=sev, polarity=pol, promise_link=link))
        recs = rc.check(research(evs, srcs))
        self.assertEqual([r["dup_of"] for r in recs], [None] + ["e1"] * 8, "one quote on nine urls: reposts")
        for r in recs:
            r["dup_of"] = None
        recs[8]["dup_of"] = "e6"
        recs[0]["kept"] = False
        mode, ts = dc.targets_of(research(evs, srcs), recs)
        self.assertEqual(mode, "negatives")
        self.assertEqual([t["target"] for t in ts], ["e6", "e2", "e5", "e7", "e8"])

    def test_green_probes_when_no_negatives(self):
        e = ev(1, FINE_URL, "fined Acme Analytics 2 million euros", polarity="positive", severity="moderate")
        res = research([e], {FINE_URL: src(FINE_PAGE)})
        recs = rc.check(res)
        self.plan_answer = {"queries": [{"target": "probe:incident", "tool": "news", "query": "Acme Analytics breach"},
                                        {"target": "probe:incident", "tool": "news", "query": "acme analytics BREACH"},
                                        {"target": "e1", "tool": "search", "query": "not a target"}]}
        self.search_results['"Acme" acquired OR acquisition OR merger'] = [
            {"url": "https://deals.example.org/x", "title": "Deal", "content": "Globex acquired Acme in 2026."}]
        self.check_answer = {"evidence": [], "outcomes": [{"target": "probe:ownership", "outcome": "survived",
                                                           "why": "nothing found"}]}
        r, out, rec = dc.disconfirm(res, recs)
        self.assertEqual(rec["mode"], "green_probes")
        self.assertEqual([t["target"] for t in rec["targets"]],
                         ["probe:incident", "probe:ownership", "probe:frozen", "probe:lawsuit"])
        self.assertEqual(targets_in(self.calls["plan"][0]), [t["target"] for t in rec["targets"]])
        qs = [(q["target"], q["tool"], q["query"]) for q in rec["queries"]]
        self.assertEqual(qs[0], ("probe:incident", "news", "Acme Analytics breach"))
        self.assertEqual(len(qs), 7, "1 model query (duplicate and unknown target dropped) + 2 templates x 3")
        self.assertIn(("probe:frozen", "search", '"Acme Analytics" discontinued OR "end of life" OR sunset'), qs)
        self.assertIn(("probe:lawsuit", "news", '"Acme" lawsuit OR "class action"'), qs)
        self.assertEqual(len(self.calls["tools"]), 7)
        self.assertEqual([o["outcome"] for o in rec["outcomes"]], ["survived"] * 4)
        self.assertEqual([o["why"] for o in rec["outcomes"]], ["no verdict", "nothing found", "no verdict",
                                                              "no verdict"])

    def test_probe_refuted_with_a_kept_receipt(self):
        res = research([], {})
        self.search_results["probe:incident news query"] = [
            {"url": "https://sec.example.org/acme", "title": "Acme breach",
             "content": "Acme Analytics confirmed a breach that exposed 40,000 customer records in May 2026."}]
        self.check_answer = {
            "evidence": [{"id": "x", "target": "probe:incident", "category": 11, "url": "https://sec.example.org/acme",
                          "quote": "Acme Analytics confirmed a breach that exposed 40,000 customer records",
                          "text": "A breach exposed 40,000 records.", "author_type": "trade_press",
                          "severity": "critical", "polarity": "negative", "date": "2026-05"}],
            "outcomes": [{"target": "probe:incident", "outcome": "refuted", "why": "a breach", "evidence": ["x"]}]}
        r, out, rec = dc.disconfirm(res, [])
        self.assertEqual(out[0]["id"], "e1")
        self.assertTrue(out[0]["kept"], out[0]["why"])
        self.assertEqual(r["evidence"][0]["hypothesis"], "disconfirmation of probe:incident: Is there a hidden "
                         "security incident, data breach or major outage?")
        self.assertEqual(rec["outcomes"][0]["outcome"], "refuted")


class TestSkip(Base):
    def test_deadline_skip(self):
        res, recs = self.fined()
        for dl in (time.time() + 100, time.monotonic() + 100, time.time() - 5):
            r, out, rec = dc.disconfirm(res, recs, deadline=dl)
            self.assertEqual(set(rec), {"skipped", "seconds"})
            self.assertTrue(rec["skipped"].startswith("deadline"), rec)
            self.assertEqual(r["disconfirmation"], rec)
            self.assertEqual(r["evidence"], res["evidence"])
            self.assertEqual(out, recs)
        self.assertEqual(self.calls, {"plan": [], "check": [], "tools": []})
        self.assertNotIn("disconfirmation", res)

    def test_monotonic_deadline_with_time_left_runs(self):
        res, recs = self.fined()
        self.search_results["e7 news query"] = [{"url": APPEAL_URL, "title": "Court", "content": APPEAL_TEXT}]
        _, _, rec = dc.disconfirm(res, recs, deadline=time.monotonic() + 1000)
        self.assertNotIn("skipped", rec)
        self.assertEqual((len(self.calls["plan"]), len(self.calls["check"])), (1, 1))

    def test_errors_never_raise(self):
        res, recs = self.fined()
        model.set_fake("disconfirm_plan", lambda s, u: (_ for _ in ()).throw(model.ModelError("boom")))
        r, out, rec = dc.disconfirm(res, recs)
        self.assertTrue(rec["skipped"].startswith("plan model error: ModelError"), rec)
        model.set_fake("disconfirm_plan", self._plan)
        r, out, rec = dc.disconfirm(res, recs)
        self.assertEqual(rec["skipped"], "no search results")
        self.assertEqual(len(rec["queries"]), 2)
        self.search_results["e7 news query"] = [{"url": APPEAL_URL, "title": "Court", "content": APPEAL_TEXT}]
        model.set_fake("disconfirm", lambda s, u: (_ for _ in ()).throw(ValueError("bad")))
        r, out, rec = dc.disconfirm(res, recs)
        self.assertTrue(rec["skipped"].startswith("disconfirm model error: ValueError"), rec)
        self.assertNotIn(APPEAL_URL, r["sources"], "a skip returns the research unchanged")
        with mock.patch.object(dc, "_run_tool", side_effect=RuntimeError("down")):
            r, out, rec = dc.disconfirm(res, recs)
        self.assertEqual(rec["skipped"], "no search results")
        self.assertEqual(r["sources_failed"], res["sources_failed"])
        for bad in ((None, None), ({}, None), ("x", [1, None]), ({"evidence": None, "identity": 3}, "junk")):
            r, out, rec = dc.disconfirm(*bad)
            self.assertIsInstance(rec, dict)
            self.assertIsInstance(out, list)

    def test_hung_search_times_out(self):
        res, recs = self.fined()
        self.search_results["e7 news query"] = [{"url": APPEAL_URL, "title": "Court", "content": APPEAL_TEXT}]
        gate = threading.Event()

        def tool(name, args):
            if args["query"] == "e7 search query":
                gate.wait(5)
            return self._tool(name, args)
        try:
            with mock.patch.object(dc, "_run_tool", tool), mock.patch.object(dc, "SEARCH_WAIT", 0.05):
                t0 = time.monotonic()
                r, _, rec = dc.disconfirm(res, recs)
                self.assertLess(time.monotonic() - t0, 2)
        finally:
            gate.set()
        self.assertTrue(rec["queries"][1]["error"].startswith("timeout"), rec["queries"])
        self.assertEqual(rec["queries"][0]["results"], 1)
        self.assertNotIn("skipped", rec)

    def test_failed_search_goes_to_sources_failed(self):
        res, recs = self.fined()
        self.search_results["e7 news query"] = [{"url": APPEAL_URL, "title": "Court", "content": APPEAL_TEXT}]

        def tool(name, args):
            if args["query"] == "e7 search query":
                return {"query": args["query"], "results": [], "error": "HTTP 432 plan limit"}
            return self._tool(name, args)
        with mock.patch.object(dc, "_run_tool", tool):
            r, _, rec = dc.disconfirm(res, recs)
        self.assertEqual(r["sources_failed"][-1], {"what": "e7 search query", "error": "HTTP 432 plan limit"})
        self.assertEqual(rec["queries"][1]["error"], "HTTP 432 plan limit")


if __name__ == "__main__":
    unittest.main()
