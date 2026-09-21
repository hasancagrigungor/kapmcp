import httpx
import pytest
import respx

pytestmark = pytest.mark.anyio

from kap_mcp.client import BASE_URL_PROD, BASE_URL_TEST, KAPClient
from kap_mcp.exceptions import KAPAPIError, KAPAuthenticationError, KAPValidationError


@pytest.fixture
def prod():
    return KAPClient(api_key="k", max_retries=2)


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(KAPClient, "_backoff", staticmethod(_noop))


@respx.mock
async def test_prod_generates_token_then_uses_bearer(prod):
    tok = respx.get(f"{BASE_URL_PROD}/auth/generateToken").respond(json={"token": "abc"})
    last = respx.get(f"{BASE_URL_PROD}/api/vyk/lastDisclosureIndex").respond(json={"lastDisclosureIndex": "1300000"})
    async with prod:
        assert await prod.get_last_disclosure_index() == 1300000
        assert await prod.get_last_disclosure_index() == 1300000
    assert tok.call_count == 1
    assert last.calls.last.request.headers["Authorization"] == "Bearer abc"


@respx.mock
async def test_prod_refreshes_token_once_on_401(prod):
    tokens = iter(["old", "new"])
    respx.get(f"{BASE_URL_PROD}/auth/generateToken").mock(side_effect=lambda r: httpx.Response(200, json={"token": next(tokens)}))

    def handler(request):
        if request.headers["Authorization"] == "Bearer old":
            return httpx.Response(401, json={"fault": {"faultCode": "OI001", "faultString": "expired"}})
        return httpx.Response(200, json={"lastDisclosureIndex": 5})

    respx.get(f"{BASE_URL_PROD}/api/vyk/lastDisclosureIndex").mock(side_effect=handler)
    async with prod:
        assert await prod.get_last_disclosure_index() == 5


@respx.mock
async def test_prod_persistent_401_raises_auth_error(prod):
    respx.get(f"{BASE_URL_PROD}/auth/generateToken").respond(json={"token": "t"})
    respx.get(f"{BASE_URL_PROD}/api/vyk/lastDisclosureIndex").respond(
        401, json={"fault": {"faultCode": "OI001", "faultString": "bad key", "correlationId": "c1"}}
    )
    async with prod:
        with pytest.raises(KAPAuthenticationError) as ei:
            await prod.get_last_disclosure_index()
    assert "OI001" in str(ei.value) and "c1" in str(ei.value)


@respx.mock
async def test_test_mode_uses_basic_auth_without_token():
    c = KAPClient(api_key="key", api_secret="sec", test_mode=True)
    route = respx.get(f"{BASE_URL_TEST}/api/vyk/lastDisclosureIndex").respond(json={"lastDisclosureIndex": 1})
    async with c:
        assert await c.get_last_disclosure_index() == 1
    assert route.calls.last.request.headers["Authorization"] == "Basic a2V5OnNlYw=="


@respx.mock
async def test_retries_on_503_then_succeeds(prod):
    respx.get(f"{BASE_URL_PROD}/auth/generateToken").respond(json={"token": "t"})
    route = respx.get(f"{BASE_URL_PROD}/api/vyk/members")
    route.side_effect = [httpx.Response(503), httpx.Response(200, json=[{"id": "1", "title": "A", "stockCode": "AAA"}])]
    async with prod:
        assert len(await prod.get_members()) == 1
    assert route.call_count == 2


@respx.mock
async def test_gives_up_after_max_retries(prod):
    respx.get(f"{BASE_URL_PROD}/auth/generateToken").respond(json={"token": "t"})
    route = respx.get(f"{BASE_URL_PROD}/api/vyk/members").respond(502)
    async with prod:
        with pytest.raises(KAPAPIError) as ei:
            await prod.get_members()
    assert ei.value.status_code == 502
    assert route.call_count == 3  # 1 + max_retries


@respx.mock
async def test_retries_network_errors(prod):
    respx.get(f"{BASE_URL_PROD}/auth/generateToken").respond(json={"token": "t"})
    route = respx.get(f"{BASE_URL_PROD}/api/vyk/blockedDisclosures")
    route.side_effect = [httpx.ConnectError("boom"), httpx.Response(200, json=[])]
    async with prod:
        assert await prod.get_blocked_disclosures() == []
    assert route.call_count == 2


@respx.mock
async def test_members_cached_and_find(prod):
    respx.get(f"{BASE_URL_PROD}/auth/generateToken").respond(json={"token": "t"})
    route = respx.get(f"{BASE_URL_PROD}/api/vyk/members").respond(
        json=[
            {"id": "1", "title": "TÜRK HAVA YOLLARI A.O.", "stockCode": "THYAO", "memberType": "IGS"},
            {"id": "2", "title": "THY TEKNİK", "stockCode": "", "memberType": "IGMS"},
            {"id": "3", "title": "X", "stockCode": "ABC, THYAOX", "memberType": "IGS"},
        ]
    )
    async with prod:
        hits = await prod.find_members("thyao")
        assert [h["id"] for h in hits] == ["1", "3"]
        hits = await prod.find_members("thy")
        assert [h["id"] for h in hits] == ["1", "3", "2"]
        await prod.get_members()
    assert route.call_count == 1


@respx.mock
async def test_disclosures_params_and_validation(prod):
    respx.get(f"{BASE_URL_PROD}/auth/generateToken").respond(json={"token": "t"})
    route = respx.get(f"{BASE_URL_PROD}/api/vyk/disclosures").respond(json=[{"disclosureIndex": "600000"}])
    async with prod:
        with pytest.raises(KAPValidationError):
            await prod.get_disclosures(1)
        with pytest.raises(KAPValidationError):
            await prod.get_disclosures(600000, disclosure_type="NOPE")
        out = await prod.get_disclosures(600000, disclosure_type="ODA", company_id="c1")
    assert out[0]["disclosureIndex"] == "600000"
    q = dict(route.calls.last.request.url.params)
    assert q == {"disclosureIndex": "600000", "disclosureTypes": "ODA", "companyId": "c1"}


@respx.mock
async def test_download_attachment(prod):
    respx.get(f"{BASE_URL_PROD}/auth/generateToken").respond(json={"token": "t"})
    respx.get(f"{BASE_URL_PROD}/api/vyk/downloadAttachment/abc").respond(
        content=b"%PDF", headers={"Content-Type": "application/pdf", "Content-Disposition": 'attachment; filename="r.pdf"'}
    )
    async with prod:
        content, ctype, name = await prod.download_attachment("abc")
        with pytest.raises(KAPValidationError):
            await prod.download_attachment("../x")
    assert (content, ctype, name) == (b"%PDF", "application/pdf", "r.pdf")


@respx.mock
async def test_non_json_body_is_reported(prod):
    respx.get(f"{BASE_URL_PROD}/auth/generateToken").respond(json={"token": "t"})
    respx.get(f"{BASE_URL_PROD}/api/vyk/fundDetail/1").respond(text="<html>oops</html>")
    async with prod:
        with pytest.raises(KAPAPIError) as ei:
            await prod.get_fund_detail(1)
    assert "Non-JSON" in str(ei.value)


def test_unwrap_java_byte_array():
    from kap_mcp.client import unwrap_java_byte_array

    raw = b"\xac\xed\x00\x05ur\x00\x02[B\xac\xf3\x17\xf8\x06\x08T\xe0\x02\x00\x00xp\x00\x00\x00\x08%PDF-1.5"
    assert unwrap_java_byte_array(raw) == b"%PDF-1.5"
    assert unwrap_java_byte_array(b"%PDF-plain") == b"%PDF-plain"


@respx.mock
async def test_empty_filtered_window_is_not_an_error(prod):
    respx.get(f"{BASE_URL_PROD}/auth/generateToken").respond(json={"token": "t"})
    respx.get(f"{BASE_URL_PROD}/api/vyk/disclosures").respond(
        400, json={"fault": {"faultCode": "ER005", "faultString": "Bildirim bulunamadı. Disclosure not found."}}
    )
    async with prod:
        assert await prod.get_disclosures(600000, disclosure_type="FR") == []
