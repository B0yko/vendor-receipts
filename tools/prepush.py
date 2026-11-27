#!/usr/bin/env python3
"""Pre-push guard and the leak rules shared with tools/publish_check.py. Stdlib only.

  prepush.py --hook        as .git hook (hooks/pre-push; git's "<lref> <lsha> <rref> <rsha>" lines on stdin): every
                           commit the push adds (tree, paths, message)
  prepush.py [--rev REV]   REV (default HEAD) and its whole history; exit 1 on any finding
  --allow-labelled-sources or env PREPUSH_ALLOW_LABELLED_SOURCES=1: "labelled-source" findings (below) pass as INFO

Rules (load_rules + scan; publish_check.py runs the same scan over every commit of every ref):
- private paths: nothing tracked under data/, runs/ or .cache/, no tracked .env file;
- labelled data (LABELS, default data/hackathon_40_solutions_commented.csv; also the other non-blind files under
  data/: the derived case files, data/forbidden.txt): the domains of every record's key field (url, website, ...; host
  and registrable domain; URL hosts of case files), the product and vendor names of the labelled CSV (whole word,
  case-insensitive, at least MIN_NAME characters; the vendor also without its legal suffix) and its row text (any
  RUN-word run of a cell that only one row has) are forbidden in every tracked file, path and commit message;
- blind products (data/ files whose name says blind: data/blind.csv with id,url,product,vendor): their domains are
  allowed only in ZONE (stackgini-verdicts.csv and traces/ at the repo root) and forbidden everywhere else. A labelled
  domain that is also a blind one (equal, or a subdomain either way) is allowed in ZONE too, with the names of its
  labelled row, and so is a labelled name that occurs in a blind file (the same vendor);
- labelled sources: inside ZONE (file content, not paths) the other labelled domains and names are reported as kind
  "labelled-source" (a blind trace citing a labelled-set site, or naming a labelled vendor) with path:line: FAIL by
  default, INFO under the switch above. Labelled row text stays FAIL in ZONE too; outside ZONE nothing changes.
  tools/redact_labelled.py withholds those URLs and names in the working tree before the commit;
- secrets: TOKENS shapes anywhere;
- attribution: ATTRIB phrases in commit and tag messages and in files (this guard's own files are exempt: they hold
  the patterns).
Allow lists: ALLOW and tools/prepush_allow.txt (domains), tests/leak_allow.txt (generic words or domains, one per
line as "word # reason"). Domains match as dot-aligned tokens: docs.vendor.io matches vendor.io, notvendor.io does
not. Every printed term and path has its middle masked, so the output never carries a row, a name or a domain.
"""
import os, re, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
ALLOW = {"example.com", "example.org", "github.com", "web.archive.org", "archive.org", "rdap.org", "tavily.com",
         "api.tavily.com", "haveibeenpwned.com", "aiagentsberlin.xyz", "stackgini.com", "wikipedia.org", "google.com",
         "linkedin.com", "localhost", "127.0.0.1"}
LABELS = "data/hackathon_40_solutions_commented.csv"
ZONE_FILE, ZONE_DIR = "stackgini-verdicts.csv", "traces/"
KEYS = ("url", "website", "product_url", "domain", "homepage", "link", "product", "site")
NAME_KEYS = ("product", "vendor", "product_name", "vendor_name", "company", "company_name")
MIN_NAME, RUN, MAX_BLOB, MAX_LABELS = 5, 10, 20_000_000, 6
URL = re.compile(r"https?://([a-z0-9.-]+\.[a-z]{2,})(?::\d+)?", re.I)
DOMAIN = re.compile(r"^(?:[a-z][a-z0-9+.-]*://)?(?:[^@/\s]*@)?((?:[a-z0-9-]+\.)+[a-z]{2,})\.?(?::\d+)?(?:[/?#]|$)", re.I)
DOMTOK = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+")
WORD = re.compile(r"[^\W_]+")
LEGAL = re.compile(r"[\s,]+(gmbh(\s*&\s*co\.?\s*kg)?|inc\.?|ltd\.?|llc|l\.l\.c\.|ag|se|plc|corp\.?|corporation|"
                   r"co\.?|company|limited|s\.a\.?|b\.v\.?|n\.v\.?|oy|ab|a/s|as|sas|sarl|s\.r\.l\.|srl|kg|ug|pty)$", re.I)
SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu", "ltd", "plc", "or", "ne", "go"}  # as app.ledger
# The phrases are written with character classes so this file does not match itself.
ATTRIB = re.compile(r"co[-]authored|generated[ ]with|claude[ ]code|anthropic[.]com/claude", re.I)
ATTRIB_EXEMPT = {"tools/prepush.py", "tools/publish_check.py", "tests/test_prepush.py", "hooks/pre-push"}
TOKENS = re.compile(r"(?<![A-Za-z0-9_-])(?:sk-[A-Za-z0-9_-]{20,}|tvly-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{30,}"
                    r"|github_pat_[A-Za-z0-9_]{30,}|xox[abeoprs]-[A-Za-z0-9-]{10,}|(?:AKIA|ASIA)[0-9A-Z]{16}(?![0-9A-Z]))"
                    r"|-----BEGIN [A-Z0-9 ]+-----")
PRIVATE = ("data/", "runs/", ".cache/")
ZERO = "0" * 40
SOURCE = "labelled-source"  # a labelled domain or name inside ZONE
KINDS = ("private path", "labelled domain", "labelled name", "labelled text", SOURCE, "blind domain", "token",
         "attribution")
BLIND_KIND = "blind domain"  # outside ZONE
ALLOW_ENV = "PREPUSH_ALLOW_LABELLED_SOURCES"


def git(*args, inp=None, ok=(0,)):
    p = subprocess.run(["git", *args], input=inp, capture_output=True)
    if p.returncode not in ok:
        raise SystemExit(f"prepush: git {' '.join(args[:3])} failed: {p.stderr.decode(errors='replace').strip()}")
    return p.stdout


def host(s):
    m = DOMAIN.match(str(s or "").strip())
    return m.group(1).lower().removeprefix("www.") if m else ""


def registrable(h):
    parts = h.lower().strip(".").removeprefix("www.").split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def domains_of(value):
    h = host(value)
    return {h, registrable(h)} if h else set()


def words(s):
    return WORD.findall(str(s or "").casefold())


def phrase(s):
    return " ".join(words(s))


def grams(ws, n):
    return (" ".join(ws[i:i + n]) for i in range(len(ws) - n + 1))


def mask(t):
    t = str(t)
    return t[:1] + "*" * (len(t) - 1) if len(t) <= 4 else t[:2] + "*" * min(len(t) - 4, 8) + t[-2:]


def in_zone(path):
    return path == ZONE_FILE or path.startswith(ZONE_DIR)


def private(path):
    base = os.path.basename(path)
    return (path.startswith(PRIVATE) or path + "/" in PRIVATE or base == ".env"
            or (base.endswith(".env") and not base.endswith(".env.example")))


def related(a, b):
    return a == b or a.endswith("." + b) or b.endswith("." + a)


class Matcher:
    """Finds forbidden domains (dot-aligned tokens), names (whole-word n-grams) and labelled text runs in a text."""

    def __init__(self, domains, names, runs, name_kind="labelled name"):
        self.domains, self.names, self.runs = dict(domains), set(names), dict(runs)
        self.name_kind = name_kind
        self.lens = sorted({len(n.split()) for n in self.names})

    def domain_hits(self, low):
        """[(kind, domain, offset)] in a casefolded text."""
        hits = []
        for m in DOMTOK.finditer(low):
            labels, off = m.group(0).split("."), m.start()
            starts = [off]
            for lab in labels[:-1]:
                starts.append(starts[-1] + len(lab) + 1)
            for i in range(len(labels) - 1):
                for j in range(i + 2, min(len(labels), i + MAX_LABELS) + 1):
                    d = ".".join(labels[i:j])
                    if d in self.domains:
                        hits.append((self.domains[d], d, starts[i]))
        return hits

    def locate(self, text):
        """[(kind, term, offset)]: every domain and name occurrence; row text once per labelled row."""
        low = str(text).casefold()  # casefold keeps every newline, so offsets give the right line
        hits = self.domain_hits(low) if self.domains else []
        if self.names or self.runs:
            ms = list(WORD.finditer(low))
            ws = [m.group(0) for m in ms]
            for n in self.lens:
                hits += [(self.name_kind, g, ms[i].start()) for i, g in enumerate(grams(ws, n)) if g in self.names]
            rows = {}
            for i, g in enumerate(grams(ws, RUN) if self.runs else ()):
                if g in self.runs:
                    rows.setdefault(self.runs[g], (g, ms[i].start()))  # one hit per labelled row
            hits += [("labelled text", g, o) for g, o in rows.values()]
        return hits

    def find(self, text):
        return {(k, t) for k, t, _ in self.locate(text)}


class Rules:
    def __init__(self, labelled=(), blind=(), names=(), runs=None, zone_names=(), related_doms=(),
                 rows=0, labels_path=LABELS, files=()):
        runs = runs or {}
        self.rows, self.labels_path, self.files = rows, labels_path, list(files)
        self.n_labelled, self.n_blind, self.n_names, self.n_runs = len(labelled), len(blind), len(names), len(runs)
        lab = {d: "labelled domain" for d in labelled}
        self.out = Matcher({**lab, **{d: BLIND_KIND for d in blind}}, names, runs)
        self.zone = Matcher({d: SOURCE for d in lab if d not in related_doms}, set(names) - set(zone_names), runs,
                            name_kind=SOURCE)
        self.n_domains = len(self.out.domains)
        self._paths = {}

    def matcher(self, zone):
        return self.zone if zone else self.out

    def safe(self, path):
        """The path with every forbidden term in it masked (a printed path never names a product)."""
        if path not in self._paths:
            out = path
            for _, term in sorted(self.out.find(path), key=lambda h: -len(h[1])):
                pat = re.escape(term) if "." in term else r"[^0-9a-z]+".join(map(re.escape, term.split()))
                out = re.sub(pat, mask(term), out, flags=re.I)
            self._paths[path] = out
        return self._paths[path]


def _allow_lines(path, reasons=False):
    out = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                s = line.strip()
                if s and not s.startswith("#"):
                    w = s.partition("#")[0].strip().lower() if reasons else s.lower()
                    out.add(w)
    return out


def _read(p):
    try:
        if os.path.getsize(p) > MAX_BLOB:
            return None
        with open(p, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def load_rules(root):
    """Rules from the local, untracked data/ folder and the allow lists (nothing here is ever printed)."""
    try:
        import eval as ev  # tools/eval.py, same folder: the one CSV/JSON loader
    except ImportError:
        ev = None
    labels = os.environ.get("LABELS") or LABELS
    labels = labels if os.path.isabs(labels) else os.path.join(root, labels)
    allow_words = _allow_lines(os.path.join(root, "tests", "leak_allow.txt"), reasons=True)
    allow_doms = set(ALLOW) | _allow_lines(os.path.join(root, "tools", "prepush_allow.txt")) | allow_words
    labelled, blind, blind_text, rows, files = set(), set(), [], [], []
    for dp, _, fs in os.walk(os.path.join(root, "data")):
        for f in sorted(fs):
            p = os.path.join(dp, f)
            text = _read(p)
            if text is None:
                continue
            is_blind = "blind" in f.lower()
            doms = set()
            if f == "forbidden.txt":
                doms |= {host(l) or l.strip().lower() for l in text.splitlines() if l.strip() and not l.startswith("#")}
            if is_blind or "case" in f.lower():
                for h in URL.findall(text):
                    doms |= domains_of(h)
            recs = []
            if ev and f.lower().endswith((".csv", ".tsv", ".json", ".jsonl", ".ndjson")):
                try:
                    recs = ev.load_records(p)
                except Exception:
                    recs = []
            for r in recs:
                for k, v in r.items():
                    if str(k).lower() in KEYS and isinstance(v, str):
                        doms |= domains_of(v)
            if is_blind:
                blind |= doms
                blind_text.append(text)
            else:
                labelled |= doms
            if os.path.exists(labels) and os.path.samefile(p, labels):
                rows = recs
            files.append(("blind " if is_blind else "") + os.path.relpath(p, root))
    for r in rows:  # a product or vendor cell that is a domain counts as a domain, not as a name
        labelled |= set().union(*(domains_of(v) for k, v in r.items() if str(k).lower() in NAME_KEYS))
    ok = lambda d: (d and "." in d and re.search(r"\.[a-z]{2,}$", d)
                    and not any(d == a or d.endswith("." + a) for a in allow_doms))
    blind = {d for d in blind if ok(d)}
    labelled = {d for d in labelled if ok(d)} - blind
    rel = {d for d in labelled if any(related(d, b) for b in blind)}
    names, runs, zone_rows = {}, {}, set()
    for i, r in enumerate(rows):
        low = {str(k).lower(): v for k, v in r.items()}
        row_doms = set().union(*(domains_of(v) for k, v in low.items() if k in KEYS and isinstance(v, str)))
        if row_doms & (blind | rel):
            zone_rows.add(i)
        for k in NAME_KEYS:
            v = " ".join(str(low.get(k) or "").split())
            if not v or host(v):
                continue
            for cand in (v, LEGAL.sub("", v).strip(" ,.")):
                n = phrase(cand)
                if len(n) >= MIN_NAME and n not in allow_words:
                    names.setdefault(n, set()).add(i)
        for v in r.values():
            for g in set(grams(words(v), RUN)):
                runs.setdefault(g, set()).add(i)
    runs = {g: next(iter(rs)) for g, rs in runs.items() if len(rs) == 1}  # text two rows share is boilerplate
    bw = words(" ".join(blind_text))
    in_blind = {g for n in {len(x.split()) for x in names} for g in grams(bw, n)}
    zone_names = {n for n, rs in names.items() if rs & zone_rows or n in in_blind}
    return Rules(labelled, blind, names, runs, zone_names, rel, len(rows), os.path.relpath(labels, root),
                 files)


def objects(shas):
    """{sha: bytes} for the given object ids (one git cat-file --batch)."""
    shas = list(dict.fromkeys(shas))
    if not shas:
        return {}
    data, out, i = git("cat-file", "--batch", inp=("\n".join(shas) + "\n").encode()), {}, 0
    while i < len(data):
        nl = data.index(b"\n", i)
        head = data[i:nl].split()
        if len(head) < 3:
            i = nl + 1
            continue
        size = int(head[2])
        out[head[0].decode()] = data[nl + 1:nl + 1 + size]
        i = nl + 1 + size + 1
    return out


def _tree(data, hl):
    """[(mode, name, sha)] of a raw tree object (hl: object id length in bytes)."""
    out, i = [], 0
    while i < len(data):
        sp = data.index(b" ", i)
        nul = data.index(b"\0", sp)
        out.append((data[i:sp], data[sp + 1:nul].decode("utf-8", "replace"), data[nul + 1:nul + 1 + hl].hex()))
        i = nul + 1 + hl
    return out


def files_of(commits, objs):
    """{commit: [(path, blob sha)]}: every tree read once, one git cat-file per tree depth (not one per commit)."""
    root = {c: objs.get(c, b"").split(b"\n", 1)[0].removeprefix(b"tree ").decode() for c in commits}
    hl, seen, todo = len(next(iter(commits), "")) // 2 or 20, {}, {t for t in root.values() if t}
    while todo:
        got = objects(todo)
        for sha in todo:
            seen[sha] = _tree(got.get(sha, b""), hl)
        todo = {s for sha in todo for mode, _, s in seen[sha] if mode == b"40000" and s not in seen}

    def walk(sha, prefix):
        for mode, name, s in seen.get(sha, ()):
            if mode == b"40000":
                yield from walk(s, prefix + name + "/")
            elif mode != b"160000":  # submodules have no blob here
                yield prefix + name, s
    return {c: list(walk(t, "")) for c, t in root.items()}


def message(obj):
    """The message of a commit or tag object (after the header block)."""
    msg = obj.split(b"\n\n", 1)[1].decode("utf-8", "replace") if b"\n\n" in obj else ""
    return re.split(r"^-----BEGIN (?:PGP|SSH) SIGNATURE-----", msg, maxsplit=1, flags=re.M)[0]  # signed tags


def check_text(text, matcher, attribution=True):
    """-> sorted [(kind, term, line)], one per term and line."""
    low = str(text).casefold()
    hits = {(k, t, low.count("\n", 0, o) + 1) for k, t, o in matcher.locate(text)}  # offsets into the casefold
    raw = []
    for m in TOKENS.finditer(text):
        t = m.group(0)
        body = t.split("-", 1)[-1] if not t.startswith("-----") else "x1"
        if any(c.isdigit() for c in body) and any(c.isalpha() for c in body):
            raw.append(("token", t, m.start()))
    if attribution:
        raw += [("attribution", m.group(0).lower(), m.start()) for m in ATTRIB.finditer(text)]
    hits |= {(k, t, text.count("\n", 0, o) + 1) for k, t, o in raw}
    return sorted(hits, key=lambda h: (h[2], h[0], h[1]))


def scan(commits, rules, tags=()):
    """-> (findings [(commit, where, line, kind, term)], stats); line 0 for a path. Each path once, each (blob, path
    class) once."""
    findings, seen_path, blobs = [], set(), {}
    commits = list(commits)
    objs = objects(commits + [sha for _, sha in tags])
    for c, entries in files_of(commits, objs).items():
        for path, sha in entries:
            if path not in seen_path:
                seen_path.add(path)
                if private(path):
                    findings.append((c, path, 0, "private path", path))
                findings += [(c, path, 0, k + " in path", t) for k, t in sorted(rules.matcher(in_zone(path)).find(path))]
            blobs.setdefault((sha, in_zone(path), path in ATTRIB_EXEMPT), (c, path))
    data = objects([k[0] for k in blobs])
    for (sha, zone, exempt), (c, path) in blobs.items():
        b = data.get(sha)
        if b is None or len(b) > MAX_BLOB:
            continue
        text = b.decode("utf-8", "replace")
        findings += [(c, path, n, k, t) for k, t, n in check_text(text, rules.matcher(zone), attribution=not exempt)]
    for c in commits:
        findings += [(c, "(message)", n, k, t) for k, t, n in check_text(message(objs.get(c, b"")), rules.out)]
    for name, sha in tags:
        findings += [(sha, f"(tag {name})", n, k, t) for k, t, n in check_text(message(objs.get(sha, b"")), rules.out)]
    return findings, {"commits": len(commits), "blobs": len(blobs), "paths": len(seen_path), "tags": len(tags)}


def split(findings, allow_sources):
    """-> (fails, infos): with allow_sources, labelled-source hits in ZONE content are INFO, everything else FAIL."""
    info = [f for f in findings if allow_sources and f[3] == SOURCE]
    return [f for f in findings if not (allow_sources and f[3] == SOURCE)], info


def allow_sources(argv):
    return "--allow-labelled-sources" in argv or os.environ.get(ALLOW_ENV, "").strip().lower() in ("1", "true", "yes")


def fmt(f, rules):
    c, where, line, kind, term = f
    term = rules.safe(term) if kind == "private path" else mask(term)
    return f"{c[:8]}:{rules.safe(where)}:{f'{line}:' if line else ''}{term} ({kind})"


def rev_list(*args):
    return git("rev-list", *args).decode().split()


def pushed(lsha, rsha):
    """Commits a push of lsha over rsha adds (all not on any remote when the remote side is new or unknown)."""
    known = rsha != ZERO and subprocess.run(["git", "cat-file", "-e", rsha + "^{commit}"],
                                            capture_output=True).returncode == 0
    return rev_list(lsha, "^" + rsha) if known else rev_list(lsha, "--not", "--remotes")


def main(argv):
    root = git("rev-parse", "--show-toplevel").decode().strip()
    os.chdir(root)
    if "--hook" in argv:
        jobs = []
        for line in sys.stdin:
            parts = line.split()
            if len(parts) == 4 and parts[1] != ZERO:
                jobs.append((parts[1], pushed(parts[1], parts[3])))
    else:
        rev = argv[argv.index("--rev") + 1] if "--rev" in argv else "HEAD"
        jobs = [(rev, rev_list(rev))]
    rules = load_rules(root)
    if not rules.rows:
        print(f"prepush: warning: no labelled rows at {rules.labels_path}: name and text checks skipped", file=sys.stderr)
    commits = list(dict.fromkeys(c for _, cs in jobs for c in cs))
    found, st = scan(commits, rules)
    findings, infos = split(found, allow_sources(argv))
    label = ",".join(r[:8] for r, _ in jobs) or "nothing"
    print(f"prepush: {label} ({st['commits']} commits, {rules.n_names} names, {rules.n_blind} blind domains) "
          f"checked against {rules.n_domains} event domains: {len(findings)} findings"
          + (f", {len(infos)} {SOURCE} allowed ({ALLOW_ENV})" if infos else ""), file=sys.stderr)
    for f in findings[:100]:
        print(f"prepush: FAIL {fmt(f, rules)}", file=sys.stderr)
    if len(findings) > 100:
        print(f"prepush: ... and {len(findings) - 100} more", file=sys.stderr)
    for f in infos[:50]:
        print(f"prepush: INFO {fmt(f, rules)}, allowed", file=sys.stderr)
    if findings:
        print("prepush: push blocked. Remove the data or the line (git rm --cached / amend), then push again. A generic "
              "word: tests/leak_allow.txt ('word # reason'); a public domain: tools/prepush_allow.txt."
              + (f" Labelled-set sources in the verdicts or traces: python3 tools/redact_labelled.py, then amend, or "
                 f"{ALLOW_ENV}=1 if that policy is decided." if any(f[3] == SOURCE for f in findings) else ""),
              file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
