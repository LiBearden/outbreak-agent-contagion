#!/usr/bin/env bash
# timestamp-freeze.sh - RFC 3161 timestamps for preregistration freezes.
#
# Purpose: before a freeze collects data, obtain externally verifiable
# timestamp tokens proving the frozen bundle existed at the TSA's time.
#
# Claim supported: the bundle digest recorded in commitment.txt existed at the
# time the TSA signed it. It does NOT show the bundle is correct, and a
# timestamp made AFTER data collection proves nothing about ordering: run
# `request` on the freeze commit before any collection starts.
#
# Usage:
#   timestamp-freeze.sh request <commit> [--tsa URL]... [--path FILE]... [--out DIR] [--dry-run] [--force]
#   timestamp-freeze.sh verify  <DIR> --cafile PEM [--untrusted PEM]
#   timestamp-freeze.sh status  <file.tsr>     (exit 0 iff the reply is Granted, else 3)
#
# A reply that is not Granted counts as a failed TSA and is kept as <host>.rejected.
# An existing <out>/commitment.tsq is reused (receipts must match each other) and only
# TSAs without a <host>.tsr are contacted; --force regenerates everything.
#
# Trust: the CA bundle for `verify` must be obtained from the TSA operator out
# of band (freetsa.org publishes cacert.pem; DigiCert's chain is in its
# published TSA certificate) and pinned into research/experiments/timestamps/ca/.
#
# Bundle = `git archive --format=tar <commit> -- <paths>` hashed with sha256,
# where paths come from FROZEN_CODE in research/experiments/run_parrot_glimmer.sh
# at that commit (paths absent at that commit are skipped and recorded).
# Each --path FILE replaces that default list entirely (repeat it to name several):
# a later experiment must name its own preregistration and manifest this way.
set -euo pipefail

RUNNER=research/experiments/run_parrot_glimmer.sh
DEFAULT_TSAS=(https://freetsa.org/tsr http://timestamp.digicert.com)
OPENSSL=${OPENSSL:-$([ -x /opt/homebrew/bin/openssl ] && echo /opt/homebrew/bin/openssl || echo openssl)}

# check_reply FILE: return 0 if Granted; else print Status lines and return 3.
check_reply() {
  local txt
  txt=$("$OPENSSL" ts -reply -in "$1" -text 2>&1) || { echo "Status: unparseable reply"; return 3; }
  ! printf '%s\n' "$txt" | grep -qE '^Status: Granted' || return 0
  printf '%s\n' "$txt" | grep -E '^Status( description)?:' || true
  return 3
}

die() { echo "error: $*" >&2; exit 2; }
sha256() { if command -v sha256sum >/dev/null; then sha256sum; else shasum -a 256; fi | cut -d' ' -f1; }

request() {
  local commit="" out="" dry=0 force=0 tsas=() named=()
  while [ $# -gt 0 ]; do
    case $1 in
      --tsa) [ $# -ge 2 ] || die "--tsa needs a URL"; tsas+=("$2"); shift 2 ;;
      --path) [ $# -ge 2 ] || die "--path needs a FILE"; named+=("$2"); shift 2 ;;
      --out) [ $# -ge 2 ] || die "--out needs a DIR"; out=$2; shift 2 ;;
      --dry-run) dry=1; shift ;;
      --force) force=1; shift ;;
      -*) die "unknown option $1" ;;
      *) [ -z "$commit" ] || die "unexpected arg $1"; commit=$1; shift ;;
    esac
  done
  [ -n "$commit" ] || die "request needs <commit>"
  [ ${#tsas[@]} -gt 0 ] || tsas=("${DEFAULT_TSAS[@]}")

  cd "$(git rev-parse --show-toplevel)"
  local full short script
  full=$(git rev-parse --verify "$commit^{commit}") || die "cannot resolve $commit"
  short=$(git rev-parse --short "$full")
  [ -n "$out" ] || out=research/experiments/timestamps/$short

  # Frozen paths, read from the runner as it was at that commit.
  script=$(git show "$full:$RUNNER") || die "no $RUNNER at $full"
  local frozen manifest prereg
  frozen=$(printf '%s\n' "$script" | sed -n 's/^FROZEN_CODE="\(.*\)"$/\1/p' | head -n1)
  manifest=$(printf '%s\n' "$script" | sed -n 's/^PREREG_MANIFEST=\${PREREG_MANIFEST:-\(.*\)}$/\1/p' | head -n1)
  prereg=$(printf '%s\n' "$script" | sed -n 's/^PREREG_FILE=\${PREREG_FILE:-\(.*\)}$/\1/p' | head -n1)
  [ -n "$frozen" ] || die "no FROZEN_CODE= line in $RUNNER at $full"
  frozen=${frozen//\$PREREG_MANIFEST/$manifest}
  frozen=${frozen//\$PREREG_FILE/$prereg}
  [ ${#named[@]} -eq 0 ] || frozen="${named[*]}"

  local paths=() skipped=() p
  for p in $frozen; do
    if git cat-file -e "$full:$p" 2>/dev/null; then paths+=("$p"); else skipped+=("$p"); fi
  done
  [ ${#paths[@]} -gt 0 ] || die "none of the frozen paths exist at $full"
  local digest
  digest=$(git archive --format=tar "$full" -- "${paths[@]}" | sha256)

  mkdir -p "$out"
  local reuse=0
  if [ $force -eq 0 ] && [ -e "$out/commitment.tsq" ]; then
    grep -qx "commit=$full" "$out/commitment.txt" || die "existing commitment in $out is for another commit (use --force)"
    # Same commit is not enough: the requested bundle (paths) must match the stored one.
    # Contents are fixed by commit+paths, so the paths= list stands in for per-file hashes.
    local stored want
    stored=$(sed -n 's/^paths=//p' "$out/commitment.txt" | tr ' ' '\n' | sort | tr '\n' ' ')
    want=$(printf '%s\n' "${paths[@]}" | sort | tr '\n' ' ')
    [ "$stored" = "$want" ] || die "existing commitment in $out is for a different bundle (stored paths: ${stored% }; requested paths: ${want% }); use a different --out directory (or --force to regenerate)"
    reuse=1; echo "reusing existing commitment"
  else
    {
      echo "commit=$full"
      echo "bundle_sha256=$digest"
      echo "paths=${paths[*]}"
      echo "skipped_missing=${skipped[*]:-}"
      echo "created_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } > "$out/commitment.txt"
    "$OPENSSL" ts -query -data "$out/commitment.txt" -sha256 -cert -no_nonce -out "$out/commitment.tsq"
  fi

  local url host ok=() bad=()
  for url in "${tsas[@]}"; do
    host=${url#*://}; host=${host%%[/:]*}
    if [ $reuse -eq 1 ] && [ -e "$out/$host.tsr" ]; then
      echo "skip $host: $out/$host.tsr exists"; ok+=("$host")
    elif [ $dry -eq 1 ]; then
      echo "curl -sS -f -H 'Content-Type: application/timestamp-query' --data-binary @$out/commitment.tsq $url -o $out/$host.tsr"
    elif curl -sS -f -H 'Content-Type: application/timestamp-query' --data-binary "@$out/commitment.tsq" "$url" -o "$out/$host.tsr"; then
      if desc=$(check_reply "$out/$host.tsr"); then
        ok+=("$host")
      else
        mv -f "$out/$host.tsr" "$out/$host.rejected"; bad+=("$host")
        echo "REJECTED $host: $(printf '%s\n' "$desc" | sed -n 's/^Status description: //p;t;s/^Status: //p' | head -n1)" >&2
      fi
    else
      rm -f "${out:?}/${host:?}.tsr"; bad+=("$host")
    fi
  done
  [ $dry -eq 1 ] || { echo "succeeded: ${ok[*]:-none}"; echo "failed: ${bad[*]:-none}"; }
  echo "files written:"; ls -1 "$out" | sed "s|^|  $out/|"
  [ $dry -eq 1 ] || [ ${#ok[@]} -gt 0 ] || die "no TSA granted a timestamp"
}

verify() {
  local dir="" cafile="" untrusted=()
  while [ $# -gt 0 ]; do
    case $1 in
      --cafile) [ $# -ge 2 ] || die "--cafile needs a PEM"; cafile=$2; shift 2 ;;
      --untrusted) [ $# -ge 2 ] || die "--untrusted needs a PEM"; untrusted=(-untrusted "$2"); shift 2 ;;
      -*) die "unknown option $1" ;;
      *) [ -z "$dir" ] || die "unexpected arg $1"; dir=$1; shift ;;
    esac
  done
  [ -d "$dir" ] || die "verify needs <DIR>"
  [ -n "$cafile" ] || die "verify needs --cafile (pinned CA bundle, see header)"
  local tsr n=0 fail=0
  for tsr in "$dir"/*.tsr; do
    [ -e "$tsr" ] || break
    n=$((n + 1))
    if "$OPENSSL" ts -verify -data "$dir/commitment.txt" -in "$tsr" -CAfile "$cafile" ${untrusted[@]+"${untrusted[@]}"}; then
      echo "OK   $tsr"
      "$OPENSSL" ts -reply -in "$tsr" -text | grep -E '^(TSA:|Time stamp:)' | sed 's/^/     /' || true
    else
      echo "FAIL $tsr" >&2; fail=1
    fi
  done
  [ $n -gt 0 ] || die "no .tsr files in $dir"
  exit $fail
}

status() { [ $# -eq 1 ] && [ -f "$1" ] || die "status needs <file.tsr>"; local d; d=$(check_reply "$1") && return 0; printf '%s\n' "$d" >&2; exit 3; }

cmd=${1:-}; [ $# -eq 0 ] || shift
case $cmd in
  request) request "$@" ;;
  verify) verify "$@" ;;
  status) status "$@" ;;
  *) die "usage: $0 request <commit> [--tsa URL]... [--path FILE]... [--out DIR] [--dry-run] [--force] | verify <DIR> --cafile PEM [--untrusted PEM] | status <file.tsr>" ;;
esac
