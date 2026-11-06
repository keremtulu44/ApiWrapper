"""Hesap havuzu testleri: yükleme, seçim, AIMD, hesap geçişi ve admin ucu."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.account import Account, AccountPool, load_accounts
from app.services.session_manager import SessionManager
from app.upstream.auth import parse_cookie_header
from tests.conftest import TEST_API_KEY, UPSTREAM_DOMAIN, ai_stream, make_settings

STREAM_URL = f"https://{UPSTREAM_DOMAIN}/nextjs-api/stream/post-to-evaluation/"

WINDOW = 1200.0
COOLDOWN = 1200.0


def two_account_settings(**overrides):
    base = {
        "target_domain": UPSTREAM_DOMAIN,
        "upstream_cookie": "COOKIE-1",
        "recaptcha_provider": "static",
        "recaptcha_static_token": "RT-1",
        "upstream_account_name": "bir",
        "upstream_cookie_2": "COOKIE-2",
        "recaptcha_static_token_2": "RT-2",
        "upstream_account_2_name": "iki",
        "account_quota_window_seconds": WINDOW,
        "account_cooldown_seconds": COOLDOWN,
    }
    base.update(overrides)
    return make_settings(**base)


@pytest.fixture
def pool_client():
    app = create_app(two_account_settings(retry_max_attempts=1))
    with TestClient(app) as test_client:
        test_client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        yield test_client


def chat_body() -> dict:
    return {"model": "test-model", "messages": [{"role": "user", "content": "selam"}]}


def route_by_cookie(router: respx.Router, responses: dict[str, httpx.Response]):
    """Cookie değerine göre yanıt döndürür; beklenmeyen cookie testi patlatır.

    `httpx.Response` gövdesi tek kullanımlıktır: aynı nesne ikinci kez
    sunulursa boş akar. Bu yüzden her çağrıda status/gövde'den taze bir yanıt
    üretilir (devir testlerinde aynı cookie'ye birden çok istek düşebilir).
    """
    specs = {cookie: (r.status_code, r.content) for cookie, r in responses.items()}

    def handler(request: httpx.Request) -> httpx.Response:
        cookie = request.headers.get("cookie", "")
        assert cookie in specs, f"beklenmeyen cookie: {cookie!r}"
        status, content = specs[cookie]
        return httpx.Response(status, content=content)

    return router.post(url__startswith=STREAM_URL).mock(side_effect=handler)


# ----------------------------------------------------------------- yükleme
def test_loads_both_accounts_from_env():
    accounts = load_accounts(two_account_settings())
    assert [(a.slot, a.label) for a in accounts] == [(1, "bir"), (2, "iki")]
    assert accounts[1].cookie == "COOKIE-2"
    assert accounts[1].recaptcha_token == "RT-2"


def test_loads_per_account_domains_instead_of_discarding_them():
    settings = two_account_settings(
        target_domain="one.example",
        target_domain_2="https://two.example/",
        upstream_cookie_3="COOKIE-3",
        target_domain_3="three.example",
    )
    accounts = load_accounts(settings)
    assert [(a.slot, a.target_domain) for a in accounts] == [
        (1, "one.example"),
        (2, "two.example"),
        (3, "three.example"),
    ]


def test_unconfigured_slots_are_skipped():
    settings = two_account_settings(upstream_cookie_2="", recaptcha_static_token_2="")
    accounts = load_accounts(settings)
    assert [a.slot for a in accounts] == [1]


def test_single_account_setup_yields_one_entry():
    accounts = load_accounts(
        make_settings(target_domain=UPSTREAM_DOMAIN, upstream_cookie="C1")
    )
    assert [a.label for a in accounts] == ["hesap-1"]


def test_effective_settings_does_not_mutate_the_original():
    settings = two_account_settings()
    account = load_accounts(settings)[1]
    derived = account.effective_settings(settings)
    assert derived.upstream_cookie == "COOKIE-2"
    assert derived.recaptcha_static_token == "RT-2"
    assert settings.upstream_cookie == "COOKIE-1"
    assert settings.recaptcha_static_token == "RT-1"


def test_effective_settings_recomputes_urls_from_account_domain():
    settings = two_account_settings()
    account = load_accounts(settings)[0]
    derived = Account(
        slot=9, name="x", cookie="c", target_domain="baska.example.com"
    ).effective_settings(settings)
    assert derived.stream_url("CID").startswith("https://baska.example.com/")
    assert account.slot == 1  # orijinal değişmedi


# ------------------------------------------------------------------ seçim
def test_pick_prefers_the_least_used_account():
    pool = AccountPool(two_account_settings())
    assert pool.pick().label == "bir"
    pool.record_message(1)
    assert pool.pick().label == "iki"


def test_pick_respects_the_budget_as_a_preference():
    pool = AccountPool(two_account_settings(account_msg_budget=2))
    pool.record_message(1)
    pool.record_message(1)
    # 1. hesap bütçeyi doldurdu ama isteği engellemez; sadece sıra değişir.
    assert pool.pick().label == "iki"
    assert pool.effective_budget(1) == 2


def test_single_account_is_never_blocked_by_budget():
    pool = AccountPool(
        make_settings(
            target_domain=UPSTREAM_DOMAIN, upstream_cookie="C1", account_msg_budget=1
        )
    )
    for _ in range(5):
        pool.record_message(1)
    assert pool.pick().slot == 1


# --------------------------------------------------------------- cooldown
def test_cooled_account_is_skipped_and_returns_after_cooldown():
    pool = AccountPool(two_account_settings())
    now = 1_000.0
    pool.record_message(1, now=now)
    pool.report_quota(1, now=now)

    assert pool.cooldown_remaining(1, now=now) == pytest.approx(COOLDOWN)
    assert pool.pick(now=now).label == "iki"
    # Cooldown bitince 1. hesap yeniden adaydır; seçim en az kullanılandan yana
    # olduğu için penceresi boş olan 2. hesap tercih edilmeye devam eder.
    assert pool.cooldown_remaining(1, now=now + COOLDOWN + 1) == 0.0
    pool.record_message(2, now=now + COOLDOWN + 1)
    pool.record_message(2, now=now + COOLDOWN + 1)
    assert pool.pick(now=now + COOLDOWN + 1).label == "bir"


def test_pick_returns_none_when_every_account_is_cooling():
    pool = AccountPool(two_account_settings())
    pool.report_quota(1, now=1_000.0)
    pool.report_quota(2, now=1_000.0)
    assert pool.pick(now=1_000.0) is None
    assert pool.next_cooldown_end(now=1_000.0) == pytest.approx(COOLDOWN)


# ------------------------------------------------------------------- AIMD
def test_quota_hit_lowers_the_learned_limit():
    """N. mesajda kilitlendiysek gerçek sınır en fazla N-1'dir."""
    pool = AccountPool(two_account_settings(account_msg_budget=15))
    for _ in range(5):
        pool.record_message(1)
    pool.report_quota(1)
    assert pool.state(1).learned_limit == 4
    assert pool.effective_budget(1) == 4


def test_budget_grows_only_after_several_clean_windows():
    """Tavan, bütçe kadar mesaj kilit olmadan geçildikten sonra ve en fazla
    pencere başına bir kez artar (her mesajda değil)."""
    pool = AccountPool(
        two_account_settings(account_msg_budget=3, account_budget_growth_streak=2)
    )
    for _ in range(5):
        pool.record_message(1)
    assert pool.state(1).learned_limit == 0  # henüz ilk temiz pencere tamamlanmadı
    assert pool.state(1).clean_windows == 1

    pool.record_message(1)  # 6 = 3 * (1 + 1) -> ikinci temiz pencere
    assert pool.state(1).learned_limit == 4

    pool.record_message(1)  # aynı pencerede ikinci kez artmaz
    assert pool.state(1).learned_limit == 4


def test_budget_does_not_grow_on_every_message():
    """En kötü durumda (bütçe 1) bile tavan her mesajda değil, her 3 mesajda bir artar."""
    pool = AccountPool(
        two_account_settings(account_msg_budget=1, account_budget_growth_streak=3)
    )
    for _ in range(4):
        pool.record_message(1)
    assert pool.effective_budget(1) == 2  # 4 mesaj -> tek artış

    for _ in range(6):
        pool.record_message(1)
    # 10 mesaj sonunda tavan 10 değil, ~4 olmalı (her 3 mesajda +1).
    assert pool.effective_budget(1) <= 4


def test_quota_hit_resets_the_growth_streak():
    pool = AccountPool(
        two_account_settings(account_msg_budget=2, account_budget_growth_streak=3)
    )
    for _ in range(2):
        pool.record_message(1)
    assert pool.state(1).clean_windows == 1
    pool.report_quota(1)
    assert pool.state(1).clean_windows == 0

# ------------------------------------------------------- sohbet hesaba bağlı
@pytest.mark.asyncio
async def test_each_account_gets_its_own_upstream_chat():
    manager = SessionManager(make_settings(session_reuse=True))
    first = await manager.acquire(None, "cli_1", "bir")
    second = await manager.acquire(None, "cli_1", "iki")
    again = await manager.acquire(None, "cli_1", "bir")
    assert first.chat_id != second.chat_id
    assert first.chat_id == again.chat_id


# ------------------------------------------------- uçtan uca: hesap geçişi
@respx.mock
def test_quota_on_first_account_switches_to_the_second(pool_client):
    route = route_by_cookie(
        respx,
        {
            "COOKIE-1": httpx.Response(
                429, content=b'{"error":"upstream limit reached"}'
            ),
            "COOKIE-2": httpx.Response(200, content=ai_stream("ikinci hesap")),
        },
    )
    response = pool_client.post("/v1/chat/completions", json=chat_body())
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "ikinci hesap"
    assert route.call_count == 2

    body = json.loads(route.calls[1].request.content.decode())
    assert body["recaptchaV3Token"] == "RT-2"  # hesabın kendi captcha token'ı


@respx.mock
def test_no_switch_when_all_accounts_are_locked(pool_client):
    route = route_by_cookie(
        respx,
        {
            "COOKIE-1": httpx.Response(429, content=b'{"error":"upstream limit reached"}'),
            "COOKIE-2": httpx.Response(429, content=b'{"error":"upstream limit reached"}'),
        },
    )
    response = pool_client.post("/v1/chat/completions", json=chat_body())
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "upstream_quota_reached"
    assert route.call_count == 2  # iki hesap denendi, fazlası değil

    # Sonraki istek hiç upstream'e gitmez: iki hesap da dinlenmede.
    second = pool_client.post("/v1/chat/completions", json=chat_body())
    assert second.status_code == 429
    assert "temporarily locked" in second.json()["error"]["message"]
    assert route.call_count == 2


@respx.mock
def test_invalid_messages_stay_400_when_all_accounts_are_locked(pool_client):
    route = route_by_cookie(
        respx,
        {
            "COOKIE-1": httpx.Response(429, content=b'{"error":"upstream limit reached"}'),
            "COOKIE-2": httpx.Response(429, content=b'{"error":"upstream limit reached"}'),
        },
    )
    locked = pool_client.post("/v1/chat/completions", json=chat_body())
    assert locked.status_code == 429

    invalid = pool_client.post(
        "/v1/chat/completions", json={"model": "test-model", "messages": []}
    )

    assert invalid.status_code == 400
    assert invalid.json()["error"]["param"] == "messages"
    assert route.call_count == 2  # Geçersiz istek yeni upstream denemesi başlatmaz.


# ------------------------------------------------ metin mesajı ile gelen kota
# Gerçek hedef kısıtlamayı HTTP hatası olarak değil, *akışın içinde metin*
# olarak gönderiyor. Kota tespiti çağıran tarafta yapıldığı sürece istisna
# `_events`'in `except UpstreamQuotaExceeded` bloğunun dışında fırlıyordu:
# istemci 429 görüyordu ama hesap ne dinlenmeye alınıyor ne de bütçe
# öğreniliyordu. Bu iki test o boşluğu kilitler.
QUOTA_TEXT = "upstream limit reached"


def text_scan_client() -> TestClient:
    app = create_app(
        two_account_settings(retry_max_attempts=1, quota_text_scan_chars=200)
    )
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
    return client


@respx.mock
def test_text_quota_in_stream_switches_account_and_learns_limit():
    with text_scan_client() as client:
        route = route_by_cookie(
            respx,
            {
                "COOKIE-1": httpx.Response(200, content=ai_stream(QUOTA_TEXT)),
                "COOKIE-2": httpx.Response(200, content=ai_stream("ikinci hesap")),
            },
        )
        response = client.post("/v1/chat/completions", json=chat_body())

        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "ikinci hesap"
        assert route.call_count == 2

        accounts = client.get("/v1/admin/accounts").json()["accounts"]
        first = next(a for a in accounts if a["slot"] == 1)
        # Devir ancak `report_quota` çağrıldıysa olur: dinlenme + öğrenilen sınır.
        # Mock `f:` + tek delta gönderir; bu TEK upstream mesajıdır, dolayısıyla
        # pencere 1 ve AIMD kuralı `max(1, pencere - 1)` = 1.
        assert first["quota_hits"] == 1
        assert first["messages_in_window"] == 1
        assert first["learned_limit"] == 1
        # Yanıt/admin isteği sırasında birkaç milisaniye geçmesi normaldir.
        assert first["cooldown_remaining_seconds"] == pytest.approx(COOLDOWN, abs=1.0)
        # Kota metni istemciye sızmamalı.
        assert QUOTA_TEXT not in response.json()["choices"][0]["message"]["content"]


@respx.mock
def test_text_quota_locks_both_accounts():
    with text_scan_client() as client:
        quota = httpx.Response(200, content=ai_stream(QUOTA_TEXT))
        route = route_by_cookie(respx, {"COOKIE-1": quota, "COOKIE-2": quota})

        response = client.post("/v1/chat/completions", json=chat_body())
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "upstream_quota_reached"
        assert route.call_count == 2

        body = client.get("/v1/admin/accounts").json()
        assert all(a["quota_hits"] == 1 for a in body["accounts"])

        # İki hesap da dinlenmede: sonraki istek upstream'e hiç gitmez.
        second = client.post("/v1/chat/completions", json=chat_body())
        assert second.status_code == 429
        assert "temporarily locked" in second.json()["error"]["message"]
        assert route.call_count == 2


@respx.mock
def test_text_quota_after_first_content_does_not_switch_but_is_reported():
    """İstemciye içerik gittiyse devir yapılmaz; kota yine de raporlanır.

    Devir ancak *hiç içerik gönderilmediyse* güvenlidir — aksi halde iki farklı
    yanıtın parçaları birbirine karışırdı. Kota yine hesaba yazılır ki bütçe
    öğrenilsin ve hesap dinlenmeye alınsın.
    """
    with text_scan_client() as client:
        route = route_by_cookie(
            respx,
            {
                "COOKIE-1": httpx.Response(
                    200, content=ai_stream("normal cevap ", QUOTA_TEXT)
                ),
                "COOKIE-2": httpx.Response(200, content=ai_stream("ikinci hesap")),
            },
        )
        response = client.post("/v1/chat/completions", json=chat_body())

        assert response.status_code == 429
        assert response.json()["error"]["code"] == "upstream_quota_reached"
        assert route.call_count == 1  # devir yok: içerik zaten yoldaydı

        accounts = client.get("/v1/admin/accounts").json()["accounts"]
        first = next(a for a in accounts if a["slot"] == 1)
        second = next(a for a in accounts if a["slot"] == 2)
        assert first["quota_hits"] == 1  # raporlandı
        assert first["learned_limit"] == 1
        assert second["quota_hits"] == 0  # ikinci hesap hiç denenmedi


@respx.mock
def test_start_event_does_not_block_account_switch():
    """`f:` (messageId) olayı devri engellememeli.

    Gerçek upstream her akışı `f:` ile açar; bu olay istemciye içerik
    taşımaz. Onu "gönderildi" saymak, kota ilk metin delta'sında geldiğinde
    devri tamamen kapatıyordu.
    """
    with text_scan_client() as client:
        body = b'f:{"messageId":"msg-1"}\n' + ai_stream(QUOTA_TEXT)
        route = route_by_cookie(
            respx,
            {
                "COOKIE-1": httpx.Response(200, content=body),
                "COOKIE-2": httpx.Response(200, content=ai_stream("ikinci hesap")),
            },
        )
        response = client.post("/v1/chat/completions", json=chat_body())

        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "ikinci hesap"
        assert route.call_count == 2


@respx.mock
def test_reasoning_deltas_do_not_block_account_switch():
    """Düşünme (thinking) modelleri devri engellememeli.

    Bu modeller cevap metninden önce `g:` düşünme delta'ları gönderir. O
    delta'lar istemciye iletilmediği için "gönderildi" sayılmamalı; aksi halde
    istemci tek bayt almamışken `emitted > 0` olur ve kota geldiğinde ikinci
    hesaba geçilemezdi.
    """
    with text_scan_client() as client:
        thinking = (
            'f:{"messageId":"msg-think"}\n'
            'g:"önce sorunu parçalara ayırıyorum"\n'
            'g:"şimdi kodu yazabilirim"\n'
        ).encode() + ai_stream(QUOTA_TEXT)
        route = route_by_cookie(
            respx,
            {
                "COOKIE-1": httpx.Response(200, content=thinking),
                "COOKIE-2": httpx.Response(200, content=ai_stream("ikinci hesap")),
            },
        )
        response = client.post("/v1/chat/completions", json=chat_body())

        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "ikinci hesap"
        assert route.call_count == 2
        assert "parçalara" not in response.json()["choices"][0]["message"]["content"]

        # Kota yine ilk hesaba yazılmalı.
        accounts = client.get("/v1/admin/accounts").json()["accounts"]
        assert next(a for a in accounts if a["slot"] == 1)["quota_hits"] == 1


@respx.mock
def test_switch_limit_is_honoured(pool_client):
    """account_max_switches=2 ile en fazla 3 hesap denenir (burada 2 hesap var)."""
    route = route_by_cookie(
        respx,
        {
            "COOKIE-1": httpx.Response(429, content=b'{"error":"upstream limit reached"}'),
            "COOKIE-2": httpx.Response(429, content=b'{"error":"upstream limit reached"}'),
        },
    )
    pool_client.post("/v1/chat/completions", json=chat_body())
    assert route.call_count == 2


@respx.mock
def test_each_account_uses_its_own_cookie_and_token(pool_client):
    route = route_by_cookie(
        respx,
        {
            "COOKIE-1": httpx.Response(200, content=ai_stream("bir")),
            "COOKIE-2": httpx.Response(200, content=ai_stream("iki")),
        },
    )
    for _ in range(4):
        assert pool_client.post("/v1/chat/completions", json=chat_body()).status_code == 200

    cookies = [call.request.headers.get("cookie") for call in route.calls]
    tokens = [
        json.loads(call.request.content.decode())["recaptchaV3Token"]
        for call in route.calls
    ]
    assert set(cookies) == {"COOKIE-1", "COOKIE-2"}
    assert set(tokens) == {"RT-1", "RT-2"}
    # Cookie ile token eşleşmeli (A hesabının token'ı B'ye gitmemeli).
    pairs = set(zip(cookies, tokens, strict=True))
    assert pairs == {("COOKIE-1", "RT-1"), ("COOKIE-2", "RT-2")}


@respx.mock
def test_each_account_gets_a_separate_upstream_chat():
    settings = two_account_settings(session_reuse=True)
    client = TestClient(create_app(settings))
    route = route_by_cookie(
        respx,
        {
            "COOKIE-1": httpx.Response(200, content=ai_stream("bir")),
            "COOKIE-2": httpx.Response(200, content=ai_stream("iki")),
        },
    )
    with client as test_client:
        test_client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        for _ in range(4):
            test_client.post("/v1/chat/completions", json=chat_body())

    per_cookie: dict[str, set[str]] = {}
    for call in route.calls:
        cookie = call.request.headers.get("cookie")
        per_cookie.setdefault(cookie, set()).add(call.request.url.path)
    assert len(per_cookie) == 2
    assert all(len(paths) == 1 for paths in per_cookie.values())
    assert settings.session_reuse is True
    # İki hesap farklı upstream sohbetlerine yazmış olmalı.
    all_paths = {p for paths in per_cookie.values() for p in paths}
    assert len(all_paths) == 2


@respx.mock
def test_warmup_uses_the_selected_account_domain_and_cookie():
    settings = two_account_settings(
        target_domain="one.test",
        upstream_cookie="auth=ONE",
        target_domain_2="two.test",
        upstream_cookie_2="auth=TWO",
    )
    warmups: list[httpx.Request] = []
    posts: list[httpx.Request] = []
    respx.get(url__startswith="https://").mock(
        side_effect=lambda request: (warmups.append(request), httpx.Response(200))[1]
    )
    respx.post(url__startswith="https://").mock(
        side_effect=lambda request: (
            posts.append(request),
            httpx.Response(200, content=ai_stream("ok")),
        )[1]
    )
    with TestClient(create_app(settings)) as client:
        client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        for _ in range(2):
            response = client.post("/v1/chat/completions", json=chat_body())
            assert response.status_code == 200

    assert [(r.url.host, r.headers.get("cookie")) for r in warmups] == [
        ("one.test", "auth=ONE"),
        ("two.test", "auth=TWO"),
    ]
    assert [(r.url.host, r.headers.get("cookie")) for r in posts] == [
        ("one.test", "auth=ONE"),
        ("two.test", "auth=TWO"),
    ]


@respx.mock
def test_cookie_jar_does_not_leak_from_one_account_into_another():
    settings = two_account_settings(
        target_domain="same.test",
        upstream_cookie="auth=ONE",
        upstream_cookie_2="",
        upstream_access_token_2="ACCESS-2",
    )
    warmups: list[httpx.Request] = []
    posts: list[httpx.Request] = []
    def warmup_handler(request: httpx.Request) -> httpx.Response:
        warmups.append(request)
        # Yalnızca birinci hesap Set-Cookie alır; ikinci hesabın kendi cevabı
        # aynı çerezi kurarsa izolasyonu yanlışlıkla testin kendisi bozar.
        headers = {"set-cookie": "jar=ONE; Path=/"} if len(warmups) == 1 else {}
        return httpx.Response(200, headers=headers)

    respx.get(url__startswith="https://").mock(side_effect=warmup_handler)
    respx.post(url__startswith="https://").mock(
        side_effect=lambda request: (
            posts.append(request),
            httpx.Response(200, content=ai_stream("ok")),
        )[1]
    )
    with TestClient(create_app(settings)) as client:
        client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        for _ in range(2):
            response = client.post("/v1/chat/completions", json=chat_body())
            assert response.status_code == 200

    first_cookies = parse_cookie_header(posts[0].headers.get("cookie", ""))
    # Başlangıç cookie'si korunur ve warm-up Set-Cookie'si aynı hesabın POST'una eklenir.
    assert first_cookies["auth"] == "ONE"
    assert first_cookies["jar"] == "ONE"
    # İkinci hesap authorization-only: birinci hesabın jar=ONE çerezi taşınmamalı.
    assert posts[1].headers.get("cookie") is None
    assert warmups[1].headers.get("cookie") is None


# --------------------------------------------------------- CAPTCHA yaşam döngüsü
@respx.mock
def test_static_captcha_rejection_is_not_retried_with_the_same_token(pool_client):
    route = route_by_cookie(
        respx,
        {
            "COOKIE-1": httpx.Response(403, content=b'{"error":"invalid recaptcha"}'),
            "COOKIE-2": httpx.Response(200, content=ai_stream("ikinci hesap")),
        },
    )
    response = pool_client.post("/v1/chat/completions", json=chat_body())
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "recaptcha_rejected"
    # Sağlayıcı static ise hata mesajı taze cURL eylemini önermeli.
    assert "fresh browser cURL" in body["error"]["message"]
    assert route.call_count == 1


@respx.mock
def test_static_captcha_rejection_clears_account_level_cached_tokens():
    """Red sonrası account'ların eski captcha_token'ı temizlenmeli ki sonraki
    istek / yeni cURL aynı ölü token'ı tekrar kullanmasın."""
    app = create_app(two_account_settings())
    with TestClient(app) as client:
        client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        # Başlangıçta account'lar RT-1 / RT-2 ile dolu olmalı.
        assert [a.recaptcha_token for a in app.state.accounts.accounts] == ["RT-1", "RT-2"]
        route = route_by_cookie(
            respx,
            {"COOKIE-1": httpx.Response(403, content=b'{"error":"invalid recaptcha"}')},
        )
        resp = client.post("/v1/chat/completions", json=chat_body())
        assert resp.status_code == 503
        assert route.call_count == 1
        # Red sonrası account-level token'lar temizlenmiş olmalı.
        assert [a.recaptcha_token for a in app.state.accounts.accounts] == ["", ""]


class SequenceRecaptchaProvider:
    """Tarayıcı/harici servis olmadan dinamik token yenilemeyi sınar."""

    name = "external"

    def __init__(self):
        self.tokens = ["LIVE-1", "LIVE-2"]
        self.calls = 0
        self.invalidations = 0

    async def startup(self):
        return None

    async def shutdown(self):
        return None

    async def get_token(self):
        token = self.tokens[min(self.calls, len(self.tokens) - 1)]
        self.calls += 1
        return token

    def invalidate(self):
        self.invalidations += 1


@respx.mock
def test_dynamic_captcha_provider_overrides_stale_account_tokens_and_refreshes(
    monkeypatch,
):
    provider = SequenceRecaptchaProvider()
    monkeypatch.setattr("app.main.create_recaptcha_provider", lambda _settings: provider)
    app = create_app(two_account_settings(recaptcha_provider="external"))
    seen_tokens = []

    def handler(request: httpx.Request) -> httpx.Response:
        token = json.loads(request.content.decode())["recaptchaV3Token"]
        seen_tokens.append(token)
        if token == "LIVE-1":
            return httpx.Response(403, content=b'{"error":"invalid recaptcha"}')
        return httpx.Response(200, content=ai_stream("taze token"))

    route = respx.post(url__startswith=STREAM_URL).mock(side_effect=handler)
    with TestClient(app) as client:
        client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        response = client.post("/v1/chat/completions", json=chat_body())

    assert response.status_code == 200
    assert seen_tokens == ["LIVE-1", "LIVE-2"]
    assert "RT-1" not in seen_tokens  # Hesaptaki eski statik değer sağlayıcıyı ezemez.
    assert provider.invalidations == 1
    assert route.call_count == 2


# ------------------------------------------------------------------ admin
@respx.mock
def test_one_message_recorded_per_upstream_request(pool_client):
    """Bir upstream isteği pencereye tam BİR mesaj yazmalı.

    `emitted` yalnızca istemciye içerik taşıyan TEXT olaylarında artar; `f:`
    (START) olayında artmaz. Bu yüzden `emitted == 0` hem `f:` hem ilk TEXT
    için doğru kalıyor ve her istek iki mesaj sayılıyordu — kota penceresi
    fiilen yarıya iniyordu.
    """
    ok = httpx.Response(200, content=ai_stream("bir ", "iki ", "üç"))
    route = route_by_cookie(respx, {"COOKIE-1": ok, "COOKIE-2": ok})

    for _ in range(3):
        assert pool_client.post("/v1/chat/completions", json=chat_body()).status_code == 200
    assert route.call_count == 3

    body = pool_client.get("/v1/admin/accounts").json()
    assert sum(a["total_messages"] for a in body["accounts"]) == 3
    assert sum(a["messages_in_window"] for a in body["accounts"]) == 3


def test_admin_accounts_lists_pool_without_secrets(pool_client):
    body = pool_client.get("/v1/admin/accounts").json()
    assert body["configured_accounts"] == 2
    names = {a["name"] for a in body["accounts"]}
    assert names == {"bir", "iki"}
    text = json.dumps(body)
    assert "COOKIE-1" not in text and "RT-1" not in text
    for account in body["accounts"]:
        assert account["has_cookie"] is True
        assert account["messages_in_window"] == 0


def test_admin_account_cooldown_reset(pool_client):
    pool = pool_client.app.state.accounts
    pool.report_quota(1)
    assert pool.cooldown_remaining(1) > 0

    response = pool_client.post("/v1/admin/accounts/1/reset")
    assert response.status_code == 200
    assert pool.cooldown_remaining(1) == 0.0

    missing = pool_client.post("/v1/admin/accounts/99/reset")
    assert missing.status_code == 400


def test_metrics_include_account_switches(pool_client):
    pool = pool_client.app.state.accounts
    pool.report_quota(1)
    text = pool_client.get("/metrics").text
    assert "apiwrapper_account_switches_total" in text
    assert "apiwrapper_account_cooldown" in text
