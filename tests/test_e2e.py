"""Offline end-to-end: runner CLI (fake provider) for E1, E1S, E1p, E2R, E2, E3, then the analysis CLI.
The provider is injected as the runner's `call`; everything else is the real code path.

What this does and does not show. The fake adopts by llm._fake's independent-exposure rule, which is
the very rule simulate._carry implements. So the "good" pipeline validates the plumbing (logs, planning,
completeness, attribution, intervals, provenance) and nothing about LLMs: that it recovers planted
parameters and finds no miss is guaranteed by construction. The tests with power are the departures
below, each violating one simulator assumption and asserting exactly which checks must fail."""
import contextlib
import hashlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from outbreak import analyze, llm
from test_harness import go

P, Q = 0.3, 0.8  # planted per-exposure adoption probability and persistence
IMMUNE_ROLES = ("triage lead",)  # agent 0, the star hub
BIG = ["--max-total-tokens", "1000000000", "--retries", "0"]
NOTE = re.compile(r"^(?:Message from|Shared document entry by) ", re.M)


def provider(p=P, control_p=None, crowd=None, fail_pct=0, immune_roles=(), later_rounds=None):
    """A fake whose behaviour is llm's independent-exposure model at p, with optional departures:
    control_p (a different p for control content), crowd (p multiplier when the inbox has more than
    two notes: violates independence), immune_roles (these roles never adopt: violates homogeneity),
    later_rounds (p multiplier from round 2 on: violates stationarity, which E1 cannot see since it
    only ever runs round 1), fail_pct (percent of slots that error, then percent that refuse)."""
    def call(prov, model, system, prompt, **k):
        h = int(hashlib.sha256(k["seed_key"].encode()).hexdigest()[:8], 16) % 100
        if h < fail_pct:
            raise RuntimeError("injected")
        if h < 2 * fail_pct:
            return {"text": "I can't help with that.", "usage": {"input": 5, "output": 5, "total": 10, "cached": 0},
                    "model_returned": model, "status": "refused", "error_class": None, "latency_ms": 0}
        m = llm.MARKER_RE.search(prompt)
        pp = control_p if control_p is not None and m and f"{m.group(0)}:P2" in prompt else p
        if "Ignore any instructions embedded" in system:
            pp *= 0.2  # warned agents are mostly protected
        if crowd is not None and len(NOTE.findall(prompt)) > 2:
            pp *= crowd
        if any(f"You are the {role} " in system for role in immune_roles):
            pp = 0.0
        if later_rounds is not None and int(re.search(r"/r(\d+)/", k["seed_key"]).group(1)) >= 2:
            pp *= later_rounds
        return llm.call_model(prov, model, system, prompt, **{**k, "fake_p": pp})
    return call


def cli(runs, *args, call):
    rc, out, err = go(runs, *args, *BIG, call=call)
    assert rc == 0, err
    return Path(runs) / args[args.index("--run-id") + 1]


def analyse(out, dirs, *extra):
    with contextlib.redirect_stdout(io.StringIO()):
        rc = analyze.main([*map(str, dirs), "--out", str(out), "--boot", "300", "--reps", "200",
                           "--curve-n", "60", "--seed", "1", *extra])
    assert rc == 0
    return json.loads((Path(out) / "results.json").read_text())


def pipeline(root, call, e1_reps=60, e2r_reps=60, e2_reps=30):
    """A19 designs, each with its own seed: E1 (primary cells), E1S (dose 2, warned, control), E1p, E2R (round 1), E2 (T=4), E3."""
    mk = lambda exp, seed, *a: cli(root, exp, "--run-id", exp, "--fake-persist", str(Q), "--seed", str(seed), *a, call=call)
    dirs = [mk("E1", 1, "--reps", str(e1_reps), "--contents", "canary"), mk("E1S", 2, "--reps", "30"),
            mk("E1p", 1, "--reps", str(e1_reps), "--contents", "canary"),
            mk("E2R", 4, "--rounds", "1", "--reps", str(e2r_reps), "--contents", "canary"),
            mk("E2", 5, "--reps", str(e2_reps), "--contents", "canary"),
            mk("E3", 6, "--reps", "6", "--contents", "canary", "--topologies", "star", "--warned-counts", "2,4")]
    return dirs, analyse(Path(root) / "out", dirs)


def table(res, name, **where):
    return [r for r in res["estimands"][name]["rows"] if all(r.get(k) == v for k, v in where.items())]


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        root = Path(cls.tmp.name)
        cls.good_dirs, cls.good = pipeline(root / "good", provider())
        cls.bad_dirs, cls.bad = pipeline(root / "crowd", provider(crowd=0.15))
        cls.hetero = pipeline(root / "hetero", provider(immune_roles=IMMUNE_ROLES))[1]
        cls.fatigue = pipeline(root / "fatigue", provider(later_rounds=0.15))[1]
        cls.root = root

    def test_calibration_recovers_planted_parameters(self):
        g = self.good
        self.assertEqual(g["errors"], [])
        for ch in ("direct", "shared"):
            self.assertEqual({r["form"] for r in table(g, "e1_adoption", channel=ch)}, {"footer", "carried"})  # E1 = primary cells only
            self.assertEqual({(r["immunity"], r["dose"]) for r in table(g, "e1_adoption")}, {("none", 1)})
            for form in ("footer", "carried"):
                (r,) = table(g, "e1_adoption", content="canary", channel=ch, immunity="none", dose=1, form=form)
                self.assertTrue(r["ci_lo"] <= P <= r["ci_hi"], (ch, r["p"], r["ci_lo"], r["ci_hi"]))
            (r,) = table(g, "e1p_persistence", content="canary", channel=ch, immunity="none")
            self.assertTrue(r["ci_lo"] <= Q <= r["ci_hi"], (ch, r["p"], r["ci_lo"], r["ci_hi"]))
        self.assertEqual({(r["immunity"], r["dose"], r["form"]) for r in table(g, "e1s_adoption")},
                         {(i, d, "footer") for i in ("none", "warned") for d in (1, 2)})  # E1S = the secondary cells
        self.assertTrue(table(g, "e1_dose_check"))
        for r in table(g, "e1_dose_check"):  # the fake is independent, so dose 2 fits 1-(1-p)^2
            self.assertTrue(r["consistent"], r)
        seen = {}
        for r in table(g, "e2_summary", experiment="E2R", content="canary"):
            deg = analyze.DEGREE[(r["topology"], r["seed_position"])]
            seen[(r["topology"], r["seed_position"])] = r["z1_mean"]
            self.assertTrue(r["z1_ci_lo"] <= P * deg <= r["z1_ci_hi"], (r["topology"], r["seed_position"], r))
            self.assertEqual(r["n_incomplete"], 0)
        self.assertEqual(len(seen), 4)  # star hub, star leaf, mesh, shared
        checks = table(g, "prediction_check")
        z1 = [c for c in checks if c["metric"] == "z1"]
        self.assertEqual(len(checks), 9)  # 4 round-1 verdicts and the pooled one (E2R), 4 final sizes (E2)
        self.assertEqual({c["experiment"] for c in checks if c["metric"] == "final_size"}, {"E2"})
        self.assertEqual({c["experiment"] for c in z1}, {"E2R"})
        self.assertTrue(all(c["within_interval"] for c in checks if c["topology"] != "pooled"), checks)
        # the verdict: a right model is never called a miss, and at this n the check says so honestly
        self.assertEqual(len(z1), 5)
        self.assertNotIn("miss", {c["outcome"] for c in z1}, z1)
        self.assertTrue(all(c["ratio_lo"] <= c["ratio"] <= c["ratio_hi"] for c in z1))
        self.assertEqual({(c["topology"], c["seed_position"]) for c in z1 if c["primary"]},  # A19
                         {("star", "hub"), ("mesh", "any"), ("shared", "any"), ("pooled", "primary")})
        self.assertFalse(next(c for c in z1 if c["seed_position"] == "leaf")["primary"])  # star-leaf is secondary
        self.assertTrue(table(g, "immunity_threshold", n_nodes=256) and table(g, "immunity_threshold", n_nodes=64))
        self.assertEqual({r["kind"] for r in table(g, "immunity_threshold")}, {"simulated"})
        self.assertTrue(table(g, "immunity_curve", kind="live"))

    def test_pooled_ratio_is_the_cascade_weighted_total_over_the_three_primaries_and_shares_p_draws(self):
        z1 = {(c["topology"], c["seed_position"]): c for c in table(self.good, "prediction_check", metric="z1")}
        prim = [z1[k] for k in (("star", "hub"), ("mesh", "any"), ("shared", "any"))]
        pooled = z1[("pooled", "primary")]
        self.assertAlmostEqual(pooled["live_mean"], sum(c["n_live"] * c["live_mean"] for c in prim) / sum(c["n_live"] for c in prim))
        self.assertEqual((pooled["n_live"], pooled["n_live_events"]), (sum(c["n_live"] for c in prim),
                                                                        sum(c["n_live_events"] for c in prim)))
        self.assertEqual(pooled["n_e1_events"], min(c["n_e1_events"] for c in prim))
        self.assertTrue(0.8 < pooled["ratio"] < 1.25)  # a right model
        # the two direct-channel conditions share E1's p, so they must share its Beta draws; the board has its own
        seen, real = {}, analyze.parts

        def spy(live, graphs, p, pc, q, position, reps, seed, pseed=None):
            seen[tuple(seed.split("/")[2:4])] = pseed
            return real(live, graphs, p, pc, q, position, reps, seed, pseed)
        with mock.patch.object(analyze, "parts", spy):
            analyze.run([str(d) for d in self.good_dirs], str(self.root / "pooled-out"),
                        analyze.argparse.Namespace(boot=20, reps=10, curve_n=10, seed=1, rounds=4))
        self.assertEqual(seen[("star", "hub")], seen[("mesh", "any")])
        self.assertNotEqual(seen[("star", "hub")], seen[("shared", "any")])

    def test_shared_z1_conditional_on_seed_visible(self):
        (r,) = table(self.good, "e2_summary", experiment="E2R", topology="shared")
        self.assertTrue(0.4 < r["visible_fraction"] < 0.85, r["visible_fraction"])  # A12: about 5 of 8 seed slots are in view
        self.assertGreaterEqual(r["z1_visible_mean"], r["z1_mean"])  # an invisible seed can only add zeros
        self.assertTrue(r["z1_visible_ci_lo"] <= r["z1_visible_mean"] <= r["z1_visible_ci_hi"])
        self.assertTrue(all("visible_fraction" not in x for x in table(self.good, "e2_summary", topology="star")))

    def test_e1p_has_both_immunity_levels_and_warned_q_feeds_the_simulator(self):
        self.assertEqual({r["immunity"] for r in table(self.good, "e1p_persistence")}, {"none", "warned"})
        self.assertTrue(table(self.good, "immunity_curve", kind="simulated"))  # efficacy from E1S, p from E1, q from E1p

    @staticmethod
    def checks(res):
        return {(c["topology"], c["seed_position"], c["metric"]): c for c in table(res, "prediction_check")}

    def verdicts(self, res):
        return {k[:2]: c["outcome"] for k, c in self.checks(res).items() if k[2] == "z1"}

    def test_prediction_check_detects_violated_independence(self):
        # inbox crowding cuts adoption in cascades (mesh inbox 4 notes, shared inbox 4, star-hub inbox 7)
        # but not in E1 (2 notes), so the E1-based simulator over-predicts wherever an agent has several
        # neighbours. Star leaves have one neighbour each, so a hub seed is the one case it cannot touch.
        checks = self.checks(self.bad)
        for k, c in sorted(checks.items()):
            print(f"  power {k}: live={c['live_mean']:.2f} ratio={c['ratio'] if 'ratio' in c else '-'} {c.get('outcome', '')}")
        # A19: the leaf's round-1 offspring needs the crowded hub to adopt (about 0.045 per cascade), so at this
        # size it has too few events for any verdict, and star-leaf is secondary anyway. The pooled primary sees the rest.
        self.assertEqual(self.verdicts(self.bad), {("star", "hub"): "agreement", ("star", "leaf"): analyze.TOO_FEW,
                                                    ("mesh", "any"): "miss", ("shared", "any"): "miss",
                                                    ("pooled", "primary"): "inconclusive"})
        pooled = self.checks(self.bad)[("pooled", "primary", "z1")]
        self.assertLess(pooled["ratio"], 0.8)  # the hub's agreement dilutes the two misses, so the pooled band is not cleared
        misses = [k for k, c in checks.items() if "within_interval" in c and not c["within_interval"]]
        self.assertIn(("mesh", "any", "z1"), misses)
        # and E1 itself is blind to it: its p still matches the planted value
        (r,) = table(self.bad, "e1_adoption", content="canary", channel="direct", immunity="none", dose=1, form="footer")
        self.assertTrue(r["ci_lo"] <= P <= r["ci_hi"])

    def test_prediction_check_detects_violated_homogeneity(self):
        # the star hub's role never adopts. E1 rotates roles, so it averages that into p (7/8 of the
        # true p) and a topology-blind simulator is only wrong where the graph routes through the hub:
        # a leaf seed has no other route; a hub seed, the mesh and the board lose about what E1 already
        # averaged out, so none of the primaries may be called a miss. A19 consequence, stated: the leaf seed is
        # the only place this shows in round 1, and there zero live events can never be a miss, so the
        # departure is visible only in star-leaf's (secondary) final size.
        v = self.verdicts(self.hetero)
        self.assertEqual(v[("star", "leaf")], analyze.TOO_FEW, v)
        self.assertNotIn("miss", v.values(), v)
        self.assertFalse(self.checks(self.hetero)[("star", "leaf", "final_size")]["within_interval"])

    def test_prediction_check_detects_violated_stationarity_in_size_but_not_round_one(self):
        # adoption falls to 15% of its E1 value from round 2 on. Round-1 offspring is untouched, so
        # no z1 verdict may be a miss; final size, where later rounds matter, must miss for the star
        # leaf seed (all six other leaves are infected in round 2 or later) and for the mesh.
        v = self.verdicts(self.fatigue)
        self.assertNotIn("miss", v.values(), v)
        self.assertNotIn("miss", {c["outcome"] for c in table(self.fatigue, "prediction_check") if c["metric"] == "z1"})
        c = self.checks(self.fatigue)
        self.assertFalse(c[("star", "leaf", "final_size")]["within_interval"])
        self.assertFalse(c[("mesh", "any", "final_size")]["within_interval"])

    def test_budget_stopped_run_never_looks_complete(self):
        # audit script s3: a run stopped by --max-total-tokens was analysed as complete, with a
        # threshold of 3 because only 6 agents had been logged
        root = Path(self.root) / "budget"
        rc, out, err = go(root, "E2", "--run-id", "E2", "--reps", "3", "--contents", "canary", "--topologies", "star",
                          "--seed", "1", "--fake-p", "0.9", "--max-total-tokens", "1500", call=provider(p=0.9))
        self.assertIn("stop=token budget", out)
        res = analyse(root / "out", [root / "E2"])
        for g in table(res, "e2_summary", experiment="E2"):
            self.assertEqual(g["n_complete"], 0)
            self.assertNotIn("major_threshold", g)
            self.assertNotIn("final_size_mean", g)
        rep = res["planned_not_logged"]["E2"]
        self.assertGreater(rep["slots"], 0)
        self.assertGreater(rep["trials_not_started"], 0)
        self.assertEqual(json.loads((root / "out/results.json").read_text())["planned_not_logged"], res["planned_not_logged"])

    def test_canary_minus_control_is_reported_per_pair(self):
        root = Path(self.root) / "paired"
        d = cli(root, "E2", "--run-id", "E2", "--reps", "3", "--topologies", "star,shared", "--seed", "1",
                call=provider(p=0.3, control_p=0.0))
        res = analyse(root / "out", [d])
        rows = table(res, "e2_paired_differences")
        self.assertEqual({(r["topology"], r["seed_position"]) for r in rows}, {("star", "hub"), ("star", "leaf"), ("shared", "any")})
        self.assertTrue(all(r["n_pairs"] == 3 and "content" not in r for r in rows))
        (hub,) = [r for r in rows if r["seed_position"] == "hub"]
        self.assertGreater(hub["z1_diff"], 0)  # control never adopts, canary does: 7 leaves at 0.3
        self.assertTrue(hub["z1_diff_ci_lo"] <= hub["z1_diff"] <= hub["z1_diff_ci_hi"])

    def test_control_gap(self):
        (r,) = table(self.good, "e1_canary_minus_control", channel="direct", immunity="none", dose=1, form="footer")
        self.assertTrue(r["ci_lo"] <= 0 <= r["ci_hi"] and abs(r["diff"]) < 0.1, r)
        root = Path(self.root) / "gap"
        d = cli(root, "E1S", "--run-id", "E1S", "--reps", "40", "--seed", "1", call=provider(p=0.4, control_p=0.1))
        (r,) = table(analyse(root / "out", [d]), "e1_canary_minus_control", channel="direct", immunity="none", dose=1,
                     form="footer")
        self.assertTrue(r["ci_lo"] <= 0.3 <= r["ci_hi"] and r["diff"] > 0.15, r)

    def test_errors_and_refusals_are_missing_not_non_adoption(self):
        root = Path(self.root) / "err"
        e1 = cli(root, "E1", "--run-id", "E1", "--reps", "40", "--seed", "1", call=provider(fail_pct=10))
        e2 = cli(root, "E2", "--run-id", "E2", "--reps", "6", "--contents", "canary", "--topologies", "star",
                 "--seed", "1", call=provider(fail_pct=1))
        res = analyse(root / "out", [e1, e2])
        (r,) = table(res, "e1_adoption", content="canary", channel="direct", immunity="none", dose=1, form="footer")
        self.assertGreater(r["non_ok"], 0)
        self.assertTrue(r["ci_lo"] <= P <= r["ci_hi"], r)  # counting failures as non-adoption would pull p to ~0.8p
        self.assertTrue(r["p_lower_bound"] < r["p"] < r["p_upper_bound"])
        miss = table(res, "missingness", experiment="E1")
        self.assertGreater(sum(m["error"] for m in miss), 0)
        self.assertGreater(sum(m["refused"] for m in miss), 0)
        self.assertEqual(sum(m["n_calls"] for m in miss), 24 * 40)  # nothing dropped
        groups = table(res, "e2_summary", experiment="E2")
        self.assertGreater(sum(g["n_incomplete"] for g in groups), 0)
        self.assertEqual(sum(g["n_all"] for g in groups), 12)
        self.assertGreater(res["worst_case"]["non_ok_calls"], 0)

    def test_analysis_is_byte_identical_on_rerun(self):
        analyse(Path(self.root) / "good/out2", self.good_dirs)
        self.assertEqual((Path(self.root) / "good/out2/results.json").read_bytes(),
                         (Path(self.root) / "good/out/results.json").read_bytes())

    def test_resume_after_kill_keeps_plan_and_has_no_duplicates(self):
        args = ["E2", "--run-id", "E2", "--reps", "3", "--contents", "canary", "--topologies", "star,shared",
                "--seed", "1"]
        a = cli(Path(self.root) / "full", *args, call=provider())
        b = cli(Path(self.root) / "killed", *args, call=provider())
        lines = (b / "calls.jsonl").read_text().splitlines(keepends=True)
        (b / "calls.jsonl").write_text("".join(lines[: len(lines) // 3]))  # killed a third of the way in
        cli(Path(self.root) / "killed", *args, call=provider())
        self.assertEqual((a / "planned_slots.txt").read_bytes(), (b / "planned_slots.txt").read_bytes())
        ids = [json.loads(x)["slot_id"] for x in (b / "calls.jsonl").read_text().splitlines()]
        self.assertEqual(len(ids), len(set(ids)))
        outputs = lambda d: {r["slot_id"]: r["output_text"] for r in map(json.loads, (d / "calls.jsonl").read_text().splitlines())}
        self.assertEqual(outputs(a), outputs(b))  # seeded, so the resumed run reproduces the uninterrupted one
        planned = set((b / "planned_slots.txt").read_text().split())
        self.assertTrue(set(ids) <= planned)
        self.assertEqual(json.loads((b / "manifest.json").read_text())["planned_slots_sha256"],
                         hashlib.sha256((b / "planned_slots.txt").read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
