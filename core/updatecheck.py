"""Read-only software-update checking for the dashboard.

Answers one question - "is there newer code on the repo than what this
bot is running?" - and nothing else.  Two lightweight network reads:

1. ``git ls-remote --heads <repo>``  -> the latest commit on each branch
   (DEV, main).  No clone, no credentials for a public repo, a few KB.
2. GitHub Releases API              -> the newest published release tag,
   used only to link to its release notes.

Results are cached (default 24 h, configurable); the popup's "Check now"
button forces a fresh read.  A failed check (offline, rate-limited) is
remembered briefly so an unreachable network never turns into a retry
storm.  Nothing here ever writes, restarts, or changes the bot - that is
stage 2, deliberately separate.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request
from typing import Any, Callable, Dict, Optional

from core.version import version_stamp

log = logging.getLogger("meshtech-bot.updatecheck")

LS_REMOTE_TIMEOUT = 20.0     # seconds before we give up on git ls-remote
API_TIMEOUT = 10.0           # seconds for the GitHub releases lookup
ERROR_CACHE_SECONDS = 300.0  # after a failed check, wait before re-trying
LOOP_SLEEP_SECONDS = 1800.0  # how often the background loop re-evaluates

_HEADS_PREFIX = "refs/heads/"


def parse_ls_remote(text: str) -> Dict[str, str]:
    """Parse ``git ls-remote --heads`` output into {branch: full-sha}.

    Lines look like ``<40-hex-sha>\\trefs/heads/DEV``.  Non-head lines
    (tags, PRs) are ignored.
    """
    branches: Dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or "\t" not in line:
            continue
        sha, ref = line.split("\t", 1)
        ref = ref.strip()
        if ref.startswith(_HEADS_PREFIX):
            branches[ref[len(_HEADS_PREFIX):]] = sha.strip()
    return branches


def is_newer_available(running_commit: str, remote_sha: Optional[str]) -> bool:
    """True when the remote head is not the commit we are running.

    ``running_commit`` is the short (7-char) stamp from version.py, so a
    prefix match means "same commit".  Anything else on the same branch
    counts as newer (we have no version numbers to compare, and on DEV
    the head always moves forward).
    """
    if not remote_sha or not running_commit:
        return False
    return not remote_sha.startswith(running_commit)


def repo_web_url(repo_url: str) -> str:
    """Turn a git remote URL into the human web URL.

    ``https://github.com/o/r.git`` -> ``https://github.com/o/r``
    ``git@github.com:o/r.git``     -> ``https://github.com/o/r``
    """
    url = (repo_url or "").strip()
    if url.endswith(".git"):
        url = url[: -len(".git")]
    if url.startswith("git@"):
        # git@github.com:owner/repo
        host, _, path = url[4:].partition(":")
        url = f"https://{host}/{path.lstrip('/')}"
    return url


def _github_api_url(repo_url: str) -> Optional[str]:
    web = repo_web_url(repo_url)
    if not web.startswith("https://github.com/"):
        return None  # custom git hosting: skip the releases lookup
    return web.replace("https://github.com/", "https://api.github.com/repos/",
                       1) + "/releases/latest"


class UpdateChecker:
    """Cached, asynchronous update checks.  One instance per service."""

    def __init__(self, settings_provider: Callable[[ ], Any]):
        self._settings_provider = settings_provider
        self._last_result: Dict[str, Any] = {}
        self._last_attempt: float = 0.0
        self._task: Optional["asyncio.Task"] = None

    # ------------------------------------------------------------ settings

    def _cfg(self) -> Any:
        return getattr(self._settings_provider(), "updates", None)

    def enabled(self) -> bool:
        cfg = self._cfg()
        return bool(cfg and getattr(cfg, "check_enabled", True))

    def ttl_seconds(self) -> float:
        cfg = self._cfg()
        try:
            hours = float(getattr(cfg, "check_hours", 24.0) or 24.0)
        except (TypeError, ValueError):
            hours = 24.0
        return max(0.25, hours) * 3600.0

    def repo_url(self) -> str:
        cfg = self._cfg()
        url = (getattr(cfg, "repo_url", "") or "").strip()
        return url

    # ------------------------------------------------------------ lifecycle

    def start(self) -> "asyncio.Task":
        """Begin the background loop; safe to call once at startup."""
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._loop())
        return self._task

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def _loop(self) -> None:
        try:
            await asyncio.sleep(20)      # let startup and the link settle
            while True:
                if self.enabled():
                    try:
                        await self.check(force=False)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:   # never die from a bad day
                        log.debug("update check failed: %s", exc)
                await asyncio.sleep(LOOP_SLEEP_SECONDS)
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------ the check

    async def check(self, force: bool = False) -> Dict[str, Any]:
        """Run (or reuse) a check.  ``force=True`` bypasses the cache."""
        now = time.time()
        if not force and self._last_result:
            fresh = now - self._last_attempt < self.ttl_seconds()
            recent_error = (bool(self._last_result.get("error"))
                            and now - self._last_attempt < ERROR_CACHE_SECONDS)
            if fresh or recent_error:
                return self.snapshot()
        self._last_attempt = now
        self._last_result = await self._run_check()
        return self.snapshot()

    async def _run_check(self) -> Dict[str, Any]:
        stamp = version_stamp()
        running_branch = stamp.get("branch") or ""
        running_commit = stamp.get("commit") or ""
        result: Dict[str, Any] = {
            "checked": True, "ok": False, "error": "",
            "checked_at": int(time.time()),
            "running": {"version": stamp.get("version") or "",
                        "commit": running_commit, "branch": running_branch},
            "branches": {}, "update_available": False, "newer_branch": "",
            "release": None, "release_url": "", "commits_url": "",
        }
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "ls-remote", "--heads", self.repo_url(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
        except FileNotFoundError:
            result["error"] = "git is not installed on this machine"
            return result
        except Exception as exc:
            result["error"] = f"could not start the check: {exc}"
            return result
        try:
            out, err = await asyncio.wait_for(proc.communicate(),
                                              timeout=LS_REMOTE_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            result["error"] = "timed out contacting the repository"
            return result
        except asyncio.CancelledError:
            proc.kill()
            raise
        if proc.returncode != 0:
            detail = (err or b"").decode(errors="replace").strip()[:200]
            result["error"] = detail or "could not read the repository"
            return result

        result["branches"] = parse_ls_remote(out.decode(errors="replace"))
        result["ok"] = True

        # Compare against the running branch; fall back to DEV when the
        # build stamp could not tell us the branch (containers etc.).
        branch = running_branch or "DEV"
        remote_sha = result["branches"].get(branch)
        if is_newer_available(running_commit, remote_sha):
            result["update_available"] = True
            result["newer_branch"] = branch

        # Optional: newest published release, for the notes link.  Failure
        # here (offline, rate limit, non-GitHub hosting) is not fatal.
        rel = await self._latest_release()
        if rel:
            result["release"] = rel
            result["release_url"] = rel.get("html_url", "")
        web = repo_web_url(self.repo_url())
        result["commits_url"] = f"{web}/commits/{branch}" if branch else web
        return result

    async def _latest_release(self) -> Optional[Dict[str, Any]]:
        api = _github_api_url(self.repo_url())
        if not api:
            return None

        def _get() -> Dict[str, Any]:
            req = urllib.request.Request(
                api, headers={"Accept": "application/vnd.github+json",
                              "User-Agent": "meshtech-bot"})
            with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))

        try:
            data = await asyncio.to_thread(_get)
        except Exception as exc:
            log.debug("release lookup failed: %s", exc)
            return None
        if not isinstance(data, dict) or not data.get("tag_name"):
            return None
        return {"tag": str(data["tag_name"]),
                "name": str(data.get("name") or ""),
                "html_url": str(data.get("html_url") or "")}

    # ------------------------------------------------------------ snapshot

    def snapshot(self) -> Dict[str, Any]:
        """Last known state for the dashboard - no network access."""
        if not self._last_result:
            return {"checked": False, "enabled": self.enabled()}
        out = dict(self._last_result)
        out["enabled"] = self.enabled()
        out["age_seconds"] = int(time.time() - self._last_attempt)
        out["ttl_seconds"] = int(self.ttl_seconds())
        return out
