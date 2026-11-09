#!/usr/bin/env bash
# RFC 3161 timestamp for a freeze commit: scripts/timestamp_freeze.sh <commit>
# Writes timestamps/<sha12>/ with commitment.txt, commitment.tsq and one <host>.tsr per authority that granted.
# Only the SHA-256 of commitment.txt leaves the machine. Run before any study data exists.
# preexisting/timestamp-freeze.sh does the same job but needs a runner file from its original repo.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
full=$(git rev-parse --verify "${1:?usage: timestamp_freeze.sh <commit>}^{commit}")
out=timestamps/${full:0:12}
[ ! -e "$out" ] || { echo "$out exists; refusing to overwrite receipts" >&2; exit 2; }
mkdir -p "$out"
{
  echo "commit=$full"
  echo "tree=$(git rev-parse "$full^{tree}")"
  echo "bundle_sha256=$(git archive --format=tar "$full" | shasum -a 256 | cut -d' ' -f1)"
  echo "created_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$out/commitment.txt"
openssl ts -query -data "$out/commitment.txt" -sha256 -cert -no_nonce -out "$out/commitment.tsq"
n=0
for url in https://freetsa.org/tsr http://timestamp.digicert.com; do
  host=${url#*://}; host=${host%%/*}
  if curl -sS -f --max-time 60 -H 'Content-Type: application/timestamp-query' --data-binary "@$out/commitment.tsq" "$url" -o "$out/$host.tsr" \
    && openssl ts -reply -in "$out/$host.tsr" -text | grep -q '^Status: Granted'; then
    n=$((n + 1)); echo "granted: $host"
  else
    rm -f "$out/$host.tsr"; echo "failed: $host" >&2
  fi
done
[ "$n" -gt 0 ] || { rm -rf "$out"; echo "no authority granted a timestamp" >&2; exit 1; }
