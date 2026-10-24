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


def repos(token):
    out, page = [], 1
    while True:
        batch = api(f"/user/repos?per_page=100&page={page}&affiliation=owner", token)
        if not isinstance(batch, list) or not batch:
            break
        out += batch
        if len(batch) < 100:
            break
        page += 1
    if not out:  # an installation token cannot list; fall back to the public view
        page = 1
        while True:
            batch = api(f"/users/{OWNER}/repos?per_page=100&page={page}", token)
            if not isinstance(batch, list) or not batch:
                break
            out += batch
            if len(batch) < 100:
                break
            page += 1
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fetch every repo's workflows into a tree.")
    ap.add_argument("--out", default="tree")
    ap.add_argument("--token", default="")
    a = ap.parse_args(argv)
    token = a.token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    rs = repos(token)
    print(f"{len(rs)} repos")
    got, skipped, empty = 0, [], []
    for r in rs:
        name, branch = r["name"], r.get("default_branch") or "main"
        t = api(f"/repos/{OWNER}/{name}/git/trees/{branch}?recursive=1", token)
        if isinstance(t, dict) and t.get("_error"):
            skipped.append(name)
            continue
        wfs = [e["path"] for e in t.get("tree", [])
               if e["path"].startswith(".github/workflows/") and e["path"].endswith((".yml", ".yaml"))]
        if not wfs:
            empty.append(name)
            continue
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
    print(f"workflow files: {got}   repos with none: {len(empty)}   unreadable: {len(skipped)}")
    if skipped:
        print("  unreadable:", ", ".join(skipped[:12]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
