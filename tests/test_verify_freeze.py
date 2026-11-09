"""Offline tests for scripts/verify_freeze.py: the manifest rebuilds the committed tree, and tampering is caught."""
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import verify_freeze as vf  # noqa: E402

REC = REPO / "timestamps" / "8327244aad0a"


class VerifyFreeze(unittest.TestCase):
    def setUp(self):
        self.rows = vf.read_manifest(REC / "manifest.txt")
        self.tree = dict(l.split("=", 1) for l in (REC / "commitment.txt").read_text().split())["tree"]

    def test_manifest_rebuilds_the_committed_tree_and_any_changed_hash_breaks_it(self):
        self.assertEqual(vf.tree_hash(self.rows), self.tree)
        i = next(k for k, r in enumerate(self.rows) if r[1] == "blob")
        bad = list(self.rows)
        bad[i] = (*bad[i][:2], "0" * 40, bad[i][3])
        self.assertNotEqual(vf.tree_hash(bad), self.tree)

    def test_compare_reports_match_differs_and_missing(self):
        mode, typ, _, path = next(r for r in self.rows if r[1] == "blob")
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "a.txt"
            f.write_bytes(b"x\n")
            rows = [(mode, typ, vf.git_hash(b"blob", b"x\n"), "a.txt"), (mode, typ, "0" * 40, "b.txt")]
            self.assertEqual(vf.compare(rows, d), {"a.txt": "match", "b.txt": "missing"})
            f.write_bytes(b"y\n")
            self.assertEqual(vf.compare(rows, d)["a.txt"], "differs")


if __name__ == "__main__":
    unittest.main()
