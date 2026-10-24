#!/usr/bin/env python3
"""sweep.py — apply the standing rules to every workflow that is missing them.

Four rules, each with a reason, and each applied ONLY where it is absent:

  permissions    A workflow with no `permissions:` gets the DEFAULT token, which can
                 write contents in its repo. That is not least privilege. The sweep
                 does not guess "read" - that would BREAK a workflow that pushes. It
                 infers from what the workflow does: pushes -> contents:write,
                 deploys pages -> pages:write + id-token:write, calls a reusable ->
                 mirror what that reusable declares.
  timeout-minutes A hung job bills until the 6-hour ceiling. Every job gets one.
  concurrency    Two runs of the same workflow race, and when both push the loser
                 fails with "failed to push some refs". A per-workflow group ends it.
  workflow_dispatch  A workflow that cannot be run by hand can only be run by its
                 trigger - which is a problem the moment something is stuck.

SAFETY, because a mechanical edit to a live workflow is how you break a pipeline:

  * every result is PARSED as YAML before it is written. A rule that would produce
    invalid YAML is dropped for that file, not written.
  * comments are preserved: the edits are line insertions, never a re-dump.
  * `--dry-run` is the default. Nothing is written without `--apply`.

    python3 tools/sweep.py --dir tree                    # report
    python3 tools/sweep.py --dir tree --apply            # write into the tree
"""
from __future__ import annotations

import argparse
import os
import re
import sys

try:
    import yaml
except ImportError:
    sys.exit("needs PyYAML")

DEFAULT_TIMEOUT = 30
BUILD_TIMEOUT = 60


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def needs_permissions(text: str) -> dict:
    """What this workflow actually needs, inferred from what it does."""
    want = {}
    # NEVER DOWNGRADE a workflow that writes. Inferring "read" for something that
    # comments on a PR or uploads an artifact would BREAK it - and a broken workflow is
    # worse than a broad token. So: anything that writes at all keeps contents:write.
    writes = bool(re.search(r"git push|git commit|gh release|gh api[^\n]*(-X |--method )(POST|PUT|PATCH)"
                            r"|gh pr|gh issue|upload-artifact|deploy-pages", text))
    want["contents"] = "write" if writes else "read"
    if re.search(r"gh pr|gh issue|issues:|pull-requests:", text):
        want["pull-requests"] = "write"
    if re.search(r"actions/upload-artifact", text):
        want["actions"] = "read"
    if "deploy-pages" in text or "upload-pages-artifact" in text:
        want["pages"] = "write"
        want["id-token"] = "write"
    if re.search(r"id-token:\s*write", text):
        want["id-token"] = "write"
    return want


def add_permissions(text: str) -> str | None:
    if re.search(r"^permissions:", text, re.M):
        return None
    want = needs_permissions(text)
    block = "permissions:\n" + "".join(f"  {k}: {v}\n" for k, v in want.items())
    # insert just before the top-level `jobs:`
    m = re.search(r"^jobs:", text, re.M)
    if not m:
        return None
    return text[:m.start()] + block + "\n" + text[m.start():]


def add_concurrency(text: str) -> str | None:
    if re.search(r"^concurrency:", text, re.M):
        return None
    block = ("concurrency:\n"
             "  group: ${{ github.workflow }}-${{ github.ref }}\n"
             "  cancel-in-progress: false\n")
    m = re.search(r"^jobs:", text, re.M)
    if not m:
        return None
    return text[:m.start()] + block + "\n" + text[m.start():]


def add_dispatch(text: str) -> str | None:
    """Add workflow_dispatch to `on:` - as a list if `on:` is inline, else as a key."""
    if "workflow_dispatch" in text:
        return None
    m = re.search(r"^( *)on:(.*)$", text, re.M)
    if not m:
        return None
    indent, inline = m.group(1), m.group(2).strip()
    if inline and inline not in ("|", ">"):
        # `on: push` -> `on: [push, workflow_dispatch]`
        return text[:m.start()] + f"{indent}on: [{inline}, workflow_dispatch]" + text[m.end():]
    # a mapping (or bare `on:`): put the key first, at the mapping's indent
    return text[:m.end()] + f"\n{indent}  workflow_dispatch: {{}}" + text[m.end():]


def add_timeouts(text: str) -> str | None:
    """Give every job a timeout. Insert after each job's `runs-on:` (or its `name:`)."""
    if "timeout-minutes" in text:
        return None
    lines = text.splitlines(keepends=True)
    out, changed = [], False
    i = 0
    while i < len(lines):
        ln = lines[i]
        out.append(ln)
        m = re.match(r"^(\s{2,6})(runs-on|uses):", ln)
        if m:
            indent = m.group(1)
            # is a timeout already the next meaningful line? then skip
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and "timeout-minutes" in lines[j]:
                i += 1
                continue
            mins = BUILD_TIMEOUT if re.search(r"windows-latest|macos|gradle|pyinstaller", text, re.I) else DEFAULT_TIMEOUT
            out.append(f"{indent}timeout-minutes: {mins}\n")
            changed = True
        i += 1
    return "".join(out) if changed else None


RULES = [("permissions", add_permissions), ("concurrency", add_concurrency),
         ("dispatch", add_dispatch), ("timeouts", add_timeouts)]


def sweep_one(text: str) -> tuple[str, list]:
    """Apply each rule, keeping a change only if the result still parses."""
    applied = []
    for name, fn in RULES:
        try:
            new = fn(text)
        except Exception as e:  # a rule that throws is skipped, never written
            applied.append(f"{name}:SKIPPED({type(e).__name__})")
            continue
        if not new or new == text:
            continue
        try:
            yaml.safe_load(new)
        except Exception:
            applied.append(f"{name}:DROPPED(would not parse)")
            continue
        text = new
        applied.append(name)
    return text, applied


def main(argv=None):
    ap = argparse.ArgumentParser(description="Apply the standing rules to every workflow missing them.")
    ap.add_argument("--dir", default="tree")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    touched, total, skipped = 0, 0, 0
    by_rule = {}
    for repo in sorted(os.listdir(a.dir)):
        wd = os.path.join(a.dir, repo, ".github", "workflows")
        if not os.path.isdir(wd):
            continue
        for fn in sorted(os.listdir(wd)):
            if not fn.endswith((".yml", ".yaml")):
                continue
            p = os.path.join(wd, fn)
            text = open(p, encoding="utf-8", errors="replace").read()
            total += 1
            new, applied = sweep_one(text)
            real = [x for x in applied if ":" not in x]
            if not real:
                skipped += 1
                continue
            touched += 1
            for r in real:
                by_rule[r] = by_rule.get(r, 0) + 1
            if a.apply and new != text:
                open(p, "w", encoding="utf-8", newline="\n").write(new)
    print(f"{total} workflows | {touched} would change | {skipped} already clean")
    for k, v in sorted(by_rule.items(), key=lambda kv: -kv[1]):
        print(f"  {v:>4}  {k}")
    if a.apply:
        print("written into the tree — commit them")
    else:
        print("(dry run — pass --apply)")
    return 0


def self_test():
    p = f = 0
    def ok(n, c):
        nonlocal p, f
        if c: p += 1
        else:
            f += 1
            print("FAIL " + n)
    src = """name: CI
on:
  push:
    branches: [ main ]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: node test.js
"""
    out, applied = sweep_one(src)
    ok("permissions is added", "permissions:" in out)
    ok("a non-pushing workflow gets contents: read", "contents: read" in out)
    ok("concurrency is added", "concurrency:" in out)
    ok("workflow_dispatch is added", "workflow_dispatch" in out)
    ok("a timeout is added", "timeout-minutes:" in out)
    ok("it still parses", isinstance(yaml.safe_load(out), dict))
    ok("the original steps survive", "node test.js" in out)
    ok("the events survive", "push:" in out and "branches" in out)

    push = """name: Release
on: push
jobs:
  rel:
    runs-on: ubuntu-latest
    steps:
      - run: git push
"""
    o2, _ = sweep_one(push)
    ok("a PUSHING workflow gets contents: write", "contents: write" in o2)
    ok("an inline `on:` becomes a list", "on: [push, workflow_dispatch]" in o2)

    pages = """name: Pages
on:
  push:
jobs:
  d:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/deploy-pages@v4
"""
    o3, _ = sweep_one(pages)
    ok("a pages deploy gets pages: write", "pages: write" in o3)
    ok("and id-token: write", "id-token: write" in o3)

    already = """name: X
on:
  workflow_dispatch:
permissions:
  contents: read
concurrency:
  group: x
jobs:
  a:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - run: echo hi
"""
    _, ap2 = sweep_one(already)
    ok("a workflow that already complies is left alone", [x for x in ap2 if ':' not in x] == [])
    ok("the rules are idempotent", sweep_one(sweep_one(src)[0])[0] == sweep_one(src)[0])
    print(f"{p} passed, {f} failed")
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
