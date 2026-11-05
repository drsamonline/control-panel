#!/usr/bin/env python3
"""_gate.py — the manifest gate, for forge's own tools.

The four generators (drsoam.py, refresh.py, scan.py, plan.py) each carry their own
copy of these helpers, because a generator runs in a repository where
tools/manifest.py may not be present yet and an import that raises is exactly the
failure the copy avoids. forge's own tools do not have that problem: they always run
beside tools/manifest.py, so they import this instead of carrying a tenth copy of
the same seventy lines.

Same semantics as the copies, deliberately:

    _manifest_check(paths)  True only when every path is clean or new. False on
                            drift, on a protected class, on a locked repo, and on
                            ANY hard error - a checker that cannot run must not
                            authorise a write.
    _manifest_record(...)   Silent on success. A warning on failure, because the
                            next run's check sees the drift anyway and that is the
                            real gate.
    _producer(tool)         tool@<blob sha of the file doing the writing>, so the
                            manifest says which version wrote the file.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

RC_CLEAN, RC_DRIFT, RC_PROTECTED = 0, 3, 4


def _tool() -> Path:
    return Path(__file__).resolve().parent / "manifest.py"


def _rel(paths) -> list:
    """Paths as manifest.py expects them: relative to the repository root.

    An ABSOLUTE path was passed once and manifest.py recorded nothing for it - the
    entry never appeared, the next check called the file unrecorded, and the write was
    refused for a reason that had nothing to do with the file. Normalise here, once.
    """
    try:
        root = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"],
                                   capture_output=True, text=True,
                                   check=True).stdout.strip()).resolve()
    except Exception:                                        # noqa: BLE001
        return [str(p) for p in paths]
    out = []
    for p in paths:
        try:
            out.append(str(Path(p).resolve().relative_to(root)))
        except Exception:                                    # noqa: BLE001
            out.append(str(p))
    return out


def _manifest_check(paths, quiet: bool = False) -> bool:
    """True = every path may be written. False = at least one may not."""
    tool = _tool()
    paths = _rel(paths)
    try:
        r = subprocess.run([sys.executable, str(tool), "check", *paths],
                           capture_output=True, text=True, timeout=60)
    except Exception as e:                                   # noqa: BLE001
        print(f"::warning::manifest check crashed ({e}) - refusing write", file=sys.stderr)
        return False
    if r.returncode not in (RC_CLEAN, RC_DRIFT, RC_PROTECTED):
        print(f"::warning::manifest check exited {r.returncode} - refusing write",
              file=sys.stderr)
        return False
    if r.returncode == RC_CLEAN:
        return True
    if quiet:
        return False
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
            print(f"::warning::skip {e.get('path')}: {e.get('status')} "
                  f"(class={e.get('class')}, "
                  f"manifest={str(e.get('manifest_sha'))[:12]}, "
                  f"committed={str(e.get('committed_sha'))[:12]}, "
                  f"worktree={str(e.get('worktree_sha'))[:12]})", file=sys.stderr)
    return False


def _manifest_record(paths, producer: str, cls: str = "generated") -> None:
    """Record what was just written. Never raises: the next check is the real gate."""
    try:
        subprocess.run([sys.executable, str(_tool()), "record",
                        *_rel(paths),
                        "--producer", producer, "--class", cls],
                       capture_output=True, text=True, timeout=60)
    except Exception as e:                                   # noqa: BLE001
        print(f"::warning::manifest record failed ({e}) - next run will see drift",
              file=sys.stderr)


def _producer(tool: str, file: str = "") -> str:
    """`tool@<7-char blob sha of the calling file>`.

    The caller passes its own __file__: sys.argv[0] is right when the tool is run as
    a script and wrong the moment it is imported, and a producer that silently says
    "unknown" is a manifest that cannot say what wrote the file.
    """
    try:
        sha = subprocess.run(["git", "hash-object", "--", file or sys.argv[0]],
                             capture_output=True, text=True, check=True).stdout.strip()[:7]
    except Exception:                                        # noqa: BLE001
        sha = "unknown"
    return f"{tool}@{sha}"
