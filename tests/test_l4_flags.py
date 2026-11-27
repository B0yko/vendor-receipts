"""evalkit.flags (matcher, extra check, error classifier, grounding, coverage) and its report section, with
model.set_fake only (no model, no network); invented Acme data."""
import io, json, os, re, shutil, sys, tempfile, unittest
from contextlib import redirect_stdout

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
from app import ledger, model  # noqa: E402
from evalkit import flags, labels, report  # noqa: E402
from tests.test_l4_report import read, write_labels  # noqa: E402

ROW = {"id": "7", "key": "acme7.example.com", "color": "Red", "reasoning": "Acme Seven ended the product in 2025.",
       "green_flags": "", "yellow_flags": "1. Small team\n2) One maintainer",
       "red_flags": "- Product discontinued in 2025\n- Domain parked"}


def flag(text, quote="q", url="https://acme7.example.com/news"):
    return {"text": text, "url": url, "quote": quote, "category": 4, "basis": "confirmed", "receipts": ["e1"]}


PRED = {"url": "https://acme7.example.com", "overall": "Yellow", "reasoning": "Alive but small [e1].",
        "red_flags": [], "yellow_flags": [flag("End of life announced for 2025"), flag("Team of three")],
        "green_flags": [flag("Pricing is public")],
        "coverage": {str(k): ("open" if k == 24 else "found") for k in ledger.CATEGORIES}}


def fake_match(system, user):
    assert "[t1] (red) Product discontinued in 2025" in user and "[o1] (yellow) End of life" in user, user
    return {"items": [{"id": "t1", "match": "found", "ours": ["o1"], "why": "same end of life"},
                      {"id": "t3", "match": "partly", "ours": ["o2", "o99"], "why": "size only"},
                      {"id": "t4", "match": "partly", "ours": [], "why": "no flag named"},
                      {"id": "t2", "match": "missed", "ours": [], "why": "nothing on the domain"}]}


def fake_extra(system, user):
    assert "[o3] (green) Pricing is public" in user and "Analyst's reasoning" in user
    return {"results": [{"id": "o3", "backed": True, "material": True, "contradicted": False, "why": "public price"}]}


def fake_classify(system, user):
    ids = re.findall(r"^\[(\w+)\]", user.split("Errors to classify:")[1].split("Digest")[0], re.M)
    assert "Digest of what the agent retrieved" in user and "[e1] cat 4" in user
    return {"errors": [{"id": i, "class": "research_gap" if i != "color" else "judge_error", "why": "w"} for i in ids]}


def write_run(run_dir, url="https://acme7.example.com"):
    d = os.path.join(run_dir, ledger.slug(url))
    os.makedirs(d, exist_ok=True)
    r = ledger.new_research(url)
    r["identity"].update(product="Acme Seven", vendor="Acme")
    r["evidence"] = [{"id": "e1", "category": 4, "text": "End of life notice", "quote": "we will shut down",
                      "url": url + "/news", "polarity": "negative", "severity": "critical", "source_title": "News"}]
    r["sources"] = {url + "/news": {"text": "We will shut down in 2025. " * 50, "kind": "fetch", "title": "News"}}
    with open(os.path.join(d, "research.json"), "w") as f:
        json.dump(r, f)
    with open(os.path.join(d, "receipts.json"), "w") as f:
        json.dump([{"id": "e1", "kept": True}, {"id": "e2", "kept": False}, {"id": "e3", "kept": True}], f)
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.addCleanup(model.clear_fakes)
        model.set_fake("match", fake_match)
        model.set_fake("extra", fake_extra)
        model.set_fake("classify", fake_classify)


class UnitTest(Base):
    def test_split_items(self):
        self.assertEqual(flags.split_items("- a\n* b\n\u2022 c\n1. d\n2) e\n\n"), ["a", "b", "c", "d", "e"])
        self.assertEqual(flags.split_items("a; b ;c"), ["a", "b", "c"])
        self.assertEqual(flags.split_items(" none "), [])
        self.assertEqual(flags.split_items(None), [])
        items = flags.their_items(ROW)
        self.assertEqual([(i["id"], i["bucket"]) for i in items],
                         [("t1", "red"), ("t2", "red"), ("t3", "yellow"), ("t4", "yellow")])

    def test_match(self):
        items, extra, err = flags.match(ROW, PRED)
        self.assertIsNone(err)
        m = {i["id"]: i for i in items}
        self.assertEqual(m["t1"]["match"], "found")
        self.assertEqual(m["t1"]["our_text"], "End of life announced for 2025")
        self.assertEqual(m["t3"]["ours"], ["o2"])                 # unknown id o99 dropped
        self.assertEqual(m["t4"]["match"], "missed")              # partly without a flag of ours is missed
        self.assertEqual(m["t2"]["match"], "missed")
        self.assertEqual([f["id"] for f in extra], ["o3"])

    def test_match_without_our_flags_needs_no_model(self):
        model.set_fake("match", lambda s, u: self.fail("no call expected"))
        items, extra, err = flags.match(ROW, dict(PRED, yellow_flags=[], green_flags=[]))
        self.assertEqual({i["match"] for i in items}, {"missed"})
        self.assertEqual((extra, err), ([], None))

    def test_model_error_is_not_a_miss(self):
        def boom(s, u):
            raise model.ModelError("limit")
        model.set_fake("match", boom)
        items, extra, err = flags.match(ROW, PRED)
        self.assertEqual({i["match"] for i in items}, {"error"})
        self.assertIn("limit", err)

    def test_extra_status_from_booleans(self):
        model.set_fake("extra", lambda s, u: {"results": [{"id": "o3", "backed": True, "material": False,
                                                           "contradicted": False, "why": "trivia"}]})
        out, err = flags.check_extra(ROW, [dict(flag("x"), id="o3", bucket="green")])
        self.assertEqual(out[0]["status"], "noise")

    def test_digest_capped(self):
        r = ledger.new_research("https://acme7.example.com")
        r["sources"] = {f"https://acme7.example.com/{i}": {"text": "x" * 500, "title": f"T{i}"} for i in range(500)}
        d = flags.digest(r, cap=2000)
        self.assertLessEqual(len(d), 2000 + 20)
        self.assertTrue(d.endswith("[digest capped]"))
        self.assertEqual(flags.digest(None), "(no research.json)")

    def test_grounding_coverage(self):
        self.assertEqual(flags.grounding([{"kept": True}, {"kept": False}]), {"kept": 1, "total": 2, "share": 0.5})
        self.assertIsNone(flags.grounding(None))
        self.assertEqual(flags.coverage(PRED, None), {"with_status": 23, "of": 24})
        r = ledger.new_research("x")
        r["matrix"]["1"]["status"] = "found"
        self.assertEqual(flags.coverage({}, r), {"with_status": 1, "of": 24})

    def test_analyze_product(self):
        run = os.path.join(self.d, "runs", "r1")
        write_run(run)
        res = flags.analyze_product(ROW, PRED, run)
        self.assertEqual(res["grounding"], {"kept": 2, "total": 3, "share": 2 / 3})
        self.assertEqual(res["coverage"]["with_status"], 23)
        self.assertEqual([f["status"] for f in res["extra"]], ["verified"])
        self.assertEqual({e["id"]: e["class"] for e in res["errors"]},
                         {"color": "judge_error", "t2": "research_gap", "t4": "research_gap"})
        self.assertEqual(res["errors_model"], [])

    def test_no_research_no_classify_call(self):
        model.set_fake("classify", lambda s, u: self.fail("no call expected"))
        res = flags.analyze_product(ROW, PRED, os.path.join(self.d, "none"))
        self.assertEqual({e["why"] for e in res["errors"]}, {"no research.json"})


class ReportSectionTest(Base):
    def test_report_with_flags(self):
        csvp = os.path.join(self.d, "labels.csv")
        write_labels(csvp)
        dev = labels.load(csvp, split="dev")
        run = os.path.join(self.d, "runs", "r1")
        preds = os.path.join(self.d, "p.jsonl")
        with open(preds, "w") as f:
            for r in dev:
                url = f"https://acme{r['id']}.example.com/"
                write_run(run, url)
                f.write(json.dumps(dict(PRED, url=url, overall=r["color"])) + "\n")
        # synthetic labels have 2 green items and 2 yellow items per row; a generic fake matcher
        model.set_fake("match", lambda s, u: {"items": [{"id": "t1", "match": "found", "ours": ["o1"]},
                                                        {"id": "t2", "match": "partly", "ours": ["o2"]}]})
        out, save, log = (os.path.join(self.d, n) for n in ("r.md", "s.json", "log.md"))
        with redirect_stdout(io.StringIO()) as buf:
            rc = report.main(["--labels", csvp, "--preds", preds, "--split", "dev", "--run-dir", run, "--out", out,
                              "--save", save, "--log", log])
        self.assertEqual(rc, 0)
        md, saved = read(out), json.loads(read(save))
        self.assertIn("## Flags and errors", md)
        self.assertIn("Their flag items: 28; found 7/28 (25%), partly 7, missed 14", md)
        self.assertIn("Our extra flags: 7; verified 7, noise 0", md)
        self.assertIn("Grounding (receipts kept): 14/21 (67%) over 7 products", md)
        self.assertIn("mean 23.0/24", md)
        self.assertIn("### Per item", md)
        self.assertIn("their flags found 7+7 partly of 28", buf.getvalue())
        self.assertEqual(saved["flags"]["totals"]["found"], 7)
        self.assertNotIn("one maintainer", json.dumps(saved).lower())   # their item text never saved
        self.assertNotIn("one maintainer", md.lower())

    def test_holdout_has_no_item_lines(self):
        csvp = os.path.join(self.d, "labels.csv")
        write_labels(csvp)
        hold = labels.load(csvp, split="holdout")
        preds = os.path.join(self.d, "p.jsonl")
        with open(preds, "w") as f:
            for r in hold:
                f.write(json.dumps(dict(PRED, url=f"https://acme{r['id']}.example.com/", overall=r["color"])) + "\n")
        model.set_fake("match", lambda s, u: {"items": [{"id": "t1", "match": "found", "ours": ["o1"], "why": "W1"}]})
        out, save = os.path.join(self.d, "r.md"), os.path.join(self.d, "s.json")
        with redirect_stdout(io.StringIO()):
            report.main(["--labels", csvp, "--preds", preds, "--split", "holdout", "--out", out, "--save", save,
                         "--log", os.path.join(self.d, "log.md")])
        md, saved = read(out), read(save)
        self.assertIn("## Flags and errors", md)
        self.assertNotIn("### Per item", md)
        self.assertNotIn("W1", md + saved)
        self.assertIn('"counts"', saved)

    def test_no_model_flag(self):
        for p in ("match", "extra", "classify"):
            model.set_fake(p, lambda s, u: self.fail("no model call with --no-model"))
        csvp = os.path.join(self.d, "labels.csv")
        write_labels(csvp)
        preds = os.path.join(self.d, "p.jsonl")
        with open(preds, "w") as f:
            f.write(json.dumps(dict(PRED, url="https://acme1.example.com/")) + "\n")
        out = os.path.join(self.d, "r.md")
        with redirect_stdout(io.StringIO()):
            report.main(["--labels", csvp, "--preds", preds, "--split", "dev", "--out", out, "--no-model",
                         "--log", os.path.join(self.d, "log.md")])
        self.assertIn("Flags not compared (--no-model)", read(out))


if __name__ == "__main__":
    unittest.main()
