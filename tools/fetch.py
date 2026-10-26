#!/usr/bin/env python3
"""fetch.py — pull every repository's workflow files into a local tree.

`scan.py` reads a tree of `<repo>/.github/workflows/`. This builds that tree from
GitHub, so the scan can run on a schedule in CI and the inventory never depends on
someone remembering to refresh it.

It needs no secret for public repos, and reads the private ones with the same token
the runner already has if it can.

    GITHUB_TOKEN=... python3 tools/fetch.py --out tree
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
OWNER = "drsamonline"


def api(path, token, raw=False):
    h = {"Accept": "application/vnd.github+json", "User-Agent": "control-panel-fetch"}
    if token:
        h["Authorization"] = "Bearer " + token
    req = urllib.request.Request(API + path, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            b = r.read()
            return b if raw else json.loads(b)
    except urllib.error.HTTPError as e:
        return {"_error": e.code, "_detail": e.read().decode("utf-8", "replace")[:160]}
    except Exception as e:  # noqa: BLE001 - a network blip must not kill the scan
        return {"_error": type(e).__name__}


def _paged(path, token):
    out, page = [], 1
    while True:
        sep = "&" if "?" in path else "?"
        batch = api(f"{path}{sep}per_page=100&page={page}", token)
        if not isinstance(batch, list) or not batch:
            break
        out += batch
        if len(batch) < 100:
            break
        page += 1
        if page > 20:
            break
    return out


def repos(token):
    """Every repository this token can see, and an honest statement of what that was.

    Tries /user/repos first, which returns private repositories too - but a
    repository-scoped GITHUB_TOKEN returns just this repository from it. The two
    answers are COMPARED rather than either being assumed, so this is correct with
    or without a cross-repository token, and the caller records which one it got.

    Before this, the fallback only ran when the primary returned NOTHING. A
    repository-scoped token returns one repository, which is not nothing, so the scan
    would have covered exactly one repository and reported success.
    """
    owned = _paged("/user/repos?affiliation=owner&sort=pushed", token)
    public = _paged(f"/users/{OWNER}/repos?sort=pushed", token)
    if owned and len(owned) >= len(public):
        return owned, "all", "read with a token that can see every repository the account owns"
    if public:
        return public, "public-only", (
            "the token cannot see private repositories, so this scan covers the PUBLIC ones "
            "only. Add FANOUT_TOKEN to control-panel to include the private ones.")
    return [], "failed", "neither /user/repos nor /users/<owner>/repos returned a repository list"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fetch every repo's workflows into a tree.")
    ap.add_argument("--out", default="tree")
    ap.add_argument("--token", default="")
    a = ap.parse_args(argv)
    token = a.token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    rs, scope, scope_note = repos(token)
    print(f"{len(rs)} repos  (scope: {scope})")
    got, skipped, empty = 0, [], []
    # THE MANIFEST IS THE POINT. scan.py can only see the repositories that produced a
    # directory, so a repository this script skipped was invisible to it - which is why
    # "repos_without_workflows" could never be non-empty and the panel reported itself
    # complete while covering 69 of 83. Every repository attempted is recorded here with
    # its outcome, and scan.py reads this file as the universe.
    manifest = {"scope": scope, "scope_note": scope_note, "attempted": len(rs), "repos": []}
    for r in rs:
        name, branch = r["name"], r.get("default_branch") or "main"
        t = api(f"/repos/{OWNER}/{name}/git/trees/{branch}?recursive=1", token)
        if isinstance(t, dict) and t.get("_error"):
            skipped.append(name)
            manifest["repos"].append({"repo": name, "status": "unreadable",
                                      "reason": f"git tree: {t.get('_error')}"})
            continue
        wfs = [e["path"] for e in t.get("tree", [])
               if e["path"].startswith(".github/workflows/") and e["path"].endswith((".yml", ".yaml"))]
        if not wfs:
            empty.append(name)
            manifest["repos"].append({"repo": name, "status": "no-workflows", "workflows": 0})
            continue
        manifest["repos"].append({"repo": name, "status": "ok", "workflows": len(wfs)})
        for p in wfs:
            blob = api(f"/repos/{OWNER}/{name}/contents/{p}?ref={branch}", token)
            if isinstance(blob, dict) and blob.get("_error"):
                continue
            try:
                import base64
                text = base64.b64decode(blob["content"]).decode("utf-8", "replace")
            except Exception:
                continue
            dst = os.path.join(a.out, name, p)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            open(dst, "w", encoding="utf-8").write(text)
            got += 1
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)
    print(f"workflow files: {got}   repos with none: {len(empty)}   unreadable: {len(skipped)}")
    if skipped:
        print("  unreadable:", ", ".join(skipped[:12]))
    if not rs:
        print("::error::no repositories were listed - refusing to call that a complete scan",
              file=sys.stderr)
        return 1
    if skipped:
        # A partial scan is not a complete one. This returned 0 unconditionally, so the
        # workflow was green while covering whatever the token happened to reach.
        print(f"::error::{len(skipped)} repositories could not be read; the inventory is "
              f"INCOMPLETE", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
