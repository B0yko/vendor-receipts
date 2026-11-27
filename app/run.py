"""The batch runner (lane L5): the one command behind make batch / eval / holdout / smoke / one.

  python3 -m app.run --cases FILE --out FILE [--only id,url,...] [--concurrency N] [--wall-cap S] [--runs-dir DIR]

Cases: JSONL ({"url", "id"?} per line), CSV with a url column, or one URL per line (# comments allowed).
RUN_ID (env) or a timestamp names runs/<run_id>/; each product runs in runs/<run_id>/<slug>/ with its own deadline
(start + wall cap). Each verdict is appended to --out as it completes (locked, flushed). --out is truncated first,
except with --only, which replaces just the selected cases' lines. At the end: <out stem>.csv and <out stem>.full.csv
in input order, and runs/<run_id>/manifest.json (git hash and dirty flag, model id, LLM_*_EXTRA values, wall cap,
concurrency, per-case seconds/overall/error, start/end, mode). Progress goes to stderr: "[3/10] <slug> Yellow 412s".
RECEIPTS_FAKE=1 swaps research for tests/fixtures/research/<slug>.json (RECEIPTS_FIXTURES overrides the folder).
Concurrency: --concurrency or CONC (default 8); wall cap: --wall-cap or WALL_CAP (default 1200 s).
"""
import argparse, concurrent.futures as cf, csv, datetime, io, json, os, subprocess, sys, threading, time

from app import ledger, model, pipeline
from app.out import writer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_cases(path):
    """-> [{"url", "id", "slug"}] in file order; ids default to the slug; duplicate slugs get -2, -3."""
    with open(path, encoding="utf-8-sig") as f:
        text = f.read()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    raw = []
    if lines and lines[0].startswith("{"):
        for ln in lines:
            d = json.loads(ln)
            raw.append({"url": str(d.get("url", "")).strip(), "id": str(d.get("id") or "").strip()})
    elif lines and "url" in [c.strip().lower() for c in next(csv.reader([lines[0]]))]:
        rows = list(csv.DictReader(io.StringIO(text)))
        for r in rows:
            r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
            raw.append({"url": r.get("url", ""), "id": r.get("id", "")})
    else:
        raw = [{"url": ln, "id": ""} for ln in lines]
    cases, seen = [], {}
    for c in raw:
        if not c["url"]:
            continue
        s = ledger.slug(c["url"])
        seen[s] = seen.get(s, 0) + 1
        if seen[s] > 1:
            s = f"{s}-{seen[s]}"
        cases.append({"url": c["url"], "id": c["id"] or s, "slug": s})
    return cases


def select(cases, only):
    """--only: comma-separated ids, urls or slugs (urls compare by slug, so scheme and www. do not matter)."""
    if not only:
        return cases
    want = {w.strip() for w in only.split(",") if w.strip()}
    keys = want | {ledger.slug(w) for w in want}
    return [c for c in cases if c["id"] in want or c["url"] in want or c["slug"] in keys
            or ledger.slug(c["url"]) in keys]


def fake_research_fn(url, deadline=None):
    d = os.environ.get("RECEIPTS_FIXTURES") or os.path.join(ROOT, "tests", "fixtures", "research")
    path = os.path.join(d, ledger.slug(url) + ".json")
    with open(path, encoding="utf-8") as f:
        r = json.load(f)
    r["url"] = url
    return r


def git_state():
    try:
        h = subprocess.run(["git", "-C", ROOT, "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                           timeout=10).stdout.strip()
        dirty = bool(subprocess.run(["git", "-C", ROOT, "status", "--porcelain", "--untracked-files=no"],
                                    capture_output=True, text=True, timeout=10).stdout.strip())
        return {"commit": h or None, "dirty": dirty}
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "dirty": None}


def llm_extras():
    """The provider extras (reasoning effort and similar) per endpoint; only *_EXTRA values, never a key."""
    return {k: v for k, v in sorted(os.environ.items()) if k.startswith("LLM_") and k.endswith("_EXTRA")}


def _load_lines(path):
    out = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for ln in f:
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    continue
    return out


def run_cases(cases, out, concurrency=8, wall_cap=1200, run_id=None, runs_dir="runs", research_fn=None,
              replace_only=False, all_cases=None):
    """Runs `cases`, appends verdicts to `out`, writes the CSVs and the manifest; returns the verdicts in input order."""
    run_id = run_id or os.environ.get("RUN_ID") or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    if research_fn is None and os.environ.get("RECEIPTS_FAKE") == "1":
        research_fn = fake_research_fn
    run_root = os.path.join(runs_dir, run_id)
    os.makedirs(run_root, exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    selected = {ledger.slug(c["url"]) for c in cases}
    kept = [v for v in _load_lines(out) if ledger.slug(v.get("url", "")) not in selected] if replace_only else []
    with open(out, "w", encoding="utf-8") as f:
        f.writelines(writer.jsonl_line(v) + "\n" for v in kept)
    started, t0 = datetime.datetime.now().isoformat(timespec="seconds"), time.time()
    lock, done, results = threading.Lock(), [0], {}

    def one(c):
        deadline = time.time() + wall_cap
        try:
            v = pipeline.check_vendor(c["url"], os.path.join(run_root, c["slug"]), deadline=deadline,
                                      research_fn=research_fn)
        except Exception as e:  # noqa: BLE001 (check_vendor catches its own; this guards the disk writes)
            v = pipeline.error_verdict(c["url"], f"{type(e).__name__}: {e}")
            v.update({"trace": "", "run_id": run_id, "seconds": round(time.time() - deadline + wall_cap, 1),
                      "mode": model.mode(), "model": model.model_id()})
        v.setdefault("id", c["id"])
        writer.append_jsonl(v, out, lock)
        with lock:
            done[0] += 1
            results[c["slug"]] = v
            err = f" error: {v['error']}" if v.get("error") else ""
            print(f"[{done[0]}/{len(cases)}] {c['slug']} {v.get('overall')} {round(v.get('seconds') or 0)}s{err}",
                  file=sys.stderr, flush=True)
        return v

    with cf.ThreadPoolExecutor(max_workers=max(1, int(concurrency))) as ex:
        list(ex.map(one, cases))
    ordered = [results[c["slug"]] for c in cases]
    # CSVs hold every verdict in --out (kept lines from earlier runs too), in the order of the full case list.
    order = {ledger.slug(c["url"]): i for i, c in enumerate(all_cases or cases)}
    everything = sorted(kept + ordered, key=lambda v: order.get(ledger.slug(v.get("url", "")), len(order)))
    stem, _ = os.path.splitext(out)
    writer.write_csv(everything, stem + ".csv")
    manifest = {"run_id": run_id, "git": git_state(), "model": model.model_id(), "mode": model.mode(),
                "llm_extra": llm_extras(), "wall_cap": wall_cap, "concurrency": int(concurrency),
                "cases_file_out": out, "start": started, "end": datetime.datetime.now().isoformat(timespec="seconds"),
                "seconds": round(time.time() - t0, 1),
                "cases": [{"id": c["id"], "url": c["url"], "slug": c["slug"], "seconds": results[c["slug"]].get("seconds"),
                           "overall": results[c["slug"]].get("overall"), "error": results[c["slug"]].get("error"),
                           "trace": results[c["slug"]].get("trace")} for c in cases]}
    with open(os.path.join(run_root, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    errors = sum(1 for v in ordered if v.get("error"))
    print(f"done {len(ordered)} cases in {manifest['seconds']}s, {errors} errors; {out}, {stem}.csv, "
          f"{os.path.join(run_root, 'manifest.json')}", file=sys.stderr, flush=True)
    return ordered


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cases", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="")
    ap.add_argument("--concurrency", type=int, default=int(os.environ.get("CONC") or 8))
    ap.add_argument("--wall-cap", type=float, default=float(os.environ.get("WALL_CAP") or 1200))
    ap.add_argument("--runs-dir", default=os.environ.get("RUNS_DIR") or "runs")
    a = ap.parse_args(argv)
    all_cases = read_cases(a.cases)
    cases = select(all_cases, a.only)
    if not cases:
        print(f"no cases selected from {a.cases}" + (f" with --only {a.only}" if a.only else ""), file=sys.stderr)
        return 2
    run_cases(cases, a.out, a.concurrency, a.wall_cap, runs_dir=a.runs_dir, replace_only=bool(a.only),
              all_cases=all_cases)
    return 0


if __name__ == "__main__":
    sys.exit(main())
