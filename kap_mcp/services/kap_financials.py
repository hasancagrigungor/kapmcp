"""
Parse an official KAP financial report (disclosureType FR) into the normalised
statement schema used by `financials.py`.

KAP delivers FR disclosures as an XBRL-like `presentation` tree: report blocks
(`general_role_210015` balance sheet, `..._310003` income statement,
`..._520003` cash flow, `..._610000` equity movements) whose leaves carry a
taxonomy `name` (IFRS-derived, e.g. `Revenue`, `Assets`) and values per
context. Contexts: CURR = current period (cumulative year-to-date for flows),
CURR3 = last three months, PREV / PREV3 = same periods a year earlier.

Only taxonomy names that exist in the tree are used; when a field is absent
(banks, insurers and funds use different templates) it stays null.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

from .financials import Period, Statements, apply_derived

# Normalised field -> ordered taxonomy candidates. "+"-joined names are summed
# (all present parts; at least one required); a leading "-" negates.
KAP_FIELD_MAP: dict[str, tuple[str, ...]] = {
    "revenue": ("Revenue", "RevenueFromContractsWithCustomers", "InterestIncome"),
    "cost_of_revenue": ("CostOfSales",),
    "gross_profit": ("GrossProfit", "GrossProfitLossFromCommercialOperations"),
    "operating_profit": ("ProfitLossFromOperatingActivities",),
    "depreciation": ("AdjustmentsForDepreciationAndAmortisationExpense", "DepreciationAndAmortisationExpense"),
    "interest_expense": ("FinanceCosts",),
    "pretax_income": ("ProfitLossBeforeTax",),
    "tax_expense": ("IncomeTaxExpenseContinuingOperations",),
    "net_income": ("ProfitLossAttributableToOwnersOfParent", "ProfitLoss"),
    "eps_diluted": ("DilutedEarningsLossPerShare", "BasicEarningsLossPerShare"),
    "total_assets": ("Assets",),
    "current_assets": ("CurrentAssets",),
    "cash": ("CashAndCashEquivalents",),
    "inventory": ("Inventories",),
    "receivables": ("CurrentTradeReceivables", "TradeAndOtherCurrentReceivables"),
    "total_liabilities": ("Liabilities",),
    "current_liabilities": ("CurrentLiabilities",),
    "financial_debt": ("CurrentBorowings+CurrentBorrowings+CurrentPortionOfNoncurrentBorrowings+LongtermBorrowings+CurrentLeaseLiabilities+NoncurrentLeaseLiabilities",),
    "equity": ("EquityAttributableToOwnersOfParent", "Equity"),
    "shares_outstanding": ("IssuedCapital",),  # nominal 1 TRY per share on BIST; flagged in sources
    "operating_cash_flow": ("CashFlowsFromUsedInOperatingActivities",),
    "investing_cash_flow": ("CashFlowsFromUsedInInvestingActivities",),
    "financing_cash_flow": ("CashFlowsFromUsedInFinancingActivities",),
    "capex": ("-PurchaseOfPropertyPlantEquipmentAndIntangibleAssetsClassifiedAsInvestingActivities",),
    "dividends_paid": ("-DividendsPaidClassifiedAsFinancingActivities", "-DividendsPaid"),
}

# "6 Aylık" -> quarter index within the fiscal year
PERIOD_TR_TO_QUARTER = {"3 Aylık": 1, "6 Aylık": 2, "9 Aylık": 3, "Yıllık": 4, "12 Aylık": 4}


def _iter_items(item: Any):
    if isinstance(item, list):
        for i in item:
            yield from _iter_items(i)
    elif isinstance(item, dict):
        yield item
        yield from _iter_items(item.get("ReportItem"))


def extract_facts(presentation: list[dict[str, Any]]) -> dict[str, Any]:
    """Return {"facts": {name: {contextKey: value}}, "labels": {name: tr_label}, "contexts": {key: {...}}, "currency"}."""
    facts: dict[str, dict[str, float]] = {}
    labels: dict[str, str] = {}
    contexts: dict[str, dict[str, Any]] = {}
    currency = None
    for block in presentation or []:
        content = block.get("content") or {}
        ctx_list = (content.get("ContextList") or {}).get("Context") or []
        ctx_list = ctx_list if isinstance(ctx_list, list) else [ctx_list]
        id_to_key = {}
        for c in ctx_list:
            key = c.get("key")
            period = c.get("Period") or {}
            id_to_key[c.get("id")] = key
            # Balance-sheet blocks use instants, flow blocks use durations, under the same key (CURR/PREV):
            # keep both so period ends can be resolved per statement kind.
            slot = contexts.setdefault(key, {})
            if "instant" in period:
                slot.setdefault("instant", period["instant"])
            else:
                slot.setdefault("startDate", period.get("startDate"))
                slot.setdefault("endDate", period.get("endDate"))
        for item in _iter_items(content.get("ReportItem")):
            vals = item.get("Values")
            name = item.get("name")
            if not vals or not name:
                continue
            v = vals.get("Value")
            for x in (v if isinstance(v, list) else [v]):
                if not isinstance(x, dict) or x.get("value") in (None, ""):
                    continue
                try:
                    num = float(str(x["value"]).replace(",", "."))
                except ValueError:
                    continue
                key = id_to_key.get(x.get("contextId"), x.get("contextId"))
                facts.setdefault(name, {})
                # First occurrence per (name, context) wins; equity-movement blocks reuse names like "Equity".
                facts[name].setdefault(key, num)
                currency = currency or x.get("currency")
            tr = next((l.get("value") for l in (item.get("langs") or {}).get("lang", []) if l.get("code") == "tr"), None)
            if tr and name not in labels:
                labels[name] = tr
    return {"facts": facts, "labels": labels, "contexts": contexts, "currency": currency}


def _resolve(facts: dict[str, dict[str, float]], spec: str, ctx: str) -> tuple[Optional[float], Optional[str]]:
    """Evaluate one candidate spec ('A+B', '-A') for a context key."""
    total = 0.0
    used = []
    for part in spec.split("+"):
        neg = part.startswith("-")
        name = part.lstrip("-")
        val = facts.get(name, {}).get(ctx)
        if val is None:
            continue
        total += -val if neg else val
        used.append(part)
    if not used:
        return None, None
    return total, "+".join(used)


def _values_for(facts: dict[str, dict[str, float]], flow_ctx: str, stock_ctx: str) -> tuple[dict[str, Optional[float]], dict[str, str]]:
    from .financials import FLOW_FIELDS

    values: dict[str, Optional[float]] = {}
    sources: dict[str, str] = {}
    for field_name, candidates in KAP_FIELD_MAP.items():
        ctx = flow_ctx if field_name in FLOW_FIELDS else stock_ctx
        for spec in candidates:
            val, used = _resolve(facts, spec, ctx)
            if val is not None:
                values[field_name] = val
                sources[field_name] = f"kap:{used}@{ctx}"
                break
        values.setdefault(field_name, None)
    if values.get("shares_outstanding") is not None:
        sources["shares_outstanding"] = sources["shares_outstanding"] + " (IssuedCapital, nominal TRY — not a share count)"
    return values, sources


def _period_end(contexts: dict[str, dict[str, Any]], key: str, kind: str = "flow") -> Optional[date]:
    c = contexts.get(key) or {}
    raw = (c.get("endDate") or c.get("instant")) if kind == "flow" else (c.get("instant") or c.get("endDate"))
    return date.fromisoformat(raw[:10]) if raw else None


def statements_from_disclosure(detail: dict[str, Any], ticker: Optional[str] = None) -> dict[str, Any]:
    """Normalise one FR disclosure. Returns {statements: Statements, meta: {...}}.

    Periods produced: current (CURR, year-to-date flows), current quarter
    (CURR3 when present) and the prior-year comparatives (PREV, PREV3).
    """
    ex = extract_facts(detail.get("presentation") or [])
    facts, contexts = ex["facts"], ex["contexts"]
    if not facts:
        raise ValueError("disclosure has no XBRL presentation data (not a financial report, or a template without facts)")
    year = detail.get("year")
    period_tr = ((detail.get("period") or {}).get("tr") if isinstance(detail.get("period"), dict) else detail.get("period")) or ""
    q = PERIOD_TR_TO_QUARTER.get(period_tr.strip())
    periods: list[Period] = []

    def add(label: str, flow_ctx: str, stock_ctx: str, kind: str) -> None:
        end = _period_end(contexts, flow_ctx, "flow") or _period_end(contexts, stock_ctx, "stock")
        if end is None:
            return
        values, sources = _values_for(facts, flow_ctx, stock_ctx)
        if not any(v is not None for v in values.values()):
            return
        p = Period(label=label, end_date=end, values=values, sources=sources)
        apply_derived(p)
        p.sources["_kind"] = kind
        bs = _period_end(contexts, stock_ctx, "stock")
        if bs and bs != end:
            p.sources["_balance_sheet_date"] = bs.isoformat()  # KAP comparatives: prior year-end balance vs prior-period flows
        periods.append(p)

    cur_label = f"{year}Q{q}" if year and q else (f"{year}" if year else "CURR")
    if "CURR3" in contexts:
        add(cur_label + " (3M)", "CURR3", "CURR", "quarter")
    add(cur_label + (" (YTD)" if q and q != 1 and "CURR3" in contexts else ""), "CURR", "CURR", "cumulative")
    if "PREV3" in contexts:
        add(f"{int(year) - 1}Q{q} (3M)" if year and q else "PREV3", "PREV3", "PREV", "quarter")
    if "PREV" in contexts:
        add(f"{int(year) - 1}Q{q}" + (" (YTD)" if q and q != 1 and "PREV3" in contexts else "") if year and q else "PREV", "PREV", "PREV", "cumulative")

    st = Statements(ticker=(ticker or (detail.get("senderExchCodes") or [None])[0] or "").upper(), frequency="quarterly",
                    currency=ex["currency"], periods=periods, source="kap")
    meta = {
        "disclosure_id": int(detail.get("disclosureIndex") or 0),
        "company_name": detail.get("senderTitle"),
        "fiscal_year": year, "fiscal_period": period_tr or None, "consolidation": detail.get("consolidation"),
        "contexts": contexts, "fact_count": sum(len(v) for v in facts.values()),
        "report_blocks": [b.get("id") for b in detail.get("presentation") or []],
        "source_url": detail.get("link"),
    }
    return {"statements": st, "meta": meta, "labels": ex["labels"], "facts": facts}
