"""scripts/launch.py: the command lines it builds from the prereg block, its stop-at-first-failure loop and log, and that
--check calls the freeze guard with exactly what runner.execute would. No provider call and no real subprocess."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from outbreak import guards, runner

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("launch", ROOT / "scripts" / "launch.py")
launch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launch)
B = launch.block((ROOT / "PREREG.md").read_text())
FULL = "0123456789abcdef" * 2 + "01234567"
STD = ["E1", "E1S", "E1p", "E2R", "E2", "E2N", "E4"]


class FakeProc:
    def __init__(self, rc, out="run_dir=x issued=1 tokens=1 stop=None\n"):
        self.stdout, self.rc = iter([out]), rc

    def wait(self):
        return self.rc


def runner_of(*results):
    queue = iter(results)
    return lambda *a, **k: next(queue)


class Launch(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        for p in (mock.patch.object(launch, "ROOT", self.tmp), mock.patch.object(launch, "print", create=True)):
            p.start()
            self.addCleanup(p.stop)

    def lines(self):
        return [json.loads(x) for x in (self.tmp / "runs" / "launch.jsonl").read_text().splitlines()]

    def test_the_standard_block_launches_seven_experiments_in_order_with_fixed_run_ids(self):
        self.assertNotIn("E3", B)
        cmds = [launch.cmd(e, B, FULL) for e in STD]
        for e, c in zip(STD, cmds):
            self.assertEqual(c[1:4], ["-m", "outbreak.runner", e])
            self.assertEqual(c[c.index("--run-id") + 1], f"study-{e}-{FULL[:12]}")
            self.assertEqual((c[c.index("--prereg") + 1], c[c.index("--provider") + 1]), (FULL, "adaption"))
            self.assertEqual(c.count("--provider"), 1)
            self.assertNotIn("--env-file", c)
        self.assertEqual(cmds[3][cmds[3].index("--rounds") + 1], "1")  # E2R's own scope wins over the shared 4

    def test_it_stops_at_the_first_nonzero_exit_and_logs_start_and_finish(self):
        run = runner_of(FakeProc(0), FakeProc(0), FakeProc(7))
        with mock.patch("sys.stderr"):
            self.assertEqual(launch.launch(FULL, B, STD, run=run), 7)
        self.assertEqual([(r["event"], r["experiment"]) for r in self.lines()],
                         [("start", "E1"), ("finish", "E1"), ("start", "E1S"), ("finish", "E1S"), ("start", "E1p"), ("finish", "E1p")])
        self.assertEqual(self.lines()[-1]["exit_code"], 7)

    def test_a_clean_exit_that_reports_a_stop_still_stops_the_study(self):
        run = runner_of(FakeProc(0, "run_dir=x issued=9 tokens=9 stop=token budget\n"))
        with mock.patch("sys.stderr"):
            self.assertEqual(launch.launch(FULL, B, STD, run=run), 3)
        self.assertEqual(len(self.lines()), 2)

    def test_e3_is_launched_only_when_the_block_has_e3_lines(self):
        with mock.patch.object(launch.guards, "run_git", return_value=(0, "```prereg-params\nrounds=4\n```\n")):
            self.assertNotIn("E3", launch.load_block(FULL)[1])
        with mock.patch.object(launch.guards, "run_git", return_value=(0, "```prereg-params\nrounds=4\nE3.reps=1\n```\n")):
            self.assertEqual(launch.load_block(FULL)[1], ["E1", "E1S", "E1p", "E2R", "E2", "E2N", "E3", "E4"])

    def test_a_ref_that_is_not_a_full_sha_is_refused(self):
        for ref in ("HEAD", "main", FULL[:12]):
            with self.assertRaises(SystemExit):
                launch.load_block(ref)

    def test_check_gives_the_guard_what_execute_gives_it(self):
        env = self.tmp / "keys.txt"
        env.write_text("ADAPTION_API_KEY=dummy\n")
        extra = ["--runs-dir", str(self.tmp / "r"), "--env-file", str(env),
                 "--e4-notes", str(ROOT / "config" / "e4_notes.example.json")]
        for e in STD:
            with self.subTest(exp=e):
                seen = []

                def spy(*args, **kw):
                    seen.append(args[:5])
                    raise guards.GuardError("spy")
                self.assertEqual(launch.check_one(e, B, FULL, extra, guard=spy), (False, "spy"))
                a = runner.parse_args(launch.cmd(e, B, FULL, extra)[3:])
                with mock.patch.object(guards, "check_prereg", spy), self.assertRaises(guards.GuardError):
                    runner.execute(a)
                self.assertEqual(len(seen), 2)
                self.assertEqual(seen[0], seen[1])
                self.assertEqual(seen[0][0], FULL)


if __name__ == "__main__":
    unittest.main()
