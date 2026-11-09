import contextlib
import hashlib
import io
import json
import math
import os
import random
import statistics
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from outbreak import analyze, simulate, stats


def cond(**kw):
    return {"content": "canary", "wording": 0, "channel": "direct", "immunity": "none", "dose": 1,
            "warned_count": None, "placement": None, "form": None, **kw}


def row(**kw):
    base = dict(slot_id="s0", run_id="r1", experiment="E1", trial_id="t0", round=1, agent="a0",
                condition=cond(),
                topology=None, seed_position=None, graph_id=None, seed_agent=None, board_order=None, inbox_sources=[], inbox_markers={}, marker="OB-00000000",
                output_text="", adopted=False, status="ok", error_class=None, attempts=1,
                model_requested="m", model_returned="m", provider="p", temperature=0.0,
                usage={"input": 1, "output": 1, "total": 2, "cached": 0}, latency_ms=1, prompt_sha256="x", ts="t")
    base.update(kw)
    return base


def write_run(root, run_id, rows, experiment="E1", smoke=False, raw_extra=(), graphs=None, rounds=4, **manifest):
    d = os.path.join(root, run_id)
    os.makedirs(d)
    plan = "".join(r["slot_id"] + "\n" for r in rows)  # A19: a run without its plan file is an analysis error
    with open(os.path.join(d, "planned_slots.txt"), "w") as f:
        f.write(plan)
    with open(os.path.join(d, "manifest.json"), "w") as f:
        json.dump({"run_id": run_id, "experiment": experiment, "smoke": smoke, "graphs": graphs or {},
                   "planned_slots_sha256": hashlib.sha256(plan.encode()).hexdigest(),
                   "params": {"rounds": rounds}, **manifest}, f)
    with open(os.path.join(d, "calls.jsonl"), "w") as f:
        for r in list(rows) + list(raw_extra):
            f.write(json.dumps(r) + "\n")
    return d


def e1_rows(run_id, content, immunity, dose, k, n, status_extra=(), start=0, exp="E1", form="footer"):
    form = form if exp == "E1" else None  # E1p has no form
    c = cond(content=content, immunity=immunity, dose=dose, form=form)
    rs = [row(slot_id=f"{run_id}-{content}-{immunity}-{dose}-{form}-{start + i}", run_id=run_id, condition=c, experiment=exp,
              trial_id=f"{content}{immunity}{dose}{form}{start + i}", adopted=i < k) for i in range(n)]
    for j, s in enumerate(status_extra):
        rs.append(row(slot_id=f"{run_id}-{content}-{immunity}-{dose}-{form}-x{j}", run_id=run_id, condition=c, experiment=exp,
                      trial_id=f"x{j}", adopted=False, status=s))
    return rs


MESH = [[i, (i + d) % 8] for i in range(8) for d in (1, 2)]  # a 4-regular ring, as a manifest edge list


def hand_cascade(run_id="r2", experiment="E2", trial="t1"):
    """Seed 0 (round 0, no call). Round 1 infects 1,2,3 (parent 0); round 2 infects 4 (parents 1,2),
    5 (parents 0,3), 6 (parent 3). Agent 7 is exposed to 4 in round 3 and never adopts."""
    inf_at = {1: 1, 2: 1, 3: 1, 4: 2, 5: 2, 6: 2}
    inbox = {(1, 1): [0], (2, 1): [0], (3, 1): [0], (4, 2): [1, 2], (5, 2): [0, 3], (6, 2): [3], (7, 3): [4]}
    carry = {1: {0}, 2: {0, 1, 2, 3}, 3: {0, 1, 2, 3, 4, 5, 6}}
    rs = []
    for rnd in (1, 2, 3):
        for a in range(8):
            src = [f"a{s}" for s in inbox.get((a, rnd), [])]
            rs.append(row(slot_id=f"{run_id}-{trial}-{rnd}-{a}", run_id=run_id, experiment=experiment, trial_id=trial,
                          round=rnd, agent=a, topology="mesh", seed_position="any", graph_id="mesh-0", seed_agent=0,
                          condition=cond(dose=None, warned_count=0), inbox_sources=src,
                          inbox_markers={s: int(s[1:]) in carry[rnd] for s in src},
                          adopted=a in inf_at and inf_at[a] <= rnd))
    return rs


class StatsTests(unittest.TestCase):
    def test_wilson_known_values(self):
        z2 = 1.96 ** 2
        lo, hi = stats.wilson(0, 10)
        self.assertEqual(lo, 0.0)
        self.assertAlmostEqual(hi, z2 / (10 + z2), places=6)
        lo, hi = stats.wilson(10, 10)
        self.assertEqual(hi, 1.0)
        self.assertAlmostEqual(lo, 10 / (10 + z2), places=6)
        lo, hi = stats.wilson(5, 10)
        self.assertAlmostEqual(lo, 0.2366, places=3)
        self.assertAlmostEqual(hi, 0.7634, places=3)
        self.assertEqual(stats.wilson(0, 0), (0.0, 1.0))

    def test_bootstrap_seeded_and_repeatable(self):
        data = [[1, 0, 3, 2, 5, 1, 0, 2]]
        f = lambda s: stats.mean(s[0])
        a = stats.cluster_bootstrap(data, f, b=300, seed=7)
        self.assertEqual(a, stats.cluster_bootstrap(data, f, b=300, seed=7))
        self.assertNotEqual(a, stats.cluster_bootstrap(data, f, b=300, seed=8))
        self.assertTrue(a[0] < stats.mean(data[0]) < a[1])

    def test_two_stage_bootstrap_seeded_and_covers_mean(self):
        groups = [[1, 0, 2], [3, 3, 2, 4], [0, 1]]
        a = stats.two_stage_bootstrap(groups, b=300, seed=7)
        self.assertEqual(a, stats.two_stage_bootstrap(groups, b=300, seed=7))
        self.assertTrue(a[0] < stats.mean([x for g in groups for x in g]) < a[1])

    def test_two_stage_bootstrap_resamples_graphs(self):
        # mutation guard: two graphs with very different means. Resampling graphs makes the grand mean
        # swing between them (wide); resampling only within each graph pins it near 2 (narrow).
        lo, hi = stats.two_stage_bootstrap([[0, 0, 0, 0], [4, 4, 4, 4]], b=500, seed=1)
        self.assertEqual((lo, hi), (0.0, 4.0))

    def test_major_outbreak_is_half_of_agents(self):
        self.assertEqual(stats.final_size_summary([3, 4, 8], 8)["p_major"], 2 / 3)
        self.assertEqual(stats.final_size_summary([1], 8)["major_threshold"], 4)


class AnalysisBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, "out")

    def go(self, dirs, **kw):
        args = analyze.argparse.Namespace(boot=100, reps=20, curve_n=20, seed=0, rounds=4, **kw)
        return analyze.run(dirs, self.out, args)


class AnalysisTests(AnalysisBase):

    def test_recovery_of_planted_p(self):
        p, n, hits = 0.3, 80, 0
        for seed in range(200):
            rng = random.Random(seed)
            k = sum(rng.random() < p for _ in range(n))
            lo, hi = stats.wilson(k, n)
            hits += lo <= p <= hi
        self.assertGreaterEqual(hits / 200, 0.9)

    def test_recovery_through_pipeline(self):
        d = write_run(self.tmp.name, "r1", e1_rows("r1", "canary", "none", 1, 30, 100))
        res = self.go([d])
        r = res["estimands"]["e1_adoption"]["rows"][0]
        self.assertEqual((r["k"], r["n"]), (30, 100))
        self.assertTrue(r["ci_lo"] <= 0.3 <= r["ci_hi"])

    def test_z1_hand_worked_cascade(self):
        c = analyze.build_cascade(hand_cascade(), 3)
        self.assertEqual(c["final_size"], 7)
        self.assertEqual(c["z1"], 3)  # primary: round-1 offspring of the seed
        self.assertAlmostEqual(c["z1_frac"], 3.5)  # 1,2,3 full + half of 5 (secondary, all rounds)
        self.assertAlmostEqual(c["z1_last"], 3.0)  # 5 credited to 3 under last-read

    def test_smoke_excluded(self):
        real = write_run(self.tmp.name, "r1", e1_rows("r1", "canary", "none", 1, 5, 10))
        smoke = write_run(self.tmp.name, "r2", e1_rows("r2", "canary", "none", 1, 10, 10), smoke=True)
        res = self.go([real, smoke])
        self.assertEqual(res["excluded_smoke_runs"], ["r2"])
        self.assertEqual(res["estimands"]["e1_adoption"]["rows"][0]["n"], 10)
        self.assertEqual(res["estimands"]["e1_adoption"]["rows"][0]["k"], 5)
        self.assertEqual(res["estimands"]["e1_adoption"]["run_ids"], ["r1"])

    def test_errors_not_counted_as_nonadoption(self):
        rows = e1_rows("r1", "canary", "none", 1, 5, 10, status_extra=["error", "refused", "truncated"])
        res = self.go([write_run(self.tmp.name, "r1", rows)])
        r = res["estimands"]["e1_adoption"]["rows"][0]
        self.assertEqual((r["n"], r["non_ok"], r["p"]), (10, 3, 0.5))
        self.assertAlmostEqual(r["p_lower_bound"], 5 / 13)
        self.assertAlmostEqual(r["p_upper_bound"], 8 / 13)
        m = res["estimands"]["missingness"]["rows"][0]
        self.assertEqual((m["error"], m["refused"], m["truncated"]), (1, 1, 1))
        self.assertEqual(res["worst_case"]["non_ok_calls"], 3)

    def test_duplicates_and_unplanned_reported(self):
        rows = e1_rows("r1", "canary", "none", 1, 2, 4)
        other = row(slot_id="zz", run_id="other")
        res = self.go([write_run(self.tmp.name, "r1", rows, raw_extra=[rows[0], other])])
        self.assertTrue(any("duplicate" in e for e in res["errors"]))
        self.assertTrue(any("unplanned" in e for e in res["errors"]))
        self.assertEqual(res["estimands"]["e1_adoption"]["rows"][0]["n"], 4)  # neither merged

    def test_results_carry_input_hashes(self):
        d = write_run(self.tmp.name, "r1", e1_rows("r1", "canary", "none", 1, 5, 10))
        res = self.go([d])
        with open(os.path.join(d, "calls.jsonl"), "rb") as f:
            want = hashlib.sha256(f.read()).hexdigest()
        with open(os.path.join(self.out, "results.json")) as f:
            saved = json.load(f)
        t = saved["estimands"]["e1_adoption"]
        self.assertEqual(t["input_sha256"], {"r1": want})
        self.assertEqual(saved["inputs"][0]["calls_sha256"], want)
        self.assertEqual(saved["provenance"][t["inputs_digest"]]["input_sha256"], {"r1": want})
        with open(os.path.join(self.out, "e1_adoption.csv")) as f:
            self.assertIn("inputs_digest", f.readline())

    def r1_run(self, n, run_id="r4"):
        """n round-1 cascades (E2R), each the hand cascade's round 1: seed 0 infects 1, 2 and 3."""
        rs = [r for i in range(n) for r in hand_cascade(run_id, "E2R", f"t{i}") if r["round"] == 1]
        return write_run(self.tmp.name, run_id, rs, experiment="E2R", graphs={"mesh-0": MESH}, rounds=1)

    def test_roles_e2r_gives_the_offspring_verdict_e2_gives_final_size_and_rounds_are_not_pooled(self):
        e1 = write_run(self.tmp.name, "r1", e1_rows("r1", "canary", "none", 1, 30, 100)
                       + e1_rows("r1", "canary", "none", 1, 20, 100, form="carried"))
        e1p = write_run(self.tmp.name, "r3", e1_rows("r3", "canary", "none", 0, 70, 100, exp="E1p"), experiment="E1p")
        e2 = write_run(self.tmp.name, "r2", hand_cascade(), experiment="E2", graphs={"mesh-0": MESH}, rounds=3)
        res = self.go([e1, e1p, self.r1_run(3), e2])
        rows = res["estimands"]["prediction_check"]["rows"]
        self.assertEqual(sorted((r["experiment"], r["metric"]) for r in rows), [("E2", "final_size"), ("E2R", "z1")])
        z1, size = sorted(rows, key=lambda r: r["metric"] != "z1")
        self.assertEqual((z1["live_mean"], z1["n_live"], z1["n_live_events"], z1["n_e1_events"]), (3, 3, 9, 30))  # E2's own z1 is not added
        self.assertEqual((size["live_mean"], size["n_live"]), (7, 1))
        self.assertTrue(z1["ratio_lo"] <= z1["ratio"] <= z1["ratio_hi"])
        self.assertIn(z1["outcome"], ("agreement", "miss", "inconclusive"))  # 9 events and 30 E1 events: a verdict is possible
        self.assertNotIn("outcome", size)
        self.assertTrue(z1["primary"] and not size["primary"])  # mesh is primary, but only its round-1 row carries the verdict
        self.assertTrue(any("pooled ratio skipped" in n for n in res["notes"]))  # star-hub and shared are absent
        self.assertEqual(sorted(res["estimands"]["prediction_check"]["run_ids"]), ["r1", "r2", "r3", "r4"])
        d = res["estimands"]["e2_derived"]["rows"][0]  # derived from round 1 only
        self.assertAlmostEqual(d["expected_z1_derived"], 4 * 0.3)
        self.assertEqual(res["estimands"]["e1p_persistence"]["rows"][0]["k"], 70)
        groups = {(g["experiment"], g["n_complete"]) for g in res["estimands"]["e2_summary"]["rows"]}
        self.assertEqual(groups, {("E2R", 3), ("E2", 1)})  # separate groups, never one pooled row

    def test_runs_of_different_experiments_may_have_their_own_seed_but_one_experiment_may_not(self):
        a = write_run(self.tmp.name, "r1", e1_rows("r1", "canary", "none", 1, 5, 10), rng_seed=1)
        b = write_run(self.tmp.name, "r2", e1_rows("r2", "canary", "none", 0, 5, 10, exp="E1p"), experiment="E1p", rng_seed=2)
        self.assertEqual(self.go([a, b])["errors"], [])
        c = write_run(self.tmp.name, "r3", e1_rows("r3", "canary", "none", 1, 5, 10), rng_seed=3)
        with self.assertRaisesRegex(ValueError, "rng_seed"):
            self.go([a, c])

    def test_results_record_provider_model_and_prereg_and_torn_lines(self):
        d = write_run(self.tmp.name, "r1", e1_rows("r1", "canary", "none", 1, 5, 10), prereg_sha="f" * 40,
                      params={"provider": "adaption", "model": "m1", "rounds": 4})
        with open(os.path.join(d, "torn_lines.jsonl"), "w") as f:
            f.write('{"slot_id": "x\n{"slot_id": "y\n')
        res = self.go([d])
        self.assertEqual((res["provider"], res["model"], res["prereg_sha"]), ("adaption", "m1", "f" * 40))
        self.assertEqual(res["torn_lines"], {"r1": 2})
        with open(os.path.join(self.out, "results.json")) as f:
            saved = json.load(f)
        self.assertEqual((saved["provider"], saved["model"], saved["prereg_sha"], saved["torn_lines"]),
                         ("adaption", "m1", "f" * 40, {"r1": 2}))
        d2 = write_run(self.tmp.name, "r2", e1_rows("r2", "canary", "none", 1, 5, 10))
        self.assertEqual(self.go([d2])["torn_lines"], {"r2": 0})

    def test_a_missing_planned_slots_file_is_an_error_not_a_skipped_check(self):
        d = write_run(self.tmp.name, "r1", e1_rows("r1", "canary", "none", 1, 5, 10))
        os.remove(os.path.join(d, "planned_slots.txt"))
        res = self.go([d])
        self.assertTrue(any("planned_slots.txt is missing" in e for e in res["errors"]))
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(analyze.main([d, "--out", self.out, "--boot", "20", "--reps", "5"]), 2)

    def test_cli_exit_code(self):
        d = write_run(self.tmp.name, "r1", e1_rows("r1", "canary", "none", 1, 5, 10))
        self.assertEqual(analyze.main([d, "--out", self.out, "--boot", "50", "--reps", "5"]), 0)

    def test_e2_incomplete_cascade_bounds(self):
        rs = hand_cascade()
        for r in rs:
            if r["agent"] == 7 and r["round"] == 3:
                r["status"] = "error"
        res = self.go([write_run(self.tmp.name, "r2", rs, experiment="E2", rounds=3)])
        g = res["estimands"]["e2_summary"]["rows"][0]
        self.assertEqual((g["n_complete"], g["n_incomplete"]), (0, 1))  # excluded from primary, not non-adoption
        self.assertAlmostEqual(g["final_size_lower_bound"], 7)
        self.assertAlmostEqual(g["final_size_upper_bound"], 8)


class Completeness(AnalysisBase):
    """A17: a cascade is complete only if every planned slot was logged (or the runner's own early stop)."""

    def cascade_rows(self, drop=lambda r: False, rounds=3):
        return [r for r in hand_cascade() if not drop(r)]

    def group(self, rows, rounds=4):
        self.n = getattr(self, "n", 0) + 1  # a fresh directory per call
        res = self.go([write_run(os.path.join(self.tmp.name, str(self.n)), "r2", rows, experiment="E2", rounds=rounds)])
        return res["estimands"]["e2_summary"]["rows"][0], res

    def test_all_rounds_logged_is_complete_and_a_missing_slot_is_not(self):
        g, _ = self.group(hand_cascade(), rounds=3)
        self.assertEqual((g["n_complete"], g["n_incomplete"], g["major_threshold"]), (1, 0, 4))
        g, _ = self.group(self.cascade_rows(lambda r: r["round"] == 3 and r["agent"] == 5), rounds=3)
        self.assertEqual((g["n_complete"], g["n_incomplete"]), (0, 1))
        self.assertNotIn("major_threshold", g)  # nothing complete, so no outbreak statistics from a partial run

    def test_stopped_after_t_minus_one_rounds_is_not_complete_but_an_early_stop_is(self):
        g, _ = self.group(hand_cascade(), rounds=4)  # rounds 1-3 logged of a planned 4, and round 3 still had spread
        self.assertEqual(g["n_complete"], 0)
        quiet = [r for r in hand_cascade() if r["round"] == 1] + [dict(r, round=2) for r in hand_cascade() if r["round"] == 1]
        for r in quiet:  # nobody adopts in rounds 1-2: the runner stops there, so rounds 3-4 never being logged is fine
            r["adopted"] = False
            r["inbox_markers"] = {s: False for s in r["inbox_sources"]}
            r["slot_id"] += f"-{r['round']}"
        g, _ = self.group(quiet, rounds=4)
        self.assertEqual(g["n_complete"], 1)

    def test_truncation_does_not_make_a_cascade_incomplete_but_error_does(self):
        rs = hand_cascade()
        rs[9]["status"] = "truncated"
        g, _ = self.group(rs, rounds=3)
        self.assertEqual(g["n_complete"], 1)
        rs[10]["status"] = "error"
        g, _ = self.group(rs, rounds=3)
        self.assertEqual(g["n_complete"], 0)

    def test_truncated_note_with_marker_is_a_known_adoption_without_it_is_censored(self):
        rows = e1_rows("r1", "canary", "none", 1, 5, 10)
        rows[0]["status"] = "truncated"     # adopted, truncated: counts
        rows[9]["status"] = "truncated"     # not adopted, truncated: censored
        res = self.go([write_run(self.tmp.name, "r1", rows)])
        r = res["estimands"]["e1_adoption"]["rows"][0]
        self.assertEqual((r["k"], r["n"], r["non_ok"]), (5, 9, 1))
        m = res["estimands"]["missingness"]["rows"][0]
        self.assertAlmostEqual(m["truncated_rate"], 0.2)

    def test_planned_not_logged_is_reported(self):
        rows = hand_cascade()
        for r in rows:
            r["slot_id"] = f"t1/r{r['round']}/a{r['agent']}"
        d = write_run(self.tmp.name, "r2", rows, experiment="E2", rounds=3)
        planned = [r["slot_id"] for r in rows] + ["gone/r1/a0", "gone/r1/a1"]
        with open(os.path.join(d, "planned_slots.txt"), "w") as f:
            f.write("\n".join(planned) + "\n")
        with open(os.path.join(d, "manifest.json")) as f:
            man = json.load(f)
        man["planned_slots_sha256"] = hashlib.sha256(Path(d, "planned_slots.txt").read_bytes()).hexdigest()
        with open(os.path.join(d, "manifest.json"), "w") as f:
            json.dump(man, f)
        res = self.go([d])
        self.assertEqual(res["planned_not_logged"], {"r2": {"slots": 2, "trials_not_started": 1}})
        with open(os.path.join(self.out, "results.json")) as f:
            self.assertEqual(json.load(f)["planned_not_logged"]["r2"]["slots"], 2)

    def test_runs_that_differ_in_model_or_provider_are_not_pooled(self):
        a = write_run(self.tmp.name, "r1", e1_rows("r1", "canary", "none", 1, 5, 10), params={"model": "m1", "provider": "fake"})
        b = write_run(self.tmp.name, "r2", e1_rows("r2", "canary", "none", 1, 5, 10), params={"model": "m1", "provider": "adaption"})
        with self.assertRaisesRegex(ValueError, "provider"):
            self.go([a, b])
        c = write_run(self.tmp.name, "r3", e1_rows("r3", "canary", "none", 1, 5, 10), params={"model": "m1", "provider": "fake"},
                      prereg_sha="x", rng_seed=1)
        with self.assertRaisesRegex(ValueError, "rng_seed, prereg_sha"):
            self.go([a, c])


class PredictionCheck(unittest.TestCase):
    """A15: ratio of live to predicted round-1 offspring, with a CI over live data, p and q."""
    RING = {i: [(i - 2) % 8, (i - 1) % 8, (i + 1) % 8, (i + 2) % 8] for i in range(8)}

    def live(self, p, n, seed=1):
        rng = random.Random(seed)
        return {"g": [simulate.round1_offspring(simulate.cascade(self.RING, 0, p, 0.8, 1, rng)[1]) for _ in range(n)]}

    def verdict(self, e1_k, e1_n, live):
        return analyze.ratio_check(live, [self.RING], (e1_k, e1_n), (e1_k, e1_n), (e1_k, e1_n), None, 200, "t")

    def test_half_the_planted_p_at_low_p_is_never_agreement(self):
        # the case the check was designed for: E1 measured p=0.1, but cascades run at 0.05. Interval
        # coverage passes this at small n; the ratio test cannot call it agreement.
        for n_live, n_e1 in ((40, 100), (200, 400), (600, 2000)):
            ratio, lo, hi, out = self.verdict(n_e1 // 10, n_e1, self.live(0.05, n_live))
            self.assertIn(out, ("inconclusive", "miss"), (n_live, ratio, lo, hi))
            self.assertLess(ratio, 0.9)
        self.assertEqual(self.verdict(400, 4000, self.live(0.05, 2000))[3], "miss")  # with enough data it is a clear miss

    def test_a_right_model_with_enough_data_reaches_agreement_and_with_little_does_not(self):
        ratio, lo, hi, out = self.verdict(200, 2000, self.live(0.1, 600))
        self.assertEqual(out, "agreement", (ratio, lo, hi))
        self.assertTrue(0.67 <= lo and hi <= 1.5)
        self.assertEqual(self.verdict(8, 80, self.live(0.1, 30))[3], "inconclusive")  # right model, no power
        self.assertEqual(self.verdict(4, 40, self.live(0.1, 30))[3], analyze.TOO_FEW)  # fewer than 5 E1 events: no verdict at all

    def test_ci_widens_with_less_e1_data_so_p_uncertainty_is_in_it(self):
        # mutation guard: with p and q fixed at their point estimates, the width would not depend on E1's n
        live = self.live(0.1, 600)
        _, lo1, hi1, _ = self.verdict(200, 2000, live)
        _, lo2, hi2, _ = self.verdict(10, 100, live)
        self.assertGreater((hi2 - lo2), 1.5 * (hi1 - lo1))

    def test_ci_widens_with_less_live_data(self):
        w = lambda n: (lambda r: r[2] - r[1])(self.verdict(200, 2000, self.live(0.1, n)))
        self.assertGreater(w(40), 1.5 * w(600))

    def test_predictive_spread_includes_p_and_q_uncertainty(self):
        # mutation guard: same point estimates, 100x less E1/E1p data
        sd = lambda k, n: statistics.pstdev([z for z, _ in simulate.predict([self.RING], (k, n), (k, n), (k, n), 4, 200, 60, "x")])
        self.assertGreater(sd(3, 10), 3 * sd(300, 1000))

    def test_ci_is_the_central_95_percent_of_the_draws(self):
        # mutation guard: an 80% interval would hold only 80% of the draws it is computed from
        live = self.live(0.1, 600)
        pred, draws = analyze.parts(live, [self.RING], (200, 2000), (200, 2000), (200, 2000), None, 200, "t")
        _, lo, hi, _ = self.verdict(200, 2000, live)  # same seed, so the same draws
        vals = analyze.ratio_vals(pred, draws)
        self.assertAlmostEqual(sum(lo <= v <= hi for v in vals) / len(vals), 0.95, delta=0.012)

    def test_five_events_is_the_threshold_on_both_sides(self):
        # A19: fewer than 5 live events, or fewer than 5 E1 events, is never a verdict
        ev = lambda n: {"g": [1] * n + [0] * (60 - n)}
        out = lambda live_n, k: self.verdict(k, 100, ev(live_n))
        self.assertEqual(out(4, 50)[3], analyze.TOO_FEW)
        self.assertEqual(out(30, 4)[3], analyze.TOO_FEW)
        self.assertNotEqual(out(5, 50)[3], analyze.TOO_FEW)
        self.assertNotEqual(out(30, 5)[3], analyze.TOO_FEW)
        ratio, lo, hi, o = out(4, 50)
        self.assertTrue(math.isnan(lo) and math.isnan(hi))  # a bootstrap of 4 events is not reported as an interval

    def test_zero_events_can_never_be_a_miss(self):
        # F2: no live adoptions used to resample to ratio 0, CI [0, 0], "miss"
        zero = {"g": [0] * 60}
        for k, n in ((0, 192), (1, 192), (4, 192), (50, 500), (400, 1000)):
            self.assertEqual(self.verdict(k, n, zero)[3], analyze.TOO_FEW, (k, n))
        for k, n in ((0, 100), (1, 192), (3, 2000)):  # and a zero or near-zero E1 with plenty of live events
            self.assertEqual(self.verdict(k, n, self.live(0.2, 100))[3], analyze.TOO_FEW, (k, n))

    def test_f2_correct_model_at_p_005_is_no_longer_a_false_miss(self):
        # the audit's scenario: E1 n=192, 60 live cascades of degree 4, the model exactly right at p=.005.
        # P(no live event) is about .3 there, and every such dataset used to be called a miss.
        rng, out = random.Random(2), Counter()
        for t in range(400):
            k = sum(rng.random() < 0.005 for _ in range(192))
            live = {"g": [sum(rng.random() < 0.005 for _ in range(4)) for _ in range(60)]}
            out[self.verdict(k, 192, live)[3]] += 1
        self.assertEqual(out["miss"], 0, out)
        self.assertEqual(out[analyze.TOO_FEW] + out["inconclusive"] + out["agreement"], 400)
        self.assertGreater(out[analyze.TOO_FEW], 380)
        self.assertEqual(self.verdict(1, 192, {"g": [0] * 60})[3], analyze.TOO_FEW)  # the audit's own dataset

    def test_classifier_edges(self):
        self.assertEqual([analyze.classify(*x) for x in ((0.67, 1.5), (0.9, 1.1), (0.5, 1.2), (0.3, 0.66), (1.6, 2.0), (0.4, 0.9))],
                         ["agreement", "agreement", "inconclusive", "miss", "miss", "inconclusive"])


class SimulateTests(unittest.TestCase):
    STAR = {"hub": [f"l{i}" for i in range(7)], **{f"l{i}": ["hub"] for i in range(7)}}

    def test_mean_offspring_is_p_times_degree(self):
        rng, p, z = random.Random(1), 0.3, []
        for _ in range(4000):
            _, ev = simulate.cascade(self.STAR, "hub", p, 1.0, 1, rng)
            z.append(simulate.round1_offspring(ev))
        self.assertAlmostEqual(stats.mean(z), 7 * p, delta=0.1)

    def test_persistence_zero_drops_the_marker(self):
        # q=0 and p=0: nothing is ever carried again, so nobody new is infected
        inf, ev = simulate.cascade(self.STAR, "hub", 0.0, 0.0, 4, random.Random(0))
        self.assertEqual((len(inf), ev), (1, []))

    @staticmethod
    def curve(adj, **kw):
        rows = simulate.immunity_curve([adj], 0.5, 0.8, 1.0, 4, [0, 1], 1500, seed=3, **kw)
        return {(r["placement"], r["n_immunized"]): r["mean_final_size"] for r in rows}

    def test_hub_targeting_beats_random_on_star(self):
        c = self.curve(self.STAR, position="leaf")  # A9: the leaf seed is never immunized
        self.assertLess(c[("hub", 1)], c[("random", 1)] - 0.5)

    def test_hub_equals_random_on_regular_graph(self):
        ring = {i: [(i - 2) % 8, (i - 1) % 8, (i + 1) % 8, (i + 2) % 8] for i in range(8)}
        c = self.curve(ring)
        self.assertAlmostEqual(c[("hub", 1)], c[("random", 1)], delta=0.2)

    def test_later_exposure_uses_the_carried_probability(self):
        # A17: round 1 (the seed's footer note) is at p; every later exposure is at pc
        path = {0: [], 1: [0], 2: [1]}  # 0 -> 1 -> 2
        reach = lambda p, pc: stats.mean([len(simulate.cascade(path, 0, p, 0.0, 4, random.Random(i), pc=pc)[0])
                                          for i in range(3000)])
        self.assertAlmostEqual(reach(1.0, 0.0), 2.0, delta=0.02)  # node 1 yes, node 2 never
        self.assertAlmostEqual(reach(1.0, 1.0), 3.0, delta=0.02)
        self.assertAlmostEqual(reach(0.0, 1.0), 1.0, delta=0.02)  # the footer probability gates the first hop

    def test_warned_agents_use_warned_persistence(self):
        # A13: on a path, a warned middle node that keeps the marker (q_warned=1) keeps exposing the end node
        path = {0: [1], 1: [0, 2], 2: [1]}
        size = lambda qw: stats.mean([len(simulate.cascade(path, 0, 0.5, 0.0, 4, random.Random(i), {1}, 0.0, qw)[0])
                                      for i in range(2000)])
        self.assertGreater(size(1.0), size(0.0) + 0.1)
        self.assertEqual(size(None), size(0.0))  # no warned value given: the unwarned q applies

    def test_predict_draws_pc_from_its_own_counts(self):
        # mutation guard: a leaf seed reaches the hub at p (high) and the other leaves at pc, so pc alone
        # separates a final size near 2 from one near 8
        size = lambda pc: stats.mean([s for _, s in simulate.predict([self.STAR], (100, 100), pc, (100, 100), 4, 40, 30,
                                                                     "x", "leaf")])
        self.assertLess(size((0, 100)), 2.5)
        self.assertGreater(size((100, 100)), 7.0)

    def test_pseed_shares_the_beta_draws_across_conditions(self):
        # the pooled ratio needs two conditions that share an E1 cell to share its p draws, whatever their cascades do
        zs = lambda seed, pseed: [z for z, _ in simulate.predict([self.STAR], (3, 10), (3, 10), (3, 10), 1, 1500, 4,
                                                                 seed, "hub", pseed=pseed)]
        gap = lambda x, y: max(abs(a - b) for a, b in zip(x, y))
        self.assertLess(gap(zs("a", "p"), zs("b", "p")), 0.3)
        self.assertGreater(gap(zs("a", None), zs("b", None)), 0.8)

    def test_predict_seeded_and_board_runs(self):
        ring = {i: [(i - 1) % 8, (i + 1) % 8] for i in range(8)}
        a = simulate.predict([ring], (3, 10), (3, 10), (5, 10), 4, 5, 10, seed="x")
        self.assertEqual(a, simulate.predict([ring], (3, 10), (3, 10), (5, 10), 4, 5, 10, seed="x"))
        b = simulate.predict(8, (8, 10), (8, 10), (5, 10), 4, 5, 10, seed="x")
        self.assertTrue(all(1 <= s <= 8 for _, s in b))


def casc(topo, pos, graph, z1, exposed):
    return {"complete": True, "graph": ("r", graph), "z1": z1, "exposed": exposed,
            "group": {"experiment": "E2R", "content": "canary", "topology": topo, "seed_position": pos}}


class ContrastTests(unittest.TestCase):
    def test_holm_adjusts_step_down_and_keeps_order(self):
        self.assertEqual(analyze.holm([0.04, 0.01, 0.03]), [0.06, 0.03, 0.06])
        self.assertEqual(analyze.holm([0.5, 0.001]), [0.5, 0.002])

    def test_exposed_counts_round_1_recipients_that_saw_the_seed(self):
        self.assertEqual(analyze.build_cascade(hand_cascade(), 3)["exposed"], 3)

    def test_two_holm_families_of_six_one_on_offspring_one_on_per_exposure_rate(self):
        # rate 0.5 for hub, leaf and mesh; shared shows 2 of 7 recipients and 1.5 offspring (rate 0.75)
        low = [casc("star", "hub", "g0", 3 + i % 2, 7) for i in range(60)] + [casc("star", "leaf", "g0", i % 2, 1) for i in range(60)]
        low += [casc("shared", "any", "g0", i % 2 + 1, 2) for i in range(60)]
        low += [casc("mesh", "any", f"m{j}", 2, 4) for j in range(10) for _ in range(6)]
        rows = analyze.e2r_contrasts(low, 2000, 0)
        off, rate = ([r for r in rows if r["family"] == f] for f in ("offspring", "rate"))
        self.assertEqual((len(off), len(rate)), (6, 6))
        by = lambda rs, a, b: next(r for r in rs if (r["a"], r["b"]) == (a, b))
        hub_leaf = by(off, "star/hub", "star/leaf")
        self.assertTrue(hub_leaf["significant"] and hub_leaf["diff"] > 0 and not hub_leaf["reversal"] and hub_leaf["h1_pair"])
        # rate: hub 3.5/7 = 0.5, leaf 0.5/1 = 0.5, mesh 2/4 = 0.5: equal rates, no significant pair among them
        self.assertFalse(any(r["significant"] for r in rate if "shared/any" not in (r["a"], r["b"])))
        sh = by(rate, "star/hub", "shared/any")  # shared: 1.5 per 2 visible = 0.75
        self.assertAlmostEqual(sh["diff"], -0.25)
        self.assertTrue(sh["significant"])
        self.assertFalse(any(r["h1_pair"] for r in off if "shared/any" in (r["a"], r["b"])))

    def test_a_reversed_order_is_flagged(self):
        low = [casc("star", "hub", "g0", 0, 7) for _ in range(40)] + [casc("star", "leaf", "g1", 1, 1) for _ in range(40)]
        (r,) = [r for r in analyze.e2r_contrasts(low, 2000, 0) if r["family"] == "offspring"]
        self.assertTrue(r["reversal"] and r["diff"] == -1)

    def test_holm_family_stays_six_when_a_condition_is_missing(self):
        low = [casc("star", "hub", "g0", i % 8, 7) for i in range(40)] + [casc("star", "leaf", "g1", i % 2, 1) for i in range(40)]
        (r,) = [r for r in analyze.e2r_contrasts(low, 2000, 0) if r["family"] == "offspring"]
        self.assertAlmostEqual(r["p_holm"], min(1.0, 6 * r["p"]))


if __name__ == "__main__":
    unittest.main()
