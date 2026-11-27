#!/usr/bin/env python3
"""Score an agent's verdicts against labelled examples. Python stdlib only; generic (no product or vendor logic).

  eval.py LABELS [PREDS] [--cases-out FILE] [--key F] [--label F] [--pred-key F] [--pred-label F] [--synonyms MAP|FILE]
          [--split all|dev|holdout] [--holdout-pct 30] [--baseline FILE] [--max-flips 1] [--save FILE]
          [--links] [--link-cache DIR] [--official "CMD {preds} {labels}"] [--min-accuracy X] [--strict] [--md]

LABELS, PREDS: CSV/TSV, JSON (a list, or an object holding one) or JSONL. Field names are auto-detected
(key: url, website, product_url, domain, id; label: call, verdict, label, decision) or set by flags.
Prediction contract (what the product's `make batch` writes, one JSON line per case):
  {"url": ..., "call": ..., "reasons": [{"text": ..., "url": ..., "quote": ...}], "seconds": 3.1, "error": null,
   "mode": "live"|"replay"}
Keys are normalised (scheme, www., trailing slash, case), labels too (case, spaces, --synonyms "buy=solid,avoid=hollow").
--split: a deterministic hash split, so hill-climbing on dev never sees the holdout cases.
--baseline: a file written earlier by --save; exit 1 if more than --max-flips cases went right->wrong or the net moved down.
--links: HEAD/GET every reason URL (1 request per host per second, cached) and look for each `quote` on its page.
--official: run the organiser's checker (argv, no shell) and show its own output and score next to ours.
--cases-out FILE: write the split's keys as the runner's input JSONL ({"url": ...}; no calls, no reasons) and exit.
Exit: 0 ok, 1 regression / below --min-accuracy / --strict shape failure / --strict official failure, 2 bad input.
"""
import argparse, collections, concurrent.futures as cf, csv, hashlib, html, io, json, os, re, shlex, statistics
import subprocess, sys, threading, time, urllib.error, urllib.parse, urllib.request

KEY_FIELDS = ("url", "website", "product_url", "product", "domain", "link", "id", "case_id")
LABEL_FIELDS = ("call", "verdict", "label", "decision", "rating", "class", "outcome")
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
UA = "Mozilla/5.0 (compatible; eval-linkcheck/1; hackathon evaluation, low rate)"


def read(path):
    with open(path, encoding="utf-8-sig") as f:
        return f.read()


def write_json(path, obj, **kw):
    with open(path, "w") as f:
        json.dump(obj, f, **kw)


def load_records(path):
    """-> list of dicts from CSV/TSV, JSON or JSONL."""
    text = read(path)
    s = text.lstrip()
    jsonl = lambda: [json.loads(line) for line in text.splitlines() if line.strip()]
    if path.endswith(".jsonl") or path.endswith(".ndjson"):
        return jsonl()
    if s[:1] in "[{":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return jsonl()
        if isinstance(data, dict):
            lists = [v for k, v in data.items() if isinstance(v, list)]
            if lists:
                data = lists[0]
            elif all(isinstance(v, dict) for v in data.values()):
                data = [dict(v, **{"id": k}) if "id" not in v else v for k, v in data.items()]
            else:
                data = [data]
        return [d for d in data if isinstance(d, dict)]
    try:
        sniffed = csv.Sniffer().sniff(text[:4096], delimiters=",;\t").delimiter
    except csv.Error:
        sniffed = ","
    # The header line decides the delimiter: long quoted multi-line fields fool the sniffer (Stackgini's CSV did).
    head = text.lstrip("\ufeff").splitlines()[0] if text.strip() else ""
    delim = max(",;\t", key=lambda d: (head.count(d), d == sniffed))
    out = []
    for row in csv.DictReader(io.StringIO(text), delimiter=delim):
        rec = {}
        for k, v in row.items():
            if isinstance(v, list):  # the extra cells of a ragged row
                v = delim.join(x or "" for x in v)
            rec[(k or "_extra").strip()] = (v or "").strip()
        out.append(rec)
    return out


def pick_field(records, wanted, candidates, what):
    if wanted:
        return wanted
    keys = {k.lower(): k for r in records[:20] for k in r}
    for c in candidates:
        if c in keys:
            return keys[c]
    raise SystemExit(f"eval: cannot find the {what} field among {sorted(keys)}; pass it with a flag")


def norm_key(s):
    low = str(s or "").strip().lower()
    if re.match(r"^[a-z][a-z0-9+.-]*://", low) or re.match(r"^(www\.)?[a-z0-9-]+(\.[a-z0-9-]+)+(:\d+)?(/|$)", low):
        p = urllib.parse.urlsplit(low if "://" in low else "http://" + low)
        host = (p.hostname or "").removeprefix("www.")
        port = f":{p.port}" if p.port and p.port not in (80, 443) else ""
        return host + port + p.path.rstrip("/") + ("?" + p.query if p.query else "")
    return " ".join(low.split())


def parse_synonyms(spec):
    if not spec:
        return {}
    if os.path.exists(spec):
        return {k.lower(): v.lower() for k, v in json.loads(read(spec)).items()}
    return {a.strip().lower(): b.strip().lower() for a, b in (p.split("=", 1) for p in spec.split(",") if "=" in p)}


def norm_label(s, syn=None):
    v = " ".join(str(s if s is not None else "").strip().lower().split())
    return (syn or {}).get(v, v)


def valid_url(u):
    try:
        p = urllib.parse.urlsplit(str(u))
    except ValueError:
        return False
    return p.scheme in ("http", "https") and bool(p.hostname) and ("." in p.hostname or p.hostname == "localhost")


def reasons_of(rec):
    r = rec.get("reasons", rec.get("evidence", []))
    if isinstance(r, str):
        t = r.strip()
        try:
            r = json.loads(t) if t.startswith(("[", "{")) else [x for x in t.splitlines() if x.strip()]
        except ValueError:
            r = [t]
    if isinstance(r, dict):
        r = [r]
    out = []
    for x in r or []:
        if isinstance(x, str):
            m = URL_RE.search(x)
            out.append({"text": x, "url": m.group(0) if m else "", "quote": ""})
        elif isinstance(x, dict):
            out.append({"text": str(x.get("text") or x.get("reason") or x.get("claim") or ""),
                        "url": str(x.get("url") or x.get("link") or x.get("source") or ""),
                        "quote": str(x.get("quote") or x.get("excerpt") or "")})
    return out


def split_of(key, holdout_pct):
    return "holdout" if int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % 100 < holdout_pct else "dev"


def stratified_splitter(labels, lk, ll, holdout_pct, syn=None):
    """-> key -> 'dev'|'holdout'. Per class, keys ranked by sha256; the first round(n * pct / 100) go to holdout
    (at least 1 when a class has 3+ cases). Deterministic, and every class is in both splits. Unknown keys: hash."""
    if not ll:
        return lambda k: split_of(k, holdout_pct)
    by_class = collections.defaultdict(list)
    for r in labels:
        k = norm_key(r.get(lk))
        if k:
            by_class[norm_label(r.get(ll), syn)].append(k)
    hold = set()
    for keys in by_class.values():
        keys = sorted(set(keys), key=lambda k: hashlib.sha256(k.encode()).hexdigest())
        n = round(len(keys) * holdout_pct / 100)
        hold.update(keys[:max(n, 1 if len(keys) >= 3 else 0)])
    known = {k for keys in by_class.values() for k in keys}
    return lambda k: ("holdout" if k in hold else "dev") if k in known else split_of(k, holdout_pct)


def page_text(raw):
    t = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", raw)
    return " ".join(html.unescape(re.sub(r"(?s)<[^>]+>", " ", t)).lower().split())


def check_links(urls, cache_dir, need_text=(), timeout=10, delay=1.0, workers=8):
    """-> {url: {"status", "ok" (True/False/None=unknown), "error", "text"}}; one request per host per `delay` s."""
    os.makedirs(cache_dir, exist_ok=True)
    need_text, results, lock = set(need_text), {}, threading.Lock()

    def fetch(url):
        f = os.path.join(cache_dir, hashlib.sha256(url.encode()).hexdigest()[:24] + ".json")
        if os.path.exists(f):
            c = json.loads(read(f))
            if url not in need_text or c.get("text") is not None or c.get("ok") is False:
                return c
        res = {"url": url, "status": None, "ok": None, "error": "", "text": None}
        for method in (["GET"] if url in need_text else ["HEAD", "GET"]):
            try:
                req = urllib.request.Request(url, method=method, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    res.update(status=r.status, ok=True, error="")
                    if method == "GET" and url in need_text:
                        res["text"] = page_text(r.read(2_000_000).decode("utf-8", "replace"))
                break
            except urllib.error.HTTPError as e:
                e.close()
                res.update(status=e.code, ok=None if e.code in (401, 403, 405, 429) else False, error=f"HTTP {e.code}")
                if e.code not in (403, 405, 429) or method == "GET":
                    break
            except Exception as e:  # DNS, TLS, timeout
                res.update(ok=False, error=type(e).__name__ + ": " + str(e)[:120])
                break
            time.sleep(delay)
        write_json(f, res)
        return res

    def per_host(host_urls):
        for i, u in enumerate(host_urls):
            r = fetch(u)
            with lock:
                results[u] = r
            if i < len(host_urls) - 1:
                time.sleep(delay)

    by_host = collections.defaultdict(list)
    for u in dict.fromkeys(u for u in urls if valid_url(u)):
        by_host[urllib.parse.urlsplit(u).hostname].append(u)
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(per_host, by_host.values()))
    return results


def kappa(pairs):
    n = len(pairs)
    if not n:
        return 0.0
    po = sum(g == p for g, p in pairs) / n
    gc, pc = collections.Counter(g for g, _ in pairs), collections.Counter(p for _, p in pairs)
    pe = sum(gc[k] * pc[k] for k in gc) / (n * n)
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


def run_official(cmd, preds, labels):
    argv = [a.replace("{preds}", preds).replace("{labels}", labels) for a in shlex.split(cmd)]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=300)
        out, rc = (p.stdout + p.stderr).strip(), p.returncode
    except (OSError, subprocess.TimeoutExpired) as e:
        out, rc = f"{type(e).__name__}: {e}", -1
    score = None
    for pat in (r"(\d+(?:\.\d+)?)\s*%", r"(\d+)\s*/\s*(\d+)", r"(?:score|accuracy|acc)\s*[:=]\s*([0-9.]+)"):
        m = re.findall(pat, out, re.I)
        if m:
            score = "/".join(m[-1]) if isinstance(m[-1], tuple) else m[-1]
            break
    return {"exit": rc, "score": score, "tail": out.splitlines()[-8:]}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("labels"), ap.add_argument("preds", nargs="?")
    for f in ("--key", "--label", "--pred-key", "--pred-label", "--synonyms", "--baseline", "--save", "--official",
              "--cases-out"):
        ap.add_argument(f)
    ap.add_argument("--split", choices=("all", "dev", "holdout"), default="all")
    ap.add_argument("--holdout-pct", type=int, default=30)
    ap.add_argument("--max-flips", type=int, default=1)
    ap.add_argument("--min-accuracy", type=float)
    ap.add_argument("--links", action="store_true")
    ap.add_argument("--link-cache", default=".cache/links")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--md", action="store_true")
    a = ap.parse_args(argv)
    if a.cases_out:  # the runner's input: keys only, never the calls or reasons
        labels = load_records(a.labels)
        lk = pick_field(labels, a.key, KEY_FIELDS, "label key")
        ll = a.label or next((f for f in LABEL_FIELDS if labels and f in labels[0]), None)
        sp = stratified_splitter(labels, lk, ll, a.holdout_pct, parse_synonyms(a.synonyms))
        keys = [r[lk] for r in labels if norm_key(r.get(lk)) and (a.split == "all" or sp(norm_key(r[lk])) == a.split)]
        os.makedirs(os.path.dirname(os.path.abspath(a.cases_out)), exist_ok=True)
        with open(a.cases_out, "w") as f:
            f.writelines(json.dumps({"url": k}) + "\n" for k in keys)
        print(f"wrote {len(keys)} cases ({a.split}) to {a.cases_out}")
        return 0
    if not a.preds:
        ap.error("PREDS is required unless --cases-out is given")
    try:
        labels, preds = load_records(a.labels), load_records(a.preds) if os.path.getsize(a.preds) else []
    except (OSError, ValueError) as e:
        print(f"eval: cannot read input: {e}", file=sys.stderr)
        return 2
    if not labels:
        print("eval: no labelled cases", file=sys.stderr)
        return 2
    syn = parse_synonyms(a.synonyms)
    lk, ll = pick_field(labels, a.key, KEY_FIELDS, "label key"), pick_field(labels, a.label, LABEL_FIELDS, "label call")
    pk = pick_field(preds, a.pred_key, (lk,) + KEY_FIELDS, "prediction key") if preds else lk
    pl = pick_field(preds, a.pred_label, (ll,) + LABEL_FIELDS, "prediction call") if preds else ll
    gold = {}
    sp = stratified_splitter(labels, lk, ll, a.holdout_pct, syn)
    for r in labels:
        k = norm_key(r.get(lk))
        if k and (a.split == "all" or sp(k) == a.split):
            gold[k] = norm_label(r.get(ll), syn)
    all_gold = {norm_key(r.get(lk)) for r in labels}
    seen, pred, dups = {}, {}, []
    for r in preds:
        k = norm_key(r.get(pk))
        if k in seen:
            dups.append(k)
        seen[k] = r
        pred[k] = norm_label(r.get(pl), syn) or None
    missing = [k for k in gold if k not in seen]
    extra = [k for k in seen if k not in all_gold]
    cases, pairs = [], []
    for k, g in gold.items():
        r = seen.get(k, {})
        rs = reasons_of(r) if r else []
        p = pred.get(k)
        pairs.append((g, p or "<none>"))
        cases.append({"key": k, "split": sp(k), "gold": g, "pred": p, "correct": p == g,
                      "reasons": rs, "seconds": r.get("seconds"), "error": r.get("error") or None, "mode": r.get("mode")})
    n, correct = len(cases), sum(c["correct"] for c in cases)
    acc = correct / n
    classes = sorted({g for g, _ in pairs} | {p for _, p in pairs})
    conf = collections.Counter(pairs)
    per = {}
    for c in classes:
        tp, fp, fn = conf[(c, c)], sum(conf[(g, c)] for g in classes if g != c), sum(conf[(c, p)] for p in classes if p != c)
        pr, rc = tp / (tp + fp) if tp + fp else 0.0, tp / (tp + fn) if tp + fn else 0.0
        per[c] = {"p": pr, "r": rc, "f1": 2 * pr * rc / (pr + rc) if pr + rc else 0.0, "n": tp + fn}
    rs_all = [x for c in cases for x in c["reasons"]]
    with_url = sum(bool(x["url"]) for x in rs_all)
    bad_url = sum(bool(x["url"]) and not valid_url(x["url"]) for x in rs_all)
    linked_cases = sum(bool(c["reasons"]) and all(valid_url(x["url"]) for x in c["reasons"]) for c in cases if c["pred"])
    secs = sorted(float(c["seconds"]) for c in cases if isinstance(c["seconds"], (int, float)))
    errors = [c["key"] for c in cases if c["error"]]
    modes = collections.Counter(c["mode"] or "?" for c in cases if c["pred"])
    summary = {"n": n, "predicted": n - len(missing), "correct": correct, "accuracy": round(acc, 4),
               "kappa": round(kappa(pairs), 4), "missing": len(missing), "extra": len(extra), "duplicates": len(dups),
               "errors": len(errors), "reasons_per_case": round(len(rs_all) / n, 2),
               "reasons_with_url": round(with_url / len(rs_all), 4) if rs_all else 0.0, "bad_urls": bad_url,
               "cases_all_linked": linked_cases, "modes": dict(modes), "per_class": per,
               "median_s": statistics.median(secs) if secs else None,
               "p90_s": secs[min(len(secs) - 1, int(0.9 * len(secs)))] if secs else None,
               "max_s": secs[-1] if secs else None}
    print(f"eval: {n} labelled (split {a.split}), {summary['predicted']} predicted | missing {len(missing)} "
          f"extra {len(extra)} duplicates {len(dups)} errors {len(errors)} | modes {dict(modes)}")
    print(f"accuracy {acc:.3f} ({correct}/{n})  kappa {summary['kappa']:.2f}")
    print("class".ljust(14) + "    P     R    F1    n")
    for c in classes:
        v = per[c]
        print(f"{c[:14]:<14} {v['p']:.2f}  {v['r']:.2f}  {v['f1']:.2f}  {v['n']:>3}")
    print("confusion (row gold -> col pred): " + "; ".join(
        f"{g}: " + ", ".join(f"{p}={conf[(g, p)]}" for p in classes if conf[(g, p)]) for g in classes if any(conf[(g, p)] for p in classes)))
    print(f"evidence: {summary['reasons_per_case']} reasons/case, {with_url}/{len(rs_all)} with URL, "
          f"{bad_url} malformed, {linked_cases} cases with every reason linked")
    if secs:
        print(f"time: median {summary['median_s']:.1f} s, p90 {summary['p90_s']:.1f} s, max {summary['max_s']:.1f} s")
    for name, ks in (("missing", missing), ("extra", extra), ("duplicates", dups), ("errors", errors)):
        if ks:
            print(f"  {name}: {', '.join(ks[:10])}{' ...' if len(ks) > 10 else ''}")
    rc = 0
    if a.links:
        urls = [x["url"] for x in rs_all if valid_url(x["url"])]
        res = check_links(urls, a.link_cache, need_text={x["url"] for x in rs_all if x["quote"]})
        ok = sum(1 for u in urls if res.get(u, {}).get("ok") is True)
        broken = [u for u in dict.fromkeys(urls) if res.get(u, {}).get("ok") is False]
        unknown = [u for u in dict.fromkeys(urls) if res.get(u, {}).get("ok") is None]
        q = [(x["quote"], res.get(x["url"], {}).get("text")) for x in rs_all if x["quote"] and valid_url(x["url"])]
        qn = lambda s: " ".join(s.lower().split())
        found = sum(1 for qt, t in q if t is not None and qn(qt)[:120] in t)
        checked = sum(1 for _, t in q if t is not None)
        summary.update(links_ok=ok, links_total=len(urls), links_broken=len(broken), links_unknown=len(unknown),
                       quotes_found=found, quotes_checked=checked, quotes_total=len(q))
        print(f"links: {ok}/{len(urls)} open, {len(broken)} broken, {len(unknown)} unknown (401/403/405/429)"
              f" | quotes found on page {found}/{checked} checked ({len(q)} given)")
        for u in broken[:10]:
            print(f"  broken: {u} ({res[u]['error']})")
        for c in cases:
            for x in c["reasons"]:
                r = res.get(x["url"], {})
                x["link_ok"] = r.get("ok")
                if x["quote"]:
                    x["quote_found"] = None if r.get("text") is None else qn(x["quote"])[:120] in r["text"]
    if a.official:
        off = run_official(a.official, a.preds, a.labels)
        summary["official"] = off
        print(f"official checker: exit {off['exit']}, score {off['score'] if off['score'] is not None else 'not parsed'}")
        for line in off["tail"]:
            print("  | " + line)
        if off["exit"] != 0 and a.strict:
            rc = 1
    if a.baseline:
        base = {c["key"]: c["correct"] for c in json.loads(read(a.baseline))["cases"]}
        r2w = [c["key"] for c in cases if base.get(c["key"]) is True and not c["correct"]]
        w2r = [c["key"] for c in cases if base.get(c["key"]) is False and c["correct"]]
        net = len(w2r) - len(r2w)
        bad = len(r2w) > a.max_flips or net < 0
        summary["baseline"] = {"right_to_wrong": r2w, "wrong_to_right": w2r, "net": net, "fail": bad}
        print(f"baseline: right->wrong {len(r2w)} {r2w[:6]}, wrong->right {len(w2r)} {w2r[:6]}, net {net:+d} -> "
              + (f"FAIL (max flips {a.max_flips}, net must be >= 0)" if bad else "ok"))
        rc = rc or int(bad)
    if a.min_accuracy is not None and acc < a.min_accuracy:
        print(f"FAIL: accuracy {acc:.3f} below --min-accuracy {a.min_accuracy}")
        rc = 1
    if a.strict and (missing or extra or dups or bad_url or any(not c["pred"] for c in cases)):
        print("FAIL (--strict): missing, extra, duplicate, empty or malformed predictions")
        rc = 1
    if a.md:
        lk_ = f"{summary.get('links_ok', '-')}/{summary.get('links_total', '-')}"
        print("\n| Cases | Accuracy | Cohen's kappa | Reasons with a link | Links that open |\n|---|---|---|---|---|")
        print(f"| {n} ({a.split}) | {acc:.0%} | {summary['kappa']:.2f} | {summary['reasons_with_url']:.0%} | {lk_} |")
    if a.save:
        os.makedirs(os.path.dirname(os.path.abspath(a.save)), exist_ok=True)
        write_json(a.save, {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "labels": a.labels, "preds": a.preds,
                            "split": a.split, "summary": summary, "cases": cases}, indent=1)
        print(f"saved {a.save}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
