"""Receipt checks (lane L6): check(research) -> [Receipt], one Receipt per Evidence, in evidence order.

    from app.check.receipts import check
    receipts = check(research)          # the app/ledger.py Receipt shape; never raises on bad data

Checks per Evidence (a copy; the research dict is never mutated):
  resolves      the quote is on research["sources"][url]["text"] after normalise() (casefold, NFKC, one kind of
                quote, apostrophe and dash, markdown emphasis and link syntax stripped, whitespace collapsed); the url
                matches modulo scheme, www., trailing slash and fragment. An inner ellipsis (..., the ellipsis char,
                [...]) splits the quote into parts that must appear in order, each >= 15 chars. A quote under 12
                chars fails ("too short to verify"); no text for the url fails ("source text not retrieved").
  same_company  the url's registrable domain is in identity.domains or is the product URL's; else the page text,
                title or url names an anchor (product, vendor, legal_entity with and without its legal suffix,
                parent, former_names, domain stems) as a whole word, case-insensitive, anchors >= 3 chars. An anchor
                made only of common English words counts only with a second anchor or a vendor domain on the page.
                Wayback URLs are judged by the archived URL.
  says_it       one model question per chunk of 50 receipts that passed both checks above (purpose "says_it",
                ThreadPoolExecutor(4)): does the quote state the finding's central assertion? The model sees claim,
                quote, source title, the source's published_date and the receipt's date (dates are the dated
                check's job). Answer supports true | "partial" | false; "partial" (central assertion yes, a
                secondary detail no) passes and records says_it_note = "partial: <missing detail>" ('' otherwise).
                A missing id is False ("no verdict"); a failed call is retried once, then its chunk is False
                ("no verdict: model error").
  dated         e.date (YYYY, YYYY-MM, YYYY-MM-DD) or the source's published_date; a date equal to the source's
                fetch day does not count. An empty e.date is filled from published_date. Never rejects alone.
Plus domain (registrable domain of url), independent (content domain not the vendor's and author_type is not
vendor_self), dup_of (the id of the first kept receipt with the same normalised quote, or the same first 120
normalised chars, when it is on another url; None on its own url). kept = resolves and same_company and says_it; why = the first failed check's reason.
"""
import concurrent.futures
import datetime
import email.utils
import json
import re
import unicodedata
import urllib.parse

from app import ledger, model

MIN_QUOTE = 12
MIN_PART = 15
MIN_ANCHOR = 3
CHUNK = 50
WORKERS = 4
DUP_PREFIX = 120

_QUOTES = dict.fromkeys(map(ord, "\u2018\u2019\u201a\u201b\u2032\u02bc\u0060\u00b4"), "'")
_QUOTES.update(dict.fromkeys(map(ord, "\u201c\u201d\u201e\u201f\u2033\u00ab\u00bb"), '"'))
_QUOTES.update(dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d"), "-"))
_QUOTES.update(dict.fromkeys(map(ord, "*_~\u00ad\u200b\u200c\u200d\ufeff"), None))
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)\s]*(?:\s+\"[^\"]*\")?\)")
_MD_HEAD = re.compile(r"(?m)^\s{0,3}#{1,6}\s+")
_WS = re.compile(r"\s+")
_ELLIPSIS = re.compile(r"\s*[\[(]?\s*(?:\.\s*){3,}[\])]?\s*")
_EDGE = " \"'.,;:!?-()[]"
_DATE = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?")
_WAYBACK = re.compile(r"^/web/\d+[a-z_]*/(.+)$")

LEGAL_SUFFIXES = {"inc", "llc", "ltd", "limited", "gmbh", "ag", "se", "sa", "sas", "sarl", "srl", "spa", "bv", "nv",
                  "oy", "ab", "as", "aps", "plc", "corp", "corporation", "co", "company", "kg", "ug", "pty", "pte",
                  "lp", "llp", "sp", "zoo", "kft", "sro", "doo", "holding", "holdings", "group", "b.v", "n.v", "s.a",
                  "s.a.s", "s.r.l", "s.p.a", "s.r.o", "e.k", "k.k"}
COMMON_WORDS = set("""
a about above access account act action active add address admin advance after again age agent air alert all alpha
amber amp amplitude anchor and angle any app apple apply arc area arena arrow art ask asset atlas atom auto avenue away
axis back balance band bank base basic basecamp beacon beam bear bee bell best beta better big bird black blade blank
block bloom blue board boat body bold bolt bond book boost border boss bot box brain branch brand brave bread break
bridge bright bring broad brook build bulb burst bus business button buy cabin cake call calm camp can canvas cap
capital car card care cargo case cash cast castle cat cell center central chain chair chalk change channel chart chat
check chief choice circle city class clay clean clear clever click client climb clip clock close cloud club coach
coast code coin cold collect color comet common compass complete connect contact content control cook cool copper
copy core corner count cove craft crane create credit crew cross crowd crown cube culture cup current curve custom
cycle daily dash data date dawn day deal deep delta desk detail dial direct discord dock dot dove draft dragon draw
dream drift drive drop dune dust eagle early earth east easy echo edge element elevate ember empire end engine enter
envoy equal escape even event every evolve exact expert fabric face fact fair faith falcon fame farm fast fathom
feather feed field figure file fill film find fine fire first fish fit five flag flash fleet flex flight float flock
flow fly focus fold folk force forest forge form fort forward found fox frame free fresh friend front frost fruit
fuel full fun fund gain game garden gate gather gauge gear gem general giant gift glass glide globe glow goal gold
golf gong good grace grain grand graph grass gravity great green grid ground group grove grow guard guide gust habit
half hall hand happy harbor hard harvest hawk head heap heart help hero high hill hive hold home honey hook hope
horizon host hot house hub human hunt idea image impact index info inside intercom iron island item ivy jade jet join
journey joy judge jump just keen key keystone kind king kit kite lab labs lake land lane large laser last launch
lattice layer lead leaf lean learn ledger legacy lens level lever lift light lime line linear link lion list little
live local lock lodge logic long loom loop lotus love loyal luck lumen lunar machine magic mail main major make map
maple marble mark market mars mason master match matrix maze meadow measure medal media meet mercury merge mesh metal
method metric mile mind mine mint mirror mission mix mode model modern moment monday money monitor moon mosaic moss
motion mount move muse music name native nature navigator near neat nest net network new news next night noble node
north note notion nova novel number oak object ocean office one open optic option orange orbit order origin outreach
owl own pace pack page paint pair palm panel paper park part pass path patch peak pearl pen people pepper perfect
pilot pine pink pioneer pitch pivot pixel place plain plan planet plant play plaza plot plus pocket point polar pole
pond pool port post power press prime prism pro profile project prompt proof pulse pure purple quest quick quiet
radar radius rain range ramp rapid raven ray reach read ready real record red reef relay remote render report rest
retail rich ride ridge right ring rise river road robin rock rocket root rose round route royal ruby rule run rush
safe sage sail salt sand save scale scene school scope scout sea seal search season seed segment select sense sentry
series set shape share sharp shell shield shift shine ship shop short side sight sign signal silver simple single
site sky slack slate smart smile snap snow social solid solo sonic sound source south space span spark speak spear
speed sphere spice spin spirit split spot spring square stack stage star start state station steady steel step
sterling still stone store storm story stream street strike stripe strong studio style summit sun super supply
sure surf swan sweet swift switch system table tag tail talent talk tank target task taste team tech tempo ten tent
test text the theory thing things think thread thrive tide tiger time tiny title today token tone tool top torch
total touch tower town trace track trade trail train trek trend trial tribe true trust truth tune turn twin two type
union unit unity up urban use valley value vault vector velocity venture verse view village vine vision visit vista
vital voice volt vote voyage walk wall watch water wave way wealth weather web well west whale wheel white wide wild
wind window wing wire wise wolf wonder wood word work world yard year yellow yes zen zero zone zoom
""".split())

SYSTEM = """You verify receipts for a vendor due-diligence report. Each receipt is a claim (the finding, one plain
sentence about the product or its vendor) and a verbatim quote from the page at url, with the page's source_title, its
published_date and the receipt's date field.
Other checks own whether the quote is on the page, whether the page is about this company, and the date. Judge only
whether the quote carries the finding:
- "supports": true when the quote states the finding's central assertion: who or what, and the key fact. Dates may come
  from the date field, the source_title or the published_date (the dated check owns them), so a date that is not in
  the quote is no reason to reject. Framing words in the finding ("widely used", "notable", "significant") need no
  literal match. A plain paraphrase, or a fact the quote plainly entails, counts.
- "supports": "partial" when the central assertion is supported but a secondary detail is not (an extra number, a
  cause, a second fact); name that detail in "missing".
- "supports": false when the quote is unrelated, states the opposite, is about another product or company, or the
  central fact is missing.
Answer with one JSON object and nothing else:
{"results": [{"id": "<receipt id>", "supports": true, "why": "<at most 20 words>", "missing": "<partial only: the
detail the quote does not carry>"}]}
where supports is true, "partial" or false, with exactly one entry per receipt id you were given."""


def normalise(s):
    """Text as compared: NFKC, casefold, one kind of quote/apostrophe/dash, markdown emphasis and links stripped,
    whitespace collapsed."""
    if not isinstance(s, str):
        s = "" if s is None else str(s)
    s = unicodedata.normalize("NFKC", s)
    s = _MD_HEAD.sub("", _MD_LINK.sub(r"\1", s))
    s = s.translate(_QUOTES).casefold()
    return _WS.sub(" ", s).strip()


def url_key(url):
    """The url modulo scheme, www., trailing slash and fragment (host lowercased, query kept)."""
    if not isinstance(url, str) or not url.strip():
        return ""
    u = url.strip()
    p = urllib.parse.urlsplit(u if "://" in u else "https://" + u)
    host = (p.hostname or "").lower().removeprefix("www.")
    if p.port:
        host += f":{p.port}"
    return host + p.path.rstrip("/") + ("?" + p.query if p.query else "")


def host_of(url):
    if not isinstance(url, str) or not url.strip():
        return ""
    u = url.strip()
    return (urllib.parse.urlsplit(u if "://" in u else "https://" + u).hostname or "").lower()


def content_url(url):
    """The archived URL for a Wayback snapshot (web.archive.org/web/<ts>/<url>), else url itself."""
    if not isinstance(url, str):
        return ""
    if host_of(url) in ("web.archive.org", "archive.org", "wayback.archive.org"):
        m = _WAYBACK.match(urllib.parse.urlsplit(url.strip()).path)
        if m:
            return m.group(1)
    return url


def domain_of(url):
    h = host_of(url)
    return ledger.registrable(h) if h else ""


def _parts(quote):
    """-> (parts, problem): the normalised quote split at inner ellipses, edges trimmed."""
    q = normalise(quote)
    pieces = [p.strip(_EDGE) for p in _ELLIPSIS.split(q)]
    pieces = [p for p in pieces if p]
    if sum(len(p) for p in pieces) < MIN_QUOTE:
        return [], "too short to verify"
    if len(pieces) > 1 and any(len(p) < MIN_PART for p in pieces):
        return [], f"too short to verify: a part between ellipses is under {MIN_PART} chars"
    return pieces, ""


def resolves(quote, text):
    """-> (ok, why) for a quote against an already normalised page text (None or '' = not retrieved)."""
    if not text:
        return False, "source text not retrieved"
    parts, why = _parts(quote)
    if why:
        return False, why
    pos = 0
    for i, part in enumerate(parts):
        j = text.find(part, pos)
        if j < 0:
            if i == 0 or text.find(part) < 0:
                return False, "quote not found on the page"
            return False, "quote parts not found in order on the page"
        pos = j + len(part)
    return True, ""


def dup_key(quote):
    """The text two receipts share when one reposts the other: the first DUP_PREFIX normalised chars."""
    q = " ".join(_parts(quote)[0]) or normalise(quote)
    return q[:DUP_PREFIX]


def _strip_legal(name):
    words = name.split()
    while len(words) > 1 and words[-1].strip(".,&") in LEGAL_SUFFIXES | {""}:
        words.pop()
    return " ".join(words)


def _is_common(anchor):
    words = [w for w in re.split(r"\W+", anchor) if w]
    return bool(words) and all(w in COMMON_WORDS or w in LEGAL_SUFFIXES for w in words)


def anchors_of(research):
    """-> (anchors (normalised, longest first), vendor registrable domains)."""
    ident = research.get("identity") if isinstance(research.get("identity"), dict) else {}
    domains = []
    for d in list(ident.get("domains") or []) + [research.get("url")]:
        r = domain_of(d) if isinstance(d, str) else ""
        if r and r not in domains:
            domains.append(r)
    names = []
    for k in ("product", "vendor", "legal_entity", "parent"):
        v = ident.get(k)
        if isinstance(v, str):
            names.append(v)
    fn = ident.get("former_names")
    names += [x for x in (fn if isinstance(fn, list) else [fn]) if isinstance(x, str)]
    names += [d.split(".")[0] for d in domains]
    out = []
    for n in names:
        for a in (normalise(n), _strip_legal(normalise(n))):
            a = a.strip(_EDGE)
            if len(a) >= MIN_ANCHOR and a not in out:
                out.append(a)
    return sorted(out, key=len, reverse=True), domains


def _names(anchor, hay):
    return re.search(r"(?<!\w)" + re.escape(anchor) + r"(?!\w)", hay) is not None


def same_company(url, page_text, title, anchors, domains):
    """-> (ok, why). page_text is already normalised."""
    if domain_of(content_url(url)) in domains or domain_of(url) in domains:
        return True, ""
    hay = " ".join((page_text or "", normalise(title), (url or "").casefold()))
    found = [a for a in anchors if _names(a, hay)]
    found = [a for a in found if not any(a != b and _names(a, b) for b in found)]  # 'acme' inside 'acme cloud'
    if any(not _is_common(a) for a in found) or len(found) >= 2:
        return True, ""
    if found and any(_names(d, hay) for d in domains):
        return True, ""
    shown = ", ".join(f"'{a}'" for a in anchors) or "(no identity anchors)"
    if found:
        return False, (f"same company failed: the page names only the common word '{found[0]}', with no second "
                       f"anchor or vendor domain (anchors: {shown})")
    return False, f"same company failed: the page names none of {shown}"


def _iso(s):
    """-> 'YYYY' | 'YYYY-MM' | 'YYYY-MM-DD' | '' from an ISO-ish or RFC 2822 date string."""
    if not isinstance(s, str) or not s.strip():
        return ""
    s = s.strip()
    m = _DATE.match(s)
    if m:
        y, mo, d = m.groups()
        if not 1980 <= int(y) <= datetime.date.today().year + 1:
            return ""
        if mo and not 1 <= int(mo) <= 12:
            return y
        if d and not 1 <= int(d) <= 31:
            return f"{y}-{mo}"
        return "-".join(x for x in (y, mo, d) if x)
    try:
        return email.utils.parsedate_to_datetime(s).date().isoformat()
    except (TypeError, ValueError, IndexError, OverflowError):
        return ""


def dated(date, src):
    """-> (ok, date to record). The fetch day of the source never counts as the event date; published_date fills
    an empty, unparseable or fetch-day e.date."""
    src = src if isinstance(src, dict) else {}
    fetched = _iso(src.get("fetched_at"))
    fetched = fetched if len(fetched) == 10 else ""
    own = _iso(date)
    if own and own != fetched:
        return True, own
    pub = _iso(src.get("published_date"))
    if pub and pub != fetched:
        return True, pub
    return False, date if isinstance(date, str) else ""


def _ask_chunk(chunk, header):
    items = [{"id": r["id"], "claim": str(r.get("text") or "")[:600], "quote": str(r.get("quote") or "")[:1500],
              "source_title": str(r.get("_src_title") or r.get("source_title") or "")[:200],
              "published_date": str(r.get("_src_pub") or "")[:40], "date": str(r.get("date") or "")[:40],
              "url": str(r.get("url") or "")}
             for r in chunk]
    user = header + "Receipts:\n" + "\n".join(json.dumps(i, ensure_ascii=False) for i in items)
    last = None
    for _ in range(2):
        try:
            ans = model.ask_json(SYSTEM, user, purpose="says_it")
            res = ans.get("results") if isinstance(ans, dict) else None
            out = {}
            for x in res if isinstance(res, list) else []:
                if isinstance(x, dict) and x.get("id") is not None:
                    out[str(x["id"])] = x
            return out, None
        except Exception as e:  # noqa: BLE001 - a model failure must never break the pipeline
            last = e
    return {}, f"{type(last).__name__}: {str(last)[:160]}"


def says_it(receipts, research):
    """Fills checks.says_it and a _says_why on each receipt passed in (all are asked)."""
    if not receipts:
        return
    ident = research.get("identity") if isinstance(research.get("identity"), dict) else {}
    who = " by ".join(x for x in (ident.get("product"), ident.get("vendor")) if isinstance(x, str) and x)
    header = (f"Product under review: {who} ({research.get('url', '')})\n" if who else
              f"Product under review: {research.get('url', '')}\n")
    chunks = [receipts[i:i + CHUNK] for i in range(0, len(receipts), CHUNK)]
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
        answers = list(pool.map(lambda c: _ask_chunk(c, header), chunks))
    for chunk, (verdicts, err) in zip(chunks, answers):
        for r in chunk:
            v = verdicts.get(str(r["id"]))
            if v is None:
                r["checks"]["says_it"] = False
                r["_says_why"] = f"no verdict: model error ({err})" if err else "no verdict"
                continue
            sup = v.get("supports")
            word = sup.strip().lower() if isinstance(sup, str) else ""
            partial = word in ("partial", "partly", "partially")
            ok = sup is True or word in ("true", "yes") or partial
            r["checks"]["says_it"] = ok
            why = v.get("why").strip() if isinstance(v.get("why"), str) else ""
            r["_says_why"] = "" if ok else ("says it failed: " + (why or "the quote does not support the claim"))
            if partial:
                miss = v.get("missing").strip() if isinstance(v.get("missing"), str) else ""
                r["says_it_note"] = ("partial: " + (miss or why or "a secondary detail is not in the quote"))[:300]


def _receipt(e, i):
    r = dict(e)
    if not isinstance(r.get("id"), str) or not r["id"]:
        r["id"] = str(r["id"]) if r.get("id") not in (None, "") else f"x{i + 1}"
    r["checks"] = {"resolves": False, "same_company": False, "says_it": False, "dated": False}
    r.update(kept=False, why="", domain="", independent=False, dup_of=None, says_it_note="")
    return r


def check(research):
    """research (app/ledger.py Research) -> [Receipt], one per Evidence dict, in order. Never raises on bad data."""
    if not isinstance(research, dict):
        return []
    evidence = research.get("evidence")
    evidence = [e for e in evidence if isinstance(e, dict)] if isinstance(evidence, list) else []
    raw = research.get("sources") if isinstance(research.get("sources"), dict) else {}
    sources, texts, companies = {}, {}, {}
    for u, s in raw.items():
        k = url_key(u)
        if k and isinstance(s, dict) and (k not in sources or not sources[k].get("text")):
            sources[k] = s
    try:
        anchors, domains = anchors_of(research)
    except Exception:  # noqa: BLE001
        anchors, domains = [], []
    out, why1, why2 = [], {}, {}
    for i, e in enumerate(evidence):
        r = _receipt(e, i)
        try:
            url = r.get("url") if isinstance(r.get("url"), str) else ""
            k = url_key(url)
            src = sources.get(k, {})
            if k not in texts:
                texts[k] = normalise(src.get("text")) if src.get("text") else ""
            ok, w = resolves(r.get("quote"), texts[k])
            r["checks"]["resolves"], why1[id(r)] = ok, w
            title = src.get("title") or r.get("source_title")
            sk = (url, str(title))
            if sk not in companies:
                companies[sk] = same_company(url, texts[k], title, anchors, domains)
            ok, w = companies[sk]
            r["checks"]["same_company"], why2[id(r)] = ok, w
            r["checks"]["dated"], r["date"] = dated(r.get("date"), src)
            r["_src_title"], r["_src_pub"] = src.get("title"), src.get("published_date")
            r["domain"] = domain_of(url)
            own = domain_of(content_url(url))
            r["independent"] = bool(own) and own not in domains and r.get("author_type") != "vendor_self"
        except Exception as ex:  # noqa: BLE001 - bad data never breaks the pipeline
            r["checks"].update(resolves=False, same_company=False)
            why1[id(r)] = f"check error: {type(ex).__name__}: {str(ex)[:120]}"
        out.append(r)
    asked = [r for r in out if r["checks"]["resolves"] and r["checks"]["same_company"]]
    try:
        says_it(asked, research)
    except Exception as ex:  # noqa: BLE001
        for r in asked:
            r["checks"]["says_it"] = False
            r["_says_why"] = f"no verdict: {type(ex).__name__}"
    seen = {}
    for r in out:
        c = r["checks"]
        r["kept"] = bool(c["resolves"] and c["same_company"] and c["says_it"])
        r["why"] = ("" if r["kept"] else why1.get(id(r)) or why2.get(id(r)) or r.get("_says_why")
                    or "says it: not asked")
        for tmp in ("_says_why", "_src_title", "_src_pub"):
            r.pop(tmp, None)
        try:
            key, here = dup_key(r.get("quote")), url_key(r.get("url"))
            first = seen.get(key) if key else None   # the first kept receipt with this text is the original
            if first and first[1] != here:
                r["dup_of"] = first[0]
            elif key and r["kept"] and not first:
                seen[key] = (r["id"], here)
        except Exception:  # noqa: BLE001
            pass
    return out
