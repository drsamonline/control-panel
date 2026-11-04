#!/usr/bin/env python3
"""plan.py — from the inventory, the GRAND PIPELINE and the routes that need one input.

The inventory says what EXISTS. This says what it MEANS: which workflows belong to
which stage, what is automatic and what needs a hand, and - the point - the routes
that take a job from nothing to delivered while asking the owner for as little as
possible.

Four stages, in the order work actually moves:

    1  SCRATCH    an idea becomes a spec and a repo          intake, new-app, adopt
    2  BUILD      the repo becomes an artefact               test, build, kit-tests, lint
    3  DELIVER    the artefact becomes something public      release, pages, shot, app-deliver
    4  REPAIR     the system keeps itself honest             self-heal, health, audit, sweep,
                                                             self-diagnose, self-update, backup

A route is a chain with an INPUT COUNT. The number is the whole argument: a route
that needs one input is a route the owner will actually use.

    python3 tools/plan.py --inventory inventory.json --out plan.json
    python3 tools/plan.py --inventory inventory.json --self-test
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# the stages. `match` is matched against the workflow NAME, lowercased, so a new
# workflow called "release-x" lands in DELIVER without anyone editing this file.
# --------------------------------------------------------------------------- #
STAGES = [
    {"id": "scratch", "n": 1, "name": "Scratch", "verb": "an idea becomes a repo",
     "what": "Take an idea, give it a spec, and stand up the repository with its guards already on.",
     "match": ["intake", "new-app", "adopt", "spec", "scaffold"],
     "auto": False, "input": "the idea, in one sentence"},
    {"id": "build", "n": 2, "name": "Build", "verb": "a repo becomes an artefact",
     "what": "Prove the code, build every platform it ships on, and lint what was written.",
     "match": ["test", "build", "kit", "lint", "validate", "ci", "normalize"],
     "auto": True, "input": "none - a push runs it"},
    {"id": "deliver", "n": 3, "name": "Deliver", "verb": "an artefact becomes public",
     "what": "Publish the release, deploy the live demo, capture the screenshot, and give every app a landing page.",
     "match": ["release", "pages", "deploy", "shot", "app-deliver", "refresh", "docs", "publish", "store", "products"],
     "auto": True, "input": "none - it follows a build"},
    {"id": "repair", "n": 4, "name": "Repair", "verb": "the system keeps itself honest",
     "what": "Find what broke, re-run what is transient, escalate what is not, audit the kits, learn from the record, and back up before any merge.",
     "match": ["self-heal", "self-diagnose", "self-update", "health", "audit", "sweep", "backup",
               "kit-audit", "repo-learning", "code-compression", "backlog-status", "heartbeat", "fanout"],
     "auto": True, "input": "none - it runs on a schedule"},
]

# --------------------------------------------------------------------------- #
# the routes. Each is a chain with an input count; the count is the argument.
# --------------------------------------------------------------------------- #
ROUTES = [
    {"id": "route-new-app", "name": "A new app, from one sentence", "inputs": 1,
     "you": ["Describe the app (one line) to `intake`"],
     "then": ["intake writes the spec", "the scaffold stands up the repo", "adopt adds the guards",
              "app-deliver adds the landing page, the demo and the pipeline",
              "build makes the EXE, the APK and the Linux binary",
              "release publishes them, pages deploys the demo, shot captures it"],
     "stage_ids": ["scratch", "build", "deliver"],
     "why": "One input. Everything after it is already wired - the only step that needs a human is the idea."},
    {"id": "route-change", "name": "Change an app that exists", "inputs": 0,
     "you": ["Push, or merge a pull request"],
     "then": ["test proves it", "build makes all three platforms", "release publishes",
              "pages deploys the demo", "shot re-captures the screenshot"],
     "stage_ids": ["build", "deliver"],
     "why": "Zero inputs. The push IS the instruction, and the demo and the release can never disagree because they come from one commit."},
    {"id": "route-repair", "name": "Repair and keep honest", "inputs": 0,
     "you": ["Nothing - it is scheduled"],
     "then": ["self-diagnose reports what is broken or silently failing", "self-heal re-runs the transient",
              "health + audit publish the state", "backup snapshots every repo before a merge",
              "repo-learning records what was learned", "backlog-status keeps the queue true"],
     "stage_ids": ["repair"],
     "why": "Zero inputs, and it is the stage that catches the failures nobody is watching."},
    {"id": "route-release", "name": "Ship a release on demand", "inputs": 1,
     "you": ["Press Run workflow on the app's Build + deploy"],
     "then": ["all three platforms build", "the rolling release updates", "the demo redeploys", "the screenshot refreshes"],
     "stage_ids": ["build", "deliver"],
     "why": "One press. For when you want a release now rather than on the next push."},
]


def classify(inv: dict) -> dict:
    """Put every workflow in a stage. A workflow in no stage is REPORTED, not hidden."""
    members = {s["id"]: [] for s in STAGES}
    unplaced = []
    for r in inv.get("repos", []):
        for w in r.get("workflows", []):
            name = (w.get("name") or w.get("file") or "").lower()
            file = (w.get("file") or "").lower()
            hit = None
            for s in STAGES:
                if any(k in name or k in file for k in s["match"]):
                    hit = s["id"]
                    break
            entry = {"repo": r["repo"], "file": w.get("file"), "name": w.get("name"),
                     "reusable": w.get("reusable"), "crons": w.get("triggers", {}).get("crons") or [],
                     "dispatchable": "workflow_dispatch" in (w.get("triggers", {}).get("events") or []),
                     "jobs": w.get("jobs_count", 0), "steps": w.get("steps_count", 0)}
            if hit:
                members[hit].append(entry)
            else:
                unplaced.append(entry)
    return {"members": members, "unplaced": unplaced}


def build(inv: dict) -> dict:
    c = classify(inv)
    stages = []
    for s in STAGES:
        ms = c["members"][s["id"]]
        stages.append({
            "id": s["id"], "n": s["n"], "name": s["name"], "verb": s["verb"], "what": s["what"],
            "auto": s["auto"], "input": s["input"],
            "count": len(ms),
            "scheduled": [m for m in ms if m["crons"]],
            "dispatchable": [m for m in ms if m["dispatchable"]],
            "members": sorted(ms, key=lambda m: (m["repo"], m["file"] or "")),
        })
    # the manual entry point per stage: a dispatchable workflow, preferring a caller
    for st in stages:
        st["manual"] = st["dispatchable"][0] if st["dispatchable"] else None
    total_inputs = sum(r["inputs"] for r in ROUTES[:2])  # the two everyday routes
    return {
        "what": "The grand pipeline: scratch -> build -> deliver -> repair, and the routes through it.",
        "stages": stages,
        "routes": ROUTES,
        "unplaced": sorted(c["unplaced"], key=lambda m: (m["repo"], m["file"] or "")),
        "totals": {
            "workflows": sum(st["count"] for st in stages),
            "unplaced": len(c["unplaced"]),
            "scheduled": sum(len(st["scheduled"]) for st in stages),
            "dispatchable": sum(len(st["dispatchable"]) for st in stages),
            "inputs_for_the_two_everyday_routes": total_inputs,
        },
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

    inv = {"repos": [
        {"repo": "a", "workflows": [{"file": "build-apps.yml", "name": "Build + deploy", "jobs_count": 1, "steps_count": 0,
                                     "triggers": {"events": ["push", "workflow_dispatch"], "crons": []}}]},
        {"repo": "b", "workflows": [{"file": "intake.yml", "name": "Intake", "jobs_count": 2, "steps_count": 5,
                                     "triggers": {"events": ["workflow_dispatch"], "crons": []}},
                                    {"file": "self-heal.yml", "name": "Self-heal", "jobs_count": 1, "steps_count": 3,
                                     "triggers": {"events": ["schedule"], "crons": ["0 20 * * *"]}}]},
        {"repo": "c", "workflows": [{"file": "mystery.yml", "name": "Mystery", "jobs_count": 1, "steps_count": 1,
                                     "triggers": {"events": [], "crons": []}}]},
    ]}
    plan = build(inv)
    by = {s["id"]: s for s in plan["stages"]}
    ok("build-apps lands in BUILD", any(m["file"] == "build-apps.yml" for m in by["build"]["members"]))
    ok("intake lands in SCRATCH", any(m["file"] == "intake.yml" for m in by["scratch"]["members"]))
    ok("self-heal lands in REPAIR", any(m["file"] == "self-heal.yml" for m in by["repair"]["members"]))
    ok("the scheduled one is found", len(by["repair"]["scheduled"]) == 1)
    ok("a workflow in no stage is REPORTED, not hidden", len(plan["unplaced"]) == 1 and plan["unplaced"][0]["file"] == "mystery.yml")
    ok("every workflow is accounted for", plan["totals"]["workflows"] + plan["totals"]["unplaced"] == 4)
    ok("the everyday routes need at most two inputs",
       plan["totals"]["inputs_for_the_two_everyday_routes"] <= 2)
    ok("every route names its stage chain", all(r["stage_ids"] for r in plan["routes"]))
    ok("every stage has an order", [s["n"] for s in plan["stages"]] == [1, 2, 3, 4])
    ok("a manual entry point is picked when one exists", by["build"]["manual"] is not None)
    print(f"{p} passed, {f} failed")
    return 1 if f else 0


# ---- manifest gate: refuse to overwrite a file a human changed ---------------
def _find_manifest_tool() -> Path:
    """Known locations, in order. Raise if neither exists — never silently skip.

    A missing tool is a deployment error and must be a red run, not a quiet no-op:
    silently returning False here would stop every generator in the estate from
    writing anything, and would look like a broken product rather than a broken
    deployment.
    """
    here = Path(__file__).resolve().parent
    candidates = [here / "manifest.py", here / "tools" / "manifest.py"]
    for c in candidates:
        if c.exists():
            return c
    raise SystemExit(
        "manifest.py not found. Expected one of: "
        + ", ".join(str(c) for c in candidates)
        + ". This is a deployment error — the tool must be present beside the generator.")


def _manifest_check(paths: list) -> bool:
    """True = safe to write every path. False = refuse the whole batch.

    Exit 0 (clean, new) -> safe. 3 (drift) -> refuse. 4 (protected, locked) ->
    refuse. Anything else -> refuse, treating it as broken. A gate that fails open
    on the one case it exists for is not a gate.
    """
    tool = _find_manifest_tool()
    try:
        r = subprocess.run([sys.executable, str(tool), "check", *paths],
                           capture_output=True, text=True, timeout=30)
    except Exception as e:
        print(f"::warning::manifest check crashed ({e}) — refusing write", file=sys.stderr)
        return False
    if r.returncode not in (0, 3, 4):
        print(f"::warning::manifest check exited {r.returncode} — refusing write",
              file=sys.stderr)
        return False
    if r.returncode == 0:
        return True
    repo_locked_seen = False
    for line in r.stdout.splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("status") in {"clean", "new"}:
            continue
        if e.get("scope") == "repo":
            if not repo_locked_seen:
                print("::warning::skip all: repository is locked (repo-level)", file=sys.stderr)
                repo_locked_seen = True
        else:
            print(f"::warning::skip {e['path']}: {e['status']} "
                  f"(class={e.get('class')}, "
                  f"manifest={str(e.get('manifest_sha'))[:12]}, "
                  f"committed={str(e.get('committed_sha'))[:12]}, "
                  f"worktree={str(e.get('worktree_sha'))[:12]})", file=sys.stderr)
    return False


def _manifest_record(paths, producer, cls="generated"):
    """Record AFTER the write lands. A record failure is a warning, not a rollback."""
    try:
        tool = _find_manifest_tool()
    except SystemExit:
        return
    try:
        subprocess.run([sys.executable, str(tool), "record", *paths,
                        "--producer", producer, "--class", cls],
                       capture_output=True, text=True, timeout=30)
    except Exception as e:
        print(f"::warning::manifest record failed ({e}) — next run will see drift",
              file=sys.stderr)


def _producer(tool: str) -> str:
    try:
        sha = subprocess.run(["git", "hash-object", "--", __file__],
                             capture_output=True, text=True, check=True).stdout.strip()[:7]
    except Exception:
        sha = "unknown"
    return f"{tool}@{sha}"
# ---- end manifest gate ------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(description="The grand pipeline and the minimum-input routes.")
    ap.add_argument("--inventory", default="inventory.json")
    ap.add_argument("--out", default="plan.json")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    inv = json.load(open(a.inventory, encoding="utf-8"))
    plan = build(inv)
    if not _manifest_check([a.out]):
        print("::warning::plan.json not written: manifest drift needs a human decision",
              file=sys.stderr)
        return 3
    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, a.out)
    _manifest_record([a.out], _producer("plan"), cls="generated")
    print("stages:")
    for s in plan["stages"]:
        print(f"  {s['n']} {s['name']:<9} {s['count']:>3} workflows  ({len(s['scheduled'])} scheduled, "
              f"{len(s['dispatchable'])} runnable by hand)  input: {s['input']}")
    print(f"  unplaced: {plan['totals']['unplaced']}")
    print("routes:")
    for r in plan["routes"]:
        print(f"  {r['inputs']} input(s)  {r['name']}")
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
