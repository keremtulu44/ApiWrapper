"""`/v1/admin/*` yetki yüzeyi: normal istemci anahtarı oturumu değiştirememeli."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import TEST_API_KEY, make_settings

CLIENT_KEY = TEST_API_KEY
ADMIN_KEY = "sk-admin-rotator"


def _client(settings):
    return TestClient(create_app(settings))


def test_admin_keys_empty_keeps_legacy_behaviour():
    """ADMIN_API_KEYS boşken eski kurulumlar kırılmamalı."""
    settings = make_settings(api_keys=[CLIENT_KEY], admin_api_keys=[])
    with _client(settings) as client:
        response = client.post(
            "/v1/admin/upstream/apikey",
            json={"apikey": "anon"},
            headers={"Authorization": f"Bearer {CLIENT_KEY}"},
        )
    # Yetki kapısı devrede değil: istek uca ulaşır (burada yenileme başarısız).
    assert response.status_code == 200
    assert response.json()["status"] == "applied"


def test_client_key_cannot_swap_upstream_credentials_when_admin_keys_set():
    settings = make_settings(
        api_keys=[CLIENT_KEY, ADMIN_KEY],
        admin_api_keys=[ADMIN_KEY],
    )
    with _client(settings) as client:
        response = client.post(
            "/v1/admin/upstream/curl",
            json={"curl": "curl 'https://upstream.test/x' -H 'cookie: a=b'"},
            headers={"Authorization": f"Bearer {CLIENT_KEY}"},
        )
    assert response.status_code == 403
    body = response.json()
    assert body["error"]["code"] == "admin_key_required"  # type: ignore[index]
    # Gizli değer veya anahtar yanıta sızmamalı.
    assert CLIENT_KEY not in response.text
    assert ADMIN_KEY not in response.text


def test_admin_key_is_accepted_for_credential_endpoints():
    settings = make_settings(
        api_keys=[CLIENT_KEY, ADMIN_KEY],
        admin_api_keys=[ADMIN_KEY],
    )
    with _client(settings) as client:
        response = client.post(
            "/v1/admin/upstream/apikey",
            json={"apikey": "anon-key"},
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        )
    assert response.status_code == 200
    assert response.json()["status"] == "applied"


def test_read_only_admin_endpoints_stay_available_to_clients():
    """Teşhis uçları (durum okuma) normal anahtarla çalışmaya devam etmeli."""
    settings = make_settings(
        api_keys=[CLIENT_KEY, ADMIN_KEY],
        admin_api_keys=[ADMIN_KEY],
    )
    with _client(settings) as client:
        response = client.get(
            "/v1/admin/auth",
            headers={"Authorization": f"Bearer {CLIENT_KEY}"},
        )
    assert response.status_code == 200


def test_admin_gate_is_not_bypassed_by_missing_key():
    settings = make_settings(
        api_keys=[CLIENT_KEY, ADMIN_KEY],
        admin_api_keys=[ADMIN_KEY],
    )
    with _client(settings) as client:
        response = client.post("/v1/admin/upstream/cookie", json={"cookie": "a=b"})
    assert response.status_code == 401
