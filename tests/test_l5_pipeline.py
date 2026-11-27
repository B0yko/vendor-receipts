"""Lane L5: writer, trace, pipeline, batch runner and CLI. Offline; check and judge are stubbed (lanes L6, L3)."""
import contextlib, csv, io, json, os, shutil, sys, tempfile, unittest
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
from app import cli, ledger, pipeline, run  # noqa: E402
from app.out import trace, writer  # noqa: E402

FX = os.path.join(ROOT, "tests", "fixtures", "research")


def fixture(slug):
    with open(os.path.join(FX, slug + ".json")) as f:
        return json.load(f)


def stub_check(research):
    """Every evidence item kept, plus one rejected same-name receipt."""
    out = []
    for e in research["evidence"]:
        d = ledger.registrable(e["url"])
        out.append(dict(e, checks={"resolves": True, "same_company": True, "says_it": True, "dated": bool(e["date"])},
                        kept=True, why="", domain=d, independent=d not in research["identity"]["domains"],
                        dup_of=None))
    out.append({"id": "e99", "category": 1, "text": "A same-name company", "url": "https://other.example.net/acme",
                "quote": "Acme Ledger Inc of Ohio", "date": "", "author_type": "aggregator", "severity": "minor",
                "polarity": "neutral", "checks": {"resolves": True, "same_company": False, "says_it": True,
                                                  "dated": False},
                "kept": False, "why": "same name, different company", "domain": "example.net", "independent": True,
                "dup_of": None})
    return out


def flag(rc, text, basis="confirmed"):
    return {"text": text, "category": rc["category"], "severity": rc["severity"], "url": rc["url"],
            "quote": rc["quote"], "date": rc["date"], "author": rc["author_type"], "corroboration": "one",
            "basis": basis, "receipts": [rc["id"]]}


def stub_judge(research, receipts):
    kept = {r["id"]: r for r in receipts if r["kept"]}
    g = [flag(kept["e2"], "Dated releases continue")]
    y = [flag(kept["e3"], "Caveat with | a pipe\nand a newline")]
    return {"url": research["url"], "overall": "Yellow", "reliability": "medium", "reliability_why": "one source",
            "green_flags": g, "yellow_flags": y, "red_flags": [], "rejected": [], "reasoning": "Alive [e2]; caveat [e3].",
            "blind_spots": ["hosting", "certifications"], "buyer_next_step": "Ask for escrow.",
            "vendor_questions": ["Q1?", "Q2?", "Q3?"], "would_flip": "A second maintainer.",
            "guards": ["guard 4: dropped 1 flag without a kept receipt"], "model": "stub"}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for name, fn in (("_check", stub_check), ("_judge", stub_judge)):
            p = mock.patch.object(pipeline, name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)
        self.err = io.StringIO()
        r = contextlib.redirect_stderr(self.err)
        r.__enter__()
        self.addCleanup(r.__exit__, None, None, None)

    def research_fn(self, url, deadline=None):
        return run.fake_research_fn(url, deadline)


class TestWriter(Base):
    def verdicts(self):
        return [stub_judge(fixture(s), stub_check(fixture(s))) | {"product": p, "vendor": "Acme", "trace": f"t/{s}"}
                for s, p in (("example-org", "Acme Notes"), ("example-com", "Acme Ledger"))]

    def test_csv_columns_flags_and_order(self):
        path = os.path.join(self.dir, "v.csv")
        self.assertEqual(writer.write_csv(self.verdicts(), path), (path, os.path.join(self.dir, "v.full.csv")))
        with open(path, newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0], ["url", "product", "vendor", "overall", "green_flags", "yellow_flags", "red_flags",
                                   "reasoning"])
        self.assertEqual([r[0] for r in rows[1:]], ["https://example.org", "https://example.com"])
        d = dict(zip(rows[0], rows[1]))
        self.assertEqual(d["green_flags"], "Dated releases continue (https://example.org/releases)")
        self.assertEqual(d["yellow_flags"], "Caveat with | a pipe and a newline "
                                            "(https://dev.example.net/acme-notes-maintainer)")
        self.assertEqual(d["red_flags"], "")
        with open(os.path.join(self.dir, "v.full.csv"), newline="") as f:
            full = list(csv.DictReader(f))
        self.assertEqual(list(full[0])[8:], ["reliability", "reliability_why", "blind_spots", "buyer_next_step",
                                             "vendor_questions", "would_flip", "trace"])
        self.assertEqual(full[0]["vendor_questions"], "Q1?; Q2?; Q3?")
        self.assertEqual(full[0]["blind_spots"], "hosting; certifications")
        self.assertEqual(full[1]["trace"], "t/example-com")

    def test_two_flags_join_and_jsonl_call(self):
        v = self.verdicts()[0]
        v["green_flags"] = v["green_flags"] * 2
        self.assertEqual(writer.fmt_flags(v["green_flags"]).count("; "), 1)
        line = json.loads(writer.jsonl_line(v))
        self.assertEqual(line["call"], "Yellow")
        self.assertNotIn("\n", writer.jsonl_line(v))


class TestTrace(Base):
    def test_sections_and_24_matrix_rows(self):
        r = fixture("example-com")
        rc = stub_check(r)
        v = stub_judge(r, rc) | {"run_id": "r1", "seconds": 3.0, "mode": "fake"}
        md = trace.render(r, rc, v)
        for h in ("# Trace:", "## 1 Identity and anchors", "## 2 Matrix (24 categories)", "## 3 Claims ledger",
                  "## 4 Decision path", "### Guard log", "### Reasoning", "### Would flip", "### Blind spots",
                  "### Vendor questions", "## 5 Rejected receipts", "### Sources failed", "### Limits",
                  "## Research rounds", "## Appendix: kept receipts"):
            self.assertIn(h, md)
        matrix = md.split("## 2 Matrix (24 categories)")[1].split("## 3")[0]
        rows = [ln for ln in matrix.splitlines() if ln.startswith("| ") and not ln.startswith("| #")]
        self.assertEqual(len(rows), 24)
        self.assertTrue(rows[23].startswith("| 24 | Platform dependency |"))
        self.assertIn("e99", md.split("## 5")[1])                      # rejected receipt with its reason
        self.assertIn("same name, different company", md)
        self.assertIn("> Version 2.4.0 (2026-10-14): faster search and CSV export.", md)  # full quote in appendix
        self.assertIn("Caveat with \\| a pipe and a newline", md)        # table-safe text
        self.assertIn("guard 4: dropped", md)

    def test_render_survives_an_empty_research(self):
        md = trace.render(ledger.new_research("https://example.com"), [], {"url": "https://example.com"})
        self.assertIn("Categories still open: 1, 2, 3", md)


class TestPipeline(Base):
    def test_writes_files_and_fields(self):
        rd = os.path.join(self.dir, "runs", "r7", "example-com")
        v = pipeline.check_vendor("https://example.com", rd, research_fn=self.research_fn)
        for f in ("identity.json", "research.json", "receipts.json", "verdict.json", "trace.md"):
            self.assertTrue(os.path.exists(os.path.join(rd, f)), f)
        self.assertIsNone(v["error"])
        self.assertEqual((v["run_id"], v["trace"], v["call"]), ("r7", os.path.join(rd, "trace.md"), "Yellow"))
        self.assertEqual(v["mode"], pipeline.model.mode())
        self.assertEqual(v["product"], "Acme Ledger")
        self.assertEqual(len(v["coverage"]), 24)
        self.assertEqual([x["url"] for x in v["reasons"]],
                         ["https://news.example.net/acme-ledger-review", "https://example.com/changelog"])
        self.assertEqual(ledger.validate_verdict(v), [])
        with open(os.path.join(rd, "verdict.json")) as f:
            self.assertEqual(json.load(f)["seconds"], v["seconds"])

    def test_research_failure_is_an_error_verdict(self):
        def boom(url, deadline=None):
            raise TimeoutError("tavily down")
        rd = os.path.join(self.dir, "runs", "r8", "example-com")
        v = pipeline.check_vendor("https://example.com", rd, research_fn=boom)
        self.assertEqual((v["overall"], v["call"], v["reliability"]), ("Yellow", "Yellow", "low"))
        self.assertEqual(v["reasoning"],
                         "The research did not complete (TimeoutError: tavily down); missing evidence is no signal.")
        self.assertEqual(v["reasons"], [{"text": v["reasoning"], "url": "https://example.com", "quote": ""}])
        self.assertEqual(v["error"], "TimeoutError: tavily down")
        self.assertEqual(ledger.validate_verdict(v), [])
        self.assertTrue(os.path.exists(os.path.join(rd, "trace.md")))
        self.assertIn("Error: TimeoutError", open(os.path.join(rd, "trace.md")).read())

    def test_judge_failure_keeps_research(self):
        rd = os.path.join(self.dir, "runs", "r9", "example-org")
        with mock.patch.object(pipeline, "_judge", side_effect=ValueError("bad json")):
            v = pipeline.check_vendor("https://example.org", rd, research_fn=self.research_fn)
        self.assertEqual(v["error"], "ValueError: bad json")
        self.assertEqual(v["product"], "Acme Notes")
        self.assertEqual(v["coverage"]["6"], "found")
        self.assertTrue(os.path.exists(os.path.join(rd, "research.json")))
        self.assertTrue(os.path.exists(os.path.join(rd, "receipts.json")))


class TestRun(Base):
    def cases_file(self, text, name="cases.jsonl"):
        p = os.path.join(self.dir, name)
        with open(p, "w") as f:
            f.write(text)
        return p

    def test_read_cases_three_formats(self):
        j = run.read_cases(self.cases_file('{"url": "https://example.com", "id": "a"}\n{"url": "https://example.org"}\n'))
        self.assertEqual([(c["id"], c["slug"]) for c in j], [("a", "example-com"), ("example-org", "example-org")])
        c = run.read_cases(self.cases_file("name,URL\nAcme,https://example.com\nB,https://www.example.com/\n", "c.csv"))
        self.assertEqual([x["slug"] for x in c], ["example-com", "example-com-2"])
        t = run.read_cases(self.cases_file("# blind\nhttps://example.org\n\nexample.com\n", "c.txt"))
        self.assertEqual([x["url"] for x in t], ["https://example.org", "example.com"])
        self.assertEqual([x["id"] for x in run.select(j, "https://www.example.org/")], ["example-org"])
        self.assertEqual([x["id"] for x in run.select(j, "a")], ["a"])

    def test_batch_writes_lines_csv_and_manifest(self):
        cases = self.cases_file('{"url": "https://example.org"}\n{"url": "https://example.com"}\n')
        out = os.path.join(self.dir, "o", "v.jsonl")
        runs = os.path.join(self.dir, "runs")
        env = {"RUN_ID": "t1", "RECEIPTS_FAKE": "1", "LLM_ORDER": "x", "LLM_X_EXTRA": '{"reasoning": {"effort": "high"}}',
               "LLM_X_KEY": "sk-secret-never", "LLM_X_URL": "http://127.0.0.1:9/v1", "LLM_X_MODEL": "m"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(run.main(["--cases", cases, "--out", out, "--runs-dir", runs, "--concurrency", "2",
                                       "--wall-cap", "30"]), 0)
        lines = [json.loads(x) for x in open(out)]
        self.assertEqual(sorted(x["url"] for x in lines), ["https://example.com", "https://example.org"])
        self.assertTrue(all(x["call"] == x["overall"] and x["mode"] == "fake" for x in lines))
        with open(os.path.join(self.dir, "o", "v.csv"), newline="") as f:
            self.assertEqual([r["url"] for r in csv.DictReader(f)], ["https://example.org", "https://example.com"])
        self.assertTrue(os.path.exists(os.path.join(self.dir, "o", "v.full.csv")))
        mpath = os.path.join(runs, "t1", "manifest.json")
        text = open(mpath).read()
        self.assertNotIn("sk-secret-never", text)
        m = json.loads(text)
        self.assertEqual(m["llm_extra"]["LLM_X_EXTRA"], '{"reasoning": {"effort": "high"}}')
        self.assertTrue(all(k.endswith("_EXTRA") for k in m["llm_extra"]))
        self.assertEqual((m["wall_cap"], m["concurrency"], m["mode"], m["model"]), (30.0, 2, "fake", "fake"))
        self.assertEqual([c["slug"] for c in m["cases"]], ["example-org", "example-com"])
        self.assertTrue(all(c["overall"] == "Yellow" and c["error"] is None for c in m["cases"]))
        self.assertIn("commit", m["git"])
        self.assertTrue(os.path.exists(os.path.join(runs, "t1", "example-com", "trace.md")))
        self.assertRegex(self.err.getvalue(), r"\[2/2\] example-(com|org) Yellow \d+s")

    def test_only_replaces_selected_lines(self):
        cases = self.cases_file('{"url": "https://example.org"}\n{"url": "https://example.com"}\n')
        out, runs = os.path.join(self.dir, "v.jsonl"), os.path.join(self.dir, "runs")
        with mock.patch.dict(os.environ, {"RUN_ID": "t2", "RECEIPTS_FAKE": "1"}):
            run.main(["--cases", cases, "--out", out, "--runs-dir", runs])
            run.main(["--cases", cases, "--out", out, "--runs-dir", runs, "--only", "example-com"])
        self.assertEqual(sorted(json.loads(x)["url"] for x in open(out)), ["https://example.com", "https://example.org"])
        with open(os.path.join(self.dir, "v.csv"), newline="") as f:
            self.assertEqual([r["url"] for r in csv.DictReader(f)], ["https://example.org", "https://example.com"])

    def test_missing_fixture_is_an_error_line_not_a_crash(self):
        cases = self.cases_file("https://example.net\n", "c.txt")
        out = os.path.join(self.dir, "v.jsonl")
        with mock.patch.dict(os.environ, {"RUN_ID": "t3", "RECEIPTS_FAKE": "1"}):
            run.main(["--cases", cases, "--out", out, "--runs-dir", os.path.join(self.dir, "runs")])
        line = json.loads(open(out).read())
        self.assertEqual((line["overall"], line["url"]), ("Yellow", "https://example.net"))
        self.assertIn("FileNotFoundError", line["error"])


class TestCli(Base):
    def test_run_prints_color_and_trace(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"RUN_ID": "c1", "RECEIPTS_FAKE": "1"}), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["run", "https://example.com", "--runs-dir", os.path.join(self.dir, "runs")]), 0)
        s = out.getvalue()
        self.assertIn("Yellow", s)
        self.assertIn(os.path.join(self.dir, "runs", "c1", "example-com", "trace.md"), s)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "runs", "c1", "one.jsonl")))


if __name__ == "__main__":
    unittest.main()
