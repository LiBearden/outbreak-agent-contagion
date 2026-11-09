import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

from outbreak import analyze

spec = importlib.util.spec_from_file_location(
    "export_cascades", os.path.join(os.path.dirname(__file__), "..", "scripts", "export_cascades.py"))
ec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ec)


class ExportTests(unittest.TestCase):
    def test_export_tiny_star(self):
        with tempfile.TemporaryDirectory() as t:
            self._run(Path(t))

    def _run(self, tmp_path):
        rid, tid = "r1", "E2-star-hub-canary-000"
        rows, slots = [], []
        for rnd in (1, 2):
            for a in range(8):
                sid = f"{tid}/r{rnd}/a{a}"
                slots.append(sid)
                rows.append({k: None for k in analyze.SCHEMA} | {
                    "slot_id": sid, "run_id": rid, "experiment": "E2", "trial_id": tid, "round": rnd, "agent": a,
                    "condition": {}, "topology": "star", "seed_position": "hub", "inbox_sources": [],
                    "inbox_markers": {}, "adopted": rnd == 1 and a in (1, 2), "status": "ok",
                    "graph_id": "star", "seed_agent": 0})
        d = tmp_path / rid
        d.mkdir()
        (d / "planned_slots.txt").write_text("\n".join(slots))
        (d / "calls.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
        sha = hashlib.sha256((d / "planned_slots.txt").read_bytes()).hexdigest()
        edges = [[0, i] for i in range(1, 8)]
        (d / "manifest.json").write_text(json.dumps({
            "run_id": rid, "experiment": "E2", "prereg_sha": "abc", "params": {"rounds": 2},
            "planned_slots_sha256": sha, "graphs": {"star": edges}}))
        res, incomplete, errors = ec.export([str(d)])
        c = res["cascades"][0]
        self.assertFalse(errors or incomplete)
        self.assertEqual((c["rounds"], c["final_size"]), ([[0], [1, 2]], 3))
        self.assertEqual((c["hub"], c["edges"], c["graph"]), (0, edges, None))
