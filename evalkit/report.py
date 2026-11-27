"""Evaluation report for one split: our colors against the labels, the baselines, regressions, one line per product.

  python3 -m evalkit.report --preds F --split dev|holdout [--run-dir D] [--prev JSON] [--baseline-preds F]
      [--save JSON] [--note "..."] [--log runs/log.md] [--labels CSV] --out report.md

Writes report.md: color accuracy n/N, the 3x3 confusion (theirs x ours), per-class recall, false Red on Green (count
and ids), Red recall, the all-Yellow baseline on the same split, the plain baseline (--baseline-preds), regressions
against --prev (a JSON written earlier by --save: right -> wrong, and the fixes), one line per product (id, theirs,
ours, reliability, seconds, error). Appends one line to the log (time, run id, accuracy, false Red, note).
A product without a prediction counts as wrong (ours "-"). Rows are named by their CSV id; no names, flags or
reasoning of the labels reach the report or stdout.
"""
import argparse, json, os, sys, time

from evalkit import labels as L

COLORS = L.COLORS


def score(rows, preds):
    """rows: label rows of one split; preds: {key: verdict line}. -> metrics dict with per-case results."""
    cases = []
    for r in rows:
        p = preds.get(r["key"])
        ours = L.pred_color(p) if p else ""
        cases.append({"id": r["id"], "theirs": r["color"], "ours": ours or "-",
                      "correct": bool(ours) and ours == r["color"],
                      "reliability": (p or {}).get("reliability") or "",
                      "seconds": (p or {}).get("seconds"), "error": (p or {}).get("error") or ("no prediction" if not p else None)})
    n = len(cases)
    correct = sum(c["correct"] for c in cases)
    cols = list(COLORS) + (["-"] if any(c["ours"] not in COLORS for c in cases) else [])
    conf = {t: {o: 0 for o in cols} for t in COLORS}
    for c in cases:
        if c["theirs"] in conf:
            conf[c["theirs"]][c["ours"] if c["ours"] in cols else "-"] += 1
    recall = {}
    for t in COLORS:
        tot = sum(conf[t].values())
        recall[t] = {"hit": conf[t][t], "n": tot, "recall": conf[t][t] / tot if tot else None}
    false_red = [c["id"] for c in cases if c["theirs"] == "Green" and c["ours"] == "Red"]
    yellow = sum(c["theirs"] == "Yellow" for c in cases)
    return {"n": n, "correct": correct, "accuracy": correct / n if n else 0.0, "columns": cols, "confusion": conf,
            "recall": recall, "red_recall": recall["Red"]["recall"], "false_red_on_green": false_red,
            "all_yellow": {"correct": yellow, "n": n, "accuracy": yellow / n if n else 0.0}, "cases": cases}


def regressions(cases, prev):
    """-> (right -> wrong ids, wrong -> right ids) against a saved report JSON (its "cases" list)."""
    before = {c["id"]: c["correct"] for c in (prev or {}).get("cases", [])}
    worse = [c["id"] for c in cases if before.get(c["id"]) is True and not c["correct"]]
    better = [c["id"] for c in cases if before.get(c["id"]) is False and c["correct"]]
    return worse, better


def _pct(x):
    return "n/a" if x is None else f"{100 * x:.0f}%"


def _frac(a, b):
    return f"{a}/{b} ({_pct(a / b if b else None)})"


def run_id_of(preds, run_dir=None):
    if run_dir:
        return os.path.basename(os.path.normpath(run_dir))
    ids = sorted({str(p.get("run_id")) for p in preds.values() if p.get("run_id")})
    return ",".join(ids) or "?"


def render(split, s, run_id, note="", base=None, base_name="", worse=None, better=None, prev_name="", extra_md=""):
    md = [f"# Evaluation report: {split} split, run {run_id}", "",
          f"Generated {time.strftime('%Y-%m-%d %H:%M')}." + (f" Note: {note}" if note else ""), "",
          "## Color", "",
          f"- Accuracy: **{_frac(s['correct'], s['n'])}**",
          f"- All-Yellow baseline (same split): {_frac(s['all_yellow']['correct'], s['n'])}"]
    if base is not None:
        md.append(f"- Plain agent{f' ({base_name})' if base_name else ''}: {_frac(base['correct'], base['n'])}, "
                  f"false Red on Green {len(base['false_red_on_green'])}, Red recall {_pct(base['red_recall'])}")
    md += [f"- False Red on Green: **{len(s['false_red_on_green'])}**"
           + (f" ({', '.join(s['false_red_on_green'])})" if s["false_red_on_green"] else ""),
           f"- Red recall: {s['recall']['Red']['hit']}/{s['recall']['Red']['n']} ({_pct(s['red_recall'])})", "",
           "Confusion (rows: theirs, columns: ours; '-' = no prediction):", "",
           "| theirs \\ ours | " + " | ".join(s["columns"]) + " | recall |",
           "|---|" + "---|" * (len(s["columns"]) + 1)]
    for t in COLORS:
        rc = s["recall"][t]
        md.append(f"| {t} | " + " | ".join(str(s["confusion"][t][o]) for o in s["columns"])
                  + f" | {rc['hit']}/{rc['n']} ({_pct(rc['recall'])}) |")
    if worse is not None:
        md += ["", f"## Against the previous report{f' ({prev_name})' if prev_name else ''}", "",
               f"- Right -> wrong: {len(worse)}" + (f" ({', '.join(worse)})" if worse else ""),
               f"- Wrong -> right: {len(better)}" + (f" ({', '.join(better)})" if better else ""),
               f"- Net: {len(better) - len(worse):+d}"]
    if extra_md:
        md += ["", extra_md.rstrip()]
    md += ["", "## Per product", "", "| id | theirs | ours | ok | reliability | seconds | error |",
           "|---|---|---|---|---|---|---|"]
    for c in s["cases"]:
        sec = f"{c['seconds']:.0f}" if isinstance(c["seconds"], (int, float)) else ""
        err = str(c["error"] or "").replace("|", "/").replace("\n", " ")[:120]
        md.append(f"| {c['id']} | {c['theirs']} | {c['ours']} | {'yes' if c['correct'] else 'NO'} | "
                  f"{c['reliability']} | {sec} | {err} |")
    return "\n".join(md) + "\n"


def append_log(log, run_id, split, s, note):
    os.makedirs(os.path.dirname(os.path.abspath(log)), exist_ok=True)
    new = not os.path.exists(log)
    with open(log, "a") as f:
        if new:
            f.write("| time | run | split | accuracy | false Red on Green | note |\n|---|---|---|---|---|---|\n")
        f.write(f"| {time.strftime('%Y-%m-%d %H:%M')} | {run_id} | {split} | {_frac(s['correct'], s['n'])} | "
                f"{len(s['false_red_on_green'])} | {str(note).replace('|', '/')} |\n")


def main(argv=None, extra=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--preds", required=True)
    ap.add_argument("--split", choices=("dev", "holdout"), required=True)
    ap.add_argument("--run-dir")
    ap.add_argument("--prev")
    ap.add_argument("--baseline-preds")
    ap.add_argument("--save")
    ap.add_argument("--note", default="")
    ap.add_argument("--labels", help="labelled CSV (default: env LABELS, then data/...)")
    ap.add_argument("--log", default="runs/log.md")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if not L.exists(a.labels):
        print(f"report: no labels at {L.path(a.labels)}", file=sys.stderr)
        return 2
    rows = L.load(a.labels, split=a.split)
    preds = L.load_preds(a.preds)
    s = score(rows, preds)
    run_id = run_id_of(preds, a.run_dir)
    base = score(rows, L.load_preds(a.baseline_preds)) if a.baseline_preds else None
    prev = None
    if a.prev and os.path.exists(a.prev):
        with open(a.prev) as f:
            prev = json.load(f)
    worse, better = regressions(s["cases"], prev) if prev else (None, None)
    extra_md, extra_json = extra(a, rows, preds) if extra else ("", {})
    md = render(a.split, s, run_id, a.note, base, os.path.basename(a.baseline_preds or ""), worse, better,
                os.path.basename(a.prev or ""), extra_md)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w") as f:
        f.write(md)
    if a.save:
        os.makedirs(os.path.dirname(os.path.abspath(a.save)), exist_ok=True)
        out = dict(s, split=a.split, run_id=run_id, at=time.strftime("%Y-%m-%dT%H:%M:%S%z"), preds=a.preds,
                   note=a.note, baseline=({k: v for k, v in base.items() if k != "cases"} if base else None),
                   regressions=worse, fixes=better, **extra_json)
        with open(a.save, "w") as f:
            json.dump(out, f, indent=1)
    append_log(a.log, run_id, a.split, s, a.note)
    print(f"{a.split}: {_frac(s['correct'], s['n'])}, false Red on Green {len(s['false_red_on_green'])}, "
          f"all-Yellow {_frac(s['all_yellow']['correct'], s['n'])}"
          + (f", plain {_frac(base['correct'], base['n'])}" if base else "")
          + (f", right->wrong {len(worse)}" if worse is not None else "") + f" -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
