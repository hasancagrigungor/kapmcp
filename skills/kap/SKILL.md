---
description: How to answer Borsa İstanbul / KAP questions with the kap MCP tools — which tool to call for disclosures, company profiles, official financial statements, attachments, prices and news, and how to cite sources. Use whenever a user mentions BIST, KAP, a Turkish ticker (THYAO, ASELS…), Turkish company filings, bilanço, temettü or kâr.
---

# Working with the kap tools

The `kap` server is a data layer: it fetches, normalises and computes; **you** interpret, compare and summarise.
Every result carries `source` / `source_url` — cite them (KAP disclosure id + link, Yahoo symbol).

## Resolve the company first
- Ticker or name → `search_companies(query)` → `company_id`, tickers.
- Profile (sector, market, index memberships, capital, shareholders %, board, subsidiaries) → `get_company(ticker=…)`.
  Fields KAP left empty come back `null` with `not_available`; say "KAP'ta bu alan boş" rather than guessing.

## Disclosures (KAP)
- Latest / by company / by date / by type → `search_disclosures(tickers=[…], disclosure_type=…, start_date=…, end_date=…, include_details=true)`.
- Topic search: KAP's list only carries the sender name and a template code, so:
  `query="…"` with `query_scope="metadata"` matches template names (e.g. "New Business Relation", "Valuation Report");
  `query_scope="content"` matches subject/summary (slower, bounded by `max_pages`).
- The KAP API pages by index only. If `scan_complete` is false, tell the user the scan was bounded and continue with `cursor` or a higher `max_pages` instead of claiming "there are none".
- Read one: `get_disclosure(disclosure_id)` (plain text + flattened data). Attachments: `get_disclosure_documents` → `search_documents(disclosure_id, query)` for snippets, `get_document_text(attachment_id, pages="1-3")` for a page range. Never request whole 200-page PDFs when a snippet search will do.
- Monitoring: keep `last_disclosure_id`, poll `search_disclosures(since_id=…, order="asc")`.

## Financials
- Official filing (exact figures, TRY): find the FR id with `search_disclosures(tickers=[…], disclosure_type="FR", query="financial statements")`, then
  `get_financials(disclosure_id=…)`, `get_financial_ratios(disclosure_id=…)`, `get_growth(disclosure_id=…, metric=…)`.
  Labels "(3M)" = quarter, "(YTD)" = cumulative; prior-year comparatives are included. Use `search_disclosure_data(disclosure_id, "hasılat")` for a single line item.
- Time series across many periods → `get_financials(ticker=…)`, `get_metric_history`, `get_growth(ticker=…)`, `compare_financials` (source: Yahoo; check `currency` — some BIST names report in USD).
- Ratios come with `formula`; a `null` value has a `reason` (e.g. TTM needs 4 quarters). Report the reason, do not compute a substitute silently.

## Market data, corporate actions, news
- `get_quote`, `get_price_history(include_indicators=true)`, `get_market_overview`, `get_analyst_estimates` (third-party consensus, report as such).
- Reaction to a filing → `get_price_reaction(disclosure_id=…)` (excess return vs BIST 100; do not claim causality).
- Dividends / bonus issues / buybacks → `get_corporate_actions(ticker)`; amounts inside KAP bodies are not parsed — open the referenced disclosure.
- News → `get_company_news` (Yahoo, mostly English). For Turkish primary sources prefer the disclosures themselves.

## Style
- Answer in the user's language; keep KAP terms (ÖDA, bedelsiz, hak kullanım) as they are.
- State the data date (`published_at`, `as_of`, `period_end`) next to every figure.
- Never present a recommendation as coming from the tools; the server does not rank or advise. Add "yatırım tavsiyesi değildir" when giving opinions.
