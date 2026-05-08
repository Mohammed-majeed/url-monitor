"""
status_spec.py
==============
Parses the `expected_statuses` field, e.g. "200-399;401-login".

Format:
    spec  := clause (";" clause)*
    clause := <range>            -> a-b means a <= status <= b
            | <code>             -> exact code
            | <code>"-login"     -> code is OK only if response looks like a login page

Examples:
    "200-399"           : 200..399
    "200-399;401-login" : 200..399, plus 401 if login-like
    "200,201,204"       : exactly those
    "200-399;401-login;500-599"  : 2xx-3xx OK, 401-login OK, 500s OK (rare)

`looks_like_login` is decided by simple URL/body heuristics in checker.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple


@dataclass
class StatusSpec:
    ranges: List[Tuple[int, int]] = field(default_factory=list)
    login_codes: List[int] = field(default_factory=list)

    @classmethod
    def parse(cls, raw: str) -> "StatusSpec":
        spec = cls()
        if not raw:
            spec.ranges.append((200, 399))
            return spec
        for clause in raw.split(";"):
            clause = clause.strip()
            if not clause:
                continue
            # login flavor:  "401-login"
            if clause.lower().endswith("-login"):
                code_part = clause[:-len("-login")].strip()
                try:
                    spec.login_codes.append(int(code_part))
                except ValueError:
                    pass
                continue
            # range:  "200-399"
            if "-" in clause:
                lo_s, hi_s = clause.split("-", 1)
                try:
                    spec.ranges.append((int(lo_s), int(hi_s)))
                except ValueError:
                    pass
                continue
            # exact code list:  "200,201,204"
            for tok in clause.split(","):
                tok = tok.strip()
                if not tok:
                    continue
                try:
                    code = int(tok)
                    spec.ranges.append((code, code))
                except ValueError:
                    pass
        if not spec.ranges and not spec.login_codes:
            spec.ranges.append((200, 399))
        return spec

    def is_ok(self, status: int, looks_like_login: bool) -> bool:
        for lo, hi in self.ranges:
            if lo <= status <= hi:
                return True
        if looks_like_login and status in self.login_codes:
            return True
        return False


LOGIN_HINTS = (
    "/login", "/oauth", "/sso", "keycloak", "/fmeserver",
    "signin", "sign-in", "auth/realms",
)


def looks_like_login(final_url: str, body_snippet: str = "") -> bool:
    u = (final_url or "").lower()
    if any(h in u for h in LOGIN_HINTS):
        return True
    b = (body_snippet or "").lower()
    if "<title" in b and "login" in b:
        return True
    return False
