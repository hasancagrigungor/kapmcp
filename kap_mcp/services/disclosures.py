"""
Disclosure search over the KAP VYK API.

The API only offers "50 disclosures starting at index N" with optional
type/class/company filters, so every richer query (by date, by ticker, by
keyword) is a scan over index windows. This module owns that scan, keeps it
bounded (`max_pages`), exposes a cursor for continuation and normalises
records into one shape with a KAP URL and company/ticker attached.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Literal, Optional

from ..client import DISCLOSURE_PAGE_SIZE, MIN_DISCLOSURE_INDEX, KAPClient, normalize_tr, parse_kap_time
from ..exceptions import KAPValidationError
from ..text import clip, flat_lines, html_to_text

KAP_DISCLOSURE_URL = "https://www.kap.org.tr/Bildirim/{index}"
KAP_COMPANY_URL = "https://www.kap.org.tr/tr/sirket-bilgileri/ozet/{member_id}"

Order = Literal["desc", "asc"]
QueryScope = Literal["metadata", "content"]


# ----------------------------------------------------------------------------- company resolution


def split_codes(stock_code: Optional[str]) -> list[str]:
    return [c.strip().upper() for c in (stock_code or "").replace(";", ",").split(",") if c.strip()]


async def member_index(client: KAPClient) -> dict[str, dict[str, Any]]:
    """member_id -> member record (cached list from KAP)."""
    return {m.get("id"): m for m in await client.get_members() if m.get("id")}


async def resolve_company(client: KAPClient, ticker: Optional[str] = None, company_id: Optional[str] = None) -> dict[str, Any]:
    """Return the KAP member for a ticker or id. Raises KAPValidationError with suggestions."""
    if company_id:
        member = (await member_index(client)).get(company_id)
        if not member:
            raise KAPValidationError(f"No KAP member with id {company_id!r}")
        return member
    if not ticker:
        raise KAPValidationError("ticker or company_id is required")
    code = normalize_tr(ticker)
    hits = await client.find_members(ticker, limit=5)
    exact = [h for h in hits if code in [normalize_tr(c) for c in split_codes(h.get("stockCode"))]]
    if exact:
        return exact[0]
    if hits:
        similar = ", ".join(f"{h.get('stockCode') or '-'} ({h.get('title')})" for h in hits)
        raise KAPValidationError(f"No company with ticker {ticker!r}. Similar: {similar}")
    raise KAPValidationError(f"No company found for {ticker!r}")


def primary_ticker(member: dict[str, Any]) -> Optional[str]:
    codes = split_codes(member.get("stockCode"))
    return codes[0] if codes else None


# ----------------------------------------------------------------------------- normalisation


def kap_url(index: int) -> str:
    return KAP_DISCLOSURE_URL.format(index=int(index))


def templates_of(sub_report_ids: Optional[list[str]]) -> list[dict[str, str]]:
    """'oda-12000_New-Business-Relation' -> {id: 'oda-12000', name: 'New Business Relation'}.

    KAP's sub-report ids are the disclosure *template* codes — the closest
    thing the list service offers to a topic. Financial reports use
    'general_role_*' block ids instead.
    """
    out = []
    for sid in sub_report_ids or []:
        sid = str(sid)
        if sid.startswith("general_role_"):
            out.append({"id": sid, "name": "Financial statements (general role " + sid.rsplit("_", 1)[-1] + ")"})
            continue
        code, _, rest = sid.partition("_")
        out.append({"id": code, "name": rest.replace("-", " ").strip() or code})
    return out


def normalise_list_item(item: dict[str, Any], members: dict[str, dict[str, Any]]) -> dict[str, Any]:
    idx = int(item["disclosureIndex"])
    member = members.get(item.get("companyId") or "", {})
    # In the list service `title` is the sender's name, not the subject.
    return {
        "disclosure_id": idx,
        "disclosure_type": item.get("disclosureType"),
        "disclosure_class": item.get("disclosureClass"),
        "company_id": item.get("companyId"),
        "company_name": item.get("title") or member.get("title"),
        "ticker": primary_ticker(member),
        "fund_id": item.get("fundId"),
        "fund_code": item.get("fundCode"),
        "templates": templates_of(item.get("subReportIds")),
        "sub_report_ids": item.get("subReportIds"),
        "source_url": kap_url(idx),
    }


def _tr(x: Any) -> Optional[str]:
    if isinstance(x, dict):
        return x.get("tr") or x.get("en")
    return x


def attachments_of(detail: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for a in detail.get("attachmentUrls") or []:
        url = a.get("url") or ""
        att_id = url.rstrip("/").rsplit("/", 1)[-1] if url else None
        out.append({"attachment_id": att_id, "name": a.get("fileName"), "source_url": url or None})
    return out


def normalise_detail(detail: dict[str, Any], *, text_format: str = "summary", max_chars: int = 30000) -> dict[str, Any]:
    """text_format: summary | text | full."""
    idx = int(detail.get("disclosureIndex") or 0)
    ts = parse_kap_time(detail.get("time"))
    out: dict[str, Any] = {
        "disclosure_id": idx,
        "disclosure_type": detail.get("disclosureType"),
        "disclosure_class": detail.get("disclosureClass"),
        "published_at": ts.isoformat(timespec="minutes") if ts else detail.get("time"),
        "is_update": (detail.get("disclosureReason") or "").upper() == "UPD",
        "company_id": detail.get("senderId"),
        "company_name": detail.get("senderTitle"),
        "ticker": (detail.get("senderExchCodes") or [None])[0],
        "tickers": detail.get("senderExchCodes") or [],
        "on_behalf_of": detail.get("behalfSenderTitle") or detail.get("behalfFundTitle"),
        "subject": _tr(detail.get("subject")),
        "summary": _tr(detail.get("summary")),
        "related_tickers": [s.get("code") for s in detail.get("relatedStocks") or [] if s.get("code")],
        "fiscal_year": detail.get("year"),
        "fiscal_period": _tr(detail.get("period")),
        "consolidation": detail.get("consolidation"),
        "related_disclosure_id": detail.get("relatedDisclosureIndex"),
        "attachments": attachments_of(detail),
        "source_url": detail.get("link") or kap_url(idx),
    }
    if text_format in ("text", "full"):
        parts = []
        body = html_to_text(detail.get("htmlMessage"))
        if body:
            parts.append(body)
        structured = []
        for key in ("presentation", "flatData"):
            if detail.get(key):
                structured.extend(flat_lines(detail[key]))
        if structured:
            parts.append("\n".join(structured))
        text, truncated = clip("\n\n".join(parts), max_chars)
        out["text"] = text or None
        out["text_truncated"] = truncated
    if text_format == "full":
        out["raw"] = {k: v for k, v in detail.items() if k not in ("htmlMessage",)}
    return out


# ----------------------------------------------------------------------------- scan


def keyword_match(text: Optional[str], terms: list[str]) -> bool:
    if not terms:
        return True
    hay = normalize_tr(text or "")
    return any(normalize_tr(t) in hay for t in terms if t.strip())


def _metadata_text(item: dict[str, Any]) -> str:
    """Searchable text available without a detail call: sender name + template names."""
    parts = [item.get("title") or ""]
    parts += [t["name"] for t in templates_of(item.get("subReportIds"))]
    return " ".join(parts)


async def index_range_for_dates(client: KAPClient, start: Optional[datetime], end: Optional[datetime]) -> tuple[int, int]:
    last = await client.get_last_disclosure_index()
    lo = await client.find_index_for_date(start) if start else MIN_DISCLOSURE_INDEX
    hi = (await client.find_index_for_date(end + timedelta(seconds=1)) - 1) if end else last
    return lo, min(hi, last)


async def search(
    client: KAPClient,
    *,
    company_ids: Optional[list[str]] = None,
    terms: Optional[list[str]] = None,
    disclosure_type: Optional[str] = None,
    disclosure_class: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    since_id: Optional[int] = None,
    cursor: Optional[int] = None,
    limit: int = 20,
    order: Order = "desc",
    max_pages: int = 40,
    include_details: bool = False,
    query_scope: QueryScope = "metadata",
    detail_concurrency: int = 4,
) -> dict[str, Any]:
    """Bounded scan of the KAP list service. See `search_disclosures` tool docstring.

    query_scope="metadata": terms match sender name + template names (no extra calls).
    query_scope="content": every scanned candidate's detail is fetched and terms are
    matched against subject + summary too (bounded by max_pages, one call per item).
    """
    last = await client.get_last_disclosure_index()
    if since_id is not None:
        lo, hi = max(since_id + 1, MIN_DISCLOSURE_INDEX), last
    else:
        lo, hi = await index_range_for_dates(client, start, end)
    if cursor is not None:
        if order == "desc":
            hi = min(hi, cursor)
        else:
            lo = max(lo, cursor)

    members = await member_index(client)
    filters = [{"company_id": cid} for cid in (company_ids or [None])]
    collected: dict[int, dict[str, Any]] = {}
    pages = 0
    exhausted = False

    detail_cache: dict[int, dict[str, Any]] = {}
    sem = asyncio.Semaphore(detail_concurrency)
    content_mode = bool(terms) and query_scope == "content"

    async def detail_of(idx: int) -> Optional[dict[str, Any]]:
        if idx in detail_cache:
            return detail_cache[idx]
        async with sem:
            try:
                d = await client.get_disclosure_detail(idx, file_type="data")
            except Exception as e:  # noqa: BLE001
                d = {"_error": str(e)}
        detail_cache[idx] = d
        return d

    async def matches(item: dict[str, Any]) -> bool:
        if not terms:
            return True
        if keyword_match(_metadata_text(item), terms):
            return True
        if not content_mode:
            return False
        d = await detail_of(int(item.get("disclosureIndex") or 0))
        if not d or "_error" in d:
            return False
        nd = normalise_detail(d)
        return keyword_match(" ".join(x for x in (nd.get("subject"), nd.get("summary")) if x), terms)

    async def fetch(start_idx: int, f: dict[str, Any]) -> list[dict[str, Any]]:
        page = await client.get_disclosures(
            disclosure_index=start_idx, disclosure_type=disclosure_type, disclosure_class=disclosure_class, company_id=f["company_id"]
        )
        flags = await asyncio.gather(*(matches(it) for it in page))
        return [it for it, ok in zip(page, flags) if ok]

    if order == "desc":
        high = hi
        while len(collected) < limit and pages < max_pages and high >= lo:
            win_lo = max(high - DISCLOSURE_PAGE_SIZE + 1, lo)
            for f in filters:
                for item in await fetch(win_lo, f):
                    idx = int(item.get("disclosureIndex") or 0)
                    if win_lo <= idx <= high:
                        collected[idx] = item
                pages += 1
            high = win_lo - 1
        exhausted = high < lo
        next_cursor = None if exhausted else high
    else:
        low = lo
        while len(collected) < limit and pages < max_pages and low <= hi:
            win_hi = min(low + DISCLOSURE_PAGE_SIZE - 1, hi)
            for f in filters:
                for item in await fetch(low, f):
                    idx = int(item.get("disclosureIndex") or 0)
                    if low <= idx <= win_hi:
                        collected[idx] = item
                pages += 1
            low = win_hi + 1
        exhausted = low > hi
        next_cursor = None if exhausted else low

    ordered_ids = sorted(collected, reverse=(order == "desc"))
    if len(ordered_ids) > limit:
        # More matches than asked for: continue from the last returned one.
        boundary = ordered_ids[limit - 1]
        next_cursor = boundary - 1 if order == "desc" else boundary + 1
        exhausted = False
        ordered_ids = ordered_ids[:limit]
    items = [normalise_list_item(collected[i], members) for i in ordered_ids]

    if (include_details or content_mode) and items:

        async def enrich(rec: dict[str, Any]) -> None:
            d = await detail_of(rec["disclosure_id"])
            if not d or "_error" in d:  # one blocked/missing detail must not fail the list
                rec["detail_error"] = (d or {}).get("_error", "detail unavailable")
                return
            nd = normalise_detail(d, text_format="summary")
            for k in ("published_at", "is_update", "subject", "summary", "fiscal_year", "fiscal_period", "consolidation",
                      "attachments", "related_tickers"):
                rec[k] = nd.get(k)
            rec["ticker"] = rec.get("ticker") or nd.get("ticker")

        await asyncio.gather(*(enrich(r) for r in items))

    return {
        "count": len(items),
        "order": order,
        "scanned_index_range": [lo, hi],
        "last_disclosure_id": last,
        "pages_scanned": pages,
        "scan_complete": exhausted,
        "next_cursor": next_cursor,
        "query_scope": query_scope if terms else None,
        "items": items,
    }


def parse_date(value: Optional[str], end_of_day: bool = False) -> Optional[datetime]:
    if value is None or not str(value).strip():
        return None
    ts = parse_kap_time(value)
    if ts is None:
        raise KAPValidationError(f"Unrecognised date {value!r}. Use YYYY-MM-DD or DD.MM.YYYY, optionally with HH:MM.")
    if end_of_day and len(str(value).strip()) <= 10:
        ts = ts.replace(hour=23, minute=59, second=59)
    return ts
