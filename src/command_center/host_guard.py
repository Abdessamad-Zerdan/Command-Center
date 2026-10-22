"""Keeps other websites out of the app (DNS rebinding / cross-site requests).

There's no login — the app trusts that only its owner can reach
localhost:8000. A malicious page can break that with DNS rebinding: it
points its own hostname at 127.0.0.1, and the browser then sends that
page's requests here with `Host: evil.example.com`. Without a check, such
a page could read the brief (Gmail content included), register a project
with a run_command and launch it.

Two layers:
  1. TrustedHostMiddleware — any request whose Host isn't one of
     ALLOWED_HOSTS gets a 400. This alone stops rebinding.
  2. An Origin check on state-changing methods — a browser request that
     names a foreign Origin (another site posting a form or fetch() at
     localhost) gets a 403, even if it somehow carries an allowed Host.

ALLOWED_HOSTS is a comma-separated env var (or .env entry); default
"localhost,127.0.0.1". The server itself stays bound to 127.0.0.1.
"""

import os
from urllib.parse import urlparse

from fastapi import FastAPI
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

DEFAULT_ALLOWED_HOSTS = ("localhost", "127.0.0.1")
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def allowed_hosts() -> list[str]:
    raw = os.environ.get("ALLOWED_HOSTS", "")
    hosts = [h.strip().lower() for h in raw.split(",") if h.strip()]
    return hosts or list(DEFAULT_ALLOWED_HOSTS)


class OriginGuardMiddleware:
    """Rejects a state-changing request whose Origin header is present and
    names a host outside `allowed`. No Origin at all (curl, the app's own
    server-side calls, older same-origin form posts) is let through — the
    Host check above already covers those."""

    def __init__(self, app: ASGIApp, allowed: list[str]) -> None:
        self.app = app
        self.allowed = set(allowed)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] in UNSAFE_METHODS:
            origin = None
            for name, value in scope["headers"]:
                if name == b"origin":
                    origin = value.decode("latin-1")
                    break
            if origin is not None and (urlparse(origin).hostname or "").lower() not in self.allowed:
                # "null" (sandboxed iframes, file://) has no hostname, so it's refused too.
                response = JSONResponse({"detail": "Cross-site request refused"}, status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def install(app: FastAPI) -> None:
    hosts = allowed_hosts()
    # Added last = runs first: a foreign Host is refused before anything else.
    app.add_middleware(OriginGuardMiddleware, allowed=hosts)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
