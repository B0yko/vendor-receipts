"""Lane L2: the research brain end to end on fake tools and a fake model (offline, invented names only)."""
import contextlib, io, os, re, sys, time, types, unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import ledger, model  # noqa: E402
from app.research import agent, prompts  # noqa: E402

HOME = "https://acme.example.com/"
FILLER = ("Acme Cloud helps mid-size teams plan their inventory across every warehouse with one dashboard. " * 6)
HOME_TEXT = ("Acme Cloud: inventory planning for mid-size manufacturers. We are SOC 2 Type II certified since 2015. "
             "Trusted by Example Industries. " + FILLER)
KEYWORD_PATHS = ["about", "company", "team", "imprint", "legal", "customers", "case-studies", "pricing", "security",
                 "trust", "compliance", "privacy", "terms", "changelog", "release-notes", "docs", "careers", "jobs",
                 "status", "blog", "news"]


def fake_tools(calls=None):
    calls = calls if calls is not None else []
    links = [{"url": HOME + p, "text": p.title()} for p in KEYWORD_PATHS]
    links += [{"url": HOME + "blog/a-very-long-post-name", "text": "Blog post"},
              {"url": "https://other.example.org/about", "text": "About them"},
              {"url": HOME + "about/logo.png", "text": "logo"}]

    def fetch(url):
        calls.append(("fetch", url))
        if url.endswith("security.txt"):
            return {"url": url, "final_url": url, "status": 404, "title": "", "text": "Not found", "links": [],
                    "meta": {}, "error": None}
        if url == HOME:
            return {"url": url, "final_url": url, "status": 200, "title": "Acme Cloud", "text": HOME_TEXT,
                    "links": links, "meta": {"generator": "ExampleBuilder 2", "copyright_years": [2019, 2021]},
                    "error": None}
        text = "Loading..." if url.endswith("/privacy") else f"Acme page {url}. " + FILLER
        return {"url": url, "final_url": url, "status": 200, "title": "Acme " + url.rsplit("/", 1)[-1],
                "text": text, "links": [], "meta": {}, "error": None}

    def extract(urls, depth="advanced"):
        calls.append(("extract", tuple(urls)))
        return {"results": [{"url": u, "text": "Acme API reference: REST endpoints for stock levels. " + FILLER}
                            for u in urls], "failed": [], "error": None}

    def search(query, **kw):
        s = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")
        return {"query": query, "error": None, "results": [
            {"url": f"https://news.example.org/{s}", "title": f"News on {query}", "score": 0.9,
             "content": f"Report on {query}: Acme Cloud was acquired by Example Holdings in March 2023.",
             "published_date": "2023-03-14"},
            {"url": "https://reviews.example.org/acme", "title": "Acme reviews", "score": 0.5,
             "content": f"Users write about {query}.", "published_date": ""}]}

    def run_tool(name, args):
        calls.append((name, tuple(sorted(args.items()))))
        if name == "search":
            return {**search(**args), "tool": name, "args": args, "seconds": time.time()}
        if name == "news":
            return {**search(args["query"]), "tool": name, "args": args, "seconds": time.time()}
        if name == "dns":
            return {"host": args["host"], "a": ["192.0.2.1"], "aaaa": [], "cname": [], "ns": [], "error": None,
                    "seconds": time.time()}
        if name == "wayback":
            return {"url": args["url"], "first": "20180101000000", "last": "20260101000000", "count": 50,
                    "snapshots": [{"timestamp": f"20{18 + i // 10}0101000000", "url": args["url"], "status": "200"}
                                  for i in range(50)], "error": None, "seconds": time.time()}
        if name == "fail":
            return {"error": "timeout"}
        return {"error": f"unknown tool {name}"}

    table = {n: {"fn": None, "doc": f"{n}: invented doc", "args": ["x"]} for n in ("search", "news", "dns", "wayback", "fail")}
    return types.SimpleNamespace(fetch=fetch, extract=extract, run_tool=run_tool, TOOLS=table)


def intake_answer(n_per_cat=2):
    actions = []
    for k in ledger.CATEGORIES:
        if k == 20:
            continue
        actions += [{"tool": "search", "args": {"query": f"Acme Cloud topic {k} q{j}"}, "category": k,
                     "hypothesis": f"h{k}"} for j in range(n_per_cat)]
    actions = [{"tool": "dns", "args": {"host": "app.acme.example.com"}, "category": 17, "hypothesis": "EU hosting"},
               {"tool": "wayback", "args": {"url": HOME + "terms"}, "category": 16, "hypothesis": "terms changed"},
               {"tool": "fail", "args": {"x": 1}, "category": 9, "hypothesis": "a failing tool"},
               {"tool": "nope", "args": {}, "category": 9, "hypothesis": "unknown tool"}] + actions
    plan = {str(k): {"hypotheses": [f"hypothesis for {k}"], "not_applicable": ""} for k in ledger.CATEGORIES}
    plan["20"] = {"hypotheses": [], "not_applicable": "closed source, no public code"}
    return {"identity": {"product": "Acme Cloud", "vendor": "Acme", "legal_entity": "Acme GmbH", "parent": "",
                         "former_names": ["Acme Stock"], "domains": ["acme.example.com"],
                         "key_people": [{"name": "Jane Roe", "role": "CEO"}], "country": "DE",
                         "languages": ["en", "de"], "same_name_risks": [], "notes": ""},
            "site": {"core_promise": "Inventory planning for mid-size manufacturers", "segment": "mid-size",
                     "claims": [{"text": "SOC 2 Type II since 2015", "kind": "certification",
                                 "quote": "We are SOC 2 Type II certified since 2015.", "url": HOME},
                                {"text": "Customer Example Industries", "kind": "customer",
                                 "quote": "Trusted by Example Industries.", "url": HOME}],
                     "inconsistencies": [], "signals": {"notes": "dated design"}},
            "evidence": [{"category": 13, "text": "Claims SOC 2", "url": HOME,
                          "quote": "We are SOC 2 Type II certified since 2015.", "date": "2015",
                          "author_type": "vendor_self", "severity": "minor", "polarity": "neutral",
                          "promise_link": "adjacent", "hypothesis": "certification", "source_title": ""},
                         {"category": 13, "text": "Bad url", "url": "https://nowhere.example.net/x", "quote": "x",
                          "author_type": "vendor_self", "severity": "minor", "polarity": "neutral"}],
            "plan": plan, "actions": actions}


def group_of(user):
    return re.search(r"Your group: (\w+) ", user).group(1)


def round_of(user):
    return int(re.search(r"Round (\d+);", user).group(1))


def set_split(ans, plan_seen=None):
    """The one-call intake answer split as the agent asks it: intake (identity, site, evidence) + one plan per group."""
    model.set_fake("intake", lambda s, u: {k: v for k, v in ans.items() if k not in ("plan", "actions")})

    def plan(system, user):
        if plan_seen is not None:
            plan_seen.append(user)
        cats = agent.GROUPS[group_of(user)]
        return {"plan": {k: v for k, v in ans.get("plan", {}).items() if int(k) in cats},
                "actions": [a for a in ans.get("actions", []) if a["category"] in cats]}
    model.set_fake("plan", plan)


def make_round(prompts_seen):
    def fn(system, user):
        prompts_seen.append(user)
        g, n = group_of(user), round_of(user)
        m = re.search(r"url: (https://news\.example\.org/acme-cloud-topic-2-q0)", user)
        if n == 1 and g == "B":                                   # a later group never lowers contradicted
            return {"claim_checks": [{"claim_id": "c1", "status": "confirmed", "evidence": []}], "done": False}
        if n == 1 and g != "A":
            return {"done": False}
        if n == 1:
            assert m, "round prompt lacks the numbered source"
            return {"evidence": [
                {"category": 2, "text": "Acquired by Example Holdings in 2023", "url": m.group(1),
                 "quote": "Acme Cloud was acquired by Example Holdings in March 2023.", "date": "2023-03",
                 "author_type": "trade_press", "severity": "moderate", "polarity": "negative",
                 "promise_link": "adjacent", "hypothesis": "ownership churn", "source_title": ""},
                {"category": "1", "text": "Identity confirmed", "url": m.group(1) + "#frag",
                 "quote": "Report on Acme Cloud topic 2 q0", "author_type": "trade_press", "severity": "minor",
                 "polarity": "neutral"},
                {"category": 9, "text": "Invented ruling", "url": "https://court.example.net/ruling",
                 "quote": "fined", "author_type": "authority", "severity": "critical", "polarity": "negative"},
                {"category": 9, "text": "Bad severity", "url": m.group(1), "quote": "Acme",
                 "author_type": "authority", "severity": "huge", "polarity": "negative"}],
                "matrix_updates": {"2": {"status": "found", "note": "acquired 2023"},
                                   "9": {"status": "searched_not_found", "note": "no ruling"}},
                "claim_checks": [{"claim_id": "c1", "status": "contradicted", "evidence": [m.group(1)],
                                  "note": "first traces 2018"},
                                 {"claim_id": "c99", "status": "confirmed", "evidence": []}],
                "next_actions": [{"tool": "search", "args": {"query": "Example Holdings layoffs after acquisition"},
                                  "category": 2, "hypothesis": "owner pattern"},
                                 {"tool": "search", "args": {"query": "Acme Cloud topic 3 q0"}, "category": 3,
                                  "hypothesis": "duplicate of round 1"}],
                "identity_updates": {"parent": "Example Holdings"}, "done": False}
        return {"evidence": [], "matrix_updates": {"3": {"status": "searched_not_found", "note": "nothing"}},
                "claim_checks": [], "next_actions": [], "done": True}
    return fn


class Base(unittest.TestCase):
    def setUp(self):
        self.addCleanup(model.clear_fakes)
        for purpose in ("intake", "plan", "round"):              # never a live model call from a test
            model.set_fake(purpose, lambda s, u: {})
        env = mock.patch.dict(os.environ, {"JUDGE_RESERVE": "300"})
        env.start()
        self.addCleanup(env.stop)

    def run_agent(self, intake=None, deadline=None, calls=None, hints=None):
        seen, intake_seen = [], []
        ans = intake or intake_answer()
        set_split(ans)
        model.set_fake("intake", lambda s, u: (intake_seen.append(u),
                                               {k: v for k, v in ans.items() if k not in ("plan", "actions")})[1])
        model.set_fake("round", make_round(seen))
        with mock.patch.object(agent, "tools", fake_tools(calls)):
            r = agent.research(HOME, deadline=deadline, hints=hints)
        return r, seen, intake_seen


class TestLoop(Base):
    def test_full_loop(self):
        calls = []
        r, seen, _ = self.run_agent(calls=calls)
        self.assertEqual(ledger.validate_research(r), [])
        self.assertEqual(set(r), set(ledger.new_research("x")))
        self.assertEqual(len(r["matrix"]), 24)
        for k, row in r["matrix"].items():
            self.assertTrue(row["status"] != "open" or row["note"], k)
        self.assertEqual(r["matrix"]["20"]["status"], "not_applicable")
        self.assertEqual(r["matrix"]["2"]["status"], "found")
        self.assertEqual(r["matrix"]["5"]["status"], "searched_not_found")   # queried, no update: closed by code
        ids = [e["id"] for e in r["evidence"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids, [f"e{i}" for i in range(1, len(ids) + 1)])
        self.assertEqual(len(ids), 3)                                          # 1 intake + 2 round, 3 dropped
        for e in r["evidence"]:
            self.assertIn(e["url"], r["sources"])
            self.assertIn(e["id"], r["matrix"][str(e["category"])]["evidence"])
        self.assertIn("e3", r["identity"]["evidence"])                         # category 1 links into identity
        dropped = [f for f in r["sources_failed"] if f["error"].startswith("dropped")]
        self.assertGreaterEqual(len(dropped), 3)
        self.assertTrue(any(f["error"] == "timeout" for f in r["sources_failed"]))
        checks = {c["claim_id"]: c for c in r["claim_checks"]}
        self.assertEqual(set(checks), {"c1", "c2"})
        self.assertEqual(checks["c1"]["status"], "contradicted")
        self.assertEqual(checks["c1"]["evidence"], ["e2", "e3"])
        self.assertEqual(checks["c2"]["status"], "not_found")
        self.assertEqual(r["identity"]["parent"], "Example Holdings")
        self.assertEqual([x["n"] for x in r["rounds"]], [1, 2])
        self.assertEqual(r["rounds"][0]["new_evidence"], 2)
        self.assertEqual(len(r["rounds"][1]["actions"]), 1)                   # the duplicate query was dropped
        self.assertEqual(r["stats"]["llm_calls"], 1 + 6 + 6 + 6)              # intake, 6 plans, 2 rounds x 6 groups
        self.assertFalse(r["stats"]["capped"])
        self.assertLessEqual(r["stats"]["tool_calls"], agent.MAX_TOOL_CALLS)
        self.assertIn("Acme Cloud topic 4 q1", r["matrix"]["4"]["queries"])
        self.assertTrue(r["started"] and r["seconds"] >= 0)
        # round-robin: the first 23 actions cover 23 categories, one each
        self.assertEqual(len({a["category"] for a in r["rounds"][0]["actions"][:23]}), 23)

    def test_site_read(self):
        calls = []
        r, _, intake_seen = self.run_agent(calls=calls)
        fetched = [c[1] for c in calls if c[0] == "fetch"]
        self.assertEqual(len(fetched), 1 + agent.MAX_SITE_PAGES + 1)
        self.assertNotIn("https://other.example.org/about", fetched)
        self.assertNotIn(HOME + "blog/a-very-long-post-name", fetched)
        self.assertNotIn(HOME + "about/logo.png", fetched)
        self.assertIn(HOME + ".well-known/security.txt", fetched)                # threads: call order varies
        self.assertEqual(fetched[0], HOME)
        pages = {p["url"]: p for p in r["site"]["pages"]}
        self.assertEqual(len(pages), 16)
        self.assertEqual(pages[HOME + ".well-known/security.txt"]["status"], 404)
        self.assertNotIn(HOME + ".well-known/security.txt", r["sources"])
        extracts = [c for c in calls if c[0] == "extract"]
        self.assertEqual(extracts, [("extract", (HOME + "privacy",))])
        self.assertEqual(r["sources"][HOME + "privacy"]["kind"], "fetch")      # short fetch text kept, extract appended
        self.assertIn("Acme API reference", r["sources"][HOME + "privacy"]["text"])
        self.assertEqual(pages[HOME + "privacy"]["kind"], "extract")
        self.assertNotIn(HOME + "docs", fetched)                               # 14 pages: keyword order decides
        self.assertEqual(r["sources"][HOME]["kind"], "fetch")
        self.assertEqual(r["site"]["signals"]["copyright_year"], "2021")
        self.assertEqual(r["site"]["signals"]["generator"], "ExampleBuilder 2")
        self.assertEqual(r["sources"]["https://news.example.org/acme-cloud-topic-2-q0"]["kind"], "search")
        self.assertEqual(r["sources"]["https://news.example.org/acme-cloud-topic-2-q0"]["published_date"],
                         "2023-03-14")
        self.assertIn("[P1] url: " + HOME, intake_seen[0])
        self.assertEqual([c["id"] for c in r["site"]["claims"]], ["c1", "c2"])

    def test_past_deadline_returns_after_intake(self):
        r, seen, _ = self.run_agent(deadline=time.time() - 1)
        self.assertEqual(seen, [])
        self.assertEqual(r["rounds"], [])
        self.assertTrue(r["stats"]["capped"])
        self.assertEqual(r["stats"]["llm_calls"], 1 + len(agent.GROUPS))
        self.assertEqual(ledger.validate_research(r), [])
        for k, row in r["matrix"].items():
            self.assertTrue(row["status"] != "open" or "not reached (cap)" in row["note"], k)
        self.assertEqual(len(r["claim_checks"]), 2)

    def test_reserve_counts(self):
        r, seen, _ = self.run_agent(deadline=time.time() + 100)               # 100 s left < 300 s reserve
        self.assertTrue(r["stats"]["capped"])
        self.assertEqual(seen, [])
        with mock.patch.dict(os.environ, {"JUDGE_RESERVE": "10"}):
            r, seen, _ = self.run_agent(deadline=time.time() + 100)
        self.assertEqual(len(seen), 12)
        self.assertFalse(r["stats"]["capped"])

    def test_two_runs_identical_prompts(self):
        _, seen1, in1 = self.run_agent()
        _, seen2, in2 = self.run_agent()
        self.assertEqual(len(seen1), 12)
        self.assertEqual(in1, in2)
        self.assertEqual(sorted(seen1), sorted(seen2))                       # calls run in parallel: order varies

    def test_tool_call_cap(self):
        n = iter(range(10_000))

        def endless(system, user):                        # 30 fresh actions per group and round, never done
            g, rn = group_of(user), round_of(user)
            return {"next_actions": [{"tool": "search", "args": {"query": f"Acme more {g} {rn} {i}"}, "category": 5}
                                     for i in range(30)], "done": False}
        set_split(intake_answer(n_per_cat=8))
        model.set_fake("round", endless)
        with mock.patch.object(agent, "tools", fake_tools()):
            r = agent.research(HOME)
        self.assertTrue(r["stats"]["capped"])
        self.assertEqual(r["stats"]["tool_calls"], agent.MAX_TOOL_CALLS)
        site = 1 + agent.MAX_SITE_PAGES + 1 + 1                  # home, 14 pages, security.txt, one extract
        self.assertEqual([len(x["actions"]) for x in r["rounds"]],
                         [40, 30, 30, agent.MAX_TOOL_CALLS - site - 100])   # 6 groups x 5 next actions
        self.assertEqual(ledger.validate_research(r), [])

    def test_model_failure_is_partial(self):
        def boom(s, u):
            raise model.ModelError("endpoint down")
        model.set_fake("intake", boom)
        model.set_fake("plan", boom)
        with mock.patch.object(agent, "tools", fake_tools()):
            r = agent.research(HOME)
        self.assertEqual(ledger.validate_research(r), [])
        self.assertTrue(any(f["what"] == "intake" for f in r["sources_failed"]))
        self.assertEqual(r["rounds"], [])
        self.assertTrue(all(row["note"] for row in r["matrix"].values()))

    def test_missing_tools_module(self):
        with mock.patch.object(agent, "tools", None), contextlib.redirect_stderr(io.StringIO()):
            r = agent.research(HOME)
        self.assertEqual(ledger.validate_research(r), [])
        self.assertTrue(any(f["what"] == "agent" for f in r["sources_failed"]))


class TestHints(Base):
    HINTS = {"id": "p7", "product": "Acme Cloud", "vendor": "Acme"}

    def test_hints_reach_intake_prompt_and_notes(self):
        r, seen, intake_seen = self.run_agent(hints=self.HINTS)
        self.assertIn('the buyer names this product "Acme Cloud" by vendor "Acme"', intake_seen[0])
        self.assertIn("anchors to verify, not facts", intake_seen[0])
        notes = r["identity"]["notes"]
        for v in ("p7", "Acme Cloud", "Acme"):
            self.assertIn(v, notes)
        self.assertIn("Buyer's identity hints", seen[0])                     # rounds see them through identity
        self.assertEqual(ledger.validate_research(r), [])

    def test_no_hints_no_line(self):
        _, _, intake_seen = self.run_agent()
        self.assertNotIn("Identity hints", intake_seen[0])
        _, _, intake_seen = self.run_agent(hints={"id": "p7"})                # an id alone names nothing
        self.assertNotIn("Identity hints", intake_seen[0])
        self.assertIn("vendor", prompts.hints_line({"vendor": "Acme"}))
        self.assertEqual(prompts.hints_line(None), "")

    def test_site_corrects_hints(self):
        r, _, _ = self.run_agent(hints={"product": "Acme Stock", "vendor": "Acme Old"})
        self.assertEqual(r["identity"]["product"], "Acme Cloud")              # the pages decide
        self.assertIn("Acme Stock", r["identity"]["notes"])

    def test_hints_stand_in_when_intake_fails(self):
        def boom(s, u):
            raise model.ModelError("endpoint down")
        model.set_fake("intake", boom)
        model.set_fake("plan", boom)
        with mock.patch.object(agent, "tools", fake_tools()):
            r = agent.research(HOME, hints=self.HINTS)
        self.assertEqual((r["identity"]["product"], r["identity"]["vendor"]), ("Acme Cloud", "Acme"))
        self.assertIn("to verify", r["identity"]["notes"])

    def test_news_tool_is_a_search(self):
        ans = intake_answer()
        ans["actions"].insert(0, {"tool": "news", "args": {"query": "Acme layoffs"}, "category": 21, "hypothesis": "x"})
        r, _, _ = self.run_agent(intake=ans)
        self.assertIn("news: Acme layoffs", r["matrix"]["21"]["queries"])
        self.assertEqual(r["sources"]["https://news.example.org/acme-layoffs"]["kind"], "search")


def words(s):
    return len(s.split())


class TestCaps(Base):
    LONG = " ".join(f"w{i}" for i in range(80))

    def oversized_intake(self):
        a = intake_answer(n_per_cat=8)
        q = "We are SOC 2 Type II certified since 2015."
        a["identity"].update(notes=self.LONG, same_name_risks=[f"Acme {i}" for i in range(6)],
                             key_people=[{"name": f"P{i}", "role": "x"} for i in range(9)])
        a["site"]["claims"] = [{"text": f"claim {i} " + self.LONG, "kind": "other", "quote": q, "url": HOME}
                               for i in range(30)]
        a["site"]["claims"][0]["quote"] = "x" * 201
        a["site"]["inconsistencies"] = [{"text": self.LONG, "quote": q, "url": HOME} for _ in range(9)]
        a["site"]["signals"] = {"notes": self.LONG}
        ev = {"category": 13, "url": HOME, "date": "2015", "author_type": "vendor_self", "severity": "minor",
              "polarity": "neutral", "hypothesis": self.LONG, "text": self.LONG}
        a["evidence"] = [{**ev, "quote": "y" * 251}] + [{**ev, "quote": f"Acme quote {i}"} for i in range(20)]
        a["plan"] = {str(k): {"hypotheses": [self.LONG] * 5} for k in ledger.CATEGORIES}
        for x in a["actions"]:
            x["hypothesis"] = self.LONG
        return a

    def oversized_round(self, prompts_seen):
        def fn(system, user):
            prompts_seen.append(user)
            g = group_of(user)
            m = re.search(r"url: (https://news\.example\.org/[a-z0-9-]+)", user)
            if not m:
                return {}
            ev = {"category": 2, "url": m.group(1), "author_type": "trade_press", "severity": "moderate",
                  "polarity": "negative", "text": self.LONG}
            return {"evidence": [{**ev, "quote": f"Report {i}"} for i in range(20)],
                    "matrix_updates": {str(k): {"status": "searched_not_found", "note": self.LONG}
                                       for k in ledger.CATEGORIES if k != 20},
                    "claim_checks": [{"claim_id": "c1", "status": "not_found", "note": self.LONG}],
                    "next_actions": [{"tool": "search", "args": {"query": f"Acme next {g} {round_of(user)} {i}"},
                                      "category": 7, "hypothesis": self.LONG} for i in range(40)],
                    "identity_updates": {"same_name_risks": [f"Other {i}" for i in range(6)]}, "done": False}
        return fn

    def test_caps_enforced_on_oversized_answers(self):
        c, seen = prompts.CAPS, []
        set_split(self.oversized_intake())
        model.set_fake("round", self.oversized_round(seen))
        with mock.patch.object(agent, "tools", fake_tools()):
            r = agent.research(HOME)
        self.assertEqual(ledger.validate_research(r), [])
        ident, site = r["identity"], r["site"]
        self.assertLessEqual(words(ident["notes"]), c["identity_notes_words"] + 1)
        self.assertEqual(len(ident["same_name_risks"]), 2 * c["same_name_risks"])   # 3 per answer: intake + round 1
        self.assertEqual(len(ident["key_people"]), c["key_people"])
        self.assertEqual(len(site["claims"]), c["claims"])
        self.assertEqual(site["claims"][0]["quote"], "")                            # over-long: dropped, never cut
        self.assertTrue(all(words(x["text"]) <= c["claim_text_words"] + 1 for x in site["claims"]))
        self.assertEqual(len(site["inconsistencies"]), c["inconsistencies"])
        self.assertLessEqual(words(site["signals"]["notes"]), c["note_words"] + 1)
        by_round = {}
        for e in r["evidence"]:
            by_round[e["round"]] = by_round.get(e["round"], 0) + 1
            self.assertLessEqual(len(e["quote"]), c["evidence_quote_chars"])
            self.assertLessEqual(words(e["text"]), c["evidence_text_words"] + 1)
            self.assertLessEqual(words(e["hypothesis"]), c["evidence_hypothesis_words"] + 1)
        self.assertEqual(by_round[0], c["evidence"])                              # intake: 12
        self.assertEqual(by_round[1], c["group_evidence"] * len(agent.GROUPS))     # 6 per group call
        self.assertNotIn("y" * 251, [e["quote"] for e in r["evidence"]])
        fails = [f["error"] for f in r["sources_failed"]]
        self.assertTrue(any("quote over 250 chars" in f for f in fails))
        self.assertTrue(any("over the cap of 12 evidence" in f for f in fails))
        self.assertTrue(any("over the cap of 6 evidence" in f for f in fails))
        for k, h in agent._Run(HOME, None).hyp.items():
            self.assertEqual(h, [])
        self.assertEqual(len(r["rounds"][0]["actions"]), c["intake_actions"])
        self.assertEqual(len(r["rounds"][1]["actions"]), c["group_round_actions"] * len(agent.GROUPS))
        for x in r["rounds"]:
            for a in x["actions"]:
                self.assertLessEqual(words(a["hypothesis"]), c["action_hypothesis_words"] + 1)
        for k, row in r["matrix"].items():                                      # model notes (20 keeps the code's
            if k != "20":                                                       # "hypotheses: ..." note)
                self.assertLessEqual(words(row["note"]), c["note_words"] + 1, k)
        self.assertLessEqual(words(r["claim_checks"][0]["note"]), c["note_words"] + 1)
        # plan: 2 hypotheses of <= 15 words per category, shown in the round prompt
        line = next(x for u in seen for x in u.splitlines() if x.startswith("5. "))
        hyp = line.split("| hypotheses: ")[1].split(" | note:")[0].split("; ")
        self.assertEqual(len(hyp), c["hypotheses"])
        self.assertTrue(all(words(h) <= c["hypothesis_words"] + 1 for h in hyp))

    def test_prompts_state_targets_and_caps(self):
        table = {"search": {"doc": "web search", "args": ["query"]}}
        i, pl, r = prompts.intake_system(table), prompts.plan_system(table), prompts.round_system(table)
        self.assertIn("under 12,000 characters", i)
        self.assertIn("under 8,000 characters", pl)
        self.assertIn("under 8,000 characters", r)
        for p in (i, pl, r):
            self.assertIn("compact JSON", p)
            self.assertIn("Never restate the input", p)
        self.assertIn("evidence <= 12 per answer", i)
        self.assertIn("evidence <= 6 per answer", r)
        for p in (i, r):
            self.assertIn("quote <= 250 chars", p)
        for cap in ("identity notes <= 40 words", "same_name_risks <= 3", "claims <= 15", "quote <= 200 chars",
                    "inconsistencies <= 5"):
            self.assertIn(cap, i)
        for cap in ("at most 2 hypotheses of <= 15 words", "actions <= 2 per category", "hypothesis <= 12 words"):
            self.assertIn(cap, pl)
        self.assertIn("next_actions <= 5", r)
        self.assertIn("notes <= 20 words", r)


class TestGroups(Base):
    def test_groups_cover_all_categories_once(self):
        cats = [c for cs in agent.GROUPS.values() for c in cs]
        self.assertEqual(sorted(cats), list(ledger.CATEGORIES))
        self.assertEqual(list(agent.GROUPS), ["A", "B", "C", "D", "E1", "E2"])

    def test_parallel_intake_and_plans_merge(self):
        plan_seen, intake_seen = [], []
        ans = intake_answer()
        ans["plan"]["14"] = {"not_applicable": "no site to judge"}
        set_split(ans, plan_seen)
        base_plan = model._fakes["plan"]

        def plan(system, user):                                   # group A also tries to plan category 9
            out = base_plan(system, user)
            if group_of(user) == "A":
                out["plan"]["9"] = {"not_applicable": "group A must not close 9"}
            return out
        model.set_fake("plan", plan)
        model.set_fake("intake", lambda s, u: (intake_seen.append(u), {k: v for k, v in ans.items()
                                                                       if k not in ("plan", "actions")})[1])
        model.set_fake("round", lambda s, u: {"done": True})
        with mock.patch.object(agent, "tools", fake_tools()):
            r = agent.research(HOME, hints={"product": "Acme Cloud", "vendor": "Acme"})
        self.assertEqual(len(intake_seen), 1)
        self.assertEqual(sorted(group_of(u) for u in plan_seen), sorted(agent.GROUPS))
        for u in plan_seen + intake_seen:
            self.assertIn("[P1] url: " + HOME, u)                                # the same site text for every call
            self.assertIn('the buyer names this product "Acme Cloud"', u)
        self.assertNotIn("Your group", intake_seen[0])
        self.assertEqual(r["identity"]["product"], "Acme Cloud")
        self.assertEqual(r["matrix"]["14"]["status"], "not_applicable")
        self.assertEqual(r["matrix"]["20"]["status"], "not_applicable")
        self.assertNotEqual(r["matrix"]["9"]["note"], "group A must not close 9")
        first = r["rounds"][0]["actions"]
        self.assertEqual(len(first), prompts.CAPS["intake_actions"])
        self.assertEqual([a["category"] for a in first[:4]], [1, 2, 3, 4])      # group order, round-robin
        self.assertEqual(len({a["category"] for a in first[:23]}), 23)
        self.assertEqual(r["stats"]["llm_calls"], 1 + 6 + 6)

    def test_failed_group_call(self):
        set_split(intake_answer())
        seen = []
        ok_round = make_round(seen)

        def plan_fails_for_c(system, user, base=model._fakes["plan"]):
            if group_of(user) == "C":
                raise model.ModelError("group C down")
            return base(system, user)

        def round_fails_for_b(system, user):
            if group_of(user) == "B" and round_of(user) == 1:
                raise model.ModelError("group B down")
            out = ok_round(system, user)
            if round_of(user) == 1 and group_of(user) == "E1":
                out = {"matrix_updates": {"5": {"status": "found", "note": "not E1's row"},
                                          "16": {"status": "searched_not_found", "note": "E1 own row"}}}
            return out
        model.set_fake("plan", plan_fails_for_c)
        model.set_fake("round", round_fails_for_b)
        with mock.patch.object(agent, "tools", fake_tools()):
            r = agent.research(HOME)
        self.assertEqual(ledger.validate_research(r), [])
        fails = [(f["what"], f["error"]) for f in r["sources_failed"]]
        self.assertIn(("plan", "model: group C down"), fails)
        self.assertIn(("round", "model: group B down"), fails)
        first = r["rounds"][0]["actions"]
        self.assertFalse([a for a in first if a["category"] in agent.GROUPS["C"]])   # C planned nothing
        self.assertTrue([a for a in first if a["category"] in agent.GROUPS["B"]])
        self.assertEqual(r["matrix"]["2"]["status"], "found")                        # A still merged
        self.assertEqual(r["matrix"]["16"]["note"], "E1 own row")
        self.assertNotEqual(r["matrix"]["5"]["note"], "not E1's row")                 # B's rows keep their state
        self.assertEqual(r["matrix"]["5"]["status"], "searched_not_found")           # closed by code at exit
        self.assertEqual(len(r["rounds"]), 2)

    def test_ids_deterministic_and_in_group_order(self):
        def per_group(system, user):
            g = group_of(user)
            urls = re.findall(r"url: (https://news\.example\.org/[a-z0-9-]+)", user)
            ev = [{"category": agent.GROUPS[g][0], "text": f"finding {g} {i}", "url": u, "quote": "Acme Cloud",
                   "author_type": "trade_press", "severity": "minor", "polarity": "neutral"}
                  for i, u in enumerate(urls[:3])]
            return {"evidence": ev, "done": True}
        runs = []
        for _ in range(2):
            set_split(intake_answer())
            model.set_fake("round", per_group)
            with mock.patch.object(agent, "tools", fake_tools()):
                r = agent.research(HOME)
            runs.append([(e["id"], e["url"], e["text"]) for e in r["evidence"]])
        self.assertEqual(runs[0], runs[1])
        groups = [t.split()[1] for _, _, t in runs[0] if t.startswith("finding")]
        self.assertEqual(groups, sorted(groups, key=list(agent.GROUPS).index))       # ids assigned in group order
        self.assertEqual(len(groups), 18)


class TestPieces(unittest.TestCase):
    def test_pick_links(self):
        links = [{"url": HOME + "blog/post-1"}, {"url": HOME + "blog"}, {"url": "https://docs.acme.example.com/"},
                 {"url": "https://example.org/about"}, {"url": HOME + "x.png", "text": "about"},
                 {"url": HOME + "a", "text": "Imprint"}, {"url": HOME + "#top", "text": "about"}]
        got = agent.pick_links(links, "example.com", skip=(HOME,))
        self.assertEqual(got, [HOME + "a", "https://docs.acme.example.com/", HOME + "blog"])

    def test_prompts_built_from_ledger(self):
        table = {"search": {"doc": "web search", "args": ["query"]}}
        for p in (prompts.intake_system(table), prompts.plan_system(table), prompts.round_system(table)):
            for name, *_ in ledger.CATEGORIES.values():
                self.assertIn(name, p)
            for k in ledger.AUTHOR_TYPES:
                self.assertIn(k, p)
            self.assertIn(prompts.QUESTION, p)
            self.assertIn("JSON object only", p)
            self.assertIn("- search: web search", p)
            self.assertIn("character for character", p)

    def test_round_prompt_cap(self):
        r = ledger.new_research(HOME)
        big = [{"url": f"https://example.org/{i}", "title": "t", "date": "", "kind": "search", "actions": [1],
                "text": "x" * 5000} for i in range(200)]
        view = {"url": HOME, "identity": r["identity"], "core_promise": "", "segment": "", "claims": [],
                "matrix": r["matrix"], "hypotheses": {}, "evidence": [], "round": 1, "rounds_left": 3,
                "tool_calls_left": 50, "actions": [], "hints": [], "sources": big}
        u = prompts.round_user(view)
        self.assertLess(len(u), prompts.PROMPT_CHARS + 6000)
        self.assertIn("more sources omitted for length", u)


if __name__ == "__main__":
    unittest.main()
