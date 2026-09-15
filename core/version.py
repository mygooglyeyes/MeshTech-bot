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
# 0.0.179: config pipeline hardened (Brett's rule: example + live stay
# current) - config.example.yaml now documents modem mode ACTIVE (mcp
# block with radio_mode/controller link, matching hilltop's proven
# layout), adds limits.dm_chunk_gap_seconds, channels[].secret_hex and
# modem_feed.queue_size; cleanmodem.conf.example matches the real
# /etc/cleanmodem token layout + irq_poll=true; the deploy sync now
# treats a COMMENTED key as MISSING (old rule made example updates
# invisible to boxes forever - the 2026-09-14 staleness bug) and
# re-installs documented defaults ACTIVE (secrets still excluded);
# manage.sh clean-config runs the sync after copying the example, so
# every config-touching menu option starts from the complete schema.
# 0.0.180: the dashboard's noise-floor card lives again in modem mode
# (the cleanmodem switchover had silently orphaned it: mcp.radio is
# None by design there, so the monitor sampled nothing). The monitor
# now asks the chip's owner over the controller link: ModemClient.noise()
# round-trips the protocol's NOISE_REQ -> NOISE_RESP (noise*10 i16),
# parsed by the new frames.parse_noise_payload. SPI mode unchanged.
# 0.0.181: the essentials walkthrough (menu option 1) now asks the
# command prefix AND the bot's radio name (bot.display_name) - it never
# asked either (git: the prompt list is unchanged from v0.0.131), so a
# prefix set via the full editor looked "lost" when the walkthrough was
# used. ask_command_prefix enforces the loader's rules (one visible
# symbol; ':' '#' '@' refused with reasons, parity test-pinned) and the
# writes activate the keys and remove the stale commented example line;
# the full editor's writer cleans it too. The name is capped at the
# advert payload's 32-character budget.
# 0.0.182: the noise-floor fix that v0.0.180 promised, actually delivered.
# The sampling path (ModemClient.noise) was right, but _start_mcp's
# modem-mode early return skipped the monitor-creation block entirely -
# service.noise_monitor stayed None, so /api/noisefloor reported
# unavailable, the card hid itself, and nothing persisted for the hourly
# analysis panel. On every cleanmodem box. Now BOTH MCP radio modes
# create the monitor (SPI reads the driver; modem asks over the link);
# two wiring tests pin it per mode.
# 0.0.183: the frozen -105 noise line - honesty pass + the diagnostic
# fork. meshtech-modem answered NOISE_REQ with a HARD-CODED -1050
# (never read the chip); cleanmodem's exception path echoed the same
# -105.0 silently. Now a failed read answers the NO-VALUE sentinel
# (-32768) -> None at the bot -> a graph GAP with a logged warning.
# _hw_status's noise field read self.noise, which nothing ever
# assigned (dormant crash on the first STATUS request) - it reads the
# chip live now. probe_modem.py updated to the v0.0.155 12-field
# STATUS format; new scripts/probe_rssi_raw.py dumps raw GetRssiInst
# vs GetPacketStatus MISO windows to settle constant-echo vs truly-
# quiet-channel (stop cleanmodem first: two SPI masters must not fight).
# 0.0.184: deploys now restart cleanmodem when its code changes - the
# restart logic only ever touched the bot service, so modem-side fixes
# sat inert on the box until a manual restart (hilltop ran 3.5 h of
# deploys while cleanmodem stayed up). deploy.sh compares the runtime
# cleanmodem tree before/after the extract and restarts the modem FIRST
# when it changed (bot lands on a live modem). The probe verdict that
# motivated it: the constant 0xD2 the running modem read as its noise
# floor was the chip STATUS byte - the ORIGINAL v0.0.163 misalignment -
# while the CURRENT driver's slice returns a live, varying -96.0
# (proven by scripts/probe_rssi_raw.py against the same chip in RX).
# 0.0.185: the stale-anything audit (the cleanmodem-restart lesson,
# generalized). Full sweep of every startup branch and second process:
# the deploy path now covers both services (v0.0.184), _start_mcp's
# three branches all wire their components (noise monitor pinned by
# v0.0.182's tests), and the web-update path reuses deploy.sh so the
# modem restart covers it too. The gap that remained: the bot's
# background-task list was WRITE-ONLY - a crashed web server, noise
# monitor or radio task left the bot looking alive while the feature
# quietly went stale. bot.py now attaches a done-callback to every
# background task: a dead task logs a loud ERROR naming the task and
# its traceback (and retrieves the exception, so asyncio's
# 'never retrieved' warning can never be lost); cancellations and
# shutdown-time deaths stay silent. The dormant modem-link task is now
# on the list too.
# 0.0.186: THE noise bug, actually found and fixed. RadioHal.__init__
# assigned self.noise = -105.0 - shadowing the async noise() METHOD the
# server calls for every NOISE_REQ. self.hal.noise() raised "'float'
# object is not callable" and every read died into the NO-VALUE sentinel
# (card dark / dash instead of a number). The shadow existed since the
# first cleanmodem commit; the pre-v0.0.183 silent -105.0 fallback made
# it display a plausible constant - so the frozen -105 line was THIS,
# not the status-byte theory (Brett's probe: cleanmodem journal showed
# the TypeError every 5 s, while STATUS noise=-97.0 - a path that calls
# _hw_noise() directly - proved the chip read is healthy). Fix: the
# attribute is gone; value travels only in RadioStatus.noise_x10. 3 new
# tests pin the class shape + the sentinel/real-value round-trips.
__version__ = "0.0.186"


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
