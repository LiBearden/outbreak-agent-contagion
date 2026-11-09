"""The only provider-specific module: one call function plus a seeded fake provider."""
import json
import random
import re
import time
import urllib.error
import urllib.request

ADAPTION_URL = "https://api.prod.adaptionlabs.ai/api/v1/responses"
TIMEOUT = 60
PENDING = ("in_progress", "queued", "cancelled")
TRANSIENT = (TimeoutError, ConnectionError, urllib.error.URLError)
CODE_RE = re.compile(r"[a-z_]{1,40}")  # server-supplied labels are logged only if they look like an enum

MARKER_RE = re.compile(r"OB-[0-9a-f]{8}")
FILLER = [
    "Progress is on track and there are no blockers on my item.",
    "I finished the review and will start the next step tomorrow.",
    "Waiting on a reply from the customer, otherwise nothing to flag.",
    "Drafted the summary and queued it for the weekly sync.",
]


def load_env(path):
    """Parse KEY=VALUE lines as text; the file is never sourced or executed."""
    env = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.removeprefix("export ").strip()] = v.strip().strip("'\"")
    return env


def preflight(provider, env=None):
    """Raise before any call is planned if the provider cannot be used."""
    if provider == "fake":
        return
    if provider != "adaption":
        raise ValueError(f"unknown provider {provider!r}")
    if not (env or {}).get("ADAPTION_API_KEY"):
        raise ValueError("ADAPTION_API_KEY not found in the env file")


NOTE_RE = re.compile(r"^Note from (a\d+):\n(.*?)(?=\n\nNote from a\d+:\n|\n\nReply with|\Z)", re.M | re.S)


def _fake_sentinel(rng, prompt, s):
    """s = (tp, fp, named, junk): flag an author with its note's marker at tp, any author at fp,
    another agent a note names at `named`; with prob `junk` answer in prose (unparseable)."""
    tp, fp, named, junk = s
    flagged = set()
    for author, text in NOTE_RE.findall(prompt):
        if MARKER_RE.search(text) and rng.random() < tp:
            flagged.add(author)
        if rng.random() < fp:
            flagged.add(author)
        for other in sorted(set(re.findall(r"\ba\d+\b", text)) - {author}):
            if rng.random() < named:
                flagged.add(other)
    if rng.random() < junk:
        return "I am not sure."
    text = json.dumps(sorted(flagged))
    # A22: a quarter of replies come fenced like the real model's; drawn last so the flags themselves are unchanged
    return f"```json\n{text}\n```" if rng.random() < 0.25 else text


def _fake(model, system, prompt, max_tokens, seed_key, p, persist, sentinel=(0.5, 0.02, 0.3, 0.0)):
    rng = random.Random(seed_key)
    if system.startswith("You are the sentinel"):
        text = _fake_sentinel(rng, prompt, sentinel)
        n_in, n_out = len(system + prompt) // 4, max(1, len(text) // 4)
        return text, {"input": n_in, "output": n_out, "total": n_in + n_out, "cached": 0, "reasoning": None}, model, "ok"
    text = "Handoff: " + rng.choice(FILLER)
    prev, _, got = prompt.partition("\n\nReceived:\n")
    m = MARKER_RE.search(prompt)
    if m:  # independent exposures at p, plus the agent's own marked note kept with prob `persist`
        keep = persist if m.group(0) in prev else 0.0
        if rng.random() < 1 - (1 - keep) * (1 - p) ** got.count(m.group(0)):
            text += f" Ref {m.group(0)}."
    status = "ok"
    if len(text) > max_tokens * 4:
        text, status = text[: max_tokens * 4], "truncated"
    n_in, n_out = len(system + prompt) // 4, max(1, len(text) // 4)
    usage = {"input": n_in, "output": n_out, "total": n_in + n_out, "cached": 0, "reasoning": None}
    return text, usage, model, status


def _label(prefix, v):
    return prefix + v if isinstance(v, str) and CODE_RE.fullmatch(v) else prefix.rstrip("_:")


def _usage(d):
    u = d.get("usage") if isinstance(d.get("usage"), dict) else {}
    sub = lambda k, f: (u.get(k) if isinstance(u.get(k), dict) else {}).get(f)  # absent stays null, never 0
    return {"input": u.get("input_tokens"), "output": u.get("output_tokens"), "total": u.get("total_tokens"),
            "cached": sub("input_tokens_details", "cached_tokens"),
            "reasoning": sub("output_tokens_details", "reasoning_tokens")}


def _parse(d):
    """Responses JSON -> (text, status, error_class). Only enum-like labels leave this function."""
    parts = [c for o in d.get("output") or [] if isinstance(o, dict) and o.get("type") == "message"
             for c in o.get("content") or [] if isinstance(c, dict)]
    text = d.get("output_text")
    if not isinstance(text, str):
        text = "".join(c.get("text", "") for c in parts if c.get("type") == "output_text") or None
    refusal = next((c.get("refusal") for c in parts if c.get("type") == "refusal"), None)
    if d.get("status") == "failed":
        err = d.get("error")
        return "", "error", _label("failed_", err.get("code") if isinstance(err, dict) else None)
    if d.get("status") in PENDING:  # A19: the text so far is not the model's answer
        return "", "error", _label("status_", d["status"])
    if refusal is not None:
        return refusal if isinstance(refusal, str) else "", "refused", None
    if d.get("status") == "incomplete":
        det = d.get("incomplete_details")
        return text or "", "truncated", _label("incomplete_", det.get("reason") if isinstance(det, dict) else None)
    if text is None:
        return "", "error", "NoOutputText"
    if not text.strip():  # A19: an empty note is a failed call, never a clean non-adoption
        return "", "error", "EmptyText"
    return text, "ok", None


def _adaption(url, key, model, system, prompt, max_tokens, temperature, retries, sleep, pace):
    body = json.dumps({"model": model, "instructions": system, "input": prompt, "temperature": temperature,
                       "max_output_tokens": max_tokens, "store": False}).encode()
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    out = {"http_429": 0, "retryable": False}  # retryable: the failure was transient and outlived the retries (A19)
    for attempt in range(1, retries + 2):
        out["attempts"] = attempt
        pace()  # the rate limiter counts HTTP attempts, so retries cannot exceed --rpm
        try:
            req = urllib.request.Request(url, body, headers, method="POST")
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                d = json.load(r)
            if not isinstance(d, dict):
                raise ValueError
            out["text"], out["status"], out["error_class"] = _parse(d)
            out["model_returned"] = d.get("model") if isinstance(d.get("model"), str) else None
            out["usage"] = _usage(d)
            return out
        except urllib.error.HTTPError as e:
            code, ra = e.code, e.headers.get("retry-after")
            e.close()
            out["http_429"] += code == 429
            out["error_class"] = f"HTTP{code}"
            out["retryable"] = code in (408, 409, 429) or code >= 500
            if out["retryable"] and attempt <= retries:
                try:
                    wait = float(ra)
                except (TypeError, ValueError):
                    wait = 0
                sleep(wait if 0 < wait <= 60 else min(0.5 * 2 ** (attempt - 1), 8))
                continue
            return out
        except TRANSIENT as e:  # timeouts (also mid-body), refused or dropped connections
            out["error_class"], out["retryable"] = type(e).__name__, True
            if attempt <= retries:
                sleep(min(0.5 * 2 ** (attempt - 1), 8))
                continue
            return out
        except Exception as e:  # class name only: messages could carry a key
            out["error_class"] = type(e).__name__
            return out
    return out


def call_model(provider, model, system, prompt, *, max_tokens, temperature,
               seed_key="", fake_p=0.5, fake_persist=1.0, fake_s=(0.5, 0.02, 0.3, 0.0), env=None,
               base_url=ADAPTION_URL, retries=0, sleep=time.sleep, pace=lambda: None):
    """Never raises. Errors come back as status=error with only the exception class name.
    base_url exists for tests against a local server; the runner never passes it."""
    t0 = time.monotonic()
    out = {"text": "", "usage": {"input": None, "output": None, "total": None, "cached": None, "reasoning": None},
           "model_returned": None, "status": "error", "error_class": None, "retryable": False}
    try:
        preflight(provider, env)
        if provider == "fake":
            pace()
            out["text"], out["usage"], out["model_returned"], out["status"] = _fake(
                model, system, prompt, max_tokens, seed_key, fake_p, fake_persist, fake_s)
        else:
            out.update(_adaption(base_url, env["ADAPTION_API_KEY"], model, system, prompt,
                                 max_tokens, temperature, retries, sleep, pace))
    except Exception as e:
        out["error_class"] = type(e).__name__
    out["latency_ms"] = int((time.monotonic() - t0) * 1000)
    return out
