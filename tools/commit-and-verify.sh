#!/usr/bin/env bash
# commit-and-verify - the verdict is "did the data land?", not "did git exit 0".
#
# SPDX-FileCopyrightText: 2026 Dr Sohil Momin
# SPDX-License-Identifier: LicenseRef-Forge-Proprietary
#
# WHY THIS EXISTS (F-97)
#
# A commit step is the one step that can fail AFTER the work succeeded. Three times
# in two days a workflow put its file on the branch correctly and concluded
# `failure` anyway: forge/kit-audit.yml once, dashboard/refresh.yml twice. The
# conclusion described the last non-zero exit; the artifact described what reached
# the branch. Those are different questions and the run reported only the first.
#
# A red run that did its job teaches people to ignore red runs. So this script
# separates the two:
#
#   1. Record the blob sha of every file we intend to land.
#   2. Commit and push, retrying with backoff when the branch moves under us.
#   3. THEN ASK THE BRANCH. Fetch it and compare each file's blob sha with the one
#      recorded in step 1. Success is equality. Nothing else counts.
#
# Consequences, both intended:
#   * A push that failed because another run landed identical content is a SUCCESS.
#     The data is there; who pushed it does not matter.
#   * A commit that succeeded locally but never reached the branch is a FAILURE,
#     even though every git command exited 0.
#   * Red now means exactly one thing in this estate: the data is not on the branch.
#
# THE SECOND HALF (added 2026-11-06)
#
# The retry loop above assumed a rebase conflict was always worth retrying. It is not.
# `manifest.py record` stamps `at` and `updated` with the current time on every run, so
# two runs of the same generator never write the same MANIFEST.json bytes even when
# every file they describe is byte-identical. A queued run checks out before the run
# ahead of it commits, so its rebase meets a MANIFEST.json that differs only in
# timestamps - a conflict with no content behind it, which no amount of backoff can
# clear. Six attempts over 88 seconds, and then red, while all five of the scan's files
# had in fact landed. Generated files are not merged; see _rebase_resolve below.
#
# Usage:
#   tools/commit-and-verify.sh "<commit message>" [branch] -- <path> [path...]
#
# Exit codes:
#   0  every named file that existed is present on the branch with the intended bytes
#   1  at least one file is missing or differs on the branch, or nothing was produced
#   2  usage error
#
# A file that does not exist locally is NOT a failure here - a workflow that
# legitimately produces a subset of its outputs must not fail on the rest. It is
# reported as a warning, and it is not part of the equality check.

set -uo pipefail

if [ "$#" -lt 4 ]; then
  echo "usage: commit-and-verify.sh \"<message>\" [branch] -- <path> [path...]" >&2
  exit 2
fi

MSG="$1"; shift
if [ "$1" = "--" ]; then
  BR="${GITHUB_REF_NAME:-main}"
else
  BR="$1"; shift
fi
[ "${1:-}" = "--" ] && shift
PATHS=("$@")

if [ "${#PATHS[@]}" -eq 0 ]; then
  echo "::error::commit-and-verify: no paths given" >&2
  exit 2
fi

git config user.name  >/dev/null 2>&1 || git config user.name  "${GIT_AUTHOR_NAME:-forge-agent}"
git config user.email >/dev/null 2>&1 || git config user.email "${GIT_AUTHOR_EMAIL:-forge-agent@users.noreply.github.com}"

# This run's own MANIFEST.json bytes, kept because the rebase may have to reconcile
# them with a newer copy on the branch, and once conflict markers are in the worktree
# our version is gone. Empty when this run does not commit a manifest.
OUR_MANIFEST=""
case " ${PATHS[*]} " in
  *" MANIFEST.json "*)
    if [ -f MANIFEST.json ]; then OUR_MANIFEST="$(mktemp)"; cp MANIFEST.json "$OUR_MANIFEST"; fi
    ;;
esac

# Resolve a rebase that stopped on a conflict between generated files.
#
# The rule: generated files are not merged.
#   MANIFEST.json  start from the branch's copy - it may describe files this run never
#                  touched - then overlay this run's own entries, which carry the shas
#                  of the bytes this run is about to verify. Union, ours winning.
#   anything else  this run's version, because those are the bytes the verdict is
#                  about, and they are what a push over the other run would have left.
#
# It refuses to resolve a conflict in a file this run did not produce: a conflict it
# does not understand stays a red run rather than becoming a guess.
_rebase_resolve() {
  local conflicted c clean
  conflicted="$(git diff --name-only --diff-filter=U 2>/dev/null)"
  [ -n "$conflicted" ] || return 1
  for c in $conflicted; do
    clean=0
    for p in "${PATHS[@]}"; do [ "$c" = "$p" ] && clean=1 && break; done
    [ "$c" = "MANIFEST.json" ] && clean=1
    if [ "$clean" -eq 0 ]; then
      echo "::warning::conflict in $c, which this run did not produce - not resolving it"
      return 1
    fi
  done

  for c in $conflicted; do
    if [ "$c" = "MANIFEST.json" ] && [ -n "$OUR_MANIFEST" ] && [ -s "$OUR_MANIFEST" ]; then
      # During a rebase `--ours` is the branch being rebased ONTO.
      git checkout --ours -- "$c" 2>/dev/null || true
      if ! python3 - "$OUR_MANIFEST" "$c" <<'PY'
import json, sys
ours = json.load(open(sys.argv[1], encoding="utf-8"))
merged = json.load(open(sys.argv[2], encoding="utf-8"))
merged.setdefault("files", {})
merged["files"].update(ours.get("files", {}))
merged["updated"] = ours.get("updated", merged.get("updated"))
with open(sys.argv[2], "w", encoding="utf-8") as fh:
    json.dump(merged, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
PY
      then
        echo "::warning::could not reconcile $c with the branch copy"
        return 1
      fi
      echo "::warning::$c reconciled: branch entries kept, this run's entries on top"
    else
      # During a rebase `--theirs` is the commit being applied: this run's bytes.
      git checkout --theirs -- "$c" 2>/dev/null || true
      echo "::warning::$c kept at this run's version - generated files are not merged"
    fi
    git add -- "$c" 2>/dev/null || true
  done
  return 0
}

# Rebase onto the branch, resolving generated-file conflicts, or give up cleanly.
_rebase_onto() {
  local rounds=0
  git rebase "origin/$BR" --quiet 2>/dev/null && return 0
  while [ "$rounds" -lt 5 ]; do
    rounds=$((rounds+1))
    _rebase_resolve || { git rebase --abort 2>/dev/null || true; return 1; }
    # `git rebase --continue` takes no options after it - `--continue --quiet` is a
    # usage error (exit 129) that reads exactly like a failed rebase. Silence it with
    # a redirect instead.
    git -c core.editor=true rebase --continue >/dev/null 2>&1 && return 0
  done
  git rebase --abort 2>/dev/null || true
  return 1
}

# The verdict compares against the sha of the bytes this run computed. If the rebase
# rewrote MANIFEST.json, the bytes that will land are the rewritten ones, so the intent
# record has to follow them - otherwise a correct commit fails its own check.
_restate() {
  local path="$1" tmp
  [ -f "$path" ] || return 0
  [ -f "$MANIFEST" ] || return 0
  tmp="$(mktemp)"
  while read -r want p; do
    if [ "$p" = "$path" ]; then
      printf '%s %s\n' "$(git hash-object -- "$path")" "$p" >> "$tmp"
    else
      printf '%s %s\n' "$want" "$p" >> "$tmp"
    fi
  done < "$MANIFEST"
  mv "$tmp" "$MANIFEST"
}

# ---- 1. what we intend to land -------------------------------------------------
MANIFEST="$(mktemp)"
present=0
for f in "${PATHS[@]}"; do
  # -f, not -e: a DIRECTORY passed here would make `git hash-object` fail and be
  # recorded as an empty sha, which the verdict would then report as "did not land" -
  # a false red. intake.yml legitimately stages a path that may be a directory, so
  # this must warn and skip rather than lie.
  if [ -f "$f" ]; then
    printf '%s %s\n' "$(git hash-object -- "$f")" "$f" >> "$MANIFEST"
    present=$((present+1))
  else
    echo "::warning::$f is not a regular file (missing, or a directory) - not verified"
  fi
done

if [ "$present" -eq 0 ]; then
  echo "::error::this run produced none of the files it was supposed to produce"
  rm -f "$MANIFEST"
  exit 1
fi

# MANIFEST.json is written by every generator alongside its output. Use
# git status --porcelain, not git diff — git diff does not see untracked files, so
# a MANIFEST.json created on this run (the first run after bootstrap in a repo the
# bootstrap missed) would be silently dropped.
if [ -f MANIFEST.json ] && [ -n "$(git status --porcelain -- MANIFEST.json 2>/dev/null)" ]; then
  _have_manifest=0
  for _p in "${PATHS[@]}"; do [ "$_p" = "MANIFEST.json" ] && _have_manifest=1; done
  [ "$_have_manifest" -eq 1 ] || PATHS+=("MANIFEST.json")
fi
git add -- "${PATHS[@]}" 2>/dev/null || true

# NOT an early exit. "Nothing staged" does NOT mean "the data is on the branch" -
# it can also mean the add failed. This shortcut used to `exit 0` here, which is the
# same disease in miniature: claiming success without asking the branch. It now falls
# through to the verdict below, which is the only thing allowed to say yes.
if git diff --staged --quiet; then
  echo "nothing staged - skipping commit and push, going straight to the verdict"
else
  git commit -q -m "$MSG" || { echo "::error::git commit failed"; rm -f "$MANIFEST"; exit 1; }
  echo "committed: $MSG"

  # ---- 2. push, retrying when the branch moves under us -------------------------
  # CV_BACKOFF exists so the retry path can be TESTED without spending 88 seconds
  # waiting. Production leaves it unset; the default is the real schedule.
  pushed=0
  attempt=0
  for wait in ${CV_BACKOFF:-0 4 8 16 30 30}; do
    attempt=$((attempt+1))
    [ "$wait" = "0" ] || sleep "$wait"
    git fetch origin "$BR" --quiet 2>/dev/null || true
    if ! _rebase_onto; then
      echo "::warning::rebase onto origin/$BR failed on attempt $attempt"
      git rebase --abort 2>/dev/null || true
      continue
    fi
    _restate MANIFEST.json
    if git push --quiet origin "HEAD:$BR" 2>/dev/null; then
      echo "pushed on attempt $attempt"
      pushed=1
      break
    fi
    echo "::warning::push rejected on attempt $attempt - the branch moved again"
  done
  git rebase --abort 2>/dev/null || true
  if [ "$pushed" -ne 1 ]; then
    echo "::warning::could not push after $attempt attempts. Not failing yet -"
    echo "::warning::the data may still be on the branch, committed by a concurrent run. Checking."
  fi
fi

# ---- 3. the verdict: ask the branch -------------------------------------------
git fetch origin "$BR" --quiet 2>/dev/null || true

missing=0
while read -r want path; do
  [ -n "$path" ] || continue
  got="$(git rev-parse "origin/$BR:$path" 2>/dev/null || echo "absent")"
  if [ "$got" = "$want" ]; then
    echo "landed: $path"
  else
    echo "::error::$path is NOT on origin/$BR with the bytes this run computed"
    echo "::error::  intended ${want}"
    echo "::error::  found    ${got}"
    missing=$((missing+1))
  fi
done < "$MANIFEST"
rm -f "$MANIFEST"

if [ "$missing" -eq 0 ]; then
  echo "VERIFIED: every file this run produced is on origin/$BR. This is what success means here."
  exit 0
fi

echo "::error::$missing file(s) did not reach origin/$BR. Red means the data is missing - that is the whole rule."
exit 1
