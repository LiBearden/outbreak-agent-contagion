"""Drive the Outbreak results seminar on the lab's seminar protocol (research-stack adapters/seminar.py, ADR-0009).
Run on the lab from the research-stack worktree that has blind judging:

  cd ~/research-stack-blind && uv run --frozen --offline python ~/outbreak/scripts/seminar_seat.py <cmd> ... --root ~/research-stack/runs

  open [--question-file PATH] [--roles a,b,c]   open the seminar (default question seminar/question.md, roles claude, gpt, kimi, conductor; packet = README, PREREG, SPEC, tables)
  prompt RUN --out FILE                 the one take prompt every model seat gets: question plus the packet bytes inline
  commit RUN --role R --take FILE --runtime M   seal a take file and file its commitment
  recommit RUN --role R --from-run OLD  carry a take sealed for OLD over unchanged (same question and packet only)
  kimi RUN --prompt FILE --out FILE --env-file PATH --env-key NAME   the open-weights seat via DeepInfra
  reveal RUN                            reveal every role from its sealed copy (refused until all have committed)
  bundle RUN --out FILE                 blind bundle (labels W, X, ...) of revealed takes, written to FILE only; mapping sealed in .sealed/<run>-panel-blind.json
  panel-unblind RUN                     print the sealed bundle mapping and its commitment (only after ballots are revealed)
  openrouter --model M --prompt FILE --out FILE --env-file PATH --env-key NAME   any model via OpenRouter

Sealed copies live in <root>/.sealed/<run>-<role>.bin, the same place the seminar CLI keeps a human take."""
import argparse
import json
import re
import secrets
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from adapters.providers.deepinfra import DeepInfraProvider
from adapters.seminar import SCRUB, Seminar, _name_re, seal
from core.store import Store, canon, digest

OUTBREAK = Path.home() / "outbreak"
# "li" ceded the PI seat to the conducting agent on 2026-11-08, so the second seminar seats "conductor";
# the li role is a human record in adapters/seminar.py, and a model take may not be filed under it.
ROLES = ["claude", "gpt", "kimi", "conductor"]
KIMI = "moonshotai/Kimi-K3.1-Pro"
KIMI_PRICE = (1.90, 9.50)  # USD per 1M input, output tokens (DeepInfra public page, 2026-11-08)
ARGON_PRICE = (2.00, 10.00)  # USD per 1M input, output tokens (OpenRouter public models list, 2026-11-08)


def packet_paths():
    tables = sorted((OUTBREAK / "analysis" / "study-8327244aad0a").glob("*.csv"))
    return [OUTBREAK / "README.md", OUTBREAK / "PREREG.md", OUTBREAK / "SPEC.md", *tables]


def sealed_path(root, run, role):
    return Path(root) / ".sealed" / f"{run}-{role}.bin"


def parse_env(path, key):
    """KEY=VALUE lines parsed as text; the file is never sourced and the value never printed."""
    for line in Path(path).read_text().splitlines():
        k, _, v = line.strip().partition("=")
        if k.removeprefix("export ").strip() == key:
            return v.strip().strip("'\"")
    raise SystemExit(f"{key} not found in {path}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["open", "prompt", "commit", "recommit", "kimi", "reveal", "bundle", "panel-unblind", "openrouter"])
    ap.add_argument("--from-run")
    ap.add_argument("run", nargs="?")
    ap.add_argument("--root", required=True)
    ap.add_argument("--out"), ap.add_argument("--role"), ap.add_argument("--take"), ap.add_argument("--runtime")
    ap.add_argument("--model"), ap.add_argument("--question-file"), ap.add_argument("--roles")
    ap.add_argument("--prompt"), ap.add_argument("--env-file"), ap.add_argument("--env-key")
    ap.add_argument("--max-tokens", type=int, default=12000)  # Kimi's hidden reasoning counts against this
    a = ap.parse_args(argv)
    store = Store(a.root, sink=None)  # local only: Braintrust is over its plan limit
    if a.cmd == "open":
        code = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True).stdout.strip())
        sem = Seminar.open(store, question=Path(a.question_file or OUTBREAK / "seminar" / "question.md").read_text(),
                           roles=a.roles.split(",") if a.roles else ROLES,
                           code_id=code, code_dirty=dirty, context=packet_paths())
        print("opened", sem.run_id, "packet files", len(sem.packet), "digest", sem.packet_digest[:16])
        return
    if a.cmd == "openrouter":
        if Path(a.out).exists():
            raise SystemExit(f"{a.out} exists; not overwritten")
        body = {"model": a.model, "messages": [{"role": "user", "content": Path(a.prompt).read_text()}],
                "max_tokens": a.max_tokens, "temperature": 0.7, "usage": {"include": True}}
        req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", json.dumps(body).encode(), {
            "Authorization": f"Bearer {parse_env(a.env_file, a.env_key)}", "Content-Type": "application/json"})
        try:
            data = json.load(urllib.request.urlopen(req, timeout=900))
        except urllib.error.HTTPError as e:
            raise SystemExit(f"HTTP {e.code}: {e.read().decode(errors='replace')}")
        text = data["choices"][0]["message"]["content"]
        Path(a.out).write_text(text)
        u = data.get("usage", {})
        cost = u["cost"] if "cost" in u else sum(u.get(k, 0) * p / 1e6 for k, p in
                                                 zip(("prompt_tokens", "completion_tokens"), ARGON_PRICE))
        reasoning = (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
        print("openrouter resolved", data["model"], "finish", data["choices"][0].get("finish_reason"),
              "prompt_tokens", u.get("prompt_tokens"), "completion_tokens", u.get("completion_tokens"),
              *(["reasoning_tokens", reasoning] if reasoning is not None else []),
              f"cost_usd {cost:.4f}", "wrote", a.out, len(text), "chars")
        return
    sem = Seminar(store, a.run)
    if a.cmd == "prompt":
        body = "\n\n".join(f"===== {p} =====\n{t}" for p, t in sem.packet_texts())
        Path(a.out).write_text(f"{sem.question}\n\nThe packet follows, every file in full.\n\n{body}\n")
        print("wrote", a.out, len(Path(a.out).read_text()), "chars")
    elif a.cmd == "commit":
        text = Path(a.take).read_text().strip()
        m = re.search(r"Confidence:\s*([0-9.]+)\s*$", text)
        sealed = seal(text, confidence=m.group(1) if m else "", runtime=a.runtime)
        keep = sealed_path(a.root, a.run, a.role)
        keep.parent.mkdir(parents=True, exist_ok=True)
        keep.write_bytes(sealed)
        print("committed", sem.commit(a.role, sealed, runtime=a.runtime, packet=sem.packet_digest), "sealed copy", keep)
    elif a.cmd == "recommit":
        # Carry a take sealed for an earlier seminar over byte for byte; only valid if that seminar asked
        # the same question over the same packet, so the take was formed on identical context.
        old = Seminar(store, a.from_run)
        if (old.question, old.packet_digest) != (sem.question, sem.packet_digest):
            raise SystemExit(f"{a.from_run} differs from {a.run} in question or packet; not carried over")
        sealed = sealed_path(a.root, a.from_run, a.role).read_bytes()
        runtime = json.loads(sealed).get("runtime")
        keep = sealed_path(a.root, a.run, a.role)
        keep.write_bytes(sealed)
        print("recommitted", a.role, "from", a.from_run, sem.commit(a.role, sealed, runtime=runtime,
                                                                    packet=sem.packet_digest))
    elif a.cmd == "kimi":
        prov = DeepInfraProvider(api_key=parse_env(a.env_file, a.env_key), timeout=900)
        c = prov.complete(KIMI, Path(a.prompt).read_text(), max_tokens=a.max_tokens, temperature=0.7)
        Path(a.out).write_text(c.text)
        cost = sum(c.counts.get(k, 0) * p / 1e6 for k, p in zip(("input", "output"), KIMI_PRICE))
        print("kimi resolved", c.resolved_model, "finish", c.termination, c.counts, f"cost_usd {cost:.4f}",
              "wrote", a.out, len(c.text), "chars")
    elif a.cmd == "bundle":
        keep = Path(a.root) / ".sealed" / f"{a.run}-panel-blind.json"
        if keep.exists():
            raise SystemExit(f"{keep} exists; a bundle is not redrawn")
        takes = sem.takes()
        order = list(sem.roles)
        secrets.SystemRandom().shuffle(order)
        labels = [chr(ord("W") + i) for i in range(len(order))]
        scrub = _name_re([*SCRUB, *sem._names()])
        parts = []
        for l, r in zip(labels, order):
            t = scrub.sub("[model]", takes[r]["take"])
            assert not scrub.search(t), f"scrub missed a name in take {l}"
            parts.append(f"===== Take {l} =====\n{t}\nStated confidence: {takes[r]['confidence']}\n")
        mapping, nonce = dict(zip(labels, order)), secrets.token_hex(16)
        commitment = digest(canon(mapping) + nonce.encode())
        kept = canon({"mapping": mapping, "nonce": nonce})
        k = json.loads(kept)
        assert digest(canon(k["mapping"]) + k["nonce"].encode()) == commitment
        keep.parent.mkdir(parents=True, exist_ok=True)
        keep.write_bytes(kept)
        Path(a.out).write_text("\n".join(parts))
        print("bundle commitment", commitment, "wrote", a.out)
    elif a.cmd == "panel-unblind":
        k = json.loads((Path(a.root) / ".sealed" / f"{a.run}-panel-blind.json").read_bytes())
        print("commitment", digest(canon(k["mapping"]) + k["nonce"].encode()))
        print(json.dumps(k["mapping"]))
    elif a.cmd == "reveal":
        for role in sem.roles:
            print("revealed", role, sem.reveal(role, sealed_path(a.root, a.run, role).read_bytes()))


if __name__ == "__main__":
    main()
