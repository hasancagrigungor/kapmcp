"""
Yahoo Finance market data for Borsa İstanbul (via yfinance).

Everything here is synchronous (yfinance is blocking) and meant to be called
through `run_blocking` from the async server. Results are plain dicts/lists so
they serialise straight into MCP tool results. A small TTL cache keeps repeated
quote/info calls from re-hitting Yahoo.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

log = logging.getLogger("kap_mcp.market")

YAHOO_TIMEOUT = 45.0

# Well-known BIST / macro symbols so users can say "XU100" or "USDTRY".
SYMBOL_ALIASES = {
    "XU100": "XU100.IS", "BIST100": "XU100.IS", "BIST 100": "XU100.IS",
    "XU030": "XU030.IS", "BIST30": "XU030.IS", "BIST 30": "XU030.IS",
    "XU050": "XU050.IS", "XBANK": "XBANK.IS", "XUSIN": "XUSIN.IS", "XUTEK": "XUTEK.IS", "XHOLD": "XHOLD.IS",
    "USDTRY": "USDTRY=X", "EURTRY": "EURTRY=X", "GBPTRY": "GBPTRY=X", "EURUSD": "EURUSD=X",
    "GOLD": "GC=F", "XAUUSD": "GC=F", "ALTIN": "GC=F", "BRENT": "BZ=F", "OIL": "CL=F", "PETROL": "BZ=F",
    "BTC": "BTC-USD", "BITCOIN": "BTC-USD",
}

MARKET_OVERVIEW_SYMBOLS = {
    "BIST 100": "XU100.IS", "BIST 30": "XU030.IS", "BIST Banka": "XBANK.IS",
    "USD/TRY": "USDTRY=X", "EUR/TRY": "EURTRY=X", "Altın (ons, USD)": "GC=F", "Brent (USD)": "BZ=F",
    "S&P 500": "^GSPC", "DAX": "^GDAXI", "VIX": "^VIX",
}


def to_yahoo_symbol(code: str) -> str:
    """THYAO -> THYAO.IS; leaves already-qualified Yahoo symbols alone."""
    c = code.strip().upper()
    if not c:
        raise ValueError("empty symbol")
    if c in SYMBOL_ALIASES:
        return SYMBOL_ALIASES[c]
    if "." in c or "=" in c or "^" in c or "-" in c:
        return c
    return f"{c}.IS"


def from_yahoo_symbol(symbol: str) -> str:
    return symbol[:-3] if symbol.upper().endswith(".IS") else symbol


# ----------------------------------------------------------------------------- cache


class _TTLCache:
    def __init__(self) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get_or_set(self, key: str, ttl: float, loader: Callable[[], Any]) -> Any:
        now = time.monotonic()
        with self._lock:
            hit = self._data.get(key)
            if hit and hit[0] > now:
                return hit[1]
        value = loader()
        with self._lock:
            self._data[key] = (now + ttl, value)
        return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


cache = _TTLCache()


def get_raw_statements(symbol: str, quarterly: bool = False) -> dict[str, Any]:
    """All three statements as {stmt: {line: {"YYYY-MM-DD": value}}} plus currency. Cached 1h."""
    sym = to_yahoo_symbol(symbol)

    def load() -> dict[str, Any]:
        t = _yf().Ticker(sym)
        out: dict[str, Any] = {"symbol": sym, "statements": {}}
        for key, (annual_attr, q_attr) in _STATEMENTS.items():
            df = getattr(t, q_attr if quarterly else annual_attr)
            lines: dict[str, dict[str, Any]] = {}
            if df is not None and not df.empty:
                for line, row in df.iterrows():
                    vals = {}
                    for col, val in row.items():
                        v = _num(val)
                        if v is not None:
                            vals[col.strftime("%Y-%m-%d") if hasattr(col, "strftime") else str(col)[:10]] = v
                    if vals:
                        lines[str(line)] = vals
            out["statements"][key] = lines
        if not any(out["statements"].values()):
            raise MarketDataError(f"No financial statements for {sym}")
        try:
            info = t.info or {}
            out["currency"] = info.get("financialCurrency") or info.get("currency")
        except Exception:  # noqa: BLE001
            out["currency"] = None
        return out

    return cache.get_or_set(f"rawstmt:{sym}:{quarterly}", 3600, load)


def get_holders(symbol: str) -> dict[str, Any]:
    """Major/institutional holder breakdown from Yahoo (often sparse for BIST)."""
    sym = to_yahoo_symbol(symbol)

    def load() -> dict[str, Any]:
        t = _yf().Ticker(sym)
        out: dict[str, Any] = {"symbol": sym, "source": "yahoo_finance"}
        try:
            mh = t.major_holders
            out["major_holders"] = [] if mh is None or mh.empty else [
                {"metric": str(i), "value": _num(r.iloc[0])} for i, r in mh.iterrows()
            ]
        except Exception as e:  # noqa: BLE001
            out["major_holders_error"] = str(e)
        try:
            ih = t.institutional_holders
            out["institutional_holders"] = [] if ih is None or ih.empty else [
                {k: _num(v) for k, v in r.items()} for _, r in ih.head(15).iterrows()
            ]
        except Exception as e:  # noqa: BLE001
            out["institutional_holders_error"] = str(e)
        return out

    return cache.get_or_set(f"holders:{sym}", 3600, load)


async def run_blocking(fn: Callable[..., Any], *args: Any, timeout: float = YAHOO_TIMEOUT, **kwargs: Any) -> Any:
    """Run a blocking yfinance call in a worker thread with a timeout."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn, *args, **kwargs), timeout)
    except asyncio.TimeoutError as e:
        raise MarketDataError(f"Yahoo Finance did not respond within {timeout:.0f}s") from e


class MarketDataError(Exception):
    """Yahoo returned nothing usable for a symbol."""


# ----------------------------------------------------------------------------- helpers


def _yf():
    import yfinance as yf

    return yf


def _num(v: Any) -> Any:
    """Make numpy/pandas scalars JSON-safe; NaN -> None."""
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if hasattr(v, "item"):
        try:
            v = v.item()
        except (ValueError, TypeError):
            return str(v)
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, (int, float, str, bool)):
        return v
    return str(v)


def _pct(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b in (None, 0):
        return None
    return round((a / b - 1) * 100, 2)


def _round(v: Any, nd: int = 2) -> Any:
    v = _num(v)
    return round(v, nd) if isinstance(v, float) else v


# ----------------------------------------------------------------------------- quotes


_FAST_INFO_KEYS = (
    "lastPrice", "previousClose", "open", "dayHigh", "dayLow", "lastVolume", "threeMonthAverageVolume",
    "yearHigh", "yearLow", "fiftyDayAverage", "twoHundredDayAverage", "marketCap", "shares", "currency", "exchange",
)


def get_quote(symbol: str) -> dict[str, Any]:
    sym = to_yahoo_symbol(symbol)

    def load() -> dict[str, Any]:
        fi = _yf().Ticker(sym).fast_info
        out: dict[str, Any] = {"symbol": sym, "code": from_yahoo_symbol(sym)}
        for k in _FAST_INFO_KEYS:
            try:
                out[k] = _round(fi[k])
            except (KeyError, AttributeError, TypeError):
                out[k] = None
        if out.get("lastPrice") is None:
            raise MarketDataError(f"No quote for {sym} (unknown symbol or delisted)")
        out["changePct"] = _pct(out.get("lastPrice"), out.get("previousClose"))
        out["change"] = _round((out["lastPrice"] or 0) - (out["previousClose"] or 0))
        out["pctFromYearHigh"] = _pct(out.get("lastPrice"), out.get("yearHigh"))
        out["pctFromYearLow"] = _pct(out.get("lastPrice"), out.get("yearLow"))
        return out

    return cache.get_or_set(f"quote:{sym}", 60, load)


def get_quotes(symbols: list[str]) -> list[dict[str, Any]]:
    out = []
    for s in symbols:
        try:
            out.append(get_quote(s))
        except Exception as e:  # noqa: BLE001 — one bad symbol shouldn't sink the batch
            out.append({"symbol": s, "error": str(e)})
    return out


def market_overview() -> dict[str, Any]:
    rows = []
    for label, sym in MARKET_OVERVIEW_SYMBOLS.items():
        try:
            q = get_quote(sym)
            rows.append({"name": label, "symbol": sym, "last": q["lastPrice"], "changePct": q["changePct"],
                         "yearHigh": q["yearHigh"], "yearLow": q["yearLow"]})
        except Exception as e:  # noqa: BLE001
            rows.append({"name": label, "symbol": sym, "error": str(e)})
    return {"as_of": datetime.now().isoformat(timespec="seconds"), "items": rows}


# ----------------------------------------------------------------------------- history


def get_history(
    symbol: str,
    period: str = "6mo",
    interval: str = "1d",
    start: Optional[str] = None,
    end: Optional[str] = None,
    max_rows: int = 400,
) -> dict[str, Any]:
    sym = to_yahoo_symbol(symbol)
    key = f"hist:{sym}:{period}:{interval}:{start}:{end}"

    def load() -> dict[str, Any]:
        t = _yf().Ticker(sym)
        if start:
            df = t.history(start=start, end=end, interval=interval, auto_adjust=True)
        else:
            df = t.history(period=period, interval=interval, auto_adjust=True)
        if df is None or df.empty:
            raise MarketDataError(f"No price history for {sym}")
        df = df.dropna(subset=["Close"])
        rows = []
        for ts, r in df.iterrows():
            rows.append({
                "date": ts.strftime("%Y-%m-%d") if interval.endswith(("d", "wk", "mo")) else ts.isoformat(),
                "open": _round(r["Open"]), "high": _round(r["High"]), "low": _round(r["Low"]),
                "close": _round(r["Close"]), "volume": _num(r.get("Volume")),
            })
        closes = [r["close"] for r in rows if r["close"] is not None]
        summary: dict[str, Any] = {}
        if len(closes) >= 2:
            rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1]]
            mean = sum(rets) / len(rets)
            var = sum((x - mean) ** 2 for x in rets) / max(len(rets) - 1, 1)
            ann = 252 if interval == "1d" else 52 if interval == "1wk" else 12 if interval == "1mo" else None
            summary = {
                "first_close": closes[0], "last_close": closes[-1],
                "return_pct": _pct(closes[-1], closes[0]),
                "high": max(r["high"] for r in rows if r["high"] is not None),
                "low": min(r["low"] for r in rows if r["low"] is not None),
                "max_drawdown_pct": _max_drawdown(closes),
                "volatility_pct": round(math.sqrt(var) * math.sqrt(ann) * 100, 2) if ann else None,
                "avg_volume": _num(round(sum(r["volume"] or 0 for r in rows) / len(rows))),
            }
        truncated = len(rows) > max_rows
        if truncated:
            rows = rows[-max_rows:]
        return {"symbol": sym, "interval": interval, "rows": len(rows), "truncated": truncated,
                "summary": summary, "candles": rows}

    return cache.get_or_set(key, 300, load)


def _max_drawdown(closes: list[float]) -> Optional[float]:
    peak, mdd = closes[0], 0.0
    for c in closes:
        peak = max(peak, c)
        if peak:
            mdd = min(mdd, c / peak - 1)
    return round(mdd * 100, 2)


def price_reaction(symbol: str, event_date: str, days_before: int = 5, days_after: int = 5) -> dict[str, Any]:
    """Price path around an event date (e.g. a KAP disclosure), relative to the BIST 100."""
    sym = to_yahoo_symbol(symbol)
    ev = datetime.fromisoformat(event_date[:19]).date() if len(event_date) > 10 else date.fromisoformat(event_date)
    start = (ev - timedelta(days=days_before * 2 + 3)).isoformat()
    end = (ev + timedelta(days=days_after * 2 + 3)).isoformat()
    stock = get_history(sym, start=start, end=end, interval="1d")["candles"]
    try:
        index = {r["date"]: r["close"] for r in get_history("XU100.IS", start=start, end=end, interval="1d")["candles"]}
    except Exception:  # noqa: BLE001
        index = {}
    before = [r for r in stock if date.fromisoformat(r["date"]) < ev][-days_before:]
    on_or_after = [r for r in stock if date.fromisoformat(r["date"]) >= ev][: days_after + 1]
    if not before or not on_or_after:
        raise MarketDataError(f"Not enough price data around {ev} for {sym}")
    base = before[-1]["close"]
    idx_base = index.get(before[-1]["date"])
    path = []
    for r in before + on_or_after:
        d = r["date"]
        rel = _pct(r["close"], base)
        idx_rel = _pct(index.get(d), idx_base) if idx_base and index.get(d) else None
        path.append({"date": d, "close": r["close"], "volume": r["volume"], "return_pct": rel,
                     "bist100_return_pct": idx_rel,
                     "excess_return_pct": round(rel - idx_rel, 2) if rel is not None and idx_rel is not None else None})
    first_after = on_or_after[0]
    last_after = on_or_after[-1]
    avg_vol_before = sum(r["volume"] or 0 for r in before) / len(before) if before else None
    return {
        "symbol": sym, "event_date": ev.isoformat(), "base_date": before[-1]["date"], "base_close": base,
        "first_session": {"date": first_after["date"], "return_pct": _pct(first_after["close"], base),
                          "volume_vs_avg": _round(first_after["volume"] / avg_vol_before) if avg_vol_before else None},
        "after_window": {"date": last_after["date"], "return_pct": _pct(last_after["close"], base)},
        "path": path,
    }


# ----------------------------------------------------------------------------- fundamentals


_INFO_GROUPS = {
    "profile": ("longName", "shortName", "sector", "industry", "country", "website", "fullTimeEmployees", "longBusinessSummary"),
    "valuation": ("marketCap", "enterpriseValue", "trailingPE", "forwardPE", "priceToBook", "priceToSalesTrailing12Months",
                  "enterpriseToEbitda", "enterpriseToRevenue", "pegRatio", "beta"),
    "profitability": ("totalRevenue", "revenueGrowth", "grossMargins", "operatingMargins", "ebitdaMargins", "profitMargins",
                      "netIncomeToCommon", "earningsGrowth", "returnOnEquity", "returnOnAssets", "trailingEps", "forwardEps"),
    "balance_sheet": ("totalCash", "totalDebt", "debtToEquity", "currentRatio", "quickRatio", "bookValue", "freeCashflow", "operatingCashflow"),
    "dividends": ("dividendRate", "dividendYield", "payoutRatio", "exDividendDate", "lastDividendValue", "lastDividendDate", "fiveYearAvgDividendYield"),
    "shares": ("sharesOutstanding", "floatShares", "heldPercentInsiders", "heldPercentInstitutions"),
    "analysts": ("recommendationKey", "recommendationMean", "numberOfAnalystOpinions", "targetLowPrice", "targetMeanPrice",
                 "targetMedianPrice", "targetHighPrice"),
}


def get_fundamentals(symbol: str) -> dict[str, Any]:
    sym = to_yahoo_symbol(symbol)

    def load() -> dict[str, Any]:
        info = _yf().Ticker(sym).info or {}
        if not info or info.get("quoteType") is None and info.get("regularMarketPrice") is None:
            raise MarketDataError(f"No fundamentals for {sym}")
        out: dict[str, Any] = {"symbol": sym, "currency": info.get("financialCurrency") or info.get("currency")}
        for group, keys in _INFO_GROUPS.items():
            g = {k: _num(info.get(k)) for k in keys if info.get(k) is not None}
            for k in ("exDividendDate", "lastDividendDate"):
                if isinstance(g.get(k), (int, float)):
                    g[k] = datetime.fromtimestamp(g[k]).date().isoformat()
            if group == "profile" and g.get("longBusinessSummary"):
                g["longBusinessSummary"] = g["longBusinessSummary"][:1500]
            out[group] = g
        return out

    return cache.get_or_set(f"info:{sym}", 900, load)


_STATEMENTS = {
    "income": ("income_stmt", "quarterly_income_stmt"),
    "balance": ("balance_sheet", "quarterly_balance_sheet"),
    "cashflow": ("cashflow", "quarterly_cashflow"),
}

def get_analyst_view(symbol: str) -> dict[str, Any]:
    sym = to_yahoo_symbol(symbol)

    def load() -> dict[str, Any]:
        t = _yf().Ticker(sym)
        out: dict[str, Any] = {"symbol": sym}
        try:
            rs = t.recommendations_summary
            out["recommendations"] = [] if rs is None else [
                {k: _num(v) for k, v in row.items()} for _, row in rs.head(4).iterrows()
            ]
        except Exception as e:  # noqa: BLE001
            out["recommendations_error"] = str(e)
        try:
            cal = t.calendar or {}
            out["calendar"] = {k: (_num(v) if not isinstance(v, list) else [_num(x) for x in v]) for k, v in cal.items()}
        except Exception as e:  # noqa: BLE001
            out["calendar_error"] = str(e)
        try:
            f = get_fundamentals(sym)
            out["targets"] = f.get("analysts", {})
            last = get_quote(sym)["lastPrice"]
            mean = out["targets"].get("targetMeanPrice")
            out["upside_to_mean_target_pct"] = _pct(mean, last) if mean and last else None
        except Exception as e:  # noqa: BLE001
            out["targets_error"] = str(e)
        try:
            ug = t.upgrades_downgrades
            if ug is not None and not ug.empty:
                ug = ug.sort_index(ascending=False).head(10)
                out["recent_rating_changes"] = [
                    {"date": i.strftime("%Y-%m-%d"), **{k: _num(v) for k, v in r.items()}} for i, r in ug.iterrows()
                ]
        except Exception:  # noqa: BLE001
            pass
        return out

    return cache.get_or_set(f"analyst:{sym}", 900, load)


def get_dividends(symbol: str, years: int = 5) -> dict[str, Any]:
    sym = to_yahoo_symbol(symbol)

    def load() -> dict[str, Any]:
        t = _yf().Ticker(sym)
        cutoff = datetime.now().year - years
        divs = t.dividends
        splits = t.splits
        d_rows = [{"date": i.strftime("%Y-%m-%d"), "amount": _round(v, 4)} for i, v in divs.items() if i.year >= cutoff] if divs is not None else []
        s_rows = [{"date": i.strftime("%Y-%m-%d"), "ratio": _round(v, 4)} for i, v in splits.items() if i.year >= cutoff] if splits is not None else []
        by_year: dict[str, float] = {}
        for r in d_rows:
            by_year[r["date"][:4]] = round(by_year.get(r["date"][:4], 0) + r["amount"], 4)
        return {"symbol": sym, "dividends": d_rows, "dividends_by_year": by_year, "splits": s_rows}

    return cache.get_or_set(f"div:{sym}:{years}", 3600, load)


def _news_item(it: dict[str, Any]) -> dict[str, Any]:
    c = it.get("content") or it
    url = (c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url") or it.get("link")
    published = c.get("pubDate") or c.get("displayTime") or it.get("providerPublishTime")
    if isinstance(published, (int, float)):
        published = datetime.utcfromtimestamp(published).isoformat() + "Z"
    tickers = [t.get("symbol") for t in (c.get("finance") or {}).get("stockTickers") or [] if t.get("symbol")]
    return {
        "title": c.get("title") or it.get("title"),
        "source": (c.get("provider") or {}).get("displayName") or it.get("publisher"),
        "published": published,
        "url": url,
        "summary": (c.get("summary") or c.get("description") or "")[:500] or None,
        "type": c.get("contentType"),
        "tickers": tickers or None,
    }


def get_ticker_news(symbol: str, limit: int = 10, tab: str = "news") -> list[dict[str, Any]]:
    """Yahoo news feed for a ticker. tab: 'news' | 'press releases' | 'all'."""
    sym = to_yahoo_symbol(symbol)
    count = max(limit, 10)

    def load() -> list[dict[str, Any]]:
        items = _yf().Ticker(sym).get_news(count=count, tab=tab) or []
        return [_news_item(it) for it in items]

    items = cache.get_or_set(f"ynews:{sym}:{tab}:{count}", 300, load)
    items = sorted(items, key=lambda n: n.get("published") or "", reverse=True)
    return items[:limit]


def search_news(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Free-text news search on Yahoo Finance (English works best, e.g. 'Turkish Airlines')."""
    q = query.strip()

    def load() -> list[dict[str, Any]]:
        res = _yf().Search(q, news_count=max(limit, 8), max_results=0)
        return [_news_item(it) for it in res.news or []]

    return cache.get_or_set(f"newssearch:{q.lower()}:{limit}", 300, load)[:limit]


def search_symbols(query: str, limit: int = 8) -> list[dict[str, Any]]:
    """Yahoo symbol lookup (company name -> ticker), BIST results first."""
    q = query.strip()

    def load() -> list[dict[str, Any]]:
        res = _yf().Search(q, max_results=max(limit, 8), news_count=0)
        rows = []
        for x in res.quotes or []:
            rows.append({"symbol": x.get("symbol"), "name": x.get("longname") or x.get("shortname"),
                         "exchange": x.get("exchDisp") or x.get("exchange"), "type": x.get("quoteType"),
                         "sector": x.get("sectorDisp"), "industry": x.get("industryDisp")})
        rows.sort(key=lambda r: not str(r["symbol"] or "").endswith(".IS"))
        return rows

    return cache.get_or_set(f"symsearch:{q.lower()}", 900, load)[:limit]
