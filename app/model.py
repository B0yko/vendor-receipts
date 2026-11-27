"""The one door to the product's LLM (Lead-owned). Every model call of the agent and of evalkit goes through ask_json.

    from app import model
    obj = model.ask_json(system, user, purpose="judge")     # dict parsed from the model's JSON answer

Live: llm.chat_json over the endpoints in llm.env (OpenRouter, Claude Opus 5.5, reasoning effort xhigh), cached by
request, so a rerun with unchanged prompts is free and a changed judge prompt re-asks only the judge.
Tests: model.set_fake(purpose, fn) with fn(system, user) -> dict; model.clear_fakes().
Smoke: RECEIPTS_FAKE=1 answers from $RECEIPTS_FAKE_DIR/<purpose>.json (default tests/fixtures/fake); a file of the
form {"by_marker": {"marker text": answer, ...}, "default": answer} picks the first marker found in the user text.
Purposes in use: intake, round, says_it, judge, baseline, match, extra, classify.
"""
import json, os, threading

import llm

_fakes = {}
_lock = threading.Lock()


class ModelError(RuntimeError):
    pass


def set_fake(purpose, fn):
    _fakes[purpose] = fn


def clear_fakes():
    _fakes.clear()


def mode():
    return "fake" if os.environ.get("RECEIPTS_FAKE") == "1" else ("replay" if os.environ.get("REPLAY") == "1" else "live")


def model_id():
    if mode() == "fake":
        return "fake"
    eps = llm.endpoints()
    return eps[0][2] if eps else "none"


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


def ask_json(system, user, purpose, max_tokens=None):
    """-> dict. Raises ModelError when the answer is not a JSON object after one retry with a reminder."""
    if purpose in _fakes:
        return _fakes[purpose](system, user)
    if mode() == "fake":
        return _fake_file(purpose, user)
    mt = max_tokens or int(os.environ.get("LLM_MAX_TOKENS", 64000))
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    try:
        obj, _ = llm.chat_json(msgs, max_tokens=mt, purpose=purpose)
    except llm.LLMError as e:
        if "not JSON" not in str(e):
            raise ModelError(str(e)) from e
        msgs.append({"role": "user", "content": "Your answer was not one valid JSON object. Answer again with only "
                                                  "the JSON object, no prose, no code fence."})
        try:
            obj, _ = llm.chat_json(msgs, max_tokens=mt, purpose=purpose + "_retry")
        except llm.LLMError as e2:
            raise ModelError(str(e2)) from e2
    if not isinstance(obj, dict):
        raise ModelError(f"{purpose}: answer is {type(obj).__name__}, not an object")
    return obj
