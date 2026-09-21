"""One ASGI process: Django serves the site at `/`, the MCP server serves `/mcp`.

Run with:  uvicorn kapweb.asgi:application --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import contextlib
import os
import time
from collections import defaultdict, deque

import django
from pathlib import Path

from mcp.server.auth.routes import create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Mount, Route

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "kapweb.settings")
django.setup()

from django.core.asgi import get_asgi_application  # noqa: E402

from django.conf import settings as dj_settings  # noqa: E402
from django.template.loader import render_to_string  # noqa: E402

from kap_mcp import oauth as kap_oauth  # noqa: E402
from kap_mcp import server as kap_server  # noqa: E402
from kap_mcp.client import KAPClient  # noqa: E402

django_app = get_asgi_application()

# Stateless + JSON responses: every request is self-contained, so the process can be
# restarted or scaled without session affinity. host="0.0.0.0" disables the SDK's
# localhost-only DNS-rebinding guard (Caddy fronts the process and sets Host).
mcp_app = kap_server.mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True, host="0.0.0.0")

SITE_URL = dj_settings.SITE_URL
AUTH_MCP_PATH = "/mcp/auth"
oauth = kap_oauth.build_provider(
    SITE_URL,
    Path(os.environ.get("KAP_OAUTH_STORE", Path(dj_settings.BASE_DIR).parent / ".oauth-store.enc")),
    os.environ.get("KAP_OAUTH_SECRET", ""),
)

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
        hdr = {k.decode(): v.decode(errors="ignore") for k, v in headers.items()}
        # Caller-supplied MKK credentials (plugin userConfig / custom headers) override the server key for this request.
        creds = kap_server.credentials_from_headers(hdr)
        if scope["path"].rstrip("/") == AUTH_MCP_PATH:
            # OAuth-protected variant: the bearer token must map to stored user credentials.
            auth = hdr.get("authorization", "")
            creds = oauth.credentials_for_token(auth[7:].strip()) if auth.lower().startswith("bearer ") else None
            if not creds:
                meta = f'{SITE_URL}/.well-known/oauth-protected-resource{AUTH_MCP_PATH}'
                resp = JSONResponse({"error": "invalid_token", "error_description": "Authentication required"}, status_code=401,
                                    headers={"WWW-Authenticate": f'Bearer resource_metadata="{meta}"'})
                await resp(scope, receive, send)
                return
            # The inner MCP app only knows "/mcp": present the protected path to it under that name.
            scope = {**scope, "path": "/mcp", "raw_path": b"/mcp"}
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


async def consent(request: Request):
    """Consent page: the user types their MKK credentials; we validate them against MKK before issuing a code."""
    form = await request.form() if request.method == "POST" else {}
    txn = form.get("txn") or request.query_params.get("txn") or ""
    t = oauth.get_txn(txn)
    if not t:
        return HTMLResponse(render_to_string("consent_error.html", {"site_url": SITE_URL}), status_code=400)
    client = await oauth.get_client(t["client_id"])
    ctx = {"txn": txn, "client_name": (client.client_name if client and client.client_name else t["client_id"]), "site_url": SITE_URL,
           "cancel_url": f"{t['redirect_uri']}{'&' if '?' in t['redirect_uri'] else '?'}error=access_denied" + (f"&state={t['state']}" if t.get("state") else ""),
           "api_key": "", "api_secret": "", "test_mode": False, "error": None}
    if request.method == "POST":
        api_key = (form.get("api_key") or "").strip()
        api_secret = (form.get("api_secret") or "").strip() or None
        test_mode = form.get("test_mode") == "1"
        ctx.update({"api_key": api_key, "api_secret": api_secret or "", "test_mode": test_mode})
        try:
            async with KAPClient(api_key=api_key, api_secret=api_secret, test_mode=test_mode, timeout=15, max_retries=0) as probe:
                await probe.get_last_disclosure_index()
        except Exception as e:  # noqa: BLE001 — show MKK's answer to the user
            ctx["error"] = f"MKK bu anahtarı kabul etmedi: {str(e)[:300]}"
            return HTMLResponse(render_to_string("consent.html", ctx), status_code=400)
        return RedirectResponse(await oauth.complete_consent(txn, api_key, api_secret, test_mode), status_code=302)
    return HTMLResponse(render_to_string("consent.html", ctx))


auth_routes = create_auth_routes(
    provider=oauth, issuer_url=AnyHttpUrl(SITE_URL), service_documentation_url=AnyHttpUrl(SITE_URL + "/"),
    client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=kap_oauth.SCOPES, default_scopes=kap_oauth.SCOPES),
    revocation_options=RevocationOptions(enabled=True),
)
resource_routes = create_protected_resource_routes(
    resource_url=AnyHttpUrl(SITE_URL + AUTH_MCP_PATH), authorization_servers=[AnyHttpUrl(SITE_URL)],
    scopes_supported=kap_oauth.SCOPES, resource_name="kap-mcp (own KAP key)", resource_documentation=AnyHttpUrl(SITE_URL + "/"),
)

application = Starlette(
    routes=[
        Route("/mcp", endpoint=RateLimited(mcp_app), methods=["GET", "POST", "DELETE"]),
        Route(AUTH_MCP_PATH, endpoint=RateLimited(mcp_app), methods=["GET", "POST", "DELETE"]),
        Route("/oauth/consent", endpoint=consent, methods=["GET", "POST"]),
        *auth_routes, *resource_routes,
        Mount("/", app=django_app),
    ],
    lifespan=lifespan,
)
