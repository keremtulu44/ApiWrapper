"""Katman kararı (`/tani` → `verdict`) testleri.

Amaç: operatörün yanlış katmana bakmasını önlemek. Belirti aynı, sebep dört
farklı katmanda olabilir; karar her durumda **tek** katmanı işaret etmeli.
"""

from __future__ import annotations

import base64
import json
import time

from app.services.layer_verdict import build_layer_verdict
from tests.conftest import TEST_API_KEY, make_settings


def _jwt(*, exp_offset: int = 3600, ttl: int | None = None) -> str:
    def seg(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    now = int(time.time())
    payload: dict[str, object] = {"exp": now + exp_offset}
    if ttl is not None:
        payload["iat"] = now + exp_offset - ttl
    return f"{seg({'alg': 'ES256'})}.{seg(payload)}.sig"


def _cookie(*, access: str | None = None, refresh: str | None = "rt-1") -> str:
    body: dict[str, object] = {}
    if access is not None:
        body["access_token"] = access
    if refresh is not None:
        body["refresh_token"] = refresh
    body["expires_in"] = 1200
    value = "base64-" + base64.b64encode(json.dumps(body).encode()).decode()
    return f"arena-auth-prod-v1={value}"


def _ids(verdict: object) -> dict[str, str]:
    return {c["id"]: c["status"] for c in verdict.as_dict()["checks"]}  # type: ignore[attr-defined]


def _by_id(verdict: object, check_id: str) -> dict:
    for check in verdict.as_dict()["checks"]:  # type: ignore[attr-defined]
        if check["id"] == check_id:
            return check
    raise AssertionError(f"{check_id} kontrolü yok")


def test_unconfigured_domain_wins_over_everything_else():
    settings = make_settings(target_domain="localhost", upstream_cookie=_cookie(access=_jwt()))
    verdict = build_layer_verdict(settings)

    assert verdict.layer == "config"
    assert "TARGET_DOMAIN" in verdict.summary or "host" in verdict.summary
    assert _by_id(verdict, "target_domain")["status"] == "fail"


def test_expired_token_points_at_identity_layer():
    settings = make_settings(
        target_domain="upstream.example",
        upstream_cookie=_cookie(access=_jwt(exp_offset=-120)),
        upstream_auth_from_cookie=True,
    )
    verdict = build_layer_verdict(settings)

    assert verdict.layer == "identity"
    assert _by_id(verdict, "access_token")["status"] == "fail"
    assert "süresi dolmuş" in verdict.summary


def test_healthy_configuration_reports_ok():
    settings = make_settings(
        target_domain="upstream.example",
        upstream_cookie=_cookie(access=_jwt(ttl=1200)),
        upstream_auth_from_cookie=True,
        upstream_refresh_api_key="public-anon-key",
        upstream_refresh_leeway_seconds=180,
    )
    verdict = build_layer_verdict(settings, recaptcha_stats={"generated": 0, "rejected": 0})

    assert verdict.layer == "ok", verdict.summary
    assert all(c["status"] == "ok" for c in verdict.as_dict()["checks"])


def test_missing_refresh_api_key_is_a_warning_not_a_failure():
    settings = make_settings(
        target_domain="upstream.example",
        upstream_cookie=_cookie(access=_jwt(ttl=1200)),
        upstream_auth_from_cookie=True,
        upstream_refresh_leeway_seconds=180,
    )
    verdict = build_layer_verdict(settings)

    assert verdict.layer == "identity"
    assert _ids(verdict)["refresh_api_key"] == "warn"
    assert _ids(verdict)["access_token"] == "ok"


def test_leeway_half_of_ttl_is_flagged():
    settings = make_settings(
        target_domain="upstream.example",
        upstream_cookie=_cookie(access=_jwt(ttl=1200)),
        upstream_auth_from_cookie=True,
        upstream_refresh_api_key="k",
        upstream_refresh_leeway_seconds=600,
    )
    verdict = build_layer_verdict(settings)

    assert _ids(verdict)["refresh_leeway"] == "warn"


def test_rejected_captcha_points_at_captcha_layer():
    settings = make_settings(
        target_domain="upstream.example",
        upstream_cookie=_cookie(access=_jwt(ttl=1200)),
        upstream_auth_from_cookie=True,
        upstream_refresh_api_key="k",
        upstream_refresh_leeway_seconds=180,
        recaptcha_provider="browser",
    )
    verdict = build_layer_verdict(
        settings, recaptcha_stats={"generated": 3, "rejected": 2, "last_error": None}
    )

    assert verdict.layer == "captcha"
    assert "Üretim çalışıyor" in _by_id(verdict, "recaptcha")["detail"]


def test_captcha_production_error_is_failure():
    settings = make_settings(
        target_domain="upstream.example",
        upstream_cookie=_cookie(access=_jwt(ttl=1200)),
        upstream_auth_from_cookie=True,
        upstream_refresh_api_key="k",
        upstream_refresh_leeway_seconds=180,
        recaptcha_provider="browser",
    )
    verdict = build_layer_verdict(
        settings,
        recaptcha_stats={"generated": 0, "rejected": 0, "last_error": "playwright missing"},
    )

    assert verdict.layer == "captcha"
    assert _by_id(verdict, "recaptcha")["status"] == "fail"


def test_stale_configured_stamp_is_reported():
    settings = make_settings(
        target_domain="upstream.example",
        upstream_cookie=_cookie(access=_jwt(ttl=1200)),
        upstream_auth_from_cookie=True,
        upstream_refresh_api_key="k",
        upstream_refresh_leeway_seconds=180,
        upstream_extra_headers="x-deployment-id=dpl_old",
    )
    verdict = build_layer_verdict(settings, deployment_stamp="dpl_new")

    assert verdict.layer == "deployment"
    assert _by_id(verdict, "deployment_stamp")["status"] == "warn"


def test_frozen_cx_is_flagged():
    settings = make_settings(
        target_domain="upstream.example",
        upstream_cookie=_cookie(access=_jwt(ttl=1200)),
        upstream_auth_from_cookie=True,
        upstream_refresh_api_key="k",
        upstream_refresh_leeway_seconds=180,
        upstream_cx_send=True,
        upstream_cx="AmswDKZJppFLZV2icYKQ",
    )
    verdict = build_layer_verdict(settings)

    assert _ids(verdict)["cx_send"] == "warn"
    assert verdict.layer == "config"


def test_admin_auth_endpoint_exposes_verdict(client, settings):
    response = client.get("/v1/admin/auth", headers={"Authorization": f"Bearer {TEST_API_KEY}"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert "verdict" in body
    assert body["verdict"]["layer"] in {
        "config",
        "identity",
        "deployment",
        "captcha",
        "ok",
    }
    assert isinstance(body["verdict"]["checks"], list)
