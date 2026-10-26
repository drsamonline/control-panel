#!/usr/bin/env python3
"""wf-check.py — is this a VALID WORKFLOW, not merely valid YAML?

WHY THIS EXISTS

On 2026-10-25 the control panel's sweep added `timeout-minutes` to every job missing one
- including jobs whose only key is `uses:`. A job that CALLS a reusable workflow accepts
name, uses, with, secrets, permissions, needs, if, strategy and concurrency, and NOTHING
else. `timeout-minutes` made ~35 workflows invalid.

Every one of them parsed as YAML. `yaml.safe_load` returned a dict. The repo's own lint
ran, validated the YAML, and PASSED. GitHub accepted the push, then rejected each run at
startup with no log, no job, and a failure named after the file path. It took a re-audit
that asked "what is actually failing?" to find it.

The lesson is narrow and worth writing down: YAML validity is not workflow validity. A
parser answers "is this well-formed?" when the question is "will GitHub accept it?" So
this checks the rules a parser cannot know.

    python3 tools/wf-check.py .github/workflows/*.yml
    python3 tools/wf-check.py --self-test
"""
from __future__ import annotations
import re

import argparse
import glob
import sys

try:
    import yaml
except ImportError:
    sys.exit("needs PyYAML")

# The keys a job that CALLS a reusable workflow may carry. Anything else is invalid.
USES_JOB_KEYS = {"name", "uses", "with", "secrets", "permissions", "needs", "if",
                 "strategy", "concurrency"}
# The keys a normal job may carry.
RUNS_JOB_KEYS = {"name", "runs-on", "steps", "timeout-minutes", "needs", "if", "env",
                 "permissions", "concurrency", "strategy", "continue-on-error", "container",
                 "services", "outputs", "environment", "defaults", "uses", "with", "secrets"}
# Valid step keys.
STEP_KEYS = {"name", "id", "if", "uses", "run", "with", "env", "working-directory",
             "shell", "continue-on-error", "timeout-minutes"}


def check_text(text: str, name: str = "workflow") -> list:
    """Every way this file would be rejected. Empty means it will be accepted."""
    out = []
    try:
        d = yaml.safe_load(text)
    except Exception as e:
        return [f"{name}: not valid YAML ({type(e).__name__})"]
    if not isinstance(d, dict):
        return [f"{name}: the document is not a mapping"]
    if "jobs" not in d or not isinstance(d.get("jobs"), dict) or not d["jobs"]:
        out.append(f"{name}: no jobs")
    # the trigger
    trig = d.get("on", d.get(True))
    if trig is None:
        out.append(f"{name}: no `on:` trigger - it can never run")
    for job_id, job in (d.get("jobs") or {}).items():
        if not isinstance(job, dict):
            out.append(f"{name}: job `{job_id}` is not a mapping")
            continue
        if job.get("uses"):
            # THE BUG THIS TOOL EXISTS FOR
            bad = sorted(set(job) - USES_JOB_KEYS)
            if bad:
                out.append(f"{name}: job `{job_id}` CALLS a reusable workflow but carries "
                           f"{', '.join(bad)} - a uses: job accepts only "
                           f"{', '.join(sorted(USES_JOB_KEYS))}. GitHub rejects the run at startup.")
            if not str(job.get("uses", "")).startswith("./") and "@" not in str(job.get("uses", "")):
                out.append(f"{name}: job `{job_id}` uses `{job['uses']}` with no @ref")
        else:
            if "runs-on" not in job:
                out.append(f"{name}: job `{job_id}` has no `runs-on` and does not call a workflow")
            if "steps" not in job and "runs-on" in job:
                out.append(f"{name}: job `{job_id}` has no `steps`")
            for k in ("steps",):
                for i, st in enumerate(job.get(k) or []):
                    if not isinstance(st, dict):
                        out.append(f"{name}: job `{job_id}` step {i} is not a mapping")
                        continue
                    bad = sorted(set(st) - STEP_KEYS)
                    if bad:
                        out.append(f"{name}: job `{job_id}` step {i} has unknown key(s) {', '.join(bad)}")
                    if "uses" not in st and "run" not in st:
                        out.append(f"{name}: job `{job_id}` step {i} has neither `uses:` nor `run:`")
                    if "uses" in st and "run" in st:
                        out.append(f"{name}: job `{job_id}` step {i} has both `uses:` and `run:`")

    # UNDECLARED INPUTS. `inputs.<name>` is only defined if the trigger declares it. A
    # workflow that reads an input nobody declared gets the EMPTY STRING - so a guard like
    # `if: inputs.killswitch_repo != ''` is always false, the job silently skips, and the
    # dispatch UI shows no field to fill in. forge/self-heal.yml's EMERGENCY STOP was inert
    # for exactly this reason, and wf_check did not catch it: actionlint did, on the one
    # occasion somebody ran actionlint on that file.
    _trig = d.get("on", d.get(True))
    declared = set()
    for trig_name in ("workflow_dispatch", "workflow_call"):
        if isinstance(_trig, dict) and isinstance(_trig.get(trig_name), dict):
            declared |= set((_trig[trig_name].get("inputs") or {}).keys())
    referenced = set(re.findall(r"\binputs\.([A-Za-z_][A-Za-z0-9_-]*)", text))
    for name_ in sorted(referenced - declared):
        out.append(
            f"{name}: reads `inputs.{name_}` but NO trigger declares it. It will always be "
            f"the empty string, so any guard on it is always false and the job silently "
            f"skips. Declare it under workflow_dispatch.inputs (and/or workflow_call.inputs).")

    # THE CONCURRENCY-COLLISION RULE. In a CALLED workflow the `github` context reflects
    # the CALLER, so `github.workflow` is the CALLER's name. A reusable workflow that groups
    # on `${{ github.workflow }}-${{ github.ref }}` therefore resolves to the SAME group as
    # its caller when both are called `backup` - and GitHub will not schedule a called
    # workflow whose group collides with its caller's. The run fails BEFORE any job is
    # created, so there is no log, no check run, no annotation and an empty 22-byte archive.
    # That is exactly how forge/backup.yml failed 40 consecutive runs, and no existing check
    # caught it: it is valid YAML, valid schema, and actionlint-clean.
    trig_ = d.get("on", d.get(True))
    is_reusable = isinstance(trig_, dict) and "workflow_call" in trig_
    conc = d.get("concurrency")
    group = None
    if isinstance(conc, dict):
        group = str(conc.get("group") or "")
    elif isinstance(conc, str):
        group = conc
    # A DISTINGUISHING LITERAL SAVES IT. `group: ${{ github.workflow }}-${{ github.ref }}`
    # reduces to the caller's name and ref, so it collides. But
    # `${{ github.workflow }}-${{ github.ref }}-snapshot` cannot: the literal suffix makes
    # the group unique to this workflow. Flagging that was a false positive on the very
    # workflow this rule was written for - backup/backup.yml, whose `-snapshot` suffix is
    # the F-02 fix itself.
    literal = re.sub(r"\$\{\{[^}]*\}\}", "", group or "")
    literal = re.sub(r"[\s:/_-]+", "", literal)
    if is_reusable and "github.workflow" in (group or "") and not literal:
        out.append(
            f"{name}: this is a REUSABLE workflow (it declares workflow_call) but its "
            f"concurrency group uses `github.workflow`, which resolves to the CALLER's name. "
            f"If the caller has the same workflow name and ref, the groups collide and GitHub "
            f"refuses to schedule the run - it fails at startup with NO log, no check run and "
            f"no annotation. Use a literal, e.g. `group: backup-reusable-${{{{ github.ref }}}}`.")
    return out


def self_test():
    p = f = 0
    def ok(n, c):
        nonlocal p, f
        if c: p += 1
        else:
            f += 1
            print("FAIL " + n)
    # THE exact bug: valid YAML, invalid workflow
    bad = """on: push
jobs:
  app:
    uses: drsamonline/app-builder/.github/workflows/build.yml@main
    timeout-minutes: 30
    permissions: { contents: write }
"""
    issues = check_text(bad, "bad.yml")
    ok("a timeout on a uses: job is caught", any("CALLS a reusable" in i and "timeout-minutes" in i for i in issues))
    ok("it is valid YAML - the parser is not enough", isinstance(yaml.safe_load(bad), dict))
    good = """on: push
jobs:
  app:
    uses: drsamonline/app-builder/.github/workflows/build.yml@main
    permissions: { contents: write }
"""
    ok("the fixed version passes", check_text(good, "good.yml") == [])
    ok("a normal job with a timeout passes",
       check_text("on: push\njobs:\n  t:\n    runs-on: ubuntu-latest\n    timeout-minutes: 5\n    steps:\n      - run: echo hi\n") == [])
    ok("a job with no runs-on and no uses is caught",
       any("no `runs-on`" in i for i in check_text("on: push\njobs:\n  t:\n    steps: []\n")))
    ok("no jobs is caught", any("no jobs" in i for i in check_text("on: push\n")))
    ok("no trigger is caught", any("no `on:`" in i for i in check_text("jobs:\n  t:\n    runs-on: x\n    steps:\n      - run: hi\n")))
    ok("a step with both uses and run is caught",
       any("both" in i for i in check_text("on: push\njobs:\n  t:\n    runs-on: x\n    steps:\n      - uses: a/b@v1\n        run: hi\n")))
    ok("a reusable with no @ref is caught",
       any("no @ref" in i for i in check_text("on: push\njobs:\n  t:\n    uses: drsamonline/x/.github/workflows/y.yml\n")))
    ok("a local reusable (./...) is fine",
       check_text("on: push\njobs:\n  t:\n    uses: ./.github/workflows/x.yml\n") == [])
    print(f"{p} passed, {f} failed")
    return 1 if f else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Is this a valid WORKFLOW, not just valid YAML?")
    ap.add_argument("files", nargs="*")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    files = a.files or glob.glob(".github/workflows/*.yml") + glob.glob(".github/workflows/*.yaml")
    bad = 0
    for path in files:
        try:
            text = open(path, encoding="utf-8").read()
        except OSError as e:
            print(f"{path}: {e}"); bad += 1; continue
        for issue in check_text(text, path):
            print("::error file=%s::%s" % (path, issue.split(": ", 1)[-1]))
            bad += 1
    print(f"{len(files)} workflow(s) checked, {bad} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
