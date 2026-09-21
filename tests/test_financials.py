"""Pure, deterministic financial computations — no network, no MCP."""

from kap_mcp.services import financials as F
from kap_mcp.services import technicals as T
from kap_mcp.services.corporate_actions import classify_title


def _raw(quarters):
    """Build raw statements for a list of (date, revenue, net_income, assets, equity, liabilities)."""
    inc, bal, cf = {"Total Revenue": {}, "Net Income": {}, "Operating Income": {}, "Gross Profit": {}}, \
        {"Total Assets": {}, "Stockholders Equity": {}, "Total Liabilities Net Minority Interest": {}, "Current Assets": {}, "Current Liabilities": {}, "Total Debt": {}, "Cash And Cash Equivalents": {}}, \
        {"Operating Cash Flow": {}, "Capital Expenditure": {}}
    for d, rev, ni, assets, eq, liab in quarters:
        inc["Total Revenue"][d] = rev
        inc["Net Income"][d] = ni
        inc["Operating Income"][d] = rev * 0.1
        inc["Gross Profit"][d] = rev * 0.3
        bal["Total Assets"][d] = assets
        bal["Stockholders Equity"][d] = eq
        bal["Total Liabilities Net Minority Interest"][d] = liab
        bal["Current Assets"][d] = assets * 0.4
        bal["Current Liabilities"][d] = liab * 0.5
        bal["Total Debt"][d] = liab * 0.6
        bal["Cash And Cash Equivalents"][d] = assets * 0.1
        cf["Operating Cash Flow"][d] = ni * 1.2
        cf["Capital Expenditure"][d] = -ni * 0.5
    return {"income": inc, "balance": bal, "cashflow": cf}


Q = [("2026-06-30", 120, 12, 1000, 400, 600), ("2026-03-31", 100, 8, 950, 390, 560), ("2025-12-31", 110, 10, 900, 380, 520),
     ("2025-09-30", 105, 9, 880, 370, 510), ("2025-06-30", 100, 5, 850, 360, 490)]


def test_normalise_labels_sources_and_derived_fields():
    st = F.normalise("THYAO", _raw(Q), "quarterly", "TRY")
    assert [p.label for p in st.periods] == ["2026Q2", "2026Q1", "2025Q4", "2025Q3", "2025Q2"]
    p = st.periods[0]
    assert p.values["revenue"] == 120 and p.sources["revenue"] == "Total Revenue"
    assert p.values["net_debt"] == 600 * 0.6 - 100 and p.sources["net_debt"].startswith("computed")
    assert p.values["free_cash_flow"] == 12 * 1.2 - 6 and p.values["ebitda"] is None  # no depreciation -> not derived
    annual = F.normalise("X", _raw([("2025-12-31", 1, 1, 1, 1, 1)]), "annual", None)
    assert annual.periods[0].label == "2025"


def test_statement_rows_and_history_are_chart_ready():
    st = F.normalise("THYAO", _raw(Q), "quarterly", "TRY")
    rows = F.statement_rows(st, 2, ["revenue", "net_income", "bogus"])
    assert rows["unknown_fields"] == ["bogus"] and rows["periods"][0] == {"period": "2026Q2", "period_end": "2026-06-30", "revenue": 120, "net_income": 12}
    hist = F.metric_history(st, ["revenue"], 3)
    assert [s["period"] for s in hist["series"]] == ["2025Q4", "2026Q1", "2026Q2"]  # oldest first


def test_ratios_use_ttm_and_report_missing_inputs():
    st = F.normalise("THYAO", _raw(Q), "quarterly", "TRY")
    r = {x["metric"]: x for x in F.ratios(st)["ratios"]}
    assert r["net_margin"]["value"] == 10.0 and r["net_margin"]["formula"] == "net_income / revenue"
    ttm_ni = 12 + 8 + 10 + 9
    assert r["roe"]["value"] == round(ttm_ni / 400 * 100, 4)
    assert r["current_ratio"]["value"] == round(400 / 300, 4)
    assert r["ebitda_margin"]["value"] is None and "ebitda" in r["ebitda_margin"]["reason"]
    # Only 1 quarter -> TTM impossible, must not fake ROE
    short = F.normalise("X", _raw(Q[:1]), "quarterly", "TRY")
    roe = {x["metric"]: x for x in F.ratios(short)["ratios"]}["roe"]
    assert roe["value"] is None and "4 quarters" in roe["reason"]
    assert F.ratios(st, "1999Q1")["error"].startswith("period")


def test_growth_yoy_qoq_and_sign_handling():
    st = F.normalise("THYAO", _raw(Q), "quarterly", "TRY")
    yoy = F.growth(st, "revenue", 1)["rows"][0]
    assert yoy["previous_period"] == "2025Q2" and yoy["percentage_change"] == 20.0 and yoy["absolute_change"] == 20
    qoq = F.growth(st, "net_income", 1, "qoq")["rows"][0]
    assert qoq["previous_period"] == "2026Q1" and qoq["percentage_change"] == 50.0
    last = F.growth(st, "revenue", 5)["rows"][-1]
    assert last["percentage_change"] is None and last["reason"] == "no earlier period available"
    neg = F.normalise("N", _raw([("2026-03-31", 10, 5, 1, 1, 1), ("2025-12-31", 10, -5, 1, 1, 1)]), "quarterly", None)
    row = F.growth(neg, "net_income", 1, "qoq")["rows"][0]
    assert row["percentage_change"] == 200.0  # -5 -> +5 relative to |prev|
    assert F.growth(F.normalise("A", _raw(Q), "annual", None), "revenue", 1, "qoq")["error"]


def test_compare_keeps_order_no_ranking():
    a = F.normalise("AAA", _raw(Q), "quarterly", "TRY")
    b = F.normalise("BBB", _raw(Q[:2]), "quarterly", "USD")
    out = F.compare([a, b], ["revenue", "roe", "nope"], "2026Q2")
    assert [r["ticker"] for r in out["rows"]] == ["AAA", "BBB"] and out["unknown_metrics"] == ["nope"]
    assert out["rows"][0]["revenue"] == 120 and out["rows"][0]["roe"] is not None
    assert out["rows"][1]["roe"] is None  # BBB lacks 4 quarters
    assert "ranking" not in out and F.compare([a], ["revenue"], "1999Q1")["rows"][0]["error"]


def test_technical_indicators():
    closes = [100 + i for i in range(60)]
    candles = [{"date": f"d{i}", "close": c, "high": c + 1, "low": c - 1} for i, c in enumerate(closes)]
    ind = T.indicators(candles)
    latest = ind["latest"]
    assert latest["sma20"] == sum(closes[-20:]) / 20 and latest["sma200"] is None
    assert latest["rsi14"] == 100.0  # monotonic rise
    assert latest["macd"] > 0 and latest["bb_upper"] > latest["bb_middle"] > latest["bb_lower"]
    assert latest["atr14"] == 2.0
    assert T.indicators(candles[:1])["reason"]


def test_corporate_action_title_rules():
    assert classify_title("Kar Payı Dağıtım İşlemlerine İlişkin Bildirim") == "dividend"
    assert classify_title("Bedelsiz Pay Alma Hakkı Kullanımına İlişkin Bildirim") == "bonus_issue"
    assert classify_title("BEDELLİ SERMAYE ARTIRIMI") == "rights_issue"
    assert classify_title("Sermaye Artırımı - Azaltımı İşlemlerine İlişkin Bildirim") == "capital_change"
    assert classify_title("Sermaye Azaltımı Kararı") == "capital_decrease"
    assert classify_title("Payların Geri Alınmasına İlişkin Bildirim") == "share_buyback"
    assert classify_title("Genel Kurul İşlemlerine İlişkin Bildirim") == "general_meeting"
    assert classify_title("Özel Durum Açıklaması (Genel)") == "other"


def test_kap_filing_parses_to_normalised_schema():
    import json
    from pathlib import Path
    from kap_mcp.services import kap_financials as K

    detail = json.loads(Path(__file__).with_name("data").joinpath("kap_fr_disclosure.json").read_text())
    parsed = K.statements_from_disclosure(detail, "TSPOR")
    st = parsed["statements"]
    assert st.source == "kap" and st.currency == "TRY" and parsed["meta"]["fiscal_period"] == "6 Aylık"
    labels = [p.label for p in st.periods]
    assert labels == ["2023Q2 (3M)", "2023Q2 (YTD)", "2022Q2 (3M)", "2022Q2 (YTD)"]
    ytd = st.period("2023Q2 (YTD)")
    assert ytd.values["revenue"] == 661324931.0 and ytd.values["net_income"] == -853804666.0
    assert ytd.values["total_assets"] == 3388325305.0 and ytd.values["equity"] == -1405422474.0
    assert ytd.values["financial_debt"] == 24670121.0 + 96949353.0 + 1071089626.0
    assert ytd.values["capex"] == -1009948790.0 and ytd.values["operating_cash_flow"] == 389864596.0
    assert ytd.values["ebitda"] == ytd.values["operating_profit"] + 153730212.0  # derived from KAP depreciation adjustment
    assert ytd.sources["revenue"] == "kap:Revenue@CURR" and "IssuedCapital" in ytd.sources["shares_outstanding"]
    q = st.period("2023Q2 (3M)")
    assert q.values["revenue"] == 332200424.0 and q.values["operating_cash_flow"] is None  # cash flow has no 3M context
    prev = st.period("2022Q2 (YTD)")
    assert prev.sources["_balance_sheet_date"] == "2023-05-31"  # prior fiscal year-end balance vs prior-period flows
    r = {x["metric"]: x for x in F.ratios(st, "2023Q2 (YTD)")["ratios"]}
    assert r["gross_margin"]["value"] == round(-550232698.0 / 661324931.0 * 100, 4)
    assert r["roe"]["value"] is None and "4 quarters" in r["roe"]["reason"]
    g = F.growth(st, "revenue", 1, "yoy")["rows"][0]
    assert g["previous_period"] == "2022Q2 (3M)"  # lag-4 over this 4-row filing is the prior-year quarter
