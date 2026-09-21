"""One ASGI process: Django serves the site at `/`, the MCP server serves `/mcp`.

Run with:  uvicorn kapweb.asgi:application --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import contextlib
import os
import time
from collections import defaultdict, deque

import django
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "kapweb.settings")
django.setup()

from django.core.asgi import get_asgi_application  # noqa: E402

from kap_mcp import server as kap_server  # noqa: E402

django_app = get_asgi_application()

# Stateless + JSON responses: every request is self-contained, so the process can be
# restarted or scaled without session affinity. host="0.0.0.0" disables the SDK's
# localhost-only DNS-rebinding guard (Caddy fronts the process and sets Host).
mcp_app = kap_server.mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True, host="0.0.0.0")

RATE_LIMIT = int(os.environ.get("MCP_RATE_LIMIT_PER_MINUTE", "120"))
_hits: dict[str, deque] = defaultdict(deque)


class RateLimited:
    """Per-IP sliding-window limit in front of the MCP endpoint (in-process; Cloudflare adds its own).
    A class so Starlette treats it as a raw ASGI app rather than a request handler."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        await rate_limited(self.app, scope, receive, send)


async def rate_limited(app, scope, receive, send):
    token = None
    if scope["type"] == "http":
        headers = dict(scope.get("headers") or [])
        # Caller-supplied MKK credentials (plugin userConfig / custom headers) override the server key for this request.
        creds = kap_server.credentials_from_headers({k.decode(): v.decode(errors="ignore") for k, v in headers.items()})
        token = kap_server.request_credentials.set(creds)
        ip = (headers.get(b"cf-connecting-ip") or headers.get(b"x-forwarded-for") or b"").decode().split(",")[0].strip() or (scope.get("client") or ("?",))[0]
        now = time.monotonic()
        q = _hits[ip]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            resp = JSONResponse({"error": "rate limit exceeded", "limit_per_minute": RATE_LIMIT}, status_code=429, headers={"Retry-After": "60"})
            await resp(scope, receive, send)
            return
        q.append(now)
    try:
        await app(scope, receive, send)
    finally:
        if token is not None:
            kap_server.request_credentials.reset(token)


@contextlib.asynccontextmanager
async def lifespan(app):
    async with kap_server.mcp.session_manager.run():
        try:
            yield
        finally:
            await kap_server.shutdown()


application = Starlette(
    routes=[Route("/mcp", endpoint=RateLimited(mcp_app), methods=["GET", "POST", "DELETE"]), Mount("/", app=django_app)],
    lifespan=lifespan,
)
