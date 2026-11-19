"""Upstream'e GİDEN isteklerin dökümü: gönderme tarafı canlı sürümle aynı mı?

Kural (operatör talimatı, 19 Kasım 2026): ana cURL'ün işlenişi ve sistemin
upstream'e gönderdikleri — `mode` dahil — **değişmez**. Bu modül o kuralı
ölçülebilir kılar: aynı sahte cURL ve aynı isteklerle uygulamayı çalıştırır,
upstream'e giden her isteği (yöntem, adres, başlıklar **sırasıyla**, gövde
anahtarları **sırasıyla**) kaydeder.

İki kullanımı vardır:

1. `tests/test_wire_contract.py` bugünkü kodun gönderdiklerini bu modülle üretir
   ve `tests/data/wire_golden_03aca2c.json` ile karşılaştırır.
2. Altın dosya, **canlıdaki commit'in kodundan** aynı modülle üretilir::

       git archive 03aca2c | tar -x -C /tmp/live
       cd /tmp/live && PYTHONPATH=/tmp/live python -B \\
           <depo>/tests/wire_capture.py <depo>/tests/data/wire_golden_03aca2c.json 03aca2c

   Bu yüzden modül yalnız iki ağaçta da bulunan arayüzleri kullanır (`Settings`,
   `create_app`); yeni sürümün eklediği ayarlar eski ağaçta sessizce süzülür.

Altın dosya ne zaman yeniden üretilir: yalnız operatör gönderme tarafında bir
değişikliği **bilerek** onayladığında, ve canlıda kanıtlanmış yeni commit'ten.
Test kırıldı diye altın dosyayı bugünkü koddan üretmek kuralı boşa çıkarır.

Ağ yok: upstream `respx` ile taklit edilir; eşleşmeyen her istek hata verir.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import respx
from fastapi.testclient import TestClient

UP = "llm.example"
KEY = "sk-wire-test-key"
MODEL_ID = "019b24bb-5caf-71c3-b854-37d0c7086f21"
HEAD = {"Authorization": f"Bearer {KEY}", "user-agent": "wire-test/1.0"}

#: İkinci hesabı (yalnız yeni sürümde) ayırt eden tarayıcı kimliği.
SECOND_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)


# --------------------------------------------------------------------- cURL
def _jwt(sub: str) -> str:
    def seg(data: dict[str, Any]) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{seg({'alg': 'HS256', 'typ': 'JWT'})}.{seg({'sub': sub, 'exp': 4102444800})}.sig"


def session_cookie(sub: str = "user-1") -> str:
    """Hedefin oturum çerezi biçiminde (iki parçalı, base64) sahte çerez dizisi."""
    session = {"access_token": _jwt(sub), "refresh_token": "r" * 40, "user": {"id": sub}}
    blob = base64.b64encode(json.dumps(session).encode()).decode()
    half = len(blob) // 2
    return (
        f"_ga=GA1.1.9; cf_clearance=CFxyz; __cf_bm=cfbm-{sub}; "
        f"arena-auth-prod-v1.0=base64-{blob[:half]}; arena-auth-prod-v1.1={blob[half:]}"
    )


def make_curl(
    *,
    path: str = "/nextjs-api/stream/create-evaluation",
    cookie: str | None = None,
    authorization: str | None = None,
    mode: str | None = "direct-battle",
    referer: str | None = f"https://{UP}/text/direct",
    captcha: str | None = "0cAFcWeA6_" + "T" * 80,
    captcha_field: str = "recaptchaV3Token",
    deployment: str | None = "dpl_OLD1234567",
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    ),
    language: str = "tr,en;q=0.9,en-GB;q=0.8,en-US;q=0.7",
    data_flag: str = "--data-raw",
    extra: tuple[str, ...] = (),
) -> str:
    """Tarayıcının "Copy as cURL (bash)" çıktısı biçiminde sahte istek."""
    cookie = session_cookie() if cookie is None else cookie
    headers = ["-H 'accept: */*'", f"-H 'accept-language: {language}'"]
    if authorization:
        headers.append(f"-H 'authorization: {authorization}'")
    headers.append("-H 'content-type: text/plain;charset=UTF-8'")
    if cookie:
        headers.append(f"-H 'cookie: {cookie}'")
    headers += ["-H 'dnt: 1'", f"-H 'origin: https://{UP}'", "-H 'priority: u=1, i'"]
    if referer:
        headers.append(f"-H 'referer: {referer}'")
    headers += [
        "-H 'sec-ch-ua: \"Chromium\";v=\"141\", \"Not?A_Brand\";v=\"8\", "
        "\"Google Chrome\";v=\"141\"'",
        "-H 'sec-ch-ua-mobile: ?0'",
        "-H 'sec-ch-ua-platform: \"Windows\"'",
        "-H 'sec-fetch-dest: empty'",
        "-H 'sec-fetch-mode: cors'",
        "-H 'sec-fetch-site: same-origin'",
        f"-H 'user-agent: {user_agent}'",
    ]
    if deployment:
        headers.append(f"-H 'x-deployment-id: {deployment}'")
        headers.append(f"-H 'x-arena-web-deployment: {deployment}'")
    headers.extend(extra)
    body: dict[str, Any] = {
        "id": "0199aaaa-bbbb-7ccc-8ddd-eeeeeeeeeeee",
        "modelAId": MODEL_ID,
        "userMessageId": "0199aaaa-0000-7ccc-8ddd-eeeeeeeeeeee",
        "modelAMessageId": "0199aaaa-1111-7ccc-8ddd-eeeeeeeeeeee",
        "userMessage": {"content": "selam", "experimental_attachments": [], "metadata": {}},
        "modality": "chat",
    }
    if mode is not None:
        body["mode"] = mode
    if captcha is not None:
        body[captcha_field] = captcha
    return (
        f"curl 'https://{UP}{path}' \\\n  "
        + " \\\n  ".join(headers)
        + f" \\\n  {data_flag} '{json.dumps(body, ensure_ascii=False)}'"
    )


def second_curl() -> str:
    """İkinci hesabın cURL'ü: başka kullanıcı, başka tarayıcı, başka captcha."""
    return make_curl(
        cookie=session_cookie("user-2"),
        user_agent=SECOND_USER_AGENT,
        language="en-US,en;q=0.9",
        captcha="0cAFcWeA6_" + "K" * 80,
    )


# ------------------------------------------------------------- sahte upstream
class FakeUpstream:
    """Hedefi taklit eder ve kendisine gelen her isteği kaydeder.

    Hedefin gerçek davranışının iki yönü taklit edilir: her cevapta bir çerez
    döner (kavanozun bir sonraki isteğe taşıdığı değer görülsün) ve sayfa güncel
    sürüm damgasını taşır (kanarya onu okuyup başlıklara koyar). Sayaçlar
    **tarayıcı kimliği başına** tutulur: ikinci bir hesap eklendiğinde ilk hesabın
    gördükleri değişmez.
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.script: Callable[[int], httpx.Response | None] | None = None
        self._gets: dict[str, int] = {}
        self._posts: dict[str, int] = {}
        self.posts = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        agent = request.headers.get("user-agent", "")
        self.requests.append(
            {
                "method": request.method,
                "url": str(request.url),
                "headers": [
                    [key.decode("latin-1"), value.decode("latin-1")]
                    for key, value in request.headers.raw
                ],
                "body": request.content.decode("utf-8", "replace"),
            }
        )
        if request.method == "GET":
            self._gets[agent] = self._gets.get(agent, 0) + 1
            return httpx.Response(
                200,
                headers=[
                    ("content-type", "text/html; charset=utf-8"),
                    ("set-cookie", f"sidebar_state=get-{self._gets[agent]}; Path=/"),
                ],
                text="<html><head><script src='/_next/x.js?dpl=dpl_NEW7654321'></script>"
                "</head></html>",
            )
        self.posts += 1
        self._posts[agent] = self._posts.get(agent, 0) + 1
        if self.script is not None:
            scripted = self.script(self.posts)
            if scripted is not None:
                return scripted
        return httpx.Response(
            200,
            headers=[
                ("content-type", "text/plain; charset=utf-8"),
                ("set-cookie", f"__cf_bm=rot-post-{self._posts[agent]}; Path=/; HttpOnly"),
            ],
            content=_answer(),
        )


def _answer(text: str = "Merhaba dünya") -> bytes:
    finish = {"finishReason": "stop", "usage": {"promptTokens": 3, "completionTokens": 2}}
    lines = ['f:{"messageId":"msg-wire"}']
    for part in (text[: len(text) // 2], text[len(text) // 2 :]):
        lines.append("0:" + json.dumps(part, ensure_ascii=False))
    lines.append("e:" + json.dumps(finish))
    lines.append("d:" + json.dumps(finish))
    return ("\n".join(lines) + "\n").encode()


def failing(status: int, body: str, times: int = 99) -> Callable[[int], httpx.Response | None]:
    """İlk `times` POST'a `status` + `body` ile cevap veren senaryo."""

    def script(number: int) -> httpx.Response | None:
        if number <= times:
            return httpx.Response(
                status, headers=[("content-type", "application/json")], text=body
            )
        return None

    return script


# ------------------------------------------------------------- normalizasyon
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TRACE = re.compile(r"00-[0-9a-f]{32}-[0-9a-f]{16}-01")


def _httpx_default_encoding() -> str:
    """httpx'in kendi `Accept-Encoding` değeri (kurulu sıkıştırma kitaplıklarına bağlı)."""
    with httpx.Client() as client:
        return client.headers.get("accept-encoding", "")


def normalize(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Her çalıştırmada değişen değerleri (rastgele kimlikler) sabit etiketlere çevirir.

    Kimlikler ilk görülüş sırasına göre numaralanır: aynı kimliğin iki yerde
    kullanılması (örn. tekrar denemede aynı sohbet kimliği) görünür kalır.
    """
    seen: dict[str, str] = {}
    default_encoding = _httpx_default_encoding()

    def scrub(value: str) -> str:
        value = _TRACE.sub("00-<trace>-<span>-01", value)

        def label(match: re.Match[str]) -> str:
            found = match.group(0)
            if found == MODEL_ID:
                return found
            return seen.setdefault(found, f"<uuid:{len(seen) + 1}>")

        return _UUID.sub(label, value)

    out: list[dict[str, Any]] = []
    for item in requests:
        headers = []
        for name, value in item["headers"]:
            if name.lower() == "accept-encoding" and value == default_encoding:
                # Uygulamanın değil httpx'in değeridir; ortamdan ortama değişebilir.
                value = "<httpx-default>"
            headers.append([name, scrub(value)])
        entry: dict[str, Any] = {
            "method": item["method"],
            "url": scrub(item["url"]),
            "headers": headers,
        }
        body = scrub(item["body"])
        if body:
            try:
                parsed = json.loads(body)
            except ValueError:
                entry["body_text"] = body
            else:
                entry["body_keys"] = list(parsed) if isinstance(parsed, dict) else None
                entry["body"] = parsed
        out.append(entry)
    return out


# ------------------------------------------------------------------ çalıştırma
def build_settings(**overrides: Any) -> Any:
    """Canlıdaki kurulumun biçimi: kimlik yalnız `UPSTREAM_CURL`'den, captcha `static`."""
    from app.core.config import Settings

    base: dict[str, Any] = {
        "upstream_curl": make_curl(),
        "api_keys": [KEY],
        "rate_limit_enabled": False,
        "log_level": "ERROR",
        # Tekrar denemelerin bekleyişi kısaltılır; sayısı ve içeriği aynı kalır.
        "retry_base_delay": 0.001,
        "retry_max_delay": 0.002,
        # Yeni sürümün istekler arası beklemesi yalnız ZAMANLAMADIR; içerik
        # karşılaştırması için kapatılır (alan eski ağaçta yoktur → süzülür).
        "request_gap_min_seconds": 0.0,
        "request_gap_max_seconds": 0.0,
    }
    base.update(overrides)
    known = set(Settings.model_fields)
    return Settings(_env_file=None, **{k: v for k, v in base.items() if k in known})


@contextmanager
def running(
    env: dict[str, str] | None = None, **overrides: Any
) -> Iterator[tuple[TestClient, FakeUpstream]]:
    """Uygulamayı sahte upstream'e karşı ayağa kaldırır."""
    from app.main import create_app

    upstream = FakeUpstream()
    for key, value in (env or {}).items():
        os.environ[key] = value
    try:
        with respx.mock(assert_all_called=False) as router:
            router.route(host=UP).mock(side_effect=upstream)
            app = create_app(build_settings(**overrides))
            with TestClient(app) as client:
                yield client, upstream
    finally:
        for key in env or {}:
            os.environ.pop(key, None)


def chat(client: TestClient, content: str = "Merhaba", **body: Any) -> int:
    headers = body.pop("_headers", HEAD)
    payload = {"model": "max", "messages": [{"role": "user", "content": content}], **body}
    if body.get("stream"):
        with client.stream("POST", "/v1/chat/completions", json=payload, headers=headers) as r:
            r.read()
            return r.status_code
    return client.post("/v1/chat/completions", json=payload, headers=headers).status_code


# ------------------------------------------------------------------- senaryolar
def _two_chats(client: TestClient, _upstream: FakeUpstream) -> None:
    chat(client, "Merhaba")
    chat(client, "İkinci soru 🙂")


def _stream(client: TestClient, _upstream: FakeUpstream) -> None:
    chat(client, "Akışlı soru", stream=True)


def _multi_turn(client: TestClient, _upstream: FakeUpstream) -> None:
    client.post(
        "/v1/chat/completions",
        headers=HEAD,
        json={
            "model": "max",
            "messages": [
                {"role": "system", "content": "Kısa cevap ver."},
                {"role": "user", "content": "2+2?"},
                {"role": "assistant", "content": "4"},
                {"role": "user", "content": "Peki 3+3?"},
            ],
            "temperature": 0.2,
            "max_tokens": 64,
            "user": "musteri-7",
        },
    )


def _legacy(client: TestClient, _upstream: FakeUpstream) -> None:
    client.post("/v1/completions", json={"model": "max", "prompt": "Tamamla: gök"}, headers=HEAD)


def _internal_headers(client: TestClient, _upstream: FakeUpstream) -> None:
    # Servisin kendi iç başlıkları dışarıdan gelse de upstream'e taşınmamalı.
    chat(
        client,
        "Merhaba",
        _headers={**HEAD, "x-apiwrapper-source": "telegram", "x-apiwrapper-account": "1"},
    )


def _failing_twice(status: int, body: str, times: int = 99) -> Callable[..., None]:
    def action(client: TestClient, upstream: FakeUpstream) -> None:
        upstream.script = failing(status, body, times)
        chat(client, "Merhaba")
        chat(client, "Hemen ardından ikinci istek")

    return action


#: ad → (ayar farkları, ortam değişkenleri, eylem)
SCENARIOS: dict[str, tuple[dict[str, Any], dict[str, str], Callable[..., None]]] = {
    "01_iki_akissiz_istek": ({}, {}, _two_chats),
    "02_akisli_istek": ({}, {}, _stream),
    "03_cok_turlu_ve_parametreli": ({}, {}, _multi_turn),
    "04_eski_completions_ucu": ({}, {}, _legacy),
    "05_ic_basliklar_tasinmaz": ({}, {}, _internal_headers),
    "06_hedef_401": ({}, {}, _failing_twice(401, '{"message":"User not found"}')),
    "07_hedef_403_captcha": (
        {},
        {},
        _failing_twice(403, '{"error":"recaptcha validation failed"}'),
    ),
    "08_hedef_429_duz": ({}, {}, _failing_twice(429, '{"error":"slow down"}')),
    "09_hedef_429_bilinen_kota": ({}, {}, _failing_twice(429, '{"error":"quota exceeded"}')),
    "10_hedef_429_spend_limit": (
        {},
        {},
        _failing_twice(429, '{"error":"daily spend limit reached"}'),
    ),
    "11_hedef_500": ({}, {}, _failing_twice(500, "Internal Server Error")),
    "12_hedef_409_refresh": (
        {},
        {},
        _failing_twice(409, '{"error":"refresh_required","reason":"forced_update"}'),
    ),
    "13_hedef_400_zod": (
        {},
        {},
        _failing_twice(
            400, '{"error":{"issues":[{"path":["mode"],"message":"Required"}],"name":"ZodError"}}'
        ),
    ),
    "14_bir_kez_409_sonra_basari": (
        {},
        {},
        _failing_twice(409, '{"error":"refresh_required"}', times=1),
    ),
    "15_bir_kez_500_sonra_basari": ({}, {}, _failing_twice(500, "boom", times=1)),
    # cURL'de `mode` yoksa env'deki UPSTREAM_MODE onu geri getirmez (canlıda da böyle).
    "16_mode_suz_curl": (
        {"upstream_curl": make_curl(mode=None)},
        {"UPSTREAM_MODE": "direct-battle"},
        _two_chats,
    ),
    "17_mode_direct": ({"upstream_curl": make_curl(mode="direct")}, {}, _two_chats),
    "18_authorization_baslikli_curl": (
        {"upstream_curl": make_curl(authorization=f"Bearer {_jwt('user-1')}")},
        {},
        _two_chats,
    ),
    "19_surum_basliksiz_curl": ({"upstream_curl": make_curl(deployment=None)}, {}, _two_chats),
    "20_eski_yol_sohbet_kimlikli": (
        {
            "upstream_curl": make_curl(
                path="/nextjs-api/stream/post-to-evaluation/0199aaaa-bbbb-7ccc-8ddd-eeeeeeeeeeee",
                referer=f"https://{UP}/c/0199aaaa-bbbb-7ccc-8ddd-eeeeeeeeeeee",
            )
        },
        {},
        _two_chats,
    ),
    "21_captcha_siz_curl": ({"upstream_curl": make_curl(captcha=None)}, {}, _two_chats),
    "22_ek_baslikli_data_binary_curl": (
        {
            "upstream_curl": make_curl(
                data_flag="--data-binary", extra=("-H 'x-custom-a: 1'", "-H 'baggage: s=1'")
            )
        },
        {},
        _two_chats,
    ),
}


def capture(name: str) -> list[dict[str, Any]]:
    """Bir senaryoyu çalıştırır; upstream'e giden istekleri sırasıyla döndürür."""
    overrides, env, action = SCENARIOS[name]
    with running(env, **overrides) as (client, upstream):
        action(client, upstream)
    return normalize(upstream.requests)


# ------------------------------------------------- ana cURL → ayarlar eşlemesi
#: Upstream isteğini biçimlendiren ayarlar: cURL'den bunlar çıkar.
SEND_FIELDS = (
    "target_domain",
    "upstream_scheme",
    "upstream_stream_path",
    "upstream_referer_path",
    "upstream_mode",
    "upstream_recaptcha_field",
    "upstream_accept_language",
    "upstream_user_agent",
    "upstream_extra_headers",
    "upstream_cookie",
    "upstream_auth_from_cookie",
    "upstream_access_token",
    "upstream_auth_scheme",
    "recaptcha_provider",
    "recaptcha_static_token",
)

#: ad → `Settings` alanları (cURL + onunla çakışabilecek eski env değerleri)
CURL_CASES: dict[str, dict[str, Any]] = {
    "tam_curl": {"upstream_curl": make_curl()},
    "mode_yok": {"upstream_curl": make_curl(mode=None)},
    "mode_yok_env_mode_var": {"upstream_curl": make_curl(mode=None), "upstream_mode": "x-battle"},
    "mode_direct_env_farkli": {"upstream_curl": make_curl(mode="direct"), "upstream_mode": "x"},
    "referer_yok": {
        "upstream_curl": make_curl(referer=None),
        "upstream_referer_path": "/eski/{chat_id}",
    },
    "captcha_yok": {"upstream_curl": make_curl(captcha=None), "recaptcha_static_token": "eski"},
    "captcha_v2_alani": {"upstream_curl": make_curl(captcha_field="recaptchaV2Token")},
    "authorization_bearer": {"upstream_curl": make_curl(authorization=f"Bearer {_jwt('u')}")},
    "authorization_semasiz": {"upstream_curl": make_curl(authorization=_jwt("u"))},
    "cerezsiz_authorization": {
        "upstream_curl": make_curl(cookie="", authorization=f"Bearer {_jwt('u')}")
    },
    "oturum_cerezi_yok": {"upstream_curl": make_curl(cookie="_ga=GA1.1.9; cf_clearance=CFxyz")},
    "surum_basligi_yok": {"upstream_curl": make_curl(deployment=None)},
    "eski_yol_sohbet_kimlikli": {
        "upstream_curl": make_curl(
            path="/nextjs-api/stream/post-to-evaluation/0199aaaa-bbbb-7ccc-8ddd-eeeeeeeeeeee"
        )
    },
    "data_binary": {"upstream_curl": make_curl(data_flag="--data-binary")},
    "data_duz": {"upstream_curl": make_curl(data_flag="--data")},
    "ek_basliklar": {
        "upstream_curl": make_curl(extra=("-H 'x-custom-a: 1'", "-H 'baggage: sentry-x=1'"))
    },
    "env_stream_path_farkli": {
        "upstream_curl": make_curl(),
        "upstream_stream_path": "/eski/yol/{chat_id}",
    },
    "env_saglayici_browser": {"upstream_curl": make_curl(), "recaptcha_provider": "browser"},
    "env_saglayici_noop": {"upstream_curl": make_curl(), "recaptcha_provider": "noop"},
    "env_saglayici_external": {"upstream_curl": make_curl(), "recaptcha_provider": "external"},
    "env_unutulmus_ikinci_yuva": {
        "upstream_curl": make_curl(),
        "upstream_cookie_2": "eski=2",
        "upstream_access_token_3": "eski-3",
        "upstream_account_2_name": "eski-hesap",
        "target_domain_2": "eski.example",
        "recaptcha_static_token_2": "eski-captcha-2",
    },
    "env_eski_kimlik_alanlari": {
        "upstream_curl": make_curl(),
        "upstream_cookie": "eski=1",
        "upstream_access_token": "eski-token",
        "upstream_user_agent": "EskiAjan/1.0",
        "upstream_accept_language": "en",
        "upstream_extra_headers": "x-eski=1",
        "target_domain": "eski.example",
        "upstream_auth_from_cookie": True,
    },
    "baska_tarayici_ve_dil": {
        "upstream_curl": make_curl(user_agent=SECOND_USER_AGENT, language="en-US,en;q=0.9")
    },
    "curl_yok_env_ile": {
        "upstream_curl": "",
        "target_domain": UP,
        "upstream_mode": "direct-battle",
        "upstream_cookie": session_cookie(),
    },
    "HATA_adres_yok": {"upstream_curl": "curl -H 'cookie: a=b' --data-raw '{}'"},
    "HATA_kimlik_yok": {"upstream_curl": make_curl(cookie="")},
    "HATA_curl_degil": {"upstream_curl": "bu bir curl değil"},
}


def read_curl(name: str) -> dict[str, Any]:
    """Bir cURL durumunu uygulamanın açılıştaki yolundan geçirir; sonucu özetler."""
    from app.core.config import Settings
    from app.services.account import build_account_pool
    from app.upstream.curl_runtime import apply_upstream_curl

    known = set(Settings.model_fields)
    kwargs = {k: v for k, v in CURL_CASES[name].items() if k in known}
    try:
        settings = Settings(_env_file=None, **kwargs)
        updated, model_id = apply_upstream_curl(settings)
    except Exception as exc:  # noqa: BLE001 - hata türü ve metni de sözleşmenin parçası
        return {"error_type": type(exc).__name__, "error": str(exc)}
    shared = {key: getattr(updated, key) for key in SEND_FIELDS}
    accounts = []
    for account in build_account_pool(updated).accounts:
        effective = account.effective_settings(updated)
        own = {key: getattr(effective, key) for key in SEND_FIELDS}
        accounts.append(
            {
                "slot": account.slot,
                "configured": account.is_configured,
                "captcha": account.recaptcha_token,
                # Hesabın isteği biçimlendiren ayarları; ortak ayarlarla aynıysa
                # yinelenmez (dosya okunur kalsın), farklıysa yalnız fark yazılır.
                "differs_from_settings": {
                    key: value for key, value in own.items() if value != shared[key]
                },
                "stream_url": effective.stream_url("CHAT"),
                "referer_url": effective.referer_url("CHAT"),
            }
        )
    return {
        "model_id": model_id,
        "settings": shared,
        "stream_url": updated.stream_url("CHAT"),
        "referer_url": updated.referer_url("CHAT"),
        "origin": updated.origin,
        "accounts": accounts,
    }


# ------------------------------------------------------------------ altın dosya
def _digest(request: dict[str, Any]) -> str:
    raw = json.dumps(request, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()[:12]


def pack(source_commit: str) -> dict[str, Any]:
    """Bütün senaryoları çalıştırır; yinelenen istekleri bir kez saklayan döküm üretir."""
    requests: dict[str, dict[str, Any]] = {}
    scenarios: dict[str, list[str]] = {}
    for name in SCENARIOS:
        keys = []
        for request in capture(name):
            key = _digest(request)
            requests[key] = request
            keys.append(key)
        scenarios[name] = keys
    return {
        "source_commit": source_commit,
        "about": "Canlı sürümün upstream'e gönderdikleri. tests/wire_capture.py ile üretilir; "
        "elle düzenlenmez. Yalnız operatör gönderme tarafında bir değişikliği bilerek "
        "onayladığında, canlıda kanıtlanmış commit'ten yeniden üretilir.",
        "scenarios": scenarios,
        "requests": requests,
        "curl": {name: read_curl(name) for name in CURL_CASES},
    }


def unpack(golden: dict[str, Any], name: str) -> list[dict[str, Any]]:
    return [golden["requests"][key] for key in golden["scenarios"][name]]


def _isolate_environment() -> None:
    """Betik olarak çalışırken: geliştiricinin `.env`'i ve ortamı sonucu etkilemesin."""
    from app.core.config import Settings
    from app.telegram.bot import TelegramSettings

    for model in (Settings, TelegramSettings):
        model.model_config["env_file"] = None
        for field in model.model_fields:
            os.environ.pop(field.upper(), None)


if __name__ == "__main__":
    _isolate_environment()
    target, commit = sys.argv[1], sys.argv[2]
    packed = pack(commit)
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(packed, handle, ensure_ascii=False, indent=1)
        handle.write("\n")
    total = sum(len(keys) for keys in packed["scenarios"].values())
    print(
        f"yazıldı: {target} · {len(packed['scenarios'])} senaryo · {total} istek "
        f"({len(packed['requests'])} farklı) · {len(packed['curl'])} cURL durumu"
    )
