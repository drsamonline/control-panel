#!/usr/bin/env python3
"""scan.py — read every workflow in every repository, and say how they connect.

Nothing in this system knew its own shape. There were ~20 reusable workflows, dozens
of callers, schedules, dispatches and triggers, and no single place that answered:

    what runs, when it runs, what calls what, and what is connected to nothing?

This walks every repo, parses every `.github/workflows/*.yml`, and writes one
inventory. For each workflow it records:

    triggers    on: push / pull_request / schedule / workflow_dispatch /
                workflow_call / workflow_run - and the CRON, verbatim
    jobs        each job's name, runner, `needs` (the stage order) and steps
    steps       each step's name, and whether it `uses:` an action or `run:`s shell
    calls       `uses: owner/repo/.github/workflows/x.yml@ref` - a REUSABLE call.
                This is the graph edge: it is how one workflow reaches another repo.
    calls_me    the inverse - who calls THIS workflow. Computed after the walk, so
                a reusable workflow with no callers is visible as dead weight.
    inputs      a workflow_call's declared inputs, and who supplies them
    permissions what it is allowed to do (contents/pages/id-token/...)

Usage:
    python3 tools/scan.py --dir <tree>   --out inventory.json   # a local checkout tree
    python3 tools/scan.py --api          --out inventory.json   # every repo the token sees
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("needs PyYAML:  python3 -m pip install pyyaml")


# --------------------------------------------------------------------------- #
# parsing one workflow
# --------------------------------------------------------------------------- #

SCHEMA_CHECK_OK = True
SCHEMA_CHECK_ERROR = ""


def _schema_problems(text):
    """The workflow-schema check, shared with the lint and the sweep.

    Valid YAML is not a valid WORKFLOW. A file can parse and still be refused at startup
    with no log - which is how ~35 workflows broke on 2026-10-25, when a sweep put
    timeout-minutes on a job whose only key is `uses:`. The scan asked "does it parse?";
    the question is "will it run?".
    """
    global SCHEMA_CHECK_OK, SCHEMA_CHECK_ERROR
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
        import wf_check
        return wf_check.check_text(text, "workflow")
    except Exception as e:
        # FAIL LOUD. This returned [] on any error, so a missing or broken wf_check made
        # every workflow look valid: the one check that catches "parses but will be
        # refused at startup" silently reported "no problems" and the inventory counted
        # it as a pass. A validator that cannot run is not a clean result.
        SCHEMA_CHECK_OK = False
        SCHEMA_CHECK_ERROR = "%s: %s" % (type(e).__name__, e)
        return []

def _triggers(doc) -> dict:
    """`on:` is a string, a list, or a mapping - normalise it to a dict."""
    on = doc.get("on", doc.get(True, None))  # YAML turns a bare `on:` key into True
    out = {"events": [], "crons": [], "paths": [], "types": []}
    if on is None:
        return out
    if isinstance(on, str):
        out["events"] = [on]
        return out
    if isinstance(on, list):
        out["events"] = [str(x) for x in on]
        return out
    if isinstance(on, dict):
        out["events"] = sorted(str(k) for k in on.keys())
        sch = on.get("schedule")
        if isinstance(sch, list):
            for s in sch:
                if isinstance(s, dict) and s.get("cron"):
                    out["crons"].append(str(s["cron"]))
        for ev in ("push", "pull_request", "pull_request_target", "workflow_run"):
            cfg = on.get(ev)
            if isinstance(cfg, dict):
                for p in (cfg.get("paths") or []):
                    out["paths"].append(f"{ev}:{p}")
                for t in (cfg.get("types") or []):
                    out["types"].append(f"{ev}:{t}")
    return out


def _steps(job) -> list:
    """Each step: its name, and whether it uses an action or runs shell."""
    out = []
    for st in (job.get("steps") or []):
        if not isinstance(st, dict):
            continue
        rec = {"name": st.get("name") or st.get("uses") or ("run" if st.get("run") else "?")}
        if st.get("uses"):
            rec["uses"] = str(st["uses"])
        if st.get("run"):
            body = str(st["run"])
            rec["run"] = body.strip().splitlines()[0][:100] if body.strip() else ""
            rec["shell_lines"] = len(body.strip().splitlines())
        if st.get("with"):
            rec["with_keys"] = sorted(str(k) for k in st["with"].keys())
        out.append(rec)
    return out


def parse_workflow(path: str, text: str) -> dict:
    """One workflow -> one record. Never raises: a file that will not parse is a
    finding, not a crash - an invalid workflow is a workflow that silently never runs."""
    rec = {"file": path, "name": os.path.basename(path), "parse_error": None,
           "triggers": {}, "jobs": [], "calls": [], "reusable": False, "inputs": [],
           "permissions": None, "jobs_count": 0, "steps_count": 0}
    try:
        doc = yaml.safe_load(text)
    except Exception as e:
        rec["parse_error"] = f"{type(e).__name__}: {str(e)[:160]}"
        return rec
    if not isinstance(doc, dict):
        rec["parse_error"] = "not a YAML mapping"
        return rec
    rec["name"] = str(doc.get("name") or os.path.basename(path))
    t = _triggers(doc)
    rec["triggers"] = t
    rec["reusable"] = "workflow_call" in t["events"]
    wc = None
    on = doc.get("on", doc.get(True, None))
    if isinstance(on, dict):
        wc = on.get("workflow_call")
    if isinstance(wc, dict) and isinstance(wc.get("inputs"), dict):
        rec["inputs"] = [{"name": k, "required": bool((v or {}).get("required")),
                          "default": (v or {}).get("default")} for k, v in wc["inputs"].items()]
    conc = doc.get("concurrency")
    groups = []
    if isinstance(conc, str):
        groups = [conc]
    elif isinstance(conc, dict) and conc.get("group"):
        groups = [str(conc["group"])]
    rec["concurrency_groups"] = groups
    rec["concurrency_cancels"] = bool(isinstance(conc, dict) and conc.get("cancel-in-progress"))
    perms = doc.get("permissions")
    if isinstance(perms, (str, dict)):
        rec["permissions"] = perms
    jobs = doc.get("jobs") or {}
    if isinstance(jobs, dict):
        for jid, job in jobs.items():
            if not isinstance(job, dict):
                continue
            j = {"id": jid, "name": job.get("name") or jid,
                 "runs_on": job.get("runs-on") or (job.get("uses") and "reusable") or None,
                 "needs": job.get("needs") if isinstance(job.get("needs"), list) else ([job["needs"]] if job.get("needs") else []),
                 "if": (str(job.get("if"))[:120] if job.get("if") else None),
                 "steps": _steps(job)}
            if job.get("uses"):
                j["uses"] = str(job["uses"])
                rec["calls"].append(str(job["uses"]))
            rec["jobs"].append(j)
            rec["steps_count"] += len(j["steps"])
        rec["jobs_count"] = len(rec["jobs"])
        # a STRUCTURE signature: the job ids, their runners, and the step names. Two
        # copies differing only in an input VALUE are the same workflow; two differing
        # in SHAPE have drifted. Comparing bytes cannot tell the difference - 33 callers
        # whose app_name is a different length look drifted by size and are identical.
        rec["signature"] = "|".join(
            "%s:%s:%s" % (j["id"], j.get("runs_on") or (j.get("uses") or "").split("@")[0],
                          ",".join((s2.get("name") or s2.get("uses") or "run") for s2 in j["steps"]))
            for j in sorted(rec["jobs"], key=lambda x: x["id"]))
    # a reusable workflow named in a `uses:` that is NOT a job-level call (rare) is
    # still a call; and any `uses:` anywhere that points at a workflow file is an edge
    for m in re.finditer(r"uses:\s*([\w.\-]+/[\w.\-]+/\.github/workflows/[\w.\-]+\.ya?ml)@([\w.\-/]+)", text):
        edge = f"{m.group(1)}@{m.group(2)}"
        if edge not in rec["calls"]:
            rec["calls"].append(edge)
    return rec


def read_manifest(root: str) -> dict:
    """What fetch.py says it attempted, or an empty stub when there is no manifest.

    The tree alone is not the universe: fetch.py creates a directory only for a
    repository it could read AND that has workflows, so a repository with none, or one
    the token could not read, simply is not there. Reading the manifest is what lets
    `repos_without_workflows` and `skipped` be true statements instead of constants.
    """
    p = os.path.join(root, "_manifest.json")
    if not os.path.isfile(p):
        return {}
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}


def scan_tree(root: str, manifest: dict | None = None) -> list:
    """Every workflow under `<root>/<repo>/.github/workflows/`, plus every repository
    the manifest says was attempted but produced no directory here."""
    out = []
    if not os.path.isdir(root):
        return out
    manifest = manifest or {}
    seen = set()
    for repo in sorted(os.listdir(root)):
        if repo.startswith("_"):
            continue
        seen.add(repo)
        wf_dir = os.path.join(root, repo, ".github", "workflows")
        if not os.path.isdir(wf_dir):
            out.append({"repo": repo, "workflows": [], "note": "no .github/workflows"})
            continue
        recs = []
        for fn in sorted(os.listdir(wf_dir)):
            if not fn.endswith((".yml", ".yaml")):
                continue
            p = os.path.join(wf_dir, fn)
            try:
                text = open(p, encoding="utf-8", errors="replace").read()
            except OSError as e:
                recs.append({"file": fn, "name": fn, "parse_error": f"unreadable: {e}"})
                continue
            rec = parse_workflow(fn, text)
            rec["bytes"] = len(text)
            # body-level flags, so the gap analysis does not have to re-read the file
            # Would GitHub ACCEPT it, not merely parse it? A workflow can be valid YAML and
            # still be refused at startup with no log - which is how ~35 workflows broke on
            # 2026-10-25, when a sweep put timeout-minutes on a job whose only key is `uses:`.
            rec["schema_problems"] = _schema_problems(text)
            rec["flags"] = {
                "invalid_workflow": bool(rec["schema_problems"]),
                "timeout": "timeout-minutes" in text,
                "concurrency": "concurrency" in text,
                "permissions": ("permissions:" in text),
                "matrix": "matrix:" in text,
                "cache": "cache:" in text,
                "skip_ci": "[skip ci]" in text,
                # the cross-action signals: a workflow that WRITES to a branch can race
                # another one that does, and a workflow that both writes and is triggered
                # by a push can loop.
                "pushes": ("git push" in text or "gh release" in text or "git commit" in text),
                "commits": ("git commit" in text),
                "pulls": ("git pull" in text or "git fetch" in text or "rebase" in text),
                "triggers_on_push": bool(re.search(r"on:[\s\S]{0,200}?push:", text)),
                "pages_deploy": ("deploy-pages" in text),
                "writes_paths": sorted(set(re.findall(r"git add[^\n]*?([\w./-]+\.(?:json|md|log|xml|yml|yaml|html|png))", text)))[:8],
            }
            recs.append(rec)
        out.append({"repo": repo, "workflows": recs})
    for entry in manifest.get("repos", []):
        name = entry.get("repo")
        if not name or name in seen:
            continue
        if entry.get("status") == "unreadable":
            out.append({"repo": name, "workflows": [],
                        "note": "could not be read: " + str(entry.get("reason") or "unknown")})
        else:
            out.append({"repo": name, "workflows": [],
                        "note": "no .github/workflows on the default branch"})
    return out


# --------------------------------------------------------------------------- #
# the connections
# --------------------------------------------------------------------------- #
def wire(repos: list) -> dict:
    """Who calls whom. `calls_me` is the useful half: a reusable workflow with none
    is dead weight, and a caller pointing at a workflow that does not exist is a
    pipeline that will fail the first time it runs."""
    reusables = {}                       # "owner/repo/file.yml" -> {"repo":..,"file":..}
    for r in repos:
        for w in r.get("workflows", []):
            if w.get("reusable"):
                reusables[f"{r['repo']}/.github/workflows/{w['file']}"] = {"repo": r["repo"], "file": w["file"], "name": w.get("name")}
    edges, dangling = [], []
    for r in repos:
        for w in r.get("workflows", []):
            for call in w.get("calls", []):
                target = call.split("@")[0].lstrip("/")
                # a local call is `./.github/workflows/x.yml`; a remote one is owner/repo/...
                if target.startswith("./"):
                    key = f"{r['repo']}/" + target[2:]
                else:
                    key = target
                # a call names `owner/repo/.github/workflows/x.yml`; our keys are
                # `repo/.github/workflows/x.yml`, so resolve it before recording -
                # otherwise `calls_me` is keyed by the raw string and never matches
                short = key.split("/", 1)[1] if key.count("/") > 1 else key
                resolved = key if key in reusables else (short if short in reusables else None)
                edges.append({"from_repo": r["repo"], "from": w["file"], "to": key,
                              "resolved": resolved, "at": call.split("@")[-1],
                              "exists": bool(resolved)})
                if not resolved:
                    dangling.append({"from_repo": r["repo"], "from": w["file"], "to": key})
    calls_me = {}
    for e in edges:
        if e["resolved"]:
            calls_me.setdefault(e["resolved"], []).append(f"{e['from_repo']}/{e['from']}")
    orphans = [k for k in reusables if k not in calls_me]
    return {"reusables": reusables, "edges": edges, "calls_me": calls_me,
            "dangling_calls": dangling, "orphan_reusables": sorted(orphans)}


def summarise(repos: list, net: dict, manifest: dict | None = None) -> dict:
    wf = [w for r in repos for w in r.get("workflows", [])]
    manifest = manifest or {}
    return {
        "repos": len(repos),
        "scope": manifest.get("scope") or "unknown",
        "scope_note": manifest.get("scope_note") or "",
        # A repository the fetch could not read is NOT a repository without workflows.
        # Both used to collapse into one silent zero.
        "skipped": [{"repo": e.get("repo"), "reason": e.get("reason")}
                    for e in manifest.get("repos", []) if e.get("status") == "unreadable"],
        # A repository that could not be READ is not a repository without workflows.
        # They are different facts and both used to collapse into this one list.
        "repos_without_workflows": sorted(
            r["repo"] for r in repos
            if not r.get("workflows") and "could not be read" not in str(r.get("note") or "")),
        "workflows": len(wf),
        "reusable": sum(1 for w in wf if w.get("reusable")),
        "scheduled": sum(1 for w in wf if w.get("triggers", {}).get("crons")),
        "dispatchable": sum(1 for w in wf if "workflow_dispatch" in (w.get("triggers", {}).get("events") or [])),
        "parse_errors": [{"repo": r["repo"], "file": w["file"], "error": w["parse_error"]}
                         for r in repos for w in r.get("workflows", []) if w.get("parse_error")],
        # Parsed but would be REFUSED by GitHub. This was computed for every workflow and
        # read by nothing: a workflow that is valid YAML and still dies at startup left no
        # trace in any published view. ~35 broke that way on 2026-10-25.
        "schema_checked": SCHEMA_CHECK_OK,
        "schema_check_error": SCHEMA_CHECK_ERROR,
        "invalid_workflows": [{"repo": r["repo"], "file": w["file"], "problems": w["schema_problems"]}
                              for r in repos for w in r.get("workflows", [])
                              if w.get("schema_problems")],
        "jobs": sum(w.get("jobs_count", 0) for w in wf),
        "steps": sum(w.get("steps_count", 0) for w in wf),
        "edges": len(net["edges"]),
        "dangling_calls": net["dangling_calls"],
        "orphan_reusables": net["orphan_reusables"],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Inventory every workflow and its connections.")
    ap.add_argument("--dir", default="", help="a tree of <repo>/.github/workflows/")
    ap.add_argument("--out", default="inventory.json")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    if not a.dir:
        raise SystemExit("--dir is required (a tree of <repo>/.github/workflows/)")
    manifest = read_manifest(a.dir)
    repos = scan_tree(a.dir, manifest)
    net = wire(repos)
    inv = {"what": "Every workflow in every repo, with its triggers, jobs, steps and calls.",
           "summary": summarise(repos, net, manifest), "connections": net, "repos": repos}
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(inv, f, indent=1)
    s = inv["summary"]
    print(f"repos={s['repos']} workflows={s['workflows']} reusable={s['reusable']} "
          f"scheduled={s['scheduled']} dispatchable={s['dispatchable']} jobs={s['jobs']} steps={s['steps']}")
    print(f"edges={s['edges']} dangling={len(s['dangling_calls'])} orphan-reusables={len(s['orphan_reusables'])} "
          f"parse-errors={len(s['parse_errors'])} invalid={len(s['invalid_workflows'])}")
    print(f"scope={s['scope']}  no-workflows={len(s['repos_without_workflows'])}  "
          f"skipped={len(s['skipped'])}")
    if s["scope"] not in ("all", "unknown"):
        print(f"  NOTE: {s['scope_note']}")
    for w in s["invalid_workflows"][:8]:
        print(f"  INVALID {w['repo']}/{w['file']}: {w['problems'][:1]}")
    for d in s["dangling_calls"][:8]:
        print(f"  DANGLING {d['from_repo']}/{d['from']} -> {d['to']}")
    for o in s["orphan_reusables"][:8]:
        print(f"  ORPHAN   {o} (nothing calls it)")
    for e in s["parse_errors"][:5]:
        print(f"  PARSE    {e['repo']}/{e['file']}: {e['error']}")
    for k in s["skipped"][:8]:
        print(f"  SKIPPED  {k['repo']}: {k['reason']}")
    print(f"wrote {a.out}")
    if s["skipped"]:
        # An inventory of a universe that could not be fully read is not an inventory of
        # the estate. It used to say so nowhere and exit 0.
        print(f"::error::{len(s['skipped'])} repositories could not be read; this inventory "
              f"is INCOMPLETE and must not be read as complete", file=sys.stderr)
        return 1
    if not s["schema_checked"]:
        print("::error::the workflow-schema check could not run (%s), so 'invalid=0' means "
              "'not checked', not 'all valid'" % s["schema_check_error"], file=sys.stderr)
        return 1
    return 0


def self_test():
    """The parser, without a network."""
    p, f = 0, 0
    def ok(name, cond):
        nonlocal p, f
        if cond:
            p += 1
        else:
            f += 1
            print("FAIL " + name)

    w = parse_workflow("x.yml", """
name: Build + deploy
on:
  push:
    branches: [ main ]
    paths: [ index.html ]
  schedule:
    - cron: "47 3 * * *"
  workflow_dispatch:
permissions: { contents: write }
jobs:
  app:
    uses: drsamonline/app-builder/.github/workflows/build.yml@main
    with: { app_name: x }
""")
    ok("the name is read", w["name"] == "Build + deploy")
    ok("events are normalised", w["triggers"]["events"] == ["push", "schedule", "workflow_dispatch"])
    ok("the cron is kept verbatim", w["triggers"]["crons"] == ["47 3 * * *"])
    ok("a path filter is recorded", "push:index.html" in w["triggers"]["paths"])
    ok("a job-level `uses` is an edge", "drsamonline/app-builder/.github/workflows/build.yml@main" in w["calls"])
    ok("it is NOT reusable", w["reusable"] is False)
    ok("the job's needs is a list", isinstance(w["jobs"][0]["needs"], list))

    r = parse_workflow("y.yml", """
name: build
on:
  workflow_call:
    inputs:
      app_name: { required: true, type: string }
      deploy_path: { required: false, default: "" }
jobs:
  exe:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - name: Build
        run: python build.py
  release:
    runs-on: ubuntu-latest
    needs: [exe]
    steps:
      - run: echo hi
""")
    ok("workflow_call makes it reusable", r["reusable"] is True)
    ok("inputs are read with their required flag", [i["name"] for i in r["inputs"]] == ["app_name", "deploy_path"])
    ok("a required input is marked", r["inputs"][0]["required"] is True)
    ok("steps are counted", r["steps_count"] == 3)
    ok("a run step keeps its first line", any(s.get("run") == "python build.py" for j in r["jobs"] for s in j["steps"]))
    ok("an action step keeps its uses", any(s.get("uses") == "actions/checkout@v4" for j in r["jobs"] for s in j["steps"]))
    ok("needs is read", r["jobs"][1]["needs"] == ["exe"])

    bad = parse_workflow("z.yml", "this: [is: not: valid")
    ok("an unparseable file is a FINDING, not a crash", bad["parse_error"] is not None)

    # the graph
    repos = [
        {"repo": "a", "workflows": [{"file": "caller.yml", "name": "caller", "reusable": False,
                                     "calls": ["drsamonline/b/.github/workflows/build.yml@main"]}]},
        {"repo": "b", "workflows": [{"file": "build.yml", "name": "build", "reusable": True, "calls": []},
                                    {"file": "unused.yml", "name": "unused", "reusable": True, "calls": []}]},
    ]
    net = wire(repos)
    ok("the edge is found", len(net["edges"]) == 1 and net["edges"][0]["exists"] is True)
    ok("calls_me names the caller", net["calls_me"].get("b/.github/workflows/build.yml") == ["a/caller.yml"])
    ok("an uncalled reusable is an ORPHAN", "b/.github/workflows/unused.yml" in net["orphan_reusables"])
    ok("a call to a workflow that does not exist is DANGLING",
       len(wire([{"repo": "a", "workflows": [{"file": "c.yml", "name": "c", "reusable": False, "calls": ["drsamonline/nope/.github/workflows/x.yml@main"]}]}])["dangling_calls"]) == 1)

    print(f"{p} passed, {f} failed")
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
