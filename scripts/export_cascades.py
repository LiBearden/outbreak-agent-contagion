"""Export the structure (indices, rounds, booleans only; no message text) of every E2 cascade to JSON."""
import argparse
import datetime
import glob
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.getcwd())  # run from the repo root
from outbreak import analyze  # noqa: E402


def export(run_dirs):
    runs = [analyze.load_run(d) for d in run_dirs]
    live = [r for r in runs if r["included"] and r["manifest"].get("experiment") == "E2"]
    out, incomplete = [], []
    for run in live:
        man, rounds_n = run["manifest"], run["rounds"]
        by = defaultdict(list)
        for r in run["rows"]:  # dict keeps log order of first appearance
            by[r["trial_id"]].append(r)
        for tid, rs in by.items():
            rs.sort(key=lambda r: r["round"])
            r0 = rs[0]
            seed, topo, gid = r0["seed_agent"], r0["topology"], r0["graph_id"]
            inf, rounds, fresh = {seed}, [[seed]], defaultdict(int)
            for r in rs:  # same adoption rule as analyze.build_cascade (best case, not worst)
                if r["agent"] in inf or not r["adopted"]:
                    continue
                inf.add(r["agent"])
                while len(rounds) <= r["round"]:
                    rounds.append([])
                rounds[r["round"]].append(r["agent"])
                fresh[r["round"]] += 1
            c = analyze.build_cascade(rs, rounds_n)  # reuse analysis' final_size and completeness
            assert c["final_size"] == len(inf)
            if not c["complete"]:
                incomplete.append(tid)
            out.append({"id": f"{run['run_id']}/{tid}", "topology": topo, "seed_position": r0["seed_position"],
                        "graph": gid if topo == "mesh" else None,
                        "edges": [] if topo == "shared" else man["graphs"][gid],
                        "seed": seed, "hub": 0 if topo == "star" else None,
                        "rounds": rounds, "final_size": len(inf), "complete": c["complete"]})
    errors = [e for r in runs for e in r["errors"]]
    src = {"prereg_sha": next((r["manifest"].get("prereg_sha") for r in live), None),
           "run_ids": [r["run_id"] for r in live],
           "generated": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    return {"source": src, "cascades": out}, incomplete, errors


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True)
    ap.add_argument("--runs", default="runs")
    a = ap.parse_args(argv)
    res, incomplete, errors = export(sorted(os.path.dirname(p) for p in glob.glob(os.path.join(a.runs, "*/manifest.json"))))
    with open(a.out, "w") as f:
        json.dump(res, f, separators=(",", ":"))
    print(f"wrote {a.out}: {len(res['cascades'])} cascades, {len(incomplete)} incomplete, {len(errors)} log errors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
