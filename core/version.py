"""Build/version stamp for the dashboard header and startup log.

Tells you exactly which commit the running bot was built from - essential
once a bot is deployed and you are comparing behavior against GitHub.

Resolution order (first hit wins):

1. ``$MESHTECH_COMMIT`` env var        - containers/custom service units
2. ``<project root>/.git-commit``      - baked by the Docker build
3. ``<project root>/.git`` (by hand)   - native git-clone installs.  The
   git *files* are read directly (no ``git`` binary), so this works for
   the unprivileged ``meshtech`` service account, under read-only
   filesystem hardening, and without any ``safe.directory`` trust config.
4. ``unknown``

The stamp is captured once per process (callers poll it every few
seconds); a code change only lands with a restart anyway.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional

_ROOT = Path(__file__).resolve().parent.parent
_REF_PREFIX = "ref: "
_SHORT_LEN = 7

# Human-friendly release number.  Project convention: bump this on EVERY
# commit, so the number doubles as a commit counter (each commit is a
# release candidate, and the dashboard chip + startup log make it obvious
# which build a box is running).  The exact source of any running build is
# still pinned by the commit stamp.
__version__ = "0.0.018"


def _short(sha: str) -> str:
    sha = (sha or "").strip()
    return sha[:_SHORT_LEN] if sha else ""


def _resolve_git_dir(root: Path) -> Optional[Path]:
    """Root's .git entry - a directory normally, a gitdir: file in
    worktrees/submodules."""
    git = root / ".git"
    if git.is_dir():
        return git
    if git.is_file():
        try:
            text = git.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        if text.startswith("gitdir:"):
            cand = Path(text.split(":", 1)[1].strip())
            return cand if cand.is_absolute() else (root / cand).resolve()
    return None


def _read_ref(git_dir: Path, ref: str) -> str:
    """Resolve a loose ref (refs/heads/main), falling back to packed-refs."""
    loose = git_dir / ref
    try:
        if loose.is_file():
            sha = loose.read_text(encoding="utf-8", errors="replace").strip()
            if sha:
                return sha
    except OSError:
        pass
    packed = git_dir / "packed-refs"
    try:
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8",
                                         errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("^"):
                    continue
                sha, name = line.split(" ", 1)
                if name == ref:
                    return sha
    except (OSError, ValueError):
        pass
    return ""


def _stamp_from_git(root: Path) -> Dict[str, str]:
    git_dir = _resolve_git_dir(root)
    if git_dir is None:
        return {"source": "unknown"}
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8",
                                            errors="replace").strip()
    except OSError:
        return {"source": "unknown"}
    branch = ""
    commit = ""
    if head.startswith(_REF_PREFIX):
        ref = head[len(_REF_PREFIX):]
        branch = ref.removeprefix("refs/heads/") or ref
        commit = _read_ref(git_dir, ref)
    else:
        commit = head          # detached HEAD: HEAD holds the sha itself
    if not commit:
        return {"source": "unknown"}
    return {"commit": _short(commit), "branch": branch, "source": "git"}


def version_stamp(root: Optional[Path] = None) -> Dict[str, str]:
    """{version, commit, branch, source} for the running code.

    ``root`` is only used by tests; production callers hit the module
    cache, so the stamp reflects the process start, not every poll.
    """
    if root is not None:
        return _resolve(root)
    return _cached()


@lru_cache(maxsize=1)
def _cached() -> Dict[str, str]:
    return _resolve(_ROOT)


def _resolve(root: Path) -> Dict[str, str]:
    env = os.environ.get("MESHTECH_COMMIT", "").strip()
    if env:
        return {"version": __version__, "commit": _short(env),
                "branch": "", "source": "env"}
    baked = root / ".git-commit"
    try:
        if baked.is_file():
            sha = baked.read_text(encoding="utf-8", errors="replace").strip()
            if sha:
                return {"version": __version__, "commit": _short(sha),
                        "branch": "", "source": "file"}
    except OSError:
        pass
    stamp = _stamp_from_git(root)
    if stamp.get("source") != "unknown":
        stamp["version"] = __version__
    return stamp
