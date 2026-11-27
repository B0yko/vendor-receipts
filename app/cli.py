"""receipts: the human entry point over app.run (lane L5).

  python3 -m app.cli run <url> [--out F] [--wall-cap S]        one product; prints color, reliability, trace path
  python3 -m app.cli batch <file> [--out F] [--only ids] [--concurrency N] [--wall-cap S]

`run` writes runs/<run_id>/one.jsonl by default; `batch` writes runs/verdicts.jsonl (the Makefile's OUT) by default.
Both also write the CSVs and runs/<run_id>/manifest.json (see app/run.py).
"""
import argparse, datetime, os, sys

from app import ledger, run as runner


def _print(verdicts, out):
    for v in verdicts:
        err = f"  error: {v['error']}" if v.get("error") else ""
        print(f"{v.get('overall', '?'):6} {v.get('url', '')}  reliability {v.get('reliability', '?')}  "
              f"trace {v.get('trace', '')}{err}")
    print(f"verdicts: {out}  csv: {os.path.splitext(out)[0]}.csv")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="receipts", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="check one vendor URL")
    r.add_argument("url")
    b = sub.add_parser("batch", help="check every case in a file (JSONL, CSV with url, or one URL per line)")
    b.add_argument("file")
    b.add_argument("--only", default="")
    b.add_argument("--concurrency", type=int, default=int(os.environ.get("CONC") or 8))
    for p in (r, b):
        p.add_argument("--out", default="")
        p.add_argument("--wall-cap", type=float, default=float(os.environ.get("WALL_CAP") or 1200))
        p.add_argument("--runs-dir", default=os.environ.get("RUNS_DIR") or "runs")
    a = ap.parse_args(argv)
    run_id = os.environ.get("RUN_ID") or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    if a.cmd == "run":
        cases = [{"url": a.url, "id": ledger.slug(a.url), "slug": ledger.slug(a.url)}]
        out = a.out or os.path.join(a.runs_dir, run_id, "one.jsonl")
        vs = runner.run_cases(cases, out, 1, a.wall_cap, run_id=run_id, runs_dir=a.runs_dir)
    else:
        all_cases = runner.read_cases(a.file)
        cases = runner.select(all_cases, a.only)
        if not cases:
            print(f"no cases selected from {a.file}", file=sys.stderr)
            return 2
        out = a.out or os.path.join(a.runs_dir, "verdicts.jsonl")
        vs = runner.run_cases(cases, out, a.concurrency, a.wall_cap, run_id=run_id, runs_dir=a.runs_dir,
                              replace_only=bool(a.only), all_cases=all_cases)
    _print(vs, out)
    return 1 if vs and all(v.get("error") for v in vs) else 0


if __name__ == "__main__":
    sys.exit(main())
