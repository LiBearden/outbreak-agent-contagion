import contextlib
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from outbreak import analyze, guards, llm, runner, world

FIELDS = ("slot_id run_id experiment trial_id round agent condition topology seed_position "
          "inbox_sources inbox_markers marker output_text adopted status error_class attempts "
          "model_requested model_returned provider temperature usage latency_ms prompt_sha256 ts "
          "graph_id seed_agent board_order failed_attempt_tokens instructions input").split()
MANIFEST = ("run_id experiment git_sha git_dirty prereg_sha params planned_slots_sha256 "
            "rng_seed graphs python_version created_ts smoke fake").split()
STAR = ["--topologies", "star", "--contents", "canary", "--reps", "3"]  # 3 = one cell of each wording
SHA = "abc" + "0" * 37


def clock():
    t = [0.0]

    def sleep(d):
        t[0] += d
    return (lambda: t[0]), sleep, t


def go(runs, *args, auto=True, **inject):
    if auto and "--smoke" not in args and "--prereg" not in args:  # pass the freeze guard
        files = frozen_setup(list(args))
        args, inject["git"] = (*args, "--prereg", SHA[:12]), fake_git(files, files)
    now, sleep, _ = clock()
    inject.setdefault("sleep", sleep)
    inject.setdefault("now", now)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = runner.main([*args, "--runs-dir", str(runs)], **inject)
    return rc, out.getvalue(), err.getvalue()


def rows(runs):
    (d,) = Path(runs).iterdir()
    return [json.loads(x) for x in (d / "calls.jsonl").read_text().splitlines()]


def fake_git(commit_files, work_files):
    """Stands in for git: `show`, `cat-file -e`, `ls-tree`, `ls-files` and `diff --quiet` against a dict of files; every
    ref resolves to SHA and is an ancestor of HEAD (the real-repo tests cover refs and ancestry)."""
    def git(repo, *args):
        if args[0] == "rev-parse":
            return 0, SHA + "\n"
        if args[0] == "merge-base":
            return 0, ""
        if args[0] == "show":
            f = args[1].split(":", 1)[1]
            return (0, commit_files[f]) if f in commit_files else (1, "")
        if args[0] == "cat-file":
            return (0 if args[2].split(":", 1)[1] in commit_files else 1), ""
        if args[0] == "ls-tree":
            return 0, "\n".join(commit_files)
        if args[0] == "ls-files":
            return 0, "\n".join(work_files)
        paths = args[args.index("--") + 1:]
        return int(any(commit_files.get(p) != work_files.get(p) for p in paths)), ""
    return git


def frozen_setup(argv, status="**Status: FROZEN", tweak=None):
    a = runner.parse_args(argv)
    block = prereg_block(runner.build_params(a), runner.plan(a)[3], tweak)
    return {"PREREG.md": f"{status}\n```prereg-params\n{block}\n```\n", **dict.fromkeys(guards.REQUIRED, "x")}


def prereg_block(params, planned_sha, tweak=None):
    """Every run parameter, plus this experiment's planned-slot hash scoped to it (A16)."""
    tweak = dict(tweak or {})
    planned_sha = tweak.pop("planned", planned_sha)
    return "\n".join([f"{k}={v}" for k, v in {**params, **tweak}.items()] +
                      [f"{params['experiment']}.planned_slots_sha256={planned_sha}"])


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runs = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def test_fake_run_writes_schema(self):
        for exp in ("E1", "E1p", "E2", "E3"):
            with self.subTest(exp=exp), tempfile.TemporaryDirectory() as d:
                self.assertEqual(go(d, exp, "--smoke", "--reps", "1")[0], 0)
                rs = rows(d)
                self.assertTrue(rs)
                for r in rs:
                    self.assertEqual(sorted(r), sorted(FIELDS))
                    self.assertEqual(sorted(r["usage"]), ["cached", "input", "output", "reasoning", "total"])
                (m,) = [json.loads(p.read_text()) for p in Path(d).glob("*/manifest.json")]
                self.assertEqual(sorted(m), sorted(MANIFEST))

    def test_prompt_hash_recomputes_from_logged_prompts(self):  # A21
        for exp in ("E1", "E2", "E4"):
            with self.subTest(exp=exp), tempfile.TemporaryDirectory() as d:
                # the real E4 notes are private, so a public clone must pass on the example file
                notes = ("--e4-notes", str(Path(__file__).resolve().parents[1] / "config/e4_notes.example.json"))
                self.assertEqual(go(d, exp, "--smoke", "--reps", "1", *(notes if exp == "E4" else ()))[0], 0)
                rs = rows(d)
                self.assertTrue(rs)
                for r in rs:
                    self.assertTrue(r["instructions"] and r["input"])
                    self.assertEqual(hashlib.sha256((r["instructions"] + "\n\n" + r["input"]).encode()).hexdigest(),
                                     r["prompt_sha256"])

    def test_cascade_stops_early_and_is_seeded(self):
        go(self.runs, "E2", *STAR, "--fake-p", "0")
        rs = rows(self.runs)
        # nobody adopts, so the 2 quiet rounds end each cascade after round 2
        self.assertEqual({r["round"] for r in rs}, {1, 2})
        self.assertEqual(len(rs), 3 * 2 * 2 * world.N)  # 3 reps x (hub, leaf)
        with tempfile.TemporaryDirectory() as d2:
            go(d2, "E2", *STAR, "--fake-p", "0")
            self.assertEqual([r["output_text"] for r in rs], [r["output_text"] for r in rows(d2)])

    def test_resume_never_reissues_and_refuses_duplicates(self):
        issued = []

        def spy(*a, **k):
            issued.append(k["seed_key"])
            return llm.call_model(*a, **k)
        rc, out, _ = go(self.runs, "E2", *STAR, "--max-total-tokens", "1500", call=spy)
        self.assertIn("stop=token budget", out)
        first = len(issued)
        self.assertTrue(0 < first < 192)
        go(self.runs, "E2", *STAR, "--max-total-tokens", "100000", call=spy)
        self.assertEqual(len(set(issued)), len(issued))  # no slot called twice
        ids = [r["slot_id"] for r in rows(self.runs)]
        self.assertEqual(len(set(ids)), len(ids))
        before = len(issued)
        go(self.runs, "E2", *STAR, "--max-total-tokens", "100000", call=spy)
        self.assertEqual(len(issued), before)
        (d,) = Path(self.runs).iterdir()
        line = (d / "calls.jsonl").read_text().splitlines()[0]
        with open(d / "calls.jsonl", "a") as f:
            f.write(line + "\n")
        rc, _, err = go(self.runs, "E2", *STAR, call=spy)
        self.assertEqual(rc, 2)
        self.assertIn("duplicate", err)

    def test_unplanned_rows_reported_not_merged(self):
        go(self.runs, "E2", *STAR, "--max-total-tokens", "1500")
        (d,) = Path(self.runs).iterdir()
        r = rows(self.runs)[0] | {"slot_id": "bogus/r1/a0"}
        with open(d / "calls.jsonl", "a") as f:
            f.write(json.dumps(r) + "\n")
        rc, _, err = go(self.runs, "E2", *STAR)
        self.assertIn("unplanned rows (not merged): 1", err)

    def test_budget_stop(self):
        rc, out, _ = go(self.runs, "E1", "--max-total-tokens", "600", "--reps", "1")
        self.assertIn("stop=token budget", out)
        rs = rows(self.runs)
        self.assertTrue(rs)
        self.assertLessEqual(sum(r["usage"]["total"] for r in rs), 600)

    def test_rate_limit_without_real_sleep(self):
        now, sleep, t = clock()
        wait = runner.make_limiter(30, now, sleep)
        for _ in range(6):
            wait()
        self.assertAlmostEqual(t[0], 10.0)  # 5 gaps of 2 s at 30 rpm
        _, sleep2, t2 = clock()
        go(self.runs, "E1", "--smoke", "--rpm", "60", sleep=sleep2, now=lambda: t2[0])
        self.assertAlmostEqual(t2[0], 19.0)  # 20 calls, 19 gaps of 1 s

    def test_retries_are_bounded_and_counted(self):
        def bad(*a, **k):
            raise RuntimeError("boom")
        go(self.runs, "E1", "--smoke", "--retries", "2", call=bad)
        rs = rows(self.runs)
        self.assertTrue(all(r["status"] == "error" and r["attempts"] == 3 and
                            r["error_class"] == "RuntimeError" for r in rs))

    def test_guards_refuse_and_accept(self):
        argv = ["E2", *STAR]
        good = frozen_setup(argv)
        work = dict(good)
        run = lambda files, w, extra=(): go(self.runs, *argv, "--prereg", SHA[:12], *extra,
                                            git=fake_git(files, w))
        self.assertEqual(go(self.runs, *argv, auto=False)[0], 2)  # no --prereg
        rc, _, err = run(frozen_setup(argv, status="**Status: DRAFT"), work)
        self.assertEqual(rc, 2)
        self.assertIn("not FROZEN", err)
        rc, _, err = run(frozen_setup(argv, tweak={"reps": 9}), work)
        self.assertIn("reps=9", err)
        rc, _, err = run(frozen_setup(argv, tweak={"planned": "0" * 64}), work)  # A16: same params, other design
        self.assertIn("planned_slots_sha256=", err)
        rc, _, err = run(good, {**work, "outbreak/world.py": "changed"})
        self.assertIn("frozen code differs", err)
        self.assertEqual(run(good, work)[0], 0)
        (m,) = [json.loads(p.read_text()) for p in Path(self.runs).glob("*/manifest.json")]
        self.assertEqual(m["prereg_sha"], SHA)  # the resolved full SHA is what gets stored
        self.assertFalse(run(good, work, ("--model", "other"))[0] == 0)

    def test_s4_partial_block_is_refused(self):
        # audit script s4: a block holding only `experiment=E2` used to pass for rounds=9, temperature=1.5
        a = runner.parse_args(["E2", *STAR, "--temperature", "1.5", "--rounds", "9"])
        files = {"PREREG.md": "**Status: FROZEN\n```prereg-params\nexperiment=E2\n```\n"}
        with self.assertRaisesRegex(guards.GuardError, "lacks .*rounds.*temperature"):
            guards.check_prereg(SHA[:12], runner.build_params(a), runner.plan(a)[3], "PREREG.md", ".",
                                fake_git(files, files))

    def test_wrong_round_count_is_caught(self):
        # mutation guard: a cascade that never stops early must log exactly T rounds
        go(self.runs, "E2", *STAR, "--fake-p", "1")
        by = {}
        for r in rows(self.runs):
            by.setdefault(r["trial_id"], []).append(r)
        leaf = [v for k, v in by.items() if "-leaf-" in k]
        self.assertTrue(leaf)
        self.assertTrue(all({r["round"] for r in v} == {1, 2, 3, 4} and len(v) == 4 * world.N for v in leaf))
        with tempfile.TemporaryDirectory() as d:
            go(d, "E2", *STAR, "--fake-p", "1", "--rounds", "2")
            self.assertEqual({r["round"] for r in rows(d)}, {1, 2})

    def test_failed_attempt_usage_counts_toward_budget(self):
        calls = []

        def flaky(*a, **k):
            calls.append(1)
            if len(calls) % 2:
                return {"text": "", "usage": {"input": 40, "output": 0, "total": 40, "cached": 0, "reasoning": None},
                        "model_returned": None, "status": "error", "error_class": "HTTP500", "latency_ms": 0}
            return llm.call_model(*a, **k)
        rc, out, _ = go(self.runs, "E1", "--smoke", "--reps", "1", "--retries", "1", call=flaky)
        rs = rows(self.runs)
        self.assertTrue(all(r["failed_attempt_tokens"] == 40 and r["attempts"] == 2 for r in rs))
        used = sum(r["usage"]["total"] + 40 for r in rs)
        self.assertIn(f"tokens={used} ", out)  # the failed attempts are in the total, not just the logged final calls
        # and the resumed run starts from the same total, so a restart cannot reset the budget
        go(self.runs, "E1", "--smoke", "--reps", "1", "--retries", "1", call=flaky)
        self.assertEqual(len(calls), 2 * len(rs))

    def torn_run(self, damage, *args):
        """A full fake E1 run, damaged as a crash would leave it, then resumed. Returns (rows before, issued slots)."""
        go(self.runs, "E1", "--reps", "1", *args)
        (d,) = Path(self.runs).iterdir()
        before = rows(self.runs)
        raw = (d / "calls.jsonl").read_bytes()
        last = raw.rstrip(b"\n").rsplit(b"\n", 1)
        (d / "calls.jsonl").write_bytes(last[0] + b"\n" + damage(last[1]))
        issued = []

        def spy(*a, **k):
            issued.append(k["seed_key"])
            return llm.call_model(*a, **k)
        rc, _, err = go(self.runs, "E1", "--reps", "1", *args, call=spy)
        self.assertEqual(rc, 0, err)
        return d, before, issued

    def test_a_half_written_last_line_is_set_aside_and_its_slot_reissued(self):
        d, before, issued = self.torn_run(lambda line: line[:50])  # a torn write: no newline, not JSON
        self.assertEqual(len(issued), 1)
        torn = (d / "torn_lines.jsonl").read_text().splitlines()
        self.assertEqual(len(torn), 1)
        self.assertTrue(torn[0] and json.dumps(before[-1]).startswith(torn[0]))
        after = rows(self.runs)  # every line parses, none merged, none lost, none duplicated
        self.assertEqual([r["slot_id"] for r in after], [r["slot_id"] for r in before])
        self.assertEqual(after[-1]["output_text"], before[-1]["output_text"])  # seeded: the same note comes back

    def test_a_complete_last_line_missing_its_newline_is_also_set_aside_not_merged(self):
        d, before, issued = self.torn_run(lambda line: line)  # a crash between the write and its newline
        self.assertEqual(len(issued), 1)
        self.assertEqual(len((d / "torn_lines.jsonl").read_text().splitlines()), 1)
        raw = (d / "calls.jsonl").read_bytes()
        self.assertTrue(raw.endswith(b"\n"))
        after = rows(self.runs)
        self.assertEqual(sorted(r["slot_id"] for r in after), sorted(r["slot_id"] for r in before))
        self.assertEqual(len(after), len({r["slot_id"] for r in after}))
        go(self.runs, "E1", "--reps", "1")  # and a clean resume afterwards sets nothing aside
        self.assertEqual(len((d / "torn_lines.jsonl").read_text().splitlines()), 1)

    def test_a_clean_log_makes_no_torn_file_and_the_event_is_counted_in_results(self):
        go(self.runs, "E1", "--reps", "1")
        (d,) = Path(self.runs).iterdir()
        go(self.runs, "E1", "--reps", "1")
        self.assertFalse((d / "torn_lines.jsonl").exists())
        raw = (d / "calls.jsonl").read_bytes()
        (d / "calls.jsonl").write_bytes(raw[:-30])
        go(self.runs, "E1", "--reps", "1")
        out = Path(self.runs) / "an"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(analyze.main([str(d), "--out", str(out), "--boot", "10", "--reps", "3"]), 0)
        self.assertEqual(json.loads((out / "results.json").read_text())["torn_lines"], {d.name: 1})

    def test_resume_recreates_a_missing_plan_file(self):
        go(self.runs, "E1", "--reps", "1")
        (d,) = Path(self.runs).iterdir()
        want = (d / "planned_slots.txt").read_bytes()
        (d / "planned_slots.txt").unlink()
        go(self.runs, "E1", "--reps", "1")
        self.assertEqual((d / "planned_slots.txt").read_bytes(), want)

    def test_rpm_and_budget_may_change_on_resume_and_are_logged_without_touching_the_design(self):
        go(self.runs, "E1", "--reps", "1", "--rpm", "30", "--max-total-tokens", "1500")
        (d,) = Path(self.runs).iterdir()
        man = json.loads((d / "manifest.json").read_text())
        self.assertFalse((d / "deviations.jsonl").exists())
        rc, out, err = go(self.runs, "E1", "--reps", "1", "--rpm", "120", "--max-total-tokens", "5000000")
        self.assertEqual(rc, 0, err)
        dev = [json.loads(x) for x in (d / "deviations.jsonl").read_text().splitlines()]
        self.assertEqual([(x["key"], x["was"], x["now"]) for x in dev],
                         [("rpm", 30.0, 120.0), ("max_total_tokens", 1500, 5000000)])
        new = json.loads((d / "manifest.json").read_text())
        self.assertEqual((new["planned_slots_sha256"], new["params"]), (man["planned_slots_sha256"], man["params"]))
        go(self.runs, "E1", "--reps", "1", "--rpm", "120", "--max-total-tokens", "5000000")
        self.assertEqual(len((d / "deviations.jsonl").read_text().splitlines()), 2)  # unchanged values log nothing

    def test_resume_refuses_any_change_to_the_design(self):
        # mutation guard: with the design check gone, each of these would silently mix two designs in one log
        base = ["E1", "--smoke", "--run-id", "x", "--reps", "1"]
        go(self.runs, *base)
        for change in (["--reps", "2"], ["--temperature", "0.9"], ["--max-tokens", "100"], ["--model", "other"],
                       ["--contents", "canary"], ["--seed", "5"], ["--fake-p", "0.1"], ["--fake-persist", "0.1"]):
            with self.subTest(change=change):
                rc, _, err = go(self.runs, *base, *change)
                self.assertEqual(rc, 2)
                self.assertIn("different design", err)
        self.assertEqual(go(self.runs, *base, "--rpm", "99", "--retries", "5")[0], 0)

    def test_manifest_records_the_fake_provider_settings(self):
        go(self.runs, "E1", "--smoke", "--reps", "1", "--fake-p", "0.3", "--fake-persist", "0.6", "--fake-sentinel", "0.1,0.2,0.3,0")
        (m,) = [json.loads(p.read_text()) for p in Path(self.runs).glob("*/manifest.json")]
        self.assertEqual(m["fake"], {"p": 0.3, "persist": 0.6, "sentinel": "0.1,0.2,0.3,0"})
        envfile = Path(self.runs) / "k.txt"
        envfile.write_text("ADAPTION_API_KEY=x\n")
        go(Path(self.runs) / "real", "E1", "--smoke", "--reps", "1", "--provider", "adaption", "--env-file", str(envfile),
           call=lambda *a, **k: {"text": "hi", "usage": dict.fromkeys(("input", "output", "total", "cached", "reasoning")),
                                 "model_returned": None, "status": "ok", "error_class": None, "latency_ms": 0})
        (m,) = [json.loads(p.read_text()) for p in (Path(self.runs) / "real").glob("*/manifest.json")]
        self.assertIsNone(m["fake"])

    def test_every_logged_line_is_fsynced(self):
        # mutation guard: a crash must not lose a row the run believes it wrote
        with mock.patch.object(runner.os, "fsync") as f:
            go(self.runs, "E1", "--reps", "1")
        self.assertGreaterEqual(f.call_count, len(rows(self.runs)))

    def test_smoke_cap_and_mark(self):
        rc, out, _ = go(self.runs, "E2", "--smoke", "--reps", "3")
        self.assertEqual(len(rows(self.runs)), 20)
        self.assertEqual(guards.SMOKE_CAP, 20)
        (m,) = [json.loads(p.read_text()) for p in Path(self.runs).glob("*/manifest.json")]
        self.assertTrue(m["smoke"])
        go(self.runs, "E2", "--smoke", "--reps", "3")
        self.assertEqual(len(rows(self.runs)), 20)  # a re-run cannot exceed the cap

    def test_topologies(self):
        star = world.graph("star", 0)
        self.assertEqual(sorted(star[0]), list(range(1, 8)))
        self.assertTrue(all(star[i] == [0] for i in range(1, 8)))
        for seed in range(20):
            mesh = world.graph("mesh", seed)
            self.assertTrue(all(len(v) == 4 and len(set(v)) == 4 and i not in v for i, v in mesh.items()))
            self.assertTrue(all(i in mesh[j] for i, v in mesh.items() for j in v))
        self.assertEqual(world.graph("mesh", 3), world.graph("mesh", 3))
        # shared: each agent reads the latest 4 entries by others
        board = [(j, f"n{j}") for j in range(8)]
        for ag in range(8):
            items = world.inbox_items(None, ag, {}, board)
            self.assertEqual(len(items), 4)
            self.assertNotIn(f"a{ag}", [s for s, _ in items])
        self.assertEqual(sorted(world.board_order({"seed": 0, "sid": "x"}, 2)), list(range(8)))

    def test_e3_warned_placement(self):
        ts = world.make_trials("E3", {"reps": 30, "seed": 0, "topologies": ["star", "mesh"],
                                      "contents": ["canary"], "warned_counts": [2]})
        star = [t for t in ts if t["topology"] == "star"]
        self.assertEqual({t["seed_position"] for t in star}, {"leaf"})  # A9
        for t in ts:
            self.assertEqual(len(t["warned"]), 2)
            self.assertNotIn(t["seed_agent"], t["warned"])
        self.assertTrue(all(0 in t["warned"] for t in star if t["condition"]["placement"] == "hub"))

    def test_round0_and_condition_keys(self):
        go(self.runs, "E2", *STAR, "--topologies", "star,shared", "--max-total-tokens", "100000")
        rs = rows(self.runs)
        keys = "content wording channel immunity dose warned_count placement form".split()
        self.assertTrue(all(sorted(r["condition"]) == sorted(keys) for r in rs))
        self.assertEqual({r["round"] for r in rs} & {0}, set())  # round 0 makes no provider call
        r1 = [r for r in rs if r["round"] == 1 and r["topology"] == "star" and r["seed_position"] == "hub"]
        self.assertTrue(all(r["inbox_markers"] == {"a0": True} for r in r1 if r["agent"] != 0))
        sh = [r for r in rs if r["round"] == 1 and r["topology"] == "shared"]
        self.assertTrue(all(len(r["inbox_sources"]) == 4 and len(r["board_order"]) == 8 for r in sh))
        (d,) = Path(self.runs).iterdir()
        m = json.loads((d / "manifest.json").read_text())
        self.assertEqual(sorted(m["graphs"]), ["shared", "star"])
        self.assertEqual(len(m["graphs"]["star"]), 7)
        planned = (d / "planned_slots.txt").read_bytes()
        self.assertEqual(hashlib.sha256(planned).hexdigest(), m["planned_slots_sha256"])

    def test_e1_context_and_e1p_persistence(self):
        go(self.runs, "E1", "--reps", "1", "--fake-p", "0")
        e1 = rows(self.runs)
        self.assertTrue(all(r["inbox_sources"][1:] in (["ext1"], ["ext1", "ext2"]) for r in e1))
        self.assertTrue(all(r["inbox_markers"]["ext1"] and not r["inbox_markers"][r["inbox_sources"][0]] for r in e1))
        self.assertEqual({r["condition"]["form"] for r in e1}, {"footer", "carried"})
        with tempfile.TemporaryDirectory() as d:
            go(d, "E1", "--reps", "16", "--fake-p", "0")
            many = rows(d)  # A17: not one fixed agent, peer or role
            self.assertEqual({r["agent"] for r in many}, set(range(8)))
            self.assertGreater(len({r["inbox_sources"][0] for r in many}), 3)
        with tempfile.TemporaryDirectory() as d:
            go(d, "E1p", "--reps", "1", "--fake-p", "0")
            ps = rows(d)
            self.assertTrue(all(len(r["inbox_sources"]) == 1 and r["inbox_markers"] == {r["inbox_sources"][0]: False}
                                and r["condition"]["dose"] == 0 for r in ps))
            self.assertTrue(all(r["adopted"] for r in ps))  # default persistence 1.0 keeps the marker

    def test_e1p_previous_note_is_self_copying_in_its_wording(self):  # A20 (was: bare carried tag)
        for t in world.make_trials("E1p", {"reps": 1, "seed": 0}):
            prev = world.contaminate(t, world.clean_note(t["recipient"]))
            self.assertTrue(prev.endswith(world.wording_text(t["condition"]["content"], t["condition"]["wording"], t["marker"])))
            self.assertNotIn(world.CARRIED[t["condition"]["content"]].format(m=t["marker"]), prev)

    def test_marker_matching_is_exact(self):
        m = world.marker(0, "t1")
        self.assertRegex(m, r"^OB-[0-9a-f]{8}$")
        self.assertNotEqual(m, world.marker(0, "t2"))
        self.assertTrue(world.has_marker(f"see {m}.", m))
        self.assertTrue(world.has_marker(f"{m}:P2", m))
        self.assertFalse(world.has_marker(m.lower().replace("ob-", "Ob-"), m))
        self.assertFalse(world.has_marker(m + "0", m))
        self.assertFalse(world.has_marker(m[:-1], m))

    def test_api_key_never_leaks(self):
        key = "pt_live_PLANTEDSECRET123"
        envfile = Path(self.runs) / "secrets.txt"
        envfile.write_text(f"# c\nexport ADAPTION_API_KEY='{key}'\nOTHER=1\n")
        self.assertEqual(llm.load_env(envfile)["ADAPTION_API_KEY"], key)
        res = llm.call_model("adaption", "m", "s", "p", max_tokens=5, temperature=0,
                             env={"ADAPTION_API_KEY": key}, base_url="http://127.0.0.1:9/")  # closed local port
        self.assertEqual(res["status"], "error")
        self.assertNotIn(key, json.dumps(res))
        envfile2 = Path(self.runs) / "nokey.txt"
        envfile2.write_text("OTHER=1\n")
        self.assertEqual(go(Path(self.runs) / "nk", "E1", "--provider", "adaption", "--env-file", str(envfile2))[0], 2)

        def leaky(*a, **k):
            raise RuntimeError(f"401 for key {k['env']['ADAPTION_API_KEY']}")
        d = Path(self.runs) / "r"
        rc, out, err = go(d, "E1", "--smoke", "--reps", "1", "--provider", "adaption", "--env-file", str(envfile), call=leaky)
        blob = out + err + "".join(p.read_text() for p in d.rglob("*") if p.is_file())
        self.assertNotIn(key, blob)
        # fake runs never touch an env file
        self.assertEqual(go(Path(self.runs) / "r2", "E1", "--smoke", "--env-file", "/nonexistent")[0], 0)


class RealGit(unittest.TestCase):
    """guards.run_git and check_prereg against a real throwaway repo (no mocks)."""

    ARGV = ["E2", *STAR]

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, "-c", "user.name=t", "-c", "user.email=t@t",
                               "-c", "commit.gpgsign=false", *args], capture_output=True, text=True, check=True)

    def freeze(self, block=None, status="**Status: FROZEN"):
        block = prereg_block(self.params, self.planned) if block is None else block
        (Path(self.repo) / "PREREG.md").write_text(f"{status}\n```prereg-params\n{block}\n```\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "freeze")
        return self.git("rev-parse", "HEAD").stdout.strip()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = tmp.name
        a = runner.parse_args(self.ARGV)
        self.params, self.planned = runner.build_params(a), runner.plan(a)[3]
        for f in (*guards.REQUIRED, "config/notes.json"):
            (Path(self.repo) / f).parent.mkdir(exist_ok=True)
            (Path(self.repo) / f).write_text("x = 1\n")
        self.git("init", "-q")
        self.sha = self.freeze()

    def check(self, commit=None, params=None, planned=None):
        return guards.check_prereg(commit or self.sha, params or self.params, planned or self.planned,
                                   "PREREG.md", self.repo)

    def test_run_git_reports_return_code_and_output(self):
        rc, out = guards.run_git(self.repo, "rev-parse", "HEAD")
        self.assertEqual((rc, out.strip()), (0, self.sha))
        self.assertNotEqual(guards.run_git(self.repo, "show", "nope:PREREG.md")[0], 0)

    def test_frozen_commit_passes_then_edits_and_mismatches_refuse(self):
        self.assertEqual(self.check(), self.sha)
        with self.assertRaisesRegex(guards.GuardError, "reps="):
            self.check(params={**self.params, "reps": 99})
        with self.assertRaisesRegex(guards.GuardError, "not a commit SHA"):
            self.check(commit="0" * 40)
        (Path(self.repo) / "outbreak/world.py").write_text("x = 2\n")
        with self.assertRaisesRegex(guards.GuardError, "frozen code differs"):
            self.check()

    def test_only_commit_shas_are_accepted_and_the_full_sha_is_returned(self):
        self.git("branch", "frozen-tip")
        self.git("tag", "t1")
        for ref in ("HEAD", "master", "main", "frozen-tip", "t1", "", None, self.sha.upper()):
            with self.subTest(ref=ref), self.assertRaises(guards.GuardError):
                guards.check_prereg(ref, self.params, self.planned, "PREREG.md", self.repo)
        self.assertEqual(self.check(commit=self.sha[:9]), self.sha)  # an abbreviation resolves, the full SHA comes back

    def test_non_ancestor_commit_is_refused(self):
        self.git("checkout", "-q", "-b", "side")
        side = self.freeze(status="**Status: FROZEN (side)")
        self.git("checkout", "-q", "-")
        self.assertEqual(self.check(), self.sha)
        with self.assertRaisesRegex(guards.GuardError, "not an ancestor"):
            self.check(commit=side)

    def test_missing_keys_are_refused_not_ignored(self):
        for drop in ("temperature", "rounds", "reps", "topologies", "model"):
            block = "\n".join(l for l in prereg_block(self.params, self.planned).splitlines() if not l.startswith(drop + "="))
            with self.subTest(drop=drop), self.assertRaisesRegex(guards.GuardError, f"lacks .*{drop}"):
                self.check(commit=self.freeze(block))
        with self.assertRaisesRegex(guards.GuardError, "lacks .*planned_slots_sha256"):
            self.check(commit=self.freeze("\n".join(f"{k}={v}" for k, v in self.params.items())))

    def test_design_binding_through_the_dry_run_hash(self):
        def dry(*extra):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(runner.main(["--dry-run", *self.ARGV, *extra]), 0)
            (line,) = out.getvalue().splitlines()
            return line
        line = dry()  # the line as printed, comment included, is what a prereg commits
        a = runner.parse_args(self.ARGV)
        planned = runner.plan(a)[1]
        self.assertIn(f"slots={len(planned)} trials=6", line)
        sha = self.freeze(prereg_block(self.params, "x").rsplit("\n", 1)[0] + "\n" + line)
        self.check(commit=sha)
        # same parameters, a different design (more reps): the run's hash differs, so it is refused
        other = runner.plan(runner.parse_args([*self.ARGV, "--reps", "6"]))[3]
        self.assertEqual(self.params["reps"], 3)
        with self.assertRaisesRegex(guards.GuardError, "planned_slots_sha256="):
            self.check(commit=sha, planned=other)
        self.assertTrue(line.startswith(f"E2.planned_slots_sha256={self.planned}  #"))
        self.assertNotEqual(dry("--reps", "6"), line)

    def test_the_whole_frozen_scope_is_bound_and_nothing_may_be_added(self):
        # A19 (and the G-2 mutant): every outbreak/*.py, __init__ and analyze included, and config/
        for f in (*guards.REQUIRED, "config/notes.json"):
            with self.subTest(edit=f):
                (Path(self.repo) / f).write_text("x = 2\n")
                with self.assertRaisesRegex(guards.GuardError, "frozen code differs"):
                    self.check()
                self.git("checkout", "--", f)
                self.assertEqual(self.check(), self.sha)
        for new in ("outbreak/extra.py", "config/extra.json"):
            with self.subTest(new=new):
                (Path(self.repo) / new).write_text("x = 1\n")
                with self.assertRaisesRegex(guards.GuardError, "added after"):
                    self.check()
                (Path(self.repo) / new).unlink()
        (Path(self.repo) / "outbreak/__pycache__").mkdir()
        (Path(self.repo) / "outbreak/__pycache__/x.pyc").write_text("x")
        (Path(self.repo) / "config/e4_notes.json").write_text("{}")  # the private notes file is ignored, hashed instead
        (Path(self.repo) / ".gitignore").write_text("__pycache__/\nconfig/e4_notes.json\n")
        sha = self.freeze()
        self.assertEqual(self.check(commit=sha), sha)  # ignored files are not "added"

    def test_a_freeze_commit_that_leaves_out_a_module_is_refused(self):
        for f in ("outbreak/analyze.py", "outbreak/__init__.py"):
            with self.subTest(missing=f):
                (Path(self.repo) / f).unlink()
                sha = self.freeze()
                with self.assertRaisesRegex(guards.GuardError, f"does not freeze .*{f}"):
                    self.check(commit=sha)
                (Path(self.repo) / f).write_text("x = 1\n")
                self.freeze()

    def test_draft_prereg_refused(self):
        with self.assertRaisesRegex(guards.GuardError, "not FROZEN"):
            self.check(commit=self.freeze(status="**Status: DRAFT"))


class Design(unittest.TestCase):
    def test_dry_run_prints_hash_and_counts_without_calls_and_matches_a_real_run(self):
        calls = []
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = runner.main(["--dry-run", "E2", *STAR], call=lambda *a, **k: calls.append(1))
        self.assertEqual((rc, calls), (0, []))
        self.assertRegex(out.getvalue(), r"^E2\.planned_slots_sha256=[0-9a-f]{64}  # slots=192 trials=6\n$")
        with tempfile.TemporaryDirectory() as d:
            go(d, "E2", *STAR)
            (m,) = [json.loads(p.read_text()) for p in Path(d).glob("*/manifest.json")]
        self.assertIn(m["planned_slots_sha256"], out.getvalue())
        everything = io.StringIO()  # no experiment named: every experiment, in one go
        with contextlib.redirect_stdout(everything):
            runner.main(["--dry-run", "--reps", "30"])
        self.assertEqual([l.split(".")[0] for l in everything.getvalue().splitlines()], ["E1", "E1S", "E1p", "E2R", "E2", "E2N", "E3", "E4"])  # A20 adds E2N

    def test_reps_that_break_balance_are_refused_but_smoke_is_exempt(self):
        for args in (STAR + ["--reps", "4"], ["--reps", "3"]):  # the second has mesh: 10 graphs x 3 wordings
            rc, _, err = go(tempfile.mkdtemp(), "E2", *args, auto=False)
            self.assertEqual(rc, 2)
            self.assertIn("breaks exact", err)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(runner.main(["--dry-run", "E4", "--reps", "20"]), 2)
        self.assertEqual(go(tempfile.mkdtemp(), "E2", "--smoke", "--reps", "4")[0], 0)
        ts = world.make_trials("E2", {"reps": 30, "seed": 0, "topologies": ["mesh"], "contents": ["canary"]})
        self.assertEqual(sorted(Counter(t["graph_id"] for t in ts).values()), [3] * 10)
        self.assertEqual(sorted(Counter(t["condition"]["wording"] for t in ts).values()), [10] * 3)

    DESIGNS = {  # A19: dry-run counts and frozen hashes of the four focused designs at a small size
        "E1": (["--contents", "canary", "--reps", "10"], 120,
               "bac8c93e1dca91f5576874ed1aed0f9b81c6a626fd607c4144b1c08cb5fa0205"),
        "E1S": (["--reps", "10"], 480, "4d2c145184b9eb02323d409a5adfb9c4cb986d6c4b0f79615cb1891c528efae0"),
        "E2R": (["--rounds", "1", "--contents", "canary", "--reps", "30"], 960,
                "7820f898d3453911c0e7088e3902fd000b82d6caad2ce71e417b1cfe6ace3959"),
        "E2": (["--contents", "canary", "--reps", "30"], 3840,
               "ad458d2a9d01497e5e02258b2cac5cd3b830f336e54fb2816fdc06c0907b0a5d"),
    }

    def test_focused_designs_have_pinned_counts_and_hashes(self):
        for exp, (args, slots, sha) in self.DESIGNS.items():
            with self.subTest(exp=exp):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(runner.main(["--dry-run", exp, *args]), 0)
                trials = slots // (1 if exp.startswith("E1") else 8 if exp == "E2R" else 32)
                self.assertEqual(out.getvalue(), f"{exp}.planned_slots_sha256={sha}  # slots={slots} trials={trials}\n")

    def test_e1_has_only_the_primary_cells_and_e1s_the_secondary_ones(self):
        cells = lambda exp: {tuple(t["condition"][k] for k in ("content", "channel", "immunity", "dose", "form"))
                             for t in world.make_trials(exp, {"reps": 3, "seed": 0, "contents": ["canary", "control"]})}
        e1 = cells("E1")
        self.assertEqual(e1, {(c, ch, "none", 1, f) for c in ("canary", "control") for ch in ("direct", "shared")
                              for f in ("footer", "carried")})  # the prediction check's cells, unwarned, dose 1
        e1s = cells("E1S")
        self.assertEqual(e1s, {(c, ch, i, d, "footer") for c in ("canary", "control") for ch in ("direct", "shared")
                               for i in ("none", "warned") for d in (1, 2)})
        self.assertEqual(len(world.make_trials("E1", {"reps": 7, "seed": 0, "contents": ["canary"]})), 7 * 12)
        self.assertEqual(len(world.make_trials("E1S", {"reps": 7, "seed": 0, "contents": ["canary", "control"]})), 7 * 48)
        ids = lambda exp: {t["id"] for t in world.make_trials(exp, {"reps": 3, "seed": 0, "contents": ["canary"]})}
        self.assertFalse(ids("E1") & ids("E1S"))  # distinct experiments never share a slot or a marker

    def test_e2r_is_the_round_one_design_and_e2_the_full_one(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(runner.main(["--dry-run", "E2R", "--reps", "30"]), 2)
        self.assertIn("--rounds 1", err.getvalue())
        a, b = (runner.plan(runner.parse_args(x))[1] for x in (["E2R", "--rounds", "1", "--reps", "30"], ["E2", "--rounds", "1", "--reps", "30"]))
        self.assertEqual(len(a), len(b))
        self.assertFalse(set(a) & set(b))
        full = runner.plan(runner.parse_args(["E2", "--reps", "30"]))[1]
        self.assertEqual(len(full), 4 * len(a))  # T=4: four rounds of the same cascades
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(go(tempfile.mkdtemp(), "E2R", "--smoke", "--reps", "4", "--rounds", "2")[0], 2)
        rc, _, _ = go(tempfile.mkdtemp(), "E2R", "--smoke", "--reps", "4", "--rounds", "1")
        self.assertEqual(rc, 0)

    def test_each_experiment_may_have_its_own_seed_in_the_prereg(self):
        argv = ["E2R", "--rounds", "1", "--reps", "30", "--contents", "canary", "--seed", "7"]
        a = runner.parse_args(argv)
        block = prereg_block(runner.build_params(a), runner.plan(a)[3]).replace("seed=7", "seed=0") + "\nE2R.seed=7"
        files = {"PREREG.md": f"**Status: FROZEN\n```prereg-params\n{block}\n```\n", **dict.fromkeys(guards.REQUIRED, "x")}
        git = fake_git(files, files)
        check = lambda args: guards.check_prereg(SHA[:12], runner.build_params(args), runner.plan(args)[3], "PREREG.md", ".", git)
        self.assertEqual(check(a), SHA)
        with self.assertRaisesRegex(guards.GuardError, "seed=7, run has seed=0"):
            check(runner.parse_args([*argv[:-1], "0"]))

    def test_canary_and_control_wordings_are_length_matched(self):
        m = world.marker(0, "t")
        for w in range(3):
            a, b = (len(world.wording_text(c, w, m)) for c in ("canary", "control"))
            self.assertLessEqual(abs(a - b) / a, 0.10, (w, a, b))

    def test_canary_and_control_cascades_are_paired_by_design(self):
        ts = world.make_trials("E3", {"reps": 30, "seed": 5, "topologies": ["star", "mesh", "shared"],
                                      "contents": ["canary", "control"], "warned_counts": [2]})
        by = {}
        for t in ts:
            by.setdefault(t["sid"], {})[t["condition"]["content"]] = t
        self.assertEqual(len(by), len(ts) // 2)
        for pair in by.values():
            a, b = pair["canary"], pair["control"]
            self.assertNotEqual(a["marker"], b["marker"])
            for k in ("seed_agent", "graph_id", "warned", "seed_position", "topology"):
                self.assertEqual(a[k], b[k])
            self.assertEqual([world.board_order(a, r) for r in range(3)], [world.board_order(b, r) for r in range(3)])
        self.assertEqual(world.pair_id("E2-star-hub-canary-004", "canary"), world.pair_id("E2-star-hub-control-004", "control"))


if __name__ == "__main__":
    unittest.main()
