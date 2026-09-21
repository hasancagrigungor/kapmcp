"""
Company profile assembly: KAP member record + KAP member detail + KAP securities,
optionally Yahoo profile/holders.

KAP's `memberDetail` is a list of `{key, nameTr, nameEn, value, publishDateTime}`
records with stable keys (`kpy41_acc5_sermayede_dogrudan` = direct shareholders,
`kpy41_acc3_endeksler` = indices, ...). The map below covers the keys observed
on the live gateway; every record is also returned under `kap_detail_fields`
keyed by its KAP key so nothing is hidden. Values KAP leaves empty are null.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from ..client import KAPClient
from .disclosures import resolve_company, split_codes

# KAP key -> (normalised name, kind). kind: text | number | list | shareholders | indices
KEY_MAP: dict[str, tuple[str, str]] = {
    "kpy41_acc2_sektor": ("sector", "text"),
    "kpy41_acc2_faaliyet_konu": ("business_scope", "text"),
    "kpy41_acc2_sure": ("company_duration", "text"),
    "kpy41_acc2_bdk": ("auditor", "text"),
    "kpy41_acc3_sermaye_arac_pazar": ("market", "text"),
    "kpy41_acc3_endeksler": ("indices", "indices"),
    "kpy41_acc3_abcd": ("abcd_group", "text"),
    "kpy41_acc3_son_durum_borsa_piyasalar": ("other_exchanges", "text"),
    "kpy41_acc3_ortaklik_hakki_vermeyen": ("non_equity_instruments", "list"),
    "kpy41_acc5_odenmis_sermaye": ("paid_in_capital", "number"),
    "kpy41_acc5_kayitli_sermaye_tavani": ("registered_capital_ceiling", "number"),
    "kpy41_acc5_sermayede_dogrudan": ("shareholders", "shareholders"),
    "kpy41_acc5_son_durum_sermayeye": ("indirect_shareholders", "list"),
    "kpy41_acc5_fiili_dolasimdaki_pay": ("free_float", "list"),
    "kpy41_acc5_sermayeyi_temsil_eden": ("share_classes", "list"),
    "kpy41_acc6_yonetim_kurulu_uyeleri": ("board_members", "list"),
    "kpy41_acc6_yonetimde_soz_sahibi": ("executives", "list"),
    "kpy41_acc7_bagli_ortakliklar": ("subsidiaries", "list"),
    "kpy41_acc1_int_addres": ("website", "text"),
    "kpy41_acc1_eposta": ("email", "text"),
    "kpy41_acc1_merkez_adresi": ("head_office_address", "text"),
    "kpy41_acc1_ilet_adres_tel_fax": ("contact", "text"),
    "kpy41_acc1_uretim_adres": ("production_facilities", "text"),
    "kpy41_acc1_yatirimci_iliskileri": ("investor_relations", "list"),
    "kpy41_acc4_ticaret_sicil_memurlugu": ("trade_registry_office", "text"),
    "kpy41_acc4_ticaret_sicil_numarasi": ("trade_registry_number", "text"),
    "kpy41_acc4_tescil_tarihi": ("registration_date", "text"),
    "kpy41_acc4_vergi_no": ("tax_number", "text"),
    "kpy41_acc4_vergi_dairesi": ("tax_office", "text"),
    "kpy41_acc10_diger_hususlar": ("other_matters", "text"),
}

PROFILE_SECTIONS = ("sector", "market", "indices", "paid_in_capital", "registered_capital_ceiling", "shareholders",
                    "board_members", "executives", "subsidiaries", "auditor", "website")


def parse_tr_number(value: Any) -> Optional[float]:
    """'1.380.000.000' -> 1380000000.0 ; '49,12' -> 49.12 ; None/'-' -> None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s or s == "-":
        return None
    s = re.sub(r"[^\d,.\-]", "", s)
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif s.count(".") > 1 or (s.count(".") == 1 and len(s.split(".")[1]) == 3):
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def _shareholders(value: Any) -> Optional[list[dict[str, Any]]]:
    if not isinstance(value, list):
        return None
    out = []
    for row in value:
        if not isinstance(row, dict):
            continue
        name = row.get("shareholder") or row.get("ortak")
        out.append({
            "shareholder": name,
            "share_in_capital": parse_tr_number(row.get("shareInCapital")),
            "ratio_in_capital_pct": parse_tr_number(row.get("ratioInCapital")),
            "voting_right_ratio_pct": parse_tr_number(row.get("votingRightRatio")),
            "is_total": (name or "").strip().upper() == "TOPLAM",
        })
    return out


def _indices(value: Any) -> Optional[list[str]]:
    if not value or not isinstance(value, str):
        return None
    return [x.strip() for x in value.split("/") if x.strip()]


def _empty(value: Any) -> bool:
    return value in (None, "", "-", [], {})


def normalise_member_detail(detail: Any) -> dict[str, Any]:
    """Return {"profile": {normalised: value}, "fields": {kap_key: {...}}}."""
    profile: dict[str, Any] = {}
    fields: dict[str, Any] = {}
    records = detail if isinstance(detail, list) else []
    for rec in records:
        if not isinstance(rec, dict) or not rec.get("key"):
            continue
        key = rec["key"]
        value = rec.get("value")
        fields[key] = {"name_tr": rec.get("nameTr"), "name_en": rec.get("nameEn"), "value": value,
                       "published_at": rec.get("publishDateTime")}
        if key not in KEY_MAP:
            continue
        name, kind = KEY_MAP[key]
        if _empty(value):
            profile.setdefault(name, None)
            continue
        if kind == "number":
            profile[name] = parse_tr_number(value)
        elif kind == "shareholders":
            profile[name] = _shareholders(value)
        elif kind == "indices":
            profile[name] = _indices(value)
        else:
            profile[name] = value
    for name, _ in KEY_MAP.values():
        profile.setdefault(name, None)
    return {"profile": profile, "fields": fields}


def _securities(sec_row: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not sec_row:
        return {"paid_in_capital": None, "registered_capital_ceiling": None, "capital_system": None, "securities": [],
                "not_available": "no securities record on KAP for this member"}
    member = sec_row.get("member") or {}
    securities = sec_row.get("securities") or []
    paid = sum((s.get("currentCapital") or 0) for s in securities) if securities else None
    return {
        "paid_in_capital": paid,
        "registered_capital_ceiling": member.get("kayitliSermayeTavani"),
        "registered_capital_valid_until": member.get("kstSonGecerlilikTarihi"),
        "capital_system": member.get("sermayeSistemi"),
        "mkk_member_id": member.get("mksMbrId"),
        "currency": "TRY",
        "securities": [
            {"isin": s.get("isin"), "description": s.get("isinDesc"), "exchange_code": s.get("borsaKodu"),
             "settlement_code": s.get("takasKodu"), "series": s.get("tertipGroup"), "group": s.get("groupCode"),
             "group_description": s.get("groupCodeDesc"), "capital": s.get("capital"), "current_capital": s.get("currentCapital"),
             "traded_on_exchange": s.get("borsadaIslemeAcik")}
            for s in securities
        ],
    }


async def get_company(client: KAPClient, *, ticker: Optional[str], company_id: Optional[str]) -> dict[str, Any]:
    member = await resolve_company(client, ticker=ticker, company_id=company_id)
    mid = member["id"]
    detail_error = None
    detail: Any = None
    try:
        detail = await client.get_member_detail(mid)
    except Exception as e:  # noqa: BLE001 — profile is still useful without the detail record
        detail_error = str(e)
    nd = normalise_member_detail(detail)
    prof = nd["profile"]
    sec_rows = [r for r in await client.get_member_securities() if (r.get("member") or {}).get("id") == mid]
    sec = _securities(sec_rows[0] if sec_rows else None)
    codes = split_codes(member.get("stockCode"))

    out: dict[str, Any] = {
        "company_id": mid,
        "name": member.get("title"),
        "ticker": codes[0] if codes else None,
        "tickers": codes,
        "member_type": member.get("memberType"),
        "is_listed": member.get("memberType") == "IGS",
        "sector": prof.get("sector"),
        "market": prof.get("market"),
        "indices": prof.get("indices"),
        "abcd_group": prof.get("abcd_group"),
        "capital": {
            "paid_in_capital": prof.get("paid_in_capital") if prof.get("paid_in_capital") is not None else sec["paid_in_capital"],
            "registered_capital_ceiling": prof.get("registered_capital_ceiling") if prof.get("registered_capital_ceiling") is not None else sec["registered_capital_ceiling"],
            "registered_capital_valid_until": sec.get("registered_capital_valid_until"),
            "capital_system": sec.get("capital_system"),
            "currency": "TRY",
            "securities": sec["securities"],
        },
        "shareholders": prof.get("shareholders"),
        "indirect_shareholders": prof.get("indirect_shareholders"),
        "free_float": prof.get("free_float"),
        "board_members": prof.get("board_members"),
        "executives": prof.get("executives"),
        "subsidiaries": prof.get("subsidiaries"),
        "auditor": prof.get("auditor"),
        "business_scope": prof.get("business_scope"),
        "website": prof.get("website"),
        "head_office_address": prof.get("head_office_address"),
        "registry": {k: prof.get(k) for k in ("trade_registry_office", "trade_registry_number", "registration_date", "tax_number", "tax_office")},
        "source": "kap",
        "source_url": member.get("kfifUrl") or f"https://www.kap.org.tr/tr/sirket-bilgileri/ozet/{mid}",
        "sources": {"member": "GET /api/vyk/members", "detail": "GET /api/vyk/memberDetail/{id}", "securities": "GET /api/vyk/memberSecurities"},
        "kap_detail_fields": nd["fields"] or None,
    }
    missing = [k for k in PROFILE_SECTIONS if out.get(k) is None and out["capital"].get(k) is None]
    if missing or detail_error:
        out["not_available"] = {k: "KAP has no value for this field (empty in memberDetail)" for k in missing}
        if detail_error:
            out["not_available"]["kap_detail_error"] = detail_error
    return out
