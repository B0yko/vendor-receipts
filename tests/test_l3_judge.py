"""Lane L3: the judge and the four guards (offline; the model is faked with model.set_fake("judge"))."""
import os, sys, threading, unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import ledger, model  # noqa: E402
from app.judge import guards, judge, prompts  # noqa: E402

SECRET_SOURCE = "RAW-SOURCE-TEXT-NEVER-IN-PROMPT"
REJECTED_QUOTE = "Acme Shipping Ltd, a different company, was fined"


def receipt(id, category, author="trade_press", url="https://news.example.org/acme", kept=True, dup_of=None,
            severity="moderate", polarity="negative", date="2026-03-01", text=None, independent=None):
    dom = ledger.registrable(url)
    return {"id": id, "category": category, "text": text or f"finding {id}", "url": url, "quote": f"quote of {id}",
            "date": date, "author_type": author, "severity": severity, "polarity": polarity, "promise_link": "direct",
            "hypothesis": "h", "source_title": "t", "round": 1,
            "checks": {"resolves": kept, "same_company": kept, "says_it": kept, "dated": bool(date)},
            "kept": kept, "why": "" if kept else "same name, different company", "domain": dom,
            "independent": (dom != "example.com") if independent is None else independent, "dup_of": dup_of}


def research():
    r = ledger.new_research("https://acme.example.com")
    r["identity"].update(product="Acme Ledger", vendor="Acme GmbH", parent="Acme Holdings", former_names=["AcmeBooks"],
                         domains=["acme.example.com"], legal_entity="Acme Software GmbH",
                         notes="buyer hint: product Acme Ledger, vendor Acme GmbH")
    r["site"].update(core_promise="bookkeeping for small firms", segment="SMB",
                     claims=[{"id": "c1", "text": "SOC 2 certified", "kind": "certification", "quote": "We are SOC 2",
                              "url": "https://acme.example.com/trust"}])
    r["sources"] = {"https://news.example.org/acme": {"text": SECRET_SOURCE, "kind": "extract", "title": "t",
                                                       "published_date": "", "fetched_at": ""}}
    r["matrix"]["5"].update(status="found", evidence=["e2"], note="releases monthly", queries=["q1", "q2"])
    r["matrix"]["9"].update(status="searched_not_found", queries=["q1", "q2"], note="no rulings")
    r["sources_failed"] = [{"what": "https://acme.example.com/blog", "error": "403"}]
    return r


def receipts():
    return [receipt("e1", 1, author="vendor_self", url="https://acme.example.com/about", polarity="neutral"),
            receipt("e2", 5, author="registry_archive", url="https://registry.example.net/acme", polarity="positive"),
            receipt("e3", 7, author="trade_press", url="https://news.example.org/acme", polarity="positive"),
            receipt("e4", 7, author="aggregator", url="https://copy.example.net/acme", dup_of="e3", polarity="positive"),
            receipt("e5", 9, author="authority", url="https://gov.example.org/ruling", kept=False,
                    text="a fine for a different Acme"),
            receipt("e6", 19, author="user_review", url="https://forum.example.org/t/1", date="2026-01-02"),
            receipt("e7", 19, author="user_review", url="https://reviews.example.net/acme", date="2026-05-20"),
            receipt("e8", 14, author="vendor_self", url="https://acme.example.com/", severity="minor"),
            receipt("e9", 9, author="authority", url="https://gov.example.org/order", severity="critical"),
            receipt("e10", 8, author="vendor_self", url="https://acme.example.com/trust", severity="critical"),
            receipt("e12", 8, author="trade_press", url="https://acme.example.com/press/award", severity="critical"),
            receipt("e13", 4, author="vendor_against_interest", url="https://acme.example.com/eol",
                    severity="critical", text="Acme Ledger ends in 2027 with no successor")]


def flag(text, category, ids, severity="moderate", basis="confirmed"):
    return {"text": text, "category": category, "severity": severity, "basis": basis, "receipts": ids}


def answer(overall, red=(), yellow=(), green=(), **kw):
    a = {"overall": overall, "reliability": "medium", "reliability_why": "one source per flag [e2]",
         "green_flags": list(green), "yellow_flags": list(yellow), "red_flags": list(red),
         "reasoning": "On its own site Acme presents itself as X [e1]; external sources show Y [e3].",
         "blind_spots": ["no certifier record"], "buyer_next_step": "Ask Acme for the SOC 2 report.",
         "vendor_questions": ["q1?", "q2?", "q3?"], "would_flip": "a regulator ruling would make it Red"}
    a.update(kw)
    return a


class Base(unittest.TestCase):
    """JUDGE_VIEWS=1 unless a test sets it; a live model call fails the test instead of reaching the pi CLI."""
    def setUp(self):
        self.addCleanup(model.clear_fakes)
        self.seen = {}
        for p in (mock.patch.dict(os.environ, {"JUDGE_VIEWS": "1", "RECEIPTS_FAKE": "", "REPLAY": ""}),
                  mock.patch.object(model, "_ask", side_effect=AssertionError("live model call in a test"))):
            p.start()
            self.addCleanup(p.stop)

    def run_judge(self, ans, res=None, recs=None):
        def fake(system, user):
            self.seen.update(system=system, user=user)
            return ans
        model.set_fake("judge", fake)
        v = judge.judge(res or research(), recs or receipts())
        self.assertEqual(ledger.validate_verdict(v), [])
        self.assertEqual(v["call"], v["overall"])
        return v


class TestJudge(Base):
    def test_flags_carry_receipt_fields_and_verdict_validates(self):
        v = self.run_judge(answer("Green", green=[flag("Monthly releases", 5, ["e99", "e2", "e3"])],
                                  yellow=[flag("Customers named only by press", 7, ["e3", "e4"], severity="minor")]))
        g = v["green_flags"][0]
        self.assertEqual(g["receipts"], ["e2", "e3"])  # unknown id dropped
        self.assertEqual((g["url"], g["quote"], g["date"], g["author"]),
                         ("https://registry.example.net/acme", "quote of e2", "2026-03-01", "registry_archive"))
        self.assertEqual(g["corroboration"], "independent")
        self.assertEqual(v["yellow_flags"][0]["corroboration"], "one")  # a repost is not corroboration
        self.assertEqual(v["overall"], "Green")
        self.assertEqual([r["url"] for r in v["reasons"]],
                         ["https://news.example.org/acme", "https://registry.example.net/acme"])  # yellow, then green
        self.assertEqual(v["rejected"], [{"text": "a fine for a different Acme", "url": "https://gov.example.org/ruling",
                                          "why": "same name, different company"}])
        self.assertEqual((v["product"], v["vendor"], v["parent"], v["former_names"]),
                         ("Acme Ledger", "Acme GmbH", "Acme Holdings", ["AcmeBooks"]))
        self.assertEqual(len(v["coverage"]), 24)
        self.assertEqual((v["coverage"]["5"], v["coverage"]["9"], v["coverage"]["1"]),
                         ("found", "searched_not_found", "open"))
        self.assertEqual(v["sources_failed"], [{"what": "https://acme.example.com/blog", "error": "403"}])
        self.assertEqual(v["model"], model.model_id())
        self.assertEqual(len(v["vendor_questions"]), 3)
        self.assertEqual(v["guards"], [])
        self.assertIsNone(v["error"])

    def test_partial_says_it_note_reaches_the_judge(self):
        recs = receipts()
        recs[2]["says_it_note"] = "partial: the date of the contract is not in the quote"
        self.run_judge(answer("Yellow", yellow=[flag("Thin record", 7, ["e3"])]), recs=recs)
        self.assertIn('"id": "e3"', self.seen["user"])
        self.assertIn('"says_it_note": "partial: the date of the contract is not in the quote"', self.seen["user"])
        self.assertEqual(self.seen["user"].count("says_it_note"), 1)  # only where present
        self.assertIn("supports only its central assertion, never the missing\ndetail", self.seen["system"])

    def test_same_domain_is_one_source(self):
        recs = receipts() + [receipt("e11", 7, url="https://news.example.org/acme-2")]
        v = self.run_judge(answer("Green", green=[flag("Named customers", 7, ["e3", "e11"])]), recs=recs)
        self.assertEqual(v["green_flags"][0]["corroboration"], "one")

    def test_judge_sees_only_kept_receipts_and_no_raw_sources(self):
        self.run_judge(answer("Yellow", yellow=[flag("Thin record", 7, ["e3"])]))
        user, system = self.seen["user"], self.seen["system"]
        self.assertNotIn(SECRET_SOURCE, user)
        self.assertNotIn("a fine for a different Acme", user)
        self.assertNotIn('"id": "e5"', user)
        self.assertIn('"reposts": [{"id": "e4"', user)  # repost listed under its original
        self.assertNotIn('{"id": "e4", "category"', user)
        for part in ("bookkeeping for small firms", "SOC 2 certified", "9 Regulators: searched_not_found",
                     "releases monthly", "Acme GmbH", "CLAIM CHECKS:\n(none)",
                     "Acme Software GmbH", "Acme Holdings", "AcmeBooks", "buyer hint: product Acme Ledger"):
            self.assertIn(part, user)
        for part in (ledger.COLOR_RULE, ledger.GUARDS, ledger.REVIEW_RULES, ledger.AUTHOR_TYPES["authority"]):
            self.assertIn(part, system)
        self.assertEqual(system, prompts.SYSTEM)

    def test_model_failure_yields_low_reliability_yellow_with_error(self):
        def boom(system, user):
            raise model.ModelError("endpoint down")
        model.set_fake("judge", boom)
        v = judge.judge(research(), receipts())
        self.assertEqual(ledger.validate_verdict(v), [])
        self.assertEqual((v["overall"], v["call"], v["reliability"]), ("Yellow", "Yellow", "low"))
        self.assertIn("endpoint down", v["error"])
        self.assertEqual(v["reasons"][0]["url"], "https://gov.example.org/order")  # best kept receipt: authority

    def test_no_flags_reason_is_best_kept_receipt(self):
        v = self.run_judge(answer("Yellow"))
        self.assertEqual(v["reasons"], [{"text": "finding e9", "url": "https://gov.example.org/order",
                                         "quote": "quote of e9"}])

    def test_invalid_color_becomes_yellow(self):
        v = self.run_judge(answer("purple", yellow=[flag("Thin record", 7, ["e3"])]))
        self.assertEqual(v["overall"], "Yellow")
        self.assertTrue(v["guards"][0].startswith("judge: overall"))


class TestGuards(Base):
    def test_g4_flag_without_kept_receipt_is_dropped(self):
        v = self.run_judge(answer("Yellow", red=[flag("Fined by a regulator", 9, ["e5", "e404"], "critical")],
                                  yellow=[flag("Thin record", 7, ["e3"])]))
        self.assertEqual(v["red_flags"], [])
        self.assertTrue(any(g.startswith("G4 dropped red flag 'Fined by a regulator'") for g in v["guards"]))
        self.assertEqual(v["overall"], "Yellow")

    def test_g2_user_reviews_alone_are_dropped_to_blind_spots(self):
        v = self.run_judge(answer("Yellow", yellow=[flag("Billing traps after cancellation", 19, ["e6", "e7"]),
                                                    flag("Thin record", 7, ["e3", "e6"])]))
        self.assertEqual([f["text"] for f in v["yellow_flags"]], ["Thin record"])  # a review plus press survives
        self.assertIn("users report billing traps after cancellation (2 sources, 2026-01-02 to 2026-05-20; "
                      "not confirmed elsewhere)", v["blind_spots"])
        self.assertTrue(any(g.startswith("G2 dropped yellow flag") for g in v["guards"]))

    def test_g3_site_quality_red_moves_to_yellow_and_alive_product_is_green(self):
        v = self.run_judge(answer("Red", red=[flag("Copyright 2015 and dead links", 14, ["e8"], "critical")],
                                  green=[flag("Monthly releases", 5, ["e2"])]))
        self.assertEqual(v["red_flags"], [])
        self.assertEqual([f["category"] for f in v["yellow_flags"]], [14])
        self.assertEqual(v["overall"], "Green")
        self.assertTrue(any(g.startswith("G3 moved red flag") for g in v["guards"]))
        self.assertTrue(any(g.startswith("G1 Red -> Yellow: no red flag left") for g in v["guards"]))
        self.assertTrue(any(g.startswith("G3 Yellow -> Green") for g in v["guards"]))

    def test_g3_site_quality_yellow_without_alive_flag_stays_yellow(self):
        v = self.run_judge(answer("Yellow", yellow=[flag("Dated design", 14, ["e8"], "minor")],
                                  green=[flag("Named customers", 7, ["e1"]) | {"category": 13}]))
        self.assertEqual(v["overall"], "Yellow")
        self.assertTrue(any("too little evidence" in g for g in v["guards"]))

    def test_g1_red_without_external_receipt_becomes_yellow(self):
        v = self.run_judge(answer("Red", red=[flag("Award claim is copied press", 8, ["e12"], "critical")]))
        self.assertEqual(v["overall"], "Yellow")
        self.assertEqual(len(v["red_flags"]), 1)  # press on the vendor's own domain: not external, not a claim only
        self.assertTrue(any(g.startswith("G1 Red -> Yellow: no red flag has an external receipt") for g in v["guards"]))

    def test_g1_vendor_self_red_flag_moves_to_yellow(self):
        """Decision L3-2 #2: a red flag on the vendor's own claims only is never proof."""
        v = self.run_judge(answer("Red", red=[flag("Claims SOC 2 without a report", 8, ["e10", "e1"], "critical")]))
        self.assertEqual((v["overall"], v["red_flags"]), ("Yellow", []))
        self.assertEqual([f["text"] for f in v["yellow_flags"]], ["Claims SOC 2 without a report"])
        self.assertTrue(any(g.startswith("G1 moved red flag 'Claims SOC 2") for g in v["guards"]))
        self.assertTrue(any(g.startswith("G1 Red -> Yellow: no red flag left") for g in v["guards"]))

    def test_g1_vendor_against_interest_on_own_domain_holds_red(self):
        """Decision L3-2 #1: the vendor's own end-of-life notice is a valid Red basis."""
        v = self.run_judge(answer("Red", red=[flag("Ends in 2027 with no successor", 4, ["e13"], "critical")]))
        self.assertEqual((v["overall"], v["guards"]), ("Red", []))
        f = v["red_flags"][0]
        self.assertEqual((f["author"], f["url"]), ("vendor_against_interest", "https://acme.example.com/eol"))

    def test_g1_green_with_any_red_flag_becomes_yellow(self):
        """Decision L3-2 #2: a Green with any surviving red flag is Yellow, whatever the receipt's author."""
        v = self.run_judge(answer("Green", red=[flag("Award claim is copied press", 8, ["e12"], "critical")],
                                  green=[flag("Monthly releases", 5, ["e2"])]))
        self.assertEqual((v["overall"], len(v["red_flags"])), ("Yellow", 1))
        self.assertTrue(any(g.startswith("G1 Green -> Yellow: red flag 'Award claim") for g in v["guards"]))
        v = self.run_judge(answer("Green", red=[flag("Ends in 2027 with no successor", 4, ["e13"], "critical")],
                                  green=[flag("Monthly releases", 5, ["e2"])]))
        self.assertEqual(v["overall"], "Yellow")
        v = self.run_judge(answer("Green", red=[flag("Claims SOC 2 without a report", 8, ["e10"], "critical")],
                                  green=[flag("Monthly releases", 5, ["e2"])]))
        self.assertEqual((v["overall"], v["red_flags"], len(v["yellow_flags"])), ("Green", [], 1))  # moved first

    def test_g1_red_stands_on_a_receipted_contradiction(self):
        res = research()
        res["claim_checks"] = [{"claim_id": "c1", "status": "contradicted", "evidence": ["e10"], "note": "n"}]
        v = self.run_judge(answer("Red", red=[flag("SOC 2 claim is false", 8, ["e10"], "critical", "contradicted")]),
                           res=res)
        self.assertEqual((v["overall"], len(v["red_flags"])), ("Red", 1))  # vendor_self, but receipted contradiction
        res["claim_checks"][0]["evidence"] = ["e5"]  # rejected receipt: not a receipted contradiction
        v = self.run_judge(answer("Red", red=[flag("SOC 2 claim is false", 8, ["e10"], "critical", "contradicted")]),
                           res=res)
        self.assertEqual(v["overall"], "Yellow")

    def test_g1_red_stands_on_an_external_receipt(self):
        v = self.run_judge(answer("Red", red=[flag("Ordered to stop selling", 9, ["e9"], "critical")]))
        self.assertEqual((v["overall"], v["guards"]), ("Red", []))
        self.assertEqual(v["red_flags"][0]["author"], "authority")
        self.assertEqual(v["reasons"][0]["url"], "https://gov.example.org/order")

    def test_g1_green_with_external_red_flag_becomes_yellow(self):
        v = self.run_judge(answer("Green", red=[flag("Ordered to stop selling", 9, ["e9"], "critical")],
                                  green=[flag("Monthly releases", 5, ["e2"])]))
        self.assertEqual(v["overall"], "Yellow")
        self.assertTrue(any(g.startswith("G1 Green -> Yellow") for g in v["guards"]))

    def test_apply_directly_keeps_call_in_step(self):
        recs = receipts()
        by_id = {r["id"]: r for r in recs}
        v = {"overall": "Red", "call": "Red", "red_flags": [judge.build_flag(flag("x", 14, ["e8"]), by_id)],
             "yellow_flags": [], "green_flags": [], "blind_spots": []}
        out = guards.apply(v, research(), recs)
        self.assertEqual((out["overall"], out["call"]), ("Yellow", "Yellow"))


class TestDisconfirmation(Base):
    def test_judge_sees_disconfirmation_outcomes_with_kept_ids_only(self):
        res = research()
        res["disconfirmation"] = {"targets": [{"id": "e9", "text": "ordered to stop selling"}], "queries": ["q1", "q2"],
                                  "outcomes": [{"target": "e9", "outcome": "survived", "why": "no appeal found",
                                                "evidence": ["e3", "e5"]}]}
        self.run_judge(answer("Yellow", yellow=[flag("Thin record", 7, ["e3"])]))
        self.assertIn("DISCONFIRMATION", self.seen["system"])
        self.run_judge(answer("Yellow", yellow=[flag("Thin record", 7, ["e3"])]), res=res)
        user = self.seen["user"]
        self.assertIn("targets: 1; queries run: 2", user)
        self.assertIn('{"target": "e9", "outcome": "survived", "why": "no appeal found", "receipts": ["e3"]}', user)
        res["disconfirmation"] = {"skipped": "no critical finding"}
        self.run_judge(answer("Yellow", yellow=[flag("Thin record", 7, ["e3"])]), res=res)
        self.assertIn("DISCONFIRMATION (attempts to disprove the decisive findings):\n(skipped: no critical finding)",
                      self.seen["user"])
        self.run_judge(answer("Yellow", yellow=[flag("Thin record", 7, ["e3"])]))
        self.assertIn("DISCONFIRMATION (attempts to disprove the decisive findings):\n(not run)", self.seen["user"])

    def test_refuted_target_cannot_carry_a_red(self):
        res = research()
        res["disconfirmation"] = {"targets": ["e9"], "queries": ["q"],
                                  "outcomes": [{"target": "e9: ordered to stop selling", "outcome": "refuted",
                                                "why": "order was lifted", "evidence": ["e3"]}]}
        v = self.run_judge(answer("Red", red=[flag("Ordered to stop selling", 9, ["e9"], "critical")]), res=res)
        self.assertEqual((v["overall"], v["red_flags"]), ("Yellow", []))
        self.assertEqual([f["text"] for f in v["yellow_flags"]], ["Ordered to stop selling"])
        self.assertTrue(any(g.startswith("D moved red flag 'Ordered to stop selling'") for g in v["guards"]))

    def test_weakened_target_lowers_reliability(self):
        res = research()
        res["disconfirmation"] = {"targets": ["e9"], "queries": ["q"],
                                  "outcomes": [{"target": {"id": "e9"}, "outcome": "weakened", "why": "under appeal",
                                                "evidence": []}]}
        v = self.run_judge(answer("Red", red=[flag("Ordered to stop selling", 9, ["e9"], "critical")],
                                  reliability="high"), res=res)
        self.assertEqual((v["overall"], v["reliability"]), ("Red", "medium"))
        self.assertTrue(any(g.startswith("D reliability high -> medium") for g in v["guards"]))


class TestViews(Base):
    """JUDGE_VIEWS=3: three views in parallel (judge_all, judge_external, judge_devil), then the reconcile (judge)."""
    def setUp(self):
        super().setUp()
        os.environ["JUDGE_VIEWS"] = "3"
        self.calls = []

    def fakes(self, views, final):
        def view(purpose):
            def fn(system, user):
                self.calls.append(purpose)
                self.seen[purpose] = (system, user)
                a = views.get(purpose)
                if isinstance(a, Exception):
                    raise a
                barrier.wait()  # breaks (BrokenBarrierError) unless the answering views run at the same time
                return a
            return fn
        barrier = threading.Barrier(max(1, sum(not isinstance(a, Exception) for a in views.values())), timeout=5)
        for p in ("judge_all", "judge_external", "judge_devil"):
            model.set_fake(p, view(p))

        def reconcile(system, user):
            self.calls.append("judge")
            self.seen["judge"] = (system, user)
            if isinstance(final, Exception):
                raise final
            return final
        model.set_fake("judge", reconcile)

    def run_views(self, views, final, res=None):
        self.fakes(views, final)
        v = judge.judge(res or research(), receipts())
        self.assertEqual(ledger.validate_verdict(v), [])
        self.assertEqual(v["call"], v["overall"])
        return v

    def yellow(self, rel="medium", ids=("e3",)):
        return answer("Yellow", yellow=[flag("Thin record", 7, list(ids))], reliability=rel)

    def test_agreement_floors_reliability_and_says_so(self):
        v = self.run_views({"judge_all": self.yellow("high", ("e3", "e1")), "judge_external": self.yellow("medium"),
                            "judge_devil": self.yellow("high") | {"argued_against": "Yellow", "argued_for": "Green"}},
                           self.yellow("low"))
        self.assertEqual(self.calls[-1], "judge")
        self.assertEqual(sorted(self.calls[:3]), ["judge_all", "judge_devil", "judge_external"])
        self.assertEqual(v["views"], [{"view": "all", "overall": "Yellow", "reliability": "high", "key_receipts": ["e3", "e1"]},
                                      {"view": "external", "overall": "Yellow", "reliability": "medium", "key_receipts": ["e3"]},
                                      {"view": "devil", "overall": "Yellow", "reliability": "high", "key_receipts": ["e3"]}])
        self.assertEqual((v["overall"], v["reliability"]), ("Yellow", "medium"))
        self.assertTrue(v["reliability_why"].startswith("3/3 views agree; "))
        self.assertIn("views: reliability low -> medium, 3/3 views agree", v["guards"])
        # each view's prompt and input
        self.assertEqual(self.seen["judge_all"][0], prompts.SYSTEM)
        self.assertEqual(self.seen["judge_devil"][0], prompts.VIEW_SYSTEM["devil"])
        self.assertIn("devil's advocate", self.seen["judge_devil"][0])
        ext = self.seen["judge_external"][1]
        for vid in ("e2", "e3", "e9", "e13"):  # independent, or the vendor's statement against interest
            self.assertIn(f'"id": "{vid}"', ext)
        for vid in ("e1", "e8", "e10", "e12"):  # vendor_self, or on the vendor's domain
            self.assertNotIn(f'"id": "{vid}"', ext)
        self.assertIn('"id": "e1"', self.seen["judge_all"][1])
        system, user = self.seen["judge"]
        self.assertEqual(system, prompts.RECONCILE_SYSTEM)
        self.assertIn("VIEWS (independent judge answers on this research):", user)
        self.assertIn('devil: {"overall": "Yellow"', user)
        self.assertIn('"argued_for": "Green"', user)

    def test_model_written_agreement_is_not_repeated(self):
        v = self.run_views({p: self.yellow() for p in ("judge_all", "judge_external", "judge_devil")},
                           self.yellow() | {"reliability_why": "3/3 views agree; one source per flag [e3]"})
        self.assertEqual(v["reliability_why"], "3/3 views agree; one source per flag [e3]")

    def test_disagreement_lowers_reliability_and_is_named(self):
        green = answer("Green", green=[flag("Monthly releases", 5, ["e2"])], reliability="high")
        red = answer("Red", red=[flag("Ordered to stop selling", 9, ["e9"], "critical")], reliability="high")
        v = self.run_views({"judge_all": green, "judge_external": self.yellow(), "judge_devil": red},
                           self.yellow("high"))
        self.assertEqual((v["overall"], v["reliability"]), ("Yellow", "low"))  # three colors: low
        self.assertTrue(v["reliability_why"].startswith("views disagree: all Green, external Yellow, devil Red; "))
        self.assertEqual([(x["view"], x["overall"], x["key_receipts"]) for x in v["views"]],
                         [("all", "Green", ["e2"]), ("external", "Yellow", ["e3"]), ("devil", "Red", ["e9"])])
        self.calls.clear()
        v = self.run_views({"judge_all": self.yellow(), "judge_external": self.yellow(), "judge_devil": green},
                           self.yellow("high"))
        self.assertEqual(v["reliability"], "medium")  # two colors: at most medium
        self.assertIn("views disagree: all Yellow, external Yellow, devil Green", v["reliability_why"])

    def test_failed_view_is_skipped(self):
        v = self.run_views({"judge_all": self.yellow(), "judge_external": model.ModelError("pi failed: timeout"),
                            "judge_devil": self.yellow()}, self.yellow())
        self.assertEqual([x["view"] for x in v["views"]], ["all", "devil"])
        self.assertIn("views: external failed, skipped: pi failed: timeout", v["guards"])
        self.assertTrue(v["reliability_why"].startswith("2/2 views agree (1 failed); "))
        self.assertNotIn("external:", self.seen["judge"][1])
        self.assertIsNone(v["error"])

    def test_every_view_failed_falls_back_to_one_call(self):
        v = self.run_views({p: model.ModelError("down") for p in ("judge_all", "judge_external", "judge_devil")},
                           self.yellow())
        self.assertEqual(self.calls.count("judge"), 1)
        self.assertEqual(self.seen["judge"][0], prompts.SYSTEM)  # the plain judge, not the reconcile
        self.assertEqual(self.seen["judge"][1], judge.judge_input(research(), receipts()))
        self.assertEqual(v["views"], [])
        self.assertIn("views: every view failed, one plain judge call instead", v["guards"])
        self.assertEqual(v["yellow_flags"][0]["url"], "https://news.example.org/acme")

    def test_failed_reconcile_keeps_the_all_view(self):
        green = answer("Green", green=[flag("Monthly releases", 5, ["e2"])], reliability="high")
        v = self.run_views({"judge_all": green, "judge_external": green, "judge_devil": self.yellow()},
                           model.ModelError("reconcile down"))
        self.assertEqual(v["overall"], "Green")
        self.assertTrue(any(g.startswith("views: reconcile failed (reconcile down), the all view's answer stands")
                            for g in v["guards"]))
        self.assertIsNone(v["error"])

    def test_judge_views_1_is_one_plain_call(self):
        os.environ["JUDGE_VIEWS"] = "1"
        self.fakes({p: AssertionError(p + " must not run") for p in ("judge_all", "judge_external", "judge_devil")},
                   self.yellow())
        v = judge.judge(research(), receipts())
        self.assertEqual(self.calls, ["judge"])
        self.assertEqual(self.seen["judge"], (prompts.SYSTEM, judge.judge_input(research(), receipts())))
        self.assertEqual((v["views"], v["reliability"], v["reliability_why"]), ([], "medium", "one source per flag [e2]"))
        self.assertFalse(any(g.startswith("views:") for g in v["guards"]))
        self.assertEqual(ledger.validate_verdict(v), [])
        self.assertEqual(judge.judge(research(), receipts(), views=1)["views"], [])


if __name__ == "__main__":
    unittest.main()
