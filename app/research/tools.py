"""The research loop's tools (lane L1). Stdlib only. Every function returns a dict and never raises; `error` is None or
a string. Contract: SPEC.md § Interfaces.

    from app.research import tools
    r = tools.run_tool("search", {"query": "acme data breach", "time_range": "year"})   # + "tool", "args", "seconds"
    page = tools.fetch("https://example.com")                                        # text, title, links, meta

Tavily (search, news, extract) goes over urllib with `Authorization: Bearer $TAVILY_API_KEY`. Every successful answer
is cached by request (`cache.cached`, key = endpoint + body; the key travels in a header, never in the cache); an error
is never cached. A 429 is retried after Retry-After (capped at 20 s), 3 tries in all. A plan or usage-limit error
(HTTP 432/433, or a body naming the plan, credits or a usage limit) appends one line to runs/alerts.log and stderr.
Vendor pages go through `cache.http_get` (1 request/s per host, no logins). REPLAY=1 turns a cache miss into an error.
"""
import datetime, html.parser, inspect, json, os, re, sys, threading, time, urllib.error, urllib.parse, urllib.request

import cache
from app.ledger import registrable

TAVILY = os.environ.get("TAVILY_URL", "https://api.tavily.com")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ALERTS = os.environ.get("ALERTS_LOG", os.path.join(ROOT, "runs", "alerts.log"))
TEXT_CAP, LINK_CAP = 80_000, 400
RETRY_CAP, TRIES = 20.0, 3
_alert_lock = threading.Lock()
_LIMIT_RE = re.compile(r"\bplan'?s?\b|\bcredits?\b|usage limit|pay-as-you-go|\bpaygo\b|\bquota\b", re.I)


class ToolError(RuntimeError):
    """Raised inside a cached fn so that the failure is not cached; caught by the public functions."""


def _short(e):
    s = str(e) if isinstance(e, (ToolError, cache.CacheMiss)) else f"{type(e).__name__}: {e}"
    return s[:300]


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def alert(source, endpoint, code, message):
    """One line to runs/alerts.log and stderr: '<ISO time> <source> <endpoint> HTTP <code> <message>'."""
    line = f"{_now()} {source} {endpoint} HTTP {code} {' '.join(str(message).split())[:300]}"
    print("ALERT " + line, file=sys.stderr)
    try:
        with _alert_lock:
            os.makedirs(os.path.dirname(ALERTS) or ".", exist_ok=True)
            with open(ALERTS, "a") as f:
                f.write(line + "\n")
    except OSError as e:
        print(f"alert: cannot write {ALERTS}: {e}", file=sys.stderr)


def _http_error_message(e):
    try:
        body = e.read().decode("utf-8", "replace")
    except Exception:
        body = ""
    finally:
        e.close()
    try:
        j = json.loads(body)
        d = j.get("detail") or j.get("message") or j.get("error") or body
        if isinstance(d, dict):
            d = d.get("error") or d
        return (d if isinstance(d, str) else json.dumps(d))[:300]
    except Exception:
        return (body.strip() or str(e.reason))[:300]


def _retry_after(headers, attempt):
    v = (headers.get("Retry-After") if headers is not None else None) or ""
    try:
        s = float(v)
    except ValueError:
        s = 2.0 ** attempt
    return min(max(s, 0.0), RETRY_CAP)


def _is_limit(code, msg):
    return code in (432, 433) or (code != 434 and bool(_LIMIT_RE.search(msg or "")))


def _tavily(endpoint, body=None, timeout=60):
    """POST (body given) or GET to the Tavily API -> parsed JSON. Raises ToolError (never cached)."""
    key = os.environ.get("TAVILY_API_KEY", "")
    if not key:
        raise ToolError("TAVILY_API_KEY not set (run through make or kc env TAVILY_API_KEY=tavily-api-key --)")
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    data = None
    if body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    name = endpoint.strip("/")
    for attempt in range(1, TRIES + 1):
        req = urllib.request.Request(TAVILY + endpoint, data=data, headers=headers,
                                     method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            msg = _http_error_message(e)
            if _is_limit(e.code, msg):
                alert("tavily", name, e.code, msg)
                raise ToolError(f"tavily {name} HTTP {e.code} {msg}")
            if e.code == 429 and attempt < TRIES:
                time.sleep(_retry_after(e.headers, attempt))
                continue
            raise ToolError(f"tavily {name} HTTP {e.code} {msg}")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            raise ToolError(f"tavily {name}: {type(e).__name__}: {e}"[:300])
    raise ToolError(f"tavily {name}: retries exhausted")


def _domains(v):
    if not v:
        return []
    return [v] if isinstance(v, str) else [str(x) for x in v]


# ---------------------------------------------------------------------------------------------------- Tavily tools
def search(query, topic="general", time_range=None, depth="advanced", max_results=8, include_domains=None,
           exclude_domains=None):
    """Tavily /search -> {"query", "results": [{"url", "title", "content", "published_date", "score"}], "error"}."""
    out = {"query": query, "results": [], "error": None}
    try:
        body = {"query": str(query), "search_depth": depth, "topic": topic or "general",
                "max_results": max(1, min(int(max_results), 20)), "include_published_date": True}
        if depth in ("advanced", "basic", "fast"):
            body["chunks_per_source"] = 3
        if time_range:
            body["time_range"] = time_range
        if _domains(include_domains):
            body["include_domains"] = _domains(include_domains)
        if _domains(exclude_domains):
            body["exclude_domains"] = _domains(exclude_domains)
        raw = cache.cached("tavily_search", body, lambda: _tavily("/search", body))
        out["results"] = [{"url": r.get("url") or "", "title": r.get("title") or "", "content": r.get("content") or "",
                           "published_date": r.get("published_date"), "score": r.get("score")}
                          for r in raw.get("results") or [] if r.get("url")]
    except Exception as e:
        out["error"] = _short(e)
    return out


def news(query, time_range="year", max_results=8, include_domains=None, exclude_domains=None):
    """search() with topic "news" (published dates on every result), default window one year."""
    return search(query, topic="news", time_range=time_range, max_results=max_results,
                  include_domains=include_domains, exclude_domains=exclude_domains)


def extract(urls, depth="advanced"):
    """Tavily /extract in batches of 20 -> {"results": [{"url", "text", "published_date"}], "failed": [{"url",
    "error"}], "error"}. Text is plain (format "text", so quotes copy verbatim), capped at 80k chars per page."""
    out = {"results": [], "failed": [], "error": None}
    try:
        lst = [urls] if isinstance(urls, str) else list(urls or [])
        lst = list(dict.fromkeys(str(u).strip() for u in lst if str(u).strip()))
        if not lst:
            out["error"] = "no urls"
            return out
        for i in range(0, len(lst), 20):
            batch = lst[i:i + 20]
            body = {"urls": batch, "extract_depth": depth, "format": "text", "include_published_date": True}
            try:
                raw = cache.cached("tavily_extract", body, lambda: _tavily("/extract", body, timeout=120))
            except Exception as e:
                out["error"] = _short(e)
                out["failed"] += [{"url": u, "error": out["error"]} for u in batch]
                continue
            out["results"] += [{"url": r.get("url") or "", "text": (r.get("raw_content") or "")[:TEXT_CAP],
                                "published_date": r.get("published_date")} for r in raw.get("results") or []]
            out["failed"] += [{"url": f.get("url") or "", "error": f.get("error") or "failed"}
                              for f in raw.get("failed_results") or []]
    except Exception as e:
        out["error"] = _short(e)
    return out


def usage():
    """Tavily /usage (not cached; the endpoint allows 10 requests per 10 minutes) -> its JSON plus "error"."""
    if cache.replay():
        return {"error": "REPLAY=1: usage is not available offline"}
    try:
        d = _tavily("/usage", None, timeout=20)
        return {**(d if isinstance(d, dict) else {"value": d}), "error": None}
    except Exception as e:
        return {"error": _short(e)}


# ---------------------------------------------------------------------------------------------------- vendor pages
_SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "object", "canvas"}
_BLOCK = {"address", "article", "aside", "blockquote", "br", "dd", "details", "dialog", "div", "dl", "dt", "fieldset",
          "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main",
          "nav", "ol", "p", "pre", "section", "summary", "table", "tbody", "thead", "tfoot", "tr", "ul", "option",
          "caption", "body", "html", "head"}
_COPY_RE = re.compile(r"(?:©|&copy;|\bcopyright\b)\s*[^\n\d]{0,40}?((?:19|20)\d{2})"
                      r"(?:\s*(?:-|–|—|to)\s*((?:19|20)\d{2}))?", re.I)


class _Page(html.parser.HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base, self.parts, self.skip = base, [], 0
        self.title, self.in_title, self.generator = "", False, ""
        self.links, self.link = [], None

    def _end_link(self):
        if self.link:
            url, text, label = self.link
            if not "".join(text).strip():
                text = [label]
            self.links.append({"url": url, "text": " ".join(" ".join(text).split())[:200]})
            self.link = None

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        a = {k: (v or "") for k, v in attrs}
        if tag == "title" and not self.title:
            self.in_title = True
        elif tag == "meta" and (a.get("name") or "").lower() == "generator" and not self.generator:
            self.generator = a.get("content", "").strip()
        elif tag == "base" and a.get("href"):
            self.base = urllib.parse.urljoin(self.base, a["href"])
        elif tag == "a":
            self._end_link()
            href = a.get("href", "").strip()
            if href:
                u = urllib.parse.urldefrag(urllib.parse.urljoin(self.base, href))[0]
                if u.startswith(("http://", "https://")):
                    self.link = (u, [], a.get("aria-label") or a.get("title") or "")
        elif tag == "img" and self.link is not None and not "".join(self.link[1]).strip():
            self.link[1].append(a.get("alt", ""))
        if tag in _BLOCK:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" ")

    def handle_startendtag(self, tag, attrs):
        if tag in _SKIP:
            return
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "title":
            self.in_title = False
        elif tag == "a":
            self._end_link()
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.skip:
            return
        if self.in_title:
            self.title += data
            return
        self.parts.append(re.sub(r"\s+", " ", data))
        if self.link is not None:
            self.link[1].append(data)

    def result(self):
        self._end_link()
        lines = (" ".join(ln.split()) for ln in "".join(self.parts).split("\n"))
        return "\n".join(ln for ln in lines if ln)


def html_to_text(html_text, base_url=""):
    """-> {"title", "text" (uncapped), "links": [{"url", "text"}] (deduped, uncapped), "generator"}."""
    p = _Page(base_url)
    try:
        p.feed(html_text or "")
        p.close()
    except Exception:
        pass
    seen, links = set(), []
    for ln in p.links:
        if ln["url"] not in seen:
            seen.add(ln["url"])
            links.append(ln)
    return {"title": " ".join(p.title.split()), "text": p.result(), "links": links, "generator": p.generator}


def copyright_years(text):
    years = set()
    for m in _COPY_RE.finditer(text or ""):
        years.update(int(y) for y in m.groups() if y)
    return sorted(years)


def fetch(url):
    """A vendor page through cache.http_get -> {"url", "final_url", "status", "title", "text", "links": [{"url",
    "text"}], "meta": {"generator", "copyright_years": []}, "error"}. Text: scripts, styles and svg dropped; headings
    and list items on their own lines; capped at 80k chars. Links absolute (http/https), deduped, capped at 400."""
    url = str(url or "").strip()
    if url and "://" not in url:
        url = "https://" + url
    out = {"url": url, "final_url": url, "status": None, "title": "", "text": "", "links": [],
           "meta": {"generator": "", "copyright_years": []}, "error": None}
    if not url.startswith(("http://", "https://")):
        out["error"] = f"not an http(s) url: {url!r}"
        return out
    try:
        r = cache.http_get(url)
        out.update(final_url=r.get("final_url") or url, status=r.get("status"), error=r.get("error"))
        body = r.get("text") or ""
        if body.lstrip().startswith("%PDF"):
            out["error"] = out["error"] or "pdf: use extract"
            return out
        page = html_to_text(body, out["final_url"])
        out.update(title=page["title"], text=page["text"][:TEXT_CAP], links=page["links"][:LINK_CAP])
        out["meta"] = {"generator": page["generator"], "copyright_years": copyright_years(page["text"])}
    except Exception as e:
        out["error"] = _short(e)
    return out


# ---------------------------------------------------------------------------------------------------- open data APIs
# Polite gap per host in seconds (cache._polite); 1 s unless named. dns.google is a public resolver built for volume.
GAPS = {"dns.google": 0.2}
MAX_BYTES = 40_000_000


def _get(url, timeout=30, headers=None, tries=2, alert_on=None):
    """Polite GET -> (final_url, text). Raises ToolError on an HTTP error or a network failure (never cached).
    429/502/503/504 are retried once after Retry-After (capped). `alert_on`: a source name whose rate or plan limit
    (HTTP 403/429 naming a limit) goes to runs/alerts.log."""
    parts = urllib.parse.urlsplit(url)
    h = {"User-Agent": cache.UA, "Accept": "application/json", **(headers or {})}
    for attempt in range(1, tries + 1):
        cache._polite(parts.hostname, GAPS.get(parts.hostname, 1.0))
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
                hdrs = getattr(r, "headers", None)
                cs = (hdrs.get_content_charset() if hdrs is not None else None) or "utf-8"
                return r.geturl(), r.read(MAX_BYTES).decode(cs, "replace")
        except urllib.error.HTTPError as e:
            msg = _http_error_message(e)
            if alert_on and e.code in (403, 429) and re.search(r"rate limit|quota|limit exceeded", msg, re.I):
                alert(alert_on, parts.path or "/", e.code, msg)
                raise ToolError(f"{parts.hostname} HTTP {e.code} {msg[:200]}")
            if e.code in (429, 502, 503, 504) and attempt < tries:
                time.sleep(_retry_after(e.headers, attempt))
                continue
            raise ToolError(f"{parts.hostname} HTTP {e.code} {msg[:200]}")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            raise ToolError(f"{parts.hostname}: {type(e).__name__}: {e}"[:300])
    raise ToolError(f"{parts.hostname}: retries exhausted")


def _get_json(url, **kw):
    final, text = _get(url, **kw)
    try:
        return json.loads(text) if text.strip() else None
    except ValueError:
        raise ToolError(f"{urllib.parse.urlsplit(url).hostname}: answer is not JSON: {text[:120]!r}")


def _day(v):
    """'2023-05-01T12:00:00Z' | '20230501123456' | epoch -> 'YYYY-MM-DD' ('' when unknown)."""
    if v in (None, ""):
        return ""
    if isinstance(v, (int, float)):
        return datetime.datetime.fromtimestamp(v, datetime.timezone.utc).strftime("%Y-%m-%d")
    v = str(v).strip()
    if re.fullmatch(r"\d{8,14}", v):
        return f"{v[:4]}-{v[4:6]}-{v[6:8]}"
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)", v)
    return "-".join(m.groups()) if m else ""


def _host(v):
    v = str(v or "").strip()
    return (urllib.parse.urlsplit(v if "://" in v else "https://" + v).hostname or "").lower().rstrip(".")


def _ts(v):
    """'2023', '2023-05-01', '20230501' -> digits of a Wayback timestamp (1-14)."""
    return re.sub(r"\D", "", str(v or ""))[:14]


def _archive_url(ts, original):
    return f"https://web.archive.org/web/{ts}/{original}"


def wayback(url, limit=200):
    """Wayback CDX, one capture per month -> {"url", "first", "last" ('YYYY-MM-DD'), "count", "snapshots":
    [{"timestamp" (14 digits), "url" (the archive URL), "status"}], "error"}. When the monthly list reaches `limit`,
    one more query fetches the true latest capture (flag "truncated")."""
    out = {"url": url, "first": None, "last": None, "count": 0, "snapshots": [], "error": None}
    try:
        target = str(url or "").strip()
        if not target:
            raise ToolError("no url")
        n = max(1, min(int(limit), 1000))
        q = {"url": target, "output": "json", "fl": "timestamp,original,statuscode", "collapse": "timestamp:6",
             "limit": n}

        def call():
            rows = _get_json("https://web.archive.org/cdx/search/cdx?" + urllib.parse.urlencode(q), timeout=40) or []
            snaps = [{"timestamp": r[0], "url": _archive_url(r[0], r[1]), "status": r[2]}
                     for r in rows[1:] if isinstance(r, list) and len(r) >= 3]
            last = snaps[-1]["timestamp"] if snaps else None
            if len(snaps) >= n:
                lq = {"url": target, "output": "json", "fl": "timestamp,original,statuscode", "limit": -1}
                tail = _get_json("https://web.archive.org/cdx/search/cdx?" + urllib.parse.urlencode(lq),
                                 timeout=40) or []
                if len(tail) > 1 and tail[-1]:
                    last = max(last, tail[-1][0])
            return {"snapshots": snaps, "last": last, "truncated": len(snaps) >= n}
        d = cache.cached("wayback", q, call)
        snaps = d["snapshots"]
        out.update(snapshots=snaps, count=len(snaps), truncated=d["truncated"],
                   first=_day(snaps[0]["timestamp"]) if snaps else None, last=_day(d["last"]) or None)
    except Exception as e:
        out["error"] = _short(e)
    return out


def wayback_get(url, timestamp):
    """The archived page nearest `timestamp` -> {"url" (the archive URL of the capture), "timestamp" (14 digits of the
    capture), "text" (as in fetch), "title", "error"}."""
    ts = _ts(timestamp)
    out = {"url": url, "timestamp": ts, "text": "", "title": "", "error": None}
    try:
        target = str(url or "").strip()
        if not target or not ts:
            raise ToolError("need url and timestamp (YYYYMMDD)")

        def call():
            final, body = _get(f"https://web.archive.org/web/{ts}id_/{target}", timeout=40,
                               headers={"Accept": "text/html,*/*;q=0.8"})
            m = re.search(r"/web/(\d{4,14})(?:id_)?/", final)
            got = m.group(1) if m else ts
            page = html_to_text(body, target)
            return {"url": _archive_url(got, target), "timestamp": got, "title": page["title"],
                    "text": page["text"][:TEXT_CAP]}
        out.update(cache.cached("wayback_get", {"url": target, "timestamp": ts}, call))
    except Exception as e:
        out["error"] = _short(e)
    return out


_RCODES = {0: "NOERROR", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN", 5: "REFUSED"}


def _doh(name, rtype):
    q = {"name": name, "type": rtype}

    def call():
        d = _get_json("https://dns.google/resolve?" + urllib.parse.urlencode(q), timeout=15) or {}
        if d.get("Status") == 2:
            raise ToolError(f"dns {name} {rtype}: SERVFAIL")
        return d
    return cache.cached("dns", q, call)


def dns(host):
    """DNS over HTTPS (dns.google) -> {"host", "a", "aaaa", "cname" (the chain), "ns" (of the registrable domain when
    the host has none), "rcode" ("NXDOMAIN" = the host does not exist: an answer, not an error), "error"}."""
    h = _host(host)
    out = {"host": h, "a": [], "aaaa": [], "cname": [], "ns": [], "rcode": None, "error": None}
    try:
        if not h:
            raise ToolError("no host")
        types = {1: "a", 28: "aaaa", 5: "cname", 2: "ns"}
        for rtype in ("A", "AAAA", "NS"):
            d = _doh(h, rtype)
            if rtype == "A":
                out["rcode"] = _RCODES.get(d.get("Status"), str(d.get("Status")))
            for ans in d.get("Answer") or []:
                k = types.get(ans.get("type"))
                v = str(ans.get("data", "")).rstrip(".")
                if k and v and v not in out[k]:
                    out[k].append(v)
        apex = registrable(h)
        if not out["ns"] and apex and apex != h:
            out["ns"] = [str(a.get("data", "")).rstrip(".") for a in _doh(apex, "NS").get("Answer") or []
                         if a.get("type") == 2]
    except Exception as e:
        out["error"] = _short(e)
    return out


def crtsh(domain):
    """Certificate-transparency names under the domain (crt.sh) -> {"domain", "names" (deduped, sorted, cap 300),
    "first_seen" (earliest not_before, 'YYYY-MM-DD'), "count" (distinct names before the cap), "error"}."""
    d = _host(domain).removeprefix("www.")
    out = {"domain": d, "names": [], "first_seen": None, "count": 0, "error": None}
    try:
        if not d:
            raise ToolError("no domain")

        def call():
            rows = _get_json(f"https://crt.sh/?q=%25.{urllib.parse.quote(d)}&output=json", timeout=40) or []
            names, first = set(), None
            for r in rows:
                for n in str(r.get("name_value", "")).lower().split("\n"):
                    n = n.strip().removeprefix("*.")
                    if n and (n == d or n.endswith("." + d)):
                        names.add(n)
                nb = _day(r.get("not_before"))
                if nb and (first is None or nb < first):
                    first = nb
            return {"names": sorted(names), "first_seen": first, "certs": len(rows)}
        r = cache.cached("crtsh", {"domain": d}, call)
        out.update(names=r["names"][:300], count=len(r["names"]), first_seen=r["first_seen"], certs=r["certs"])
    except Exception as e:
        out["error"] = _short(e)
    return out


def _pypi(name):
    d = _get_json(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json")
    latest = d["info"]["version"]
    times = {v: min((f.get("upload_time_iso_8601") or f.get("upload_time") or "") for f in fs)
             for v, fs in (d.get("releases") or {}).items() if fs}
    return {"latest": latest, "latest_date": _day(times.get(latest)), "first_date": _day(min(times.values(), default="")),
            "url": f"https://pypi.org/project/{name}/"}


def _npm(name):
    d = _get_json("https://registry.npmjs.org/" + urllib.parse.quote(name, safe="@"))
    latest = (d.get("dist-tags") or {}).get("latest", "")
    t = d.get("time") or {}
    return {"latest": latest, "latest_date": _day(t.get(latest)), "first_date": _day(t.get("created")),
            "url": f"https://www.npmjs.com/package/{name}"}


def _crates(name):
    d = _get_json(f"https://crates.io/api/v1/crates/{urllib.parse.quote(name)}")
    c = d.get("crate") or {}
    latest = c.get("max_stable_version") or c.get("newest_version") or c.get("max_version") or ""
    ld = next((v.get("created_at") for v in d.get("versions") or [] if v.get("num") == latest), c.get("updated_at"))
    return {"latest": latest, "latest_date": _day(ld), "first_date": _day(c.get("created_at")),
            "url": f"https://crates.io/crates/{name}"}


def _packagist(name):
    d = _get_json(f"https://packagist.org/packages/{urllib.parse.quote(name, safe='/')}.json")
    p = d.get("package") or {}
    vers = [v for k, v in (p.get("versions") or {}).items() if not k.startswith("dev-") and "-dev" not in k]
    stable = [v for v in vers if not re.search(r"(alpha|beta|rc)", v.get("version", ""), re.I)] or vers
    top = max(stable, key=lambda v: v.get("time") or "", default={})
    times = [v.get("time") for v in vers if v.get("time")]
    return {"latest": top.get("version", ""), "latest_date": _day(top.get("time")),
            "first_date": _day(min(times, default="") or p.get("time")), "url": f"https://packagist.org/packages/{name}"}


def _rubygems(name):
    vs = _get_json(f"https://rubygems.org/api/v1/versions/{urllib.parse.quote(name)}.json") or []
    top = next((v for v in vs if not v.get("prerelease")), vs[0] if vs else {})
    return {"latest": top.get("number", ""), "latest_date": _day(top.get("created_at")),
            "first_date": _day(min((v.get("created_at") or "" for v in vs), default="")),
            "url": f"https://rubygems.org/gems/{name}"}


REGISTRIES = {"pypi": _pypi, "npm": _npm, "crates": _crates, "packagist": _packagist, "rubygems": _rubygems}


def registry(name, ecosystem):
    """A package on pypi, npm, crates, packagist or rubygems -> {"name", "ecosystem", "latest", "latest_date",
    "first_date" ('YYYY-MM-DD'), "url" (the registry page), "error"}."""
    eco = str(ecosystem or "").lower().strip()
    eco = {"pip": "pypi", "python": "pypi", "node": "npm", "cargo": "crates", "crates.io": "crates", "rust": "crates",
           "composer": "packagist", "php": "packagist", "gem": "rubygems", "ruby": "rubygems"}.get(eco, eco)
    name = str(name or "").strip()
    out = {"name": name, "ecosystem": eco, "latest": None, "latest_date": None, "first_date": None, "url": None,
           "error": None}
    try:
        if eco not in REGISTRIES:
            raise ToolError(f"unknown ecosystem {ecosystem!r}; one of {', '.join(REGISTRIES)}")
        if not name:
            raise ToolError("no package name")
        r = cache.cached("registry", {"name": name, "ecosystem": eco}, lambda: REGISTRIES[eco](name))
        out.update({k: (r.get(k) or None) for k in ("latest", "latest_date", "first_date", "url")})
    except Exception as e:
        out["error"] = _short(e)
    return out


def github(owner_repo):
    """GitHub repository facts (REST, unauthenticated unless GITHUB_TOKEN is set: 60 requests/hour) -> {"repo",
    "pushed_at", "created_at" ('YYYY-MM-DD'), "stars", "archived", "license" (SPDX id), "open_issues", "url",
    "error"}."""
    v = str(owner_repo or "").strip()
    path = v.split("github.com", 1)[1] if "github.com" in v else v
    parts = [x for x in re.split(r"[/:]", path.split("?")[0].split("#")[0]) if x][:2]
    ok = len(parts) == 2 and all(re.fullmatch(r"[A-Za-z0-9_.-]+", x) for x in parts)
    repo = f"{parts[0]}/{parts[1].removesuffix('.git')}" if ok else v
    out = {"repo": repo, "pushed_at": None, "created_at": None, "stars": None, "archived": None, "license": None,
           "open_issues": None, "url": None, "error": None}
    try:
        if not ok:
            raise ToolError(f"not owner/repo: {v!r}")
        hdr = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if os.environ.get("GITHUB_TOKEN"):
            hdr["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]

        def call():
            d = _get_json(f"https://api.github.com/repos/{repo}", headers=hdr, alert_on="github")
            return {"repo": d.get("full_name") or repo, "pushed_at": _day(d.get("pushed_at")),
                    "created_at": _day(d.get("created_at")), "stars": d.get("stargazers_count"),
                    "archived": d.get("archived"), "license": (d.get("license") or {}).get("spdx_id"),
                    "open_issues": d.get("open_issues_count"), "url": d.get("html_url")}
        out.update(cache.cached("github", {"repo": repo.lower()}, call))
    except Exception as e:
        out["error"] = _short(e)
    return out


def research(query):
    """Tavily /research. Not available: the docs pack (data/docs/tavily-llms.txt) names the endpoint but documents
    no request or response fields, so the call would be a guess; the loop uses search + extract instead."""
    return {"report": "", "sources": [], "error": "not available"}


# ---------------------------------------------------------------------------------------------------- the registry
TOOLS = {
    "search": {"fn": search, "args": ["query", "topic", "time_range", "max_results", "include_domains",
                                      "exclude_domains"],
               "doc": "search(query, topic='general'|'news'|'finance', time_range=None|'day'|'week'|'month'|'year', "
                      "max_results=8, include_domains=[...], exclude_domains=[...]): web search; per result url, title, "
                      "content snippets, published_date"},
    "news": {"fn": news, "args": ["query", "time_range", "max_results", "include_domains", "exclude_domains"],
             "doc": "news(query, time_range='year'|'month'|'week'|'day', max_results=8, include_domains=[...], "
                    "exclude_domains=[...]): news search with publish dates"},
    "extract": {"fn": extract, "args": ["urls"],
                "doc": "extract(urls=[up to 20 urls]): full plain text of pages (JS pages, PDFs, third-party sites)"},
    "fetch": {"fn": fetch, "args": ["url"],
              "doc": "fetch(url): one page directly (vendor pages): text, title, links, generator, copyright years"},
    "wayback": {"fn": wayback, "args": ["url", "limit"],
                "doc": "wayback(url, limit=200): Internet Archive snapshots of a URL or domain (first, last, count, "
                       "monthly list)"},
    "wayback_get": {"fn": wayback_get, "args": ["url", "timestamp"],
                    "doc": "wayback_get(url, timestamp='YYYYMMDD'): text of the archived page nearest that date"},
    "dns": {"fn": dns, "args": ["host"],
            "doc": "dns(host): A, AAAA, CNAME and NS records (hosting, data residency, real subdomains)"},
    "crtsh": {"fn": crtsh, "args": ["domain"],
              "doc": "crtsh(domain): certificate-log host names under the domain and the first certificate date"},
    "registry": {"fn": registry, "args": ["name", "ecosystem"],
                 "doc": "registry(name, ecosystem='pypi'|'npm'|'crates'|'packagist'|'rubygems'): latest version and "
                        "date, first release date"},
    "github": {"fn": github, "args": ["owner_repo"],
               "doc": "github(owner_repo='owner/repo'): last push, stars, archived, licence, open issues"},
}


def tool_docs():
    """The TOOLS docs as lines for the loop prompt."""
    return "\n".join(f"- {t['doc']}" for t in TOOLS.values())


def run_tool(name, args=None):
    """Validate and run one tool -> its dict plus "tool", "args", "seconds". Never raises."""
    t0 = time.time()
    args = args if args is not None else {}
    base = {"tool": name, "args": args}

    def fail(msg):
        return {"error": msg, **base, "seconds": round(time.time() - t0, 3)}
    if name not in TOOLS:
        return fail(f"unknown tool {name!r}; tools: {', '.join(TOOLS)}")
    if not isinstance(args, dict):
        return fail(f"args must be an object, got {type(args).__name__}")
    spec = TOOLS[name]
    bad = [k for k in args if k not in spec["args"]]
    if bad:
        return fail(f"unknown args {bad} for {name}; allowed: {spec['args']}")
    params = inspect.signature(spec["fn"]).parameters
    missing = [k for k, p in params.items() if p.default is inspect.Parameter.empty and k not in args
               and p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)]
    if missing:
        return fail(f"missing args {missing} for {name}")
    try:
        out = spec["fn"](**args)
    except Exception as e:  # the tools never raise; this is the last net
        out = {"error": _short(e)}
    if not isinstance(out, dict):
        out = {"value": out, "error": None}
    out.setdefault("error", None)
    return {**out, **base, "seconds": round(time.time() - t0, 3)}
