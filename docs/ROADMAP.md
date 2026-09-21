# kap-mcp — Rakip Analizi ve Yol Haritası

_Tarih: 2026-09-21. Durum: **v0.2** — Faz 0 tamamlandı: 25 konsolide araç (title + outputSchema), `services/` katmanı (deterministik finansal hesaplar, bildirim arama motoru, doküman arama), config/env ayrımı, sonuç boyutu ve tarama sınırları, 34 test._

## 1. Dünyadaki benzer çalışmalar

### 1.1 Türkiye odaklı (doğrudan rakipler)

| Proje | Kaynaklar | Araç | Öne çıkan | Zayıf yanı |
|---|---|---|---|---|
| [saidsurucu/borsa-mcp](https://github.com/saidsurucu/borsa-mcp) (654★, MIT) | KAP (Mynet üzerinden), Yahoo, TEFAS, BtcTurk/Coinbase, TCMB EVDS, doviz.com | 26 | **Uzak sunucu hazır** (`borsa.surucu.dev/mcp`), teknik analiz (RSI/MACD/Bollinger/pivot), BIST scanner, TEFAS 836 fon, kripto, makro, FastMCP response caching | KAP verisi resmi API değil (Mynet aracılığı); bildirim içeriği/ek dosya/flatData yok; bildirim→fiyat ilişkisi yok; test altyapısı zayıf |
| [YakupEmreYerli/borsa-mcp](https://github.com/YakupEmreYerli/borsa-mcp) | Yahoo, TEFAS, kripto, FX | — | borsa-mcp türevi | Aynı |
| [tufankoc/finans-mcp](https://github.com/tufankoc/finans-mcp) (0★) | 62 aracı kurum raporu, KAP, EVDS, BDDK, TEFAS, OpenBB | 19 | Markowitz/Monte Carlo/stres testi, konsensüs analizi, SPK uyum "guardrail"leri | Veri toplama scraping'e dayalı, dosya sistemi depolama, olgunlaşmamış |
| [BerkantACUN/mcp-turkiye](https://github.com/BerkantACUN/mcp-turkiye) | TCMB, EVDS, AFAD, tatil, TCKN/IBAN | — | Kamu verisi tek sunucu | Borsa derinliği yok |
| [cemsinano/pykap](https://github.com/cemsinano/pykap) | kap.org.tr | — | Kütüphane, MCP değil | Scraping |

### 1.2 Global referanslar

| Proje | Ne öğreniyoruz |
|---|---|
| [hachecito/yfinance-market-mcp](https://github.com/hachecito/yfinance-market-mcp) (30 araç) | Opsiyon zinciri, kurumsal/insider holder'lar, earnings/revenue tahminleri, sektör/endüstri özetleri, hazır screener'lar, batch OHLCV indirme |
| [narumiruna/yfinance-mcp](https://github.com/narumiruna/yfinance-mcp) | Grafik üretimi (görsel çıktı) |
| [Kanishka-dabas/financial-data-mcp-server](https://github.com/Kanishka-dabas/financial-data-mcp-server) | Uzak sunucu + **OAuth 2.1** — connector olmanın asgari şartı |
| [financial-datasets/mcp-server](https://github.com/financial-datasets/mcp-server) | Az ama net isimli araçlar (`get_income_statements`), API key ile ticari model |
| [cyanheads/secedgar-mcp-server](https://github.com/cyanheads/secedgar-mcp-server), [asp53826/edgar-mcp](https://github.com/asp53826/edgar-mcp), [ykshah1309/financial-hub-mcp](https://github.com/ykshah1309/financial-hub-mcp) | KAP'ın ABD karşılığı EDGAR: **dosya metni + XBRL normalizasyonu + fact dedup + hesaplanmış oranlar**; stdio ve Streamable HTTP birlikte; rate-limit koruması |
| [Alex2Yang97/yahoo-finance-mcp](https://github.com/Alex2Yang97/yahoo-finance-mcp), [danishashko/yahoo-finance-mcp](https://github.com/danishashko/yahoo-finance-mcp) | "Beginner friendly" kurulum, karşılaştırma araçları |

### 1.3 Bizim farkımız (koruyup büyütülecek)

- **Resmi MKK VYK API** — yapılandırılmış `flatData`, ekler, bloklanmış bildirimler, hak kullanım durumları, fon sicili. Rakiplerin hiçbiri resmi kaynaktan bildirim *içeriği* okumuyor.
- **Bildirim ↔ piyasa** bağlantısı: `get_disclosure_price_reaction`, `get_company_timeline` — EDGAR dünyasında bile nadir.
- Tarih → index binary search, Türkçe-duyarsız arama, PDF ek okuma, LLM'e uygun düz metin.
- 44 mock'lu test; rakiplerde neredeyse yok.

## 2. Tespit edilen eksikler (öncelik sırasıyla)

| # | Eksik | Neden önemli | Zorluk |
|---|---|---|---|
| E1 | **Araç `title`'ları ve `output_schema`** yok | MCP istemcilerinde keşif ve yapılandırılmış çıktı için gerekli | Düşük |
| E2 | **Uzak sunucu + OAuth 2.1** (DCR/PKCE, `401 + WWW-Authenticate`) | MCP istemcisi eklentisi olmanın ön şartı | Orta |
| E3 | **Teknik analiz** (SMA/EMA, RSI, MACD, Bollinger, ATR, pivot) | borsa-mcp'de var, kullanıcıların en sık isteği | Düşük (pandas) |
| E4 | **BIST tarayıcı/screener** (F/K < x, temettü > y, RSI < 30, 52 hafta dibi…) | Rakipte var; tüm evren için toplu veri gerekir | Orta (gece batch + cache) |
| E5 | **TEFAS** fon fiyat/getiri/portföy dağılımı | KAP fon sicili + TEFAS fiyat = tam fon resmi | Orta |
| E6 | **TCMB** (günlük kur XML anahtarsız; EVDS anahtarlı: enflasyon, faiz, rezerv) | Makro bağlam; rakipte var | Düşük/Orta |
| E7 | **Türkçe haber** | Yahoo haber modülü İngilizce; ihtiyaç Türkçe | Orta (kaynak seçimi: KAP + RSS/lisanslı) |
| E8 | ~~Ortaklık yapısı~~ **Yapıldı (v0.2):** `memberDetail` anahtar eşlemesi (ortaklar %, YK, iştirakler, endeksler, pazar) | — | — |
| E9 | **KAP arşivi tam metin arama** | Kimsede yok; "temettü açıklayan şirketler" tipi sorular | Orta (Postgres FTS, Django tarafı) |
| E10 | **Grafik/görsel çıktı** (MCP Apps widget) | MCP Apps üzerinden görsel sunum sağlar | Orta |
| E11 | ~~Hesaplanmış oranlar KAP verisinden~~ **Yapıldı (v0.2):** FR `presentation` XBRL ağacı → normalize şema + oranlar (`get_financials(disclosure_id)`) | — | — |
| E12 | **Bilanço takvimi / beklenen açıklama tarihleri** | Katalizör takibi | Orta (KAP'ta yapılandırılmış yok) |
| E13 | Watchlist, uyarı, izleme (Telegram/e-posta) | Eklenti dışı kullanıcı için asıl değer | Orta (Django) |
| E14 | Portföy analitiği (risk, Sharpe, korelasyon) | finans-mcp'nin niş alanı | Orta |
| E15 | Rate limiting, observability, çok kiracılılık | Uzak sunucu için şart | Orta |

## 3. Hedef mimari

```
kap-mcp (monorepo, tek pip paketi, extras ile)
├── kap_mcp/core/        # SAF ASYNC KÜTÜPHANE — MCP'ye/Django'ya bağımlı değil
│   ├── kap/             # MKK VYK istemcisi (mevcut client.py)
│   ├── market/          # Yahoo (mevcut market.py) + teknik analiz
│   ├── tefas/, tcmb/    # yeni kaynaklar
│   ├── text.py          # HTML/PDF/flatData → metin
│   ├── cache.py         # Protocol: get/set/ttl → bellek | Redis | Django cache
│   └── models.py        # Pydantic çıktı modelleri (Quote, Disclosure, Timeline…)
├── kap_mcp/services/    # Birleşik iş mantığı: overview, timeline, reaction, screener
├── kap_mcp/mcp/         # İnce adaptör: services → MCP araçları (title, annotations, output_schema)
│   ├── server.py        # stdio + streamable-http
│   ├── auth.py          # OAuth 2.1 (uzak mod)
│   └── widgets/         # MCP Apps HTML kaynakları (grafik, tablo)
├── kap_mcp/web/         # Django projesi (extras: kap-mcp[web])
│   ├── api/             # DRF/Ninja REST — services'i çağırır, aynı Pydantic modeller
│   ├── ui/              # HTMX + Tailwind şablonlar
│   ├── tasks/           # gece evren indirme, screener önhesap, uyarılar
│   └── mcp_mount.py     # /mcp altında MCPServer ASGI mount (tek deploy)
├── packaging/mcpb/      # Masaüstü MCP .mcpb manifest + privacy
└── tests/
```

**İlke:** Bir işlev üç yerden (MCP aracı, REST, HTML sayfası) aynı `services` fonksiyonunu çağırır; hiçbir iş mantığı araç fonksiyonunun içinde yaşamaz. Bugünkü `server.py`'deki `_collect_backwards`, `get_company_overview` gövdeleri `services/`e taşınır.

### 3.1 Performans

- **Tek async çekirdek**: httpx bağlantı havuzu, semaphore ile kaynak başına eşzamanlılık (KAP 4, Yahoo 8).
- **Katmanlı cache** (`core/cache.py` Protocol): bellek (stdio) → Redis (Django/uzak). Anahtar şeması `kap:members`, `yq:THYAO.IS`, `kt:1300000` (yayın zamanı kalıcı). Fiyat 60 s, geçmiş 5 dk, temel 15 dk, KAP listeleri 10 dk, bildirim detayı 24 s (değişmez).
- **Önhesaplama**: gece 19:30 (kapanış sonrası) BIST evreni için OHLCV + temel + teknik göstergeler tek `yf.download` batch'i ile → screener saniyeler içinde, Yahoo'ya gündüz yük yok.
- **KAP arşivi**: `get_new_disclosures_since` ile artımlı toplama → Postgres (`tsvector` Türkçe konfigürasyon) → tam metin arama ve tarih sorguları için binary search'e gerek kalmaz.
- **Django**: 5.x async view'lar + ASGI (uvicorn), `django-redis`, sayfa parçaları için HTMX + `Cache-Control`/ETag, DB için `select_related` + indeksler, `django-silk`/OpenTelemetry ile izleme.
- **Uzak MCP**: `stateless_http=True` + `json_response=True` (serverless dostu), istemci başına token-bucket rate limit, tool sonuçlarına boyut tavanı.

### 3.2 Django arayüzü — "basit, modern, sade"

- **Stack**: Django 5 + HTMX + Alpine.js (minimum) + Tailwind; SPA yok, build adımı sadece Tailwind. Inter/Geist yazı tipi, tek vurgu rengi, karanlık mod, mobil öncelikli, Türkçe sayı biçimi (1.234,56 ₺).
- **Sayfalar** (5 tane, fazlası değil):
  1. `/` — tek arama kutusu + piyasa şeridi (BIST100, USD/TRY, altın) + son bildirim akışı.
  2. `/s/THYAO` — şirket: fiyat + sparkline, değerleme kartları, **zaman çizelgesi** (bildirim/haber/fiyat), finansal tablolar sekmesi, ortaklık yapısı.
  3. `/b/1300000` — bildirim: düz metin, ekler (PDF metni satır içi), "piyasa tepkisi" kartı, ilgili hisse.
  4. `/tara` — screener: filtre çipleri (F/K, temettü, RSI, 52h), sıralanabilir tablo, CSV.
  5. `/izle` — watchlist + uyarı kuralları (yeni ODA, fiyat eşiği, RSI<30) → e-posta/Telegram.
- **Sohbet**: sağ altta "sor" kutusu — Model API + aynı MCP araçları (tool runner) ile; eklenti kullanmayanlara aynı deneyim. (Opsiyonel, Faz 4.)
- Bir dil rehberi: başlıklar kısa, sayı vurgulu, her veri kartında kaynak + zaman damgası ("KAP · 14:32", "Yahoo · 15 dk gecikmeli"), "yatırım tavsiyesi değildir" footer'ı.

### 3.3 MCP istemcisi eklentisi olma gereksinimleri (araştırma özeti)

**Ortak (tek uzak sunucu ikisine de hizmet eder):**
- HTTPS, **Streamable HTTP** `/mcp` (SSE deprecated), stateless çalışabilme.
- Her araçta `title`, ≤64 karakter ad, net "ne yapar / ne zaman çağrılır" açıklaması; `readOnlyHint`/`destructiveHint`/`openWorthHint` doğru; okuma-yazma ayrı araçlar (bizde tümü read-only — avantaj).
- `structuredContent` + `outputSchema` (ChatGPT), `_meta` ile modele gösterilmeyen istemci verisi.
- Sunucu `instructions` ≤512 karakter (şu anki metnimiz uzun, kısalacak).
- OAuth 2.1: DCR veya CIMD, PKCE S256, form-urlencoded token, `401 + WWW-Authenticate`. Anonim erişim de kabul (herkese açık veri) — **ilk sürüm anonim + rate limit, ikinci sürüm OAuth ile kişisel watchlist**.
- MCP Inspector ile tüm araçlar test; sınır/boş/hatalı girdi, timeout senaryoları.
- Gizlilik politikası URL'si; README'de Privacy bölümü.


**ChatGPT'ye özel:** Apps SDK — widget kaynakları (HTML, CSP uyumlu origin), OpenAI-managed mTLS seçeneği, "Scan Tools" ile şema snapshot'ı, yayınlanmış araç adlarında geriye uyumluluk zorunlu → **araç adları v1'de dondurulmalı**.


## 4. Fazlar

| Faz | Kapsam | Çıktı | Süre |
|---|---|---|---|
| **0 — Eklenti-hazır temel** | E1: tüm araçlara `title`, Pydantic çıktı modelleri, `instructions` kısaltma; araç adlarını dondur (v1 sözleşmesi); `core/services/mcp` ayrımı; `cache.py` Protocol | v0.2, PyPI `kapmcp` | 1 hafta |
| **1 — Veri derinliği** | E3 teknik analiz, E6 TCMB kur, E8 ortaklık yapısı, E5 TEFAS, E7 Türkçe haber (kaynak kararı) | v0.3 | 2 hafta |
| **2 — Uzak sunucu** | Streamable HTTP dağıtımı (Fly.io/Railway/Hetzner + Redis), anonim + rate limit, `stateless_http`, OpenTelemetry, `/healthz`; MCP Inspector test matrisi; gizlilik politikası | `mcp.kap…/mcp` | 1–2 hafta |
| **3 — Django** | Model+arşiv toplayıcı, REST, 5 sayfa, screener önhesabı (E4, E9), watchlist/uyarı (E13); `/mcp` aynı ASGI'de | v0.5, canlı site | 4 hafta |
| **4 — Eklentiler** | `.mcpb` paketi; MCP istemci entegrasyonları; MCP Apps widget'ları (E10: fiyat grafiği, zaman çizelgesi); OAuth 2.1 (django-oauth-toolkit, DCR) | Dizinlerde yayın | 2–3 hafta |
| **5 — Analitik** | E11 KAP flatData → normalize oranlar (canlı şema ile), E14 portföy analitiği, E12 takvim | v1.0 | sürekli |

### Hemen yapılabilecek "quick win"ler (Faz 0'ın parçası)
1. `@mcp.tool(title=...)` — 39 araç.
2. `instructions` → 512 karakter.
3. `output_schema` için `TypedDict`/Pydantic dönüş tipleri (MCP SDK 2.x otomatik `structuredContent` üretir).
4. Sonuç boyutu tavanı (ör. 60 KB) ve `truncated` bayrağı tüm liste araçlarında.
5. `market.py`'de `get_technicals(symbol)` (SMA20/50/200, RSI14, MACD, Bollinger, ATR, pivot) — pandas ile ~80 satır.

## 5. Açık kararlar (kullanıcıya)

2. **Türkçe haber kaynağı**: yalnızca KAP + Yahoo mu, yoksa RSS (Bloomberg HT, Dünya, AA Finans) mı? Lisans/telif ve kırılganlık riski.
3. **Ticari model**: anonim ücretsiz + OAuth ile watchlist ücretsiz mi, yoksa API key/limit kademesi mi? (financial-datasets modeli.)
4. **KAP kimlik bilgisi**: uzak sunucuda tek MKK anahtarı (bizim) mı, kullanıcı kendi anahtarını mı girecek? MKK sözleşmesi yeniden dağıtıma izin veriyor mu — kontrol edilmeli.
5. Hosting: Fly.io (kolay, Redis dahil) vs Hetzner (ucuz, kendi yönetim).
