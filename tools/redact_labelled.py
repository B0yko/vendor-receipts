#!/usr/bin/env python3
"""Withhold labelled-set sources from the public verdicts and traces, before they are committed. Stdlib only.

  python3 tools/redact_labelled.py [--dry-run]

Files: stackgini-verdicts.csv and every text file under traces/ (working tree, repo root). Terms: the labelled-only
domains and names of tools/prepush.py (labelled-set domains and product or vendor names that no blind product shares;
the "labelled-source" kind of the guard). In each file:
- a URL that names a labelled-only domain anywhere in it (host or an archived URL inside it) becomes DOMAIN_MARK;
- a URL whose text names a labelled-only product or vendor (a review page about it) becomes NAME_URL_MARK, so no
  link is left half rewritten;
- a bare mention of a labelled-only domain (docs.vendor.io/path, an e-mail domain) becomes DOMAIN_MARK;
- a labelled-only name (whole word, case-insensitive, any separator between its words) becomes NAME_MARK.
Everything else stays byte for byte, quotes included; the markers hold no comma or quote, so the CSV keeps its
columns. Idempotent: the markers themselves are never rewritten, and a second run changes nothing.
Prints counts only: per file, per masked term, then what the guard still finds in those files (labelled row text is
never rewritten: edit it by hand). Exit 0 when nothing labelled is left (dry run: as if written), 1 otherwise.
Run it before the commit that adds the traces: publish_check reads every commit, so a redaction made after that
commit needs the commit amended (before any push).
"""
import collections, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import prepush as pp  # noqa: E402

DOMAIN_MARK = "[source withheld: labelled-set domain]"
NAME_URL_MARK = "[source withheld: labelled-set name]"
NAME_MARK = "[name withheld]"
MARKS = re.compile("|".join(re.escape(m) for m in (DOMAIN_MARK, NAME_URL_MARK, NAME_MARK)))
STOP = r"\s<>\"'`()\[\]{}|\\"
URL_SPAN = re.compile(rf"https?://[^{STOP}]+", re.I)
BARE_SPAN = re.compile(rf"(?<![a-z0-9.-])[a-z0-9-]+(?:\.[a-z0-9-]+)+(?::\d+)?(?:[/?#][^{STOP}]*)?", re.I)
TRAIL = ".,;:!?*_~"


def files(root):
    out = [pp.ZONE_FILE] if os.path.isfile(os.path.join(root, pp.ZONE_FILE)) else []
    for d, dirs, fs in os.walk(os.path.join(root, pp.ZONE_DIR.rstrip("/"))):
        dirs[:] = sorted(x for x in dirs if not x.startswith("."))
        out += [os.path.relpath(os.path.join(d, f), root) for f in sorted(fs) if not f.startswith(".")]
    return out


class Redactor:
    def __init__(self, rules):
        z = rules.zone
        self.domains = {d for d, k in z.domains.items() if k == pp.SOURCE}
        self.names = pp.Matcher({}, z.names, {}, name_kind=pp.SOURCE)
        self.dom = pp.Matcher({d: pp.SOURCE for d in self.domains}, (), {})
        alts = [r"[\W_]+".join(map(re.escape, n.split())) for n in sorted(z.names, key=len, reverse=True)]
        self.name_rx = re.compile(r"(?<![^\W_])(?:" + "|".join(alts) + r")(?![^\W_])", re.I) if alts else None
        self.terms, self.counts = collections.Counter(), collections.Counter()

    def _span(self, rx, text, scheme):
        def sub(m):
            s = m.group(0)
            core = s.rstrip(TRAIL)
            tail = s[len(core):]
            doms = self.dom.find(core)
            if doms:
                self.terms.update(t for _, t in doms)
                self.counts["urls" if scheme else "domains"] += 1
                return DOMAIN_MARK + tail
            hits = self.names.find(core) if scheme or "/" in core else ()  # a bare span with a path is a URL too
            if hits:
                self.terms.update(t for _, t in hits)
                self.counts["urls"] += 1
                return NAME_URL_MARK + tail
            return s
        return rx.sub(sub, text)

    def _names(self, text):
        if not self.name_rx:
            return text

        def sub(m):
            self.terms[pp.phrase(m.group(0))] += 1
            self.counts["names"] += 1
            return NAME_MARK
        return self.name_rx.sub(sub, text)

    @staticmethod
    def outside(text, fn):
        """fn applied to the text between markers only: a marker is never rewritten (idempotence)."""
        parts, marks = MARKS.split(text), MARKS.findall(text)
        return "".join(fn(p) + (marks[i] if i < len(marks) else "") for i, p in enumerate(parts))

    def redact(self, text):
        text = self.outside(text, lambda s: self._span(URL_SPAN, s, scheme=True))
        text = self.outside(text, lambda s: self._span(BARE_SPAN, s, scheme=False))
        return self.outside(text, self._names)


def main(argv):
    dry = "--dry-run" in argv
    root = pp.git("rev-parse", "--show-toplevel").decode().strip()
    os.chdir(root)
    rules = pp.load_rules(root)
    if not rules.rows:
        print(f"redact: no labelled rows at {rules.labels_path}: nothing to redact against")
        return 1
    red = Redactor(rules)
    changed, left = 0, collections.Counter()
    for rel in files(root):
        with open(rel, "rb") as f:
            raw = f.read()
        if b"\0" in raw[:4096]:
            continue
        text = raw.decode("utf-8", "surrogateescape")
        before = sum(red.counts.values())
        new = red.redact(text)
        n = sum(red.counts.values()) - before
        if new != text:
            changed += 1
            print(f"redact: {rules.safe(rel)}: {n} withheld")
            if not dry:
                with open(rel, "wb") as f:
                    f.write(new.encode("utf-8", "surrogateescape"))
        for kind, _, line in pp.check_text(new, rules.zone, attribution=False):
            if kind in (pp.SOURCE, "labelled text"):
                left[kind] += 1
                print(f"redact: left: {rules.safe(rel)}:{line} ({kind}{', edit by hand' if kind != pp.SOURCE else ''})")
    c = red.counts
    print(f"redact: {c['urls']} URLs, {c['domains']} bare domains, {c['names']} names withheld in {changed} files"
          + (" (dry run: nothing written)" if dry else ""))
    if red.terms:
        print("redact: terms: " + ", ".join(f"{pp.mask(t)} x{k}" for t, k in red.terms.most_common()))
    print(f"redact: left in the verdicts and traces: {left[pp.SOURCE]} {pp.SOURCE}, {left['labelled text']} labelled "
          f"text")
    return 1 if left else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
