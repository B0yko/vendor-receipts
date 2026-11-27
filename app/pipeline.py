"""One product end to end: research -> receipt checks -> disconfirmation -> judge -> self-audit -> verdict + trace
(lane L5).

    v = pipeline.check_vendor(url, "runs/<run_id>/<slug>", deadline=time.time() + 1200,
                              hints={"id": "3", "product": "Acme Notes", "vendor": "Acme Ltd"})

hints (from the blind CSV) go to research(url, deadline=deadline, hints=hints); a research function without a hints
parameter is called without them. verdict["id"] = hints["id"] when given.
Writes identity.json, research.json, receipts.json, verdict.json and trace.md into run_dir. The verdict gets trace,
run_id (the parent folder of run_dir), seconds, mode, error None unless the judge set one, and stats (tool_calls,
llm_calls, capped, receipts_kept, receipts_rejected, disconfirmation, audit, and steps: the wall seconds of research,
check, disconfirm, judge and audit, None for a step that was not reached). Any exception becomes an error verdict (Yellow,
reliability low, "missing evidence is no signal"); whatever research was written stays on disk.
Disconfirmation (app.check.disconfirm, lane L6) runs after the receipt checks unless DISCONFIRM=0 or the module is
absent: research, receipts, record = disconfirm(research, receipts, deadline=deadline). Its record lands in
research["disconfirmation"]; on any exception the research and receipts from before the call are kept and the record
is {"skipped": "<error>"}, and the run carries on.
Judge near the cap: when the deadline leaves under SINGLE_VIEW_BELOW (300) s at judge time and the judge would run
more than one view (JUDGE_VIEWS), it is called with views=1 (an argument, never os.environ: products run in threads)
and verdict["guards"] gets "judge: single view (cap)".
Self-audit (app.check.audit, lane L6, code only) runs on the judge's verdict unless AUDIT=0 or the module is absent:
verdict = audit(verdict, receipts), which fills verdict["audit"]. On any exception or a malformed return the judge's
verdict stays as it was, verdict["audit"] = {"skipped": "<error>"}, and the failure is a guard log line.
The lanes' modules are imported inside the functions, so each lane can land on its own.
"""
import copy, inspect, json, os, time, traceback

from app import ledger, model
from app.out import trace as trace_md


def _research(url, deadline=None, hints=None):
    from app.research.agent import research
    return _call(research, url, deadline, hints)


def _takes(fn, name, default=False):
    """Does fn accept the keyword `name`? `default` when its signature cannot be read."""
    try:
        ps = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return default
    return any(p.name == name or p.kind is p.VAR_KEYWORD for p in ps)


def _call(fn, url, deadline, hints):
    """research_fn(url, deadline=..., hints=...); hints are left out for a function that does not take them yet."""
    if _takes(fn, "hints", default=True):
        return fn(url, deadline=deadline, hints=hints)
    return fn(url, deadline=deadline)


def _check(research):
    from app.check.receipts import check
    return check(research)


def _disconfirm(research, receipts, deadline=None):
    from app.check.disconfirm import disconfirm
    return disconfirm(research, receipts, deadline=deadline)


def disconfirmation(research, receipts, deadline=None):
    """-> (research, receipts) with research["disconfirmation"] set to the record or to {"skipped": why}. Never raises."""
    if os.environ.get("DISCONFIRM", "1").strip() == "0":
        research["disconfirmation"] = {"skipped": "DISCONFIRM=0"}
        return research, receipts
    before = (copy.deepcopy(research), copy.deepcopy(receipts))
    t0 = time.time()
    try:
        out = _disconfirm(research, receipts, deadline=deadline)
        if not (isinstance(out, tuple) and len(out) == 3 and isinstance(out[0], dict) and isinstance(out[1], list)
                and isinstance(out[2], dict)):
            raise TypeError(f"disconfirm returned {type(out).__name__}, not (research, receipts, record)")
        research, receipts, record = out
    except ModuleNotFoundError as e:
        research, receipts = before
        why = "not available (no app.check.disconfirm)" if e.name == "app.check.disconfirm" else f"{type(e).__name__}: {e}"
        record = {"skipped": why}
    except Exception as e:  # noqa: BLE001 (a failed stretch step never costs the verdict)
        traceback.print_exc()
        research, receipts = before
        record = {"skipped": f"{type(e).__name__}: {e}"}
    record.setdefault("seconds", round(time.time() - t0, 1))
    research["disconfirmation"] = record
    return research, receipts


STEPS = ("research", "check", "disconfirm", "judge", "audit")
SINGLE_VIEW_BELOW = 300  # seconds left at judge time under which the judge runs one view (a view takes 80-110 s)


def judge_views(deadline, now=None):
    """-> 1 when the deadline leaves under SINGLE_VIEW_BELOW s and the judge would run more than one view, else None
    (the judge's own default)."""
    if deadline is None or deadline - (time.time() if now is None else now) >= SINGLE_VIEW_BELOW:
        return None
    try:
        n = int(os.environ.get("JUDGE_VIEWS") or 3)
    except ValueError:
        n = 3
    return 1 if n > 1 else None


def _audit(verdict, receipts):
    from app.check.audit import audit
    return audit(verdict, receipts)


def self_audit(verdict, receipts):
    """-> the audited verdict, or the judge's verdict unchanged with audit {"skipped": why}. Never raises."""
    if os.environ.get("AUDIT", "1").strip() == "0":
        verdict["audit"] = {"skipped": "AUDIT=0"}
        return verdict
    before = copy.deepcopy(verdict)
    try:
        out = _audit(verdict, receipts)
        if not isinstance(out, dict) or out.get("overall") not in ledger.COLORS:
            raise TypeError(f"audit returned {type(out).__name__} without a color, not a verdict")
        out.setdefault("audit", {})
        return out
    except ModuleNotFoundError as e:
        if e.name != "app.check.audit":
            traceback.print_exc()
        why = "not available (no app.check.audit)" if e.name == "app.check.audit" else f"{type(e).__name__}: {e}"
    except Exception as e:  # noqa: BLE001 (a failed self-audit never costs the verdict)
        traceback.print_exc()
        why = f"{type(e).__name__}: {e}"
    before["audit"] = {"skipped": why}
    if not why.startswith("not available"):
        before["guards"] = list(before.get("guards") or []) + [f"audit failed, verdict left as judged: {why}"]
    return before


def _stats(research, receipts, steps=None, verdict=None):
    st = (research or {}).get("stats") or {}
    return {"tool_calls": st.get("tool_calls", 0), "llm_calls": st.get("llm_calls", 0),
            "capped": bool(st.get("capped")), "receipts_kept": sum(1 for r in receipts or [] if r.get("kept")),
            "receipts_rejected": sum(1 for r in receipts or [] if not r.get("kept")),
            "disconfirmation": trace_md.disconfirmation_summary(research),
            "audit": trace_md.audit_summary(verdict or {}), "steps": dict(steps or dict.fromkeys(STEPS))}


def _judge(research, receipts, views=None):
    """views=None: the judge's default (JUDGE_VIEWS). views=1 near the cap, logged in the verdict's guards."""
    from app.judge.judge import judge
    if views is None:
        return judge(research, receipts)
    if _takes(judge, "views"):
        v, note = dict(judge(research, receipts, views=views)), "judge: single view (cap)"
    else:
        v, note = dict(judge(research, receipts)), "judge: single view wanted (cap), but this judge takes no views arg"
    v["guards"] = list(v.get("guards") or []) + [note]
    return v


def _dump(run_dir, name, obj):
    path = os.path.join(run_dir, name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if isinstance(obj, str):
            f.write(obj)
        else:
            json.dump(obj, f, ensure_ascii=False, indent=1, default=str)
    os.replace(tmp, path)
    return path


def _reasons(v):
    out = []
    for b in ("red_flags", "yellow_flags", "green_flags"):
        out += [{"text": f.get("text", ""), "url": f.get("url", ""), "quote": f.get("quote", "")}
                for f in v.get(b) or [] if f.get("url")]
    return out


def error_verdict(url, error, research=None):
    r = research or ledger.new_research(url)
    ident = r.get("identity") or {}
    reasoning = f"The research did not complete ({error}); missing evidence is no signal."
    return {"url": url, "product": ident.get("product", ""), "vendor": ident.get("vendor", ""),
            "parent": ident.get("parent", ""), "former_names": ident.get("former_names", []),
            "overall": "Yellow", "call": "Yellow", "reliability": "low",
            "reliability_why": "The run failed before a verdict; nothing here is evidence either way.",
            "green_flags": [], "yellow_flags": [], "red_flags": [], "rejected": [], "reasoning": reasoning,
            "blind_spots": ["The whole matrix: the run did not complete."],
            "buyer_next_step": "Re-run the check, or ask the vendor the questions below before a shortlist.",
            "vendor_questions": ["Who is the legal entity behind the product, and who owns it?",
                                 "Which recent releases and paying customers can you name?",
                                 "Where is customer data hosted, and under which certifications?"],
            "would_flip": "A completed research run.",
            "coverage": {k: (row or {}).get("status", "open") for k, row in (r.get("matrix") or {}).items()},
            "sources_failed": r.get("sources_failed", []), "guards": [],
            "reasons": [{"text": reasoning, "url": url, "quote": ""}], "error": str(error)}


def check_vendor(url, run_dir, deadline=None, research_fn=None, hints=None):
    t0 = time.time()
    os.makedirs(run_dir, exist_ok=True)
    run_id = os.path.basename(os.path.dirname(os.path.normpath(run_dir))) or os.environ.get("RUN_ID", "")
    trace_path = os.path.join(run_dir, "trace.md")
    research, receipts, steps = None, [], dict.fromkeys(STEPS)

    def timed(step, fn, *a, **kw):
        t = time.time()
        try:
            return fn(*a, **kw)
        finally:
            steps[step] = round(time.time() - t, 1)
    try:
        research = timed("research", _call, research_fn or _research, url, deadline, hints)
        _dump(run_dir, "identity.json", research.get("identity") or {})
        _dump(run_dir, "research.json", research)
        receipts = timed("check", _check, research)
        _dump(run_dir, "receipts.json", receipts)
        research, receipts = timed("disconfirm", disconfirmation, research, receipts, deadline)
        _dump(run_dir, "research.json", research)
        _dump(run_dir, "receipts.json", receipts)
        views = judge_views(deadline)
        v = dict(timed("judge", _judge, research, receipts, **({"views": views} if views else {})))
        ident = research.get("identity") or {}
        for k in ("product", "vendor", "parent", "former_names"):
            v.setdefault(k, ident.get(k, [] if k == "former_names" else ""))
        v.setdefault("url", url)
        v.setdefault("coverage", {k: (row or {}).get("status", "open")
                                  for k, row in (research.get("matrix") or {}).items()})
        v.setdefault("sources_failed", research.get("sources_failed", []))
        v = timed("audit", self_audit, v, receipts)
        if not v.get("reasons"):
            v["reasons"] = _reasons(v)
        v["error"] = v.get("error") or None  # a judge that failed its model call says so; keep it
    except Exception as e:  # noqa: BLE001 (any failure is an error verdict, never a crash of the batch)
        err = f"{type(e).__name__}: {e}"
        traceback.print_exc()
        v = error_verdict(url, err, research)
    v["call"] = v.get("overall")
    if (hints or {}).get("id"):
        v["id"] = hints["id"]
    v.setdefault("model", model.model_id())
    v.update({"trace": trace_path, "run_id": run_id, "seconds": round(time.time() - t0, 1), "mode": model.mode(),
              "stats": _stats(research, receipts, steps, v)})
    try:
        _dump(run_dir, "trace.md", trace_md.render(research or ledger.new_research(url), receipts, v))
    except Exception as e:  # noqa: BLE001 (a broken trace must not lose the verdict)
        v.setdefault("guards", []).append(f"trace render failed: {type(e).__name__}: {e}")
    _dump(run_dir, "verdict.json", v)
    return v
