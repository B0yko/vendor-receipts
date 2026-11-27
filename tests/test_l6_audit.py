"""Lane L6: self-audit (app/check/audit.py). Code only, no model; invented vendor Acme on example domains."""
import copy, os, sys, unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app.check import audit as au  # noqa: E402

A, B, C = "https://news.example.org/acme-fined", "https://acme.example/terms", "https://other.example.org/x"
RECEIPTS = [{"id": "e1", "kept": True, "url": A}, {"id": "e2", "kept": True, "url": B},
            {"id": "e3", "kept": False, "url": C, "why": "same company failed: ..."}]


def verdict(reasoning, **kw):
    v = {"url": "https://acme.example/", "overall": "Yellow", "reasoning": reasoning, "red_flags": [],
         "yellow_flags": [], "green_flags": []}
    v.update(kw)
    return v


def run(reasoning, receipts=RECEIPTS, **kw):
    v = au.audit(verdict(reasoning, **kw), receipts)
    return v["reasoning"], v["audit"]


def texts(text):
    return [text[a:b] for a, b in au.sentences(text)]


class TestReasoning(unittest.TestCase):
    def test_all_cited_is_unchanged(self):
        r = "On its own site Acme presents itself as an EU leader [e2]. External sources show a 2025 fine [e1]."
        out, a = run(r)
        self.assertEqual(out, r)
        self.assertEqual((a["kept_sentences"], a["total_sentences"], a["removed"]), (2, 2, []))
        self.assertEqual(a["cited_ids"], ["e2", "e1"])
        self.assertEqual(a["note"], "")

    def test_removal_of_uncited_and_rejected(self):
        r = "Acme is alive [e1]. Acme is great. A court fined Acme [e3]. The fine stands [e1, e3]."
        out, a = run(r)
        self.assertEqual(out, "Acme is alive [e1]. The fine stands [e1, e3].")
        self.assertEqual(a["removed"], [{"sentence": "Acme is great.", "why": "cites no receipt"},
                                        {"sentence": "A court fined Acme [e3].",
                                         "why": "cites only rejected receipts: e3"}])
        self.assertEqual(a["rejected_ids"], ["e3"])
        self.assertEqual(a["cited_ids"], ["e1"])
        self.assertEqual(a["kept_sentences"], 2)
        self.assertIn("2 of 4 reasoning sentences kept, 2 removed", a["summary"])

    def test_unknown_ids(self):
        out, a = run("Acme was acquired twice [e99]. Acme is alive [E01]. Mixed [e3; e42].")
        self.assertEqual(out, "Acme is alive [E01].")
        self.assertEqual([x["why"] for x in a["removed"]],
                         ["cites only unknown ids: e99", "cites no kept receipt (rejected: e3; unknown: e42)"])
        self.assertEqual(a["unknown_ids"], ["e99", "e42"])
        self.assertEqual(a["cited_ids"], ["e1"])

    def test_kept_plus_unknown_stays(self):
        out, a = run("Acme was fined [e1, e77].")
        self.assertEqual(out, "Acme was fined [e1, e77].")
        self.assertEqual(a["unknown_ids"], ["e77"])

    def test_advice_exception(self):
        r = "Acme is alive [e1]. A buyer should ask Acme for its SOC 2 report before signing."
        out, a = run(r)
        self.assertEqual(out, r)
        self.assertEqual(a["advice"], ["A buyer should ask Acme for its SOC 2 report before signing."])
        self.assertEqual(a["removed"], [])
        for final in ("Ask Acme for the DPA and the subprocessor list.", "Next step: request the ISO 27001 certificate.",
                      "Shortlist after a reference call with a current customer."):
            out, a = run("Acme is alive [e1]. " + final)
            self.assertEqual(a["advice"], [final], final)

    def test_advice_only_when_final_short_and_uncited(self):
        out, a = run("A buyer should ask for references. Acme is alive [e1].")
        self.assertEqual(out, "Acme is alive [e1].")
        self.assertEqual(a["advice"], [])
        long = "A buyer should " + " ".join(["check"] * 50) + "."
        out, a = run("Acme is alive [e1]. " + long)
        self.assertEqual(out, "Acme is alive [e1].")
        out, a = run("Acme is alive [e1]. A buyer should read the ruling [e3].")
        self.assertEqual(out, "Acme is alive [e1].")
        self.assertEqual(a["removed"][0]["why"], "cites only rejected receipts: e3")
        out, a = run("Acme is alive [e1]. The website looks modern.")
        self.assertEqual(out, "Acme is alive [e1].")

    def test_empty_reasoning_fallback(self):
        r = "Acme is great. Acme is fast [e3]. A buyer should ask for references."
        out, a = run(r)
        self.assertEqual(out, r)
        self.assertEqual(a["note"], "audit would empty the reasoning")
        self.assertEqual(a["removed"], [])
        self.assertEqual([x["sentence"] for x in a["would_remove"]], ["Acme is great.", "Acme is fast [e3]."])
        self.assertIn("audit would empty the reasoning", a["summary"])
        out, a = run("Everything checks out.", receipts=[])
        self.assertEqual((out, a["note"]), ("Everything checks out.", "audit would empty the reasoning"))

    def test_no_reasoning(self):
        for r in ("", None, "   "):
            out, a = run(r)
            self.assertEqual(a["note"], "no reasoning")
            self.assertEqual(a["removed"], [])

    def test_only_advice_stays(self):
        out, a = run("A buyer should ask for the SOC 2 report.")
        self.assertEqual(out, "A buyer should ask for the SOC 2 report.")
        self.assertEqual((a["kept_sentences"], a["note"]), (1, ""))

    def test_newlines_and_bullets_are_kept_in_the_rebuild(self):
        out, _ = run("- Acme is alive [e1]\n- Acme is great\n- Fined in 2025 [e2]")
        self.assertEqual(out, "- Acme is alive [e1]\n- Fined in 2025 [e2]")
        out, _ = run("Acme is alive [e1].\n\nUncited paragraph.\n\nFined in 2025 [e2].")
        self.assertEqual(out, "Acme is alive [e1].\n\nFined in 2025 [e2].")


class TestSentences(unittest.TestCase):
    def test_citation_after_the_period_stays_with_its_sentence(self):
        self.assertEqual(texts("Acme was fined. [e1] The vendor appealed. [e2, e3]"),
                         ["Acme was fined. [e1]", "The vendor appealed. [e2, e3]"])
        self.assertEqual(texts("Acme was fined.[e1] It appealed (e2)."), ["Acme was fined.[e1]", "It appealed (e2)."])

    def test_leading_citation_of_a_lowercase_continuation(self):
        self.assertEqual(texts("Acme is alive [e1]. [e2] shows a fine."), ["Acme is alive [e1].", "[e2] shows a fine."])

    def test_no_split_on_abbreviations_initials_decimals_urls(self):
        for s in ("Acme Inc. was fined 2.5 million euros by the U.S. regulator, e.g. for tracking [e1].",
                  "Its founder J. Smith left acme.example in Oct. 2025 [e1].",
                  "Revenue grew vs. the prior year... according to the filing [e1]."):
            self.assertEqual(texts(s), [s])

    def test_question_exclamation_and_quotes(self):
        self.assertEqual(texts('Is it alive? Yes [e1]! It said "we are closing." Then it closed [e2].'),
                         ["Is it alive?", "Yes [e1]!", 'It said "we are closing."', "Then it closed [e2]."])

    def test_citation_forms(self):
        self.assertEqual(au.cited("x [e3, e7] y [e9; e10] (e11) [e12-e14] [see e2] [sic] (e.g. this)"),
                         ["e3", "e7", "e9", "e10", "e12", "e13", "e14", "e2", "e11"])


class TestFlags(unittest.TestCase):
    def test_flags_with_and_without_a_kept_receipt_on_their_url(self):
        flags = {
            "red_flags": [{"text": "Fined in 2025", "url": "http://www.news.example.org/acme-fined/", "receipts": ["e1"]}],
            "yellow_flags": [{"text": "Terms changed", "url": B, "receipts": ["e1"]},
                             {"text": "No ids", "url": A, "receipts": []}],
            "green_flags": [{"text": "Same name", "url": C, "receipts": ["e3", "e9"]},
                            {"text": "No url", "url": "", "receipts": ["e2"]}, "junk"]}
        v0 = verdict("Acme is alive [e1].", **flags)
        before = copy.deepcopy(v0)
        v = au.audit(v0, RECEIPTS)
        self.assertEqual(v0, before, "the input verdict is never mutated")
        for b in au.BUCKETS:
            self.assertEqual(v[b], flags[b], "flags are never dropped here")
        got = [(f["bucket"], f["text"], f["why"]) for f in v["audit"]["uncited_flags"]]
        self.assertEqual(got, [
            ("yellow_flags", "Terms changed", f"no kept receipt on the flag url (kept receipts on: {A})"),
            ("yellow_flags", "No ids", "no receipt ids"),
            ("green_flags", "Same name", "no kept receipt among its receipts (rejected: e3; unknown: e9)"),
            ("green_flags", "No url", "flag has no url"),
            ("green_flags", "", "flag is not an object")])
        self.assertIn("5 flag(s) without a kept receipt on their url", v["audit"]["summary"])


class TestNeverRaises(unittest.TestCase):
    def test_bad_data(self):
        self.assertIsNone(au.audit(None, RECEIPTS))
        self.assertEqual(au.audit("x", None), "x")
        for v, recs in ((verdict(None, red_flags=None, green_flags="x"), None),
                        (verdict(["Acme is alive [e1].", None]), "junk"),
                        (verdict(42, yellow_flags=[{"receipts": "e1, e2", "url": 3}]), [None, {"id": None}, 7,
                                                                                          {"id": 5, "kept": True}]),
                        ({}, [])):
            out = au.audit(v, recs)
            self.assertIn("audit", out)
            self.assertNotIn("error", out["audit"])

    def test_internal_error_keeps_the_reasoning(self):
        with mock.patch.object(au, "sentences", side_effect=RuntimeError("boom")):
            v = au.audit(verdict("Acme is great."), RECEIPTS)
        self.assertEqual(v["reasoning"], "Acme is great.")
        self.assertTrue(v["audit"]["error"].startswith("RuntimeError"))


if __name__ == "__main__":
    unittest.main()
