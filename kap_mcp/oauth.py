"""
OAuth 2.1 authorization server for "bring your own KAP key".

Clients that cannot send custom headers (claude.ai custom connectors, ChatGPT
with OAuth) go through the standard flow: dynamic client registration, PKCE
authorization at `/authorize`, a consent page where the *user* types their MKK
credentials, then `/token`. The access token maps to those credentials, which
the MCP endpoint `/mcp/auth` uses for every request carrying the bearer token.

Storage is a single encrypted JSON file (Fernet, key from KAP_OAUTH_SECRET) so
KAP keys never sit on disk in clear text. Tokens are opaque random strings.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

ACCESS_TTL = 3600 * 24 * 30      # 30 days; refresh keeps sessions alive beyond that
REFRESH_TTL = 3600 * 24 * 180
CODE_TTL = 600
TXN_TTL = 900
SCOPES = ["kap"]


class EncryptedStore:
    """Tiny persisted dict, encrypted at rest. Writes are debounced-free (small file)."""

    def __init__(self, path: Path, secret: str) -> None:
        self.path = path
        self.fernet = Fernet(secret.encode() if len(secret) == 44 else Fernet.generate_key())
        self.data: dict[str, dict[str, Any]] = {"clients": {}, "creds": {}, "codes": {}, "access": {}, "refresh": {}, "txns": {}}
        self._lock = asyncio.Lock()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            self.data.update(json.loads(self.fernet.decrypt(self.path.read_bytes()).decode()))
        except (InvalidToken, ValueError):
            # Wrong secret or corrupt file: start empty rather than crash; users simply re-authorise.
            pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(self.fernet.encrypt(json.dumps(self.data).encode()))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def purge_expired(self) -> None:
        now = time.time()
        for bucket in ("codes", "access", "refresh", "txns"):
            self.data[bucket] = {k: v for k, v in self.data[bucket].items() if not v.get("expires_at") or v["expires_at"] > now}
        used = {v["creds_id"] for b in ("access", "refresh", "codes") for v in self.data[b].values() if v.get("creds_id")}
        self.data["creds"] = {k: v for k, v in self.data["creds"].items() if k in used}


class KAPKeyProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    def __init__(self, store: EncryptedStore, consent_url: str) -> None:
        self.store = store
        self.consent_url = consent_url  # absolute URL of the consent page

    # ---- clients (DCR)
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = self.store.data["clients"].get(client_id)
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        async with self.store._lock:
            self.store.data["clients"][client_info.client_id] = client_info.model_dump(mode="json")
            self.store.save()

    # ---- authorization
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        txn = secrets.token_urlsafe(24)
        async with self.store._lock:
            self.store.purge_expired()
            self.store.data["txns"][txn] = {
                "client_id": client.client_id, "state": params.state, "scopes": params.scopes or SCOPES,
                "code_challenge": params.code_challenge, "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "resource": params.resource, "expires_at": time.time() + TXN_TTL,
            }
            self.store.save()
        return f"{self.consent_url}?txn={txn}"

    def get_txn(self, txn: str) -> Optional[dict[str, Any]]:
        t = self.store.data["txns"].get(txn)
        return t if t and t["expires_at"] > time.time() else None

    async def complete_consent(self, txn: str, api_key: str, api_secret: Optional[str], test_mode: bool) -> str:
        """Called by the consent page after the user submitted valid credentials. Returns the redirect URL."""
        t = self.get_txn(txn)
        if not t:
            raise AuthorizeError(error="invalid_request", error_description="authorization request expired")
        creds_id = secrets.token_urlsafe(12)
        code = secrets.token_urlsafe(32)
        async with self.store._lock:
            self.store.data["creds"][creds_id] = {"api_key": api_key, "api_secret": api_secret, "test_mode": test_mode}
            self.store.data["codes"][code] = {**t, "creds_id": creds_id, "expires_at": time.time() + CODE_TTL}
            self.store.data["txns"].pop(txn, None)
            self.store.save()
        from urllib.parse import urlencode

        q = {"code": code}
        if t.get("state"):
            q["state"] = t["state"]
        sep = "&" if "?" in t["redirect_uri"] else "?"
        return f"{t['redirect_uri']}{sep}{urlencode(q)}"

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        c = self.store.data["codes"].get(authorization_code)
        if not c or c["client_id"] != client.client_id or c["expires_at"] < time.time():
            return None
        return AuthorizationCode(code=authorization_code, scopes=c["scopes"], expires_at=c["expires_at"], client_id=c["client_id"],
                                 code_challenge=c["code_challenge"], redirect_uri=AnyUrl(c["redirect_uri"]),
                                 redirect_uri_provided_explicitly=c["redirect_uri_provided_explicitly"], resource=c.get("resource"),
                                 subject=c["creds_id"])

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        async with self.store._lock:
            c = self.store.data["codes"].pop(authorization_code.code, None)
            if not c:
                raise TokenError(error="invalid_grant", error_description="code already used")
            token = self._issue(client.client_id, c["creds_id"], c["scopes"], c.get("resource"))
            self.store.save()
        return token

    def _issue(self, client_id: str, creds_id: str, scopes: list[str], resource: Optional[str]) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = time.time()
        self.store.data["access"][access] = {"client_id": client_id, "creds_id": creds_id, "scopes": scopes, "resource": resource, "expires_at": now + ACCESS_TTL}
        self.store.data["refresh"][refresh] = {"client_id": client_id, "creds_id": creds_id, "scopes": scopes, "resource": resource, "expires_at": now + REFRESH_TTL}
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL, refresh_token=refresh, scope=" ".join(scopes))

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        r = self.store.data["refresh"].get(refresh_token)
        if not r or r["client_id"] != client.client_id or r["expires_at"] < time.time():
            return None
        return RefreshToken(token=refresh_token, client_id=r["client_id"], scopes=r["scopes"], expires_at=int(r["expires_at"]),
                            resource=r.get("resource"), subject=r["creds_id"])

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        async with self.store._lock:
            r = self.store.data["refresh"].pop(refresh_token.token, None)
            if not r:
                raise TokenError(error="invalid_grant", error_description="refresh token unknown")
            token = self._issue(client.client_id, r["creds_id"], scopes or r["scopes"], r.get("resource"))
            self.store.save()
        return token

    async def load_access_token(self, token: str) -> AccessToken | None:
        a = self.store.data["access"].get(token)
        if not a or a["expires_at"] < time.time():
            return None
        return AccessToken(token=token, client_id=a["client_id"], scopes=a["scopes"], expires_at=int(a["expires_at"]),
                           resource=a.get("resource"), subject=a["creds_id"])

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        async with self.store._lock:
            self.store.data["access"].pop(token.token, None)
            self.store.data["refresh"].pop(token.token, None)
            self.store.purge_expired()
            self.store.save()

    async def exchange_identity_assertion(self, *args: Any, **kwargs: Any) -> OAuthToken:  # RFC 7523 not offered
        raise TokenError(error="unsupported_grant_type", error_description="identity assertion is not supported")

    # ---- used by the MCP endpoint
    def credentials_for_token(self, bearer: str) -> Optional[dict[str, Any]]:
        a = self.store.data["access"].get(bearer)
        if not a or a["expires_at"] < time.time():
            return None
        return self.store.data["creds"].get(a["creds_id"])


def build_provider(site_url: str, store_path: Path, secret: str) -> KAPKeyProvider:
    return KAPKeyProvider(EncryptedStore(store_path, secret), consent_url=f"{site_url}/oauth/consent")
