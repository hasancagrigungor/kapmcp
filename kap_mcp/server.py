"""
KAP MCP server — a data + deterministic-computation layer for Borsa İstanbul.

Sources: the official KAP (MKK VYK) API for disclosures, companies, funds and
corporate actions; Yahoo Finance for prices, statements, estimates and news.
Every tool returns structured JSON with `source` / `source_url` fields. Tools
never interpret, rank, recommend or draw — that is the calling agent's job.

Configuration (environment, see config.py): KAP_API_KEY (required for KAP
tools), KAP_API_SECRET, KAP_TEST_MODE, KAP_TIMEOUT, KAP_CACHE_TTL,
KAP_MAX_SCAN_PAGES, KAP_MAX_RESULT_CHARS, YAHOO_TIMEOUT, KAP_LOG_LEVEL.
Transports: stdio (default) and streamable-http (`--transport streamable-http`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Literal, Optional

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from . import __version__, market
from .client import MIN_DISCLOSURE_INDEX, KAPClient, normalize_tr, parse_kap_time
from .config import Settings, load_settings
from .exceptions import KAPAPIError, KAPAuthenticationError, KAPError, KAPValidationError
from .market import MarketDataError, run_blocking, to_yahoo_symbol
from .services import company as company_svc
from .services import corporate_actions as ca_svc
from .services import disclosures as disc_svc
from .services import documents as doc_svc
from .services import financials as fin_svc
from .services import kap_financials as kapfin_svc
from .services import technicals as ta_svc
from .text import html_to_text, search_flat

log = logging.getLogger("kap_mcp")

DisclosureType = Literal["FR", "ODA", "DG", "DUY", "FON", "CA"]
DisclosureClass = Literal["FR", "ODA", "DG", "DUY"]
MemberType = Literal["IGS", "IGMS", "YK", "PYS", "DDK", "FK", "BDK", "DCS", "DS", "DG"]
Frequency = Literal["annual", "quarterly"]
FundState = Literal["Y", "N", "T"]
Period = Literal["1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"]
Interval = Literal["1m", "5m", "15m", "30m", "1h", "1d", "1wk", "1mo"]
MetricName = Literal[tuple(fin_svc.NORMALISED_FIELDS)]  # type: ignore[valid-type]
RatioName = Literal[fin_svc.RATIO_NAMES]  # type: ignore[valid-type]
ActionType = Literal[ca_svc.ACTION_TYPES]  # type: ignore[valid-type]

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)

# ----------------------------------------------------------------------------- state


settings: Settings = load_settings()
_client: Optional[KAPClient] = None
_client_lock = asyncio.Lock()


def set_client(client: Optional[KAPClient]) -> None:
    """Inject a client (tests)."""
    global _client
    _client = client


def reload_settings() -> None:
    global settings
    settings = load_settings()


async def get_client() -> KAPClient:
    global _client
    if _client is None:
        async with _client_lock:
            if _client is None:
                if not settings.kap_configured:
                    raise ToolError("KAP is not configured: set KAP_API_KEY (and KAP_API_SECRET + KAP_TEST_MODE=1 for the MKK test gateway). "
                                    "Yahoo Finance tools work without it.")
                _client = KAPClient(api_key=settings.kap_api_key, api_secret=settings.kap_api_secret, test_mode=settings.kap_test_mode,
                                    timeout=settings.kap_timeout, cache_ttl=settings.kap_cache_ttl,
                                    max_concurrency=settings.kap_max_concurrency)
    return _client


async def shutdown() -> None:
    """Close the shared KAP client (call once at process exit)."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


@asynccontextmanager
async def _lifespan(_server: MCPServer) -> AsyncIterator[dict[str, Any]]:
    # Runs once per MCP session; the KAP client is process-wide, so it is closed in shutdown(), not here.
    yield {}


mcp = MCPServer(
    name="kap",
    version=__version__,
    instructions=(
        "Borsa İstanbul data layer. KAP (official) tools: search_companies, get_company, search_disclosures, get_disclosure, "
        "documents, funds. Yahoo tools (no key needed): quotes, price history, financials, ratios, growth, news. "
        "Tickers are BIST codes (THYAO); Yahoo symbols get '.IS' automatically. Computations are deterministic; "
        "interpretation, ranking and charts are yours. Unofficial project, not affiliated with KAP/MKK/Borsa İstanbul/Yahoo."
    ),
    lifespan=_lifespan,
)

# ----------------------------------------------------------------------------- errors


def _tool_error(e: Exception) -> ToolError:
    if isinstance(e, ToolError):
        return e
    if isinstance(e, KAPValidationError):
        return ToolError(f"Invalid input: {e}")
    if isinstance(e, KAPAuthenticationError):
        return ToolError(f"KAP authentication failed: {e}. Check KAP_API_KEY / KAP_API_SECRET / KAP_TEST_MODE and IP whitelist.")
    if isinstance(e, KAPAPIError):
        if e.not_found:
            return ToolError(f"Not found: KAP has no record for this request ({e.error_message or 'not found'}). "
                             "Blocked/withdrawn ids are listed by get_blocked_disclosures.")
        return ToolError(f"KAP API error: {e}")
    if isinstance(e, KAPError):
        return ToolError(str(e))
    if isinstance(e, MarketDataError):
        return ToolError(f"Yahoo Finance: {e}")
    if isinstance(e, ValueError):
        return ToolError(f"Invalid input: {e}")
    log.exception("Unexpected error in tool")
    return ToolError(f"Unexpected error: {type(e).__name__}: {e}")


async def _run(coro):
    try:
        return await coro
    except Exception as e:  # noqa: BLE001
        raise _tool_error(e) from e


async def _yahoo(fn, *args, **kwargs):
    return await _run(run_blocking(fn, *args, timeout=settings.yahoo_timeout, **kwargs))


def _bounded(out: dict[str, Any]) -> dict[str, Any]:
    """Keep any single result under the configured size so clients don't reject it."""
    size = len(json.dumps(out, ensure_ascii=False, default=str))
    if size <= settings.max_result_chars:
        return out
    for key in ("items", "series", "candles", "rows", "events", "news", "results", "matches", "periods"):
        if isinstance(out.get(key), list) and out[key]:
            keep = max(1, int(len(out[key]) * settings.max_result_chars / size))
            out[key] = out[key][:keep]
            out["truncated"] = True
            out["truncation_note"] = f"result exceeded {settings.max_result_chars} chars; '{key}' cut to {keep} entries — narrow the query or paginate"
            break
    return out


def _date(value: Optional[str], end_of_day: bool = False) -> Optional[datetime]:
    return disc_svc.parse_date(value, end_of_day=end_of_day)


def _pages(requested: Optional[int], default: int) -> int:
    return min(requested or default, settings.max_scan_pages)


# ----------------------------------------------------------------------------- reference


REFERENCE_CODES: dict[str, dict[str, str]] = {
    "disclosure_types": {"FR": "Finansal Rapor", "ODA": "Özel Durum Açıklaması", "DG": "Diğer Bildirim",
                         "DUY": "Düzenleyici Kurum Bildirimi", "FON": "Fon Bildirimi", "CA": "Hak Kullanım (corporate action)"},
    "disclosure_classes": {"FR": "Finansal Rapor", "ODA": "Özel Durum Açıklaması", "DG": "Diğer", "DUY": "Düzenleyici Kurum"},
    "member_types": {"IGS": "İşlem Gören Şirket (listed)", "IGMS": "İşlem Görmeyen Şirket", "YK": "Yatırım Kuruluşu",
                     "PYS": "Portföy Yönetim Şirketi", "DDK": "Düzenleyici/Denetleyici Kurum", "FK": "Fon Kurucu",
                     "BDK": "Bağımsız Denetim Kuruluşu", "DCS": "Derecelendirme Şirketi", "DS": "Değerlendirme Şirketi", "DG": "Diğer"},
    "fund_states": {"Y": "Aktif", "N": "Pasif", "T": "Tasfiye"},
    "fund_types": {"SYF": "Şemsiye Yatırım Fonu", "KGF": "Koruma Amaçlı/Garantili", "EYF": "Emeklilik Yatırım Fonu",
                   "OKS": "OKS Emeklilik", "YYF": "Yabancı Yatırım Fonu", "BYF": "Borsa Yatırım Fonu", "VFF": "Varlık Finansman",
                   "KFF": "Konut Finansman", "GMF": "Gayrimenkul Yatırım Fonu", "GSF": "Girişim Sermayesi", "PFF": "Proje Finansman"},
    "corporate_action_statuses": {"DV": "Devam Ediyor", "TM": "Tamamlandı", "YM": "Yapılmadı", "IP": "İptal"},
    "corporate_action_types": {t: t for t in ca_svc.ACTION_TYPES},
    "financial_fields": {k: " | ".join(v) for k, v in fin_svc.NORMALISED_FIELDS.items()},
    "ratios": {r: r for r in fin_svc.RATIO_NAMES},
    "gateway_fault_codes": {"ER001": "no service permission", "ER002": "unauthorized", "ER003": "IP not whitelisted",
                            "ER004": "token invalid", "ER005": "IP could not be verified", "ER006": "invalid token",
                            "ER007": "token could not be verified", "ER008": "authorization token not valid"},
    "notes": {"disclosure_id": f"KAP disclosure index; starts at {MIN_DISCLOSURE_INDEX}",
              "timezone": "KAP times are Türkiye local (UTC+3); Yahoo dates are exchange local",
              "financials_source": "Normalised statements come from Yahoo Finance; the official filing is the KAP FR disclosure"},
}


@mcp.resource("kap://reference/codes", title="KAP reference codes", mime_type="application/json")
def reference_codes_resource() -> str:
    return json.dumps(REFERENCE_CODES, ensure_ascii=False, indent=2)


@mcp.tool(title="Reference codes", annotations=READ_ONLY)
async def get_reference_codes() -> dict[str, dict[str, str]]:
    """Code tables used across tools: disclosure types/classes, member types, fund states/types,
    corporate-action types/statuses, normalised financial field names, ratio names, gateway fault codes.
    No network call."""
    return REFERENCE_CODES


@mcp.tool(title="Server status", annotations=READ_ONLY)
async def kap_status() -> dict[str, Any]:
    """Configuration and connectivity check. Call first when other tools fail to tell a
    configuration problem (missing key, IP whitelist) from a data problem."""
    info: dict[str, Any] = {"version": __version__, "kap_configured": settings.kap_configured,
                            "kap_environment": "test" if settings.kap_test_mode else "production",
                            "yahoo_available": True, "limits": {"max_scan_pages": settings.max_scan_pages,
                                                                "max_result_chars": settings.max_result_chars}}
    if not settings.kap_configured and _client is None:
        info["kap_reachable"] = False
        info["kap_error"] = "KAP_API_KEY not set"
    else:
        try:
            client = await get_client()
            info["kap_base_url"] = client.base_url
            info["last_disclosure_id"] = await client.get_last_disclosure_index()
            info["kap_reachable"] = True
        except Exception as e:  # noqa: BLE001
            info["kap_reachable"] = False
            info["kap_error"] = str(_tool_error(e))
    try:
        info["yahoo_check"] = (await run_blocking(market.get_quote, "XU100", timeout=15))["lastPrice"]
    except Exception as e:  # noqa: BLE001
        info["yahoo_available"] = False
        info["yahoo_error"] = str(e)
    return info


# ----------------------------------------------------------------------------- companies


@mcp.tool(title="Search companies", annotations=READ_ONLY)
async def search_companies(
    query: Optional[str] = Field(default=None, description="Ticker (THYAO) or part of the name, Turkish-insensitive. Omit to list by member_type."),
    member_type: Optional[MemberType] = Field(default=None, description="Filter by KAP member type; IGS = listed companies"),
    limit: int = Field(default=20, ge=1, le=500),
    offset: int = Field(default=0, ge=0),
) -> dict[str, Any]:
    """Find KAP members (companies, funds' founders, brokers…) by ticker or name and get their
    `company_id`, which other KAP tools take. Exact ticker matches rank first. Use when the
    user names a company; use get_company for the full profile."""
    client = await get_client()

    async def go():
        if query:
            hits = await client.find_members(query, limit=10000)
        else:
            hits = await client.get_members()
        if member_type:
            hits = [m for m in hits if m.get("memberType") == member_type]
        page = hits[offset: offset + limit]
        return {"query": query, "total": len(hits), "offset": offset, "count": len(page), "source": "kap",
                "items": [{"company_id": m.get("id"), "name": m.get("title"), "ticker": disc_svc.primary_ticker(m),
                           "tickers": disc_svc.split_codes(m.get("stockCode")), "member_type": m.get("memberType"),
                           "source_url": disc_svc.KAP_COMPANY_URL.format(member_id=m.get("id"))} for m in page]}

    return await _run(go())


@mcp.tool(title="Company profile", annotations=READ_ONLY)
async def get_company(
    ticker: Optional[str] = Field(default=None, description="BIST ticker, e.g. THYAO"),
    company_id: Optional[str] = Field(default=None, description="KAP member id (from search_companies)"),
    include_market_profile: bool = Field(default=True, description="Add Yahoo sector/industry/description and holder breakdown"),
) -> dict[str, Any]:
    """Company profile from KAP: name, tickers, member type, paid-in/registered capital, ISINs,
    plus shareholders / board / subsidiaries / sector / market / indices when KAP's memberDetail
    payload contains them (sections KAP does not provide are returned as null with a
    `not_available` note, never invented). Optionally adds Yahoo profile and holders."""
    client = await get_client()
    out = await _run(company_svc.get_company(client, ticker=ticker, company_id=company_id))
    if include_market_profile and out.get("ticker"):
        try:
            prof = (await run_blocking(market.get_fundamentals, out["ticker"], timeout=settings.yahoo_timeout))["profile"]
            out["market_profile"] = {"source": "yahoo_finance", "symbol": to_yahoo_symbol(out["ticker"]), **prof}
        except Exception as e:  # noqa: BLE001
            out["market_profile"] = {"source": "yahoo_finance", "error": str(e)}
        try:
            out["holders"] = await run_blocking(market.get_holders, out["ticker"], timeout=settings.yahoo_timeout)
        except Exception as e:  # noqa: BLE001
            out["holders"] = {"source": "yahoo_finance", "error": str(e)}
    return _bounded(out)


# ----------------------------------------------------------------------------- disclosures


@mcp.tool(title="Search disclosures", annotations=READ_ONLY)
async def search_disclosures(
    tickers: Optional[list[str]] = Field(default=None, description="Restrict to these BIST tickers (e.g. ['THYAO','PGSUS'])", max_length=10),
    company_id: Optional[str] = Field(default=None, description="Restrict to one KAP member id"),
    query: Optional[str] = Field(default=None, description="Keywords (any word matches, Turkish-insensitive). With query_scope='metadata' they match the sender name and the disclosure template name (e.g. 'New Business Relation', 'Valuation Report'); with 'content' also the subject/summary of every scanned item (one KAP call per item, slower)."),
    query_scope: Literal["metadata", "content"] = Field(default="metadata", description="Where keywords are matched; see `query`"),
    disclosure_type: Optional[DisclosureType] = Field(default=None, description="FR financial report, ODA material event, DG other, DUY regulator, FON fund, CA corporate action"),
    disclosure_class: Optional[DisclosureClass] = Field(default=None),
    start_date: Optional[str] = Field(default=None, description="YYYY-MM-DD (inclusive). Resolved to an index via binary search."),
    end_date: Optional[str] = Field(default=None, description="YYYY-MM-DD (inclusive)"),
    since_id: Optional[int] = Field(default=None, ge=MIN_DISCLOSURE_INDEX, description="Only disclosures newer than this id (polling). Overrides dates."),
    cursor: Optional[int] = Field(default=None, description="`next_cursor` from a previous call to continue paging"),
    limit: int = Field(default=20, ge=1, le=200),
    order: Literal["desc", "asc"] = Field(default="desc", description="desc = newest first"),
    include_details: bool = Field(default=False, description="Also fetch published_at, subject, summary, attachments per item (one KAP call each)"),
    max_pages: Optional[int] = Field(default=None, ge=1, description="Cap on 50-item index windows scanned (default 40, server max applies). Company/keyword filters on long ranges need more."),
) -> dict[str, Any]:
    """Search KAP disclosures by company, keywords, type/template and date range, newest first,
    with cursor paging. Covers: latest disclosures, a company's disclosures over a period,
    everything since an id (monitoring), topic hunts like 'sözleşme' or 'bedelsiz' across
    companies (use query_scope='content' for subject/summary matching).
    The KAP API only pages by index, so wide ranges scan many windows: check `scan_complete`;
    if false, continue with `cursor` or raise `max_pages`. Items carry `disclosure_id`,
    `templates` (KAP's topic template codes), ticker, company name and `source_url`."""
    client = await get_client()

    async def go():
        ids: list[str] = []
        if company_id:
            ids.append(company_id)
        for t in tickers or []:
            ids.append((await disc_svc.resolve_company(client, ticker=t))["id"])
        terms = [w for w in (query or "").split() if w.strip()] or None
        start = _date(start_date)
        end = _date(end_date, end_of_day=True)
        if start and end and end < start:
            raise KAPValidationError("end_date is before start_date")
        res = await disc_svc.search(client, company_ids=ids or None, terms=terms, disclosure_type=disclosure_type,
                                    disclosure_class=disclosure_class, start=start, end=end, since_id=since_id, cursor=cursor,
                                    limit=limit, order=order, max_pages=_pages(max_pages, 40), include_details=include_details,
                                    query_scope=query_scope)
        res["filters"] = {"tickers": tickers, "company_id": company_id, "query": query, "disclosure_type": disclosure_type,
                          "disclosure_class": disclosure_class, "start_date": start_date, "end_date": end_date, "since_id": since_id}
        res["source"] = "kap"
        return res

    return _bounded(await _run(go()))


@mcp.tool(title="Get disclosure", annotations=READ_ONLY)
async def get_disclosure(
    disclosure_id: int = Field(ge=1, description="KAP disclosure index"),
    format: Literal["summary", "text", "full"] = Field(default="text", description="summary = metadata only; text = + body as plain text and flattened data; full = + raw KAP payload"),
    sub_report_list: Optional[str] = Field(default=None, description="Comma-separated sub-report ids (financial reports)"),
    max_chars: int = Field(default=30000, ge=1000, le=200000),
) -> dict[str, Any]:
    """One disclosure with normalised metadata (company, ticker, published_at, subject, summary,
    fiscal period, related tickers, attachments with ids, source_url) and, by default, the body
    as plain text plus structured data flattened to 'path: value' lines."""
    client = await get_client()

    async def go():
        detail = await client.get_disclosure_detail(disclosure_id, file_type="data", sub_report_list=sub_report_list)
        if format != "summary" and not detail.get("htmlMessage"):
            try:
                detail["htmlMessage"] = (await client.get_disclosure_detail(disclosure_id, file_type="html", sub_report_list=sub_report_list)).get("htmlMessage")
            except KAPAPIError:
                pass
        out = disc_svc.normalise_detail(detail, text_format=format, max_chars=max_chars)
        out["source"] = "kap"
        return out

    return _bounded(await _run(go()))


@mcp.tool(title="Search disclosure data fields", annotations=READ_ONLY)
async def search_disclosure_data(
    disclosure_id: int = Field(ge=1),
    query: str = Field(min_length=1, description="Field name or value to find, e.g. 'net dönem karı', 'hasılat', 'kar payı'"),
    sub_report_list: Optional[str] = Field(default=None),
    limit: int = Field(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """Find matching fields inside a disclosure's structured data (financial statement line
    items, form fields) and return path/value pairs. Turkish-insensitive. Use to pull single
    figures from an official KAP financial report or form without reading the whole body."""
    client = await get_client()

    async def go():
        data = await client.get_disclosure_detail(disclosure_id, file_type="data", sub_report_list=sub_report_list)
        hits = []
        blocks = [str(b.get("id")) for b in data.get("presentation") or []]
        if any(b.startswith("general_role_") for b in blocks):
            # Financial statements: search the XBRL facts by Turkish label or taxonomy name and return
            # the numbers per context instead of raw tree paths.
            ex = kapfin_svc.extract_facts(data["presentation"])
            q = normalize_tr(query)
            for name, ctx_vals in ex["facts"].items():
                label = ex["labels"].get(name) or ""
                if q in normalize_tr(label) or q in normalize_tr(name):
                    hits.append({"section": "xbrl", "taxonomy_name": name, "label_tr": label, "currency": ex["currency"],
                                 "values": ctx_vals, "contexts": {k: ex["contexts"].get(k) for k in ctx_vals}})
            return {"disclosure_id": disclosure_id, "query": query, "count": len(hits), "source": "kap",
                    "source_url": disc_svc.kap_url(disclosure_id), "matches": hits[:limit]}
        for key in ("presentation", "flatData"):
            if data.get(key):
                hits.extend({**h, "section": key} for h in search_flat(data[key], query, normalize_tr))
        if not hits and data.get("htmlMessage"):
            q = normalize_tr(query)
            hits = [{"section": "html", "path": f"line {i}", "value": ln}
                    for i, ln in enumerate(html_to_text(data["htmlMessage"]).split("\n"), 1) if q in normalize_tr(ln)]
        return {"disclosure_id": disclosure_id, "query": query, "count": len(hits), "source": "kap",
                "source_url": disc_svc.kap_url(disclosure_id), "matches": hits[:limit]}

    return await _run(go())


@mcp.tool(title="Blocked disclosures", annotations=READ_ONLY)
async def get_blocked_disclosures() -> dict[str, Any]:
    """Disclosures KAP has withdrawn from public access (e.g. under KVKK) with the stated reason.
    Explains 'not found' results for specific ids."""
    client = await get_client()
    items = await _run(client.get_blocked_disclosures())
    return _bounded({"count": len(items), "source": "kap", "items": [
        {"disclosure_id": b.get("disclosureIndex"), "blocked_type": b.get("blockedType"), "company_name": b.get("senderTitle"),
         "on_behalf_of": b.get("behalfSenderTitle"), "reason_tr": b.get("isBlockedDescriptionTr"), "reason_en": b.get("isBlockedDescriptionEn"),
         "source_url": disc_svc.kap_url(b["disclosureIndex"]) if b.get("disclosureIndex") else None} for b in items]})


# ----------------------------------------------------------------------------- documents


@mcp.tool(title="Disclosure documents", annotations=READ_ONLY)
async def get_disclosure_documents(disclosure_id: int = Field(ge=1)) -> dict[str, Any]:
    """List a disclosure's attachments (PDF/XLSX/…) with `attachment_id`, name and source_url.
    Pass an id to get_document_text or search_documents to read it."""
    client = await get_client()

    async def go():
        detail = await client.get_disclosure_detail(disclosure_id, file_type="data")
        atts = disc_svc.attachments_of(detail)
        for a in atts:
            a["disclosure_id"] = disclosure_id
        return {"disclosure_id": disclosure_id, "count": len(atts), "source": "kap",
                "source_url": disc_svc.kap_url(disclosure_id), "documents": atts}

    return await _run(go())


@mcp.tool(title="Document text", annotations=READ_ONLY)
async def get_document_text(
    attachment_id: str = Field(min_length=1, description="From get_disclosure_documents / get_disclosure attachments"),
    pages: Optional[str] = Field(default=None, description="PDF page selection like '1-3,7' (1-based). Default: all pages, subject to max_chars."),
    max_chars: int = Field(default=30000, ge=1000, le=200000),
) -> dict[str, Any]:
    """Extract text from a PDF/HTML/text attachment (in memory; nothing is stored). Binary
    formats (xlsx, zip) and scanned PDFs return `text: null` with a reason. Prefer
    search_documents to pull only the relevant part of long reports."""
    client = await get_client()

    async def go():
        doc = await doc_svc.load_document(client, attachment_id)
        out = doc_svc.document_text(doc, pages, max_chars)
        out.update({"attachment_id": attachment_id, "name": doc["name"], "content_type": doc["content_type"], "bytes": doc["bytes"], "source": "kap"})
        return out

    return await _run(go())


@mcp.tool(title="Search documents", annotations=READ_ONLY)
async def search_documents(
    query: str = Field(min_length=2, description="Words to find (any word matches; Turkish-insensitive)"),
    disclosure_id: Optional[int] = Field(default=None, ge=1, description="Search all attachments of this disclosure"),
    attachment_id: Optional[str] = Field(default=None, description="…or a single attachment"),
    max_results: int = Field(default=10, ge=1, le=50),
    context_chars: int = Field(default=300, ge=80, le=2000, description="Snippet length around each match"),
) -> dict[str, Any]:
    """Find passages inside disclosure attachments and return short snippets with page numbers
    and source_url instead of whole documents. Use for 'what does the report say about X'."""
    client = await get_client()

    async def go():
        if not disclosure_id and not attachment_id:
            raise KAPValidationError("disclosure_id or attachment_id is required")
        targets: list[dict[str, Any]] = []
        if attachment_id:
            targets.append({"attachment_id": attachment_id, "name": None, "source_url": None, "disclosure_id": disclosure_id})
        else:
            detail = await client.get_disclosure_detail(disclosure_id, file_type="data")
            targets = [{**a, "disclosure_id": disclosure_id} for a in disc_svc.attachments_of(detail) if a.get("attachment_id")]
        results, skipped = [], []
        for t in targets:
            if len(results) >= max_results:
                break
            doc = await doc_svc.load_document(client, t["attachment_id"])
            if doc["kind"] in ("binary", "too_large"):
                skipped.append({"attachment_id": t["attachment_id"], "name": doc["name"], "reason": doc["kind"]})
                continue
            for h in doc_svc.search_document(doc, query, context_chars=context_chars, max_hits=max_results - len(results)):
                results.append({"document": doc["name"] or t.get("name"), "attachment_id": t["attachment_id"],
                                "disclosure_id": t.get("disclosure_id"), "page": h["page"], "text": h["text"],
                                "matched_term": h["matched_term"], "source_url": t.get("source_url")})
        return {"query": query, "count": len(results), "documents_searched": len(targets) - len(skipped),
                "skipped": skipped or None, "source": "kap", "results": results}

    return await _run(go())


# ----------------------------------------------------------------------------- financials (Yahoo, normalised)


async def _statements(ticker: str, frequency: Frequency) -> fin_svc.Statements:
    raw = await run_blocking(market.get_raw_statements, ticker, quarterly=(frequency == "quarterly"), timeout=settings.yahoo_timeout)
    return fin_svc.normalise(ticker.upper(), raw["statements"], frequency, raw.get("currency"))


async def _kap_statements(disclosure_id: int) -> dict[str, Any]:
    client = await get_client()
    detail = await client.get_disclosure_detail(disclosure_id, file_type="data")
    if detail.get("disclosureType") != "FR":
        raise KAPValidationError(f"disclosure {disclosure_id} is a {detail.get('disclosureType')} disclosure, not a financial report (FR)")
    blocks = [b.get("id") for b in detail.get("presentation") or []]
    if not any(str(b).startswith("general_role_") for b in blocks):
        raise KAPValidationError(
            f"disclosure {disclosure_id} is an FR-typed filing without financial statements (templates: {blocks}); "
            "look for FR items whose sub_report_ids start with 'general_role_' (e.g. search_disclosures(disclosure_type='FR', query='general role'))"
        )
    parsed = kapfin_svc.statements_from_disclosure(detail)
    if not parsed["statements"].periods:
        raise KAPValidationError(f"no recognised statement facts in disclosure {disclosure_id} (template {blocks})")
    return parsed


@mcp.tool(title="Financial statements (normalised)", annotations=READ_ONLY)
async def get_financials(
    ticker: Optional[str] = Field(default=None, description="BIST ticker, e.g. THYAO — time series from Yahoo Finance"),
    disclosure_id: Optional[int] = Field(default=None, ge=1, description="A KAP financial-report (FR) disclosure id — the official filing parsed from KAP's XBRL data (current period + prior-year comparatives)"),
    frequency: Frequency = Field(default="quarterly", description="Yahoo series only"),
    periods: int = Field(default=8, ge=1, le=40),
    fields: Optional[list[MetricName]] = Field(default=None, description="Subset of normalised fields; default all (see get_reference_codes.financial_fields)"),
) -> dict[str, Any]:
    """Income statement, balance sheet and cash flow in one normalised schema (revenue,
    gross_profit, operating_profit, ebitda, net_income, total_assets, cash, financial_debt,
    equity, operating/investing/financing cash flow …) with the source line/taxonomy name per
    field and the reporting currency. Two sources: `ticker` → Yahoo Finance time series
    (several periods); `disclosure_id` → the official KAP filing (`source: kap`, exact
    figures, labels '(3M)' quarter vs '(YTD)' cumulative). Find FR ids with
    search_disclosures(tickers=[…], disclosure_type='FR')."""
    if disclosure_id:
        parsed = await _run(_kap_statements(disclosure_id))
        out = fin_svc.statement_rows(parsed["statements"], periods, list(fields) if fields else None)
        out["filing"] = parsed["meta"]
        out["note"] = "KAP comparatives: flows compare with the prior-year same period; balance-sheet items with the prior fiscal year-end (see field_sources '_balance_sheet_date')."
        return _bounded(out)
    if not ticker:
        raise ToolError("Invalid input: ticker or disclosure_id is required")
    st = await _run(_statements(ticker, frequency))
    out = fin_svc.statement_rows(st, periods, list(fields) if fields else None)
    out["official_filings_hint"] = "search_disclosures(tickers=[ticker], disclosure_type='FR', include_details=True) then get_financials(disclosure_id=…)"
    return _bounded(out)


@mcp.tool(title="Metric history (chart-ready)", annotations=READ_ONLY)
async def get_metric_history(
    ticker: str = Field(description="BIST ticker"),
    metrics: list[MetricName] = Field(min_length=1, max_length=10, description="e.g. ['revenue','net_income']"),
    periods: int = Field(default=12, ge=2, le=40),
    frequency: Frequency = Field(default="quarterly"),
) -> dict[str, Any]:
    """Time series of normalised financial metrics, oldest first, ready for tables and charts:
    {series: [{period: '2025Q1', revenue: …, net_income: …}]}. No charts are produced here."""
    st = await _run(_statements(ticker, frequency))
    return _bounded(fin_svc.metric_history(st, list(metrics), periods))


@mcp.tool(title="Financial ratios", annotations=READ_ONLY)
async def get_financial_ratios(
    ticker: Optional[str] = Field(default=None, description="BIST ticker (Yahoo statements)"),
    disclosure_id: Optional[int] = Field(default=None, ge=1, description="KAP FR disclosure id to compute ratios on the official filing (TTM-based ratios need 4 quarters and are reported as unavailable)"),
    period: Optional[str] = Field(default=None, description="'2026Q2' or '2025'; default latest (for a KAP filing: the cumulative current period)"),
    frequency: Frequency = Field(default="quarterly"),
    ratios: Optional[list[RatioName]] = Field(default=None, description="Subset; default all"),
    include_market_multiples: bool = Field(default=True, description="Add P/E, P/B, EV/EBITDA, dividend yield, market cap from Yahoo quote data"),
) -> dict[str, Any]:
    """Deterministic ratios with the formula stated per item: margins, current/cash ratio,
    debt-to-equity, financial debt-to-equity, net debt (/EBITDA), interest coverage, ROA, ROE,
    asset turnover, equity ratio. Flow-based ratios on quarterly data use trailing-12-month sums.
    A ratio whose inputs are missing returns value null with a reason — never a guess."""
    if disclosure_id:
        parsed = await _run(_kap_statements(disclosure_id))
        st = parsed["statements"]
        target = period or next((p.label for p in st.periods if p.sources.get("_kind") == "cumulative"), None)
        out = fin_svc.ratios(st, target, list(ratios) if ratios else None)
        out["filing"] = parsed["meta"]
        ticker = ticker or st.ticker or None
        if not ticker:
            include_market_multiples = False
    else:
        if not ticker:
            raise ToolError("Invalid input: ticker or disclosure_id is required")
        st = await _run(_statements(ticker, frequency))
        out = fin_svc.ratios(st, period, list(ratios) if ratios else None)
    if include_market_multiples and "error" not in out:
        try:
            f = await run_blocking(market.get_fundamentals, ticker, timeout=settings.yahoo_timeout)
            val = f.get("valuation", {})
            out["market_multiples"] = {"source": "yahoo_finance", "as_of": datetime.now().date().isoformat(),
                                       "market_cap": val.get("marketCap"), "enterprise_value": val.get("enterpriseValue"),
                                       "pe_trailing": val.get("trailingPE"), "pe_forward": val.get("forwardPE"),
                                       "price_to_book": val.get("priceToBook"), "ev_to_ebitda": val.get("enterpriseToEbitda"),
                                       "price_to_sales": val.get("priceToSalesTrailing12Months"),
                                       "dividend_yield": f.get("dividends", {}).get("dividendYield"), "beta": val.get("beta")}
        except Exception as e:  # noqa: BLE001
            out["market_multiples"] = {"source": "yahoo_finance", "error": str(e)}
    return out


@mcp.tool(title="Growth (YoY / QoQ)", annotations=READ_ONLY)
async def get_growth(
    metric: MetricName = Field(description="e.g. revenue, net_income, equity"),
    ticker: Optional[str] = Field(default=None, description="BIST ticker (Yahoo series)"),
    disclosure_id: Optional[int] = Field(default=None, ge=1, description="KAP FR disclosure id: YoY of the filing's current period vs its prior-year comparative"),
    periods: int = Field(default=8, ge=1, le=40, description="How many current periods to compute"),
    frequency: Frequency = Field(default="quarterly"),
    comparison: Literal["yoy", "qoq", "auto"] = Field(default="auto", description="auto = yoy (same quarter last year / previous year)"),
) -> dict[str, Any]:
    """Period-over-period change of one metric: current/previous period and value, absolute and
    percentage change (relative to |previous|, so loss→profit swings are signed correctly).
    Computed here, not by the model."""
    if disclosure_id:
        parsed = await _run(_kap_statements(disclosure_id))
        out = fin_svc.growth(parsed["statements"], metric, periods, comparison)
        out["filing"] = parsed["meta"]
        return out
    if not ticker:
        raise ToolError("Invalid input: ticker or disclosure_id is required")
    st = await _run(_statements(ticker, frequency))
    return fin_svc.growth(st, metric, periods, comparison)


@mcp.tool(title="Compare financials", annotations=READ_ONLY)
async def compare_financials(
    tickers: list[str] = Field(min_length=2, max_length=15, description="e.g. ['THYAO','PGSUS']"),
    metrics: list[str] = Field(min_length=1, max_length=15, description="Normalised fields and/or ratio names, e.g. ['revenue','net_income','roe','net_margin']"),
    period: Optional[str] = Field(default=None, description="'2026Q2' or '2025'; default latest available per company"),
    frequency: Frequency = Field(default="quarterly"),
) -> dict[str, Any]:
    """Side-by-side values of metrics and ratios for several companies in one call. Rows are
    returned in the order given, with currency per company — no ranking or judgement."""
    async def go():
        sts = await asyncio.gather(*(_statements(t, frequency) for t in tickers), return_exceptions=True)
        ok, errors = [], []
        for t, s in zip(tickers, sts):
            if isinstance(s, Exception):
                errors.append({"ticker": t.upper(), "error": str(_tool_error(s))})
            else:
                ok.append(s)
        out = fin_svc.compare(ok, metrics, period)
        out["frequency"] = frequency
        if errors:
            out["errors"] = errors
        return out

    return await _run(go())


# ----------------------------------------------------------------------------- corporate actions


@mcp.tool(title="Corporate actions", annotations=READ_ONLY)
async def get_corporate_actions(
    ticker: str = Field(description="BIST ticker"),
    types: Optional[list[ActionType]] = Field(default=None, description="Filter: dividend, capital_increase, capital_decrease, bonus_issue, rights_issue, share_buyback, general_meeting, split"),
    start_date: Optional[str] = Field(default=None, description="YYYY-MM-DD"),
    end_date: Optional[str] = Field(default=None, description="YYYY-MM-DD"),
    limit: int = Field(default=30, ge=1, le=200),
    include_kap: bool = Field(default=True, description="Scan KAP disclosures (needs KAP key; bounded by max_pages)"),
    max_pages: Optional[int] = Field(default=None, ge=1),
    process_ref_ids: Optional[list[int]] = Field(default=None, max_length=100, description="KAP corporate-action process reference ids to get their status"),
) -> dict[str, Any]:
    """Dividends, bonus/rights issues, capital changes, buybacks and general meetings for a company.
    Yahoo supplies exact ex-dates and per-share cash amounts; KAP supplies the disclosures,
    classified by a fixed title keyword table (type 'other' when unmatched). Amounts inside
    KAP bodies are not parsed — read the referenced disclosure. Optionally returns KAP process
    statuses for given reference ids."""
    start = _date(start_date)
    end = _date(end_date, end_of_day=True)
    out: dict[str, Any] = {"ticker": ticker.upper(), "types": types, "start_date": start_date, "end_date": end_date, "items": []}
    years = max(1, (datetime.now().year - start.year + 1) if start else 5)
    try:
        div = await run_blocking(market.get_dividends, ticker, years=years, timeout=settings.yahoo_timeout)
        out["items"].extend(ca_svc.yahoo_actions(div, types, start, end))
    except Exception as e:  # noqa: BLE001
        out["yahoo_error"] = str(e)
    if include_kap:
        try:
            client = await get_client()
            member = await disc_svc.resolve_company(client, ticker=ticker)
            kap = await ca_svc.kap_actions(client, company_id=member["id"], types=types, start=start, end=end, limit=limit,
                                           max_pages=_pages(max_pages, 40))
            out["items"].extend(kap["items"])
            out["kap_scan"] = kap["scan"]
            out["company"] = {"company_id": member["id"], "name": member.get("title")}
        except Exception as e:  # noqa: BLE001
            out["kap_error"] = str(_tool_error(e))
    if process_ref_ids:
        try:
            client = await get_client()
            statuses = await client.get_ca_event_status(process_ref_ids)
            names = REFERENCE_CODES["corporate_action_statuses"]
            out["process_statuses"] = [{"process_ref_id": s.get("refId"), "status": s.get("status"),
                                        "status_text": names.get(s.get("status"), s.get("status")),
                                        "completed_at": s.get("completeDate"), "source": "kap"} for s in statuses]
        except Exception as e:  # noqa: BLE001
            out["process_statuses_error"] = str(_tool_error(e))
    out["items"].sort(key=lambda x: x.get("date") or "", reverse=True)
    out["items"] = out["items"][:limit]
    out["count"] = len(out["items"])
    return _bounded(out)


# ----------------------------------------------------------------------------- news (Yahoo)


@mcp.tool(title="Company news", annotations=READ_ONLY)
async def get_company_news(
    ticker: Optional[str] = Field(default=None, description="BIST ticker (mapped to .IS) or any Yahoo symbol"),
    query: Optional[str] = Field(default=None, description="Free-text search instead of a ticker (English works best, e.g. 'Turkish Airlines')"),
    limit: int = Field(default=10, ge=1, le=50),
    start_date: Optional[str] = Field(default=None, description="YYYY-MM-DD; filters on published_at"),
    end_date: Optional[str] = Field(default=None),
    tab: Literal["news", "press releases", "all"] = Field(default="news"),
) -> dict[str, Any]:
    """News from Yahoo Finance's news module for a ticker or keyword: title, publisher,
    published_at, url, summary, source. No sentiment or interpretation is added. Coverage is
    mostly English wire/press; for Turkish primary sources use search_disclosures."""
    if not ticker and not query:
        raise ToolError("Invalid input: ticker or query is required")
    fetch = max(limit * 2, 20) if (start_date or end_date) else limit
    if ticker:
        items = await _yahoo(market.get_ticker_news, ticker, limit=fetch, tab=tab)
    else:
        items = await _yahoo(market.search_news, query, limit=fetch)
    s, e = _date(start_date), _date(end_date, end_of_day=True)
    out = []
    for n in items:
        pub = (n.get("published") or "")[:19]
        if s and pub and pub < s.isoformat():
            continue
        if e and pub and pub > e.isoformat():
            continue
        out.append({"title": n.get("title"), "publisher": n.get("source"), "published_at": n.get("published"), "url": n.get("url"),
                    "summary": n.get("summary"), "related_tickers": n.get("tickers"), "source": "yahoo_finance"})
    return _bounded({"ticker": ticker.upper() if ticker else None, "symbol": to_yahoo_symbol(ticker) if ticker else None,
                     "query": query, "count": len(out[:limit]), "items": out[:limit]})


# ----------------------------------------------------------------------------- market data (Yahoo)


@mcp.tool(title="Quotes", annotations=READ_ONLY)
async def get_quote(
    symbols: list[str] = Field(min_length=1, max_length=50, description="BIST tickers (THYAO), indices (XU100), FX (USDTRY), commodities (GOLD, BRENT) or full Yahoo symbols"),
) -> dict[str, Any]:
    """Latest price data per symbol: last, previous close, change %, day and 52-week range,
    volume vs 3-month average, market cap, currency. Bad symbols return an error entry
    without failing the batch. Source: Yahoo Finance (delayed for BIST)."""
    return {"source": "yahoo_finance", "as_of": datetime.now().isoformat(timespec="seconds"),
            "items": await _yahoo(market.get_quotes, symbols)}


@mcp.tool(title="Price history", annotations=READ_ONLY)
async def get_price_history(
    symbol: str = Field(description="e.g. THYAO, XU100, USDTRY"),
    period: Period = Field(default="6mo", description="Lookback (ignored when start is given)"),
    interval: Interval = Field(default="1d", description="Intraday intervals only cover recent weeks"),
    start: Optional[str] = Field(default=None, description="YYYY-MM-DD"),
    end: Optional[str] = Field(default=None, description="YYYY-MM-DD, exclusive"),
    max_rows: int = Field(default=250, ge=10, le=2000),
    include_indicators: bool = Field(default=False, description="Add SMA/EMA/RSI/MACD/Bollinger/ATR series and latest values"),
) -> dict[str, Any]:
    """OHLCV candles with a computed summary (return, high/low, max drawdown, annualised
    volatility, average volume) and optional standard technical indicators — all numeric,
    chart-ready, no interpretation."""
    # Indicators need the full window (SMA200 etc.), so fetch everything and trim afterwards.
    hist = await _yahoo(market.get_history, symbol, period=period, interval=interval, start=start, end=end,
                        max_rows=2000 if include_indicators else max_rows)
    hist["source"] = "yahoo_finance"
    if include_indicators:
        ind = ta_svc.indicators(hist["candles"])
        ind["series"] = ind["series"][-max_rows:]
        hist["indicators"] = ind
        if len(hist["candles"]) > max_rows:
            hist["candles"] = hist["candles"][-max_rows:]
            hist["rows"] = len(hist["candles"])
            hist["truncated"] = True
    return _bounded(hist)


@mcp.tool(title="Market overview", annotations=READ_ONLY)
async def get_market_overview() -> dict[str, Any]:
    """Snapshot of BIST 100/30, bank index, USD/TRY, EUR/TRY, gold, Brent, S&P 500, DAX and VIX
    (last, change %, 52-week range). Source: Yahoo Finance."""
    out = await _yahoo(market.market_overview)
    out["source"] = "yahoo_finance"
    return out


@mcp.tool(title="Price reaction around an event", annotations=READ_ONLY)
async def get_price_reaction(
    symbol: Optional[str] = Field(default=None, description="BIST ticker; required unless disclosure_id is given"),
    event_date: Optional[str] = Field(default=None, description="YYYY-MM-DD; required unless disclosure_id is given"),
    disclosure_id: Optional[int] = Field(default=None, ge=1, description="Use a KAP disclosure as the event: ticker and time are read from it (after-close publications map to the next session)"),
    days_before: int = Field(default=5, ge=1, le=30),
    days_after: int = Field(default=5, ge=1, le=60),
) -> dict[str, Any]:
    """Daily closes, returns and volume around an event date, plus excess return versus BIST 100.
    Pure arithmetic on price data; whether the move was 'caused' by the event is not asserted."""
    async def go():
        sym, ev, meta = symbol, event_date, None
        if disclosure_id:
            client = await get_client()
            detail = await client.get_disclosure_detail(disclosure_id, file_type="data")
            nd = disc_svc.normalise_detail(detail)
            sym = nd["ticker"] or (nd["related_tickers"][0] if nd["related_tickers"] else None)
            ts = parse_kap_time(detail.get("time"))
            if not sym:
                raise KAPValidationError("Disclosure has no exchange code (sender is not a listed company)")
            if not ts:
                raise KAPValidationError(f"Could not parse disclosure time {detail.get('time')!r}")
            after_close = ts.hour >= 18
            ev = (ts.date() + timedelta(days=1) if after_close else ts.date()).isoformat()
            meta = {"disclosure_id": disclosure_id, "published_at": nd["published_at"], "after_close": after_close,
                    "company_name": nd["company_name"], "subject": nd["subject"], "source_url": nd["source_url"]}
        if not sym or not ev:
            raise KAPValidationError("symbol and event_date are required unless disclosure_id is given")
        out = await run_blocking(market.price_reaction, sym, ev, days_before=days_before, days_after=days_after, timeout=settings.yahoo_timeout)
        out["source"] = "yahoo_finance"
        out["benchmark"] = "XU100.IS"
        if meta:
            out["disclosure"] = meta
        return out

    return await _run(go())


@mcp.tool(title="Analyst estimates", annotations=READ_ONLY)
async def get_analyst_estimates(ticker: str = Field(description="BIST ticker")) -> dict[str, Any]:
    """Third-party analyst consensus as published on Yahoo Finance: buy/hold/sell counts, mean/
    high/low targets, next earnings date, recent rating changes. Reported as data; the server
    issues no target or recommendation of its own."""
    out = await _yahoo(market.get_analyst_view, ticker)
    out["source"] = "yahoo_finance"
    return out


# ----------------------------------------------------------------------------- timeline


@mcp.tool(title="Company timeline", annotations=READ_ONLY)
async def get_company_timeline(
    ticker: str = Field(description="BIST ticker"),
    start_date: Optional[str] = Field(default=None, description="YYYY-MM-DD; default 90 days ago"),
    end_date: Optional[str] = Field(default=None),
    event_types: Optional[list[Literal["disclosure", "corporate_action", "news"]]] = Field(default=None, description="Default: all"),
    disclosure_type: Optional[DisclosureType] = Field(default=None, description="Restrict KAP items to one type"),
    limit: int = Field(default=100, ge=1, le=500),
    max_pages: Optional[int] = Field(default=None, ge=1, description="KAP scan cap (default 120)"),
) -> dict[str, Any]:
    """Chronological events for a company (newest first): KAP disclosures typed by KAP's own
    metadata (disclosure_type/class, subject), corporate actions (Yahoo ex-dates/splits) and
    Yahoo news. Each event has date, type, title, ids and source_url. No classification or
    summarisation beyond KAP's fields."""
    start = _date(start_date) or (datetime.now() - timedelta(days=90))
    end = _date(end_date, end_of_day=True)
    wanted = set(event_types or ["disclosure", "corporate_action", "news"])
    out: dict[str, Any] = {"ticker": ticker.upper(), "start_date": start.date().isoformat(),
                           "end_date": (end or datetime.now()).date().isoformat(), "events": []}

    async def kap_part():
        client = await get_client()
        member = await disc_svc.resolve_company(client, ticker=ticker)
        res = await disc_svc.search(client, company_ids=[member["id"]], start=start, end=end, limit=limit,
                                    disclosure_type=disclosure_type, max_pages=_pages(max_pages, 120), include_details=True)
        out["company"] = {"company_id": member["id"], "name": member.get("title")}
        out["kap_scan"] = {k: res[k] for k in ("scanned_index_range", "pages_scanned", "scan_complete", "next_cursor")}
        return [{"date": (i.get("published_at") or "")[:16] or None, "type": "disclosure",
                 "disclosure_type": i.get("disclosure_type"), "disclosure_class": i.get("disclosure_class"),
                 "title": i.get("summary") or i.get("subject"), "subject": i.get("subject"), "disclosure_id": i["disclosure_id"],
                 "attachments": len(i.get("attachments") or []), "source": "kap", "source_url": i["source_url"]} for i in res["items"]]

    async def ca_part():
        div = await run_blocking(market.get_dividends, ticker, years=max(1, datetime.now().year - start.year + 1), timeout=settings.yahoo_timeout)
        return [{"date": a["date"], "type": "corporate_action", "action_type": a["type"],
                 "title": f"{a['type']} {a.get('gross_amount_per_share') or a.get('ratio') or ''}".strip(),
                 "value": a.get("gross_amount_per_share") or a.get("ratio"), "source": "yahoo_finance"}
                for a in ca_svc.yahoo_actions(div, None, start, end)]

    async def news_part():
        items = await run_blocking(market.get_ticker_news, ticker, limit=50, tab="all", timeout=settings.yahoo_timeout)
        s_iso, e_iso = start.isoformat(), (end or datetime.now()).isoformat()
        return [{"date": (n.get("published") or "")[:16], "type": "news", "title": n.get("title"), "publisher": n.get("source"),
                 "source": "yahoo_finance", "source_url": n.get("url")}
                for n in items if n.get("published") and s_iso <= n["published"][:19] <= e_iso]

    parts = {"disclosure": kap_part, "corporate_action": ca_part, "news": news_part}
    results = await asyncio.gather(*(parts[k]() for k in parts if k in wanted), return_exceptions=True)
    for name, r in zip([k for k in parts if k in wanted], results):
        if isinstance(r, Exception):
            out[f"{name}_error"] = str(_tool_error(r))
        else:
            out["events"].extend(r)
    out["events"].sort(key=lambda e: e.get("date") or "", reverse=True)
    out["events"] = out["events"][:limit]
    out["count"] = len(out["events"])
    return _bounded(out)


# ----------------------------------------------------------------------------- funds (KAP)


@mcp.tool(title="Search funds", annotations=READ_ONLY)
async def search_funds(
    query: Optional[str] = Field(default=None, description="Substring on fund name / code / founder (Turkish-insensitive)"),
    fund_id: Optional[int] = Field(default=None, ge=1, description="Return the detail record of one fund instead of a list"),
    fund_state: Optional[list[FundState]] = Field(default=None, description="Y active, N passive, T liquidation"),
    fund_type: Optional[list[str]] = Field(default=None, description="e.g. ['EYF','BYF'] — see get_reference_codes.fund_types"),
    fund_class: Optional[list[str]] = Field(default=None),
    limit: int = Field(default=50, ge=1, le=2000),
    offset: int = Field(default=0, ge=0),
) -> dict[str, Any]:
    """Investment funds registered on KAP (identity, type, class, state, founder, KAP URL), or one
    fund's full detail when fund_id is given. Prices/returns are not in KAP — this is the registry."""
    client = await get_client()

    async def go():
        if fund_id:
            return {"fund_id": fund_id, "source": "kap", "detail": await client.get_fund_detail(fund_id)}
        funds = await client.get_funds(fund_state=fund_state, fund_class=fund_class, fund_type=fund_type)
        if query:
            q = normalize_tr(query)
            funds = [f for f in funds if q in normalize_tr(" ".join(str(f.get(k) or "") for k in ("fundName", "fundCode", "fundCompanyTitle")))]
        page = funds[offset: offset + limit]
        return {"total": len(funds), "offset": offset, "count": len(page), "source": "kap", "items": [
            {"fund_id": f.get("fundId"), "name": f.get("fundName"), "code": f.get("fundCode"), "type": f.get("fundType"),
             "class": f.get("fundClass"), "state": f.get("fundState"), "founder_id": f.get("fundCompanyId"),
             "founder": f.get("fundCompanyTitle"), "source_url": f.get("kapUrl")} for f in page]}

    return _bounded(await _run(go()))


# ----------------------------------------------------------------------------- entrypoint


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(prog="kapmcp", description="KAP + Yahoo Finance MCP server for Borsa İstanbul")
    parser.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--path", default="/mcp", help="HTTP path for streamable-http")
    parser.add_argument("--stateless", action="store_true", help="Stateless HTTP sessions (serverless / load-balanced deployments)")
    args = parser.parse_args(argv)

    # stdio owns stdout; everything else goes to stderr.
    logging.basicConfig(level=settings.log_level, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("yfinance", "peewee", "httpx"):
        logging.getLogger(noisy).setLevel(logging.ERROR)
    if not settings.kap_configured:
        log.warning("KAP_API_KEY is not set; KAP tools will report 'not configured' (Yahoo tools still work)")

    try:
        if args.transport == "stdio":
            mcp.run("stdio")
            return
        if args.transport == "streamable-http":
            mcp.run("streamable-http", host=args.host, port=args.port, streamable_http_path=args.path, stateless_http=args.stateless)
        else:
            mcp.run("sse", host=args.host, port=args.port)
    finally:
        asyncio.run(shutdown())


if __name__ == "__main__":
    main()
