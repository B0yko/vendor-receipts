"""OpenAI-compatible chat client over an ordered list of endpoints, cached. Generic utility, stdlib only.

    from llm import chat, chat_json
    r = chat([{"role": "user", "content": "..."}])        # {"text", "endpoint", "model", "seconds", "usage"}
    obj, r = chat_json([...])                              # first JSON object in the answer, parsed

Endpoints come from the environment, in order: LLM_ORDER="sparks,nebius". Each NAME needs LLM_<NAME>_URL (base URL
ending in /v1), LLM_<NAME>_MODEL and LLM_<NAME>_KEY (the key itself, injected by `kc env LLM_SPARKS_KEY=item -- ...`;
never in a file). Switching provider is an env change, not a code change.
Per call: LLM_TIMEOUT (60 s) per request; LLM_RETRIES (2) retries per endpoint on 429/5xx/529/timeouts with
exponential backoff (LLM_BACKOFF s, default 1) plus jitter that honours Retry-After (capped at 20 s); then the next endpoint. A 429 that names
quota, billing or credits is never retried, nor are 400/401/403/404/422. One deadline for the whole call
(LLM_DEADLINE, 180 s). No nested retries. Answers are cached by request plus the first endpoint's model (or
LLM_CACHE_TAG), so REPLAY=1 replays whichever endpoint answered.
"""
import json, os, random, re, sys, time, urllib.error, urllib.request

import cache


class LLMError(RuntimeError):
    pass


def endpoints(env=None):
    env = os.environ if env is None else env
    out = []
    for name in (n.strip() for n in env.get("LLM_ORDER", "").split(",") if n.strip()):
        u, m, k = (env.get(f"LLM_{name.upper()}_{x}", "") for x in ("URL", "MODEL", "KEY"))
        if u and m:
            out.append((name, u.rstrip("/"), m, k))
        else:
            print(f"llm: endpoint {name} skipped (LLM_{name.upper()}_URL or _MODEL unset)", file=sys.stderr)
    return out


def _post(url, key, body, timeout):
    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})}
    req = urllib.request.Request(url + "/chat/completions", data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _call(messages, temperature, max_tokens, json_mode, eps):
    if not eps:
        raise LLMError("no LLM endpoint configured: set LLM_ORDER and LLM_<NAME>_URL, _MODEL, _KEY")
    deadline = time.time() + float(os.environ.get("LLM_DEADLINE", 180))
    timeout, retries = float(os.environ.get("LLM_TIMEOUT", 60)), int(os.environ.get("LLM_RETRIES", 2))
    errors = []
    for name, url, model, key in eps:
        for attempt in range(retries + 1):
            left = deadline - time.time()
            if left <= 1:
                raise LLMError("deadline exceeded: " + "; ".join(errors))
            body = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
            extra = os.environ.get(f"LLM_{name.upper()}_EXTRA", "").strip()
            if extra:  # provider extras, e.g. {"reasoning": {"effort": "xhigh"}}; reasoning models take no temperature
                body.update(json.loads(extra))
                if "reasoning" in body:
                    body.pop("temperature", None)
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            t0, wait = time.time(), None
            try:
                d = _post(url, key, body, min(timeout, left))
                _cost_log(name, d, time.time() - t0)
                return {"text": d["choices"][0]["message"].get("content") or "", "endpoint": name,
                        "model": d.get("model", model), "seconds": round(time.time() - t0, 2), "usage": d.get("usage")}
            except urllib.error.HTTPError as e:
                detail, wait = e.read(600).decode("utf-8", "replace").lower(), e.headers.get("Retry-After")
                e.close()
                errors.append(f"{name} HTTP {e.code}")
                if e.code in (400, 401, 403, 404, 422) or (e.code == 429 and re.search(r"quota|billing|credit|insufficient", detail)):
                    break  # our request, key or spend cap: retrying cannot help, try the next endpoint
            except (urllib.error.URLError, TimeoutError, OSError, KeyError, IndexError, ValueError) as e:
                errors.append(f"{name} {type(e).__name__}")
            if attempt < retries:
                base = float(os.environ.get("LLM_BACKOFF", 1))
                pause = float(wait) if wait and wait.replace(".", "", 1).isdigit() else base * (2 ** attempt + random.random())
                time.sleep(max(0.0, min(20.0, pause, deadline - time.time() - 1)))
        print(f"llm: endpoint {name} failed ({errors[-1]}), trying the next", file=sys.stderr)
    raise LLMError("all endpoints failed: " + "; ".join(errors))


def _cost_log(name, d, seconds):
    """One line per real (uncached) call into LLM_COST_LOG, if set: model, tokens, cost (OpenRouter usage.include)."""
    path = os.environ.get("LLM_COST_LOG")
    if not path:
        return
    try:
        u = d.get("usage") or {}
        line = {"at": time.time(), "endpoint": name, "model": d.get("model"), "seconds": round(seconds, 1),
                "prompt": u.get("prompt_tokens"), "completion": u.get("completion_tokens"),
                "reasoning": (u.get("completion_tokens_details") or {}).get("reasoning_tokens"), "cost": u.get("cost")}
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(line) + "\n")
    except Exception:
        pass


def chat(messages, temperature=0, max_tokens=2000, json_mode=False, purpose="chat"):
    eps = endpoints()
    tag = os.environ.get("LLM_CACHE_TAG") or (eps[0][2] + os.environ.get(f"LLM_{eps[0][0].upper()}_EXTRA", "") if eps else "")
    args = {"messages": messages, "temperature": temperature, "max_tokens": max_tokens, "json_mode": json_mode,
            "purpose": purpose, "tag": tag}
    return cache.cached("llm", args, lambda: _call(messages, temperature, max_tokens, json_mode, eps))


def chat_json(messages, **kw):
    r = chat(messages, **kw)
    m = re.search(r"\{.*\}", r["text"], re.S)
    try:
        return json.loads(m.group(0) if m else r["text"]), r
    except ValueError as e:
        raise LLMError(f"answer is not JSON: {r['text'][:200]!r}") from e
