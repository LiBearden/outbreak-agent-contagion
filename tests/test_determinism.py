"""A fake run plus analysis under two PYTHONHASHSEED values must produce byte-identical results.json."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DRIVER = """
import sys
sys.path.insert(0, sys.argv[1])
from test_harness import go
rc, _, err = go(sys.argv[2], *sys.argv[3:], '--run-id', sys.argv[3], '--seed', '3')
assert rc == 0, err
"""


def pipeline(base, hashseed):
    env = {**os.environ, "PYTHONHASHSEED": str(hashseed)}
    runs = base / "runs"  # same path every time, so the recorded input dirs match
    shutil.rmtree(runs, ignore_errors=True)
    dirs = []
    for args in (["E1", "--reps", "3"], ["E1S", "--reps", "3"], ["E1p", "--reps", "3"],
                 ["E2R", "--rounds", "1", "--reps", "30", "--contents", "canary"],
                 ["E2", "--reps", "30", "--contents", "canary"],
                 ["E3", "--reps", "3", "--contents", "canary", "--topologies", "star", "--warned-counts", "2"]):
        subprocess.run([sys.executable, "-c", DRIVER, str(ROOT / "tests"), str(runs), *args],
                       cwd=ROOT, env=env, check=True, capture_output=True)
        dirs.append(runs / args[0])
    for d in dirs:  # wall-clock fields differ between any two runs; everything else must not
        man = json.loads((d / "manifest.json").read_text())
        man["created_ts"] = ""
        (d / "manifest.json").write_text(json.dumps(man, indent=1))
        lines = [dict(json.loads(x), ts="", latency_ms=0) for x in (d / "calls.jsonl").read_text().splitlines()]
        (d / "calls.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    out = base / f"out{hashseed}"
    subprocess.run([sys.executable, "-m", "outbreak.analyze", *map(str, dirs), "--out", str(out), "--boot", "50",
                    "--reps", "30", "--curve-n", "20", "--seed", "1"],
                   cwd=ROOT, env=env, check=True, capture_output=True)
    return (out / "results.json").read_bytes()


class Determinism(unittest.TestCase):
    def test_results_do_not_depend_on_hashseed(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = (pipeline(Path(d), s) for s in (1, 2))
        self.assertGreater(len(json.loads(a)["estimands"]["e2_summary"]["rows"]), 0)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
