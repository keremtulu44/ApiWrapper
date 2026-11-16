"""Ölçüm sisteminin uçtan uca testleri: 401'de ne olduğu, hangi kimlikle gidildiği.

Canlı arızanın iki ayrı sebebi aynı belirtiyi üretiyordu (502 `upstream_unauthorized`):

* **H3** — `UPSTREAM_ACCESS_TOKEN` env'de sabitlenmiş ve bayat; çerez rotasyona
  girse bile `resolve_bearer_token` önceliği yüzünden istekler eski token'la
  gidiyor. Ölçüm: 401 sonrası çerezdeki taze token'la yapılan tek deneme **geçer**.
* **H2** — oturum hedef tarafında düşmüş; çerezde taze token yok ya da yenisi de
  reddediliyor. Ölçüm: deneme **geçmez**.

Bu testler iki durumu birbirinden ayıran mekanizmayı doğrular; ayrıca olay
defterinin ham token sızdırmadığını ve `/zaman` yüzeylerinin çalıştığını.
"""

from __future__ import annotations

import base64
import json
import time

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import session_timeline
from tests.conftest import TEST_API_KEY, UPSTREAM_DOMAIN, make_settings

STREAM_URL = f"https://{UPSTREAM_DOMAIN}/nextjs-api/stream/post-to-evaluation/"
DEAD_BODY = b'{"message":"User not found"}'
OK_BODY = b'0:merhaba\n2:[{"messageId":"m"}]\n'


def _jwt(*, tag: str, ttl: int = 1200, age: int = 0) -> str:
    issued = int(time.time()) - age

    def part(payload: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")

    return (
        f"{part({'alg': 'HS256'})}."
        f"{part({'sub': 'u', 'tag': tag, 'iat': issued, 'exp': issued + ttl})}."
        "signature"
    )


def _route(router: respx.Router):
    return router.post(url__startswith=STREAM_URL)


def _pinned_settings(**overrides):
    """Sabitlenmiş (bayat) bir `UPSTREAM_ACCESS_TOKEN` ile dolu tek hesap."""
    stale = _jwt(tag="bayat-pinned", ttl=1200, age=3600)
    base = {
        "upstream_access_token": stale,
        "upstream_cookie": f"arena-auth-prod-v1.0={stale}",
    }
    base.update(overrides)
    return make_settings(**base), stale


def _kinds() -> list[str]:
    return [str(event["kind"]) for event in session_timeline.snapshot(0)]


@pytest.fixture(autouse=True)
def _clean_timeline():
    session_timeline.reset()
    yield
    session_timeline.reset()


def _post(client: TestClient) -> httpx.Response:
    return client.post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
    )


# ------------------------------------------------------- H3: bayat sabitlenmiş token
@respx.mock
def test_stale_pinned_token_is_proven_by_a_recovered_retry():
    """401 → çerezdeki taze token'la deneme → 200. Suçlu bizim env değerimiz."""
    settings, stale = _pinned_settings()
    fresh = _jwt(tag="cerezden-taze", ttl=1200, age=0)
    route = _route(respx).mock(
        side_effect=[
            httpx.Response(
                401,
                content=DEAD_BODY,
                headers={"set-cookie": f"arena-auth-prod-v1.0={fresh}"},
            ),
            httpx.Response(200, content=OK_BODY),
        ]
    )

    with TestClient(create_app(settings)) as client:
        client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        response = _post(client)

    assert response.status_code == 200, response.text
    assert "merhaba" in response.text, "doğrulama denemesi tüketiciye şeffaf olmalı"
    assert route.call_count == 2

    sent_first = route.calls[0].request.headers["authorization"]
    sent_second = route.calls[1].request.headers["authorization"]
    assert stale in sent_first and fresh not in sent_first
    assert fresh in sent_second, "ikinci deneme çerezdeki taze token'la gitmeli"

    analysis = session_timeline.analysis()
    assert analysis["forensic"]["outcomes"] == {"recovered": 1}
    assert "H3 DOĞRULANDI" in analysis["verdict"]
    forensic = [e for e in session_timeline.snapshot(0) if e["kind"] == "forensic_retry"][0]
    assert forensic["stale_id"] == session_timeline.token_id(stale)
    assert forensic["fresh_id"] == session_timeline.token_id(fresh)
    assert forensic["first_status"] == 401 and forensic["status"] == 200


@respx.mock
def test_recovered_retry_records_the_identity_switch():
    """Olay defteri 'pinned ile gittik, sonra cookie'ye döndük' ayrımını göstermeli."""
    settings, _stale = _pinned_settings()
    fresh = _jwt(tag="taze-2", ttl=1200, age=0)
    _route(respx).mock(
        side_effect=[
            httpx.Response(
                401, content=DEAD_BODY, headers={"set-cookie": f"arena-auth-prod-v1.0={fresh}"}
            ),
            httpx.Response(200, content=OK_BODY),
        ]
    )

    with TestClient(create_app(settings)) as client:
        client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        _post(client)

    identities = [e for e in session_timeline.snapshot(0) if e["kind"] == "identity_used"]
    assert [event["source"] for event in identities] == ["pinned", "cookie"]
    assert identities[0]["id"] != identities[1]["id"], "ikinci deneme farklı token'la gitti"
    assert identities[1]["changed"] is True
    assert session_timeline.analysis()["identity_last_used"]["source"] == "cookie"


# ------------------------------------------------------------ H2: oturum gerçekten ölü
@respx.mock
def test_fresh_cookie_token_also_rejected_proves_a_dead_session():
    settings, _stale = _pinned_settings()
    fresh = _jwt(tag="taze-ama-olu", ttl=1200, age=0)
    route = _route(respx).mock(
        side_effect=[
            httpx.Response(
                401, content=DEAD_BODY, headers={"set-cookie": f"arena-auth-prod-v1.0={fresh}"}
            ),
            httpx.Response(401, content=DEAD_BODY),
        ]
    )

    with TestClient(create_app(settings)) as client:
        client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        response = _post(client)

    assert response.status_code == 502, response.text
    assert response.json()["error"]["code"] == "upstream_unauthorized"
    assert route.call_count == 2, "tek doğrulama denemesi yapılır, üçüncü istek yok"

    analysis = session_timeline.analysis()
    assert analysis["forensic"]["outcomes"] == {"rejected": 1}
    # İki 401 yanıtı geldi ama tek istek öldü: denemenin yanıtı ayrıca sayılmaz.
    assert analysis["upstream"]["deaths"] == 1
    assert "H2 DOĞRULANDI" in analysis["verdict"]


@respx.mock
def test_no_fresh_token_in_cookie_skips_the_retry():
    """Çerez döndü ama içinde taze token yok: deneme yapılmaz, tek istek atılır."""
    settings, _stale = _pinned_settings()
    route = _route(respx).mock(
        side_effect=[
            httpx.Response(401, content=DEAD_BODY, headers={"set-cookie": "tema=koyu"}),
            httpx.Response(200, content=OK_BODY),
        ]
    )

    with TestClient(create_app(settings)) as client:
        client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        response = _post(client)

    assert response.status_code == 502, response.text
    assert route.call_count == 1, "taze token yokken ikinci istek atılmamalı"

    analysis = session_timeline.analysis()
    assert analysis["forensic"]["outcomes"] == {"skipped_no_fresh_token": 1}
    assert "H2 DESTEKLENDİ" in analysis["verdict"]


@respx.mock
def test_forensic_retry_can_be_turned_off():
    settings, _stale = _pinned_settings(upstream_forensic_retry=False)
    route = _route(respx).mock(return_value=httpx.Response(401, content=DEAD_BODY))

    with TestClient(create_app(settings)) as client:
        client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        response = _post(client)

    assert response.status_code == 502
    assert route.call_count == 1
    assert session_timeline.analysis()["forensic"]["outcomes"] == {"skipped_disabled": 1}


@respx.mock
def test_only_one_forensic_attempt_per_request():
    """Deneme hakkı tektir: taze token her seferinde gelse de döngü uzamaz."""
    settings, _stale = _pinned_settings()
    fresh = _jwt(tag="taze-3", ttl=1200, age=0)
    route = _route(respx).mock(
        side_effect=[
            httpx.Response(
                401, content=DEAD_BODY, headers={"set-cookie": f"arena-auth-prod-v1.0={fresh}"}
            ),
            httpx.Response(401, content=DEAD_BODY),
            httpx.Response(200, content=OK_BODY),
        ]
    )

    with TestClient(create_app(settings)) as client:
        client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        response = _post(client)

    assert response.status_code == 502
    assert route.call_count == 2, "üçüncü yanıt hazır olsa bile deneme tekrarlanmaz"


# ------------------------------------------------- gölgeleme: canlı arızanın imzası
@respx.mock
def test_cookie_rotation_that_the_pinned_token_shadows_is_recorded():
    """Çerez rotasyona girer, env'deki sabit token yerinde kalır ⇒ istekler bayat gider.

    Canlı logdaki 1 saatlik `expired_seconds_ago` artışı tam olarak budur ve
    bugüne dek hiçbir log satırı bunu söylemiyordu.
    """
    pinned = _jwt(tag="env-de-sabit", ttl=1200, age=3600)
    cookie_old = _jwt(tag="cerez-eski", ttl=1200, age=1800)
    cookie_fresh = _jwt(tag="cerez-taze", ttl=1200, age=0)
    settings = make_settings(
        upstream_access_token=pinned,
        upstream_cookie=f"arena-auth-prod-v1.0={cookie_old}",
    )
    route = _route(respx).mock(
        return_value=httpx.Response(
            200,
            content=OK_BODY,
            headers={"set-cookie": f"arena-auth-prod-v1.0={cookie_fresh}"},
        )
    )

    with TestClient(create_app(settings)) as client:
        client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        response = _post(client)

    assert response.status_code == 200, response.text
    assert route.call_count == 1
    # İstek, çerezdeki taze token'la değil env'deki bayat token'la gitti.
    assert pinned in route.calls[0].request.headers["authorization"]

    kinds = _kinds()
    assert "token_rotated" in kinds, "çerez rotasyonu ölçülmeli"
    assert "pinned_token_shadowing" in kinds, "gölgeleme tespit edilmeli"
    shadow = [e for e in session_timeline.snapshot(0) if e["kind"] == "pinned_token_shadowing"][0]
    assert shadow["pinned_id"] == session_timeline.token_id(pinned)
    assert shadow["fresh_id"] == session_timeline.token_id(cookie_fresh)
    assert shadow["pinned_expired"] is True


@respx.mock
def test_working_token_older_than_its_ttl_refutes_the_expiry_theory():
    """Yaşı tasarlanmış ömrünü aşmış bir token'la istek geçiyorsa hedef yaşı denetlemiyor."""
    old_but_working = _jwt(tag="yasli-ama-calisiyor", ttl=1200, age=54000)
    settings = make_settings(
        upstream_access_token=old_but_working,
        upstream_cookie=f"arena-auth-prod-v1.0={old_but_working}",
    )
    _route(respx).mock(return_value=httpx.Response(200, content=OK_BODY))

    with TestClient(create_app(settings)) as client:
        client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        response = _post(client)

    assert response.status_code == 200, response.text
    upstream = session_timeline.analysis()["upstream"]
    assert upstream["max_working_age_s"] == pytest.approx(54000, abs=5)
    assert upstream["max_exp_overrun_s"] == pytest.approx(52800, abs=5)
    assert "H1 ÇÜRÜTÜLDÜ" in session_timeline.analysis()["verdict"]


# ----------------------------------------------------------------------- yüzeyler
def test_timeline_endpoint_reports_without_leaking_tokens(client: TestClient):
    token = _jwt(tag="uc-nokta-gizli", ttl=1200, age=100)
    session_timeline.note_request_identity(token=token, pinned_token=token)
    session_timeline.note_identity_rejected(status=401, body_class="user_not_found", token=token)

    response = client.get("/v1/admin/timeline", params={"limit": 10, "report_events": 5})

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "session_timeline"
    assert body["in_memory"] is True
    assert body["analysis"]["upstream"]["deaths"] == 1
    assert isinstance(body["events"], list) and body["events"]
    assert "OTURUM OLAY DEFTERİ" in body["report"]
    assert token not in response.text, "uç nokta ham token döndürmemeli"


def test_timeline_endpoint_respects_the_limit(client: TestClient):
    for index in range(20):
        session_timeline.record(f"olay-{index}")

    body = client.get("/v1/admin/timeline", params={"limit": 3}).json()

    assert len(body["events"]) == 3
    assert body["events"][-1]["kind"] == "olay-19"


def test_auth_diagnostics_exposes_the_forensics_summary(client: TestClient):
    """/tani'nin okuduğu uç nokta artık 'zaman içinde ne oldu'yu da taşıyor."""
    session_timeline.note_token_rotation(new_token=_jwt(tag="rotasyon", ttl=1200, age=0))

    body = client.get("/v1/admin/auth").json()

    forensics = body["identity_forensics"]
    assert forensics["rotations"] == 1
    assert forensics["forensic_retry_enabled"] is True
    assert "verdict" in forensics


def test_auth_diagnostics_requires_an_api_key(settings):
    with TestClient(create_app(settings)) as anonymous:
        response = anonymous.get("/v1/admin/timeline")

    assert response.status_code == 401
