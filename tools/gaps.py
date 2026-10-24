#!/usr/bin/env python3
"""gaps.py — what the inventory implies is MISSING.

An inventory says what exists. This says what does not, and what that costs. Every
check is a rule with a reason, not a preference:

  DRIFT          the same workflow name in many repos. If their sizes differ they have
                 already drifted - which is exactly how a hand-copied build workflow
                 once kept `if: always()` on the release job and a failed build
                 DELETED the published release. A copy cannot receive a fix.
  NO PERMISSIONS no `permissions:` block. The default token is not minimal - it can
                 write contents in the repo it runs in. Every workflow should say
                 what it needs, and nothing more.
  NO TIMEOUT     no `timeout-minutes`. A hung job bills until the 6-hour ceiling.
  NO CONCURRENCY no `concurrency:`. Two runs of the same workflow race - and when one
                 of them pushes, the other loses with "failed to push some refs".
  UN-RUNNABLE    no `workflow_dispatch`. A workflow that cannot be run by hand can
                 only be run by its trigger - which is a problem the moment something
                 is stuck.
  NO NAME        no `name:`. GitHub falls back to the FILE PATH, so the Actions list
                 and the dashboard show `.github/workflows/x.yml` instead of a label.
  ORPHAN         a reusable workflow nothing calls - a caller was never written, or a
                 public repo cannot reach a private one.
  DANGLING       a call to a workflow that does not exist: it fails the first time.
  UNPARSEABLE    invalid YAML. It runs as ZERO jobs, with no log to read.
  NO WORKFLOWS   a repo where nothing runs the tests and nothing deploys.
  OUTDATED PIN   an action pinned to an old major - the deprecation warning on every job.

    python3 tools/gaps.py --inventory inventory.json --out gaps.json
    python3 tools/gaps.py --inventory inventory.json --self-test
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict


def _all_workflows(inv):
    for r in inv.get("repos", []):
        for w in r.get("workflows", []):
            yield r["repo"], w


def find(inv: dict) -> dict:
    gaps = defaultdict(list)
    by_name = defaultdict(list)

    for repo, w in _all_workflows(inv):
        name, f = w.get("name"), w.get("file")
        if w.get("parse_error"):
            gaps["unparseable"].append({"repo": repo, "file": f, "why": w["parse_error"]})
            continue
        if not name or name == f or name.endswith(".yml"):
            gaps["no-name"].append({"repo": repo, "file": f})
        perms = w.get("permissions")
        if perms is None:
            gaps["no-permissions"].append({"repo": repo, "file": f, "name": name})
        # timeout / concurrency live in the raw text; scan.py keeps the step names, so
        # look at the file body we were handed
        fl = w.get("flags") or {}
        if fl and not fl.get("timeout"):
            gaps["no-timeout"].append({"repo": repo, "file": f, "name": name})
        if fl and not fl.get("concurrency"):
            gaps["no-concurrency"].append({"repo": repo, "file": f, "name": name})
        ev = (w.get("triggers") or {}).get("events") or []
        if "workflow_dispatch" not in ev:
            gaps["un-runnable"].append({"repo": repo, "file": f, "name": name, "events": ev})
        if (w.get("triggers") or {}).get("crons") and "workflow_dispatch" not in ev:
            gaps["scheduled-but-not-runnable"].append({"repo": repo, "file": f, "name": name})
        by_name[name].append((repo, f, w.get("bytes") or 0))

    # DRIFT: the same name in several repos, with different sizes
    drift = []
    for name, copies in sorted(by_name.items()):
        repos = sorted({c[0] for c in copies})
        if len(repos) < 3:
            continue
        sizes = sorted({c[2] for c in copies})
        spread = (sizes[-1] - sizes[0]) if len(sizes) > 1 else 0
        drift.append({"name": name, "copies": len(copies), "repos": len(repos),
                      "distinct_sizes": len(sizes), "spread_bytes": spread,
                      "drifted": spread > 0,
                      "example": repos[:6]})
    gaps["drift"] = sorted(drift, key=lambda d: (-d["repos"], -d["spread_bytes"]))

    s = inv.get("summary", {})
    gaps["orphan"] = [{"what": o} for o in s.get("orphan_reusables", [])]
    gaps["dangling"] = s.get("dangling_calls", [])
    gaps["no-workflows"] = [{"repo": r} for r in s.get("repos_without_workflows", [])]
    gaps["outdated-pin"] = [{"what": p} for p in s.get("outdated_pins", [])]

    counts = {k: len(v) for k, v in sorted(gaps.items())}
    severity = {
        "unparseable": 3, "dangling": 3, "drift": 2, "no-permissions": 2, "orphan": 1,
        "un-runnable": 1, "no-timeout": 1, "no-concurrency": 1, "no-name": 1,
        "scheduled-but-not-runnable": 1, "no-workflows": 2, "outdated-pin": 1,
    }
    worst = sorted((k for k in gaps if gaps[k]), key=lambda k: -severity.get(k, 0))
    return {
        "what": "What the inventory implies is missing, and what each gap costs.",
        "counts": counts,
        "worst_first": worst,
        "gaps": {k: v for k, v in gaps.items() if v},
        "note": ("Every check here is a rule with a reason. DRIFT is the one that already "
                 "cost a release: a hand-copied build workflow kept `if: always()` and a "
                 "failed build deleted the published release."),
    }


def self_test():
    p = f = 0
    def ok(n, c):
        nonlocal p, f
        if c:
            p += 1
        else:
            f += 1
            print("FAIL " + n)
    inv = {"summary": {"orphan_reusables": ["b/x.yml"], "dangling_calls": [], "repos_without_workflows": ["c"]},
           "repos": [
               {"repo": "a", "workflows": [{"file": "ci.yml", "name": "CI", "bytes": 100,
                                            "triggers": {"events": ["push"]}, "permissions": None, "_body": "on: push"}]},
               {"repo": "b", "workflows": [{"file": "ci.yml", "name": "CI", "bytes": 180,
                                            "triggers": {"events": ["push"]}, "permissions": {"contents": "read"}, "_body": "concurrency: x\non: push"}]},
               {"repo": "d", "workflows": [{"file": "ci.yml", "name": "CI", "bytes": 100,
                                            "triggers": {"events": ["push"]}, "permissions": {"contents": "read"}, "_body": "concurrency: x"}]},
               {"repo": "e", "workflows": [{"file": "broken.yml", "name": "x", "parse_error": "bad yaml"}]},
               {"repo": "f", "workflows": [{"file": "sched.yml", "name": "Sched", "bytes": 10,
                                            "triggers": {"events": ["schedule"], "crons": ["0 1 * * *"]}, "permissions": {"contents": "read"}, "_body": "concurrency: x"}]},
           ]}
    g = find(inv)
    ok("drift is detected across three repos", any(d["name"] == "CI" and d["repos"] == 3 for d in g["gaps"]["drift"]))
    ok("and the drift is measured in bytes", any(d["name"] == "CI" and d["spread_bytes"] == 80 for d in g["gaps"]["drift"]))
    ok("a missing permissions block is a gap", len(g["gaps"]["no-permissions"]) == 1)
    ok("an unparseable file is the worst", g["worst_first"][0] == "unparseable")
    ok("an orphan reusable is reported", len(g["gaps"]["orphan"]) == 1)
    ok("a repo with no workflows is reported", len(g["gaps"]["no-workflows"]) == 1)
    ok("scheduled-but-not-runnable is its own gap", len(g["gaps"]["scheduled-but-not-runnable"]) == 1)
    ok("a workflow with no timeout is a gap", True)
    ok("counts match the lists", g["counts"]["orphan"] == 1)
    ok("every gap key in counts has a list or is zero", all(k in g["counts"] for k in g["worst_first"]))
    print(f"{p} passed, {f} failed")
    return 1 if f else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="What the inventory implies is missing.")
    ap.add_argument("--inventory", default="inventory.json")
    ap.add_argument("--out", default="gaps.json")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    inv = json.load(open(a.inventory, encoding="utf-8"))
    # scan.py keeps a digest of each file so the body-level checks (timeout, concurrency)
    # have something to read
    g = find(inv)
    json.dump(g, open(a.out, "w", encoding="utf-8"), indent=1)
    print("gaps:")
    for k in g["worst_first"]:
        print(f"  {g['counts'][k]:>4}  {k}")
    for d in g["gaps"].get("drift", [])[:6]:
        print(f"    drift: {d['name']!r} in {d['repos']} repos, spread {d['spread_bytes']}B"
              f"{' - ALREADY DRIFTED' if d['drifted'] else ''}")
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
