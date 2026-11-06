#!/usr/bin/env bash
# commit-and-verify.test.sh - the self-test for tools/commit-and-verify.sh
#
# SPDX-FileCopyrightText: 2026 Dr Sohil Momin
# SPDX-License-Identifier: LicenseRef-Forge-Proprietary
#
# WHY THIS EXISTS
#
# The scan went red three times on the same commit step, and each time the five files
# it exists to publish had in fact landed. The cause was a rebase conflict inside a
# generated MANIFEST.json that differed from the branch's only in timestamps - a
# conflict no retry could clear, and one nothing tested. This test builds the exact
# race in a scratch repository: two runs of one generator, the second checked out
# before the first committed.
#
# It is a self-test in the same sense as tools/scan.py --self-test: run it, get a
# pass/fail, no fleet access, no network, nothing outside a mktemp directory.
#
#   0  every case behaved as specified
#   1  a case did not
#
# Usage: bash tools/commit-and-verify.test.sh

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CV="$HERE/commit-and-verify.sh"
[ -f "$CV" ] || { echo "FATAL: $CV not found" >&2; exit 1; }

# The retry path must not spend 88 seconds waiting to be tested.
export CV_BACKOFF="0"

pass=0
fail=0
ok()   { echo "  ok   $1"; pass=$((pass+1)); }
bad()  { echo "  FAIL $1"; fail=$((fail+1)); }
check() { if [ "$1" = "$2" ]; then ok "$3"; else bad "$3 (want '$2', got '$1')"; fi; }

ROOT="$(mktemp -d)"
trap 'rm -rf "$ROOT"' EXIT

# A scratch origin plus two clones of it: the two runs.
setup() {
  local name="$1"
  local d="$ROOT/$name"
  mkdir -p "$d"; cd "$d" || exit 1
  git init -q --bare origin.git
  git clone -q origin.git seed 2>/dev/null
  (
    cd seed || exit 1
    git config user.name test; git config user.email test@example.invalid
    mkdir -p docs tools
    cp "$CV" tools/commit-and-verify.sh
    echo '{"n":1}' > docs/inventory.json
    git add -A; git commit -qm init
    git push -q origin HEAD:main
    git -C ../origin.git symbolic-ref HEAD refs/heads/main
  )
  git clone -q origin.git a 2>/dev/null
  git clone -q origin.git b 2>/dev/null
  for c in a b; do
    ( cd "$c" && git config user.name test && git config user.email test@example.invalid )
  done
  echo "$d"
}

# What `manifest.py record` writes: same files, fresh timestamps, every run.
write_manifest() {   # write_manifest <clone> <timestamp> [extra-json-files]
  local c="$1" at="$2" extra="${3:-}"
  ( cd "$c" || exit 1
    printf '{\n  "schema": 1,\n  "locked": false,\n  "files": {\n    "docs/inventory.json": {\n      "sha": "%s",\n      "producer": "scan.yml@abc1234",\n      "class": "generated",\n      "at": "%s"\n    }%s\n  },\n  "updated": "%s"\n}\n' \
      "$(git hash-object -- docs/inventory.json)" "$at" "$extra" "$at" > MANIFEST.json )
}

# ---------------------------------------------------------------------------
echo "case 1: the race that made the scan red"
D="$(setup race)"; cd "$D" || exit 1

write_manifest a 2026-11-06T09:22:11Z
( cd a && git add -A && git commit -qm "run A" && git push -q origin HEAD:main )

# Run B is checked out before A committed, so it cannot see A's MANIFEST.json. Its
# data files are byte-identical - only the timestamps differ.
write_manifest b 2026-11-06T09:25:11Z
out="$( cd b && bash tools/commit-and-verify.sh "run B" main -- docs/inventory.json MANIFEST.json 2>&1 )"
rc=$?
check "$rc" "0" "a queued run lands its files instead of failing on a timestamp conflict"
case "$out" in
  *VERIFIED*) ok "the verdict is taken from the branch" ;;
  *) bad "the verdict is taken from the branch" ;;
esac
case "$out" in
  *"generated files are not merged"*|*"reconciled"*) ok "the conflict is reported, not hidden" ;;
  *) bad "the conflict is reported, not hidden" ;;
esac

# ---------------------------------------------------------------------------
echo "case 2: the reconciled manifest is a union, not a replacement"
D="$(setup union)"; cd "$D" || exit 1

# A records an entry for a file only A knows about.
write_manifest a 2026-11-06T09:22:11Z ',
    "docs/only-in-a.json": {
      "sha": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
      "producer": "scan.yml@abc1234",
      "class": "generated",
      "at": "2026-11-06T09:22:11Z"
    }'
( cd a && git add -A && git commit -qm "run A" && git push -q origin HEAD:main )

write_manifest b 2026-11-06T09:25:11Z
( cd b && bash tools/commit-and-verify.sh "run B" main -- docs/inventory.json MANIFEST.json >/dev/null 2>&1 )

landed="$(git -C "$ROOT/union" show "origin/main:MANIFEST.json" 2>/dev/null || git --git-dir="$D/origin.git" show "main:MANIFEST.json")"
case "$landed" in
  *only-in-a.json*) ok "an entry only the branch knew about survives" ;;
  *) bad "an entry only the branch knew about survives" ;;
esac
case "$landed" in
  *2026-11-06T09:25:11Z*) ok "this run's own entries win" ;;
  *) bad "this run's own entries win" ;;
esac

# ---------------------------------------------------------------------------
echo "case 3: a conflict this run did not produce stays red"
D="$(setup refuse)"; cd "$D" || exit 1

( cd a && echo branch-version > notes.txt && git add -A && git commit -qm "notes" && git push -q origin HEAD:main )
( cd b && echo my-version > notes.txt && git add -A && git commit -qm "notes" )
write_manifest b 2026-11-06T09:30:00Z
out="$( cd b && bash tools/commit-and-verify.sh "run B" main -- docs/inventory.json MANIFEST.json 2>&1 )"
rc=$?
check "$rc" "1" "an unproduced conflict is refused, not guessed at"
case "$out" in
  *"did not produce - not resolving it"*) ok "the refusal says why" ;;
  *) bad "the refusal says why" ;;
esac

# ---------------------------------------------------------------------------
echo "case 4: the ordinary path still works"
D="$(setup plain)"; cd "$D" || exit 1
write_manifest a 2026-11-06T09:22:11Z
out="$( cd a && bash tools/commit-and-verify.sh "run A" main -- docs/inventory.json MANIFEST.json 2>&1 )"
rc=$?
check "$rc" "0" "a run with no contention verifies"
case "$out" in
  *"pushed on attempt 1"*) ok "no backoff spent when there is nothing to retry" ;;
  *) bad "no backoff spent when there is nothing to retry" ;;
esac

# ---------------------------------------------------------------------------
echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
echo "commit-and-verify: all cases behave as specified"
