"""One raw Adaption call, kept whole (status, headers, body), to see whether anything names the serving provider.
A planning probe: excluded from all estimates. Run on the lab: python3 scripts/probe_raw.py --env-file .env

Saves runs/probe-provider-headers/raw.json (gitignored) and prints only header names, the body's top-level keys,
and any header or body field whose name suggests a provider, host or upstream. The key is never printed."""
import argparse
import json
import re
import urllib.request
from pathlib import Path

from outbreak.llm import ADAPTION_URL, load_env

HINT = re.compile(r"provider|upstream|backend|served|host|via|server|region|route|deploy|origin", re.I)


def hints(obj, path=""):
    """Every (path, value) in a JSON object whose key matches HINT; values cut to 200 characters."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            if HINT.search(k):
                out.append((p, json.dumps(v)[:200]))
            out += hints(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += hints(v, f"{path}[{i}]")
    return out


def main(argv=None):
    a = argparse.ArgumentParser()
    a.add_argument("--env-file", required=True)
    a.add_argument("--model", default="gemma-4-26b-a4b-it")
    a = a.parse_args(argv)
    key = load_env(a.env_file)["ADAPTION_API_KEY"]
    body = json.dumps({"model": a.model, "instructions": "Reply with one word.", "input": "Say ok.",
                       "temperature": 0.7, "max_output_tokens": 16, "store": False}).encode()
    req = urllib.request.Request(ADAPTION_URL, body, {"Content-Type": "application/json",
                                                      "Authorization": f"Bearer {key}"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        status, headers, raw = r.status, dict(r.headers.items()), r.read().decode()
    d = json.loads(raw)
    out = Path("runs/probe-provider-headers")
    out.mkdir(parents=True, exist_ok=True)
    (out / "raw.json").write_text(json.dumps({"status": status, "headers": headers, "body": d}, indent=1))
    print("status", status)
    print("header names:", sorted(headers))
    print("body keys:", sorted(d))
    print("model:", d.get("model"))
    for k, v in headers.items():
        if HINT.search(k):
            print(f"header {k}: {v[:200]}")
    for p, v in hints(d):
        print(f"body {p}: {v}")
    print("saved", out / "raw.json")


if __name__ == "__main__":
    main()
