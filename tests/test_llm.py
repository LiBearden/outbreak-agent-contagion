"""Adaption provider against a local stdlib server on 127.0.0.1. No other host is ever contacted."""
import contextlib
import io
import json
import tempfile
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from outbreak import analyze, llm
from test_harness import go, rows

KEY = "pt_live_PLANTEDSECRET123"
OK = {"status": "completed", "model": "served-model", "output_text": "hello",
      "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                "input_tokens_details": {"cached_tokens": 4}, "output_tokens_details": {"reasoning_tokens": 2}}}


def serve(script):
    """script: [(status, headers, body_dict)], consumed in order, the last one repeating."""
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append({"path": self.path, "auth": self.headers["Authorization"],
                         "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
            code, headers, body = script[min(len(seen), len(script)) - 1]
            raw = json.dumps(body).encode()
            self.send_response(code)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen, f"http://127.0.0.1:{srv.server_port}/api/v1/responses"


class Adaption(unittest.TestCase):
    def call(self, script, retries=2, pace=lambda: None):
        srv, seen, url = serve(script)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        sleeps = []
        res = llm.call_model("adaption", "m1", "sys", "hi", max_tokens=50, temperature=0.7,
                             env={"ADAPTION_API_KEY": KEY}, base_url=url, retries=retries, sleep=sleeps.append, pace=pace)
        self.assertNotIn(KEY, json.dumps(res))
        return res, seen, sleeps

    def test_success_and_request_body(self):
        res, seen, sleeps = self.call([(200, {}, OK)])
        self.assertEqual((res["status"], res["text"], res["model_returned"], res["attempts"]),
                         ("ok", "hello", "served-model", 1))
        self.assertEqual(res["usage"], {"input": 10, "output": 5, "total": 15, "cached": 4, "reasoning": 2})
        (req,) = seen
        self.assertEqual(req["path"], "/api/v1/responses")
        self.assertEqual(req["auth"], f"Bearer {KEY}")
        self.assertEqual(req["body"], {"model": "m1", "instructions": "sys", "input": "hi", "temperature": 0.7,
                                       "max_output_tokens": 50, "store": False})
        self.assertEqual(sleeps, [])

    def test_truncation_keeps_text_and_reason(self):
        res, *_ = self.call([(200, {}, {**OK, "status": "incomplete", "output_text": "partial",
                                        "incomplete_details": {"reason": "max_output_tokens"}})])
        self.assertEqual((res["status"], res["text"], res["error_class"]),
                         ("truncated", "partial", "incomplete_max_output_tokens"))

    def test_refusal_part(self):
        body = {"status": "completed", "model": "x", "output": [{"type": "message", "content": [
            {"type": "refusal", "refusal": "I can't help with that."}]}]}
        res, *_ = self.call([(200, {}, body)])
        self.assertEqual((res["status"], res["text"]), ("refused", "I can't help with that."))

    def test_failed_status_logs_no_body(self):
        body = {"status": "failed", "error": {"code": "server_error", "message": f"boom {KEY}"}}
        res, *_ = self.call([(200, {}, body)])
        self.assertEqual((res["status"], res["error_class"], res["text"]), ("error", "failed_server_error", ""))

    def test_429_then_success_honors_retry_after(self):
        res, seen, sleeps = self.call([(429, {"retry-after": "3"}, {"error": KEY}), (200, {}, OK)])
        self.assertEqual((res["status"], res["attempts"], res["http_429"], len(seen)), ("ok", 2, 1, 2))
        self.assertEqual(sleeps, [3.0])

    def test_500_exhausted_is_bounded_and_backs_off(self):
        res, seen, sleeps = self.call([(500, {}, {"error": f"echo {KEY}"})])
        self.assertEqual((res["status"], res["error_class"], res["attempts"], len(seen)), ("error", "HTTP500", 3, 3))
        self.assertEqual(sleeps, [0.5, 1.0])

    def test_client_error_is_not_retried(self):
        res, seen, sleeps = self.call([(401, {}, {"error": f"bad key {KEY}"})])
        self.assertEqual((res["error_class"], res["attempts"], len(seen), sleeps), ("HTTP401", 1, 1, []))

    def test_missing_usage_fields_are_null_not_zero(self):
        res, *_ = self.call([(200, {}, {"status": "completed", "output_text": "x", "usage": {"input_tokens": 7}})])
        self.assertEqual(res["usage"], {"input": 7, "output": None, "total": None, "cached": None, "reasoning": None})
        res, *_ = self.call([(200, {}, {"status": "completed", "output_text": "x"})])
        self.assertEqual(set(res["usage"].values()), {None})
        self.assertIsNone(res["model_returned"])

    def test_429_storm_is_retryable_and_credit_and_client_errors_are_not_retried(self):
        res, seen, _ = self.call([(429, {}, {})])
        self.assertEqual((res["status"], res["error_class"], res["retryable"], res["attempts"], res["http_429"]),
                         ("error", "HTTP429", True, 3, 3))
        for code in (401, 402, 403):
            res, seen, sleeps = self.call([(code, {}, {})])
            self.assertEqual((res["error_class"], res["retryable"], res["attempts"], len(seen)), (f"HTTP{code}", False, 1, 1))
        self.assertFalse(self.call([(200, {}, OK)])[0]["retryable"])

    def test_the_rate_limiter_is_paced_once_per_http_attempt(self):
        paced = []
        res, seen, _ = self.call([(429, {}, {}), (500, {}, {}), (200, {}, OK)], pace=lambda: paced.append(1))
        self.assertEqual((res["status"], res["attempts"]), ("ok", 3))
        self.assertEqual((len(paced), len(seen)), (3, 3))  # not once per logical call
        paced.clear()
        self.call([(401, {}, {})], pace=lambda: paced.append(1))
        self.assertEqual(len(paced), 1)

    def ok_response(self):
        return io.BytesIO(json.dumps(OK).encode())

    def test_timeouts_are_retried_with_backoff_including_mid_body(self):
        class Stalls(io.BytesIO):  # the connection opens, then the body times out
            def read(self, *a):
                raise TimeoutError("slow body")
        sleeps = []
        with mock.patch.object(llm.urllib.request, "urlopen", side_effect=[TimeoutError("t"), Stalls(), self.ok_response()]) as u:
            res = llm.call_model("adaption", "m", "s", "p", max_tokens=5, temperature=0, env={"ADAPTION_API_KEY": KEY},
                                 retries=2, sleep=sleeps.append)
        self.assertEqual((res["status"], res["attempts"], u.call_count, sleeps), ("ok", 3, 3, [0.5, 1.0]))
        with mock.patch.object(llm.urllib.request, "urlopen", side_effect=TimeoutError("t")) as u:
            res = llm.call_model("adaption", "m", "s", "p", max_tokens=5, temperature=0, env={"ADAPTION_API_KEY": KEY},
                                 retries=2, sleep=lambda d: None)
        self.assertEqual((res["status"], res["error_class"], res["retryable"], res["attempts"], u.call_count),
                         ("error", "TimeoutError", True, 3, 3))

    def test_error_class_is_the_class_name_never_the_message(self):
        # mutation guard: str(e) would put the key in the log
        with mock.patch.object(llm.urllib.request, "urlopen", side_effect=RuntimeError(f"auth {KEY}")):
            res = llm.call_model("adaption", "m", "s", "p", max_tokens=5, temperature=0, env={"ADAPTION_API_KEY": KEY})
        self.assertEqual((res["status"], res["error_class"], res["retryable"]), ("error", "RuntimeError", False))
        with mock.patch.object(llm, "_fake", side_effect=KeyError(KEY)):
            res = llm.call_model("fake", "m", "s", "p", max_tokens=5, temperature=0)
        self.assertEqual((res["status"], res["error_class"]), ("error", "KeyError"))
        self.assertNotIn(KEY, json.dumps(res))

    def test_empty_missing_and_pending_outputs_are_errors_never_clean_non_adoption(self):
        # A19 (audit F4): each of these used to be logged "ok" with text "" and counted as a non-adoption
        cases = {
            "empty": ({**OK, "output_text": ""}, "EmptyText"),
            "blank": ({**OK, "output_text": " \n"}, "EmptyText"),
            "missing": ({"status": "completed", "model": "x"}, "NoOutputText"),
            "no parts": ({"status": "completed", "output": [{"type": "message", "content": []}]}, "NoOutputText"),
            "in_progress": ({**OK, "status": "in_progress", "output_text": "part"}, "status_in_progress"),
            "queued": ({**OK, "status": "queued", "output_text": "part"}, "status_queued"),
        }
        for name, (body, cls) in cases.items():
            with self.subTest(name):
                res, *_ = self.call([(200, {}, body)])
                self.assertEqual((res["status"], res["error_class"], res["text"], res["retryable"]), ("error", cls, "", False))

    def test_no_key_never_calls(self):
        res = llm.call_model("adaption", "m", "s", "p", max_tokens=5, temperature=0, env={},
                             base_url="http://127.0.0.1:9/")
        self.assertEqual((res["status"], res["error_class"]), ("error", "ValueError"))


def via(url):
    """The runner's `call`, pointed at a local server (the CLI itself never accepts a URL)."""
    return lambda *a, **k: llm.call_model(*a, base_url=url, **k)


class Outages(unittest.TestCase):
    """A19: the runner against a local server that fails in various ways. 24 slots: E1, 2 contents, 1 rep."""
    ARGS = ["E1", "--run-id", "X", "--reps", "1", "--provider", "adaption", "--max-total-tokens", "1000000000"]

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.runs = Path(tmp.name) / "runs"
        env = Path(tmp.name) / "keys.txt"
        env.write_text(f"ADAPTION_API_KEY={KEY}\n")
        self.args = [*self.ARGS, "--env-file", str(env)]

    def server(self, script):
        srv, seen, url = serve(script)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return seen, via(url)

    def run_cli(self, script, *extra, **inject):
        seen, call = self.server(script)
        rc, out, err = go(self.runs, *self.args, *extra, call=call, **inject)
        return rc, out, err, seen

    def jsonl(self, name):
        p = self.runs / "X" / name
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def test_429_storm_leaves_slots_unissued_and_resume_finishes_them_with_no_duplicates(self):
        rc, out, err, seen = self.run_cli([(429, {}, {})], "--breaker", "3")
        self.assertEqual(rc, 0, err)
        self.assertIn("stop=circuit breaker (3 consecutive retryable failures)", out)
        self.assertEqual(self.jsonl("calls.jsonl"), [])  # no error rows: an outage is not an outcome
        att = self.jsonl("attempts.jsonl")
        self.assertEqual([(a["attempts"], a["error_class"]) for a in att], [(3, "HTTP429")] * 3)  # exactly N, each retried twice
        self.assertEqual(len(seen), 9)
        rc, out, err, seen2 = self.run_cli([(200, {}, OK)])  # the API is healthy again
        self.assertEqual((rc, len(seen2)), (0, 24))  # every slot, once
        ids = [r["slot_id"] for r in self.jsonl("calls.jsonl")]
        self.assertEqual((len(ids), len(set(ids))), (24, 24))
        self.assertEqual(set(ids), set((self.runs / "X" / "planned_slots.txt").read_text().split()))
        self.assertTrue(all(r["status"] == "ok" for r in self.jsonl("calls.jsonl")))
        self.assertEqual(len(self.jsonl("attempts.jsonl")), 3)  # the history stays

    def test_one_failed_slot_is_skipped_not_fatal_and_a_success_resets_the_breaker(self):
        # slot 1 fails (3 requests), the rest succeed: no stop, 23 rows, then a resume issues exactly the missing one
        rc, out, err, seen = self.run_cli([(500, {}, {})] * 3 + [(200, {}, OK)], "--breaker", "2")
        self.assertIn("stop=None", out)
        self.assertEqual((len(self.jsonl("calls.jsonl")), len(self.jsonl("attempts.jsonl")), len(seen)), (23, 1, 26))
        rc, out, err, seen2 = self.run_cli([(200, {}, OK)])
        self.assertEqual((len(seen2), len(self.jsonl("calls.jsonl"))), (1, 24))

    def test_the_limiter_counts_http_attempts(self):
        now_t = [0.0]
        rc, out, err, seen = self.run_cli([(429, {}, {})], "--breaker", "3", "--rpm", "60",
                                          now=lambda: now_t[0], sleep=lambda d: now_t.__setitem__(0, now_t[0] + d))
        self.assertEqual(len(seen), 9)
        self.assertGreaterEqual(now_t[0], 8.0 - 1e-9)  # 9 requests at 60 rpm: never faster than 8 s, retries included

    def test_credit_exhaustion_is_its_own_stop_reason(self):
        rc, out, err, seen = self.run_cli([(402, {}, {"error": "payment"})])
        self.assertIn("stop=credit exhausted (HTTP402)", out)
        self.assertEqual((len(seen), self.jsonl("calls.jsonl"), len(self.jsonl("attempts.jsonl"))), (1, [], 1))  # not retried, unissued
        self.runs = self.runs.parent / "runs2"
        rc, out, err, seen = self.run_cli([(403, {}, {})])  # 403 could be a key problem: only a streak counts as credit
        self.assertIn("stop=credit exhausted (HTTP403)", out)
        self.assertEqual((len(seen), len(self.jsonl("attempts.jsonl"))), (3, 3))
        self.runs = self.runs.parent / "runs3"
        rc, out, err, seen = self.run_cli([(403, {}, {}), (200, {}, OK)])  # one 403, then fine: no stop
        self.assertIn("stop=None", out)
        self.assertEqual((len(self.jsonl("calls.jsonl")), len(self.jsonl("attempts.jsonl"))), (23, 1))

    def test_non_retryable_failures_stay_logged_as_error_rows(self):
        rc, out, err, seen = self.run_cli([(401, {}, {"error": "bad key"})])
        self.assertIn("stop=None", out)
        rs = self.jsonl("calls.jsonl")
        self.assertEqual((len(rs), {(r["status"], r["error_class"], r["attempts"]) for r in rs}), (24, {("error", "HTTP401", 1)}))
        self.assertEqual(self.jsonl("attempts.jsonl"), [])

    def test_unissued_attempt_tokens_count_toward_the_budget_after_a_restart(self):
        self.run_cli([(429, {}, {})], "--breaker", "1")
        with open(self.runs / "X" / "attempts.jsonl", "a") as f:
            f.write(json.dumps({"slot_id": "x", "attempts": 3, "error_class": "HTTP500", "tokens": 5000}) + "\n")
        rc, out, err, seen = self.run_cli([(200, {}, OK)], "--max-total-tokens", "4000")
        self.assertIn("stop=token budget", out)
        self.assertEqual(len(seen), 0)

    def test_empty_and_pending_responses_are_error_rows_that_analysis_counts_as_missing(self):
        for name, body in (("empty", {**OK, "output_text": ""}), ("queued", {**OK, "status": "queued"}),
                           ("none", {"status": "completed", "model": "m"})):
            with self.subTest(name):
                self.runs = self.runs.parent / name
                rc, out, err, seen = self.run_cli([(200, {}, body)])
                rs = self.jsonl("calls.jsonl")
                self.assertEqual((len(rs), {r["status"] for r in rs}, {r["adopted"] for r in rs}), (24, {"error"}, {False}))
                self.assertEqual(len(self.jsonl("attempts.jsonl")), 0)  # a bad answer is an outcome, not an outage
                out_dir = self.runs.parent / f"an-{name}"
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(analyze.main([str(self.runs / "X"), "--out", str(out_dir), "--boot", "10", "--reps", "3"]), 0)
                res = json.loads((out_dir / "results.json").read_text())
                cells = res["estimands"]["e1_adoption"]["rows"]
                self.assertTrue(all(c["n"] == 0 and c["non_ok"] == 3 for c in cells), cells[:1])  # excluded, not non-adopted
                self.assertEqual((res["provider"], res["model"]), ("adaption", "fake-model"))


class Probe(unittest.TestCase):
    def test_probe_runs_20_smoke_calls_and_summarises(self):
        with tempfile.TemporaryDirectory() as d:
            rc, out, err = go(d, "--probe", "some-model", auto=False)
            self.assertEqual(rc, 0, err)
            rs = rows(d)
            self.assertEqual(len(rs), 20)
            self.assertEqual({(r["condition"]["content"], r["condition"]["channel"]) for r in rs},
                             {(c, ch) for c in ("canary", "control") for ch in ("direct", "shared")})
            self.assertEqual({r["model_requested"] for r in rs}, {"some-model"})
            (m,) = [json.loads(p.read_text()) for p in Path(d).glob("*/manifest.json")]
            self.assertTrue(m["smoke"])
            s = json.loads(out[out.index("{"):])
            self.assertEqual(s["calls"], 20)
            self.assertEqual(s["status"], {"ok": 20})
            self.assertEqual(s["models_returned"], ["some-model"])
            self.assertIsNone(s["mean_reasoning_tokens"])  # the fake reports none
            self.assertEqual(s["http_429"], 0)

    def test_no_experiment_and_no_probe_refuses(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(go(d, auto=False)[0], 2)


if __name__ == "__main__":
    unittest.main()
