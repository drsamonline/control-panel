#!/usr/bin/env python3
"""interactions.py — the orphans, the duplication, and where one workflow breaks another.

Three questions the inventory can answer, and none of them are visible by reading a
single workflow:

  1. ORPHANS      a reusable workflow nothing calls. That is either a caller nobody
                  wrote, or a caller that CANNOT exist: a public repo may only call a
                  PUBLIC reusable workflow, so a reusable in a private repo is
                  unreachable from every public app. The verdict differs, so the two
                  must not be reported as the same thing.
  2. DUPLICATION  the same workflow name in many repos. If their sizes differ they have
                  already drifted, and a copy cannot receive a fix. The fix is not
                  "sync them" - it is to make them CALLERS, so there is one to fix.
  3. CROSS-ACTION where one workflow's action breaks another. This is the class of bug
                  that is invisible in any single file and obvious in the graph:
                    RACE      two workflows that write to the same branch. The slow one
                              loses with "failed to push some refs" - which is exactly
                              how the dashboard refresh sat at a stale timestamp for
                              four runs while reporting nothing wrong.
                    LOOP      a workflow that commits AND is triggered by a push. It
                              retriggers itself; `[skip ci]` is what stops it, so a
                              writer without it is a loop waiting to happen.
                    COLLISION two workflows sharing a `concurrency:` group cancel each
                              other - sometimes deliberately, sometimes not.
                    PERMISSION a caller granting fewer permissions than the reusable it
                              calls needs. It fails at run time, not at review time.
                    PILE-UP   many schedules at the same minute - the API rate limit is
                              per token, and they all share one.

    python3 tools/interactions.py --inventory inventory.json --out interactions.json
    python3 tools/interactions.py --self-test
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict

# a private repo cannot be reached by a public caller. That single fact decides half
# the orphan verdicts, so it is stated once, here.
PRIVATE_REPOS = {"forge", "registry", "knowledge", "dumps", "app-companion-forge"}
SHARED_REPOS = {"app-builder", "forge"}          # where a canonical copy belongs


def _wf(inv):
    for r in inv.get("repos", []):
        for w in r.get("workflows", []):
            yield r["repo"], w


def orphans(inv: dict) -> list:
    s = inv.get("summary", {})
    reusables = inv.get("connections", {}).get("reusables", {})
    calls_me = inv.get("connections", {}).get("calls_me", {})
    out = []
    for key in s.get("orphan_reusables", []):
        repo = key.split("/", 1)[0]
        meta = reusables.get(key, {})
        name = meta.get("name") or key.rsplit("/", 1)[-1]
        private = repo in PRIVATE_REPOS
        # is there a public workflow with the same NAME elsewhere? then this private copy
        # is a duplicate, not a gap.
        public_twin = None
        for other_key, m in reusables.items():
            o_repo = other_key.split("/", 1)[0]
            if o_repo != repo and o_repo not in PRIVATE_REPOS and (m.get("name") or "") == name:
                public_twin = other_key
                break
        if private and public_twin:
            verdict, why = "DELETE THE PRIVATE COPY", (
                f"it lives in {repo}, which is PRIVATE, so no public repo can call it - and {public_twin} is the "
                "public twin, which is the one that can be called. Two copies, one reachable: keep the reachable one.")
        elif private:
            verdict, why = "INTENTIONAL (private only)", (
                f"it lives in {repo}, which is private, so only private callers can reach it. If that is the intent, "
                "say so in its header; if not, it is dead.")
        else:
            verdict, why = "WRITE THE CALLER", (
                f"it is public and callable, and nothing calls it. Either a caller was never written, or the thing it "
                "was built for was solved another way - in which case it is dead weight.")
        out.append({"key": key, "repo": repo, "name": name, "private_repo": private,
                    "public_twin": public_twin, "verdict": verdict, "why": why,
                    "callers": calls_me.get(key, [])})
    return out


def dedupe(inv: dict) -> list:
    fam = defaultdict(list)
    for repo, w in _wf(inv):
        if w.get("parse_error"):
            continue
        fam[w.get("name") or w.get("file")].append({"repo": repo, "file": w.get("file"),
                                                    "bytes": w.get("bytes") or 0,
                                                    "flags": w.get("flags") or {}})
    out = []
    for name, members in fam.items():
        repos = {m["repo"] for m in members}
        if len(repos) < 3:
            continue
        sizes = sorted({m["bytes"] for m in members})
        spread = sizes[-1] - sizes[0]
        # the canonical: a copy in a SHARED repo if there is one, else the largest
        shared = [m for m in members if m["repo"] in SHARED_REPOS]
        canonical = (shared or sorted(members, key=lambda m: -m["bytes"]))[0]
        others = [m for m in members if not (m["repo"] == canonical["repo"] and m["file"] == canonical["file"])]
        out.append({
            "name": name, "copies": len(members), "repos": len(repos),
            "distinct_sizes": len(sizes), "spread_bytes": spread, "drifted": spread > 0,
            "canonical": f"{canonical['repo']}/{canonical['file']}",
            "canonical_bytes": canonical["bytes"],
            "plan": ("turn the %d other copies into CALLERS of the canonical, so a fix reaches all of them at once"
                     % len(others)) if len(others) > 1 else "only one other copy",
            "migrate": sorted(f"{m['repo']}/{m['file']}" for m in others)[:40],
            "migrate_count": len(others),
        })
    return sorted(out, key=lambda d: (-d["repos"], -d["spread_bytes"]))


def cross_action(inv: dict) -> dict:
    writers = defaultdict(list)      # repo -> workflows that write
    loops, collisions = [], defaultdict(list)
    perm_gaps = []
    crons = defaultdict(list)
    # what each reusable NEEDS, so a caller granting less can be spotted
    needs = {}
    for repo, w in _wf(inv):
        if w.get("reusable"):
            p = w.get("permissions")
            needs[f"{repo}/.github/workflows/{w['file']}"] = p if isinstance(p, dict) else {}
    for repo, w in _wf(inv):
        fl = w.get("flags") or {}
        f = w.get("file")
        # `gh release` publishes an asset; it does not move a branch. Only a real
        # commit+push can retrigger a push-triggered workflow.
        if fl.get("commits") and fl.get("pushes"):
            writers[repo].append(f)
            # LOOP: it writes AND a push triggers it, and it does not skip itself
            # a push trigger FILTERED to paths this workflow never writes cannot retrigger
            # it. Only an unfiltered push, or one filtered to a path it writes, loops.
            filters = [p.split(":", 1)[1] for p in ((w.get("triggers") or {}).get("paths") or []) if p.startswith("push:")]
            writes = fl.get("writes_paths") or []
            unfiltered = not filters
            matches = any(any(f.endswith(w) or w.endswith(f) for w in writes) for f in filters) if writes else unfiltered
            if fl.get("triggers_on_push") and not fl.get("skip_ci") and (unfiltered or matches):
                loops.append({"repo": repo, "file": f, "name": w.get("name"),
                              "why": "it commits AND is triggered by a push, and it does not mark its own commit "
                                     "`[skip ci]` - so it retriggers itself"})
        # COLLISION: two workflows sharing a concurrency group
        for g in (w.get("concurrency_groups") or []):
            # REPO-SCOPED: the same group in two repos cannot collide. And a shared group
            # with cancel-in-progress:FALSE is deliberate SERIALISATION - two refreshers
            # taking turns - not a collision. Only cancelling each other is a collision.
            if w.get("concurrency_cancels"):
                collisions[(repo, g)].append(f)
        for c in (w.get("triggers") or {}).get("crons", []):
            crons[c].append(f"{repo}/{f}")
        # PERMISSION: a caller granting less than the callee declares
        for call in w.get("calls", []):
            tgt = call.split("@")[0]
            short = tgt.split("/", 1)[1] if tgt.count("/") > 1 else tgt
            key = short if short in needs else (tgt if tgt in needs else None)
            if not key:
                continue
            want = needs.get(key) or {}
            have = w.get("permissions") if isinstance(w.get("permissions"), dict) else {}
            RANK = {"none": 0, "read": 1, "write": 2}
            for scope, level in want.items():
                if scope not in ("contents", "pages", "id-token", "actions", "pull-requests"):
                    continue
                if RANK.get(str(have.get(scope)), 0) >= RANK.get(str(level), 0):
                    continue  # granting MORE is fine; granting less is the failure
                perm_gaps.append({"repo": repo, "file": f, "calls": key, "scope": scope,
                                  "callee_wants": level, "caller_grants": have.get(scope),
                                  "why": f"the caller does not grant `{scope}: {level}`, which the reusable declares. "
                                         "A reusable cannot raise its own permissions above the caller's - it fails "
                                         "at RUN time, not at review time."})

    races = [{"repo": r, "writers": sorted(fs), "count": len(fs),
              "why": "%d workflows in this repo push to the same branch. The slow one loses with \"failed to push "
                     "some refs\"." % len(fs)}
             for r, fs in writers.items() if len(fs) > 1]
    pileups = [{"cron": c, "count": len(fs), "files": sorted(fs)[:12]}
               for c, fs in crons.items() if len(fs) > 1]
    return {
        "races": sorted(races, key=lambda x: -x["count"]),
        "loops": loops,
        "concurrency_collisions": [{"repo": g[0], "group": g[1], "files": sorted(fs)}
                                   for g, fs in collisions.items() if len(fs) > 1],
        "permission_gaps": perm_gaps,
        "schedule_pileups": sorted(pileups, key=lambda x: -x["count"]),
        "totals": {"races": len(races), "loops": len(loops),
                   "concurrency_collisions": sum(1 for g, fs in collisions.items() if len(fs) > 1),
                   "permission_gaps": len(perm_gaps), "schedule_pileups": len(pileups)},
    }


def build(inv: dict) -> dict:
    return {"what": "The orphans, the duplication, and where one workflow breaks another.",
            "orphans": orphans(inv), "dedupe": dedupe(inv), "cross_action": cross_action(inv)}


def self_test():
    p = f = 0
    def ok(n, c):
        nonlocal p, f
        if c:
            p += 1
        else:
            f += 1
            print("FAIL " + n)
    inv = {
        "summary": {"orphan_reusables": ["forge/.github/workflows/release.yml",
                                         "app-builder/.github/workflows/lonely.yml"]},
        "connections": {"reusables": {"forge/.github/workflows/release.yml": {"repo": "forge", "file": "release.yml", "name": "release"},
                                      "app-builder/.github/workflows/release.yml": {"repo": "app-builder", "file": "release.yml", "name": "release"},
                                      "app-builder/.github/workflows/lonely.yml": {"repo": "app-builder", "file": "lonely.yml", "name": "lonely"}},
                        "calls_me": {}},
        "repos": [
            {"repo": "app-builder", "workflows": [
                {"file": "release.yml", "name": "release", "reusable": True, "permissions": {"contents": "write", "pages": "write"},
                 "triggers": {"events": ["workflow_call"]}, "bytes": 900, "flags": {}},
                {"file": "lonely.yml", "name": "lonely", "reusable": True, "permissions": {}, "triggers": {"events": ["workflow_call"]}, "bytes": 10, "flags": {}}]},
            {"repo": "app", "workflows": [
                {"file": "build-apps.yml", "name": "Build + deploy", "reusable": False, "permissions": {"contents": "write"},
                 "triggers": {"events": ["push"], "crons": ["0 3 * * *"]}, "bytes": 1400,
                 "flags": {"pushes": True, "commits": True, "triggers_on_push": True, "skip_ci": False},
                 "calls": ["drsamonline/app-builder/.github/workflows/release.yml@main"]},
                {"file": "health.yml", "name": "health", "reusable": False, "permissions": {"contents": "write"},
                 "triggers": {"events": ["schedule"], "crons": ["0 3 * * *"]}, "bytes": 900,
                 "flags": {"pushes": True, "commits": True, "triggers_on_push": True, "skip_ci": True}, "calls": []}]},
            {"repo": "app2", "workflows": [
                {"file": "build-apps.yml", "name": "Build + deploy", "reusable": False, "permissions": {"contents": "write"},
                 "triggers": {"events": ["push"], "crons": []}, "bytes": 1200, "flags": {}, "calls": []}]},
            {"repo": "app3", "workflows": [
                {"file": "build-apps.yml", "name": "Build + deploy", "reusable": False, "permissions": {"contents": "write"},
                 "triggers": {"events": ["push"], "crons": []}, "bytes": 1500, "flags": {}, "calls": []}]},
        ],
    }
    r = build(inv)
    orph = {o["key"]: o for o in r["orphans"]}
    ok("a private orphan with a public twin says DELETE",
       orph["forge/.github/workflows/release.yml"]["verdict"] == "DELETE THE PRIVATE COPY")
    ok("a public orphan says WRITE THE CALLER",
       orph["app-builder/.github/workflows/lonely.yml"]["verdict"] == "WRITE THE CALLER")
    d = [x for x in r["dedupe"] if x["name"] == "Build + deploy"][0]
    ok("the drifted family is found", d["repos"] == 3 and d["drifted"] is True)
    ok("the drift is measured", d["spread_bytes"] == 300)
    ok("a canonical is chosen", d["canonical"].endswith("build-apps.yml"))
    ok("the migration lists the other copies", d["migrate_count"] == 2)
    ca = r["cross_action"]
    ok("a race is found where two workflows push", any(x["repo"] == "app" for x in ca["races"]))
    ok("a writer triggered by a push WITHOUT [skip ci] is a LOOP", len(ca["loops"]) == 1 and ca["loops"][0]["file"] == "build-apps.yml")
    ok("a writer WITH [skip ci] is not a loop", all(l["file"] != "health.yml" for l in ca["loops"]))
    ok("a shared group with cancel-in-progress:FALSE is serialisation, NOT a collision",
       len(r["cross_action"]["concurrency_collisions"]) == 0)
    ok("a caller granting less than the callee needs is a PERMISSION gap",
       any(g["scope"] == "pages" for g in ca["permission_gaps"]))
    ok("two schedules at the same minute are a PILE-UP", any(x["count"] == 2 for x in ca["schedule_pileups"]))
    print(f"{p} passed, {f} failed")
    return 1 if f else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Orphans, duplication, and cross-action failures.")
    ap.add_argument("--inventory", default="inventory.json")
    ap.add_argument("--out", default="interactions.json")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    inv = json.load(open(a.inventory, encoding="utf-8"))
    r = build(inv)
    json.dump(r, open(a.out, "w", encoding="utf-8"), indent=1)
    print(f"orphans: {len(r['orphans'])}")
    for o in r["orphans"]:
        print(f"  {o['verdict']:<28} {o['key']}")
    print(f"\nduplication: {len(r['dedupe'])} families")
    for d in r["dedupe"][:6]:
        print(f"  {d['name']!r} in {d['repos']} repos, spread {d['spread_bytes']}B -> {d['plan']}")
    ca = r["cross_action"]
    print(f"\ncross-action: {ca['totals']}")
    for x in ca["races"][:5]:
        print(f"  RACE       {x['repo']}: {x['count']} writers")
    for l in ca["loops"][:5]:
        print(f"  LOOP       {l['repo']}/{l['file']}")
    for g in ca["permission_gaps"][:5]:
        print(f"  PERMISSION {g['repo']}/{g['file']} -> {g['calls']} wants {g['scope']}:{g['callee_wants']}, caller has {g['caller_grants']}")
    for x in ca["schedule_pileups"][:5]:
        print(f"  PILE-UP    {x['cron']}: {x['count']} workflows")
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
