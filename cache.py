"""Disk cache for every slow or paid external call, plus a polite HTTP GET. Generic utility, stdlib only.

    value = cached("search", {"q": q}, lambda: client.search(q))   # any JSON-serialisable result
    page = http_get(url)                                            # {"url", "status", "final_url", "text", "error"}

Keys are sha256(name + args). A miss logs one line to stderr. REPLAY=1 turns a miss into an error (CacheMiss), so the
demo path provably runs offline. CACHE_DIR (default .cache) holds the files; keep it out of git.
"""
import hashlib, json, os, sys, threading, time, urllib.error, urllib.parse, urllib.request

CACHE_DIR = os.environ.get("CACHE_DIR", ".cache")
UA = os.environ.get("HTTP_UA", "Mozilla/5.0 (compatible; research-agent/0.1; polite, cached, low rate)")
_hosts, _lock = {}, threading.Lock()


class CacheMiss(RuntimeError):
    pass


def replay():
    return os.environ.get("REPLAY") == "1"


def cached(name, args, fn):
    key = hashlib.sha256((name + json.dumps(args, sort_keys=True, default=str)).encode()).hexdigest()
    path = os.path.join(CACHE_DIR, name, key[:2], key + ".json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)["value"]
    if replay():
        print(f"CACHE MISS (REPLAY=1) {name} {json.dumps(args, default=str)[:160]}", file=sys.stderr)
        raise CacheMiss(f"{name} {key[:12]} not cached and REPLAY=1")
    print(f"cache miss {name} {json.dumps(args, default=str)[:120]}", file=sys.stderr)
    value = fn()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump({"name": name, "args": args, "at": time.time(), "value": value}, f, default=str)
    os.replace(path + ".tmp", path)
    return value


def _polite(host, min_gap):
    """At most one request per host per `min_gap` seconds (vendors' sites must not be hammered; local test servers skip it)."""
    if host in ("localhost", "127.0.0.1", "::1"):
        return
    with _lock:
        wait = _hosts.get(host, 0) + min_gap - time.time()
        _hosts[host] = time.time() + max(wait, 0)
    if wait > 0:
        time.sleep(wait)


class _Transient(Exception):
    pass


def http_get(url, timeout=15, max_bytes=3_000_000, min_gap=1.0):
    """HTTP answers (200, 404, ...) are cached; network failures (DNS, TLS, timeout) are returned but not cached."""
    def fetch():
        _polite(urllib.parse.urlsplit(url).hostname, min_gap)
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en,de;q=0.8"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read(max_bytes).decode(r.headers.get_content_charset() or "utf-8", "replace")
                return {"url": url, "status": r.status, "final_url": r.geturl(), "text": body, "error": None}
        except urllib.error.HTTPError as e:
            e.close()
            return {"url": url, "status": e.code, "final_url": url, "text": "", "error": f"HTTP {e.code}"}
        except Exception as e:
            raise _Transient(f"{type(e).__name__}: {e}"[:200])
    try:
        return cached("http_get", {"url": url}, fetch)
    except _Transient as e:
        return {"url": url, "status": None, "final_url": url, "text": "", "error": str(e)}
