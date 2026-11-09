"""Launch the preregistered study: `scripts/launch.py --prereg <full sha> [--check]`.
Reads the prereg-params block from `git show <sha>:PREREG.md` (never the working tree), then runs the experiments one
at a time as `python3 -m outbreak.runner` subprocesses with fixed run ids, so a re-run resumes. Stops at the first
non-zero exit. --check runs only each experiment's freeze guard (no model call). Secrets never reach a command line."""
import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from outbreak import guards, runner  # noqa: E402

ORDER = ["E1", "E1S", "E1p", "E2R", "E2", "E2N", "E3", "E4"]  # E3 only if the block has E3 lines
FLAGS = ("provider", "model", "rounds", "max_tokens", "temperature", "seed", "retries", "rpm", "topologies", "contents",
         "warned_counts", "reps", "policies", "scenarios", "max_total_tokens")


def fenced(text, tag):
    return re.search(rf"^```{tag}\n(.*?)^```", text, re.M | re.S).group(1)


def block(text):
    """{scope or None: {key: value}} from the prereg-params block of a PREREG.md text."""
    out = {}
    for line in fenced(text, "prereg-params").splitlines():
        k, _, v = line.partition("=")
        scope, dot, name = k.partition(".")
        out.setdefault(scope if dot else None, {})[name if dot else scope] = v.partition(" #")[0].strip()
    return out


def argv(exp, b, **over):
    """The runner arguments (after the experiment name) the block describes for one experiment."""
    vals = {**b[None], **b.get(exp, {}), **over}
    return [exp, *[x for k in FLAGS if vals.get(k, "TBD") != "TBD" for x in (f"--{k.replace('_', '-')}", vals[k])]]


def cmd(exp, b, sha, extra=()):
    return [sys.executable, "-m", "outbreak.runner", *argv(exp, b, provider="adaption"), "--prereg", sha,
            "--run-id", f"study-{exp}-{sha[:12]}", *extra]


def check_one(exp, b, sha, extra=(), guard=guards.check_prereg):
    """The freeze guard exactly as runner.execute calls it for a fresh run. Returns (ok, message)."""
    try:
        a = runner.parse_args(cmd(exp, b, sha, extra)[3:])
        guard(a.prereg, runner.build_params(a), runner.plan(a)[3], a.prereg_file, runner.ROOT)
        return True, ""
    except (guards.GuardError, ValueError, OSError) as e:
        return False, str(e)


def load_block(sha):
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        sys.exit(f"--prereg must be a full 40-character commit sha, not {sha!r}")
    rc, txt = guards.run_git(ROOT, "show", f"{sha}:PREREG.md")
    if rc:
        sys.exit(f"cannot read PREREG.md at {sha}")
    b = block(txt)
    return b, [e for e in ORDER if e != "E3" or "E3" in b]


def log(row):
    (ROOT / "runs").mkdir(exist_ok=True)
    row = {"ts": datetime.now(timezone.utc).isoformat(), **row}
    with open(ROOT / "runs" / "launch.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def launch(sha, b, exps, extra=(), run=subprocess.Popen):
    """Returns 0, or the stopping experiment's exit code (3 if it exited 0 but reported a stop, e.g. budget)."""
    for exp in exps:
        c = cmd(exp, b, sha, extra)
        log({"event": "start", "experiment": exp, "run_id": f"study-{exp}-{sha[:12]}", "prereg": sha})
        p = run(c, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out = []
        for line in p.stdout:
            print(line, end="", flush=True)
            out.append(line)
        rc = p.wait()
        stop = next((m.group(1) for l in reversed(out) if (m := re.search(r"\bstop=(.+)$", l.strip()))), "None")
        if rc == 0 and stop != "None":  # the runner exits 0 on a budget stop, breaker or credit stop; the study must not go on
            rc = 3
        log({"event": "finish", "experiment": exp, "exit_code": rc, "stop": stop})
        if rc:
            print(f"STOPPED at {exp}: exit code {rc} (stop={stop})", file=sys.stderr)
            return rc
    return 0


def main(args=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prereg", required=True, help="full commit sha of the frozen PREREG.md")
    ap.add_argument("--check", action="store_true", help="run each experiment's freeze guard only; no model call")
    a = ap.parse_args(args)
    b, exps = load_block(a.prereg)
    if not a.check:
        return launch(a.prereg, b, exps)
    bad = 0
    for exp in exps:
        ok, msg = check_one(exp, b, a.prereg)
        print(f"{'PASS' if ok else 'FAIL'} {exp}" + (f": {msg}" if msg else ""))
        bad += not ok
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
