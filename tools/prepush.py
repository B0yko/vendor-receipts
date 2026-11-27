#!/usr/bin/env python3
"""Pre-push guard: event data never leaves the event, and commits carry no AI attribution. Generic, stdlib only.

  prepush.py --hook            as .git hook (hooks/pre-push; reads git's "<lref> <lsha> <rref> <rsha>" lines on stdin)
  prepush.py [--rev REV]       check one commit (default HEAD) and its whole history; exit 1 on any finding

Fails when the pushed tree or commits contain:
- a tracked path under data/, runs/ or .cache/, or a tracked .env file;
- a forbidden domain: the key field (url, website, domain, ...) of every record in files under data/ (the labelled
  examples, blind cases, verdicts), every URL host in data/ files whose name says blind or case, and the lines of
  data/forbidden.txt; minus ALLOW below and the lines of tools/prepush_allow.txt;
- a commit message with "co-authored" or "generated with" (any coding agent).
Matching is case-insensitive on the domain as a token, so docs.vendor.io matches vendor.io and notvendor.io does not.
"""
import os, re, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
ALLOW = {"example.com", "example.org", "github.com", "web.archive.org", "archive.org", "rdap.org", "tavily.com",
         "api.tavily.com", "haveibeenpwned.com", "aiagentsberlin.xyz", "stackgini.com", "wikipedia.org", "google.com",
         "linkedin.com", "localhost", "127.0.0.1"}
KEYS = ("url", "website", "product_url", "domain", "homepage", "link", "product", "site")
URL = re.compile(r"https?://([a-z0-9.-]+\.[a-z]{2,}|localhost|127\.0\.0\.1)(?::\d+)?", re.I)
ATTRIB = re.compile(r"co-authored|generated with", re.I)
PRIVATE = ("data/", "runs/", ".cache/")
ZERO = "0" * 40


def git(*args, check=True):
    p = subprocess.run(["git", *args], capture_output=True, text=True)
    if check and p.returncode not in (0, 1):
        raise SystemExit(f"prepush: git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout


def host(s):
    m = URL.match(s.strip()) or re.match(r"^(?:www\.)?([a-z0-9-]+(?:\.[a-z0-9-]+)+)", s.strip(), re.I)
    return m.group(1).lower().removeprefix("www.") if m else ""


def forbidden(root):
    """Domains taken from the local, untracked data/ folder."""
    out, data = set(), os.path.join(root, "data")
    try:
        import eval as ev  # tools/eval.py, same folder
    except ImportError:
        ev = None
    for dp, _, files in os.walk(data):
        for f in files:
            p = os.path.join(dp, f)
            if os.path.getsize(p) > 20_000_000:
                continue
            try:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            if f == "forbidden.txt":
                out |= {host(l) or l.strip().lower() for l in text.splitlines() if l.strip() and not l.startswith("#")}
            if re.search(r"blind|case", f, re.I):
                out |= {m.lower().removeprefix("www.") for m in URL.findall(text)}
            if ev and f.lower().endswith((".csv", ".tsv", ".json", ".jsonl", ".ndjson")):
                try:
                    recs = ev.load_records(p)
                except Exception:
                    continue
                for r in recs:
                    for k, v in r.items():
                        if str(k).lower() in KEYS and isinstance(v, str) and host(v):
                            out.add(host(v))
    allow = set(ALLOW)
    af = os.path.join(root, "tools", "prepush_allow.txt")
    if os.path.exists(af):
        with open(af) as fh:
            allow |= {l.strip().lower() for l in fh if l.strip() and not l.startswith("#")}
    return sorted(d for d in out if d and "." in d and d not in allow)


def check(rev, msgs_range, root):
    findings = []
    paths = git("ls-tree", "-r", "--name-only", rev).splitlines()
    for p in paths:
        base = os.path.basename(p)
        if p.startswith(PRIVATE) or p + "/" in PRIVATE or base == ".env" or (base.endswith(".env") and not base.endswith(".env.example")):
            findings.append(f"tracked private path: {p}")
    doms = forbidden(root)
    if doms:
        pats = [rf"(^|[^a-z0-9-]){re.escape(d)}($|[^a-z0-9-])" for d in doms]
        hits = git("grep", "-n", "-I", "-i", "-E", *sum((["-e", p] for p in pats), []), rev, "--").splitlines()
        for h in hits[:50]:
            _, path, line, text = (h.split(":", 3) + ["", "", ""])[:4]
            which = [d for d in doms if re.search(pats[doms.index(d)], text, re.I)]
            findings.append(f"{path}:{line} names event domain {', '.join(which[:3])}")
    for block in git("log", "--format=%h%x00%B%x01", *msgs_range).split("\x01"):
        if "\x00" not in block:
            continue
        sha, msg = block.strip().split("\x00", 1)
        if ATTRIB.search(msg):
            findings.append(f"commit {sha}: attribution line ({ATTRIB.search(msg).group(0)})")
        named = [d for d in doms if re.search(rf"(^|[^a-z0-9-]){re.escape(d)}($|[^a-z0-9-])", msg, re.I)]
        if named:
            findings.append(f"commit {sha}: message names event domain {', '.join(named[:3])}")
    return findings, len(doms)


def main(argv):
    root = git("rev-parse", "--show-toplevel").strip()
    os.chdir(root)
    jobs = []
    if "--hook" in argv:
        for line in sys.stdin:
            parts = line.split()
            if len(parts) == 4 and parts[1] != ZERO:
                lsha, rsha = parts[1], parts[3]
                jobs.append((lsha, [f"{rsha}..{lsha}"] if rsha != ZERO else [lsha, "--not", "--remotes"]))
    else:
        rev = argv[argv.index("--rev") + 1] if "--rev" in argv else "HEAD"
        jobs.append((rev, [rev]))
    total = []
    for rev, rng in jobs:
        f, n = check(rev, rng, root)
        total += f
        print(f"prepush: {rev[:8]} checked against {n} event domains: {len(f)} findings", file=sys.stderr)
    for f in total:
        print(f"prepush: FAIL {f}", file=sys.stderr)
    if total:
        print("prepush: push blocked. Remove the data or the line (git rm --cached / amend), then push again.", file=sys.stderr)
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
