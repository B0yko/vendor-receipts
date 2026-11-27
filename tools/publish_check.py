#!/usr/bin/env python3
"""Full-history leak check before the repo goes public, and the visibility flip behind it. Stdlib only.

  python3 tools/publish_check.py          every commit reachable from any ref (git rev-list --all), every blob of
                                          every tracked path, every path, every commit and annotated tag message
  python3 tools/publish_check.py --flip   git fetch origin (so every branch on GitHub is in --all), the same check,
                                          and only on PASS: gh repo edit REPO --visibility public, then prints
                                          gh repo view REPO --json visibility. Never flips on FAIL.

Rules: tools/prepush.py (labelled domains, names and row text everywhere; blind-product domains outside
stackgini-verdicts.csv and traces/; private paths; token shapes; attribution phrases in messages and files). Each blob
is checked once per path class. Output: one PASS or FAIL line per rule, then one FAIL line per finding as
commit:path:term (kind) with the middle of the term masked, then RESULT. Exit 1 on any FAIL. Fails closed: no
labelled rows under data/ is a FAIL, since there would be nothing to check against.
"""
import os, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import prepush as pp  # noqa: E402

REPO = "B0yko/vendor-receipts"
SHOW = 100


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def tags():
    """[(name, sha)] of the annotated tags (lightweight tags carry no message)."""
    out = pp.git("for-each-ref", "--format=%(objecttype) %(objectname) %(refname:short)", "refs/tags").decode()
    return [(p[2], p[1]) for p in (l.split(" ", 2) for l in out.splitlines()) if len(p) == 3 and p[0] == "tag"]


def check(root):
    rules = pp.load_rules(root)
    blind = sum(f.startswith("blind ") for f in rules.files)
    print(f"publish_check: rules: {rules.rows} labelled rows ({rules.labels_path}), {rules.n_labelled} labelled "
          f"domains, {rules.n_names} names, {rules.n_runs} text runs; {rules.n_blind} blind domains from {blind} files")
    commits = pp.rev_list("--all")
    findings, st = pp.scan(commits, rules, tags())
    print(f"publish_check: scanned {st['commits']} commits, {st['blobs']} blobs, {st['paths']} paths, "
          f"{st['tags']} tag messages")
    fails = 0
    if not rules.rows:
        print(f"FAIL setup: no labelled rows at {rules.labels_path} (nothing to check against)")
        fails += 1
    by = {k: [f for f in findings if f[2].removesuffix(" in path") == k] for k in pp.KINDS}
    for kind, fs in by.items():
        print(f"{'FAIL' if fs else 'PASS'} {kind}: {len(fs)} findings")
    shown = [f for k in pp.KINDS for f in by[k]]
    for f in shown[:SHOW]:
        print(f"FAIL {pp.fmt(f, rules)}")
    if len(shown) > SHOW:
        print(f"FAIL ... and {len(shown) - SHOW} more")
    fails += len(findings)
    print(f"RESULT {'FAIL' if fails else 'PASS'}: {len(findings)} findings in {sum(1 for v in by.values() if v)} "
          f"of {len(pp.KINDS)} rules")
    return fails == 0


def main(argv):
    flip = "--flip" in argv
    repo = argv[argv.index("--repo") + 1] if "--repo" in argv else REPO
    root = pp.git("rev-parse", "--show-toplevel").decode().strip()
    os.chdir(root)
    if flip:
        rc, out = run(["git", "fetch", "--quiet", "origin"])
        if rc:
            print(f"FAIL fetch: git fetch origin: {out[-300:]}\nnot flipped")
            return 1
    if not check(root):
        if flip:
            print(f"not flipped: {repo} stays private")
        return 1
    if flip:
        rc, out = run(["gh", "repo", "edit", repo, "--visibility", "public", "--accept-visibility-change-consequences"])
        if rc:
            print(f"FAIL flip: gh repo edit: {out[-300:]}")
            return 1
        rc, out = run(["gh", "repo", "view", repo, "--json", "visibility"])
        print(out)
        return rc
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
