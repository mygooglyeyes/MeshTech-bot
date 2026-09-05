"""Tests for web/auth.py - dashboard tokens + login throttling.

Pure stdlib - no FastAPI/network needed.
"""
from __future__ import annotations

import time

from web.auth import Auth, LoginThrottle


# ------------------------------------------------------------------ tokens

def test_auth_required_follows_password():
    assert Auth("").required() is False
    assert Auth("secret").required() is True


def test_token_issue_and_check_roundtrip():
    auth = Auth("hunter2")
    assert auth.issue("wrong") == ""          # wrong password -> no token
    token = auth.issue("hunter2")
    assert token                              # 256-bit urlsafe token
    assert auth.check(token)
    assert not auth.check("nonsense")
    assert not auth.check("")


def test_passwordless_dashboard_accepts_any_token():
    auth = Auth("")
    assert auth.check("anything") is True     # no password -> open (by design)
    assert auth.issue("whatever") == ""


def test_expired_token_rejected_and_forgotten(monkeypatch):
    auth = Auth("pw")
    token = auth.issue("pw")
    real_now = time.time()
    monkeypatch.setattr("web.auth.time.time", lambda: real_now)
    assert auth.check(token)                  # valid now (sliding window refreshes)
    # jump past the 12h TTL
    monkeypatch.setattr("web.auth.time.time", lambda: real_now + 13 * 3600)
    assert not auth.check(token)


# ------------------------------------------------------------------ throttle

def test_throttle_locks_after_max_failures():
    throttle = LoginThrottle(max_failures=3, window=900.0, cooldown=300.0)
    ip = "10.0.0.9"
    now = 1_000_000.0
    assert not throttle.locked_out(ip, now)
    for _ in range(2):
        throttle.record_failure(ip, now)
        now += 1.0
        assert not throttle.locked_out(ip, now)   # under the limit
    throttle.record_failure(ip, now)
    now += 1.0
    assert throttle.locked_out(ip, now)
    assert throttle.retry_after(ip, now) > 0.0


def test_throttle_clears_after_cooldown():
    throttle = LoginThrottle(max_failures=3, window=900.0, cooldown=300.0)
    ip = "10.0.0.9"
    start = 1_000_000.0
    for _ in range(3):
        throttle.record_failure(ip, start)
    assert throttle.locked_out(ip, start)
    # after the cooldown elapses the IP may try again
    assert not throttle.locked_out(ip, start + 301.0)
    assert throttle.retry_after(ip, start + 301.0) == 0.0


def test_throttle_success_resets():
    throttle = LoginThrottle(max_failures=3, window=900.0, cooldown=300.0)
    ip = "10.0.0.9"
    now = 1_000_000.0
    for _ in range(3):
        throttle.record_failure(ip, now)
    assert throttle.locked_out(ip, now)
    throttle.record_success(ip)
    assert not throttle.locked_out(ip, now)
    assert throttle.retry_after(ip, now) == 0.0


def test_throttle_is_per_ip():
    throttle = LoginThrottle(max_failures=3, window=900.0, cooldown=300.0)
    now = 1_000_000.0
    for _ in range(3):
        throttle.record_failure(ip := "10.0.0.9", now)
    assert throttle.locked_out("10.0.0.9", now)
    assert not throttle.locked_out("10.0.0.10", now)  # other client unaffected
    assert not throttle.locked_out("", now)           # unknown peer never locks
