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
        d = json.loads(body).get("detail", body)
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


# ---------------------------------------------------------------------------------------------------- L1-2 stubs
def _stub(**kw):
    return {**kw, "error": "not implemented"}


def wayback(url, limit=200):
    return _stub(url=url, first=None, last=None, count=0, snapshots=[])


def wayback_get(url, timestamp):
    return _stub(url=url, timestamp=timestamp, text="")


def dns(host):
    return _stub(host=host, a=[], aaaa=[], cname=[], ns=[])


def crtsh(domain):
    return _stub(domain=domain, names=[], first_seen=None, count=0)


def registry(name, ecosystem):
    return _stub(name=name, ecosystem=ecosystem, latest=None, latest_date=None, first_date=None, url=None)


def github(owner_repo):
    return _stub(repo=owner_repo, pushed_at=None, stars=None, archived=None, license=None, open_issues=None, url=None)


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
