"""The one door to the product's LLM (Lead-owned). Every model call of the agent and of evalkit goes through ask_json.

    from app import model
    obj = model.ask_json(system, user, purpose="judge")     # dict parsed from the model's JSON answer

Live: Claude Opus 5.5 at thinking xhigh through the pi CLI on the owner's Claude subscription (no API key):
`pi -p --no-session --no-tools ... --system-prompt S --model M --thinking T` with the user text on stdin (tested
27.11 17:25 with 200k chars: 8.6 s, valid JSON). Every answer is cached by request (system, user, purpose, model,
thinking) through cache.cached, so a rerun with unchanged prompts is free and a changed judge prompt re-asks only
the judge; REPLAY=1 turns a miss into an error.
Concurrency: at most MODEL_CONC (default 6) pi processes at once in this Python process. Accounts: MODEL_ACCOUNTS
(default "default,acct3": Claude 1 and Claude 2; never acct2, inactive) round-robin per call via
PI_CLAUDE_TETHER_ACCOUNT; a usage-limit or rate-limit failure retries once on the next account and is logged to
runs/alerts.log. Every real call is logged to runs/model_calls.jsonl (purpose, seconds, account, sizes, rc).
Env: MODEL_ID (pi-claude-tether/claude-opus-5-5), MODEL_THINKING (xhigh), MODEL_TIMEOUT (900 s), PI_BIN (pi).
Tests: model.set_fake(purpose, fn) with fn(system, user) -> dict; model.clear_fakes().
Smoke: RECEIPTS_FAKE=1 answers from $RECEIPTS_FAKE_DIR/<purpose>.json (default tests/fixtures/fake); a file of the
form {"by_marker": {"marker text": answer, ...}, "default": answer} picks the first marker found in the user text.
Purposes in use: intake, round, says_it, judge, baseline, match, extra, classify.
"""
import itertools, json, os, re, subprocess, sys, tempfile, threading, time

import cache

_fakes = {}
_sem = None
_sem_lock = threading.Lock()
_rr = itertools.count()
_log_lock = threading.Lock()
LIMIT_RE = re.compile(r"usage limit|rate limit|rate_limit|429|overloaded|529|too many requests", re.I)


class ModelError(RuntimeError):
    pass


def set_fake(purpose, fn):
    _fakes[purpose] = fn


def clear_fakes():
    _fakes.clear()


def mode():
    return "fake" if os.environ.get("RECEIPTS_FAKE") == "1" else ("replay" if os.environ.get("REPLAY") == "1" else "live")


def _model():
    return os.environ.get("MODEL_ID", "pi-claude-tether/claude-opus-5-5")


def _thinking():
    return os.environ.get("MODEL_THINKING", "xhigh")


def model_id():
    return "fake" if mode() == "fake" else f"{_model()}:{_thinking()}"


def _accounts():
    acc = [a.strip() for a in os.environ.get("MODEL_ACCOUNTS", "default,acct3").split(",") if a.strip()]
    return [a for a in acc if a != "acct2"] or ["default"]


def _semaphore():
    global _sem
    with _sem_lock:
        if _sem is None:
            _sem = threading.BoundedSemaphore(int(os.environ.get("MODEL_CONC", 6)))
        return _sem


def _fake_file(purpose, user):
    path = os.path.join(os.environ.get("RECEIPTS_FAKE_DIR", "tests/fixtures/fake"), purpose + ".json")
    if not os.path.exists(path):
        raise ModelError(f"RECEIPTS_FAKE=1 but no fixture {path}")
    with open(path) as f:
        d = json.load(f)
    if isinstance(d, dict) and "by_marker" in d:
        for marker, answer in d["by_marker"].items():
            if marker in user:
                return answer
        return d.get("default", {})
    return d


def _log(path, line):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with _log_lock, open(path, "a") as f:
            f.write(json.dumps(line) + "\n" if isinstance(line, dict) else line + "\n")
    except OSError:
        pass


def _pi(system, user, purpose, account):
    cmd = [os.environ.get("PI_BIN", "pi"), "-p", "--no-session", "--no-tools", "--no-mcp", "--no-skills",
           "--no-prompt-templates", "--no-context-files", "--system-prompt", system, "--model", _model(),
           "--thinking", _thinking()]
    env = dict(os.environ, PI_CLAUDE_TETHER_ACCOUNT=account)
    t0 = time.time()
    with _semaphore():
        try:
            p = subprocess.run(cmd, input=user, capture_output=True, text=True, env=env, cwd=tempfile.gettempdir(),
                               timeout=float(os.environ.get("MODEL_TIMEOUT", 900)))
            rc, out, err = p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired:
            rc, out, err = -9, "", "timeout"
    secs = round(time.time() - t0, 1)
    _log("runs/model_calls.jsonl", {"at": time.strftime("%H:%M:%S"), "purpose": purpose, "account": account,
                                    "seconds": secs, "in_chars": len(system) + len(user), "out_chars": len(out),
                                    "rc": rc, "err": err[-200:] if rc else ""})
    return rc, out, err


def _text(system, user, purpose):
    """One model answer as text, with one failover to the next account on a limit error."""
    accs = _accounts()
    start = next(_rr) % len(accs)
    errors = []
    for i in range(min(2, len(accs)) if len(accs) > 1 else 2):
        acc = accs[(start + i) % len(accs)]
        rc, out, err = _pi(system, user, purpose, acc)
        if rc == 0 and out.strip():
            return {"text": out, "account": acc}
        msg = (err or out or "empty answer").strip()[-300:]
        errors.append(f"{acc} rc={rc} {msg}")
        if LIMIT_RE.search(msg):
            _log("runs/alerts.log", f"{time.strftime('%Y-%m-%dT%H:%M:%S')} model {purpose} account {acc} limit: {msg[:200]}")
            print(f"model: {acc} limit, trying the next account", file=sys.stderr)
    raise ModelError(f"{purpose}: pi failed: " + " | ".join(errors))


def _parse(text):
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except ValueError:
        pass
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        raise ValueError("no JSON object")
    return json.loads(m.group(0))


def _ask(system, user, purpose):
    key = {"system": system, "user": user, "purpose": purpose, "model": _model(), "thinking": _thinking()}
    return cache.cached("model", key, lambda: _text(system, user, purpose))["text"]


def ask_json(system, user, purpose, max_tokens=None):
    """-> dict. Raises ModelError when the answer is not a JSON object after one retry with a reminder."""
    if purpose in _fakes:
        return _fakes[purpose](system, user)
    if mode() == "fake":
        return _fake_file(purpose, user)
    system = system + "\n\nAnswer with exactly one JSON object and nothing else: no prose, no code fence."
    try:
        text = _ask(system, user, purpose)
        obj = _parse(text)
    except ValueError:
        try:
            text = _ask(system, user + "\n\nYour previous answer was not one valid JSON object. Answer again with "
                                       "only the JSON object.", purpose + "_retry")
            obj = _parse(text)
        except ValueError as e:
            raise ModelError(f"{purpose}: answer is not JSON: {text[:200]!r}") from e
    except cache.CacheMiss as e:
        raise ModelError(str(e)) from e
    if not isinstance(obj, dict):
        raise ModelError(f"{purpose}: answer is {type(obj).__name__}, not an object")
    return obj
