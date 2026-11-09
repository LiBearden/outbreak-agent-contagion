"""Check the prereg freeze from a public export, without git history. Standard library plus the openssl CLI.

  python3 scripts/verify_freeze.py [--receipts timestamps/8327244aad0a] [--root .] [--cafile PEM]

1. Each <host>.tsr in the receipts folder timestamps the SHA-256 of commitment.txt, and openssl verifies its
   signature against the CA bundle (system roots by default).
2. manifest.txt (`git ls-tree -r -t` of the frozen commit: paths and hashes, no contents) rebuilds to the
   tree= hash named in commitment.txt.
3. Each file under --root is compared with its frozen hash and reported as match, differs or missing.
Exits 0 only if every receipt's digest matches, at least one signature verifies, and the tree rebuilds.
Files that differ are listed, not failed: some files are meant to change after a freeze."""
import argparse
import hashlib
import re
import subprocess
from pathlib import Path

ROOTS = ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt")


def git_hash(kind, data):
    return hashlib.sha1(b"%s %d\0" % (kind, len(data)) + data).hexdigest()


def read_manifest(path):
    """Rows (mode, type, sha, path) from `git ls-tree -r -t` output."""
    rows = []
    for line in Path(path).read_text().splitlines():
        meta, name = line.split("\t", 1)
        rows.append((*meta.split(), name))
    return rows


def tree_hash(rows):
    """Root tree hash rebuilt from blob hashes alone; listed tree hashes are recomputed, not trusted."""
    kids = {}
    for mode, typ, sha, path in rows:
        kids.setdefault(path.rpartition("/")[0], []).append((mode, typ, sha, path))

    def build(d):
        ents = []
        for mode, typ, sha, path in kids.get(d, []):
            name = path.rpartition("/")[2]
            if typ == "tree":
                mode, sha = "40000", build(path)
            ents.append(((name + "/" if typ == "tree" else name).encode(), mode, name, sha))  # git's sort order
        body = b"".join(b"%s %s\0" % (m.encode(), n.encode()) + bytes.fromhex(s) for _, m, n, s in sorted(ents))
        return git_hash(b"tree", body)
    return build("")


def compare(rows, root):
    """{path: 'match' | 'differs' | 'missing'} for every frozen file."""
    out = {}
    for _, typ, sha, path in rows:
        if typ == "blob":
            f = Path(root) / path
            out[path] = "missing" if not f.is_file() else "match" if git_hash(b"blob", f.read_bytes()) == sha else "differs"
    return out


def receipt(tsr, commitment, cafile):
    """(digest matches, signature verified) for one RFC 3161 reply."""
    text = subprocess.run(["openssl", "ts", "-reply", "-in", str(tsr), "-text"], capture_output=True, text=True).stdout
    block = text.split("Message data:")[1].split("\n", 1)[1] if "Message data:" in text else ""
    imprint = "".join(re.sub(r"[ -]", "", m) for m in re.findall(r"^\s+[0-9a-f]{4} - (.{47})", block, re.M))
    ok_digest = imprint == hashlib.sha256(Path(commitment).read_bytes()).hexdigest()
    ok_sig = bool(cafile) and subprocess.run(
        ["openssl", "ts", "-verify", "-data", str(commitment), "-in", str(tsr), "-CAfile", cafile],
        capture_output=True).returncode == 0
    return ok_digest, ok_sig


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--receipts", default="timestamps/8327244aad0a")
    p.add_argument("--root", default=".")
    p.add_argument("--cafile", default=next((c for c in ROOTS if Path(c).is_file()), None))
    a = p.parse_args(argv)
    rec = Path(a.receipts)
    commitment = rec / "commitment.txt"
    fields = dict(line.split("=", 1) for line in commitment.read_text().split())
    ok = True
    sigs = 0
    for tsr in sorted(rec.glob("*.tsr")):
        d, s = receipt(tsr, commitment, a.cafile)
        print(f"receipt {tsr.name}: digest {'matches' if d else 'DOES NOT MATCH'}, "
              f"signature {'verified' if s else 'not verified against ' + str(a.cafile)}")
        ok &= d
        sigs += s
    if not sigs:
        print("no receipt signature verified; pass --cafile with the authority's root")
    rows = read_manifest(rec / "manifest.txt")
    rebuilt = tree_hash(rows)
    print(f"tree from manifest {rebuilt}: {'matches' if rebuilt == fields['tree'] else 'DOES NOT MATCH'} commitment")
    res = compare(rows, a.root)
    for path, r in sorted(res.items()):
        if r != "match":
            print(f"  {r}: {path}")
    print(f"frozen files: {sum(r == 'match' for r in res.values())} match, "
          f"{sum(r == 'differs' for r in res.values())} differ, {sum(r == 'missing' for r in res.values())} missing")
    ok = ok and sigs > 0 and rebuilt == fields["tree"]
    print("FREEZE CHECK", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
