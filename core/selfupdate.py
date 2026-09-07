"""Web-console driven software updates (dashboard stage 2).

The bot's part is deliberately small and heavily guarded.  It may only:

  1. validate a requested branch (fixed allowlist + strict shape),
  2. write the branch into ``data/update-request``,
  3. run the ONE sudo-whitelisted script (exact path, no arguments) that
     reads that file and does the real work detached from the bot,
  4. tail ``data/update-log`` so the dashboard can show the update text.

All privileged work happens in ``scripts/update-trigger.sh`` and
``scripts/update-runner.sh`` behind a sudoers rule that whitelists the
trigger by exact path with no arguments.  The bot itself never runs git,
never touches /opt, and never gains anything beyond what a logged-in
dashboard operator could ask for.
"""
from __future__ import annotations

import asyncio
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

log = __import__("logging").getLogger("meshtech-bot.selfupdate")

# Branches the dashboard may switch to.  The running branch is always
# allowed too (so "update my current branch to its latest" works even on
# a feature branch).
ALLOWED_BRANCHES = ("DEV", "main")

_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
RUNNER_TIMEOUT = 20.0          # seconds before we give up waiting for sudo
MIN_BETWEEN_UPDATES = 30.0     # cooldown between update attempts
LOG_TAIL_CHARS = 8000          # how much of update-log the dashboard gets
UNIT_NAME = "meshtech-bot-update"

OK_MARKER = "=== update finished OK"
FAIL_MARKER = "=== UPDATE FAILED"


def branch_allowed(branch: str, running_branch: str = "") -> bool:
    """True when the dashboard may switch to this branch."""
    return branch in ALLOWED_BRANCHES or (branch != "" and branch == running_branch)


class SelfUpdater:
    """Validate, launch, and watch the detached update job."""

    def __init__(self, data_dir: str | Path, trigger_path: str | Path):
        self.data_dir = Path(data_dir)
        self.trigger_path = Path(trigger_path)
        self.request_file = self.data_dir / "update-request"
        self.log_file = self.data_dir / "update-log"
        self._last_start = 0.0
        self._lock = asyncio.Lock()

    # ---- launching ------------------------------------------------------

    async def request_update(self, branch: str, running_branch: str = "") -> dict:
        """Validate and start an update.  Returns a JSON-shaped dict."""
        branch = str(branch or "").strip()
        if not _BRANCH_RE.match(branch):
            return {"error": f"'{branch}' is not a valid branch name"}
        if not branch_allowed(branch, running_branch):
            return {"error": "only DEV and main may be selected "
                             "(plus the branch you are running)"}
        if not self.trigger_path.is_file():
            return {"error": "the update script is missing on this machine - "
                             "reinstall or update the bot"}
        async with self._lock:
            if self._in_flight():
                return {"error": "an update is already running - wait for it to finish"}
            if time.monotonic() - self._last_start < MIN_BETWEEN_UPDATES:
                return {"error": "please wait a moment between update attempts"}
            if not self._runner_available():
                return {"error": "web updates are not enabled on this machine - "
                                 "run 'sudo ./manage.sh' and choose the web-updates "
                                 "option (or re-run install.sh)"}
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                self.request_file.write_text(branch + "\n", encoding="utf-8")
            except OSError as exc:
                return {"error": f"could not write the update request: {exc}"}
            log.info("Web update requested: branch %s", branch)
            try:
                proc = await asyncio.create_subprocess_exec(
                    "sudo", str(self.trigger_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await asyncio.wait_for(proc.communicate(), RUNNER_TIMEOUT)
            except (OSError, asyncio.TimeoutError) as exc:
                return {"error": f"could not start the updater: {exc}"}
            self._last_start = time.monotonic()
            if proc.returncode != 0:
                detail = (stderr or b"").decode(errors="replace").strip()
                return {"error": detail.splitlines()[-1] if detail
                        else "the updater refused to start"}
            return {"started": True, "branch": branch}

    # ---- watching -------------------------------------------------------

    def _runner_available(self) -> bool:
        """True when the machine is a Linux install with sudo set up.

        Cheap probe: the trigger exists and the platform is POSIX.  The
        sudoers rule itself is checked by sudo at run time (a missing rule
        surfaces as a non-zero exit with a clear message).
        """
        import os
        return os.name == "posix"

    def _in_flight(self) -> bool:
        """True while the detached update unit exists (running or uncollected)."""
        if not _systemctl_available():
            return False
        try:
            proc = subprocess.run(
                ["systemctl", "is-active", f"{UNIT_NAME}.service"],
                capture_output=True, text=True, timeout=5,
            )
            if proc.returncode == 0:
                return True
            # 'activating'/'failed' states return non-zero but the unit exists
            proc2 = subprocess.run(
                ["systemctl", "status", f"{UNIT_NAME}.service"],
                capture_output=True, text=True, timeout=5,
            )
            return proc2.returncode != 4   # 4 = no such unit
        except (OSError, subprocess.TimeoutExpired):
            return False

    def status(self) -> dict:
        """Everything the dashboard's log view needs, no network calls."""
        running = self._in_flight()
        text = self.log_tail()
        finished_ok = OK_MARKER in text
        failed = FAIL_MARKER in text
        return {
            "running": running,
            "finished": finished_ok or (failed and not running),
            "success": finished_ok if (finished_ok or failed) else None,
            "log": text,
        }

    def log_tail(self) -> str:
        try:
            return self.log_file.read_text(encoding="utf-8", errors="replace")[-LOG_TAIL_CHARS:]
        except OSError:
            return ""


def _systemctl_available() -> bool:
    import os
    import shutil
    return os.name == "posix" and shutil.which("systemctl") is not None
