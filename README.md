# kapmcp

## Türkçe

**kapmcp**, Borsa İstanbul için KAP bildirimlerini ve Yahoo Finance piyasa verilerini MCP uyumlu uygulamalara sunan bir Python sunucusudur. Resmî MKK VYK API üzerinden şirket bilgilerini, bildirim içeriklerini, finansal raporları, ek belgeleri ve fon sicilini getirir. Yahoo Finance üzerinden fiyat, geçmiş fiyat, finansal tablo, oran, analist tahmini ve haber verilerine erişim sağlar.

25 salt okunur araç sunar. Verileri yapılandırılmış JSON olarak döndürür; finansal oranları, büyümeyi ve teknik göstergeleri tanımlı formüllerle hesaplar. Yatırım önerisi, sıralama veya fiyat tahmini üretmez. Yerel kullanım için **stdio**, uzak bağlantılar için **Streamable HTTP** destekler.

### Kurulum ve çalıştırma

Python 3.10 veya üzeri gerekir:

```bash
pip install kapmcp
kapmcp
```

`kap-mcp` komutu da aynı sunucuyu başlatır. Yahoo Finance araçları ek anahtar gerektirmez. KAP araçları için MKK tarafından sağlanan API anahtarı ve MKK tarafında IP yetkilendirmesi gerekir:

```bash
export KAP_API_KEY="MKK_API_ANAHTARINIZ"
kapmcp
```

Uzak bağlantı için:

```bash
kapmcp --transport streamable-http --port 8000 --stateless
```

Yerel uç nokta `http://127.0.0.1:8000/mcp` olur. Dış erişim için HTTPS ve uygun erişim denetimi sağlayan bir ters vekil kullanın.

### MCP istemci ayarı

`mcpServers` yapılandırmasını destekleyen istemciler için:

```json
{
  "mcpServers": {
    "kap": {
      "command": "kapmcp",
      "env": { "KAP_API_KEY": "MKK_API_ANAHTARINIZ" }
    }
  }
}
```

İstemci komutu bulamazsa `command` alanında sanal ortamınızdaki çalıştırılabilir dosyanın tam yolunu kullanın.

### Neler yapabilir?

| Alan | Araçlar |
|---|---|
| Durum ve kodlar | `kap_status`, `get_reference_codes` |
| Şirket bilgileri | `search_companies`, `get_company` |
| Bildirimler | `search_disclosures`, `get_disclosure`, `search_disclosure_data`, `get_blocked_disclosures` |
| Ek belgeler | `get_disclosure_documents`, `get_document_text`, `search_documents` |
| Finansallar | `get_financials`, `get_metric_history`, `get_financial_ratios`, `get_growth`, `compare_financials` |
| Hak kullanımları ve haberler | `get_corporate_actions`, `get_company_news` |
| Piyasa verileri | `get_quote`, `get_price_history`, `get_market_overview`, `get_price_reaction`, `get_analyst_estimates` |
| Zaman çizelgesi ve fonlar | `get_company_timeline`, `search_funds` |

Örnek istekler: “THYAO'nun son bildirimlerini getir”, “ASELS'in sözleşme açıklamalarını bul”, “THYAO ve PGSUS gelirlerini karşılaştır”, “Bu bildirimin ekindeki kapasite bilgisini ara”.

### Yapılandırma

| Değişken | Açıklama / varsayılan |
|---|---|
| `KAP_API_KEY` | MKK API anahtarı; yalnızca KAP araçları için zorunlu |
| `KAP_API_SECRET` | MKK test geçidi için |
| `KAP_TEST_MODE` | `1`: test geçidini kullan |
| `KAP_TIMEOUT` | 30 saniye |
| `KAP_CACHE_TTL` | 600 saniye |
| `KAP_MAX_CONCURRENCY` | 4 eşzamanlı KAP isteği |
| `KAP_MAX_SCAN_PAGES` | Çağrı başına en fazla 400 indeks penceresi |
| `KAP_MAX_RESULT_CHARS` | Araç sonucu için 60000 karakter sınırı |
| `YAHOO_TIMEOUT` | 45 saniye |
| `KAP_LOG_LEVEL` | Günlük seviyesi; günlükler stderr'e yazılır |

### Sınırlar

KAP liste API'si indeks pencereleriyle çalışır. Tarih ve metin aramaları çok sayıda sayfa tarayabilir; `scan_complete` ve `next_cursor` alanlarıyla sonucun tamamlanıp tamamlanmadığını kontrol edin. Bildirim içeriğinde arama için `query_scope="content"` kullanılır.

Yahoo verilerinde eksik dönemler veya farklı raporlama para birimleri olabilir. Resmî KAP finansalları `disclosure_id` ile tek rapordan okunabilir; bankalar ve sigorta şirketleri için alan eşlemesi kısmi olabilir. Taranmış PDF'ler OCR olmadan metne dönüştürülemez. KAP hak kullanım tutarları bildirim gövdesinden ayrıştırılmaz; ilgili bildirimin bağlantısı döndürülür.

### Geliştirme ve lisans

```bash
pip install -e ".[dev]"
pytest
```

MIT lisansı. Geliştirici: Çağrı Güngör. [Kaynak kod](https://github.com/hasancagrigungor/kapmcp).

---

## English


[MCP](https://modelcontextprotocol.io) server for **Borsa İstanbul**: official **KAP** disclosures (Kamuyu Aydınlatma
Platformu, via the MKK VYK API) plus **Yahoo Finance** prices, normalised financial statements, ratios, analyst data
and news. Works with compatible MCP clients over stdio or Streamable HTTP.

**Design principle.** The server is a *data + deterministic-computation layer*: it fetches, normalises, filters,
paginates, computes ratios/growth and returns structured JSON with `source` / `source_url` on every result. It never
interprets, ranks, recommends, forecasts or draws charts — that is the calling agent's job. There are no
`analyze_*` / `should_buy` / `draw_chart` tools by design.

## Install

```bash
pip install kapmcp
kap-mcp                      # stdio
kap-mcp --transport streamable-http --port 8000 --stateless   # remote: http://host:8000/mcp
```

Python 3.10+. Yahoo tools work with no configuration. KAP tools need an MKK API key:

| Variable | Description |
|---|---|
| `KAP_API_KEY` | API key issued by MKK (required for KAP tools; also whitelist your IP with MKK) |
| `KAP_API_SECRET` | Only for the MKK test gateway |
| `KAP_TEST_MODE` | `1` → `apigwdev.mkk.com.tr` |
| `KAP_TIMEOUT` / `KAP_CACHE_TTL` / `KAP_MAX_CONCURRENCY` | 30 s / 600 s / 4 |
| `KAP_MAX_SCAN_PAGES` | Hard cap on 50-item index windows one call may scan (default 400) |
| `KAP_MAX_RESULT_CHARS` | Hard cap on a single tool result (default 60000; lists are truncated with a note) |
| `YAHOO_TIMEOUT` | 45 s |
| `KAP_LOG_LEVEL` | Logs go to stderr only |

Example stdio configuration for clients supporting `mcpServers`:

```json
{ "mcpServers": { "kap": { "command": "/path/to/.venv/bin/kap-mcp", "env": { "KAP_API_KEY": "…" } } } }
```

ChatGPT / remote clients: run with `--transport streamable-http` behind HTTPS and add `https://host/mcp`.

## Tools (25)

All tools are read-only (`readOnlyHint`), have titles, typed input schemas with enums, and output schemas.

| Tool | What it returns |
|---|---|
| **Reference** | |
| `kap_status` | Config + connectivity (KAP reachable? Yahoo reachable? limits) |
| `get_reference_codes` | Code tables: disclosure/member/fund types, action types, field & ratio names, MKK fault codes (also resource `kap://reference/codes`) |
| **Companies (KAP)** | |
| `search_companies` | Ticker or name (Turkish-insensitive) → `company_id`, tickers, member type, KAP URL |
| `get_company` | Profile from KAP: sector, market (e.g. YILDIZ PAZAR), index memberships (BIST 30/100/…), paid-in capital & registered ceiling, ISINs, direct/indirect shareholders with %, free float, board, executives, subsidiaries, auditor, registry/tax ids, website. Empty KAP fields are `null` + `not_available`. Optional Yahoo profile/holders. |
| **Disclosures (KAP)** | |
| `search_disclosures` | One search tool: tickers / company_id / keywords / type / class / date range / `since_id` / cursor / order / `include_details`. `query_scope="metadata"` matches sender name + KAP template name (e.g. *New Business Relation*, *Valuation Report*); `"content"` also matches subject/summary via detail calls. Reports `scan_complete`, `next_cursor`, scanned index range. |
| `get_disclosure` | Normalised metadata + body as plain text + structured data flattened to `path: value` (`format`: summary / text / full) |
| `search_disclosure_data` | Find fields inside a filing's structured data; for financial statements returns XBRL facts by Turkish label (`hasılat` → `Revenue` with CURR/PREV/CURR3/PREV3 values) |
| `get_blocked_disclosures` | Withdrawn disclosures and reasons |
| **Documents (KAP attachments, in memory only)** | |
| `get_disclosure_documents` | Attachment metadata (`attachment_id`, name, source_url) |
| `get_document_text` | PDF/HTML/text extraction with page selection; binary/scanned → `text: null` + reason |
| `search_documents` | Snippets with page numbers for a query across a disclosure's attachments |
| **Financials (Yahoo, normalised; deterministic maths)** | |
| `get_financials` | One schema for income / balance / cash flow (`revenue`, `gross_profit`, `operating_profit`, `ebitda`, `net_income`, `total_assets`, `current_assets`, `cash`, `total_liabilities`, `current_liabilities`, `financial_debt`, `equity`, cash-flow lines …) with source line per field and reporting currency. `ticker` → Yahoo time series; `disclosure_id` → the **official KAP filing** parsed from its XBRL data (current quarter, YTD and prior-year comparatives, `source: kap`) |
| `get_metric_history` | Chart-ready series, oldest first: `{series:[{period:"2025Q1", revenue:…}]}` |
| `get_financial_ratios` | Margins, current/cash ratio, D/E, financial D/E, net debt(/EBITDA), interest coverage, ROA, ROE (TTM), asset turnover, equity ratio — formula stated per item; missing input → `value: null, reason` |
| `get_growth` | YoY / QoQ rows: current/previous period & value, absolute and percentage change (`ticker` series or a KAP `disclosure_id`) |
| `compare_financials` | Metrics and ratios for several tickers in one call, same order as requested, no ranking |
| **Corporate actions** | |
| `get_corporate_actions` | Yahoo ex-dates/amounts + KAP CA disclosures classified by a fixed title keyword table; optional KAP process statuses |
| **News (Yahoo)** | |
| `get_company_news` | Ticker or keyword; title, publisher, published_at, url, summary — no sentiment |
| **Market data (Yahoo)** | |
| `get_quote` | Batch quotes; bad symbols return an error entry |
| `get_price_history` | OHLCV + summary (return, drawdown, volatility) + optional SMA/EMA/RSI/MACD/Bollinger/ATR |
| `get_market_overview` | BIST 100/30, banks, USD/TRY, EUR/TRY, gold, Brent, S&P, DAX, VIX |
| `get_price_reaction` | Returns/volume around a date or a `disclosure_id`, excess return vs BIST 100 |
| `get_analyst_estimates` | Third-party consensus as published (counts, targets, earnings date) |
| **Timeline** | |
| `get_company_timeline` | KAP disclosures (typed by KAP metadata) + corporate actions + news, newest first, each with `source_url` |
| **Funds (KAP registry)** | |
| `search_funds` | Filter/search funds or fetch one by `fund_id` |

### Example flows

- "THYAO'nun son 3 aylık açıklamaları" → `search_disclosures(tickers=["THYAO"], start_date="2026-06-21", include_details=true)`
- "ASELS'in sözleşme açıklamaları" → `search_disclosures(tickers=["ASELS"], query="sözleşme", query_scope="content")`
- "Bedelsiz sermaye artırımı açıklamaları" → `search_disclosures(query="bedelsiz", disclosure_type="CA")`
- "2026Q2 net kâr, YoY" → `get_growth(ticker="THYAO", metric="net_income", periods=1)`
- "THYAO vs PGSUS" → `compare_financials(tickers=["THYAO","PGSUS"], metrics=["revenue","net_margin","roe"])`
- "Rapor ekinde kapasite ne diyor?" → `get_disclosure_documents(id)` → `search_documents(disclosure_id=id, query="kapasite")`
- Monitoring → store `last_disclosure_id`, poll `search_disclosures(since_id=…, order="asc")`

## Honest limitations

- **KAP list API pages by index only** (50 per call, no date/keyword filter). Date and keyword queries scan index
  windows; every response reports `scan_complete`, `pages_scanned` and `next_cursor`. Long company histories need
  either many pages or an archive (planned, see `docs/ROADMAP.md`).
- **The list service's `title` is the sender's name, not the subject.** Cheap keyword search therefore matches the
  company name and KAP's template codes (`sub_report_ids`, e.g. `oda-12000_New-Business-Relation`); subject/summary
  matching needs `query_scope="content"` (one detail call per scanned item).
- **Financial time series come from Yahoo Finance** (may be USD for some BIST names — `currency` is always returned;
  small caps can be missing). The official KAP filing is parsed exactly from its XBRL data for one report at a time
  (`get_financials(disclosure_id=…)`); building a multi-year series from KAP alone means locating each FR id by
  scanning, which the archive planned in `docs/ROADMAP.md` will make cheap. Banks/insurers use other templates and
  map partially.
- The MKK gateway answers empty filtered windows and unknown ids with HTTP 400 + `ER005 "Bildirim bulunamadı"`;
  the server treats these as empty/not-found, not as authentication failures. Attachments arrive as a
  Java-serialised `byte[]` and are unwrapped transparently.
- **Corporate-action amounts inside KAP bodies are not parsed**; the tool returns the disclosure id/URL to read.
- Yahoo news is English-centric. No semantic search in v1 (title keyword + document snippet search instead).

## Security

Designed for remote deployment: no file-system writes, no shell, no arbitrary URL fetching. Attachments are fetched only
by KAP-issued id through the authenticated client, size-capped (40 MB) and processed in memory. User input never builds
URL paths unchecked. Every result is size-bounded; every scan is page-bounded.

## Development

```bash
uv pip install -e ".[dev]"
pytest          # 37 tests: pure computations, KAP client (respx), end-to-end tools via in-process MCP client
```

MIT
