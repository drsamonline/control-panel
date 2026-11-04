#!/usr/bin/env python3
"""manifest.py — per-file provenance for a Forge-managed repository.

MANIFEST.json at the repo root. Schema 1:

    {"schema": 1, "locked": false, "files": {
      "<path>": {"sha": "<git-blob-sha>", "producer": "tool@<sha>",
                 "class": "generated|canonical|hand|agent|locked",
                 "at": "<ISO8601>"}}}

A repo-level "locked": true short-circuits every path to locked.

The verdict is a three-way blob comparison — manifest sha, HEAD sha, worktree sha.
No git history, no author detection. Correct at fetch-depth: 1, which is what every
generator checkout in this estate uses.

check exit codes (JSON lines on stdout, one object per path):
    0  clean, new (file absent, safe to create)
    3  unrecorded, deleted, uncommitted, committed-drift  (refuse; human decision)
    4  protected, locked (refuse; by class)
    any other -> hard error (callers must fail closed)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = 1
DEFAULT_MANIFEST = "MANIFEST.json"
RC_CLEAN, RC_DRIFT, RC_PROTECTED = 0, 3, 4

# A class a generator must never silently overwrite *or* reclassify. `canonical` is
# here because record() would otherwise let any generator downgrade the pinned
# build-apps.yml caller to `generated` by recording it without --class canonical.
REFUSE_CLASSES = frozenset({"hand", "agent", "locked", "canonical"})


def repo_root() -> Path:
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("not a git checkout — manifest.py requires a git working tree")
    return Path(r.stdout.strip())


def manifest_file() -> Path:
    return repo_root() / DEFAULT_MANIFEST


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load() -> dict:
    p = manifest_file()
    if not p.exists():
        return {"schema": SCHEMA, "locked": False, "files": {}}
    d = json.loads(p.read_text(encoding="utf-8"))
    if d.get("schema") != SCHEMA:
        raise SystemExit(
            f"MANIFEST.json has schema {d.get('schema')}, this tool understands {SCHEMA}. "
            "Run `python3 tools/manifest.py version` to confirm which tool is installed.")
    d.setdefault("files", {})
    d.setdefault("locked", False)
    return d


def save(d: dict) -> None:
    d["updated"] = now()
    p = manifest_file()
    tmp = p.with_name(p.name + ".tmp")     # MANIFEST.json.tmp, not MANIFEST.tmp
    tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(p)


def sha_worktree(path: str) -> str | None:
    """The blob sha git would record for this file right now.

    -C root and --path= both matter: -C so a caller invoked from a subdirectory
    still resolves HEAD:path against the repo, and --path so clean/smudge filters
    fire exactly as `git add` fires them. Without --path, a repository with a
    .gitattributes reports `uncommitted` forever.
    """
    root = repo_root()
    full = root / path
    if not full.is_file():
        return None
    return subprocess.run(
        ["git", "-C", str(root), "hash-object", f"--path={path}", "--", str(full)],
        capture_output=True, text=True, check=True).stdout.strip()


def sha_committed(path: str) -> str | None:
    r = subprocess.run(
        ["git", "-C", str(repo_root()), "rev-parse", f"HEAD:{path}"],
        capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def check_one(d: dict, path: str) -> dict:
    entry = d["files"].get(path)
    manifest_sha = entry.get("sha") if entry else None
    worktree_sha = sha_worktree(path)
    committed_sha = sha_committed(path)
    base = {"path": path, "manifest_sha": manifest_sha,
            "committed_sha": committed_sha, "worktree_sha": worktree_sha}

    if d.get("locked"):
        return {**base, "status": "locked", "class": "locked", "scope": "repo"}

    if entry is None:
        if worktree_sha is None and committed_sha is None:
            return {**base, "status": "new", "class": None}
        # A file with no manifest entry — the bootstrap missed it, or a user made it.
        # Refuse: this is the exact window the manifest exists to close.
        return {**base, "status": "unrecorded", "class": None}

    cls = entry.get("class", "generated")
    if cls == "locked":
        return {**base, "status": "locked", "class": cls, "scope": "file"}
    if cls in {"hand", "agent"}:
        return {**base, "status": "protected", "class": cls}

    # In the manifest — the three-way comparison.
    if worktree_sha is None and committed_sha is None:
        return {**base, "status": "new", "class": cls}
    if worktree_sha is None and committed_sha is not None:
        return {**base, "status": "deleted", "class": cls}
    if worktree_sha is not None and committed_sha is None:
        return {**base, "status": "uncommitted", "class": cls}
    if worktree_sha != committed_sha:
        return {**base, "status": "uncommitted", "class": cls}
    if committed_sha != manifest_sha:
        return {**base, "status": "committed-drift", "class": cls}
    return {**base, "status": "clean", "class": cls}


def cmd_check(paths: list[str]) -> int:
    d = load()
    rc = RC_CLEAN
    for path in paths:
        r = check_one(d, path)
        print(json.dumps(r, ensure_ascii=False))
        if r["status"] in {"locked", "protected"}:
            rc = max(rc, RC_PROTECTED)
        elif r["status"] in {"unrecorded", "deleted", "uncommitted", "committed-drift"}:
            rc = max(rc, RC_DRIFT)
        # clean / new -> 0
    return rc


def cmd_record(paths: list[str], producer: str, cls: str | None, force: bool) -> int:
    d = load()
    if d.get("locked") and not force:
        print("refuse: repo is locked", file=sys.stderr)
        return RC_PROTECTED
    rc = RC_CLEAN
    for path in paths:
        disk = sha_worktree(path)
        if disk is None:
            print(f"skip {path}: no such file", file=sys.stderr)
            rc = RC_DRIFT
            continue
        prev = d["files"].get(path, {})
        prev_cls = prev.get("class")
        if prev_cls in REFUSE_CLASSES and not force:
            print(f"refuse {path}: class={prev_cls} (use --force to override)",
                  file=sys.stderr)
            rc = RC_PROTECTED
            continue
        d["files"][path] = {
            "sha": disk, "producer": producer,
            "class": cls or prev_cls or "generated", "at": now(),
        }
        print(f"recorded {path} ({d['files'][path]['class']})")
    save(d)
    return rc


def cmd_claim(path: str, cls: str) -> int:
    if cls not in {"hand", "agent", "canonical", "generated"}:
        raise SystemExit(f"unknown class {cls}")
    if not (repo_root() / path).exists():
        raise SystemExit(f"refuse: {path} does not exist — cannot claim a missing file")
    d = load()
    entry = d["files"].get(path, {})
    entry["class"] = cls
    entry["sha"] = sha_worktree(path)
    # A hand/agent claim means a human owns it now — do not leave a bot's producer
    # string on it, or a reader cannot tell who took it over.
    if cls in {"hand", "agent"}:
        entry["producer"] = "human"
    else:
        entry.setdefault("producer", "human")
    entry["at"] = now()
    d["files"][path] = entry
    save(d)
    print(f"claimed {path} as {cls}")
    return 0


def cmd_lock(reason: str) -> int:
    d = load()
    d["locked"] = True
    d["reason"] = reason
    save(d)
    print("repo locked")
    return 0


def cmd_unlock() -> int:
    d = load()
    d["locked"] = False
    d.pop("reason", None)
    save(d)
    print("repo unlocked")
    return 0


def cmd_status() -> int:
    d = load()
    counts: dict[str, int] = {}
    for e in d["files"].values():
        c = e.get("class", "?")
        counts[c] = counts.get(c, 0) + 1
    print(json.dumps({"schema": d.get("schema"), "locked": d.get("locked"),
                      "updated": d.get("updated"), "files": len(d["files"]),
                      "classes": counts}, indent=2))
    return 0


def cmd_version() -> int:
    print(f"manifest.py schema {SCHEMA}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="per-file provenance for a Forge-managed repository")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check", help="classify one or more paths")
    p.add_argument("paths", nargs="+")
    p = sub.add_parser("record", help="record the current blob sha after a write")
    p.add_argument("paths", nargs="+")
    p.add_argument("--producer", required=True)
    p.add_argument("--class", dest="cls", default=None,
                   choices=["generated", "canonical", "hand", "agent"])
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("claim", help="take ownership of a path as a human")
    p.add_argument("path")
    p.add_argument("--as", dest="cls", required=True,
                   choices=["hand", "agent", "canonical", "generated"])
    p = sub.add_parser("lock", help="lock the whole repository")
    p.add_argument("--reason", default="See LOCKED.md")
    sub.add_parser("unlock", help="unlock the repository")
    sub.add_parser("status", help="summarise the manifest")
    sub.add_parser("version", help="print the schema this tool understands")
    a = ap.parse_args(argv)
    if a.cmd == "check":   return cmd_check(a.paths)
    if a.cmd == "record":  return cmd_record(a.paths, a.producer, a.cls, a.force)
    if a.cmd == "claim":   return cmd_claim(a.path, a.cls)
    if a.cmd == "lock":    return cmd_lock(a.reason)
    if a.cmd == "unlock":  return cmd_unlock()
    if a.cmd == "status":  return cmd_status()
    if a.cmd == "version": return cmd_version()
    return 2


if __name__ == "__main__":
    sys.exit(main())
