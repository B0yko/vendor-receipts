"""Verdict outputs: one JSON line per product, the Stackgini-shaped CSV and its full companion (lane L5).

    line = writer.jsonl_line(verdict)                 # call = overall, one line, no trailing newline
    writer.append_jsonl(verdict, "runs/v.jsonl")      # thread-safe append + flush (one lock per process)
    writer.write_csv(verdicts, "runs/v.csv")          # also writes runs/v.full.csv

CSV columns (Stackgini's): url, product, vendor, overall, green_flags, yellow_flags, red_flags, reasoning. Each flag is
"text (url)", joined by "; ". The full CSV adds reliability, reliability_why, blind_spots, buyer_next_step,
vendor_questions, would_flip, trace. Rows keep the order of the list given (the runner passes input order).
"""
import csv, json, os, threading

COLUMNS = ("url", "product", "vendor", "overall", "green_flags", "yellow_flags", "red_flags", "reasoning")
FULL_EXTRA = ("reliability", "reliability_why", "blind_spots", "buyer_next_step", "vendor_questions", "would_flip",
              "trace")
FULL_COLUMNS = COLUMNS + FULL_EXTRA
_lock = threading.Lock()


def jsonl_line(verdict):
    v = dict(verdict)
    v["call"] = v.get("overall")
    return json.dumps(v, ensure_ascii=False, default=str)


def append_jsonl(verdict, path, lock=None):
    with lock or _lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(jsonl_line(verdict) + "\n")
            f.flush()


def _one_line(s):
    return " ".join(str(s or "").split())


def fmt_flags(flags):
    out = []
    for f in flags or []:
        text, url = _one_line(f.get("text")), (f.get("url") or "").strip()
        out.append(f"{text} ({url})" if url else text)
    return "; ".join(out)


def fmt_list(xs):
    if isinstance(xs, str):
        return _one_line(xs)
    return "; ".join(_one_line(x) for x in xs or [] if _one_line(x))


def row(verdict, full=False):
    v = verdict
    r = {"url": v.get("url", ""), "product": _one_line(v.get("product")), "vendor": _one_line(v.get("vendor")),
         "overall": v.get("overall", ""), "green_flags": fmt_flags(v.get("green_flags")),
         "yellow_flags": fmt_flags(v.get("yellow_flags")), "red_flags": fmt_flags(v.get("red_flags")),
         "reasoning": _one_line(v.get("reasoning"))}
    if full:
        r.update({"reliability": v.get("reliability", ""), "reliability_why": _one_line(v.get("reliability_why")),
                  "blind_spots": fmt_list(v.get("blind_spots")), "buyer_next_step": _one_line(v.get("buyer_next_step")),
                  "vendor_questions": fmt_list(v.get("vendor_questions")), "would_flip": _one_line(v.get("would_flip")),
                  "trace": v.get("trace", "")})
    return r


def full_path(path):
    stem, _ = os.path.splitext(path)
    return stem + ".full.csv"


def write_csv(verdicts, path):
    """Writes `path` (Stackgini columns) and `<path stem>.full.csv`; returns both paths."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    for p, cols, full in ((path, COLUMNS, False), (full_path(path), FULL_COLUMNS, True)):
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for v in verdicts:
                w.writerow(row(v, full))
    return path, full_path(path)
