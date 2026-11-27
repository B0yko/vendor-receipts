"""The plain agent (lane L4): the baseline our pipeline must beat on the same retrieved data.

  python3 -m evalkit.baseline --run-dir runs/<run_id> --out runs/baseline_<run_id>.jsonl [--concurrency N]

Per product folder in the run (one research.json each): the homepage text (the research's own fetch of its URL, else
app.research.tools.fetch, which is cached) plus every search result our research retrieved (sources of kind
"search": title, URL, snippet), in one model call (purpose "baseline", cached). No color rule, no matrix, no receipt
checks, no guards. Output: one JSON line per product in our shape (url, overall, call, reasons [{text, url, quote}],
flags, reasoning, seconds, error, mode, run_id "baseline"), so evalkit.report scores it next to ours
(--baseline-preds, or found automatically as <parent of run dir>/baseline_<run id>.jsonl).
"""
import argparse, concurrent.futures as cf, json, os, sys, time

from app import ledger, model
from evalkit import labels as L

HOME_CAP, SNIPPET_CAP, SEARCH_CAP = 12000, 700, 80000

SYSTEM = """You are a procurement analyst. A buyer is deciding whether to shortlist a software vendor. You get the
vendor's homepage text and web search results about the vendor. Give one color: Green (solid, shortlist), Yellow
(shortlist only after a named check) or Red (do not shortlist). List green, yellow and red flags; each flag names
the URL of the homepage or of the search result it comes from and a short verbatim quote from that text.
Answer {"overall": "Green|Yellow|Red", "green_flags": [{"text": "...", "url": "...", "quote": "..."}],
"yellow_flags": [...], "red_flags": [...], "reasoning": "two or three sentences"}"""


def _clip(s, n):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 3] + "..."


def homepage(research):
    """-> (url, text): the research's fetch of its own URL (or of the shortest same-domain page), else a cached fetch."""
    url = research.get("url", "")
    want, dom = L.key(url), ledger.registrable(url)
    fetched = [(u, s) for u, s in (research.get("sources") or {}).items()
               if isinstance(s, dict) and s.get("kind") == "fetch" and s.get("text")]
    exact = [(u, s) for u, s in fetched if L.key(u) == want]
    same = sorted(((u, s) for u, s in fetched if ledger.registrable(u) == dom), key=lambda us: len(us[0]))
    for u, s in exact + same:
        return u, s["text"]
    try:
        from app.research import tools
        r = tools.fetch(url)
        return r.get("final_url") or url, r.get("text") or ""
    except Exception as e:  # the baseline degrades to search results only; never fails the batch
        return url, f"(homepage not retrieved: {type(e).__name__})"


def search_results(research):
    out = [{"url": u, "title": s.get("title") or "", "date": s.get("published_date") or "",
            "snippet": _clip(s.get("text"), SNIPPET_CAP)}
           for u, s in (research.get("sources") or {}).items() if isinstance(s, dict) and s.get("kind") == "search"]
    return out


def prompt(research):
    hurl, htext = homepage(research)
    lines, used = [], 0
    for r in search_results(research):
        line = f"- {r['title']} ({r['date']}) {r['url']}\n  {r['snippet']}" if r["date"] else \
            f"- {r['title']} {r['url']}\n  {r['snippet']}"
        if used + len(line) > SEARCH_CAP:
            break
        lines.append(line)
        used += len(line)
    return (f"Vendor URL: {research.get('url', '')}\n\nHomepage ({hurl}):\n{_clip(htext, HOME_CAP)}\n\n"
            f"Search results ({len(lines)}):\n" + ("\n".join(lines) or "(none)"))


def _flags(ans, bucket):
    return [{"text": str(f.get("text") or ""), "url": str(f.get("url") or ""), "quote": str(f.get("quote") or "")}
            for f in ans.get(bucket) or [] if isinstance(f, dict) and f.get("text")]


def run_one(research):
    t0 = time.time()
    line = {"url": research.get("url", ""), "overall": "", "call": "", "reasons": [], "green_flags": [],
            "yellow_flags": [], "red_flags": [], "reasoning": "", "seconds": 0.0, "error": None,
            "mode": model.mode(), "run_id": "baseline", "model": model.model_id()}
    try:
        ans = model.ask_json(SYSTEM, prompt(research), purpose="baseline")
        color = L.color(ans.get("overall"))
        fl = {b: _flags(ans, b) for b in ("red_flags", "yellow_flags", "green_flags")}
        line.update(overall=color, call=color, reasoning=str(ans.get("reasoning") or ""), **fl,
                    reasons=[f for b in ("red_flags", "yellow_flags", "green_flags") for f in fl[b]],
                    error=None if color else f"baseline: no color in the answer ({ans.get('overall')!r})")
    except model.ModelError as e:
        line["error"] = f"baseline: {e}"
    line["seconds"] = round(time.time() - t0, 1)
    return line


def researches(run_dir):
    out = []
    for d in sorted(os.listdir(run_dir)) if os.path.isdir(run_dir) else []:
        p = os.path.join(run_dir, d, "research.json")
        if os.path.isfile(p):
            try:
                with open(p) as f:
                    r = json.load(f)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("url"):
                out.append(r)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--concurrency", type=int, default=int(os.environ.get("CONC") or 8))
    a = ap.parse_args(argv)
    rs = researches(a.run_dir)
    if not rs:
        print(f"baseline: no research.json under {a.run_dir}", file=sys.stderr)
        return 2
    with cf.ThreadPoolExecutor(max(1, a.concurrency)) as ex:
        lines = list(ex.map(run_one, rs))
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w") as f:
        f.writelines(json.dumps(x) + "\n" for x in lines)
    errs = sum(bool(x["error"]) for x in lines)
    print(f"baseline: {len(lines)} products, {errs} errors -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
