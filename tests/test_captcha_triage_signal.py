"""403 triyajı: "CAPTCHA olabilir" ile "CAPTCHA reddi kanıtlandı" ayrımı.

Canlı logda okunan satır `recaptcha_rejected` **ise** ret kanıtlıdır; değilse
hata jenerik görünür ve operatör yanlış katmana bakar. `CAPTCHA_MARKERS`
listesi bilinçli olarak özgü ifadeleri arar (genel "forbidden"/"bot"
kelimeleri herhangi bir yetki hatasında geçebiliyor), yani "gövde captcha'ya
benziyor ama eşleşmedi" hâli kodda iz bırakmıyordu.

Bu modül o izi doğrular: gövdenin **metni loglanmaz**, yalnızca hangi işaret
ailelerine benzediği bildirilir.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.upstream.client import UpstreamClient
from app.upstream.exceptions import RecaptchaRejected, UpstreamHTTPError
from tests.conftest import make_settings


def _client_for(status: int, body: str) -> UpstreamClient:
    settings = make_settings(deployment_id_refresh_enabled=False, retry_max_attempts=1)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body)

    upstream = UpstreamClient(settings)
    upstream._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return upstream


def test_body_signature_reports_marker_families_only() -> None:
    signature = UpstreamClient._body_signature(
        '{"error":"captcha challenge required","detail":"private-token-value"}'
    )
    assert signature == {
        "captcha_like": True,
        "bot_like": False,
        "verify_like": True,
        "forbidden_like": False,
    }


async def test_captcha_looking_403_without_marker_is_flagged_not_misreported(capsys) -> None:
    """`captcha` kelimesi var ama özgü ifade yok → jenerik hata + teşhis satırı."""
    secret = "kisiye-ozel-deger-9f3a"
    body = json.dumps({"error": "captcha challenged", "reference": secret})
    upstream = _client_for(403, body)
    try:
        with pytest.raises(UpstreamHTTPError) as caught:
            async with upstream.stream_completion("chat", {"id": "chat"}):
                pass  # pragma: no cover
        assert not isinstance(caught.value, RecaptchaRejected)
        log = capsys.readouterr().out
    finally:
        await upstream.shutdown()

    assert "upstream_error_unclassified" in log
    assert '"captcha_like": true' in log
    assert "recaptcha_rejected" not in log
    # Gövde ve içindeki gizli değer loga/koda sızmaz.
    assert secret not in log
    assert "captcha challenged" not in log


async def test_proven_captcha_rejection_keeps_its_own_signal(capsys) -> None:
    """Özgü ifade eşleşirse sınıflandırma değişir; ek triyaj satırı gürültü üretmez."""
    body = '{"error":"recaptcha verification failed"}'
    upstream = _client_for(403, body)
    try:
        with pytest.raises(RecaptchaRejected):
            async with upstream.stream_completion("chat", {"id": "chat"}):
                pass  # pragma: no cover
        log = capsys.readouterr().out
    finally:
        await upstream.shutdown()

    assert "upstream_recaptcha_rejected" in log
    assert "upstream_error_unclassified" not in log


async def test_empty_body_403_is_not_claimed_as_captcha(capsys) -> None:
    """Boş gövdeli 403 oturum işaretidir; `captcha_like` False kalmalı."""
    upstream = _client_for(403, "")
    try:
        with pytest.raises(UpstreamHTTPError):
            async with upstream.stream_completion("chat", {"id": "chat"}):
                pass  # pragma: no cover
        log = capsys.readouterr().out
    finally:
        await upstream.shutdown()

    assert '"captcha_like": false' in log
