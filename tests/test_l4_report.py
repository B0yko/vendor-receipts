"""evalkit.labels and evalkit.report on synthetic labels (invented Acme names, example.com); offline, no model."""
import csv, io, json, os, shutil, sys, tempfile, unittest
from contextlib import redirect_stderr, redirect_stdout

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
from app import model  # noqa: E402
from evalkit import labels, report  # noqa: E402

COLS = ["id", "url", "product", "vendor", "overall", "green_flags", "yellow_flags", "red_flags", "reasoning",
        "reviewer_notes_original"]
# 4 Green, 3 Yellow, 3 Red: the stratified 30% split puts 1 of each class in holdout.
COLORS = ["Green"] * 4 + ["Yellow"] * 3 + ["Red"] * 3


def read(path):
    with open(path) as f:
        return f.read()


def write_labels(path, colors=COLORS):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, COLS)
        w.writeheader()
        for i, c in enumerate(colors, 1):
            w.writerow({"id": str(i), "url": f"https://acme{i}.example.com/", "product": f"Acme Widget {i}",
                        "vendor": f"Acme {i} GmbH", "overall": c.lower() if i == 1 else c,
                        "green_flags": "- solid\n- busy", "yellow_flags": "small team; one maintainer",
                        "red_flags": "", "reasoning": "Synthetic.", "reviewer_notes_original": ""})


def write_preds(path, colors_by_id, extra=None):
    with open(path, "w") as f:
        for i, c in colors_by_id.items():
            line = {"url": f"https://www.acme{i}.example.com", "overall": c, "call": c, "reliability": "medium",
                    "seconds": 12.5, "error": None, "run_id": "r1", "reasons": []}
            line.update((extra or {}).get(i, {}))
            f.write(json.dumps(line) + "\n")


def no_model_call(system, user):
    raise AssertionError("these tests make no model call")


class Base(unittest.TestCase):
    def setUp(self):
        for p in ("match", "extra", "classify"):
            model.set_fake(p, no_model_call)
        self.addCleanup(model.clear_fakes)
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.csv = os.path.join(self.d, "labels.csv")
        write_labels(self.csv)
        self.rows = labels.load(self.csv)
        self.split = {r["id"]: r["split"] for r in self.rows}


class LabelsTest(Base):
    def test_rows_have_split_key_color(self):
        self.assertEqual(len(self.rows), 10)
        self.assertEqual(sum(s == "holdout" for s in self.split.values()), 3)
        self.assertEqual(self.rows[0]["color"], "Green")  # "green" in the CSV
        self.assertEqual(self.rows[0]["key"], "acme1.example.com")
        self.assertEqual({r["color"] for r in labels.load(self.csv, split="holdout")}, {"Green", "Yellow", "Red"})

    def test_split_matches_eval_cases_out(self):
        ev = labels.ev()
        out = os.path.join(self.d, "dev.jsonl")
        with redirect_stdout(io.StringIO()):
            ev.main([self.csv, "--split", "dev", "--cases-out", out, "--label", "overall"])
        dev = {ev.norm_key(json.loads(l)["url"]) for l in read(out).splitlines()}
        self.assertEqual(dev, {r["key"] for r in labels.load(self.csv, split="dev")})

    def test_env_labels_and_color(self):
        os.environ["LABELS"] = self.csv
        self.addCleanup(os.environ.pop, "LABELS")
        self.assertTrue(labels.exists())
        self.assertEqual(len(labels.load()), 10)
        self.assertEqual(labels.color(" YELLOW "), "Yellow")
        self.assertEqual(labels.color("buy"), "")


class ScoreTest(Base):
    def test_metrics(self):
        dev = [r for r in self.rows if r["split"] == "dev"]
        ids = [r["id"] for r in dev]
        green = [r["id"] for r in dev if r["color"] == "Green"]
        red = [r["id"] for r in dev if r["color"] == "Red"]
        preds = os.path.join(self.d, "p.jsonl")
        guess = {i: next(r["color"] for r in dev if r["id"] == i) for i in ids}
        guess[green[0]] = "Red"          # a false Red on Green
        guess.pop(red[0])                # a missing prediction counts as wrong
        write_preds(preds, guess)
        s = report.score(dev, labels.load_preds(preds))
        self.assertEqual(s["n"], 7)
        self.assertEqual(s["correct"], 5)
        self.assertEqual(s["false_red_on_green"], [green[0]])
        self.assertEqual(s["confusion"]["Green"]["Red"], 1)
        self.assertEqual(s["confusion"]["Red"]["-"], 1)
        self.assertEqual(s["recall"]["Red"], {"hit": 1, "n": 2, "recall": 0.5})
        self.assertEqual(s["red_recall"], 0.5)
        self.assertEqual(s["all_yellow"], {"correct": 2, "n": 7, "accuracy": 2 / 7})

    def test_regressions(self):
        cases = [{"id": "1", "correct": False}, {"id": "2", "correct": True}, {"id": "3", "correct": True}]
        prev = {"cases": [{"id": "1", "correct": True}, {"id": "2", "correct": False}, {"id": "3", "correct": True}]}
        self.assertEqual(report.regressions(cases, prev), (["1"], ["2"]))


class CliTest(Base):
    def run_report(self, *extra):
        out = os.path.join(self.d, "report.md")
        log = os.path.join(self.d, "log.md")
        with redirect_stdout(io.StringIO()) as buf:
            rc = report.main(["--labels", self.csv, "--split", "dev", "--out", out, "--log", log, *extra])
        return rc, read(out), read(log), buf.getvalue()

    def test_end_to_end_with_prev_and_baseline(self):
        all_yellow = os.path.join(self.d, "y.jsonl")
        write_preds(all_yellow, {r["id"]: "Yellow" for r in self.rows})
        perfect = os.path.join(self.d, "p.jsonl")
        write_preds(perfect, {r["id"]: r["color"] for r in self.rows})
        save1 = os.path.join(self.d, "r1.json")
        rc, md, log, out = self.run_report("--preds", perfect, "--save", save1, "--note", "first",
                                           "--baseline-preds", all_yellow, "--run-dir", "runs/run-a")
        self.assertEqual(rc, 0)
        self.assertIn("Accuracy: **7/7 (100%)**", md)
        self.assertIn("All-Yellow baseline (same split): 2/7", md)
        self.assertIn("Plain agent (y.jsonl): 2/7", md)
        self.assertIn("False Red on Green: **0**", md)
        self.assertIn("| theirs \\ ours | Green | Yellow | Red | recall |", md)
        self.assertEqual(md.count("| yes |"), 7)
        self.assertIn("run-a", log)
        self.assertIn("first", log)
        self.assertIn("dev: 7/7", out)
        saved = json.loads(read(save1))
        self.assertEqual(saved["correct"], 7)
        self.assertEqual(saved["baseline"]["correct"], 2)
        # second run: everything Yellow; the regressions are listed against the first report
        rc, md, log, out = self.run_report("--preds", all_yellow, "--prev", save1, "--note", "second")
        self.assertIn("Right -> wrong: 5", md)
        self.assertIn("Net: -5", md)
        self.assertEqual(len([l for l in log.splitlines() if l.startswith("| 20")]), 2)
        self.assertIn("right->wrong 5", out)

    def test_no_label_text_in_outputs(self):
        preds = os.path.join(self.d, "p.jsonl")
        write_preds(preds, {r["id"]: "Green" for r in self.rows})
        save = os.path.join(self.d, "s.json")
        _, md, log, out = self.run_report("--preds", preds, "--save", save)
        for text in (md, log, out, read(save)):
            self.assertNotIn("acme", text.lower())
            self.assertNotIn("Synthetic", text)

    def test_missing_labels(self):
        with redirect_stdout(io.StringIO()):
            with redirect_stderr(io.StringIO()):
                rc = report.main(["--labels", os.path.join(self.d, "none.csv"), "--preds", "x", "--split", "dev",
                                  "--out", os.path.join(self.d, "o.md")])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
