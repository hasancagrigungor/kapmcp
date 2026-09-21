"""
Deterministic financial computations over normalised statements.

Everything here is pure: input is a `Statements` object (already fetched and
cached elsewhere), output is plain dicts. No network, no LLM, no judgement.
Every ratio states its formula and returns `value: None` with a `reason` when
an input is missing instead of guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal, Optional

Frequency = Literal["annual", "quarterly"]

# Normalised field -> ordered list of source line names (first present wins).
# Source lines are Yahoo Finance's standardised statement labels.
NORMALISED_FIELDS: dict[str, tuple[str, ...]] = {
    "revenue": ("Total Revenue", "Operating Revenue"),
    "cost_of_revenue": ("Cost Of Revenue", "Reconciled Cost Of Revenue"),
    "gross_profit": ("Gross Profit",),
    "operating_profit": ("Operating Income", "Total Operating Income As Reported"),
    "ebitda": ("EBITDA", "Normalized EBITDA"),
    "depreciation": ("Reconciled Depreciation", "Depreciation And Amortization"),
    "interest_expense": ("Interest Expense", "Interest Expense Non Operating"),
    "pretax_income": ("Pretax Income",),
    "tax_expense": ("Tax Provision",),
    "net_income": ("Net Income Common Stockholders", "Net Income", "Net Income From Continuing Operation Net Minority Interest"),
    "eps_diluted": ("Diluted EPS",),
    "total_assets": ("Total Assets",),
    "current_assets": ("Current Assets",),
    "cash": ("Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"),
    "inventory": ("Inventory",),
    "receivables": ("Receivables", "Accounts Receivable"),
    "total_liabilities": ("Total Liabilities Net Minority Interest",),
    "current_liabilities": ("Current Liabilities",),
    "financial_debt": ("Total Debt",),
    "net_debt": ("Net Debt",),
    "equity": ("Stockholders Equity", "Total Equity Gross Minority Interest", "Common Stock Equity"),
    "shares_outstanding": ("Ordinary Shares Number", "Share Issued"),
    "operating_cash_flow": ("Operating Cash Flow", "Cash Flow From Continuing Operating Activities"),
    "investing_cash_flow": ("Investing Cash Flow", "Cash Flow From Continuing Investing Activities"),
    "financing_cash_flow": ("Financing Cash Flow", "Cash Flow From Continuing Financing Activities"),
    "capex": ("Capital Expenditure",),
    "free_cash_flow": ("Free Cash Flow",),
    "dividends_paid": ("Cash Dividends Paid", "Common Stock Dividend Paid"),
}

FLOW_FIELDS = frozenset({
    "revenue", "cost_of_revenue", "gross_profit", "operating_profit", "ebitda", "depreciation", "interest_expense",
    "pretax_income", "tax_expense", "net_income", "eps_diluted", "operating_cash_flow", "investing_cash_flow",
    "financing_cash_flow", "capex", "free_cash_flow", "dividends_paid",
})
STOCK_FIELDS = frozenset(NORMALISED_FIELDS) - FLOW_FIELDS

RATIO_NAMES = (
    "gross_margin", "operating_margin", "ebitda_margin", "net_margin", "current_ratio", "cash_ratio",
    "debt_to_equity", "financial_debt_to_equity", "net_debt", "net_debt_to_ebitda", "interest_coverage",
    "roa", "roe", "asset_turnover", "equity_ratio",
)


@dataclass
class Period:
    label: str            # "2025" or "2026Q2"
    end_date: date
    values: dict[str, Optional[float]] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)   # normalised field -> source line used


@dataclass
class Statements:
    ticker: str
    frequency: Frequency
    currency: Optional[str]
    periods: list[Period]     # newest first
    source: str = "yahoo_finance"

    def period(self, label: Optional[str]) -> Optional[Period]:
        if label is None:
            return self.periods[0] if self.periods else None
        for p in self.periods:
            if p.label == label:
                return p
        return None

    def index(self, label: str) -> Optional[int]:
        for i, p in enumerate(self.periods):
            if p.label == label:
                return i
        return None


# ----------------------------------------------------------------------------- normalisation


def period_label(end: date, frequency: Frequency) -> str:
    if frequency == "annual":
        return str(end.year)
    return f"{end.year}Q{(end.month - 1) // 3 + 1}"


def normalise(
    ticker: str,
    raw: dict[str, dict[str, dict[str, Any]]],
    frequency: Frequency,
    currency: Optional[str],
) -> Statements:
    """raw = {"income": {line: {"YYYY-MM-DD": value}}, "balance": {...}, "cashflow": {...}}."""
    dates: set[str] = set()
    for stmt in raw.values():
        for line_vals in stmt.values():
            dates.update(line_vals.keys())
    periods: list[Period] = []
    for d in sorted(dates, reverse=True):
        end = date.fromisoformat(d[:10])
        p = Period(label=period_label(end, frequency), end_date=end)
        for fld, candidates in NORMALISED_FIELDS.items():
            for stmt in raw.values():
                found = False
                for line in candidates:
                    val = (stmt.get(line) or {}).get(d)
                    if val is not None:
                        p.values[fld] = float(val)
                        p.sources[fld] = line
                        found = True
                        break
                if found:
                    break
            p.values.setdefault(fld, None)
        apply_derived(p)
        periods.append(p)
    # Drop periods that carry no data at all (Yahoo sometimes pads an empty column).
    periods = [p for p in periods if any(x is not None for x in p.values.values())]
    return Statements(ticker=ticker, frequency=frequency, currency=currency, periods=periods)


def apply_derived(p: Period) -> None:
    """Fill derived fields only from present inputs; make sure every normalised key exists."""
    for fld in NORMALISED_FIELDS:
        p.values.setdefault(fld, None)
    v = p.values
    if v["gross_profit"] is None and v["revenue"] is not None and v["cost_of_revenue"] is not None:
        v["gross_profit"] = v["revenue"] - v["cost_of_revenue"]
        p.sources["gross_profit"] = "computed: revenue - cost_of_revenue"
    if v["ebitda"] is None and v["operating_profit"] is not None and v["depreciation"] is not None:
        v["ebitda"] = v["operating_profit"] + v["depreciation"]
        p.sources["ebitda"] = "computed: operating_profit + depreciation"
    if v["net_debt"] is None and v["financial_debt"] is not None and v["cash"] is not None:
        v["net_debt"] = v["financial_debt"] - v["cash"]
        p.sources["net_debt"] = "computed: financial_debt - cash"
    if v["free_cash_flow"] is None and v["operating_cash_flow"] is not None and v["capex"] is not None:
        v["free_cash_flow"] = v["operating_cash_flow"] + v["capex"]  # capex is negative
        p.sources["free_cash_flow"] = "computed: operating_cash_flow + capex"


# ----------------------------------------------------------------------------- helpers


def _div(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or b == 0:
        return None
    return a / b


def ttm(stmts: Statements, field_name: str, at: int = 0) -> tuple[Optional[float], Optional[str]]:
    """Trailing-twelve-month sum of a flow field starting at period index `at`.

    Annual statements return the period value itself. Quarterly needs four
    consecutive quarters; otherwise (None, reason).
    """
    if stmts.frequency == "annual":
        p = stmts.periods[at] if at < len(stmts.periods) else None
        return (p.values.get(field_name), None) if p else (None, "period unavailable")
    window = stmts.periods[at: at + 4]
    if len(window) < 4:
        return None, f"TTM {field_name} needs 4 quarters, have {len(window)}"
    vals = [p.values.get(field_name) for p in window]
    if any(v is None for v in vals):
        return None, f"{field_name} missing in one of the last 4 quarters"
    return sum(vals), None  # type: ignore[arg-type]


def _ratio(name: str, value: Optional[float], formula: str, reason: Optional[str], pct: bool = False,
           unit: Optional[str] = None) -> dict[str, Any]:
    out: dict[str, Any] = {"metric": name, "value": None if value is None else round(value * (100 if pct else 1), 4),
                           "unit": unit or ("percent" if pct else "ratio"), "formula": formula}
    if value is None:
        out["reason"] = reason or "required input unavailable"
    return out


# ----------------------------------------------------------------------------- public computations


def statement_rows(stmts: Statements, periods: int, fields: Optional[list[str]] = None) -> dict[str, Any]:
    wanted = fields or list(NORMALISED_FIELDS)
    unknown = [f for f in wanted if f not in NORMALISED_FIELDS]
    rows = []
    for p in stmts.periods[:periods]:
        rows.append({"period": p.label, "period_end": p.end_date.isoformat(),
                     **{f: p.values.get(f) for f in wanted if f in NORMALISED_FIELDS}})
    sources = {}
    for p in stmts.periods[:periods]:
        for f, src in p.sources.items():
            if f in wanted:
                sources.setdefault(f, src)
    return {"ticker": stmts.ticker, "frequency": stmts.frequency, "currency": stmts.currency, "source": stmts.source,
            "fields": [f for f in wanted if f in NORMALISED_FIELDS], "unknown_fields": unknown or None,
            "periods": rows, "field_sources": sources}


def metric_history(stmts: Statements, metrics: list[str], periods: int) -> dict[str, Any]:
    known = [m for m in metrics if m in NORMALISED_FIELDS]
    series = [{"period": p.label, "period_end": p.end_date.isoformat(), **{m: p.values.get(m) for m in known}}
              for p in reversed(stmts.periods[:periods])]  # oldest first: chart-ready
    return {"ticker": stmts.ticker, "frequency": stmts.frequency, "currency": stmts.currency, "source": stmts.source,
            "metrics": known, "unknown_metrics": [m for m in metrics if m not in NORMALISED_FIELDS] or None,
            "series": series}


def ratios(stmts: Statements, period: Optional[str] = None, names: Optional[list[str]] = None) -> dict[str, Any]:
    p = stmts.period(period)
    if p is None:
        return {"ticker": stmts.ticker, "period": period, "error": f"period {period!r} not available",
                "available_periods": [x.label for x in stmts.periods]}
    i = stmts.index(p.label) or 0
    v = p.values
    ni, ni_reason = ttm(stmts, "net_income", i)
    rev, rev_reason = ttm(stmts, "revenue", i)
    ebitda_ttm, ebitda_reason = ttm(stmts, "ebitda", i)
    opi, opi_reason = ttm(stmts, "operating_profit", i)
    intexp, intexp_reason = ttm(stmts, "interest_expense", i)

    def need(*fields: str) -> Optional[str]:
        missing = [f for f in fields if v.get(f) is None]
        return f"{', '.join(missing)} unavailable" if missing else None

    all_ratios = {
        "gross_margin": _ratio("gross_margin", _div(v["gross_profit"], v["revenue"]), "gross_profit / revenue", need("gross_profit", "revenue"), pct=True),
        "operating_margin": _ratio("operating_margin", _div(v["operating_profit"], v["revenue"]), "operating_profit / revenue", need("operating_profit", "revenue"), pct=True),
        "ebitda_margin": _ratio("ebitda_margin", _div(v["ebitda"], v["revenue"]), "ebitda / revenue", need("ebitda", "revenue"), pct=True),
        "net_margin": _ratio("net_margin", _div(v["net_income"], v["revenue"]), "net_income / revenue", need("net_income", "revenue"), pct=True),
        "current_ratio": _ratio("current_ratio", _div(v["current_assets"], v["current_liabilities"]), "current_assets / current_liabilities", need("current_assets", "current_liabilities")),
        "cash_ratio": _ratio("cash_ratio", _div(v["cash"], v["current_liabilities"]), "cash / current_liabilities", need("cash", "current_liabilities")),
        "debt_to_equity": _ratio("debt_to_equity", _div(v["total_liabilities"], v["equity"]), "total_liabilities / equity", need("total_liabilities", "equity")),
        "financial_debt_to_equity": _ratio("financial_debt_to_equity", _div(v["financial_debt"], v["equity"]), "financial_debt / equity", need("financial_debt", "equity")),
        "net_debt": _ratio("net_debt", v["net_debt"], "financial_debt - cash", need("net_debt"), unit="currency"),
        "net_debt_to_ebitda": _ratio("net_debt_to_ebitda", _div(v["net_debt"], ebitda_ttm), "net_debt / ebitda (TTM)", need("net_debt") or ebitda_reason),
        "interest_coverage": _ratio("interest_coverage", _div(opi, intexp), "operating_profit (TTM) / interest_expense (TTM)", opi_reason or intexp_reason),
        "roa": _ratio("roa", _div(ni, v["total_assets"]), "net_income (TTM) / total_assets", ni_reason or need("total_assets"), pct=True),
        "roe": _ratio("roe", _div(ni, v["equity"]), "net_income (TTM) / equity", ni_reason or need("equity"), pct=True),
        "asset_turnover": _ratio("asset_turnover", _div(rev, v["total_assets"]), "revenue (TTM) / total_assets", rev_reason or need("total_assets")),
        "equity_ratio": _ratio("equity_ratio", _div(v["equity"], v["total_assets"]), "equity / total_assets", need("equity", "total_assets"), pct=True),
    }
    selected = [n for n in (names or RATIO_NAMES) if n in all_ratios]
    return {"ticker": stmts.ticker, "period": p.label, "period_end": p.end_date.isoformat(), "frequency": stmts.frequency,
            "currency": stmts.currency, "source": stmts.source,
            "ratios": [all_ratios[n] for n in selected],
            "unknown_ratios": [n for n in (names or []) if n not in all_ratios] or None}


def growth(stmts: Statements, metric: str, periods: int, comparison: Literal["yoy", "qoq", "auto"] = "auto") -> dict[str, Any]:
    if metric not in NORMALISED_FIELDS:
        return {"ticker": stmts.ticker, "metric": metric, "error": "unknown metric", "known_metrics": list(NORMALISED_FIELDS)}
    if comparison == "auto":
        comparison = "yoy"
    if stmts.frequency == "annual" and comparison == "qoq":
        return {"ticker": stmts.ticker, "metric": metric, "error": "qoq requires quarterly statements"}
    lag = 1 if (stmts.frequency == "annual" or comparison == "qoq") else 4
    rows = []
    for i, p in enumerate(stmts.periods[:periods]):
        if stmts.source == "kap":
            # A single KAP filing carries current + prior-year comparatives of the same kind (3M / YTD):
            # the comparison partner is the next period with the same kind. QoQ is not derivable from one filing.
            if comparison == "qoq":
                return {"ticker": stmts.ticker, "metric": metric, "error": "qoq is not available from a single KAP filing; use the Yahoo series (ticker)"}
            prev = next((x for x in stmts.periods[i + 1:] if x.sources.get("_kind") == p.sources.get("_kind")), None)
        else:
            j = i + lag
            prev = stmts.periods[j] if j < len(stmts.periods) else None
        cur_v = p.values.get(metric)
        prev_v = prev.values.get(metric) if prev else None
        row: dict[str, Any] = {"current_period": p.label, "previous_period": prev.label if prev else None,
                               "current_value": cur_v, "previous_value": prev_v, "comparison_type": comparison,
                               "absolute_change": None, "percentage_change": None}
        if cur_v is not None and prev_v is not None:
            row["absolute_change"] = cur_v - prev_v
            if prev_v != 0:
                # Sign-aware: a swing from loss to profit is reported relative to |previous|.
                row["percentage_change"] = round((cur_v - prev_v) / abs(prev_v) * 100, 2)
            else:
                row["reason"] = "previous value is zero"
        elif prev is None:
            row["reason"] = "no earlier period available"
        else:
            row["reason"] = f"{metric} unavailable for one of the periods"
        rows.append(row)
    return {"ticker": stmts.ticker, "metric": metric, "frequency": stmts.frequency, "currency": stmts.currency,
            "source": stmts.source, "comparison_type": comparison, "rows": rows}


def compare(all_stmts: list[Statements], metrics: list[str], period: Optional[str]) -> dict[str, Any]:
    known = [m for m in metrics if m in NORMALISED_FIELDS or m in RATIO_NAMES]
    rows = []
    for s in all_stmts:
        p = s.period(period)
        row: dict[str, Any] = {"ticker": s.ticker, "period": p.label if p else None, "currency": s.currency}
        if p is None:
            row["error"] = f"period {period!r} not available"
            row["available_periods"] = [x.label for x in s.periods][:8]
        else:
            r = None
            for m in known:
                if m in NORMALISED_FIELDS:
                    row[m] = p.values.get(m)
                else:
                    r = r or {x["metric"]: x for x in ratios(s, p.label)["ratios"]}
                    row[m] = r[m]["value"]
        rows.append(row)
    return {"period": period or "latest", "metrics": known,
            "unknown_metrics": [m for m in metrics if m not in known] or None, "source": "yahoo_finance",
            "note": "Values are as reported per company; currencies may differ. No ranking is applied.",
            "rows": rows}
