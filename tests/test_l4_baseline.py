"""evalkit.baseline (the plain agent) and evalkit.controls with model.set_fake; invented data, no network."""
import io, json, os, shutil, sys, tempfile, unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
from app import ledger, model  # noqa: E402
from evalkit import baseline, controls, labels  # noqa: E402


def research(url, home=True, n_search=3):
    r = ledger.new_research(url)
    if home:
        r["sources"][url.rstrip("/") + "/"] = {"text": "Acme builds widgets. " * 10, "kind": "fetch", "title": "Acme"}
        r["sources"][url.rstrip("/") + "/pricing"] = {"text": "Pricing", "kind": "fetch", "title": "Pricing"}
    for i in range(n_search):
        r["sources"][f"https://news.example.org/{i}"] = {"text": f"Acme news item {i}. " * 100, "kind": "search",
                                                         "title": f"News {i}", "published_date": "2026-01-0" + str(i + 1)}
    r["sources"]["https://archive.example.org/x"] = {"text": "WAYBACK-ONLY", "kind": "wayback", "title": "Old"}
    r["evidence"] = [{"id": "e1", "text": "EVIDENCE-ONLY"}]
    return r


def write_run(run_dir, rs):
    for r in rs:
        d = os.path.join(run_dir, r["slug"])
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "research.json"), "w") as f:
            json.dump(r, f)


class BaselineTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.addCleanup(model.clear_fakes)
        self.seen = []

        def fake(system, user):
            self.seen.append((system, user))
            if "acme2" in user:
                return {"overall": "red", "red_flags": [{"text": "Shut down", "url": "https://news.example.org/1",
                                                         "quote": "Acme news item 1"}], "reasoning": "Ended."}
            if "acme3" in user:
                raise model.ModelError("limit")
            return {"overall": "Green", "green_flags": [{"text": "Busy", "url": "https://acme1.example.com/",
                                                         "quote": "Acme builds widgets"}, {"url": "no text"}],
                    "yellow_flags": [], "red_flags": [], "reasoning": "Fine."}
        model.set_fake("baseline", fake)

    def test_prompt_has_homepage_and_search_only(self):
        p = baseline.prompt(research("https://acme1.example.com"))
        self.assertIn("Homepage (https://acme1.example.com/):", p)
        self.assertIn("Acme builds widgets.", p)
        self.assertIn("Search results (3):", p)
        self.assertIn("News 2 (2026-01-03) https://news.example.org/2", p)
        self.assertNotIn("WAYBACK-ONLY", p)       # only kind "search" sources
        self.assertNotIn("EVIDENCE-ONLY", p)      # none of our evidence, rule or checks
        self.assertNotIn("Pricing", p)            # the homepage, not another page
        for word in ("matrix", "guard", "rule", "receipt", "categor"):   # no rule, no checks in the plain agent
            self.assertNotIn(word, baseline.SYSTEM.lower())

    def test_caps(self):
        p = baseline.prompt(research("https://acme1.example.com", n_search=2000))
        self.assertLess(len(p), baseline.HOME_CAP + baseline.SEARCH_CAP + 2000)

    def test_homepage_fallback_fetch(self):
        from app.research import tools
        with mock.patch.object(tools, "fetch", return_value={"final_url": "https://acme9.example.com/", "text": "Hi"}) as f:
            self.assertEqual(baseline.homepage(research("https://acme9.example.com", home=False)),
                             ("https://acme9.example.com/", "Hi"))
            f.assert_called_once()
        with mock.patch.object(tools, "fetch", side_effect=OSError("down")):
            u, t = baseline.homepage(research("https://acme9.example.com", home=False))
            self.assertIn("not retrieved", t)

    def test_cli_writes_preds_in_our_shape(self):
        run = os.path.join(self.d, "runs", "r1")
        write_run(run, [research(f"https://acme{i}.example.com") for i in (1, 2, 3)])
        os.makedirs(os.path.join(run, "no-research"))
        out = os.path.join(self.d, "runs", "baseline_r1.jsonl")
        with redirect_stdout(io.StringIO()) as buf:
            rc = baseline.main(["--run-dir", run, "--out", out, "--concurrency", "2"])
        self.assertEqual(rc, 0)
        self.assertIn("3 products, 1 errors", buf.getvalue())
        with open(out) as f:
            lines = {labels.key(x["url"]): x for x in map(json.loads, f)}
        g, r, e = lines["acme1.example.com"], lines["acme2.example.com"], lines["acme3.example.com"]
        self.assertEqual((g["overall"], g["call"]), ("Green", "Green"))
        self.assertEqual(g["reasons"], [{"text": "Busy", "url": "https://acme1.example.com/", "quote": "Acme builds widgets"}])
        self.assertEqual(r["overall"], "Red")
        self.assertEqual(r["reasons"][0]["url"], "https://news.example.org/1")
        self.assertIn("limit", e["error"])
        self.assertEqual(e["overall"], "")
        for x in lines.values():
            self.assertEqual(x["run_id"], "baseline")
            self.assertTrue({"url", "overall", "call", "reasons", "seconds", "error", "mode"} <= set(x))
        self.assertEqual(len(self.seen), 3)       # one call per product

    def test_empty_run_dir(self):
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(baseline.main(["--run-dir", os.path.join(self.d, "none"), "--out",
                                            os.path.join(self.d, "o.jsonl")]), 2)


class ControlsTest(unittest.TestCase):
    def test_control_cases_file(self):
        cases = controls.load_cases()
        self.assertEqual(len(cases), 5)
        self.assertEqual(sorted(c["expect"] for c in cases), ["not_red"] * 3 + ["red_or_yellow"] * 2)
        for c in cases:
            self.assertTrue(c["url"].startswith("https://") and c["id"] and c["why"])
        self.assertEqual(len({c["id"] for c in cases}), 5)

    def test_check(self):
        cases = [{"id": "a", "url": "https://acme1.example.com", "expect": "not_red"},
                 {"id": "b", "url": "https://acme2.example.com", "expect": "red_or_yellow"},
                 {"id": "c", "url": "https://acme3.example.com", "expect": "not_red"},
                 {"id": "d", "url": "https://acme4.example.com", "expect": "red_or_yellow"}]
        preds = {"acme1.example.com": {"overall": "Yellow"}, "acme2.example.com": {"overall": "Red"},
                 "acme3.example.com": {"overall": "Red"}}
        self.assertEqual(controls.check(cases, preds), [("a", "not_red", "Yellow", True), ("b", "red_or_yellow", "Red", True),
                                                        ("c", "not_red", "Red", False), ("d", "red_or_yellow", "-", False)])

    def test_cli_exit_code(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "p.jsonl")
        with open(p, "w") as f:
            for c in controls.load_cases():
                f.write(json.dumps({"url": c["url"], "overall": "Yellow"}) + "\n")
        with redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(controls.main(["--preds", p]), 0)
        self.assertIn("controls: 5/5 pass", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
