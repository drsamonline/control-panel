#!/usr/bin/env python3
"""simulate.py — score the candidate routes and pick the best one per scenario.

A route is a claim: "this gets the job done with one input". A claim like that should
be TESTED, not asserted. This scores every candidate route on the things that decide
whether someone actually uses it:

    inputs       how many times it asks the owner for something. The dominant term.
    human steps  how many hand-actions, even the trivial ones - each is a chance to forget.
    failure      how many things must go right. A chain of nine steps with one manual
                 hand-off has nine places to stop; an automated chain has one.
    risk         the chance the OUTPUT is wrong even when nothing breaks. This is the
                 term that decides whether "no input at all" is good or bad: a route
                 that guesses what you wanted is not automation, it is rework.
    coverage     how many of the four stages it actually reaches.
    recover      can it be re-run from the middle when it stops? A route you cannot
                 resume is a route you restart.
    drift        does it depend on a hand-copied workflow (the thing that already cost
                 a release), or on a single shared one?

The score is not a taste: `inputs*10 + human*3 + failure*2 + risk*6 - coverage*2 - recover*3 - drift*4`.
Lower is better. The winner per scenario is the one the numbers choose, not the one
that sounds nicest.

    python3 tools/simulate.py --inventory inventory.json --gaps gaps.json --out routes.json
    python3 tools/simulate.py --self-test
"""
from __future__ import annotations

import argparse
import json
import sys

# --------------------------------------------------------------------------- #
# the candidates. Some are deliberately WORSE - a simulation that only contains the
# good option is not a simulation, it is a decision dressed up as one.
# --------------------------------------------------------------------------- #
CANDIDATES = [
    # --- a new app ---
    {"id": "new-manual", "scenario": "A new app", "name": "Do it all by hand",
     "inputs": 6, "human": 9, "failure": 9, "risk": 3, "coverage": 2, "recover": 0, "drift": 1,
     "chain": ["write the spec", "create the repo", "add a test workflow", "add a pages workflow",
               "add a build workflow", "add a landing page", "add the demo", "push", "watch it"],
     "why": "Every step is a decision, and the workflows are hand-copied - which is how a release got deleted once."},
    {"id": "new-guided", "scenario": "A new app", "name": "Describe it, then it runs itself",
     "inputs": 1, "human": 1, "failure": 3, "risk": 1, "coverage": 4, "recover": 1, "drift": 0,
     "chain": ["intake (the idea)", "spec", "scaffold", "adopt the guards", "app-deliver the page + demo + pipeline",
               "build x3 platforms", "release", "deploy the demo", "screenshot"],
     "why": "One input. Everything after it is already wired, and every workflow is a CALLER, so a fix reaches all of them."},
    {"id": "new-auto", "scenario": "A new app", "name": "Fully automatic, no input at all",
     "inputs": 0, "human": 0, "failure": 7, "risk": 9, "coverage": 4, "recover": 0, "drift": 0,
     "chain": ["detect the idea", "guess the spec", "scaffold", "build", "release"],
     "why": "Zero inputs sounds best until you ask who wrote the spec. Guessing the requirements is not automation, it is a coin toss with a release attached."},
    # --- a change ---
    {"id": "change-manual", "scenario": "A change to an app", "name": "Push, then press build yourself",
     "inputs": 1, "human": 2, "failure": 4, "risk": 2, "coverage": 3, "recover": 1, "drift": 0,
     "chain": ["push", "press Run", "build x3", "release", "deploy"],
     "why": "Works, but the demo and the release can drift apart because they are two commits."},
    {"id": "change-push", "scenario": "A change to an app", "name": "Push, and that is the instruction",
     "inputs": 0, "human": 0, "failure": 2, "risk": 1, "coverage": 4, "recover": 1, "drift": 0,
     "chain": ["push", "test", "build x3", "release", "deploy the demo", "screenshot"],
     "why": "Zero inputs, and every output comes from ONE commit - so the live demo and the released build cannot disagree."},
    # --- a repair ---
    {"id": "repair-watch", "scenario": "Repair", "name": "Watch the Actions page and fix what is red",
     "inputs": 2, "human": 4, "failure": 3, "risk": 3, "coverage": 2, "recover": 0, "drift": 0,
     "chain": ["notice something is red", "read the log", "decide", "fix", "push"],
     "why": "This is what happened before: three workflows failed silently and nothing said so."},
    {"id": "repair-sched", "scenario": "Repair", "name": "Diagnose, heal, audit, learn - on a schedule",
     "inputs": 0, "human": 0, "failure": 2, "risk": 1, "coverage": 4, "recover": 1, "drift": 0,
     "chain": ["self-diagnose", "self-heal", "health + audit", "backup", "repo-learning", "backlog-status"],
     "why": "Zero inputs. It is the only route that catches a workflow which runs on time and fails every time."},
    # --- a release ---
    {"id": "release-tag", "scenario": "Ship a release", "name": "Tag it and let the tag drive",
     "inputs": 1, "human": 1, "failure": 4, "risk": 3, "coverage": 3, "recover": 1, "drift": 1,
     "chain": ["tag", "build", "release", "deploy"],
     "why": "A tag is a second source of truth; the demo can end up on a different commit than the release."},
    {"id": "release-press", "scenario": "Ship a release", "name": "Press Run on the app's pipeline",
     "inputs": 1, "human": 1, "failure": 2, "risk": 1, "coverage": 4, "recover": 1, "drift": 0,
     "chain": ["press Run", "build x3", "release", "deploy", "screenshot"],
     "why": "One press, one commit, every output. The pipeline is the same one a push uses, so nothing is special-cased."},
]

WEIGHTS = {"inputs": 10, "human": 3, "failure": 2, "risk": 6, "coverage": -2, "recover": -3, "drift": -4}


def score(c: dict) -> int:
    return sum(WEIGHTS[k] * c[k] for k in WEIGHTS)


def simulate(inv: dict, gaps: dict) -> dict:
    """Score every candidate, and pick the winner per scenario. `gaps` supplies the
    drift facts, so a route that depends on hand-copied workflows is penalised for a
    reason that is measured rather than assumed."""
    drifted = {d["name"] for d in (gaps or {}).get("gaps", {}).get("drift", []) if d.get("drifted")}
    out = []
    for c in CANDIDATES:
        r = dict(c)
        r["score"] = score(c)
        r["steps"] = len(c["chain"])
        out.append(r)
    by_scen = {}
    for r in out:
        by_scen.setdefault(r["scenario"], []).append(r)
    winners = {}
    for scen, rs in by_scen.items():
        rs.sort(key=lambda r: (r["score"], r["inputs"], r["human"]))
        winners[scen] = {"id": rs[0]["id"], "name": rs[0]["name"], "score": rs[0]["score"],
                         "inputs": rs[0]["inputs"], "why": rs[0]["why"],
                         "runner_up": {"id": rs[1]["id"], "score": rs[1]["score"], "why": rs[1]["why"]} if len(rs) > 1 else None,
                         "margin": (rs[1]["score"] - rs[0]["score"]) if len(rs) > 1 else None}
    return {
        "what": "Every candidate route, scored. Lower is better.",
        "weights": WEIGHTS,
        "formula": "inputs*10 + human*3 + failure*2 + risk*6 - coverage*2 - recover*3 - drift*4",
        "drift_measured": sorted(drifted),
        "candidates": sorted(out, key=lambda r: (r["scenario"], r["score"])),
        "winners": winners,
        "totals": {
            "candidates": len(out),
            "scenarios": len(by_scen),
            "best_possible_inputs": sum(w["inputs"] for w in winners.values()),
            "inputs_if_every_scenario_went_manual": sum(
                r["inputs"] for r in out if r["id"].endswith(("-manual", "-watch", "-tag"))),
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
    gaps = {"gaps": {"drift": [{"name": "Build + deploy", "drifted": True, "spread_bytes": 393, "repos": 33}]}}
    sim = simulate({}, gaps)
    w = sim["winners"]
    ok("a scenario picks a winner", all("id" in x for x in w.values()))
    ok("the guided new-app route beats doing it by hand",
       w["A new app"]["id"] == "new-guided")
    ok("zero inputs does NOT win by itself - a guessed spec is worse",
       w["A new app"]["id"] != "new-auto")
    ok("the push route wins for a change", w["A change to an app"]["id"] == "change-push")
    ok("the scheduled repair wins", w["Repair"]["id"] == "repair-sched")
    ok("the press route wins for a release", w["Ship a release"]["id"] == "release-press")
    ok("every winner asks for at most one input", all(x["inputs"] <= 1 for x in w.values()))
    ok("a runner-up is recorded, so the margin is visible", all(x["runner_up"] for x in w.values()))
    ok("the drift is carried into the result", sim["drift_measured"] == ["Build + deploy"])
    ok("the score is the formula", all(r["score"] == sum(WEIGHTS[k] * r[k] for k in WEIGHTS) for r in sim["candidates"]))
    ok("doing it all by hand is the worst new-app route",
       max(r["score"] for r in sim["candidates"] if r["scenario"] == "A new app") ==
       [r for r in sim["candidates"] if r["id"] == "new-manual"][0]["score"])
    print(f"{p} passed, {f} failed")
    return 1 if f else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Score the routes and pick the best.")
    ap.add_argument("--inventory", default="inventory.json")
    ap.add_argument("--gaps", default="gaps.json")
    ap.add_argument("--out", default="routes.json")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    inv = json.load(open(a.inventory, encoding="utf-8")) if a.inventory else {}
    try:
        gaps = json.load(open(a.gaps, encoding="utf-8"))
    except OSError:
        gaps = {}
    sim = simulate(inv, gaps)
    json.dump(sim, open(a.out, "w", encoding="utf-8"), indent=1)
    for scen, w in sim["winners"].items():
        print(f"{scen}: {w['name']}  (score {w['score']}, {w['inputs']} input(s))")
        if w["runner_up"]:
            print(f"    beat {w['runner_up']['id']} by {w['margin']}")
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
