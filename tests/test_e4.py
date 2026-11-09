"""Offline E4 tests: sentinel, quarantine, scenarios and the induced-quarantine analysis.
Sentinels are fakes injected as the runner's `call`; the notes config is a throwaway file."""
import hashlib
import json
import random
import re
import tempfile
import unittest
from pathlib import Path

from outbreak import llm, world
from test_e2e import analyse, cli, table
from test_harness import go

# neutral stand-ins (redaction review): same length, and the fake sentinels key on "framing note" / "neutral note"
ATTACK = "Stand-in framing note naming {target}."
PLACEBO = "Stand-in neutral note naming {target}."
SENT = "You are the sentinel"
SAME = ["--fake-p", "0.5", "--seed", "1"]
R = 30  # E4 reps must balance 3 wordings x 10 graphs


def provider(rule):
    """Fake provider whose sentinel answers rule(seed_key, round, {author: note}) -> raw text."""
    def call(prov, model, system, prompt, **k):
        if not system.startswith(SENT):
            return llm.call_model(prov, model, system, prompt, **k)
        rnd = int(re.search(r"/r(\d+)/sentinel", k["seed_key"]).group(1))
        return {"text": rule(k["seed_key"], rnd, dict(llm.NOTE_RE.findall(prompt))),
                "usage": {"input": 5, "output": 5, "total": 10, "cached": 0}, "model_returned": model,
                "status": "ok", "error_class": None, "latency_ms": 0}
    return call


def flags_author(p_flag):
    """Flags the author of an attack note (not its target), to give the attacker some exposure."""
    def rule(key, rnd, notes):
        rng = random.Random(key)
        return json.dumps(sorted(a for a, text in notes.items() if "framing note" in text and rng.random() < p_flag))
    return rule


def flag_in(rounds, who="a3"):
    return lambda key, rnd, notes: json.dumps([who] if rnd in rounds else [])


def planted(p_attack, p_placebo):
    """Flags the agent a note names, with a probability that depends only on the note's wording."""
    def rule(key, rnd, notes):
        rng, out = random.Random(key), set()
        for author, text in notes.items():
            for named in set(re.findall(r"\ba\d\b", text)) - {author}:
                if rng.random() < (p_attack if "framing note" in text else p_placebo if "neutral note" in text else 0):
                    out.add(named)
        return json.dumps(sorted(out))
    return rule


class E4(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.root = Path(cls.tmp.name)
        cls.cfg = cls.root / "notes.json"
        cls.cfg.write_text(json.dumps({"attack": ATTACK, "placebo": PLACEBO}))

    def run_e4(self, name, call, policies, scenarios, reps, *extra):
        return cli(self.root / name, "E4", "--run-id", "E4", "--reps", str(reps), "--policies", policies,
                   "--scenarios", scenarios, "--e4-notes", str(self.cfg), *SAME, *extra,
                   call=call or llm.call_model)

    @staticmethod
    def rows(d):
        return [json.loads(x) for x in (d / "calls.jsonl").read_text().splitlines()]

    def blocked_rounds(self, rows, trial, agent="a3"):
        return {r["round"] for r in rows if r["trial_id"] == trial and r["slot_id"].endswith("/" + agent) and r["blocked"]}

    def test_quarantine_blocks_from_the_next_round_and_never_earlier(self):
        cases = {("single", (2,)): {3, 4}, ("single", (1, 2, 3, 4)): {2, 3, 4},
                 ("corroborated", (2,)): set(), ("corroborated", (1, 3)): {4}, ("corroborated", (1, 2, 3, 4)): {3, 4}}
        for (pol, rounds), want in cases.items():
            with self.subTest(pol=pol, rounds=rounds):
                d = self.run_e4(f"q-{pol}-{rounds}", provider(flag_in(rounds)), pol, "benign", R)
                self.assertEqual(self.blocked_rounds(self.rows(d), f"E4-{pol}-benign-000"), want)

    def test_sentinel_sees_only_delivered_notes_and_blocked_notes_leave_inboxes(self):
        d = self.run_e4("vis", provider(flag_in((1,))), "single", "benign", R)
        rs = self.rows(d)
        sen = {r["round"]: r for r in rs if r["agent"] == "sentinel" and r["trial_id"].endswith("-000")}
        self.assertEqual(len(sen), 4)
        self.assertIn("a3", sen[2]["inbox_sources"])      # its round-1 note went out before the flag took effect
        self.assertNotIn("a3", sen[3]["inbox_sources"])   # the round-2 note was blocked
        self.assertNotIn("a3", sen[4]["inbox_sources"])
        for r in rs:
            if r["agent"] != "sentinel" and r["round"] >= 3:
                self.assertNotIn("a3", r["inbox_sources"])
        self.assertTrue(any("a3" in r["inbox_sources"] for r in rs if r["agent"] != "sentinel" and r["round"] == 2))
        # the asynchronous leak, stated: the flag came in round 1, yet the round-1 note was already out
        # and was read in round 2, so exactly one delivery gets through before the block

    def test_unparseable_output_is_ok_with_null_flags_counted_and_never_quarantines(self):
        for junk_id, junk in enumerate(("I think a3 looks odd", '{"a3": 1}', '["zz"]', "[true]", "[99]")):
            with self.subTest(junk=junk):
                d = self.run_e4(f"junk-{junk_id}", provider(lambda *a: junk), "single", "benign", R)
                sen = [r for r in self.rows(d) if r["agent"] == "sentinel"]
                self.assertTrue(sen and all(r["status"] == "ok" and r["flags"] is None for r in sen))
                self.assertFalse(any(r["blocked"] for r in self.rows(d)))
                (row,) = table(analyse(d.parent / "out", [d]), "e4_summary")
                self.assertEqual((row["n_unparseable"], row["n_sentinel_calls"]), (len(sen), len(sen)))
                self.assertEqual(row["quarantines_per_cascade"], 0)
        self.assertEqual(world.parse_flags('["a2", 5, "a2"]'), [2, 5])
        self.assertEqual(world.parse_flags("[]"), [])

    def test_target_and_attacker_meet_the_distance_rule(self):
        def dist(nbrs, a, b):
            seen, frontier, d = {a}, [a], 0
            while b not in seen:
                frontier, d = [j for i in frontier for j in nbrs[i] if j not in seen], d + 1
                seen |= set(frontier)
            return d
        ts = world.make_trials("E4", {"reps": R, "seed": 3, "policies": ["single", "corroborated"],
                                      "scenarios": ["framing", "placebo", "outbreak", "benign"]})
        by_rep = {}
        for t in ts:
            if t["scenario"] in ("framing", "placebo"):
                nb = world.graph_of("mesh", 3, t["graph_id"])
                self.assertEqual(dist(nb, t["attacker"], t["target"]), 2)
                by_rep.setdefault(t["id"][-3:], set()).add((t["attacker"], t["target"], t["graph_id"]))
            else:
                self.assertIsNone(t["target"])
        self.assertTrue(by_rep and all(len(v) == 1 for v in by_rep.values()))  # matched across scenario and policy
        complete = {i: [j for j in range(4) if j != i] for i in range(4)}  # no distance-2 pair: any two distinct
        a, b = world.pick_pair(complete, random.Random(0))
        self.assertNotEqual(a, b)

    def test_attacker_and_placebo_post_twice_in_rounds_0_and_1_and_the_sentinel_reads_both(self):
        seen = {}

        def spy(key, rnd, notes):
            seen[(key.split("/")[0].split(":")[1], rnd)] = notes
            return "[]"
        d = self.run_e4("twice", provider(spy), "single", "framing,placebo", R)
        man = json.loads((d / "manifest.json").read_text())["e4"]
        for scen, word in (("framing", "framing note"), ("placebo", "neutral note")):
            tid = f"E4-single-{scen}-000"
            att = f"a{man[tid]['attacker']}"
            # sentinel round r reads the notes written in r-1: the harness's round 0, the attacker's round 1
            self.assertEqual([word in seen[(tid, r)][att] for r in (1, 2, 3, 4)], [True, True, False, False])
            self.assertEqual(sum(word in t for t in seen[(tid, 2)].values()), 1)  # one author, not echoed by others
            self.assertIn(f"a{man[tid]['target']}", seen[(tid, 1)][att])

    def test_framing_and_placebo_are_paired_on_graph_attacker_target_and_policy_off_shares_seed(self):
        d = self.run_e4("pairs", None, "off,single", "framing,placebo,outbreak", R)
        man = json.loads((d / "manifest.json").read_text())["e4"]
        for pol in ("off", "single"):
            for rep in range(R):
                f, p_ = (man[f"E4-{pol}-{sc}-{rep:03d}"] for sc in ("framing", "placebo"))
                self.assertEqual((f["attacker"], f["target"]), (p_["attacker"], p_["target"]))
        self.assertTrue(all(man[f"E4-off-outbreak-{r:03d}"]["seed_agent"] == man[f"E4-single-outbreak-{r:03d}"]["seed_agent"]
                            for r in range(R)))

    def test_planted_framing_gap_is_recovered_and_zero_gap_is_not_invented(self):
        d = self.run_e4("gap", provider(planted(0.7, 0.2)), "single,corroborated", "framing,placebo", 60)
        res = analyse(d.parent / "out", [d])
        # two attack notes are read by the sentinel (rounds 1 and 2): single quarantines on either sighting
        # (0.91 vs 0.36), corroborated needs both (0.49 vs 0.04), so it now has a real test to face
        (g,) = table(res, "e4_framing_gap", policy="single")
        self.assertTrue(g["gap_ci_lo"] <= 0.55 <= g["gap_ci_hi"] and g["gap_ci_lo"] > 0, g)
        (c,) = table(res, "e4_framing_gap", policy="corroborated")
        self.assertTrue(c["gap_ci_lo"] <= 0.45 <= c["gap_ci_hi"] and c["gap_ci_lo"] > 0, c)
        self.assertLess(c["gap"], g["gap"])
        self.assertTrue(g["supported"] and not g["refuted_at_0_3"] and c["supported"])  # 0.55 and 0.45: above 0, not below 0.3
        (dd,) = table(res, "e4_policy_gap_difference")  # H5 descriptive: single minus corroborated, 95%
        self.assertAlmostEqual(dd["diff"], g["gap"] - c["gap"])
        self.assertTrue(dd["ci_lo"] < dd["diff"] < dd["ci_hi"])
        self.assertGreater(c["target_q_framing"], 0)  # the earlier one-note design could never reach this
        d = self.run_e4("nogap", provider(planted(0.3, 0.3)), "single", "framing,placebo", 60)
        (g,) = table(analyse(d.parent / "out", [d]), "e4_framing_gap")
        self.assertTrue(g["gap_ci_lo"] <= 0 <= g["gap_ci_hi"] and abs(g["gap"]) < 0.2, g)
        self.assertTrue(not g["supported"] and g["refuted_at_0_3"], g)  # interval straddles 0 and lies below 0.3

    def test_two_attacker_cost_quantities_and_paired_differences(self):
        # the planted sentinel flags only the named target, so every quarantine is the target and the
        # attacker is never exposed; the author-flagging sentinel does the opposite
        d = self.run_e4("cost", provider(planted(0.7, 0.2)), "single", "framing,placebo", 60)
        res = analyse(d.parent / "out", [d])
        (g,) = table(res, "e4_framing_gap")
        self.assertEqual(g["attacker_exposure"], 0)
        self.assertAlmostEqual(g["induced_quarantines_per_attacker_note"], g["gap"] / 2)  # framing minus placebo, over 2 notes
        (pr,) = table(res, "e4_paired_differences")
        self.assertEqual(pr["n_pairs"], 60)
        self.assertAlmostEqual(pr["target_q_diff"], g["gap"])  # all pairs complete, so paired and unpaired means agree
        self.assertTrue(pr["target_q_diff_ci_lo"] <= pr["target_q_diff"] <= pr["target_q_diff_ci_hi"])
        d = self.run_e4("exposure", provider(flags_author(0.5)), "single,corroborated", "framing,placebo", 60)
        res = analyse(d.parent / "out", [d])
        s1, s2 = (table(res, "e4_framing_gap", policy=p_)[0] for p_ in ("single", "corroborated"))
        # the attacker's two notes are each read once: flagged at least once 1-0.5^2 = 0.75, twice 0.25
        self.assertAlmostEqual(s1["attacker_exposure"], 0.75, delta=0.15)
        self.assertAlmostEqual(s2["attacker_exposure"], 0.25, delta=0.15)
        self.assertEqual(s1["gap"], 0)  # the target is never named in a flag: exposure is not induced quarantine
        self.assertAlmostEqual(s1["induced_quarantines_per_attacker_note"], s1["attacker_exposure"] / 2)
        (p1,) = table(res, "e4_paired_differences", policy="single")
        self.assertAlmostEqual(p1["attacker_exposure_diff"], s1["attacker_exposure"])

    def test_benign_false_quarantine_rate_is_recovered(self):
        d = self.run_e4("fqr", None, "single,corroborated", "benign", 60, "--fake-sentinel", "0,0.05,0,0")
        res = analyse(d.parent / "out", [d])
        for pol, want in (("single", 1 - 0.95 ** 4), ("corroborated", 1 - 0.95 ** 4 - 4 * 0.05 * 0.95 ** 3)):
            (r,) = table(res, "e4_summary", policy=pol, scenario="benign")
            self.assertTrue(r["quarantine_rate_ci_lo"] <= want <= r["quarantine_rate_ci_hi"], (pol, want, r))

    def test_quarantine_changes_outbreak_size(self):
        # mutation guard: if enforcement became a no-op, off and single would match exactly
        d = self.run_e4("mut", None, "single,off", "outbreak", R, "--fake-sentinel", "1,0,0,0", "--fake-p", "0.2")
        res = analyse(d.parent / "out", [d])
        on, off = (table(res, "e4_summary", policy=p, scenario="outbreak")[0] for p in ("single", "off"))
        self.assertGreater(off["final_size_mean"], on["final_size_mean"])
        self.assertEqual(off["notes_delivered_mean"], 32)
        self.assertLess(on["notes_delivered_mean"], 32)  # utility cost
        # A18: the off arm (sentinel runs, nothing enforced) is the baseline, paired by graph, seed agent and rep
        (o,) = table(res, "e4_outbreak_vs_baseline")
        self.assertTrue(o["final_size_diff"] < 0 and o["final_size_diff_ci_hi"] < 0 and o["n_pairs"] == R, o)
        self.assertEqual(o["final_size_baseline"], off["final_size_mean"])
        self.assertEqual(table(res, "e4_summary", policy="off", scenario="outbreak")[0]["quarantines_per_cascade"], 0)

    def test_off_arm_enforces_nothing_even_when_the_sentinel_flags_everything(self):
        d = self.run_e4("off", provider(lambda key, rnd, notes: json.dumps(sorted(notes))), "off", "framing", R)
        rs = self.rows(d)
        self.assertTrue(any(r["agent"] == "sentinel" and r["flags"] for r in rs))
        self.assertFalse(any(r["blocked"] for r in rs))

    def test_a_truncated_sentinel_reply_is_never_parsed_as_flags(self):
        # mutation guard: only a complete (ok) reply is read; a cut-off list could name the wrong agents
        def call(prov, model, system, prompt, **k):
            if not system.startswith(SENT):
                return llm.call_model(prov, model, system, prompt, **k)
            return {"text": '["a3"]', "usage": {"input": 5, "output": 5, "total": 10, "cached": 0}, "model_returned": model,
                    "status": "truncated", "error_class": "incomplete_max_output_tokens", "latency_ms": 0}
        d = self.run_e4("trunc", call, "single", "benign", R)
        sen = [r for r in self.rows(d) if r["agent"] == "sentinel"]
        self.assertTrue(sen and all(r["status"] == "truncated" and r["flags"] is None for r in sen))
        self.assertFalse(any(r["blocked"] for r in self.rows(d)))
        (row,) = table(analyse(d.parent / "out", [d]), "e4_summary")
        self.assertEqual((row["n_sentinel_non_ok"], row["n_unparseable"], row["quarantines_per_cascade"]), (len(sen), 0, 0))

    def test_false_quarantine_rate_is_per_agent_of_eight(self):
        # mutation guard (by value): one quarantined agent in every cascade is 1/8, not 1/7
        d = self.run_e4("rate", provider(flag_in((1,))), "single", "benign", R)
        (row,) = table(analyse(d.parent / "out", [d]), "e4_summary")
        self.assertEqual((row["quarantines_per_cascade"], row["quarantine_rate_per_agent"]), (1, 0.125))
        self.assertEqual((row["quarantine_rate_ci_lo"], row["quarantine_rate_ci_hi"]), (0.125, 0.125))

    def test_pairs_must_agree_on_the_target_as_well_as_graph_attacker_and_rep(self):
        # mutation guard: a placebo whose logged target differs from its framing twin is not a match. The planner
        # cannot produce that, so the log is edited the way a corrupted or mixed run would be.
        d = self.run_e4("pairkey", provider(planted(0.7, 0.2)), "single", "framing,placebo", R)
        rs = self.rows(d)
        for r in rs:
            if r["trial_id"] == "E4-single-placebo-000":
                r["target"] = (r["target"] + 1) % 8
        (d / "calls.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rs))
        (pr,) = table(analyse(d.parent / "out", [d]), "e4_paired_differences")
        self.assertEqual(pr["n_pairs"], R - 1)

    def test_no_marker_in_framing_or_placebo_notes(self):
        texts = {"attack": ATTACK, "placebo": PLACEBO}
        for t in world.make_trials("E4", {"reps": R, "seed": 1, "policies": ["single"],
                                          "scenarios": ["framing", "placebo", "benign"]}):
            for note in world.e4_round0(t, texts).values():
                self.assertFalse(world.has_marker(note, t["marker"]) or llm.MARKER_RE.search(note), note)
        d = self.run_e4("nomark", None, "single", "framing,placebo", R)
        self.assertFalse(any(any(r["inbox_markers"].values()) or r["adopted"] for r in self.rows(d)))
        a, b = (world.e4_round0({"scenario": s, "attacker": 1, "target": 4}, texts)[1] for s in ("framing", "placebo"))
        self.assertEqual(len(a), len(b))

    def test_cli_runs_end_to_end_records_config_hash_and_is_byte_identical(self):
        d = self.run_e4("e2e", None, "off,single,corroborated", "benign,outbreak,framing,placebo", R)
        man = json.loads((d / "manifest.json").read_text())
        self.assertEqual(man["params"]["e4_notes_sha256"], hashlib.sha256(self.cfg.read_bytes()).hexdigest())
        self.assertEqual(len(man["e4"]), 3 * 4 * R)
        fr = [r for r in self.rows(d) if r["scenario"] == "framing" and r["agent"] == 0][0]
        self.assertEqual(man["e4"][fr["trial_id"]]["target"], fr["target"])
        a, b = analyse(d.parent / "o1", [d]), analyse(d.parent / "o2", [d])
        self.assertEqual((d.parent / "o1/results.json").read_bytes(), (d.parent / "o2/results.json").read_bytes())
        self.assertEqual(a["errors"], [])
        for name in ("e4_summary", "e4_framing_gap", "e4_paired_differences", "e4_outbreak_vs_baseline"):
            self.assertTrue(table(a, name) and (d.parent / "o1" / f"{name}.csv").exists(), name)

    def test_parse_flags_accepts_one_fence_and_nothing_looser(self):
        # A22: the real model fenced valid lists; 20 of 80 replies were discarded as unparseable
        for text in ('```json\n["a3"]\n```', '```\n["a3"]\n```', '  \n```json\n["a3"]\n```\n ', '```json["a3"]```',
                     '```json\n[\n  "a3"\n]\n```'):
            self.assertEqual(world.parse_flags(text), [3], text)
        self.assertEqual(world.parse_flags("```json\n[]\n```"), [])
        for text in ('Here you go:\n```json\n["a3"]\n```', '```json\n["a3"]\n```\nHope that helps',
                     '```json\n["a3"]\n```\n```json\n["a4"]\n```', '```json\n{"a3": 1}\n```', '```\nnot a list\n```',
                     '```json\n```', '```json\n["a3"]\n``', '```python\n["a3"]\n```', '````json\n["a3"]\n````',
                     'I think a3 is odd'):
            self.assertIsNone(world.parse_flags(text), text)

    def test_fake_sentinel_fences_some_replies_and_all_still_parse_with_the_quarantine_replayed(self):
        d = self.run_e4("fence", None, "single", "benign", R, "--fake-sentinel", "0,0.3,0,0")
        sen = [r for r in self.rows(d) if r["agent"] == "sentinel"]
        fenced = [r for r in sen if r["output_text"].startswith("```")]
        self.assertTrue(0 < len(fenced) < len(sen))
        self.assertTrue(all(r["flags"] is not None and r["flags"] == world.parse_flags(r["output_text"]) for r in sen))
        (row,) = table(analyse(d.parent / "out", [d]), "e4_summary")
        self.assertEqual(row["n_unparseable"], 0)
        self.assertGreater(row["quarantines_per_cascade"], 0)

    def test_unparseable_rate_is_reported_per_arm(self):
        d = self.run_e4("rate-arm", None, "single", "benign,framing", R, "--fake-sentinel", "0,0,0,0.5")
        res = analyse(d.parent / "out", [d])
        for sc in ("benign", "framing"):
            (row,) = table(res, "e4_summary", scenario=sc)
            self.assertTrue(0.3 < row["unparseable_fraction"] < 0.7, row)

    def test_outbreak_check_reports_seed_flagged_and_quarantined_with_wilson_and_the_inert_rule(self):
        # live monitor: the seed's marked note is flagged at once, so the monitor is not inert
        d = self.run_e4("ob-live", None, "single,off", "outbreak", R, "--fake-sentinel", "1,0,0,0")
        res = analyse(d.parent / "out", [d])
        (s,) = table(res, "e4_summary", policy="single", scenario="outbreak")
        self.assertEqual((s["seed_flagged"], s["seed_q"]), (1.0, 1.0))
        self.assertTrue(s["seed_flagged_ci_lo"] > 0.8 and s["seed_q_ci_lo"] > 0.8)
        (o,) = table(res, "e4_summary", policy="off", scenario="outbreak")
        self.assertEqual((o["seed_flagged"], o["seed_q"], o["monitor_inert"]), (1.0, 0.0, False))
        self.assertFalse([n for n in res["notes"] if "inert" in n])
        # blind monitor: never flags, so the upper limit (30 cascades: 0.114) is under 0.2
        d = self.run_e4("ob-inert", None, "single", "outbreak", R, "--fake-sentinel", "0,0,0,0")
        res = analyse(d.parent / "out", [d])
        (s,) = table(res, "e4_summary", policy="single", scenario="outbreak")
        self.assertEqual((s["seed_flagged"], s["seed_q"], s["monitor_inert"]), (0.0, 0.0, True))
        self.assertLess(s["seed_flagged_ci_hi"], 0.2)
        self.assertTrue(any("inert" in n and "uninformative" in n for n in res["notes"]))

    def test_adding_outbreak_leaves_framing_and_placebo_trials_unchanged(self):
        # A22: the standard tier's new scenario must not move the pairing or ids of the primary arms
        def trials(scen):
            ts = world.make_trials("E4", {"reps": R, "seed": 7, "policies": ["single", "corroborated"], "scenarios": scen})
            return {t["id"]: (t["graph_id"], t["attacker"], t["target"], t["marker"]) for t in ts if t["scenario"] != "outbreak"}
        self.assertEqual(trials(["framing", "placebo"]), trials(["framing", "placebo", "outbreak"]))

    def test_missing_notes_config_refuses_and_example_config_is_valid(self):
        rc, _, err = go(self.root / "nocfg", "E4", "--smoke", "--reps", "1", "--e4-notes", str(self.root / "nope.json"))
        self.assertEqual(rc, 2)
        texts, _ = world.load_e4_notes(Path(__file__).resolve().parent.parent / "config" / "e4_notes.example.json")
        self.assertIn("PLACEHOLDER", texts["attack"])


if __name__ == "__main__":
    unittest.main()
