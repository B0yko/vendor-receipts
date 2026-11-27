"""Dataset-leak scanner behind tests/test_leak.py: no product name, vendor name or registrable domain of a labelled
row may appear in the agent's files (SPEC § Dataset policy).

    from evalkit import leak
    hits = leak.scan(ROOT, leak.terms(labels.load()), leak.load_allow("tests/leak_allow.txt")[0])
    # -> ["app/x.py:12:acme widgets", ...]  (file:line:term; empty when clean)

Terms: product, vendor (also without a legal suffix such as GmbH or Inc) and app.ledger.registrable(url), lowercased,
at least MIN_LEN characters, minus the allow list. Matching is case-insensitive and whole-word (no letter or digit
right before or after the term; spaces inside a term match any whitespace).
"""
import os, re

from app.ledger import registrable

MIN_LEN = 4
# What the agent reads or ships: its code and prompts, fixtures, configs, docs.
SCAN = ("app", "tests/fixtures", "tests/control_cases.jsonl", "Makefile", "SPEC.md", "README.md", "AGENTS.md",
        "pyproject.toml", "llm.py", "cache.py")
SKIP_DIRS = {"__pycache__", ".git", ".venv", "node_modules"}
_LEGAL = re.compile(r"[\s,]+(gmbh(\s*&\s*co\.?\s*kg)?|inc\.?|ltd\.?|llc|l\.l\.c\.|ag|se|plc|corp\.?|corporation|"
                    r"co\.?|company|limited|s\.a\.?|b\.v\.?|n\.v\.?|oy|ab|a/s|as|sas|sarl|s\.r\.l\.|srl|kg|ug|pty)$",
                    re.I)


def _norm(s):
    return " ".join(str(s or "").lower().split())


def terms(rows):
    """-> sorted set of lowercased terms from label rows (product, vendor, vendor minus legal suffix, domain)."""
    out = set()
    for r in rows:
        vendor = _norm(r.get("vendor"))
        cands = [_norm(r.get("product")), vendor, _LEGAL.sub("", vendor).strip(" ,.")]
        url = str(r.get("url") or "").strip()
        if url:
            cands.append(registrable(url if "://" in url else "https://" + url))
        out.update(c for c in cands if len(c) >= MIN_LEN)
    return sorted(out)


def load_allow(path):
    """-> (allowed terms, problems). One generic word per line followed by '# reason'; blank lines and lines that
    start with '#' are comments. A line without a reason is a problem (the test fails on it)."""
    allowed, problems = set(), []
    if not os.path.exists(path):
        return allowed, problems
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            word, _, reason = s.partition("#")
            if not reason.strip():
                problems.append(f"{os.path.basename(path)}:{n}: '{word.strip()}' has no '# reason'")
            if word.strip():
                allowed.add(_norm(word))
    return allowed, problems


def pattern(ts):
    """One case-insensitive whole-word regex for all terms (longest first), or None when there are none."""
    if not ts:
        return None
    alts = [r"\s+".join(map(re.escape, t.split())) for t in sorted(ts, key=len, reverse=True)]
    return re.compile(r"(?<![0-9A-Za-z])(?:" + "|".join(alts) + r")(?![0-9A-Za-z])", re.I)


def files(root, scan=SCAN):
    """-> relative paths of the text files under the scanned paths (missing paths skipped, binaries skipped)."""
    out = []
    for p in scan:
        full = os.path.join(root, p)
        if os.path.isfile(full):
            out.append(p)
        elif os.path.isdir(full):
            for d, dirs, fs in os.walk(full):
                dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS)
                out += [os.path.relpath(os.path.join(d, f), root) for f in sorted(fs) if not f.endswith(".pyc")]
    return out


def _text(full):
    with open(full, "rb") as f:
        b = f.read()
    if b"\0" in b[:4096]:
        return None
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return None


def scan(root, ts, allow=(), scan=SCAN):
    """-> ["file:line:term", ...] for every term hit in the scanned files."""
    rx = pattern([t for t in ts if t not in set(allow)])
    if rx is None:
        return []
    hits = []
    for rel in files(root, scan):
        text = _text(os.path.join(root, rel))
        if text is None:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            hits += [f"{rel}:{n}:{_norm(m.group(0))}" for m in rx.finditer(line)]
    return hits
