"""Lane L3: the judge and the four guards (offline; the model is faked with model.set_fake("judge"))."""
import os, sys, unittest

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
                         domains=["acme.example.com"])
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
            receipt("e10", 8, author="vendor_self", url="https://acme.example.com/trust", severity="critical")]


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
    def setUp(self):
        self.addCleanup(model.clear_fakes)
        self.seen = {}

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
                     "releases monthly", "Acme GmbH", "CLAIM CHECKS:\n(none)"):
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
        v = self.run_judge(answer("Red", red=[flag("Claims SOC 2 without a report", 8, ["e10"], "critical")]))
        self.assertEqual(v["overall"], "Yellow")
        self.assertEqual(len(v["red_flags"]), 1)
        self.assertTrue(any(g.startswith("G1 Red -> Yellow: no red flag has an external receipt") for g in v["guards"]))

    def test_g1_red_stands_on_a_receipted_contradiction(self):
        res = research()
        res["claim_checks"] = [{"claim_id": "c1", "status": "contradicted", "evidence": ["e10"], "note": "n"}]
        v = self.run_judge(answer("Red", red=[flag("SOC 2 claim is false", 8, ["e10"], "critical", "contradicted")]),
                           res=res)
        self.assertEqual(v["overall"], "Red")
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


if __name__ == "__main__":
    unittest.main()
