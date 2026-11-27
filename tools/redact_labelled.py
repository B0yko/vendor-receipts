#!/usr/bin/env python3
"""Withhold labelled-set sources from the public verdicts and traces, before they are committed. Stdlib only.

  python3 tools/redact_labelled.py [--dry-run] [--row-text] [--keep-sources]

  (default)        withhold labelled-set sources (domains, names: below)
  --row-text       also withhold each passage with labelled row text (a RUN-word run that only one labelled row has,
                   the guard's "labelled text" kind, FAIL everywhere): in stackgini-verdicts.csv and traces/*.md
                   the containing sentence or table cell becomes PASSAGE_MARK. Done first,
                   on the original text, so a name withheld later cannot hide a run from it.
  --keep-sources   leave domains and names as they are (the allow policy: PREPUSH_ALLOW_LABELLED_SOURCES=1 and
                   publish_check --allow-labelled-sources); with it, labelled sources left do not make exit 1.
  tools/blind.sh ship runs `--row-text --keep-sources` before its commit.

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
Passages: Markdown list, heading and quote markers stay; a run across sentences or cells withholds each
intersecting passage. CSV quoting, field delimiters and record endings stay intact.
Prints counts and masked samples only: per file, per masked term or run, then what the guard still finds in those
files. Exit 0 when nothing blocking is left (dry run: as if written), 1 otherwise.
Run it before the commit that adds the traces: publish_check reads every commit, so a redaction made after that
commit needs the commit amended (before any push).
"""
import bisect, collections, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import prepush as pp  # noqa: E402

DOMAIN_MARK = "[source withheld: labelled-set domain]"
NAME_URL_MARK = "[source withheld: labelled-set name]"
NAME_MARK = "[name withheld]"
PASSAGE_MARK = "[passage withheld: matches labelled-set text]"
MARKS = re.compile("|".join(re.escape(m) for m in (DOMAIN_MARK, NAME_URL_MARK, NAME_MARK, PASSAGE_MARK)))
SENT_END = re.compile(r"[.!?]+[\"'\u201d\u2019*_]*(?=\s|$)")
LEAD = re.compile(r"[ \t]*(?:(?:[-*+>]|#{1,6}|\d+[.)])[ \t]+)*")  # list, quote and heading markers stay
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


def csv_fields(text, delim):
    """[(start, end)] of every field's content (inside its quotes), RFC 4180 style, multi-line fields included."""
    out, i, n = [], 0, len(text)
    while i < n:
        if text[i] == '"':
            k = i + 1
            while True:
                k = text.find('"', k)
                if k < 0 or text[k + 1:k + 2] != '"':
                    break
                k += 2
            k = n if k < 0 else k
            out.append((i + 1, k))
            i = k + 1
            while i < n and text[i] not in delim + "\r\n":
                i += 1
        else:
            j = i
            while j < n and text[j] not in delim + "\r\n":
                j += 1
            out.append((i, j))
            i = j
        i += 2 if text[i:i + 2] == "\r\n" else 1
    return out


def passages(text, csv_file):
    """Original offsets of CSV/Markdown cells or Markdown sentences, excluding structural markup."""
    if csv_file:
        yield from csv_fields(text, ',')
        return

    def sentences(start, end):
        for m in SENT_END.finditer(text, start, end):
            yield start, m.end()
            start = m.end()
        yield start, end

    start = pos = 0
    for line in text.splitlines(keepends=True):
        end = pos + len(line)
        lead = LEAD.match(line).end()
        table = line.lstrip().startswith('|')
        if table or not line.strip() or line[:lead].strip():
            yield from sentences(start, pos)
            if table:
                cell = pos
                for m in re.finditer(r'(?<!\\)\|', line):
                    yield cell, pos + m.start()
                    cell = pos + m.end()
                yield cell, end
                start = end
            else:
                start = pos + lead if line.strip() else end
        pos = end
    yield from sentences(start, pos)


def hit_spans(text, runs):
    """Match the guard's raw casefolded word stream, even across punctuation, lines and cells."""
    low = text.casefold()
    offsets = None
    if len(low) != len(text):
        offsets = [0]
        for ch in text:
            offsets.append(offsets[-1] + len(ch.casefold()))
    window = collections.deque(maxlen=pp.RUN)
    for word in pp.WORD.finditer(low):
        window.append(word)
        if len(window) == pp.RUN:
            gram = ' '.join(m[0] for m in window)
            if gram in runs:
                a, b = window[0].start(), word.end()
                if offsets is not None:
                    a, b = bisect.bisect_right(offsets, a) - 1, bisect.bisect_left(offsets, b)
                yield a, b, gram


class Redactor:
    def __init__(self, rules):
        z = rules.zone
        self.runs = dict(z.runs)
        self.rows, self.samples = set(), collections.Counter()
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

    def row_text(self, text, csv_file=False):
        """Withhold each passage once, even when many overlapping windows hit the same row."""
        hits = []
        for a, b, gram in hit_spans(text, self.runs) if self.runs else ():
            self.rows.add(self.runs[gram])
            self.samples[pp.mask(gram)] += 1
            if hits and a <= hits[-1][1]:
                hits[-1] = (hits[-1][0], b)
            else:
                hits.append((a, b))
        if not hits:
            return text
        parts, last, i = [], 0, 0
        for s, e in passages(text, csv_file):
            while i < len(hits) and hits[i][1] <= s:
                i += 1
            if i == len(hits):
                break
            if e <= hits[i][0]:
                continue
            while s < e and text[s].isspace():
                s += 1
            while s < e and text[e-1].isspace():
                e -= 1
            if s < e:
                parts.extend((text[last:s], PASSAGE_MARK))
                self.counts['passages'] += 1
                last = e
        return ''.join(parts) + text[last:]

    def redact(self, text, row_text=False, sources=True, csv_file=False):
        if row_text:
            text = self.row_text(text, csv_file)
        if not sources:
            return text
        text = self.outside(text, lambda s: self._span(URL_SPAN, s, scheme=True))
        text = self.outside(text, lambda s: self._span(BARE_SPAN, s, scheme=False))
        return self.outside(text, self._names)


def main(argv):
    dry, row_text, keep = "--dry-run" in argv, "--row-text" in argv, "--keep-sources" in argv
    root = pp.git("rev-parse", "--show-toplevel").decode().strip()
    os.chdir(root)
    rules = pp.load_rules(root)
    if not rules.rows:
        print(f"redact: no labelled rows at {rules.labels_path}: nothing to redact against")
        return 1
    red = Redactor(rules)
    changed, left = 0, collections.Counter()
    if os.path.islink(pp.ZONE_DIR.rstrip('/')):
        print('redact: refusing symlinked trace directory')
        return 1
    for rel in files(root):
        if os.path.islink(rel):
            print('redact: refusing symlink in publication files')
            return 1
        with open(rel, "rb") as f:
            raw = f.read()
        if b"\0" in raw[:4096]:
            continue
        text = raw.decode("utf-8", "surrogateescape")
        before = sum(red.counts.values())
        csv_file = rel == pp.ZONE_FILE
        eligible = csv_file or (os.path.dirname(rel) + '/' == pp.ZONE_DIR and rel.endswith('.md'))
        new = red.redact(text, row_text=row_text and eligible, sources=not keep, csv_file=csv_file)
        n = sum(red.counts.values()) - before
        if new != text:
            changed += 1
            print(f"redact: {rules.safe(rel)}: {n} withheld")
            if not dry:
                with open(rel, "wb") as f:
                    f.write(new.encode("utf-8", "surrogateescape"))
        for kind, _, line in pp.check_text(new, rules.zone, attribution=False):
            if kind == pp.SOURCE and keep:
                left["kept"] += 1
            elif kind in (pp.SOURCE, "labelled text"):
                left[kind] += 1
                how = "" if kind == pp.SOURCE else ", edit by hand" if row_text or not eligible else ", run with --row-text"
                print(f"redact: left: {rules.safe(rel)}:{line} ({kind}{how})")
    c = red.counts
    print(f"redact: {c['passages']} passages ({len(red.rows)} labelled rows), {c['urls']} URLs, {c['domains']} bare "
          f"domains, {c['names']} names withheld in {changed} files"
          + (" (dry run: nothing written)" if dry else "") + ("; sources kept (--keep-sources)" if keep else ""))
    if red.samples:
        print("redact: runs: " + ", ".join(f"{t} x{k}" for t, k in red.samples.most_common(10)))
    if red.terms:
        print("redact: terms: " + ", ".join(f"{pp.mask(t)} x{k}" for t, k in red.terms.most_common()))
    print(f"redact: left in the verdicts and traces: {left[pp.SOURCE]} {pp.SOURCE}, {left['labelled text']} labelled "
          f"text" + (f"; {left['kept']} {pp.SOURCE} kept" if keep else ""))
    return 1 if left[pp.SOURCE] or left["labelled text"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
