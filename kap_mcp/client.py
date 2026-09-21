"""
Async KAP (Kamuyu Aydınlatma Platformu) API client.

Ported from the `pykapfinance` library and hardened for long-running MCP use:
- httpx.AsyncClient with connection pooling and explicit timeouts
- automatic retry with exponential backoff on transient failures (429/5xx/network)
- production bearer-token generation with expiry-aware refresh (refreshes early,
  and re-authenticates once on an unexpected 401)
- in-memory TTL cache for the slow, rarely-changing member/securities lists
- concurrency limiting so a burst of tool calls can't hammer the gateway
- typed exceptions carrying the gateway's fault code / correlation id
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from .exceptions import KAPAPIError, KAPAuthenticationError, KAPValidationError

log = logging.getLogger("kap_mcp.client")

BASE_URL_PROD = "https://apigw.mkk.com.tr"
BASE_URL_TEST = "https://apigwdev.mkk.com.tr"

# The disclosure list endpoint rejects anything below this index.
MIN_DISCLOSURE_INDEX = 538004
# Documented page size of /api/vyk/disclosures.
DISCLOSURE_PAGE_SIZE = 50

DISCLOSURE_TYPES = ("FR", "ODA", "DG", "DUY", "FON", "CA")
DISCLOSURE_CLASSES = ("FR", "ODA", "DG", "DUY")
FUND_STATES = ("Y", "N", "T")

RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

# Gateway fault codes documented by MKK.
FAULT_CODES = {
    "ER001": "Servis erişim yetkiniz bulunmamaktadır (API key bu servise yetkili değil)",
    "ER002": "Unauthorized request",
    "ER003": "Tanımlı olmayan IP adresinden erişim — IP adresiniz MKK tarafında whitelist'e eklenmeli",
    "ER004": "Token geçersiz",
    "ER005": "IP bilgisi doğrulanamadı (IP whitelist) — the gateway also reuses this code for 'Bildirim bulunamadı' (empty result)",
    "ER006": "Geçersiz token",
    "ER007": "Token bilgisi doğrulanamadı",
    "ER008": "Authorization token is not valid",
    "OI001": "Authentication policy failed — API key / secret / token boş veya hatalı",
}
TOKEN_FAULT_CODES = frozenset({"ER004", "ER006", "ER007", "ER008"})
AUTH_FAULT_CODES = TOKEN_FAULT_CODES | {"ER001", "ER002", "ER003", "ER005", "OI001"}


_TR_MAP = str.maketrans("çğıöşüÇĞİÖŞÜâîû", "cgiosucgiosuaiu")


def _is_not_found_message(message: Optional[str]) -> bool:
    m = normalize_tr(message or "")
    return "BULUNAMADI" in m or "NOT FOUND" in m


def normalize_tr(text: str) -> str:
    """Upper-case ASCII fold for Turkish text so 'İş' == 'IS' == 'is'."""
    return text.translate(_TR_MAP).upper().strip()


_TIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
)


def parse_kap_time(value: Any) -> Optional[datetime]:
    """Parse the `time` field of a disclosure detail (several formats seen in the wild)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # epoch millis or seconds
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1]
    if "+" in text[10:]:
        text = text[: text.rindex("+")]
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def unwrap_java_byte_array(content: bytes) -> bytes:
    """The attachment service returns the file wrapped in a Java-serialised `byte[]`
    (stream magic AC ED 00 05, TC_ARRAY, class desc for [B, then int32 length + data).
    Return the payload; pass anything else through unchanged."""
    if not content.startswith(b"\xac\xed\x00\x05"):
        return content
    marker = content.find(b"xp", 4)  # TC_ENDBLOCKDATA + TC_NULL closing the class descriptor
    if marker < 0 or marker + 6 > len(content):
        return content
    length = int.from_bytes(content[marker + 2: marker + 6], "big")
    payload = content[marker + 6: marker + 6 + length]
    return payload if len(payload) == length else content[marker + 6:]


@dataclass
class _CacheEntry:
    value: Any
    expires_at: float


@dataclass
class KAPClient:
    """Async client for the KAP VYK API."""

    api_key: str
    api_secret: Optional[str] = None
    test_mode: bool = False
    timeout: float = 30.0
    max_retries: int = 3
    cache_ttl: float = 600.0
    max_concurrency: int = 4
    token_ttl_hours: float = 24.0

    _client: Optional[httpx.AsyncClient] = field(default=None, init=False, repr=False)
    _token: Optional[str] = field(default=None, init=False, repr=False)
    _token_expiry: float = field(default=0.0, init=False, repr=False)
    _token_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _cache: dict[str, _CacheEntry] = field(default_factory=dict, init=False, repr=False)
    _cache_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _sem: Optional[asyncio.Semaphore] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.api_key:
            raise KAPValidationError("api_key is required (set KAP_API_KEY)")
        self._sem = asyncio.Semaphore(self.max_concurrency)

    # ------------------------------------------------------------------ lifecycle
    @property
    def base_url(self) -> str:
        return BASE_URL_TEST if self.test_mode else BASE_URL_PROD

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout, connect=10.0),
                headers={"Accept": "application/json", "User-Agent": "kap-mcp/0.1"},
                limits=httpx.Limits(max_connections=self.max_concurrency + 2),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "KAPClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ auth
    async def _auth_header(self, force_refresh: bool = False) -> dict[str, str]:
        if self.test_mode:
            creds = f"{self.api_key}:{self.api_secret or ''}"
            return {"Authorization": "Basic " + base64.b64encode(creds.encode()).decode()}
        token = await self._get_token(force_refresh)
        return {"Authorization": f"Bearer {token}"}

    async def _get_token(self, force_refresh: bool = False) -> str:
        # Refresh 10 minutes before the documented 24h expiry.
        if not force_refresh and self._token and time.monotonic() < self._token_expiry - 600:
            return self._token
        async with self._token_lock:
            if not force_refresh and self._token and time.monotonic() < self._token_expiry - 600:
                return self._token
            log.info("Generating new KAP token")
            try:
                resp = await self._request_raw(
                    "GET", "/auth/generateToken", params={"apiKey": self.api_key}, auth=False
                )
            except KAPAPIError as e:
                raise KAPAuthenticationError(f"Token generation failed: {e}") from e
            data = self._json(resp)
            token = data.get("token") if isinstance(data, dict) else None
            if not token:
                raise KAPAuthenticationError(f"Token generation returned no token: {data!r}")
            self._token = token
            self._token_expiry = time.monotonic() + self.token_ttl_hours * 3600
            return token

    # ------------------------------------------------------------------ transport
    @staticmethod
    def _json(resp: httpx.Response) -> Any:
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as e:
            raise KAPAPIError(
                f"Non-JSON response from {resp.request.url.path}",
                status_code=resp.status_code,
                error_message=resp.text[:500],
            ) from e

    @staticmethod
    def _error_from_response(resp: httpx.Response) -> KAPAPIError:
        code = message = correlation = None
        try:
            body = resp.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            fault = body.get("fault") if isinstance(body.get("fault"), dict) else body
            code = fault.get("faultCode") or fault.get("code")
            message = fault.get("faultString") or fault.get("message")
            correlation = fault.get("correlationId")
        is_auth = resp.status_code in (401, 403) or (code in AUTH_FAULT_CODES and not _is_not_found_message(message))
        cls = KAPAuthenticationError if is_auth else KAPAPIError
        if code in FAULT_CODES and not _is_not_found_message(message):
            message = f"{FAULT_CODES[code]}" + (f" [{message}]" if message and message != FAULT_CODES[code] else "")
        return cls(
            f"KAP API request failed ({resp.request.method} {resp.request.url.path})",
            status_code=resp.status_code,
            error_code=code,
            error_message=message or (resp.text[:300] if resp.text else None),
            correlation_id=correlation,
        )

    @staticmethod
    def _fault_code(resp: httpx.Response) -> Optional[str]:
        try:
            body = resp.json()
        except ValueError:
            return None
        if isinstance(body, dict):
            fault = body.get("fault") if isinstance(body.get("fault"), dict) else body
            return fault.get("faultCode") or fault.get("code")
        return None

    async def _request_raw(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        auth: bool = True,
        timeout: Optional[float] = None,
    ) -> httpx.Response:
        """Perform a request with retries and a one-shot re-auth on 401 (prod)."""
        assert self._sem is not None
        reauthed = False
        attempt = 0
        while True:
            headers = await self._auth_header(force_refresh=reauthed) if auth else {}
            # httpx treats an explicit timeout=None as "no timeout", so only pass it when set.
            extra: dict[str, Any] = {"timeout": timeout} if timeout is not None else {}
            try:
                async with self._sem:
                    resp = await self._http().request(method, path, params=params, headers=headers, **extra)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                if attempt >= self.max_retries:
                    raise KAPAPIError(f"Request failed after {attempt + 1} attempts: {type(e).__name__}: {e or self.base_url}") from e
                await self._backoff(attempt, None)
                attempt += 1
                continue

            if resp.status_code < 400:
                return resp

            # Bearer token may have been revoked server-side: refresh once and retry.
            token_bad = resp.status_code == 401 or self._fault_code(resp) in TOKEN_FAULT_CODES
            if token_bad and auth and not self.test_mode and not reauthed:
                log.warning("401 from KAP with bearer token; refreshing token once")
                reauthed = True
                continue

            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                await self._backoff(attempt, resp)
                attempt += 1
                continue

            raise self._error_from_response(resp)

    @staticmethod
    async def _backoff(attempt: int, resp: Optional[httpx.Response]) -> None:
        delay = min(2**attempt, 20)
        if resp is not None:
            ra = resp.headers.get("Retry-After")
            if ra and ra.isdigit():
                delay = max(delay, min(int(ra), 60))
        log.info("Retrying KAP request in %ss (attempt %d)", delay, attempt + 1)
        await asyncio.sleep(delay)

    async def _get_json(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        resp = await self._request_raw("GET", path, params=params)
        return self._json(resp)

    async def _cached(self, key: str, loader):
        now = time.monotonic()
        entry = self._cache.get(key)
        if entry and entry.expires_at > now:
            return entry.value
        async with self._cache_lock:
            entry = self._cache.get(key)
            if entry and entry.expires_at > time.monotonic():
                return entry.value
            value = await loader()
            self._cache[key] = _CacheEntry(value, time.monotonic() + self.cache_ttl)
            return value

    def clear_cache(self) -> None:
        self._cache.clear()

    # ------------------------------------------------------------------ disclosures
    async def get_last_disclosure_index(self) -> int:
        data = await self._get_json("/api/vyk/lastDisclosureIndex")
        if isinstance(data, dict):
            return int(data.get("lastDisclosureIndex", 0))
        return int(data)

    async def get_disclosures(
        self,
        disclosure_index: int,
        disclosure_type: Optional[str] = None,
        disclosure_class: Optional[str] = None,
        company_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """One page (up to 50) of disclosures with index >= disclosure_index."""
        if disclosure_index < MIN_DISCLOSURE_INDEX:
            raise KAPValidationError(f"disclosure_index must be >= {MIN_DISCLOSURE_INDEX}")
        if disclosure_type and disclosure_type not in DISCLOSURE_TYPES:
            raise KAPValidationError(f"disclosure_type must be one of {DISCLOSURE_TYPES}")
        if disclosure_class and disclosure_class not in DISCLOSURE_CLASSES:
            raise KAPValidationError(f"disclosure_class must be one of {DISCLOSURE_CLASSES}")

        params: dict[str, Any] = {"disclosureIndex": disclosure_index}
        if disclosure_type:
            params["disclosureTypes"] = disclosure_type
        if disclosure_class:
            params["disclosureClass"] = disclosure_class
        if company_id:
            params["companyId"] = company_id
        try:
            data = await self._get_json("/api/vyk/disclosures", params)
        except KAPAPIError as e:
            # The gateway answers an empty (filtered) window with HTTP 400 and the ER005 fault code
            # reused as "Bildirim bulunamadı / Disclosure not found". That is an empty page, not an error.
            if e.not_found:
                return []
            raise
        return list(data or [])

    async def get_disclosure_detail(
        self,
        disclosure_index: int,
        file_type: str = "data",
        sub_report_list: Optional[str] = None,
    ) -> dict[str, Any]:
        if file_type not in ("html", "data"):
            raise KAPValidationError("file_type must be 'html' or 'data'")
        params: dict[str, Any] = {"fileType": file_type}
        if sub_report_list:
            params["subReportList"] = sub_report_list
        data = await self._get_json(f"/api/vyk/disclosureDetail/{int(disclosure_index)}", params)
        return data or {}

    async def download_attachment(self, attachment_id: str) -> tuple[bytes, Optional[str], Optional[str]]:
        """Returns (content, content_type, filename-from-header)."""
        attachment_id = str(attachment_id).strip()
        if not attachment_id or "/" in attachment_id:
            raise KAPValidationError("attachment_id must be a bare id (last path segment of the url)")
        resp = await self._request_raw(
            "GET", f"/api/vyk/downloadAttachment/{attachment_id}", timeout=max(self.timeout, 90.0)
        )
        filename = None
        cd = resp.headers.get("Content-Disposition", "")
        if "filename=" in cd:
            filename = cd.split("filename=", 1)[1].strip().strip('"')
        return unwrap_java_byte_array(resp.content), resp.headers.get("Content-Type"), filename

    async def get_disclosure_time(self, disclosure_index: int) -> Optional[datetime]:
        """Publication time of a disclosure (via the detail service). Cached; None if unavailable."""
        key = f"time:{int(disclosure_index)}"
        entry = self._cache.get(key)
        if entry and entry.expires_at > time.monotonic():
            return entry.value
        try:
            detail = await self.get_disclosure_detail(disclosure_index, file_type="data")
        except KAPAPIError as e:
            if e.not_found:
                return None
            raise
        ts = parse_kap_time(detail.get("time"))
        # Publication times never change, so cache for long.
        self._cache[key] = _CacheEntry(ts, time.monotonic() + 86400)
        return ts

    async def find_index_for_date(self, when: datetime, max_probes: int = 40) -> int:
        """Smallest disclosure index whose publication time is >= `when` (binary search).

        Indices that have no detail (blocked/removed) are skipped by probing neighbours.
        Returns last_index + 1 if `when` is in the future.
        """
        lo, hi = MIN_DISCLOSURE_INDEX, await self.get_last_disclosure_index()
        probes = 0
        async def time_near(idx: int) -> tuple[int, Optional[datetime]]:
            # Walk a few neighbours if this exact index has no detail.
            for delta in (0, 1, -1, 2, -2, 3, -3, 5, -5, 8, -8):
                j = idx + delta
                if lo <= j <= hi:
                    ts = await self.get_disclosure_time(j)
                    if ts is not None:
                        return j, ts
            return idx, None

        result = hi + 1
        while lo <= hi and probes < max_probes:
            mid = (lo + hi) // 2
            j, ts = await time_near(mid)
            probes += 1
            if ts is None:
                # Whole neighbourhood missing; bias upward.
                lo = mid + 1
                continue
            if ts >= when:
                result = j
                hi = j - 1
            else:
                lo = j + 1
        return result

    async def get_blocked_disclosures(self) -> list[dict[str, Any]]:
        return list(await self._get_json("/api/vyk/blockedDisclosures") or [])

    # ------------------------------------------------------------------ members
    async def get_members(self) -> list[dict[str, Any]]:
        return await self._cached("members", lambda: self._load_list("/api/vyk/members"))

    async def _load_list(self, path: str) -> list[dict[str, Any]]:
        return list(await self._get_json(path) or [])

    async def get_member_detail(self, member_id: str) -> dict[str, Any]:
        member_id = str(member_id).strip()
        if not member_id or "/" in member_id:
            raise KAPValidationError("member_id is invalid")
        return await self._get_json(f"/api/vyk/memberDetail/{member_id}") or {}

    async def get_member_securities(self) -> list[dict[str, Any]]:
        return await self._cached("member_securities", lambda: self._load_list("/api/vyk/memberSecurities"))

    async def find_members(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Turkish-insensitive search by stock code or title. Exact stock-code hits rank first."""
        q = normalize_tr(query)
        if not q:
            raise KAPValidationError("query cannot be empty")
        members = await self.get_members()
        exact, code_partial, title_partial = [], [], []
        for m in members:
            code = normalize_tr(m.get("stockCode") or "")
            title = normalize_tr(m.get("title") or "")
            codes = [c.strip() for c in code.replace(";", ",").split(",") if c.strip()]
            if q in codes:
                exact.append(m)
            elif q in code:
                code_partial.append(m)
            elif q in title:
                title_partial.append(m)
        return (exact + code_partial + title_partial)[:limit]

    # ------------------------------------------------------------------ funds
    async def get_funds(
        self,
        fund_state: Optional[list[str]] = None,
        fund_class: Optional[list[str]] = None,
        fund_type: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        if fund_state:
            bad = [s for s in fund_state if s not in FUND_STATES]
            if bad:
                raise KAPValidationError(f"fund_state values must be in {FUND_STATES}, got {bad}")
        params: dict[str, Any] = {}
        if fund_state:
            params["fundState"] = fund_state
        if fund_class:
            params["fundClass"] = fund_class
        if fund_type:
            params["fundType"] = fund_type
        return list(await self._get_json("/api/vyk/funds", params) or [])

    async def get_fund_detail(self, fund_id: int) -> dict[str, Any]:
        return await self._get_json(f"/api/vyk/fundDetail/{int(fund_id)}") or {}

    # ------------------------------------------------------------------ corporate actions
    async def get_ca_event_status(self, process_ref_ids: list[int]) -> list[dict[str, Any]]:
        if not process_ref_ids:
            raise KAPValidationError("process_ref_ids cannot be empty")
        params = {"processRefId": ",".join(str(int(i)) for i in process_ref_ids)}
        return list(await self._get_json("/api/vyk/caEventStatus", params) or [])
