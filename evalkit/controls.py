"""Control cases: public products outside the labels with an obvious answer (tests/control_cases.jsonl, "expect":
not_red for indisputably solid products, red_or_yellow for publicly ended ones). A sanity check, not a score.

  make control RUN_ID=<id>                        # the batch over the control cases
  python3 -m evalkit.controls --preds runs/control_<id>.jsonl [--cases tests/control_cases.jsonl]

Prints one line per case (id, expect, ours, PASS/FAIL) and exits 1 when any case fails or has no prediction.
"""
import argparse, json, os, sys

from evalkit import labels as L

CASES = os.path.join(L.ROOT, "tests", "control_cases.jsonl")
EXPECT = {"not_red": ("Green", "Yellow"), "red_or_yellow": ("Red", "Yellow")}


def load_cases(path=CASES):
    with open(path) as f:
        return [json.loads(x) for x in f if x.strip()]


def check(cases, preds):
    """-> [(id, expect, ours, ok)]; ours '-' when there is no prediction or no color."""
    out = []
    for c in cases:
        ours = L.pred_color(preds.get(L.key(c["url"]))) or "-"
        out.append((c["id"], c["expect"], ours, ours in EXPECT[c["expect"]]))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--preds", required=True)
    ap.add_argument("--cases", default=CASES)
    a = ap.parse_args(argv)
    res = check(load_cases(a.cases), L.load_preds(a.preds))
    for cid, exp, ours, ok in res:
        print(f"{cid}: expect {exp}, ours {ours}: {'PASS' if ok else 'FAIL'}")
    print(f"controls: {sum(r[3] for r in res)}/{len(res)} pass")
    return 0 if all(r[3] for r in res) else 1


if __name__ == "__main__":
    sys.exit(main())
