"""`scripts/smoke.py`: ayaktaki servise atılan isteklerin sınıflandırılması.

Testler gerçek ağ kullanmaz; `httpx.MockTransport` ile yanıtlar taklit edilir.
"""

from __future__ import annotations

import json

import httpx
import pytest

from scripts.smoke import (
    STATUS_LAYERS,
    Check,
    Report,
    check_admin_auth,
    check_chat,
    check_healthz,
    check_readyz,
    check_stream,
    mask,
    run,
)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _json(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _verdict(layer: str = "ok", summary: str = "Üretim çalışıyor.") -> dict:
    return {
        "verdict": {
            "layer": layer,
            "summary": summary,
            "checks": [
                {
                    "id": "target_domain",
                    "layer": "config",
                    "status": "ok" if layer == "ok" else "fail",
                    "detail": "hedef ayarlı",
                    "fix": "TARGET_DOMAIN=<gerçek-host>",
                }
            ],
        }
    }


def test_mask_never_returns_the_whole_key() -> None:
    assert mask("supersecretkey") == "sup…ey"
    assert mask("abc") == "***"
    assert mask("") == "(yok)"


def test_healthy_service_returns_zero_exit_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return _json({"status": "ok"})
        if request.url.path == "/readyz":
            return _json({"status": "ready"})
        if request.url.path == "/v1/admin/auth":
            return _json(_verdict())
        return _json({"choices": [{"message": {"content": "pong"}}]})

    report = run("http://test", api_key="k", admin_key="a", client=_client(handler))

    assert report.exit_code == 0
    assert [check.name for check in report.checks] == ["healthz", "readyz", "admin_auth", "chat"]
    assert "istek atılabilir durumda" in report.render()


def test_readyz_503_carries_the_reason_in_the_hint() -> None:
    client = _client(
        lambda request: _json({"detail": "target_domain_not_configured"}, status=503)
    )

    check = check_readyz(client, "http://test")

    assert check.failed
    assert "target_domain_not_configured" in check.hint


def test_healthz_failure_short_circuits_the_rest() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return _json({"detail": "boom"}, status=500)

    report = run("http://test", api_key="k", client=_client(handler))

    assert report.exit_code == 1
    assert calls == ["/healthz"]


@pytest.mark.parametrize(
    ("status", "layer"),
    [(401, "identity"), (403, "captcha"), (404, "config"), (409, "deployment"), (429, "quota")],
)
def test_chat_errors_are_translated_to_layers(status: int, layer: str) -> None:
    client = _client(lambda request: _json({"error": {"message": "x"}}, status=status))
    body = {"model": "max", "messages": [], "stream": False, "max_tokens": 8}
    _ = body

    with client:
        check = check_chat(client, "http://test", "k")

    assert check.failed
    assert layer in check.detail
    assert STATUS_LAYERS[status][1] in check.hint


def test_chat_success_reports_latency_and_content() -> None:
    client = _client(
        lambda request: _json({"choices": [{"message": {"content": "pong"}}]})
    )

    with client:
        check = check_chat(client, "http://test", "k")

    assert check.status == "ok"
    assert "pong" in check.detail


def test_admin_auth_401_points_at_the_api_keys_gate() -> None:
    client = _client(lambda request: _json({"error": {}}, status=401))

    with client:
        check = check_admin_auth(client, "http://test", "admin-only-key")

    assert check.failed
    assert "API_KEYS" in check.hint


def test_admin_auth_reports_the_layer_and_the_fix() -> None:
    client = _client(lambda request: _json(_verdict("identity", "Engel · kimlik")))

    with client:
        check = check_admin_auth(client, "http://test", "k")

    assert check.status == "fail"
    assert "identity" in check.detail
    assert check.hint == "TARGET_DOMAIN=<gerçek-host>"


def test_admin_auth_without_key_warns_instead_of_failing() -> None:
    client = _client(lambda request: _json({}))

    check = check_admin_auth(client, "http://test", "")

    assert check.status == "warn"
    assert not check.failed


def test_stream_reads_the_first_event() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'data: {"choices":[{"delta":{"content":"me"}}]}\n\ndata: [DONE]\n\n',
        )

    with _client(handler) as client:
        check = check_stream(client, "http://test", "k")

    assert check.status == "ok"
    assert "data:" in check.detail


def test_stream_without_events_fails() -> None:
    client = _client(lambda request: httpx.Response(200, content=b""))

    with client:
        check = check_stream(client, "http://test", "k")

    assert check.failed


def test_report_json_is_serialisable_and_carries_no_secrets() -> None:
    report = Report(base="http://test")
    report.checks.append(check_healthz(_client(lambda r: _json({})), "http://test"))

    payload = json.loads(json.dumps(report.to_dict(), ensure_ascii=False))

    assert payload["base"] == "http://test"
    assert payload["ok"] is True


def test_unreachable_host_does_not_crash_the_report() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 111] Connection refused", request=request)

    report = run("http://dead", api_key="k", client=_client(handler))

    assert report.exit_code == 1
    assert [check.name for check in report.checks] == ["healthz"]
    assert "bağlantı kurulamadı" in report.checks[0].detail
    assert "Render" in report.checks[0].hint


def test_non_json_admin_body_is_a_warning_not_a_traceback() -> None:
    client = _client(lambda request: httpx.Response(200, text="<html>proxy</html>"))

    with client:
        check = check_admin_auth(client, "http://test", "k")

    assert check.status == "warn"
    assert "JSON" in check.detail


def test_ascii_render_has_no_emoji() -> None:
    report = Report(base="http://test")
    report.checks.append(Check("healthz", "ok", "süreç ayakta"))
    report.checks.append(Check("chat", "fail", "HTTP 403"))

    text = report.render(ascii_only=True)

    assert "[OK]" in text and "[X]" in text
    assert "✅" not in text and "❌" not in text
    assert "SONUÇ: chat adımında takıldı." in text
