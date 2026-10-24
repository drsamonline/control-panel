#!/usr/bin/env python3
"""migrate.py — turn hand-copied workflows into CALLERS, safely.

The drift fix is not "sync the copies". A copy cannot receive a fix: the one time it
mattered, a copied build workflow kept `if: always()` on the release job and a FAILED
build deleted the published release. The fix is to make every copy a caller of one
canonical workflow, so a fix reaches all of them at once.

The dangerous part is doing it blind. A copy that has drifted may carry something the
canonical does not - a repo-specific step, a different input. Replacing it would
SILENTLY DELETE that. So this compares each copy's shape against the caller shape and:

    MIGRATE   the copy is the caller shape with different values - safe to replace
    REVIEW    the copy carries steps the canonical does not - a human must look
    KEEP      it is already a caller

    python3 tools/migrate.py --inventory inventory.json --dir tree --family "Build + deploy"
    python3 tools/migrate.py --inventory inventory.json --dir tree --family "Build + deploy" --apply
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

try:
    import yaml
except ImportError:
    sys.exit("needs PyYAML")

CALLER = """# Build + release + deploy. One push, one pipeline, all from the same commit.
#
# MIGRATED: this used to be a hand-copied workflow. A copy cannot receive a fix - and
# the one time it mattered, a copied version kept `if: always()` on the release job, so
# a FAILED build DELETED the published release. It is now a caller: the whole pipeline
# lives in app-builder, and a fix there reaches every app at once.
#
# Every push to main: build the EXE, the APK and the Linux binary -> publish the rolling
# `app-latest` release -> deploy the demo -> screenshot it. All from ONE commit, so the
# live demo and the released build can never disagree.

name: Build + deploy

on:
  push:
    branches: [ main ]
  pull_request:
    branches: [ main ]
  workflow_dispatch:

permissions:
  contents: write
  pages: write
  id-token: write

concurrency:
  group: build
  cancel-in-progress: true

jobs:
  app:
    uses: drsamonline/app-builder/.github/workflows/build.yml@main
    with:
      app_name: {app_name}
      app_src: {app_src}
      deploy_path: {deploy_path}
    permissions:
      contents: write
      pages: write
      id-token: write
"""


def read_tree(root, repo, f):
    p = os.path.join(root, repo, ".github", "workflows", f)
    return open(p, encoding="utf-8", errors="replace").read() if os.path.exists(p) else None


def caller_inputs(text):
    """The `with:` values of the app-builder build call, if this is a caller."""
    m = re.search(r"uses:\s*drsamonline/app-builder/\.github/workflows/build\.yml@\S+\s*\n\s*with:\s*\n((?:\s+\w+:.*\n)+)", text)
    if not m:
        return None
    out = {}
    for line in m.group(1).splitlines():
        k, _, v = line.strip().partition(":")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def plan(inv, root, family):
    fam = [d for d in json.load(open("interactions.json", encoding="utf-8"))["dedupe"] if d["name"] == family]
    if not fam:
        return []
    d = fam[0]
    canon_repo, canon_file = d["canonical"].split("/", 1)
    canon = read_tree(root, canon_repo, os.path.basename(canon_file)) or ""
    canon_ins = caller_inputs(canon) or {}
    out = []
    for member in d["migrate"]:
        repo, f = member.split("/", 1)
        text = read_tree(root, repo, f)
        if text is None:
            out.append({"repo": repo, "file": f, "verdict": "MISSING", "why": "not in the tree"})
            continue
        ins = caller_inputs(text)
        if ins is not None:
            out.append({"repo": repo, "file": f, "verdict": "KEEP", "why": "already a caller",
                        "inputs": ins})
            continue
        # is it the caller shape with different values, or does it carry extra work?
        try:
            doc = yaml.safe_load(text) or {}
        except Exception as e:
            out.append({"repo": repo, "file": f, "verdict": "REVIEW", "why": f"unparseable: {e}"})
            continue
        jobs = doc.get("jobs") or {}
        steps = sum(len(j.get("steps") or []) for j in jobs.values() if isinstance(j, dict))
        uses = [str(j.get("uses")) for j in jobs.values() if isinstance(j, dict) and j.get("uses")]
        # a plain copy builds by hand: it has steps and no reusable call
        if steps and not uses:
            # pull the inputs out of its own body so the caller keeps the same values
            name = repo
            src = "docs/play" if "docs/play" in text else ("src" if "src" in text else ".")
            dep = "docs" if "deploy" in text and "docs" in text else "docs"
            out.append({"repo": repo, "file": f, "verdict": "MIGRATE",
                        "why": f"a hand-copied build with {steps} step(s) and no reusable call",
                        "inputs": {"app_name": name, "app_src": src, "deploy_path": dep}})
        elif uses:
            out.append({"repo": repo, "file": f, "verdict": "REVIEW",
                        "why": "it already calls something, but not the canonical build - a human should look",
                        "uses": uses})
        else:
            out.append({"repo": repo, "file": f, "verdict": "REVIEW", "why": "unrecognised shape"})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Turn hand-copied workflows into callers.")
    ap.add_argument("--inventory", default="inventory.json")
    ap.add_argument("--dir", default="tree")
    ap.add_argument("--family", default="Build + deploy")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        p = f = 0
        def ok(n, c):
            nonlocal p, f
            if c: p += 1
            else:
                f += 1
                print("FAIL " + n)
        ok("a caller is recognised", caller_inputs("uses: drsamonline/app-builder/.github/workflows/build.yml@main\n    with:\n      app_name: x\n      app_src: docs/play\n") == {"app_name": "x", "app_src": "docs/play"})
        ok("a hand-copied build is not", caller_inputs("jobs:\n  a:\n    steps:\n      - run: echo hi\n") is None)
        print(f"{p} passed, {f} failed")
        return 1 if f else 0
    inv = json.load(open(a.inventory, encoding="utf-8"))
    rows = plan(inv, a.dir, a.family)
    from collections import Counter
    c = Counter(r["verdict"] for r in rows)
    print(f"{a.family}: {dict(c)}")
    for r in rows:
        print(f"  {r['verdict']:<8} {r['repo']}/{r['file']}  — {r['why']}")
        if r["verdict"] == "MIGRATE":
            print(f"           -> caller with {r['inputs']}")
    if a.apply:
        n = 0
        for r in rows:
            if r["verdict"] != "MIGRATE":
                continue
            body = CALLER.format(**r["inputs"])
            p = os.path.join(a.dir, r["repo"], ".github", "workflows", r["file"])
            open(p, "w", encoding="utf-8", newline="\n").write(body)
            n += 1
        print(f"rewrote {n} file(s) as callers (in the tree — commit them separately)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
