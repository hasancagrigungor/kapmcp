"""
Corporate actions from two sources, kept separate and labelled:

- Yahoo Finance: cash dividends (ex-date, per-share amount) and splits/bonus
  ratios — machine-readable, so structured fields are exact.
- KAP: corporate-action ("CA") and material-event disclosures whose *subject
  and summary* (from the detail record) are classified by a fixed keyword table
  below — the list `title` is only the sender name. The
  structured dividend fields (record/payment date, gross/net) live inside the
  disclosure body whose schema is not documented, so they are NOT parsed here —
  the disclosure id and URL are returned for the agent to read.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from ..client import KAPClient, normalize_tr
from .disclosures import search

ACTION_TYPES = ("dividend", "capital_increase", "capital_decrease", "capital_change", "bonus_issue", "rights_issue",
                "share_buyback", "general_meeting", "split", "other")

# Ordered: first match wins. Patterns are compared against normalize_tr(title).
_TITLE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # KAP's generic capital form title names both directions; keep it distinct rather than guessing.
    ("capital_change", ("ARTIRIMI - AZALTIMI", "ARTIRIM - AZALTIM", "ARTIRIMI/AZALTIMI")),
    ("bonus_issue", ("BEDELSIZ",)),
    ("rights_issue", ("BEDELLI", "RUCHAN")),
    ("capital_decrease", ("SERMAYE AZALT",)),
    ("capital_increase", ("SERMAYE ARTIR", "SERMAYE ARTTIR", "KAYITLI SERMAYE TAVANI")),
    ("dividend", ("KAR PAYI", "TEMETTU", "KAR DAGITIM")),
    ("share_buyback", ("GERI ALIM", "PAY GERI", "PAYLARIN GERI")),
    ("general_meeting", ("GENEL KURUL",)),
)


def classify_title(title: Optional[str]) -> str:
    t = normalize_tr(title or "")
    for action, patterns in _TITLE_RULES:
        if any(p in t for p in patterns):
            return action
    return "other"


def yahoo_actions(div: dict[str, Any], types: Optional[list[str]], start: Optional[datetime], end: Optional[datetime]) -> list[dict[str, Any]]:
    out = []
    for d in div.get("dividends", []):
        if (not types or "dividend" in types) and _in_range(d["date"], start, end):
            out.append({"type": "dividend", "date": d["date"], "date_kind": "ex_dividend_date",
                        "gross_amount_per_share": d["amount"], "currency": "TRY", "source": "yahoo_finance",
                        "note": "Yahoo reports the cash amount per share on the ex-date; net amount and payment date are on the KAP disclosure."})
    for s in div.get("splits", []):
        if (not types or "split" in types or "bonus_issue" in types) and _in_range(s["date"], start, end):
            out.append({"type": "split", "date": s["date"], "ratio": s["ratio"], "source": "yahoo_finance",
                        "note": "On BIST a 'split' usually reflects a bonus issue (bedelsiz); confirm with the KAP disclosure."})
    return out


def _in_range(d: str, start: Optional[datetime], end: Optional[datetime]) -> bool:
    if start and d < start.date().isoformat():
        return False
    if end and d > end.date().isoformat():
        return False
    return True


async def kap_actions(
    client: KAPClient, *, company_id: str, types: Optional[list[str]], start: Optional[datetime], end: Optional[datetime],
    limit: int, max_pages: int,
) -> dict[str, Any]:
    res = await search(client, company_ids=[company_id], start=start, end=end, limit=limit, max_pages=max_pages,
                       include_details=True)
    items = []
    for it in res["items"]:
        action = classify_title(" ".join(x for x in (it.get("subject"), it.get("summary")) if x) or it.get("title"))
        # Keep every CA-typed disclosure (even unmatched -> "other"); other types only when the title matches.
        if it.get("disclosure_type") != "CA" and action == "other":
            continue
        if types and action not in types:
            continue
        items.append({
            "type": action, "date": (it.get("published_at") or "")[:10] or None, "date_kind": "kap_publication_date",
            "title": it.get("title"), "subject": it.get("subject"), "summary": it.get("summary"),
            "disclosure_id": it["disclosure_id"], "disclosure_type": it.get("disclosure_type"),
            "source": "kap", "source_url": it["source_url"],
            "structured_fields": None,
            "note": "Decision/record/payment dates and gross/net amounts are inside the disclosure body: read it with get_disclosure.",
        })
    return {"items": items, "scan": {k: res[k] for k in ("scanned_index_range", "pages_scanned", "scan_complete", "next_cursor")}}
