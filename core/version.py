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
# 0.0.149: DEV's small-repairs merge also took 0.0.148 (deployed on
# the box) - next free number wins. 0.0.150: bench runbook (docs).
# 0.0.151: example-config comment fix (the deploy's config-sync inserts
# "# key: value" lines as ACTIVE settings - prose got into a live file).
# 0.0.152: cleanmodem LBT retry jitter goes continuous (uniform 0.10-0.30 s,
# the old stack's empirically robust pattern) instead of three fixed delays.
# 0.0.153: cleanmodem lbt_max_attempts documented as reserved (never
# implemented; retry loop is bounded by clear_channel_wait_seconds).
# 0.0.158: cleanmodem gpiod backend rewritten for the v2 Python API
# (gpiod.Chip + request_lines) - the v1 pip bindings are ABI-broken on
# Debian 13; 2.x ships cp313 aarch64 wheels.
# 0.0.159: gpiod 2.x enums live in the gpiod.line submodule, not
# top-level (caught by the clean-venv API probe on hilltop).
# 0.0.160: a failed modem init stops its client (two clients fighting
# over the single controller slot); worker skips IRQ polls until
# bring-up finishes.
# 0.0.161: modem-mode TX no longer dropped - the TX guard required
# self.radio, which is None by design in modem mode.
# 0.0.162: CAD issued from continuous RX is ignored by the SX126x
# (SetCAD is STANDBY-only) - the driver now dances RX->STDBY->CAD->RX.
# 0.0.163: _read_cmd misaligned by one byte - the chip status byte
# was read as data, so IRQ flags never included the low byte
# (RX_DONE/CAD_DONE/TX_DONE invisible; rx=0 from day one).
# 0.0.164: SetCadParams sent 4 of its 7 required bytes - truncated
# commands are rejected, so CAD never ran even with clean reads.
# 0.0.165: response data starts at MISO byte 3 on hilltop (raw
# capture aa aa 00 00 03 -> flags 00 03 = real RF at bytes 3-4).
# 0.0.174: switchover runbook updated to the operational truth -
# steps marked DONE, completion section with the reboot-proven
# architecture, all 12 fixes, healthy signatures, and gotchas.
# 0.0.175: restore the runbook the 0.0.174 rewrite accidentally
# truncated (104 lines) - full hunt narrative back, plus the
# completion section. Docs only.
# 0.0.176: post-mortem doc - opcode-table root cause, the full
# v0.0.154-0.0.173 fix chain, and the probe-driven debugging method.
# 0.0.177: the one-size-modem dependency split (gpiod/rpi-lgpio extras)
# recorded as DEFERRED in DEV-NOTES - revisit trigger documented.
# 0.0.173: modem pushes OBSERVER_STATE (observer count, one byte) to
# the controller on connect + observer join/leave - the dashboard's
# TCP Push chip shows the truth (green when openHop is connected).
# 0.0.172: the bot pushes its config.yaml radio settings (SET_CONFIG,
# controller-only) to the modem at startup - mcp.tx_power_dbm is the
# single source of truth; the modem's modem.conf value is a boot
# default. Modem logs the asked-vs-kept config on mismatch.
# 0.0.171: authenticated observers exempt from the idle read timeout
# - openhop_core's driver sends nothing after its handshake, so a
# live repeater was idle-recycled every ~60 s. Dead observers are
# still reaped by TCP keepalive + the slow-client transport guards.
# 0.0.170: observer SET_CAD_PARAMS echoed too - the repeater driver
# restores cached CAD settings after SET_CONFIG in its handshake.
# 0.0.169: observer SET_CONFIG answered with the live config (echo),
# never applied - openhop_core's TCPLoRaRadio handshake requires a
# config echo or it reconnect-loops treating the link as dead.
# 0.0.168: controller keepalive - the client PINGs every 15 s so the
# server's ~30 s idle recycler stops dropping an idle controller
# every ~32 s (TX landing in the 2 s reconnect gap failed).
# 0.0.167: opcode table was shifted by one (cross-checked against the
# LoRaRF driver openhop_core vendors for this exact E22 module) -
# TcxoCtrl is 0x97 not 0xD4 (no 32 MHz clock: every clocked command
# EXEC_FAILed), TxParams 0x8E, BufBase 0x8F, sync word is a register
# write to 0x0740 (no such command), CalibrateImage pairs (0xE1,0xE9).
# 0.0.178: feature/mesh-health resurrected into DEV (the Sept-8 branch:
# mesh health registry + card, DM help rework, packet export) - every
# branch is now merged; dm_chunk_gap_seconds restored to the config
# parser alongside reply_delay_seconds.
__version__ = "0.0.178"


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
            # Format: "<sha>" (legacy deploys) or "<sha> <branch>" (the
            # deploy script bakes both so the bot knows its branch).
            parts = baked.read_text(encoding="utf-8",
                                    errors="replace").split()
            if parts:
                return {"version": __version__,
                        "commit": _short(parts[0]),
                        "branch": parts[1] if len(parts) > 1 else "",
                        "source": "file"}
    except OSError:
        pass
    stamp = _stamp_from_git(root)
    if stamp.get("source") != "unknown":
        stamp["version"] = __version__
    return stamp
