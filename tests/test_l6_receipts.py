"""Lane L6: receipt checks (app/check/receipts.py). Offline: model.set_fake("says_it") answers every model call.
Vendor domains use the reserved .example TLD (RFC 2606) so their stem is "acme", not "example"."""
import json, os, re, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import ledger, model  # noqa: E402
from app.check import receipts as rc  # noqa: E402

PAGE = ("Acme Analytics is shutting down. The Acme Analytics service will end on 31 March 2026 and customer data "
        "will be deleted 30 days later. We thank our customers for ten great years.")


def ev(i, url, quote, **kw):
    e = {"id": f"e{i}", "category": 4, "text": kw.pop("text", "Acme Analytics ends on 31 March 2026."), "url": url,
         "quote": quote, "date": "", "author_type": "trade_press", "severity": "critical", "polarity": "negative",
         "promise_link": "direct", "hypothesis": "is it ending", "source_title": "News", "round": 1}
    e.update(kw)
    return e


def research(evidence, sources, **ident):
    r = ledger.new_research("https://acme.example/")
    r["identity"].update({"product": "Acme Analytics", "vendor": "Acme", "legal_entity": "Acme Data GmbH",
                          "domains": ["acme.example"]})
    r["identity"].update(ident)
    r["evidence"], r["sources"] = evidence, sources
    return r


def src(text, title="News", published_date="", fetched_at="2026-11-27T15:10:00Z"):
    return {"text": text, "kind": "extract", "title": title, "published_date": published_date,
            "fetched_at": fetched_at}


def ids_in(user):
    return [json.loads(line)["id"] for line in user.splitlines() if line.startswith("{")]


class Calls:
    """A says_it fake: supports everything except the ids in `no`; omits the ids in `skip`; records the calls."""

    def __init__(self, no=(), skip=(), raise_exc=None):
        self.no, self.skip, self.raise_exc, self.users = set(no), set(skip), raise_exc, []

    def __call__(self, system, user):
        self.users.append(user)
        if self.raise_exc:
            raise self.raise_exc
        return {"results": [{"id": i, "supports": i not in self.no, "why": "quote is about pricing, not shutdown"}
                            for i in ids_in(user) if i not in self.skip]}


class Base(unittest.TestCase):
    def setUp(self):
        model.clear_fakes()
        self.fake = Calls()
        model.set_fake("says_it", self.fake)

    def tearDown(self):
        model.clear_fakes()

    def one(self, e, sources, **ident):
        out = rc.check(research([e], sources, **ident))
        self.assertEqual(len(out), 1)
        return out[0]


class TestNormalise(Base):
    def test_quotes_dashes_markdown_whitespace(self):
        self.assertEqual(rc.normalise("“It’s  **Acme**” \u2014 _the_\n\tbest… [docs](https://x.example.com/a)"),
                         "\"it's acme\" - the best... docs")

    def test_url_key(self):
        keys = {rc.url_key(u) for u in ("https://www.Example.org/a/b/", "http://example.org/a/b#top",
                                       "example.org/a/b")}
        self.assertEqual(keys, {"example.org/a/b"})
        self.assertNotEqual(rc.url_key("https://example.org/a?x=1"), rc.url_key("https://example.org/a"))


class TestResolves(Base):
    def test_pass_modulo_url_and_typography(self):
        e = ev(1, "http://www.news.example.org/acme-ends/#x",
               "The ACME Analytics service will end on 31 March 2026 \u2014 and customer data")
        page = PAGE.replace("2026 and", "2026 -- and").replace("2026 -- and", "2026 \u2013 and")
        r = self.one(e, {"https://news.example.org/acme-ends": src(page)})
        self.assertTrue(r["checks"]["resolves"], r["why"])
        self.assertTrue(r["kept"], r["why"])
        self.assertEqual(r["why"], "")

    def test_fail_quote_not_on_page(self):
        r = self.one(ev(1, "https://news.example.org/a", "Acme Analytics raised a new funding round"),
                     {"https://news.example.org/a": src(PAGE)})
        self.assertFalse(r["checks"]["resolves"])
        self.assertFalse(r["kept"])
        self.assertEqual(r["why"], "quote not found on the page")

    def test_missing_source(self):
        r = self.one(ev(1, "https://news.example.org/gone", "The Acme Analytics service will end"),
                     {"https://news.example.org/other": src(PAGE)})
        self.assertFalse(r["checks"]["resolves"])
        self.assertEqual(r["why"], "source text not retrieved")
        r = self.one(ev(1, "https://news.example.org/a", "The Acme Analytics service will end"),
                     {"https://news.example.org/a": src("")})
        self.assertEqual(r["why"], "source text not retrieved")

    def test_too_short(self):
        r = self.one(ev(1, "https://news.example.org/a", "shutting"), {"https://news.example.org/a": src(PAGE)})
        self.assertFalse(r["checks"]["resolves"])
        self.assertEqual(r["why"], "too short to verify")

    def test_ellipsis_in_order(self):
        for q in ("Acme Analytics is shutting down... customer data will be deleted 30 days later",
                  "Acme Analytics is shutting down \u2026 customer data will be deleted 30 days later",
                  "Acme Analytics is shutting down [...] customer data will be deleted 30 days later"):
            r = self.one(ev(1, "https://news.example.org/a", q), {"https://news.example.org/a": src(PAGE)})
            self.assertTrue(r["checks"]["resolves"], (q, r["why"]))

    def test_ellipsis_out_of_order_fails(self):
        q = "customer data will be deleted 30 days later ... Acme Analytics is shutting down"
        r = self.one(ev(1, "https://news.example.org/a", q), {"https://news.example.org/a": src(PAGE)})
        self.assertFalse(r["checks"]["resolves"])
        self.assertEqual(r["why"], "quote parts not found in order on the page")

    def test_ellipsis_part_too_short_fails(self):
        q = "Acme Analytics is shutting down ... great years"
        r = self.one(ev(1, "https://news.example.org/a", q), {"https://news.example.org/a": src(PAGE)})
        self.assertFalse(r["checks"]["resolves"])
        self.assertTrue(r["why"].startswith("too short to verify"), r["why"])

    def test_leading_and_trailing_ellipsis_are_not_parts(self):
        r = self.one(ev(1, "https://news.example.org/a", "...will end on 31 March 2026..."),
                     {"https://news.example.org/a": src(PAGE)})
        self.assertTrue(r["checks"]["resolves"], r["why"])


class TestSameCompany(Base):
    def test_vendor_domain_passes_without_anchor(self):
        r = self.one(ev(1, "https://docs.acme.example/eol", "service will end on 31 March"),
                     {"https://docs.acme.example/eol": src("The service will end on 31 March.")})
        self.assertTrue(r["checks"]["same_company"], r["why"])
        self.assertFalse(r["independent"])

    def test_anchor_in_text_passes(self):
        r = self.one(ev(1, "https://news.example.org/a", "will end on 31 March 2026"),
                     {"https://news.example.org/a": src(PAGE)})
        self.assertTrue(r["checks"]["same_company"], r["why"])
        self.assertTrue(r["independent"])
        self.assertEqual(r["domain"], "example.org")

    def test_legal_entity_without_suffix_and_title_count(self):
        text = "The service will end on 31 March 2026 for all plans."
        r = self.one(ev(1, "https://news.example.org/x", "will end on 31 March 2026"),
                     {"https://news.example.org/x": src(text, title="Acme Data to close its service")},
                     product="Zyxw", vendor="Qqqq")
        self.assertTrue(r["checks"]["same_company"], r["why"])

    def test_no_anchor_fails_with_reason(self):
        text = "Globex Analytics will end on 31 March 2026, the company said."
        r = self.one(ev(1, "https://news.example.org/globex", "will end on 31 March 2026"),
                     {"https://news.example.org/globex": src(text, title="Globex closes")})
        self.assertFalse(r["checks"]["same_company"])
        self.assertFalse(r["kept"])
        self.assertTrue(r["why"].startswith("same company failed: the page names none of "), r["why"])
        self.assertIn("'acme'", r["why"])

    def test_whole_word_only(self):
        text = "Acmeville Analytica will end on 31 March 2026, the town said."
        r = self.one(ev(1, "https://news.example.org/z", "will end on 31 March 2026"),
                     {"https://news.example.org/z": src(text, title="Town news")})
        self.assertFalse(r["checks"]["same_company"])

    def test_common_word_needs_second_anchor_or_domain(self):
        ident = dict(product="Beacon", vendor="Beacon", legal_entity="", domains=["beacon.example"])
        url, q = "https://news.example.org/b", "will end on 31 March 2026"
        alone = "The beacon will end on 31 March 2026, the harbour office said."
        r = self.one(ev(1, url, q), {url: src(alone, title="Harbour news")}, **ident)
        self.assertFalse(r["checks"]["same_company"])
        self.assertIn("common word 'beacon'", r["why"])
        with_domain = alone + " See beacon.example for details."
        r = self.one(ev(1, url, q), {url: src(with_domain, title="Harbour news")}, **ident)
        self.assertTrue(r["checks"]["same_company"], r["why"])
        two = "Beacon by Lumio Software will end on 31 March 2026."
        r = self.one(ev(1, url, q), {url: src(two, title="SaaS news")}, **dict(ident, parent="Lumio Software"))
        self.assertTrue(r["checks"]["same_company"], r["why"])

    def test_anchor_in_url_and_wayback_counts_as_vendor(self):
        url = "https://web.archive.org/web/20240101000000/https://acme.example/terms"
        r = self.one(ev(1, url, "customers may export their data", author_type="registry_archive"),
                     {url: src("Customers may export their data at any time.", title="Terms")})
        self.assertTrue(r["checks"]["same_company"], r["why"])
        self.assertEqual(r["domain"], "archive.org")
        self.assertFalse(r["independent"], "an archived vendor page is the vendor's own content")


class TestSaysIt(Base):
    URL = "https://news.example.org/a"

    def test_pass_and_fail(self):
        self.fake.no = {"e2"}
        evs = [ev(1, self.URL, "will end on 31 March 2026"),
               ev(2, self.URL, "Acme Analytics is shutting down", text="Acme raised prices by 40%.")]
        a, b = rc.check(research(evs, {self.URL: src(PAGE)}))
        self.assertTrue(a["checks"]["says_it"] and a["kept"])
        self.assertFalse(b["checks"]["says_it"] or b["kept"])
        self.assertEqual(b["why"], "says it failed: quote is about pricing, not shutdown")
        self.assertIn("Acme raised prices by 40%.", self.fake.users[0])

    def test_missing_id_is_no_verdict(self):
        self.fake.skip = {"e1"}
        r = self.one(ev(1, self.URL, "will end on 31 March 2026"), {self.URL: src(PAGE)})
        self.assertFalse(r["checks"]["says_it"])
        self.assertEqual(r["why"], "no verdict")

    def test_only_receipts_passing_both_checks_are_asked_in_chunks_of_50(self):
        evs = [ev(i, self.URL, "will end on 31 March 2026") for i in range(1, 121)]
        evs.append(ev(121, self.URL, "not on the page at all, really"))
        evs.append(ev(122, "https://other.example.org/z", "will end on 31 March 2026"))
        out = rc.check(research(evs, {self.URL: src(PAGE), "https://other.example.org/z":
                                      src("Globex will end on 31 March 2026.", title="Globex")}))
        asked = [i for u in self.fake.users for i in ids_in(u)]
        self.assertEqual(sorted(len(ids_in(u)) for u in self.fake.users), [20, 50, 50])
        self.assertEqual(len(asked), 120)
        self.assertNotIn("e121", asked)
        self.assertNotIn("e122", asked)
        self.assertEqual(sum(r["kept"] for r in out), 120)
        self.assertEqual(out[120]["why"], "quote not found on the page")
        self.assertFalse(out[121]["checks"]["says_it"])
        self.assertTrue(out[121]["why"].startswith("same company failed"))

    def test_model_error_never_raises(self):
        model.set_fake("says_it", Calls(raise_exc=model.ModelError("boom")))
        r = self.one(ev(1, self.URL, "will end on 31 March 2026"), {self.URL: src(PAGE)})
        self.assertFalse(r["checks"]["says_it"] or r["kept"])
        self.assertTrue(r["why"].startswith("no verdict"), r["why"])


class TestDated(Base):
    URL = "https://news.example.org/a"

    def test_own_date_passes(self):
        for d in ("2025", "2025-03", "2025-03-14"):
            r = self.one(ev(1, self.URL, "will end on 31 March 2026", date=d), {self.URL: src(PAGE)})
            self.assertTrue(r["checks"]["dated"], d)
            self.assertEqual(r["date"], d)

    def test_filled_from_published_date(self):
        r = self.one(ev(1, self.URL, "will end on 31 March 2026"),
                     {self.URL: src(PAGE, published_date="Tue, 14 Oct 2025 09:00:00 GMT")})
        self.assertTrue(r["checks"]["dated"])
        self.assertEqual(r["date"], "2025-10-14")

    def test_fetch_date_does_not_count_and_never_rejects_alone(self):
        r = self.one(ev(1, self.URL, "will end on 31 March 2026"),
                     {self.URL: src(PAGE, published_date="2026-11-27T08:00:00Z")})
        self.assertFalse(r["checks"]["dated"])
        self.assertEqual(r["date"], "")
        self.assertTrue(r["kept"], "dated never rejects alone")


class TestDup(Base):
    def test_same_quote_on_another_url(self):
        u1, u2, u3 = "https://news.example.org/a", "https://blog.example.net/b", "https://news.example.org/a#again"
        q = "The Acme Analytics service will end on 31 March 2026"
        evs = [ev(1, u1, q), ev(2, u2, "\u201cthe acme analytics service will end on 31 March 2026\u201d"),
               ev(3, u3, q), ev(4, u2, "We thank our customers for ten great years")]
        out = rc.check(research(evs, {u1: src(PAGE), u2: src(PAGE)}))
        self.assertEqual([r["dup_of"] for r in out], [None, "e1", None, None])
        self.assertTrue(all(r["kept"] for r in out), "a dup is still kept; it only marks corroboration")

    def test_same_first_120_chars(self):
        long_page = ("Acme " + "lorem ipsum dolor sit amet " * 6 + "and then version one. ") * 1
        tail_a, tail_b = "first ending here", "second ending there"
        u1, u2 = "https://a.example.org/1", "https://b.example.org/2"
        evs = [ev(1, u1, long_page + tail_a), ev(2, u2, long_page + tail_b)]
        out = rc.check(research(evs, {u1: src(long_page + tail_a), u2: src(long_page + tail_b)}))
        self.assertGreater(len(rc.normalise(long_page)), 120)
        self.assertEqual(out[1]["dup_of"], "e1")

    def test_not_a_dup_of_a_rejected_receipt(self):
        u1, u2 = "https://news.example.org/a", "https://blog.example.net/b"
        q = "The Acme Analytics service will end on 31 March 2026"
        self.fake.no = {"e1"}
        out = rc.check(research([ev(1, u1, q), ev(2, u2, q)], {u1: src(PAGE), u2: src(PAGE)}))
        self.assertEqual([r["dup_of"] for r in out], [None, None])


class TestShapeAndBadData(Base):
    def test_receipt_shape(self):
        r = self.one(ev(1, "https://news.example.org/a", "will end on 31 March 2026"),
                     {"https://news.example.org/a": src(PAGE)})
        for k in ("checks", "kept", "why", "domain", "independent", "dup_of", "id", "quote", "url", "category"):
            self.assertIn(k, r)
        self.assertEqual(set(r["checks"]), {"resolves", "same_company", "says_it", "dated"})
        self.assertEqual(ledger.validate_evidence(r), [])

    def test_research_evidence_is_not_mutated(self):
        e = ev(1, "https://news.example.org/a", "will end on 31 March 2026")
        r = research([e], {"https://news.example.org/a": src(PAGE, published_date="2025-01-02")})
        rc.check(r)
        self.assertEqual(r["evidence"][0]["date"], "")
        self.assertNotIn("checks", r["evidence"][0])

    def test_vendor_self_is_not_independent(self):
        r = self.one(ev(1, "https://news.example.org/a", "will end on 31 March 2026", author_type="vendor_self"),
                     {"https://news.example.org/a": src(PAGE)})
        self.assertFalse(r["independent"])

    def test_never_raises(self):
        for bad in (None, [], "x", {}, {"evidence": None, "sources": None, "identity": None},
                    {"evidence": [None, 3, {"id": None, "quote": None, "url": None, "date": 7}],
                     "sources": {"https://a.example.org": None, None: {"text": 3}}, "identity": {"domains": [None, 5],
                     "former_names": "Old Acme"}, "url": None},
                    research([{"id": "e1", "quote": ["list"], "url": "https://a.example.org/x", "date": "2025-13-40"}],
                             {"https://a.example.org/x": {"text": 42, "published_date": 2025}})):
            out = rc.check(bad)
            self.assertIsInstance(out, list)
            for r in out:
                self.assertFalse(r["kept"])
                self.assertTrue(r["why"])


if __name__ == "__main__":
    unittest.main()
