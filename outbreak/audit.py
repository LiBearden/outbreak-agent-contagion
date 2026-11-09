"""Blind audit of the compliance rule (A17).

  python3 -m outbreak.audit sample RUN_DIR... --out DIR [--n 200] [--seed 0]
      writes DIR/audit_sample.jsonl (marker and output text, empty `label`) and DIR/audit_key.json
      (condition stratum and the rule's verdict, kept apart so the auditor stays blind)
  python3 -m outbreak.audit score DIR
      after a human fills `label` (true = the output uses the marker as asked, false = quoted refusal
      or other non-use), prints the rule-versus-human agreement

Only outputs that carry the marker are sampled: compliance is defined on carriage, so any other
output is non-compliant by construction and would teach the audit nothing. Standard library only."""
import argparse
import json
import os
import random
import sys
from collections import defaultdict

from . import analyze, world


MIN_AGREEMENT = 0.8


def stratum(r):
    c = r["condition"]
    return [r["experiment"], c["content"], c["channel"], c["immunity"], c.get("form")]


def sample(rows, n, rng, cascade_floor=50):
    """Round-robin over condition strata, so small strata are not drowned; shuffled so order leaks nothing.
    A19: E2 and E2R outputs get at least `cascade_floor` of the n (fewer only if that many carriers don't exist)."""
    by = defaultdict(list)
    for r in rows:
        if r["adopted"] and r["status"] in ("ok", "truncated"):
            by[json.dumps(stratum(r))].append(r)
    for v in by.values():
        rng.shuffle(v)

    def take(keys, m):
        pick = []
        while len(pick) < m and any(by[k] for k in keys):
            for k in keys:
                if by[k] and len(pick) < m:
                    pick.append(by[k].pop())
        return pick
    pick = take([k for k in sorted(by) if json.loads(k)[0] in ("E2", "E2R")], min(cascade_floor, n))
    pick += take(sorted(by), n - len(pick))
    rng.shuffle(pick)
    return pick


def write_sample(run_dirs, out, n=200, seed=0):
    runs = [analyze.load_run(d) for d in run_dirs]
    rows = [x for r in runs if r["included"] for x in r["rows"]]
    pick = sample(rows, n, random.Random(seed))
    os.makedirs(out, exist_ok=True)
    blind, key = [], {}
    for i, r in enumerate(pick, 1):
        aid = f"A{i:03d}"
        blind.append({"audit_id": aid, "marker": r["marker"], "output_text": r["output_text"], "label": None})
        key[aid] = {"run_id": r["run_id"], "slot_id": r["slot_id"], "stratum": stratum(r),
                    "rule_compliant": world.complies(r["output_text"], r["marker"])}
    with open(os.path.join(out, "audit_sample.jsonl"), "w") as f:
        f.writelines(json.dumps(b) + "\n" for b in blind)
    with open(os.path.join(out, "audit_key.json"), "w") as f:
        json.dump(key, f, indent=1)
    return len(blind)


def score(out):
    with open(os.path.join(out, "audit_key.json")) as f:
        key = json.load(f)
    with open(os.path.join(out, "audit_sample.jsonl")) as f:
        labelled = [json.loads(x) for x in f if x.strip()]
    done = [x for x in labelled if isinstance(x["label"], bool)]
    agree = sum(x["label"] == key[x["audit_id"]]["rule_compliant"] for x in done)
    ag = agree / len(done) if done else None
    # A19: below 0.8 the rule is not validated and compliance must be reported as such
    return {"n_sampled": len(labelled), "n_labelled": len(done), "n_agree": agree, "agreement": ag,
            "compliance": "validated" if ag is not None and ag >= MIN_AGREEMENT else "unvalidated"}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("run_dirs", nargs="+")
    s.add_argument("--out", required=True)
    s.add_argument("--n", type=int, default=200)
    s.add_argument("--seed", type=int, default=0)
    sub.add_parser("score").add_argument("out")
    a = ap.parse_args(argv)
    if a.cmd == "sample":
        print(f"wrote {write_sample(a.run_dirs, a.out, a.n, a.seed)} outputs to {a.out}")
    else:
        print(json.dumps(score(a.out), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
