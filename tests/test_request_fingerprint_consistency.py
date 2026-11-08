"""Paylaşılan cURL ile kodun ürettiği isteğin parmak izi tutarlılığı.

Bu modül, tarayıcıdan kopyalanan bir isteğin **biçimini** (başlık adları,
client hint'ler, gövde alanları) sentetik değerlerle modeler; gerçek alan adı,
gerçek oturum ve gerçek CAPTCHA token'ı kullanılmaz. Ölçtüğü şey tek bir
kuraldır: *bir tarayıcının asla üretmeyeceği* istek kombinasyonlarını kodumuz
üretmemeli — çünkü bot/CAPTCHA korumaları tam olarak bu tür çelişkileri
kovuyor.

Kanıtlanan hatalar (test edilerek giderildi):

1. `UPSTREAM_EXTRA_HEADERS` anahtarları büyük/küçük harf duyarlı bir `dict`te
   biriktiriliyordu. `Sec-CH-UA=...` yazıldığında `sec-ch-ua` (kodun
   hesapladığı) ile **iki satır** gönderiliyor ve `X-Deployment-Id` taze
   damgayla değiştirilemiyordu.
2. Donmuş client hint'leri başka bir `User-Agent` ile gönderilebiliyordu
   (ör. `UPSTREAM_USER_AGENT_2` olan hesap, yuva-1'in ipuçlarını taşıyordu).
3. `cx` oturuma bağlı bir alan olduğu halde, `cx` taşımayan yeni bir cURL
   uygulandığında eski oturumun `cx` değeri hayatta kalıyordu.
4. Sürüm damgası kanaryası, istemci çerezleri hesaba ait olsa bile başlık ve
   keşif ayarlarını **global** ayarlardan alıyordu.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from app.upstream.curl_import import build_settings, parse_curl
from app.upstream.deployment import DeploymentIdResolver
from app.upstream.header_store import format_extra_headers, parse_extra_headers
from app.upstream.headers import build_page_headers, build_stream_headers, client_hint_conflicts
from tests.conftest import make_settings

# UA/parametre dışında hiçbir değer gerçek değil.
CHROME_WIN_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)
EDGE_WIN_UA = CHROME_WIN_UA + " Edg/156.0.0.0"
ANDROID_UA = (
    "Mozilla/5.0 (Linux; Android 14; SM-S911B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Mobile Safari/537.36"
)
STAMP_STALE = "dpl_STALE0000000000"
STAMP_FRESH = "dpl_FRESH1111111111"


def _raw_header_names(request: httpx.Request) -> list[bytes]:
    return [name.lower() for name, _value in request.headers.raw]


# ------------------------------------------------ 1) başlık adı normalizasyonu
def test_extra_header_names_are_canonicalised_to_lower_case() -> None:
    parsed = parse_extra_headers('Sec-CH-UA-Mobile=?1; DNT=1; X-Deployment-Id=dpl_x')
    assert set(parsed) == {"sec-ch-ua-mobile", "dnt", "x-deployment-id"}
    assert parsed["sec-ch-ua-mobile"] == "?1"


def test_json_shaped_extra_headers_are_also_canonicalised() -> None:
    raw = json.dumps({"Sec-CH-UA": '"Chromium";v="156"', "DNT": "1"})
    assert set(parse_extra_headers(raw)) == {"sec-ch-ua", "dnt"}


def test_stream_request_never_sends_two_conflicting_client_hint_lines() -> None:
    """Karışık harfli override, hesaplanan ipucun *yerine* geçmeli (ikisi birden değil)."""
    # `sec-ch-ua` değeri `;` taşıdığı için importer bunu JSON biçiminde yazar;
    # karışık harfli ad da orada korunur.
    frozen = json.dumps(
        {"Sec-CH-UA": '"Chromium";v="156", "Google Chrome";v="156"', "DNT": "1"}
    )
    settings = make_settings(upstream_user_agent=CHROME_WIN_UA, upstream_extra_headers=frozen)
    headers = build_stream_headers(settings, "chat-1", include_cookie=False)
    request = httpx.Request("POST", settings.stream_url("chat-1"), headers=headers)
    names = _raw_header_names(request)
    assert names.count(b"sec-ch-ua") == 1, "aynı başlıktan iki satır gönderildi"
    assert names.count(b"dnt") == 1
    assert request.headers["sec-ch-ua"] == '"Chromium";v="156", "Google Chrome";v="156"'


def test_fresh_deployment_stamp_replaces_mixed_case_configured_header() -> None:
    settings = make_settings(
        upstream_user_agent=CHROME_WIN_UA,
        upstream_extra_headers=f"X-Deployment-Id={STAMP_STALE}",
    )
    headers = build_stream_headers(
        settings, "chat-1", include_cookie=False, deployment_id=STAMP_FRESH
    )
    request = httpx.Request("POST", settings.stream_url("chat-1"), headers=headers)
    values = [value for name, value in request.headers.raw if name.lower() == b"x-deployment-id"]
    assert values == [STAMP_FRESH.encode()], "donmuş damga taze damganın yerini alamadı"


def test_page_headers_do_not_duplicate_client_hints_either() -> None:
    settings = make_settings(
        upstream_user_agent=CHROME_WIN_UA,
        upstream_extra_headers='Sec-CH-UA-Platform="Windows"; Accept-Language=tr,en;q=0.9',
    )
    headers = build_page_headers(settings, include_cookie=False)
    request = httpx.Request("GET", settings.base_url, headers=headers)
    assert _raw_header_names(request).count(b"sec-ch-ua-platform") == 1
    assert _raw_header_names(request).count(b"accept-language") == 1


# ------------------------------------------- 2) UA ile çelişen donmuş ipuçları
def test_hint_conflict_is_detected_against_ua() -> None:
    """Masaüstü UA'sı + mobil ipucu: gerçek bir tarayıcı bunu asla göndermez."""
    conflicts = client_hint_conflicts(
        CHROME_WIN_UA, {"sec-ch-ua-mobile": "?1", "sec-ch-ua-platform": '"Android"'}
    )
    assert conflicts == {"sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"'}


def test_matching_frozen_hints_are_kept_untouched() -> None:
    """cURL ile UA aynı tarayıcıdan geldiğinde ipuçları **dokunulmadan** korunur."""
    settings = make_settings(
        upstream_user_agent=ANDROID_UA,
        upstream_extra_headers='sec-ch-ua-mobile=?1; sec-ch-ua-platform="Android"',
    )
    headers = build_stream_headers(settings, "chat-1", include_cookie=False)
    assert headers["sec-ch-ua-mobile"] == "?1"
    assert headers["sec-ch-ua-platform"] == '"Android"'


def test_conflicting_frozen_hints_are_replaced_by_ua_derived_values() -> None:
    settings = make_settings(
        upstream_user_agent=CHROME_WIN_UA,
        upstream_extra_headers='sec-ch-ua-mobile=?1; sec-ch-ua-platform="Android"',
    )
    headers = build_stream_headers(settings, "chat-1", include_cookie=False)
    assert headers["sec-ch-ua-mobile"] == "?0"
    assert headers["sec-ch-ua-platform"] == '"Windows"'
    assert len(headers) == len({key.lower() for key in headers})


def test_sec_ch_ua_brand_major_must_appear_in_the_user_agent() -> None:
    overrides = {"sec-ch-ua": '"Chromium";v="140", "Google Chrome";v="140"'}
    assert "sec-ch-ua" in client_hint_conflicts(CHROME_WIN_UA, overrides)
    matching = {"sec-ch-ua": '"Not:A-Brand";v="8", "Chromium";v="156", "Google Chrome";v="156"'}
    assert client_hint_conflicts(CHROME_WIN_UA, matching) == {}


def test_non_chromium_user_agent_keeps_its_own_hints() -> None:
    """UA'dan Chrome sürümü çıkarılamıyorsa ipucu hakkında hüküm verilmez."""
    firefox_ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:139.0) "
        "Gecko/20100101 Firefox/139.0"
    )
    assert client_hint_conflicts(firefox_ua, {"sec-ch-ua-mobile": "?1"}) == {}
    settings = make_settings(
        upstream_user_agent=firefox_ua,
        upstream_extra_headers='sec-ch-ua-mobile=?1; sec-ch-ua-platform="Android"',
    )
    headers = build_stream_headers(settings, "chat-1", include_cookie=False)
    assert headers["sec-ch-ua-mobile"] == "?1"


def test_autofix_can_be_disabled_for_deliberate_spoofing() -> None:
    settings = make_settings(
        upstream_user_agent=CHROME_WIN_UA,
        upstream_extra_headers='sec-ch-ua-mobile=?1; sec-ch-ua-platform="Android"',
        upstream_client_hint_autofix=False,
    )
    headers = build_stream_headers(settings, "chat-1", include_cookie=False)
    assert headers["sec-ch-ua-mobile"] == "?1"
    assert headers["sec-ch-ua-platform"] == '"Android"'


def test_edge_user_agent_and_edge_brands_are_consistent() -> None:
    brands = '"Chromium";v="156", "Not:A-Brand";v="8", "Microsoft Edge";v="156"'
    assert client_hint_conflicts(EDGE_WIN_UA, {"sec-ch-ua": brands}) == {}


def test_round_trip_through_format_extra_headers_stays_canonical() -> None:
    text = format_extra_headers({"Sec-CH-UA-Mobile": "?0"})
    assert "sec-ch-ua-mobile" in parse_extra_headers(text)


# ------------------------------------------------------ 3) kanarya: hesap ayarı
def test_canary_probe_uses_the_account_user_agent_not_the_global_one() -> None:
    """GET (keşif) ve POST (akış) aynı tarayıcıyı temsil etmeli."""

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.setdefault("agent", request.headers.get("user-agent", ""))
        return httpx.Response(200, headers={"x-deployment-id": STAMP_FRESH}, text="<html></html>")

    seen: dict[str, str] = {}
    global_settings = make_settings(
        upstream_user_agent=CHROME_WIN_UA,
        upstream_extra_headers=f"x-deployment-id={STAMP_STALE}",
    )
    account_settings = global_settings.model_copy(
        update={"upstream_user_agent": ANDROID_UA}
    )
    resolver = DeploymentIdResolver(global_settings)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reading = await resolver.refresh(
                client, "https://upstream.test", settings=account_settings
            )
        assert reading.value == STAMP_FRESH

    asyncio.run(run())
    assert seen["agent"] == ANDROID_UA, "kanarya GET'i başka hesabın UA'sıyla yapıldı"


def test_active_for_reports_the_account_own_configuration() -> None:
    """Damga yalnızca gönderiliyorsa tazelenir; hesap bazında sorulabilir olmalı."""
    settings = make_settings(upstream_extra_headers="")
    resolver = DeploymentIdResolver(settings)
    assert resolver.active_for(settings) is False
    with_stamp = settings.model_copy(
        update={"upstream_extra_headers": f"x-deployment-id={STAMP_STALE}"}
    )
    assert resolver.active_for(with_stamp) is True


def test_inactive_canary_keeps_the_configured_stamp() -> None:
    settings = make_settings(upstream_extra_headers="", deployment_id_refresh_enabled=False)
    resolver = DeploymentIdResolver(settings)

    async def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("kanarya kapalıyken ağ isteği yapılmamalı")

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reading = await resolver.refresh(client, "https://upstream.test")
        assert reading.source == "none" and reading.value is None

    asyncio.run(run())


# ----------------------------------------------------- 4) `cx` oturuma bağlıdır
def _curl_with_body(body: dict[str, object]) -> str:
    return (
        "curl 'https://svc.test/nextjs-api/stream/create-evaluation' "
        "-H 'cookie: sb-session=abc' "
        "-H 'user-agent: SyntheticBrowser/1.0' "
        f"--data-raw '{json.dumps(body)}'"
    )


def _base_body(**extra: object) -> dict[str, object]:
    body: dict[str, object] = {
        "id": "01a1bbb6-a61b-0000-0000-000000000001",
        "modelAId": "fake-model-1",
        "mode": "direct-battle",
        "userMessage": {"content": "selam"},
        "modality": "chat",
        "recaptchaV3Token": "fake-captcha-token",
    }
    body.update(extra)
    return body


def test_new_curl_without_cx_clears_the_previous_session_context() -> None:
    env, warnings, _model = build_settings(
        parse_curl(_curl_with_body(_base_body())),
        existing={"UPSTREAM_CX": "old-context", "UPSTREAM_CX_SEND": "true"},
    )
    assert env["UPSTREAM_CX"] == ""
    assert env["UPSTREAM_CX_SEND"] == "false"
    assert any("UPSTREAM_CX" in item for item in warnings)


def test_new_curl_with_cx_still_enables_the_field() -> None:
    env, _warnings, _model = build_settings(
        parse_curl(_curl_with_body(_base_body(cx="fake-context")))
    )
    assert env["UPSTREAM_CX"] == "fake-context"
    assert env["UPSTREAM_CX_SEND"] == "true"


def test_unparsable_body_does_not_clear_cx_without_evidence() -> None:
    raw = (
        "curl 'https://svc.test/nextjs-api/stream/create-evaluation' "
        "-H 'cookie: sb-session=abc' --data-raw 'bozuk json {'"
    )
    env, warnings, _model = build_settings(
        parse_curl(raw), existing={"UPSTREAM_CX_SEND": "true"}
    )
    assert "UPSTREAM_CX" not in env
    assert "UPSTREAM_CX_SEND" not in env
    assert any("JSON olarak ayrıştırılamadı" in item for item in warnings)


def test_runtime_curl_apply_clears_stale_cx() -> None:
    """Sıcak güncelleme yalnız çerezi değil, bağlam alanını da sıfırlamalı."""
    from app.upstream.curl_runtime import apply_curl_to_settings

    settings = make_settings(
        upstream_cx="old-session-context",
        upstream_cx_send=True,
        upstream_stream_path="/nextjs-api/stream/create-evaluation",
    )
    result = apply_curl_to_settings(
        settings,
        _curl_with_body(_base_body()),
        allow_path_change=True,
    )
    assert "upstream_cx" in result["fields"] or settings.upstream_cx == ""
    assert settings.upstream_cx == ""
    assert settings.upstream_cx_send is False


# ------------------------------------- K5 tamamlama: ingest de hesap ayarına bakar
def test_ingest_honours_the_account_settings_gate() -> None:
    """`ingest()` kararı **hesabın** başlıklarından verilmeli (globalden değil)."""
    page = httpx.Response(200, headers={"x-deployment-id": STAMP_FRESH}, text="<html></html>")

    # Global damga göndermiyor; hesap gönderiyor -> kanarya hesap için aktif.
    resolver = DeploymentIdResolver(make_settings(upstream_extra_headers=""))
    account = make_settings(upstream_extra_headers=f"x-deployment-id={STAMP_STALE}")
    assert resolver.ingest(page, "https://upstream.test", settings=account) == STAMP_FRESH
    assert resolver.cached("https://upstream.test") == STAMP_FRESH


def test_ingest_is_skipped_when_only_the_global_config_has_a_stamp() -> None:
    page = httpx.Response(200, headers={"x-deployment-id": STAMP_FRESH}, text="<html></html>")
    resolver = DeploymentIdResolver(
        make_settings(upstream_extra_headers=f"x-deployment-id={STAMP_STALE}")
    )
    account = make_settings(upstream_extra_headers="")
    assert resolver.ingest(page, "https://upstream.test", settings=account) is None
