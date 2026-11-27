"""Lane L5: writer, trace, pipeline, batch runner and CLI. Offline; check and judge are stubbed (lanes L6, L3)."""
import contextlib, csv, io, json, os, shutil, sys, tempfile, time, unittest
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


def absent_disconfirm(research, receipts, deadline=None):
    """Today's main: app.check.disconfirm does not exist yet (and the real one must never run in these tests)."""
    raise ModuleNotFoundError("No module named 'app.check.disconfirm'", name="app.check.disconfirm")


def stub_disconfirm(research, receipts, deadline=None):
    """Like lane L6: one target (e3) survives with two new receipts, one kept and one rejected."""
    new = [{"id": "e10", "category": 6, "text": "No second maintainer announced since", "url": "https://dev.example.net/x",
            "quote": "still a one-person shop", "date": "2026-11-01", "author_type": "trade_press",
            "severity": "moderate", "polarity": "negative", "hypothesis": "disconfirmation of e3: a second engineer?",
            "checks": {"resolves": True, "same_company": True, "says_it": True, "dated": True}, "kept": True, "why": "",
            "domain": "example.net", "independent": True, "dup_of": None},
           {"id": "e11", "category": 6, "text": "A same-name shop hired", "url": "https://other.example.net/y",
            "quote": "Acme hires", "date": "", "author_type": "aggregator", "severity": "minor", "polarity": "neutral",
            "checks": {"resolves": True, "same_company": False, "says_it": True, "dated": False}, "kept": False,
            "why": "same name, different company", "domain": "example.net", "independent": True, "dup_of": None}]
    record = {"targets": ["e3"],
              "queries": [{"target": "e3", "tool": "search", "query": '"Acme" second engineer', "results": [1, 2]},
                          {"target": "e3", "tool": "news", "query": '"Acme" hires', "error": "timeout"}],
              "outcomes": [{"target": "e3", "outcome": "survived", "why": "nothing newer says otherwise",
                            "evidence": ["e10", "e11"]}],
              "seconds": 12.5, "deadline_seen": deadline is not None}
    research["evidence"] = research["evidence"] + [{k: x[k] for k in x if k not in ("checks", "kept", "why")}
                                                   for x in new]
    research["disconfirmation"] = record
    return research, receipts + new, record


VIEWS = [{"view": "all", "overall": "Yellow", "reliability": "medium", "key_receipts": ["e3"]},
         {"view": "external", "overall": "Yellow", "reliability": "high", "key_receipts": ["e3", "e10"]},
         {"view": "devil", "overall": "Green", "reliability": "low", "key_receipts": ["e2"]}]


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("DISCONFIRM", None)
        for name, fn in (("_check", stub_check), ("_judge", stub_judge), ("_disconfirm", absent_disconfirm)):
            p = mock.patch.object(pipeline, name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)
        self.err = io.StringIO()
        r = contextlib.redirect_stderr(self.err)
        r.__enter__()
        self.addCleanup(r.__exit__, None, None, None)

    def research_fn(self, url, deadline=None, hints=None):
        return run.fake_research_fn(url, deadline, hints)


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
        r["matrix"]["9"]["note"] = "a | b\nc"
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
        self.assertIn("**Caveat with | a pipe and a newline**", md)      # prose keeps the text
        self.assertIn("| a \\| b c |", md)                                # table cells stay table-safe
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
        self.assertEqual(m["disconfirm"], "1")
        org = m["cases"][0]
        self.assertEqual({k: org[k] for k in run.STAT_KEYS},
                         {"tool_calls": 3, "llm_calls": 3, "capped": False, "receipts_kept": 3, "receipts_rejected": 1,
                          "disconfirmation": "skipped (not available (no app.check.disconfirm))"})
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


BLIND = ('\ufeffid,url,product,vendor\r\n'
         '7,https://example.org,"Acme Notes, Field Edition","Acme ""Notes"" Ltd"\r\n'
         ',https://example.com,,\r\n')


class TestBlindAndSubmission(Base):
    def blind(self, text=BLIND):
        p = os.path.join(self.dir, "blind.csv")
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        return p

    def test_blind_csv_parse_quoted_fields_and_bom(self):
        cs = run.read_cases(self.blind())
        self.assertEqual([(c["id"], c["url"], c["product"], c["vendor"]) for c in cs],
                         [("7", "https://example.org", "Acme Notes, Field Edition", 'Acme "Notes" Ltd'),
                          ("example-com", "https://example.com", "", "")])
        self.assertEqual(run.hints_of(cs[0]), {"id": "7", "product": "Acme Notes, Field Edition",
                                               "vendor": 'Acme "Notes" Ltd'})
        dup = run.read_cases(self.blind("id,url\na,https://example.com\na,https://example.org\n"))
        self.assertEqual([c["id"] for c in dup], ["a", "a-2"])

    def test_hints_reach_research_and_id_reaches_verdict(self):
        seen = []

        def research(url, deadline=None, hints=None):
            seen.append((url, hints, deadline is not None))
            return run.fake_research_fn(url)
        out = os.path.join(self.dir, "v.jsonl")
        with mock.patch.dict(os.environ, {"RUN_ID": "h1"}):
            run.run_cases(run.read_cases(self.blind()), out, 2, 60, runs_dir=os.path.join(self.dir, "runs"),
                          research_fn=research)
        self.assertEqual(sorted(seen), [
            ("https://example.com", {"id": "example-com", "product": "", "vendor": ""}, True),
            ("https://example.org", {"id": "7", "product": "Acme Notes, Field Edition", "vendor": 'Acme "Notes" Ltd'},
             True)])
        self.assertEqual(sorted(json.loads(x)["id"] for x in open(out)), ["7", "example-com"])
        with open(os.path.join(self.dir, "runs", "h1", "example-org", "verdict.json")) as f:
            self.assertEqual(json.load(f)["id"], "7")

    def test_research_without_hints_param_still_runs(self):
        v = pipeline.check_vendor("https://example.com", os.path.join(self.dir, "r", "x"),
                                  research_fn=lambda url, deadline=None: run.fake_research_fn(url),
                                  hints={"id": "9", "product": "", "vendor": ""})
        self.assertIsNone(v["error"])
        self.assertEqual(v["id"], "9")

    def test_judge_error_is_kept(self):
        with mock.patch.object(pipeline, "_judge", side_effect=lambda r, rc: stub_judge(r, rc) | {"error": "judge: x"}):
            v = pipeline.check_vendor("https://example.com", os.path.join(self.dir, "r", "y"),
                                      research_fn=self.research_fn)
        self.assertEqual(v["error"], "judge: x")

    def test_submission_csv_and_one_trace_per_id(self):
        sub, out = os.path.join(self.dir, "sub"), os.path.join(self.dir, "v.jsonl")
        with mock.patch.dict(os.environ, {"RUN_ID": "s1", "RECEIPTS_FAKE": "1"}):
            self.assertEqual(run.main(["--cases", self.blind(), "--out", out, "--runs-dir",
                                       os.path.join(self.dir, "runs"), "--submit", sub]), 0)
        with open(os.path.join(sub, "stackgini-verdicts.csv"), newline="", encoding="utf-8") as f:
            text = f.read()
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual(rows[0], ["id", "url", "product", "vendor", "overall", "green_flags", "yellow_flags",
                                   "red_flags", "reasoning"])
        d = [dict(zip(rows[0], r)) for r in rows[1:]]
        self.assertEqual([(x["id"], x["url"]) for x in d], [("7", "https://example.org"), ("example-com", "https://example.com")])
        self.assertEqual((d[0]["product"], d[0]["vendor"]), ("Acme Notes, Field Edition", 'Acme "Notes" Ltd'))
        self.assertEqual((d[1]["product"], d[1]["vendor"]), ("Acme Ledger", "Acme Ledger GmbH"))  # from identity
        self.assertEqual(d[0]["yellow_flags"],
                         "Caveat with | a pipe and a newline (https://dev.example.net/acme-notes-maintainer)")
        self.assertEqual(len(text.splitlines()), 3)  # one line per row, so the trace's line link holds
        self.assertEqual(sorted(os.listdir(os.path.join(sub, "traces"))), ["7.md", "example-com.md"])
        t = open(os.path.join(sub, "traces", "7.md"), encoding="utf-8").read()
        self.assertTrue(t.startswith("> Submission row: [stackgini-verdicts.csv line 2](../stackgini-verdicts.csv#L2)"
                                     " · id 7 · https://example.org"))
        self.assertIn("## 2 Matrix (24 categories)", t)
        r = fixture("example-org")
        for e in r["evidence"][1:]:  # every flag's url and full quote sit under it, verbatim
            self.assertIn(f"  - Quote: \"{e['quote']}\"", t)
            self.assertIn(f"  - URL: {e['url']}", t)

    def test_long_quote_is_never_cut(self):
        r = fixture("example-org")
        rc = stub_check(r)
        long = "word " * 200 + "end | of quote"
        rc[2]["quote"] = long
        md = trace.render(r, rc, stub_judge(r, rc))
        self.assertIn(f"  - Quote: \"{long.strip()}\"", md)



class TestDisconfirmAndViews(Base):
    def run_one(self, slug="example-org"):
        rd = os.path.join(self.dir, "runs", "d1", slug)
        v = pipeline.check_vendor("https://" + slug.replace("-", "."), rd, deadline=time.time() + 900,
                                  research_fn=self.research_fn)
        files = {}
        for f in ("research.json", "receipts.json"):
            with open(os.path.join(rd, f)) as fh:
                files[f] = json.load(fh)
        with open(os.path.join(rd, "trace.md")) as fh:
            files["trace.md"] = fh.read()
        return v, files

    def test_disconfirm_record_lands_in_research_json_and_trace(self):
        with mock.patch.object(pipeline, "_disconfirm", side_effect=stub_disconfirm) as d:
            v, f = self.run_one()
        self.assertEqual(d.call_count, 1)
        rec = f["research.json"]["disconfirmation"]
        self.assertEqual((rec["targets"], rec["outcomes"][0]["outcome"], rec["deadline_seen"]), (["e3"], "survived", True))
        self.assertEqual([x["id"] for x in f["receipts.json"]][-2:], ["e10", "e11"])
        self.assertIn("e10", [e["id"] for e in f["research.json"]["evidence"]])
        self.assertIsNone(v["error"])
        self.assertEqual(v["stats"]["disconfirmation"], "1 target: 1 survived")
        self.assertEqual((v["stats"]["receipts_kept"], v["stats"]["receipts_rejected"]), (4, 2))
        t = f["trace.md"]
        sec = t.split("### Disconfirmation (before the judge)")[1].split("### Judge views")[0]
        self.assertIn("Ran: 1 target: 1 survived; 2 queries; 12.5 s.", sec)
        self.assertIn("- e3: One person builds and releases the product", sec)
        self.assertIn('- [e3] search: `"Acme" second engineer` · 2 results', sec)
        self.assertIn("· error: timeout", sec)
        self.assertIn("| e3 | survived | nothing newer says otherwise | e10 kept, e11 rejected (same name, different company) |",
                      sec)
        self.assertIn("    - Disconfirmation: survived (nothing newer says otherwise); new receipts e10, e11", t)
        self.assertIn("- Hypothesis: disconfirmation of e3: a second engineer?", t)
        self.assertIn("- Disconfirmation: 1 target: 1 survived · judge views: not run", t)

    def test_disconfirm_off_is_not_called(self):
        os.environ["DISCONFIRM"] = "0"
        with mock.patch.object(pipeline, "_disconfirm", side_effect=stub_disconfirm) as d:
            v, f = self.run_one()
        self.assertEqual(d.call_count, 0)
        self.assertEqual(f["research.json"]["disconfirmation"], {"skipped": "DISCONFIRM=0"})
        self.assertNotIn("e10", [x["id"] for x in f["receipts.json"]])
        self.assertIn("### Disconfirmation (before the judge)\nNot run: skipped (DISCONFIRM=0).", f["trace.md"])
        self.assertEqual(v["stats"]["disconfirmation"], "skipped (DISCONFIRM=0)")

    def test_disconfirm_absent_or_failing_keeps_the_research(self):
        v, f = self.run_one()
        self.assertEqual(f["research.json"]["disconfirmation"]["skipped"], "not available (no app.check.disconfirm)")

        def breaks(research, receipts, deadline=None):
            research["evidence"].append({"id": "e50"})
            receipts.append({"id": "e50", "kept": True})
            raise RuntimeError("plan call failed")
        with mock.patch.object(pipeline, "_disconfirm", side_effect=breaks):
            v, f = self.run_one()
        self.assertIsNone(v["error"])
        self.assertEqual(f["research.json"]["disconfirmation"]["skipped"], "RuntimeError: plan call failed")
        self.assertNotIn("e50", [e["id"] for e in f["research.json"]["evidence"]])
        self.assertNotIn("e50", [x["id"] for x in f["receipts.json"]])
        with mock.patch.object(pipeline, "_disconfirm", return_value=None):
            v, f = self.run_one()
        self.assertIn("TypeError: disconfirm returned NoneType", f["research.json"]["disconfirmation"]["skipped"])

    def test_views_render_with_agreement(self):
        r = fixture("example-org")
        rc = stub_check(r)
        md = trace.render(r, rc, stub_judge(r, rc) | {"views": VIEWS})
        sec = md.split("### Judge views")[1].split("### Red flags")[0]
        self.assertIn("Agreement: 2/3 views say Yellow; final Yellow.", sec)
        self.assertIn("| external | Yellow | high | e3, e10 |", sec)
        self.assertIn("| devil | Green | low | e2 |", sec)
        self.assertIn("judge views: 2/3 views say Yellow; final Yellow", md)
        split = [VIEWS[0], VIEWS[2], VIEWS[1] | {"overall": "Red"}]
        self.assertEqual(trace.views_summary({"views": split, "overall": "Yellow"}),
                         "no majority: Yellow 1, Green 1, Red 1 of 3 views; final Yellow")
        agree = [x | {"overall": "Green"} for x in VIEWS]
        self.assertEqual(trace.views_summary({"views": agree, "overall": "Green"}), "3/3 views say Green; final Green")

    def test_absent_sections_say_not_run(self):
        r = fixture("example-com")
        rc = stub_check(r)
        md = trace.render(r, rc, stub_judge(r, rc))
        self.assertIn("### Disconfirmation (before the judge)\nNot run.", md)
        self.assertIn("### Judge views\nNot run (one judge call).", md)
        self.assertIn("- Disconfirmation: not run · judge views: not run", md)


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
