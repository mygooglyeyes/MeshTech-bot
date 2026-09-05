"""Simple bearer-token auth for the dashboard.

When config has web.password set, the user POSTs it once to /api/login and
receives a token kept in localStorage. When no password is configured the
dashboard is open (still bound to web.host, loopback by default).
"""
from __future__ import annotations

import secrets
import time
from collections import OrderedDict, deque
from typing import Deque, Dict, Optional

TOKEN_TTL_SECONDS = 12 * 3600


class Auth:
    def __init__(self, password: str = ""):
        self.password = password
        self._tokens: Dict[str, float] = {}

    def required(self) -> bool:
        return bool(self.password)

    def check(self, token: str) -> bool:
        if not self.required():
            return True
        if not token:
            return False
        expiry = self._tokens.get(token)
        if expiry is None:
            return False
        if time.time() > expiry:
            self._tokens.pop(token, None)
            return False
        self._tokens[token] = time.time() + TOKEN_TTL_SECONDS
        return True

    def issue(self, password: str) -> str:
        if not self.required():
            return ""
        if not secrets.compare_digest(password, self.password):
            return ""
        token = secrets.token_urlsafe(32)
        self._tokens[token] = time.time() + TOKEN_TTL_SECONDS
        return token

    def bearer_token(self, authorization: str) -> str:
        if authorization and authorization.lower().startswith("bearer "):
            return authorization[len("bearer "):].strip()
        return ""


class LoginThrottle:
    """In-memory per-IP lockout for failed dashboard logins.

    After ``max_failures`` failed attempts from one client IP within
    ``window`` seconds, further attempts from that IP are refused for
    ``cooldown`` seconds. A successful login resets the counter. This stops
    LAN peers from guessing a weak dashboard password indefinitely.

    Memory is bounded: at most ``max_ips`` client IPs are tracked, and
    stale entries are pruned as they age out.
    """

    def __init__(self, max_failures: int = 5, window: float = 900.0,
                 cooldown: float = 300.0, max_ips: int = 1024):
        self.max_failures = max_failures
        self.window = window
        self.cooldown = cooldown
        self._fails: "OrderedDict[str, Deque[float]]" = OrderedDict()
        self._max_ips = max_ips

    def _touch(self, ip: str, now: float) -> Optional[Deque[float]]:
        """Fetch the failure log for ``ip``, pruning entries older than the
        window. Returns None (and forgets the IP) when nothing remains."""
        failures = self._fails.get(ip)
        if failures is None:
            return None
        while failures and now - failures[0] > self.window:
            failures.popleft()
        if not failures:
            del self._fails[ip]
            return None
        self._fails.move_to_end(ip)
        return failures

    def locked_out(self, ip: str, now: Optional[float] = None) -> bool:
        if not ip:
            return False
        now = time.time() if now is None else now
        failures = self._touch(ip, now)
        if not failures:
            return False
        return (len(failures) >= self.max_failures
                and now - failures[-1] < self.cooldown)

    def retry_after(self, ip: str, now: Optional[float] = None) -> float:
        if not ip:
            return 0.0
        now = time.time() if now is None else now
        failures = self._touch(ip, now)
        if not failures:
            return 0.0
        return max(0.0, self.cooldown - (now - failures[-1]))

    def record_failure(self, ip: str, now: Optional[float] = None) -> None:
        if not ip:
            return
        now = time.time() if now is None else now
        failures = self._fails.get(ip)
        if failures is None:
            if len(self._fails) >= self._max_ips:
                self._fails.popitem(last=False)  # drop the least-recent IP
            failures = deque()
            self._fails[ip] = failures
        else:
            self._fails.move_to_end(ip)
        failures.append(now)
        self._touch(ip, now)

    def record_success(self, ip: str) -> None:
        if ip:
            self._fails.pop(ip, None)
