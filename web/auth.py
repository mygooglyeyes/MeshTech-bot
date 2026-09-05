"""Simple bearer-token auth for the dashboard.

When config has web.password set, the user POSTs it once to /api/login and
receives a token kept in localStorage. When no password is configured the
dashboard is open (still bound to web.host, loopback by default).
"""
from __future__ import annotations

import secrets
import time
from typing import Dict

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
