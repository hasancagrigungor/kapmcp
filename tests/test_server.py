"""End-to-end: drive the MCP server through an in-process client against a mocked KAP gateway
and a fake yfinance (see tests/fakes.py)."""

import io
import json
from datetime import datetime, timedelta

import pytest
import respx
from mcp import Client

from kap_mcp import market, server
from kap_mcp.client import BASE_URL_PROD, KAPClient
from tests.fakes import FakeYF

pytestmark = pytest.mark.anyio

LAST = 1_300_119
MIN_IDX = 538004
BASE_TIME = datetime(2020, 1, 1, 0, 0, 0)


def _pdf(texts):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    w = PdfWriter()
    for t in texts:
        page = w.add_blank_page(width=300, height=300)
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 20 200 Td ({t}) Tj ET".encode("latin-1"))
        page[NameObject("/Contents")] = w._add_object(stream)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): w._add_object(font)})})
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


PDF_BYTES = _pdf(["Toplam hasilat 1.000 milyon TL", "Net donem kari 50 milyon TL", "Sayfa uc"])


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.setenv("KAP_API_KEY", "k")
    server.reload_settings()
    market.cache.clear()
    monkeypatch.setattr(market, "_yf", lambda: FakeYF)
    client = KAPClient(api_key="k", max_retries=0)
    server.set_client(client)
    with respx.mock(base_url=BASE_URL_PROD, assert_all_called=False) as mock:
        mock.get("/auth/generateToken").respond(json={"token": "t"})
        mock.get("/api/vyk/lastDisclosureIndex").respond(json={"lastDisclosureIndex": LAST})

        def disclosures(request):
            start = int(request.url.params["disclosureIndex"])
            ctype = request.url.params.get("disclosureTypes")
            cid = request.url.params.get("companyId")
            items = []
            for i in range(start, start + 50):
                if i > LAST:
                    break
                t = "ODA" if i % 3 == 0 else ("CA" if i % 7 == 0 else "FR")
                company = "c1" if i % 2 == 0 else "c3"
                if ctype and t != ctype:
                    continue
                if cid and company != cid:
                    continue
                name = "TÜRK HAVA YOLLARI A.O." if company == "c1" else "İŞ BANKASI"
                sub = None if t == "CA" else (["oda-12000_New-Business-Relation"] if i % 5 == 0 else ["oda-10000_Material-Event-Disclosure-General"])
                items.append({"disclosureIndex": str(i), "disclosureType": t, "disclosureClass": t if t != "CA" else "ODA",
                              "title": name, "companyId": company, "subReportIds": sub})
            return respx.MockResponse(200, json=items)

        mock.get("/api/vyk/disclosures").mock(side_effect=disclosures)

        def detail(request, idx):
            idx = int(idx)
            if idx == 1300050:
                return respx.MockResponse(404, json={"fault": {"faultCode": "NF", "faultString": "blocked"}})
            t = BASE_TIME + timedelta(minutes=idx - MIN_IDX)
            body = {
                "disclosureIndex": str(idx), "disclosureType": "FR" if idx % 3 else "ODA", "senderId": "c1",
                "senderTitle": "TÜRK HAVA YOLLARI A.O.", "senderExchCodes": ["THYAO"],
                "time": t.strftime("%d.%m.%Y %H:%M:%S"),
                "disclosureReason": "UPD" if idx % 11 == 0 else "NEW",
                "subject": {"tr": "Kar Payı Dağıtım İşlemlerine İlişkin Bildirim" if idx % 7 == 0 and idx % 3 else "Özel Durum Açıklaması (Genel)", "en": "Subject"},
                "summary": {"tr": "Kapasite artışı yatırımı hk." if idx % 5 == 0 else "Özet metni", "en": None},
                "year": "2025", "period": {"tr": "6 Aylık", "en": "6 Months"}, "consolidation": "Konsolide",
                "htmlMessage": "<html><body><h1>Başlık</h1><table><tr><td>Hasılat</td><td>1.000</td></tr></table><script>x</script></body></html>",
                "flatData": {"gelirTablosu": [{"tr": "Hasılat", "value": 1000}, {"tr": "Net Dönem Karı", "value": 50}]},
                "attachmentUrls": [{"url": "https://kap/api/vyk/downloadAttachment/att1", "fileName": "rapor.pdf"},
                                   {"url": "https://kap/api/vyk/downloadAttachment/att3", "fileName": "tablo.xlsx"}],
                "link": f"https://www.kap.org.tr/tr/Bildirim/{idx}",
            }
            return respx.MockResponse(200, json=body)

        mock.get(path__regex=r"/api/vyk/disclosureDetail/(?P<idx>\d+)").mock(side_effect=detail)
        mock.get("/api/vyk/downloadAttachment/att1").respond(content=PDF_BYTES, headers={"Content-Type": "application/pdf"})
        mock.get("/api/vyk/downloadAttachment/att2").respond(content=b"<html><body><p>Merhaba</p></body></html>", headers={"Content-Type": "text/html"})
        mock.get("/api/vyk/downloadAttachment/att3").respond(content=b"PK\x03\x04zip", headers={"Content-Type": "application/zip", "Content-Disposition": 'attachment; filename="t.xlsx"'})
        mock.get("/api/vyk/members").respond(json=[
            {"id": "c1", "title": "TÜRK HAVA YOLLARI A.O.", "stockCode": "THYAO", "memberType": "IGS", "kfifUrl": "https://www.kap.org.tr/tr/kfif/c1"},
            {"id": "c2", "title": "FON A.Ş.", "stockCode": "", "memberType": "FK"},
            {"id": "c3", "title": "İŞ BANKASI", "stockCode": "ISCTR, ISATR", "memberType": "IGS"},
        ])
        mock.get("/api/vyk/memberDetail/c1").respond(json=[
            {"key": "kpy41_acc2_sektor", "nameTr": "Şirketin Sektörü", "value": "ULAŞTIRMA VE DEPOLAMA / ULAŞTIRMA VE DEPOLAMA"},
            {"key": "kpy41_acc3_sermaye_arac_pazar", "nameTr": "Pazar", "value": "YILDIZ PAZAR"},
            {"key": "kpy41_acc3_endeksler", "nameTr": "Endeksler", "value": "BIST 30 / BIST 100 / BIST ULAŞTIRMA"},
            {"key": "kpy41_acc5_odenmis_sermaye", "nameTr": "Ödenmiş Sermaye", "value": "1.380.000.000"},
            {"key": "kpy41_acc5_kayitli_sermaye_tavani", "nameTr": "KST", "value": "5.000.000.000"},
            {"key": "kpy41_acc5_sermayede_dogrudan", "nameTr": "Ortaklar", "value": [
                {"shareholder": "TÜRKİYE VARLIK FONU", "shareInCapital": "677.884.848,57", "ratioInCapital": "49,12", "votingRightRatio": "49,12"},
                {"shareholder": "TOPLAM", "shareInCapital": "1.380.000.000", "ratioInCapital": "100", "votingRightRatio": "100"}]},
            {"key": "kpy41_acc6_yonetim_kurulu_uyeleri", "nameTr": "YK", "value": [{"name": "A. B.", "title": "Başkan"}]},
            {"key": "kpy41_acc7_bagli_ortakliklar", "nameTr": "Bağlı Ortaklıklar", "value": None},
            {"key": "kpy41_acc1_int_addres", "nameTr": "Web", "value": "https://www.turkishairlines.com"},
        ])
        mock.get("/api/vyk/memberSecurities").respond(json=[{"member": {"id": "c1", "sermayeSistemi": "KS", "kayitliSermayeTavani": 5000000000, "mksMbrId": "TRTHYAO"},
                                                             "securities": [{"isin": "TRATHYAO91M5", "borsaKodu": "THYAO", "currentCapital": 1379999999.99, "groupCode": "E", "borsadaIslemeAcik": True}]}])
        mock.get("/api/vyk/blockedDisclosures").respond(json=[{"disclosureIndex": 1300050, "blockedType": "KVKK", "senderTitle": "X", "isBlockedDescriptionTr": "Kişisel veri"}])
        mock.get("/api/vyk/caEventStatus").respond(json=[{"refId": 1, "status": "TM"}])
        mock.get("/api/vyk/funds").respond(json=[{"fundId": 1, "fundName": "İş Portföy Altın", "fundCode": "IPB", "fundType": "BYF", "fundState": "Y", "kapUrl": "https://kap/fon/1"}])
        mock.get("/api/vyk/fundDetail/1").respond(json={"fundId": 1, "fundName": "İş Portföy Altın"})
        yield mock
    server.set_client(None)


@pytest.fixture
async def session(gateway):
    async with Client(server.mcp) as c:
        yield c


async def call(s, name, **args):
    res = await s.call_tool(name, args)
    text = "".join(c.text for c in res.content if c.type == "text")
    if res.is_error:
        return res, text
    return res, (res.structured_content if res.structured_content is not None else json.loads(text))


# ----------------------------------------------------------------------------- companies


async def test_status_and_reference(session):
    _, d = await call(session, "kap_status")
    assert d["kap_reachable"] and d["last_disclosure_id"] == LAST and d["yahoo_check"] == 13000.0
    _, d = await call(session, "get_reference_codes")
    assert "revenue" in d["financial_fields"] and "roe" in d["ratios"]
    res = await session.read_resource("kap://reference/codes")
    assert "fund_types" in res.contents[0].text


async def test_search_companies_turkish_and_member_type(session):
    _, d = await call(session, "search_companies", query="is bankasi")
    assert d["items"][0]["company_id"] == "c3" and d["items"][0]["tickers"] == ["ISCTR", "ISATR"]
    _, d = await call(session, "search_companies", member_type="FK")
    assert d["total"] == 1 and d["items"][0]["company_id"] == "c2"
    _, d = await call(session, "search_companies", query="isatr")
    assert d["items"][0]["ticker"] == "ISCTR" and d["items"][0]["source_url"].endswith("/c3")


async def test_get_company_profile_sections(session):
    _, d = await call(session, "get_company", ticker="thyao")
    assert d["name"].startswith("TÜRK") and d["ticker"] == "THYAO" and d["is_listed"]
    assert d["capital"]["paid_in_capital"] == 1380000000.0 and d["capital"]["registered_capital_ceiling"] == 5000000000.0
    assert d["capital"]["capital_system"] == "KS" and d["capital"]["securities"][0]["isin"] == "TRATHYAO91M5"
    assert d["sector"].startswith("ULAŞTIRMA") and d["market"] == "YILDIZ PAZAR" and d["indices"] == ["BIST 30", "BIST 100", "BIST ULAŞTIRMA"]
    sh = d["shareholders"]
    assert sh[0]["shareholder"].startswith("TÜRKİYE") and sh[0]["ratio_in_capital_pct"] == 49.12 and sh[0]["share_in_capital"] == 677884848.57 and sh[1]["is_total"]
    assert d["board_members"][0]["title"] == "Başkan"
    assert d["subsidiaries"] is None and "subsidiaries" in d["not_available"] and "executives" in d["not_available"]
    assert d["source_url"] == "https://www.kap.org.tr/tr/kfif/c1"
    assert d["market_profile"]["sector"] == "Industrials" and d["holders"]["source"] == "yahoo_finance"
    assert d["kap_detail_fields"]["kpy41_acc1_int_addres"]["value"].startswith("https")
    res, text = await call(session, "get_company", ticker="NOPE")
    assert res.is_error and "No company" in text


# ----------------------------------------------------------------------------- disclosures


async def test_search_disclosures_latest_and_cursor(session):
    _, d = await call(session, "search_disclosures", limit=5)
    ids = [i["disclosure_id"] for i in d["items"]]
    assert ids == list(range(LAST, LAST - 5, -1)) and d["next_cursor"] == LAST - 5 and d["scan_complete"] is False
    assert d["items"][0]["source_url"].endswith(str(LAST)) and d["items"][0]["ticker"] in ("THYAO", "ISCTR")
    _, d2 = await call(session, "search_disclosures", limit=5, cursor=d["next_cursor"])
    assert [i["disclosure_id"] for i in d2["items"]][0] == LAST - 5


async def test_search_disclosures_by_ticker_type_and_template_keyword(session):
    # metadata scope: matches the template name, no detail calls
    _, d = await call(session, "search_disclosures", tickers=["THYAO"], disclosure_type="ODA", query="business relation", limit=3)
    assert d["count"] == 3 and d["query_scope"] == "metadata"
    for it in d["items"]:
        assert it["company_id"] == "c1" and it["disclosure_type"] == "ODA" and it["disclosure_id"] % 5 == 0
        assert it["templates"] == [{"id": "oda-12000", "name": "New Business Relation"}] and it["company_name"].startswith("TÜRK")
        assert "published_at" not in it
    # content scope: matches the summary from the detail record
    _, d = await call(session, "search_disclosures", tickers=["THYAO"], disclosure_type="ODA", query="kapasite artışı", query_scope="content", limit=2, max_pages=3)
    assert d["count"] == 2
    for it in d["items"]:
        assert it["disclosure_id"] % 5 == 0 and it["summary"].startswith("Kapasite") and it["published_at"]
        assert it["attachments"][0]["attachment_id"] == "att1" and it["is_update"] in (True, False)
    assert d["filters"]["query"] == "kapasite artışı"


async def test_search_disclosures_since_id_and_asc(session):
    _, d = await call(session, "search_disclosures", since_id=LAST - 5, order="asc", limit=100)
    assert [i["disclosure_id"] for i in d["items"]] == list(range(LAST - 4, LAST + 1))
    assert d["scan_complete"] is True and d["next_cursor"] is None


async def test_search_disclosures_date_range_inclusive(session):
    start = BASE_TIME + timedelta(minutes=1_300_100 - MIN_IDX)
    end = BASE_TIME + timedelta(minutes=1_300_109 - MIN_IDX)
    _, d = await call(session, "search_disclosures", start_date=start.strftime("%d.%m.%Y %H:%M"),
                      end_date=end.strftime("%Y-%m-%dT%H:%M:%S"), disclosure_type="ODA", limit=50)
    assert d["scanned_index_range"] == [1300100, 1300109]
    assert [i["disclosure_id"] for i in d["items"]] == [1300107, 1300104, 1300101]
    res, text = await call(session, "search_disclosures", start_date="bogus")
    assert res.is_error and "Unrecognised date" in text


async def test_get_disclosure_text_and_fields(session):
    _, d = await call(session, "get_disclosure", disclosure_id=1300000)
    assert d["ticker"] == "THYAO" and d["fiscal_period"] == "6 Aylık" and d["published_at"] == "2021-06-13T03:56" and d["is_update"] is False
    assert d["source_url"] == "https://www.kap.org.tr/tr/Bildirim/1300000"  # detail's own link wins
    assert "Hasılat | 1.000" in d["text"] and "gelirTablosu[Net Dönem Karı].value: 50" in d["text"] and "<" not in d["text"]
    assert d["attachments"][0]["attachment_id"] == "att1"
    _, s = await call(session, "get_disclosure", disclosure_id=1300000, format="summary")
    assert "text" not in s
    _, f = await call(session, "get_disclosure", disclosure_id=1300000, format="full")
    assert "flatData" in f["raw"] and "htmlMessage" not in f["raw"]
    _, m = await call(session, "search_disclosure_data", disclosure_id=1300000, query="net donem kari")
    assert {x["value"] for x in m["matches"]} == {"Net Dönem Karı", "50"}
    res, text = await call(session, "get_disclosure", disclosure_id=1300050)
    assert res.is_error and "Not found" in text
    _, b = await call(session, "get_blocked_disclosures")
    assert b["items"][0]["disclosure_id"] == 1300050


# ----------------------------------------------------------------------------- documents


async def test_documents_list_text_and_search(session):
    _, d = await call(session, "get_disclosure_documents", disclosure_id=1300000)
    assert [x["attachment_id"] for x in d["documents"]] == ["att1", "att3"] and d["documents"][0]["disclosure_id"] == 1300000
    _, t = await call(session, "get_document_text", attachment_id="att1", pages="2")
    assert t["pages_total"] == 3 and t["pages_returned"] == [2] and "Net donem kari" in t["text"]
    _, x = await call(session, "get_document_text", attachment_id="att3")
    assert x["text"] is None and "binary" in x["reason"]
    _, h = await call(session, "get_document_text", attachment_id="att2")
    assert h["text"] == "Merhaba"
    _, s = await call(session, "search_documents", disclosure_id=1300000, query="net dönem karı")
    assert s["count"] == 1 and s["results"][0]["page"] == 2 and s["results"][0]["document"] == "rapor.pdf"
    assert s["skipped"][0]["attachment_id"] == "att3"
    res, text = await call(session, "search_documents", query="x y")
    assert res.is_error and "required" in text


# ----------------------------------------------------------------------------- financials (fake Yahoo)


async def test_financials_ratios_growth_compare(session):
    _, d = await call(session, "get_financials", ticker="THYAO", periods=2, fields=["revenue", "net_income"])
    assert d["periods"][0]["period"] == "2026Q2" and d["periods"][0]["revenue"] == 120.0 and d["currency"] == "TRY"
    assert d["field_sources"]["revenue"] == "Total Revenue"
    _, h = await call(session, "get_metric_history", ticker="THYAO", metrics=["revenue"], periods=3)
    assert [s["period"] for s in h["series"]] == ["2025Q4", "2026Q1", "2026Q2"]
    _, r = await call(session, "get_financial_ratios", ticker="THYAO")
    ratios = {x["metric"]: x for x in r["ratios"]}
    assert ratios["net_margin"]["value"] == 10.0 and r["market_multiples"]["pe_forward"] == 3.9
    _, g = await call(session, "get_growth", ticker="THYAO", metric="revenue", periods=1)
    assert g["rows"][0]["percentage_change"] == 20.0
    _, c = await call(session, "compare_financials", tickers=["THYAO", "PGSUS", "NOPE"], metrics=["revenue", "roe"])
    assert [x["ticker"] for x in c["rows"]] == ["THYAO", "PGSUS"] and c["errors"][0]["ticker"] == "NOPE"


# ----------------------------------------------------------------------------- corporate actions, news, market


async def test_corporate_actions_merge_sources(session):
    _, d = await call(session, "get_corporate_actions", ticker="THYAO", limit=50, max_pages=4, process_ref_ids=[1])
    assert {i["source"] for i in d["items"]} == {"yahoo_finance", "kap"}
    kap = [i for i in d["items"] if i["source"] == "kap"]
    assert kap and all(i["type"] == "dividend" and i["disclosure_type"] == "CA" for i in kap) and kap[0]["structured_fields"] is None
    y = [i for i in d["items"] if i["source"] == "yahoo_finance"][0]
    assert y["type"] == "dividend" and y["gross_amount_per_share"] == 6.884 and y["date_kind"] == "ex_dividend_date"
    assert d["process_statuses"][0]["status_text"] == "Tamamlandı"
    _, only = await call(session, "get_corporate_actions", ticker="THYAO", types=["split"], include_kap=False)
    assert only["count"] == 0


async def test_news_quote_history_reaction(session):
    _, n = await call(session, "get_company_news", ticker="THYAO", start_date="2026-09-01")
    assert [x["title"] for x in n["items"]] == ["Boeing deal"] and n["items"][0]["publisher"] == "Reuters"
    _, q = await call(session, "get_quote", symbols=["THYAO", "NOPE"])
    assert q["items"][0]["changePct"] == 2.04 and "error" in q["items"][1]
    _, h = await call(session, "get_price_history", symbol="THYAO", period="1mo", include_indicators=True)
    assert h["summary"]["max_drawdown_pct"] < -4 and "rsi14" in h["indicators"]["latest"]
    _, r = await call(session, "get_price_reaction", disclosure_id=1300000, days_after=2)
    assert r["symbol"] == "THYAO.IS" and r["disclosure"]["after_close"] is False and r["benchmark"] == "XU100.IS"
    res, text = await call(session, "get_price_reaction", symbol="THYAO")
    assert res.is_error and "event_date" in text
    _, a = await call(session, "get_analyst_estimates", ticker="THYAO")
    assert a["upside_to_mean_target_pct"] == 64.29


async def test_timeline_types_and_sources(session):
    _, d = await call(session, "get_company_timeline", ticker="THYAO", start_date="2025-08-01", max_pages=3, event_types=["news", "corporate_action"])
    assert {e["type"] for e in d["events"]} == {"news", "corporate_action"} and "disclosure_error" not in d
    dates = [e["date"] for e in d["events"]]
    assert dates == sorted(dates, reverse=True)


async def test_funds(session):
    _, d = await call(session, "search_funds", query="altin")
    assert d["items"][0]["code"] == "IPB" and d["items"][0]["source_url"].startswith("https://kap")
    _, one = await call(session, "search_funds", fund_id=1)
    assert one["detail"]["fundName"].startswith("İş")


async def test_result_size_is_bounded(session, monkeypatch):
    monkeypatch.setattr(server, "settings", server.settings.__class__(**{**server.settings.__dict__, "max_result_chars": 2000}))
    _, d = await call(session, "search_disclosures", limit=100)
    assert d["truncated"] is True and len(d["items"]) < 100


async def test_kap_tools_report_missing_key(monkeypatch):
    monkeypatch.delenv("KAP_API_KEY", raising=False)
    server.reload_settings()
    server.set_client(None)
    market.cache.clear()
    monkeypatch.setattr(market, "_yf", lambda: FakeYF)
    async with Client(server.mcp) as c:
        res, text = await call(c, "search_disclosures")
        assert res.is_error and "KAP_API_KEY" in text
        _, q = await call(c, "get_quote", symbols=["THYAO"])  # Yahoo still works
        assert q["items"][0]["lastPrice"] == 280.0
        _, ca = await call(c, "get_corporate_actions", ticker="THYAO")
        assert "KAP_API_KEY" in ca["kap_error"] and ca["count"] >= 1


async def test_caller_credentials_override_server_key(monkeypatch):
    """Requests carrying X-KAP-API-KEY are served with the caller's key even when the server has none."""
    monkeypatch.delenv("KAP_API_KEY", raising=False)
    server.reload_settings()
    server.set_client(None)
    with respx.mock(base_url="https://apigwdev.mkk.com.tr", assert_all_called=False) as mock:
        last = mock.get("/api/vyk/lastDisclosureIndex").respond(json={"lastDisclosureIndex": 42})
        creds = server.credentials_from_headers({"X-KAP-API-KEY": "userkey", "X-KAP-Api-Secret": "s", "x-kap-test-mode": "1"})
        assert creds == {"api_key": "userkey", "api_secret": "s", "test_mode": True}
        assert server.credentials_from_headers({"Accept": "*/*"}) is None
        token = server.request_credentials.set(creds)
        try:
            async with Client(server.mcp) as c:
                res = await c.call_tool("kap_status")
                d = res.structured_content
                assert d["using_caller_credentials"] and d["kap_reachable"] and d["last_disclosure_id"] == 42
        finally:
            server.request_credentials.reset(token)
        assert last.calls.last.request.headers["Authorization"].startswith("Basic ")
        # Without the header the server key is still absent -> clean error
        async with Client(server.mcp) as c:
            res = await c.call_tool("search_disclosures")
            assert res.is_error and "KAP_API_KEY" in res.content[0].text
    await server.shutdown()


async def test_oauth_provider_roundtrip(tmp_path):
    """DCR -> authorize -> consent -> code -> token -> credentials lookup, persisted encrypted."""
    from cryptography.fernet import Fernet
    from mcp.server.auth.provider import AuthorizationParams
    from mcp.shared.auth import OAuthClientInformationFull
    from pydantic import AnyUrl

    from kap_mcp import oauth as O

    secret = Fernet.generate_key().decode()
    prov = O.build_provider("https://x.test", tmp_path / "s.enc", secret)
    client = OAuthClientInformationFull(client_id="c1", client_name="Claude", redirect_uris=[AnyUrl("https://claude.ai/api/mcp/auth_callback")])
    await prov.register_client(client)
    url = await prov.authorize(client, AuthorizationParams(state="st", scopes=["kap"], code_challenge="ch", redirect_uri=AnyUrl("https://claude.ai/api/mcp/auth_callback"), redirect_uri_provided_explicitly=True))
    txn = url.split("txn=")[1]
    assert url.startswith("https://x.test/oauth/consent?txn=") and prov.get_txn(txn)["client_id"] == "c1"
    redirect = await prov.complete_consent(txn, "k", "s", True)
    assert redirect.startswith("https://claude.ai/api/mcp/auth_callback?code=") and redirect.endswith("&state=st")
    code = redirect.split("code=")[1].split("&")[0]
    ac = await prov.load_authorization_code(client, code)
    assert ac and ac.code_challenge == "ch"
    tok = await prov.exchange_authorization_code(client, ac)
    assert await prov.load_authorization_code(client, code) is None  # single use
    assert prov.credentials_for_token(tok.access_token) == {"api_key": "k", "api_secret": "s", "test_mode": True}
    assert prov.credentials_for_token("nope") is None
    # persisted + encrypted: a fresh provider with the same secret sees the token, a wrong secret sees nothing
    again = O.build_provider("https://x.test", tmp_path / "s.enc", secret)
    assert again.credentials_for_token(tok.access_token)["api_key"] == "k"
    assert b"k" not in (tmp_path / "s.enc").read_bytes()[:0] and (tmp_path / "s.enc").stat().st_mode & 0o077 == 0
    wrong = O.build_provider("https://x.test", tmp_path / "s.enc", Fernet.generate_key().decode())
    assert wrong.credentials_for_token(tok.access_token) is None
    rt = await prov.load_refresh_token(client, tok.refresh_token)
    new = await prov.exchange_refresh_token(client, rt, ["kap"])
    assert new.access_token != tok.access_token and await prov.load_refresh_token(client, tok.refresh_token) is None
    await prov.revoke_token(await prov.load_access_token(new.access_token))
    assert prov.credentials_for_token(new.access_token) is None
