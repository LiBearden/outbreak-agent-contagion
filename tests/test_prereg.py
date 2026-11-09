"""PREREG.md against the code: its parameter block passes the freeze guard (except where it says TBD), every design it
names has the counts and hash it states and is accepted by --dry-run, scripts/power.py plans the same designs, and
the file is still a draft. Nothing here reads a run or calls a provider."""
import contextlib
import importlib.util
import io
import re
import unittest
from pathlib import Path

from outbreak import guards, runner
from test_harness import SHA, fake_git

ROOT = Path(__file__).resolve().parent.parent
TEXT = (ROOT / "PREREG.md").read_text()
spec = importlib.util.spec_from_file_location("launch", ROOT / "scripts" / "launch.py")
launch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launch)  # the block-to-argv logic lives in the launch script


def fenced(tag):
    return launch.fenced(TEXT, tag)


def block():
    """{scope or None: {key: value}} from the prereg-params block."""
    return launch.block(TEXT)


def high_e3():
    """A20: E3's lines are documented for the high tier in their own block, not in prereg-params."""
    lines = [l.partition(".")[2] for l in fenced("high-e3").splitlines()]  # drop the `E3.` scope
    return {k: v for k, _, v in (l.partition("=") for l in lines)}


def argv(exp, **over):
    """The command line the block describes for one experiment; TBD values are left to the defaults."""
    b = block()
    b.setdefault(exp, high_e3() if exp == "E3" else {})
    return [*launch.argv(exp, b, **over), "--e4-notes", str(ROOT / "config" / "e4_notes.example.json")]


class Prereg(unittest.TestCase):
    def test_it_is_frozen_with_nothing_unfilled(self):
        self.assertRegex(TEXT, r"(?m)^\*\*Status: FROZEN")
        self.assertNotRegex(TEXT, r"(?m)^\*\*Status: DRAFT")
        self.assertNotIn("TBD", TEXT)
        b = block()
        self.assertEqual((b[None]["model"], b[None]["rpm"]), ("gemma-4-26b-a4b-it", "50.0"))
        self.assertEqual(b["E4"]["e4_notes_sha256"], "aee36dc9511c055204c42b944ab2869533d3d425d734b2f406bdbc35f3726da7")
        self.assertNotIn("E3", b)  # A20: the standard tier's block has no E3 lines
        std = {exp: kv for tier, exp, kv in self.tier_lines() if tier == "standard"}
        for e in (e for e in runner.EXPERIMENTS if e != "E3"):  # max_total_tokens = planned calls x 750 (scripts/power.py)
            self.assertEqual(int(b[e]["max_total_tokens"]), int(std[e]["slots"]) * 750, e)
        self.assertEqual(int(high_e3()["max_total_tokens"]), 9792 * 750)

    def test_the_block_passes_the_freeze_guard_for_every_experiment_but_its_tbd_values(self):
        for exp in (e for e in runner.EXPERIMENTS if e != "E3"):  # E3 is refused instead, below
            with self.subTest(exp=exp):
                a = runner.parse_args(argv(exp))
                params, sha = runner.build_params(a), runner.plan(a)[3]
                if exp == "E4":  # the real notes file is gitignored; the test runs on the example file, so use the block's hash
                    params = {**params, "e4_notes_sha256": block()["E4"]["e4_notes_sha256"]}
                filled = re.sub(r"^((?:\w+\.)?(\w+))=TBD$", lambda m: f"{m.group(1)}={params.get(m.group(2), 'TBD')}",
                                fenced("prereg-params"), flags=re.M)
                files = {"PREREG.md": f"**Status: FROZEN\n```prereg-params\n{filled}\n```\n", **dict.fromkeys(guards.REQUIRED, "x")}
                self.assertEqual(guards.check_prereg(SHA[:12], params, sha, "PREREG.md", ".", fake_git(files, files)), SHA)

    def test_e3_is_refused_under_the_standard_block_and_accepted_with_the_high_lines(self):
        a = runner.parse_args(argv("E3"))
        params, sha = runner.build_params(a), runner.plan(a)[3]
        std = fenced("prereg-params")
        filled = lambda t: re.sub(r"^((?:\w+\.)?(\w+))=TBD$", lambda m: f"{m.group(1)}={params.get(m.group(2), 'TBD')}", t, flags=re.M)
        for text, ok in ((std, False), (std + fenced("high-e3"), True)):
            files = {"PREREG.md": f"**Status: FROZEN\n```prereg-params\n{filled(text)}\n```\n", **dict.fromkeys(guards.REQUIRED, "x")}
            git = fake_git(files, files)
            if ok:
                self.assertEqual(guards.check_prereg(SHA[:12], params, sha, "PREREG.md", ".", git), SHA)
            else:
                with self.assertRaisesRegex(guards.GuardError, "has no E3 lines"):
                    guards.check_prereg(SHA[:12], params, sha, "PREREG.md", ".", git)

    def test_e2n_is_the_same_small_design_in_every_tier(self):
        e2n = {t: kv for t, e, kv in self.tier_lines() if e == "E2N"}
        self.assertEqual(sorted(e2n), ["high", "lean", "standard"])
        self.assertEqual({(kv["reps"], kv["slots"], kv["sha256"]) for kv in e2n.values()}, {(e2n["lean"]["reps"], e2n["lean"]["slots"], e2n["lean"]["sha256"])})
        self.assertLessEqual(int(e2n["lean"]["slots"]), 800)
        self.assertEqual(block()["E2N"]["contents"], "canary")

    def tier_lines(self):
        for line in fenced("tiers").splitlines():
            tier, exp, *kv = line.split()
            yield tier, exp, dict(x.split("=", 1) for x in kv)

    def test_every_tier_design_has_the_stated_counts_and_hash_and_is_accepted_by_dry_run(self):
        seen = set()
        for tier, exp, kv in self.tier_lines():
            with self.subTest(tier=tier, exp=exp):
                over = {k: kv[k] for k in ("reps", "policies", "scenarios") if k in kv}
                a = runner.parse_args(argv(exp, **over))
                trials, planned, _, sha = runner.plan(a)
                self.assertEqual((len(planned), len(trials), sha), (int(kv["slots"]), int(kv["trials"]), kv["sha256"]))
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(runner.main(["--dry-run", *argv(exp, **over)]), 0)
                self.assertIn(f"{exp}.planned_slots_sha256={sha}", out.getvalue())
                row = next(l for l in TEXT.splitlines() if l.startswith(f"| {exp} `--"))
                reps = int(kv["reps"])
                self.assertRegex(row, rf"(?<![\d,]){reps:,}(?![\d,])")  # the table quotes the same reps
                seen.add((tier, exp))
        self.assertEqual(len(seen), 22)  # 3 tiers x 7 experiments (A20 adds E2N), plus E3 in the high tier

    def test_the_standard_tier_in_the_block_is_the_standard_tier_in_the_tiers(self):
        b = block()
        std = {exp: kv for tier, exp, kv in self.tier_lines() if tier == "standard"}
        for exp, kv in std.items():
            self.assertEqual((b[exp]["reps"], b[exp]["planned_slots_sha256"]), (kv["reps"], kv["sha256"]), exp)

    def test_power_py_plans_exactly_the_designs_the_prereg_states(self):
        spec = importlib.util.spec_from_file_location("power", ROOT / "scripts" / "power.py")
        power = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(power)
        got = {(t, e): (str(v[0]), str(v[1]), str(v[2]), v[3]) for t, tier in power.TIERS.items()
               for e, v in power.tier_plans(tier).items()}
        want = {(t, e): (kv["reps"], kv["slots"], kv["trials"], kv["sha256"]) for t, e, kv in self.tier_lines()}
        self.assertEqual(got, want)
        seeds = {e: {**block(), "E3": high_e3()}[e]["seed"] for e in runner.EXPERIMENTS}
        self.assertEqual(seeds, {e: str(s) for e, s in power.SEEDS.items()})  # each experiment's own seed


if __name__ == "__main__":
    unittest.main()
