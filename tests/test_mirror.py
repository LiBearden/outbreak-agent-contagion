"""A21: the Braintrust mirror, against a local fake server. No network beyond loopback, no real key."""
import contextlib
import importlib.util
import io
import json
import re
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from outbreak import world

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("bt_mirror", ROOT / "scripts" / "bt_mirror.py")
bt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bt)
KEY = "sk-SECRETKEYVALUE123"
M = "OB-0123abcd"


def row(i, run="probe-x", **kw):
    r = {"slot_id": f"t/r1/a{i}", "run_id": run, "experiment": "E1", "trial_id": "t", "round": 1, "agent": i,
         "condition": {"content": "canary", "wording": 0, "form": "footer"}, "topology": None, "marker": M,
         "output_text": f"ok {M} word for word", "adopted": True, "status": "ok", "error_class": None,
         "model_requested": "m", "model_returned": "m2", "usage": {"input": 5, "output": 6, "total": 11},
         "latency_ms": 7, "instructions": "sys", "input": "hello"}
    r.update(kw)
    return r


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        s = self.server
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        s.auth.append(self.headers["Authorization"])
        code, out = 200, {}
        if self.path == "/v1/project":
            out = {"id": "proj"}
        elif self.path == "/v1/experiment":
            out = {"id": "exp-" + body["name"]}
        elif self.path.endswith("/insert"):
            if s.fail:
                s.fail -= 1
                code = 500
            else:
                s.calls += 1
                for e in body["events"]:
                    s.events.setdefault(self.path.split("/")[3], {})[e["id"]] = e
        data = json.dumps(out).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.srv = HTTPServer(("127.0.0.1", 0), Fake)
        self.srv.events, self.srv.fail, self.srv.calls, self.srv.auth = {}, 0, 0, []
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.runs = Path(tmp.name) / "runs"
        (self.runs / "probe-x").mkdir(parents=True)
        self.env = Path(tmp.name) / "env"
        self.env.write_text(f"# c\nOTHER=1\nBRAINTRUST_API_KEY='{KEY}'\n")
        self.calls = self.runs / "probe-x" / "calls.jsonl"

    def write(self, rows, torn=""):
        with open(self.calls, "a") as f:
            f.writelines(json.dumps(r) + "\n" for r in rows)
            f.write(torn)

    def run_main(self, *extra, sleeps=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = bt.main(["--runs-dir", str(self.runs), "--env-file", str(self.env),
                          "--api-url", f"http://127.0.0.1:{self.srv.server_port}", *extra],
                         sleep=(sleeps.append if sleeps is not None else lambda d: None))
        return rc, out.getvalue() + err.getvalue()

    def ids(self):
        return sorted(self.srv.events.get("exp-probe-x", {}))

    def test_event_mapping_and_stable_ids(self):
        self.write([row(0), row(1) | {"adopted": False}])
        self.assertEqual(self.run_main()[0], 0)
        ev = self.srv.events["exp-probe-x"]["probe-x:t/r1/a0"]
        self.assertEqual(ev["input"], {"instructions": "sys", "input": "hello"})
        self.assertEqual(ev["output"], f"ok {M} word for word")
        self.assertEqual(ev["scores"], {"adopted": 1.0, "request_carried": 1.0})
        self.assertEqual(ev["metrics"], {"prompt_tokens": 5, "completion_tokens": 6, "tokens": 11, "latency_ms": 7})
        self.assertEqual((ev["metadata"]["phase"], ev["metadata"]["marker"], ev["metadata"]["condition"]["content"]),
                         ("planning", M, "canary"))
        # re-upload from scratch (state deleted) keeps the same ids: no duplicates
        before = self.ids()
        (self.runs / "bt_mirror_state.json").unlink()
        self.run_main()
        self.assertEqual(self.ids(), before)
        self.assertEqual(len(before), 2)

    def test_old_rows_without_prompts_have_no_input(self):
        r = row(0)
        del r["instructions"], r["input"]
        self.assertNotIn("input", bt.to_event(r, "planning"))

    def test_offset_resume_and_torn_line(self):
        self.write([row(0), row(1)], torn=json.dumps(row(2))[:30])
        self.run_main()
        self.assertEqual(len(self.ids()), 2)  # the torn tail is not read
        state = json.loads((self.runs / "bt_mirror_state.json").read_text())
        self.assertEqual(state["offsets"]["probe-x"], len(self.calls.read_bytes().rsplit(b"\n", 1)[0]) + 1)
        with open(self.calls, "a") as f:  # complete the torn line, add one more
            f.write(json.dumps(row(2))[30:] + "\n" + json.dumps(row(3)) + "\n")
        n = self.srv.calls
        self.run_main()
        self.assertEqual(len(self.ids()), 4)
        self.assertEqual(self.srv.calls, n + 1)  # only the new rows were sent
        self.run_main()
        self.assertEqual(self.srv.calls, n + 1)  # nothing new, nothing sent

    def test_backoff_survives_500_and_key_stays_out_of_logs(self):
        self.write([row(0)])
        self.srv.fail = 2
        sleeps = []
        rc, out = self.run_main(sleeps=sleeps)
        self.assertEqual((rc, len(self.ids()), sleeps), (0, 1, [1, 2]))
        self.srv.fail = 99  # a hard outage: logged, offset kept, run goes on
        self.write([row(1)])
        sleeps.clear()
        rc, out2 = self.run_main("--retries", "2", "--backoff-cap", "1", sleeps=sleeps)
        self.assertEqual((rc, len(self.ids()), sleeps), (0, 1, [1, 1]))
        self.srv.fail = 0
        self.run_main()
        self.assertEqual(len(self.ids()), 2)
        log = (self.runs / "bt_mirror.jsonl").read_text()
        self.assertIn("HTTP500", log)
        for text in (log, out, out2, (self.runs / "bt_mirror_state.json").read_text()):
            self.assertNotIn(KEY, text)
        self.assertEqual(set(self.srv.auth), {f"Bearer {KEY}"})

    def test_connection_failure_logs_class_only(self):
        self.write([row(0)])
        self.srv.shutdown()
        self.srv.server_close()
        rc, out = self.run_main("--retries", "1")
        self.assertEqual(rc, 0)
        self.assertRegex((self.runs / "bt_mirror.jsonl").read_text(), r"URLError|ConnectionRefused")
        self.assertNotIn(KEY, (self.runs / "bt_mirror.jsonl").read_text())

    def test_phase_filter_and_run_dir_untouched(self):
        (self.runs / "E1-abc").mkdir()
        (self.runs / "E1-abc" / "calls.jsonl").write_text(json.dumps(row(0, run="E1-abc")) + "\n")
        self.write([row(0)])
        snap = {p: p.read_bytes() for p in self.runs.glob("*/*")}
        self.run_main("--phase", "planning")
        self.assertEqual(list(self.srv.events), ["exp-probe-x"])
        self.run_main("--phase", "study", "--run-id", "E1-abc")
        self.assertEqual(sorted(self.srv.events), ["exp-E1-abc", "exp-probe-x"])
        self.assertEqual({p: p.read_bytes() for p in self.runs.glob("*/*")}, snap)
        self.assertEqual(sorted(p.name for p in self.runs.glob("*") if p.is_file()),
                         ["bt_mirror.jsonl", "bt_mirror_state.json"] if (self.runs / "bt_mirror.jsonl").exists()
                         else ["bt_mirror_state.json"])

    def test_follow_does_final_pass_on_interrupt(self):
        self.write([row(0)])
        n = []

        def sleep(d):
            n.append(d)
            if len(n) == 1:  # a row arrives during the first wait
                self.write([row(1)])
            else:
                raise KeyboardInterrupt
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bt.main(["--runs-dir", str(self.runs), "--env-file", str(self.env), "--follow", "3",
                     "--api-url", f"http://127.0.0.1:{self.srv.server_port}"], sleep=sleep)
        self.assertEqual((n, len(self.ids())), ([3, 3], 2))
        self.assertIn("final pass", out.getvalue())

    def test_copied_request_carried_matches_world(self):
        for w in range(3):
            for t in (f"{M} {world.ANCHORS[w]}", M, world.ANCHORS[w], f"OB-ffffffff {world.ANCHORS[w].upper()}"):
                for m in (M, None):
                    self.assertEqual(bt.request_carried(t, w, m), world.request_carried(t, w, m))
        self.assertEqual(bt.ANCHORS, world.ANCHORS)

    def test_outbreak_does_not_import_mirror(self):
        for p in (ROOT / "outbreak").glob("*.py"):
            self.assertIsNone(re.search(r"bt_mirror|braintrust", p.read_text(), re.I), p.name)


if __name__ == "__main__":
    unittest.main()
