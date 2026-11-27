"""The labelled examples with their split, through tools/eval.py's loader and stratified split (same args as
`make cases`: --label overall, --holdout-pct 30), so evalkit and `make eval` / `make holdout` agree on every row.

    from evalkit import labels
    rows = labels.load()              # [{CSV columns..., "key", "color", "split"}]; 40 rows: 28 dev / 12 holdout
    dev = labels.load(split="dev")
    preds = labels.load_preds(path)   # {key: verdict line}, keyed like the rows (tools/eval.py norm_key of url)

LABELS (env) names the CSV, default data/hackathon_40_solutions_commented.csv (relative to the cwd, then to the repo).
Columns: id, url, product, vendor, overall, green_flags, yellow_flags, red_flags, reasoning, reviewer_notes_original.
Never print rows: callers write ids, colors and aggregates only.
"""
import importlib.util, os, threading

_ev_lock = threading.Lock()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT = "data/hackathon_40_solutions_commented.csv"
KEY, LABEL, HOLDOUT_PCT = "url", "overall", 30
COLORS = ("Green", "Yellow", "Red")
_ev = None


def ev():
    """tools/eval.py as a module (it is a script, not a package)."""
    global _ev
    with _ev_lock:  # products run in threads: never hand out a half-initialised module
        if _ev is None:
            spec = importlib.util.spec_from_file_location("_tools_eval", os.path.join(ROOT, "tools", "eval.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _ev = mod
    return _ev


def path(p=None):
    p = p or os.environ.get("LABELS") or DEFAULT
    if not os.path.isabs(p) and not os.path.exists(p) and os.path.exists(os.path.join(ROOT, p)):
        p = os.path.join(ROOT, p)
    return p


def exists(p=None):
    return os.path.isfile(path(p))


def color(s):
    """'green' / ' Red ' / 'YELLOW' -> 'Green' / 'Red' / 'Yellow'; anything else -> ''."""
    v = " ".join(str(s or "").strip().lower().split())
    v = {"amber": "yellow", "orange": "yellow"}.get(v, v)
    return v.capitalize() if v.capitalize() in COLORS else ""


def key(url):
    return ev().norm_key(url)


def load(p=None, split=None, holdout_pct=HOLDOUT_PCT):
    """-> rows with "key", "color" and "split" ('dev' | 'holdout'); split=None keeps all rows."""
    e = ev()
    recs = e.load_records(path(p))
    sp = e.stratified_splitter(recs, KEY, LABEL, holdout_pct)
    out = []
    for r in recs:
        k = e.norm_key(r.get(KEY))
        if not k:
            continue
        row = dict(r, key=k, color=color(r.get(LABEL)), split=sp(k))
        row["id"] = str(r.get("id") or k)
        if split in (None, "all") or row["split"] == split:
            out.append(row)
    return out


def load_preds(p):
    """-> {key: line} from a verdict JSONL (or any format tools/eval.py reads); the last line per key wins."""
    if not p or not os.path.exists(p) or not os.path.getsize(p):
        return {}
    e = ev()
    return {e.norm_key(r.get("url")): r for r in e.load_records(p) if e.norm_key(r.get("url"))}


def pred_color(line):
    return color((line or {}).get("overall") or (line or {}).get("call"))
