"""In-memory stand-in for the yfinance module used by the market layer."""

from datetime import datetime

import pandas as pd


class FakeTicker:
    calls: list[str] = []
    PRICES = {"THYAO.IS": 280.0, "XU100.IS": 13000.0, "USDTRY=X": 48.0, "PGSUS.IS": 200.0}

    def __init__(self, symbol):
        self.symbol = symbol
        FakeTicker.calls.append(symbol)
        self._price = self.PRICES.get(symbol)

    @property
    def fast_info(self):
        if self._price is None:
            return {}
        return dict(lastPrice=self._price, previousClose=self._price * 0.98, marketCap=1e11, currency="TRY",
                    yearHigh=self._price * 1.3, yearLow=self._price * 0.8, lastVolume=1_000_000, threeMonthAverageVolume=900_000)

    def history(self, period=None, interval="1d", start=None, end=None, auto_adjust=True):
        if self._price is None:
            return pd.DataFrame()
        if start:
            days = pd.date_range(start, end or datetime(2026, 9, 21), freq="B")
        else:
            days = pd.date_range(end=datetime(2026, 9, 21), periods=30, freq="B")
        closes, p = [], self._price
        for d in days:
            p *= 0.95 if (self.symbol == "THYAO.IS" and d.date() == datetime(2026, 9, 15).date()) else 1.001
            closes.append(round(p, 2))
        return pd.DataFrame({"Open": closes, "High": [c * 1.01 for c in closes], "Low": [c * 0.99 for c in closes],
                             "Close": closes, "Volume": [1_000_000] * len(days)}, index=days)

    @property
    def info(self):
        if self._price is None:
            return {}
        return {"longName": "Türk Hava Yollari", "sector": "Industrials", "forwardPE": 3.9, "marketCap": 1e11,
                "totalRevenue": 2.6e10, "dividendYield": 2.2, "targetMeanPrice": 460.0, "recommendationKey": "buy",
                "exDividendDate": 1756771200, "quoteType": "EQUITY", "financialCurrency": "TRY"}

    def _stmt(self, lines):
        if self._price is None:
            return pd.DataFrame()
        cols = [pd.Timestamp(d) for d in ("2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30")]
        return pd.DataFrame({c: [row[i] for row in lines.values()] for i, c in enumerate(cols)}, index=list(lines))

    @property
    def quarterly_income_stmt(self):
        return self._stmt({"Total Revenue": [120.0, 100.0, 110.0, 105.0, 100.0], "Net Income": [12.0, 8.0, 10.0, 9.0, 5.0],
                           "Operating Income": [12.0, 10.0, 11.0, 10.5, 10.0], "Gross Profit": [36.0, 30.0, 33.0, 31.5, 30.0]})

    @property
    def quarterly_balance_sheet(self):
        return self._stmt({"Total Assets": [1000.0, 950.0, 900.0, 880.0, 850.0], "Stockholders Equity": [400.0, 390.0, 380.0, 370.0, 360.0],
                           "Total Liabilities Net Minority Interest": [600.0, 560.0, 520.0, 510.0, 490.0],
                           "Current Assets": [400.0, 380.0, 360.0, 352.0, 340.0], "Current Liabilities": [300.0, 280.0, 260.0, 255.0, 245.0],
                           "Total Debt": [360.0, 336.0, 312.0, 306.0, 294.0], "Cash And Cash Equivalents": [100.0, 95.0, 90.0, 88.0, 85.0]})

    @property
    def quarterly_cashflow(self):
        return self._stmt({"Operating Cash Flow": [14.4, 9.6, 12.0, 10.8, 6.0], "Capital Expenditure": [-6.0, -4.0, -5.0, -4.5, -2.5]})

    income_stmt, balance_sheet, cashflow = quarterly_income_stmt, quarterly_balance_sheet, quarterly_cashflow

    @property
    def recommendations_summary(self):
        return pd.DataFrame([{"period": "0m", "strongBuy": 3, "buy": 8, "hold": 2, "sell": 0, "strongSell": 0}])

    calendar = {"Earnings Date": [datetime(2026, 11, 5).date()]}
    upgrades_downgrades = None

    @property
    def major_holders(self):
        return pd.DataFrame({"Value": [0.49]}, index=["insidersPercentHeld"])

    institutional_holders = None

    @property
    def dividends(self):
        return pd.Series([6.884, 1.0], index=[pd.Timestamp("2025-09-02"), pd.Timestamp("2024-09-02")])

    @property
    def splits(self):
        return pd.Series([], dtype=float)

    def get_news(self, count=10, tab="news"):
        return [
            {"content": {"title": "Old story", "pubDate": "2026-08-01T10:00:00Z", "provider": {"displayName": "Reuters"},
                         "canonicalUrl": {"url": "https://x/old"}, "summary": "s", "contentType": "STORY"}},
            {"content": {"title": "Boeing deal", "pubDate": "2026-09-15T17:14:42Z", "provider": {"displayName": "Reuters"},
                         "canonicalUrl": {"url": "https://x/boeing"}, "summary": "s", "contentType": "STORY",
                         "finance": {"stockTickers": [{"symbol": "THYAO.IS"}]}}},
        ][:count]


class FakeSearch:
    def __init__(self, query, **kw):
        self.news = [{"title": f"news about {query}", "publisher": "AP", "providerPublishTime": 1757000000, "link": "https://x/n"}]
        self.quotes = [{"symbol": "THYAO.IS", "longname": "Türk Hava Yollari", "exchDisp": "Istanbul", "quoteType": "EQUITY"}]


class FakeYF:
    Ticker = FakeTicker
    Search = FakeSearch
