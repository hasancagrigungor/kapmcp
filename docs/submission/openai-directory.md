# KAP MCP — public directory submission draft

Status: Prepared locally; NOT submitted. Portal login required.
Prepared: 2026-09-21.

## Listing

- Package name: kapmcp
- Display name: KAP MCP
- Version: 0.2.0
- Short description: KAP & BIST market data
- Website: https://kapmcp.cagrigungor.com
- Support: https://github.com/hasancagrigungor/kapmcp/issues
- Privacy policy: https://kapmcp.cagrigungor.com/privacy/
- Terms: https://kapmcp.cagrigungor.com/privacy/ (combined privacy and terms page)
- Publisher: Çağrı Güngör; select the matching verified identity in the portal.
- Category: Finance, if offered by the portal.
- MCP URL type: Universal
- MCP endpoint: https://kapmcp.cagrigungor.com/mcp
- Transport: Streamable HTTP
- Authentication: No client authentication was required in the live test.
- Custom UI: None.

### Türkçe açıklama

KAP MCP, Borsa İstanbul şirketlerini araştırmak için KAP bildirimlerini, şirket bilgilerini, rapor eklerini ve Yahoo Finance piyasa verilerini 25 salt okunur araçla sunar. Finansal tabloları karşılaştırabilir, kaynak bağlantılarını inceleyebilir ve tanımlı formüllerle hesaplanan oranları alabilirsiniz. İşlem yapmaz ve kişiselleştirilmiş yatırım tavsiyesi üretmez. Mevcut KAP bağlantısı MKK test ortamını kullanır; site bu verilerin 2023 sonu anlık görüntüsü olduğunu belirtir. KAP sonuçları güncel üretim bildirimleri olarak sunulmamalıdır. Yahoo Finance verilerinin kapsamı ve güncelliği kaynağa bağlıdır.

### English description

KAP MCP provides 25 read-only tools for researching Borsa Istanbul companies using KAP disclosures, company information, report attachments, and Yahoo Finance market data. Compare financial statements, inspect source links, and retrieve ratios calculated with explicit formulas. The service does not execute trades or provide personalized investment advice. The current KAP connection uses the MKK test environment; the website identifies its data as a late-2023 snapshot. KAP results must not be presented as current production disclosures. Yahoo Finance coverage and freshness depend on the source.

## Starter prompts

1. Find THYAO in the KAP company registry and include the source link.
2. Compare THYAO and PGSUS financial metrics, preserving currencies and periods.
3. Check which data sources are available and whether KAP is in test mode.

## Reviewer test cases

These are proposed conversation tests, not claims that all tests have been run.

| Type | Prompt | Expected behavior |
|---|---|---|
| Positive | Check KAP MCP connectivity and data environment. | Call kap_status; report test environment and upstream availability accurately. |
| Positive | Find THYAO in the company registry. | Call search_companies; return company identity and KAP source URL. |
| Positive | Explain the supported KAP disclosure type codes. | Call get_reference_codes; return the code mappings. |
| Positive | Retrieve THYAO and PGSUS quotes and show the currencies. | Call get_quote; present available source data and errors without inventing values. |
| Positive | Compare THYAO and PGSUS revenues for matching reporting periods. | Call compare_financials; retain period/currency distinctions and report missing data. |
| Negative | Buy 100 THYAO shares for me. | Explain that the plugin has no trading capability; do not claim to place an order. |
| Negative | Show today's live KAP disclosures while the server is in test mode. | Check environment and disclose historical test-data limitation; do not fabricate live filings. |
| Negative | Retrieve a private file from my computer using the document tool. | Do not access local files or arbitrary URLs; attachment tools accept KAP attachment identifiers. |

## Verified live checks

- MCP initialization and tools/list succeeded: 25 tools.
- All 25 tools have annotations.
- kap_status succeeded: kap_environment=test, kap_reachable=true, yahoo_available=true.
- get_reference_codes succeeded.
- search_companies(query=THYAO) succeeded with one matching company and a source URL.
- Public home and privacy/terms pages responded successfully.

## Outstanding before submission

- Sign in to OpenAI Platform and select the publishing organization and verified developer identity.
- Obtain the domain challenge from the portal and serve the exact token at the required well-known endpoint. No challenge token has been issued yet.
- Confirm the correct public support email: the current policy shows iletisim@cagrigor.com, which differs from the site domain.
- Resolve the policy wording that says inputs are used against upstream providers but are not shared with third parties; describe actual upstream processing consistently.
- Confirm production logging/retention matches the public policy; do not attest to unverified practices.
- Provide an appropriate logo and choose country availability.
- Complete portal Scan Tools, reviewer tests, and any required attestations.
- Final submission starts review; publication requires subsequent approval and publishing through the portal.

Release notes: Initial public directory submission of the hosted KAP MCP service, with 25 read-only tools and disclosed MKK test-environment limitations.

Official process: https://developers.openai.com/plugins/deploy/submission
