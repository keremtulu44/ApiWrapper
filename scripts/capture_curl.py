"""Yerel tarayıcıyla oturum açıp gerçek sohbet isteğini cURL olarak yakalar.

Neden ayrı bir araç var?
------------------------
Sunucudaki Chromium sizin yerinize **giriş yapamaz** (şifre/2FA yok, veri
merkezi IP'si Google/Cloudflare tarafından challenge'a düşürülür). Ama kendi
bilgisayarınızda, kendi profilinizle giriş yapılmış bir tarayıcıyı sürebilirsiniz.
Bu betik tam olarak onu yapar:

1. Kalıcı bir Chromium profili açar (ilk çalıştırmada bir kez elle giriş
   yaparsınız; sonraki çalıştırmalarda oturum durur).
2. Sohbet sayfasına gider, ``direct`` modu seçer, bir mesaj gönderir.
3. Ağ trafiğini dinler ve **sohbet akış isteğini** (``create-evaluation`` gibi)
   yakalar.
4. İsteği ``curl`` komutuna çevirir ve — ``--send`` verilmişse — merkezdeki
   wrapper'a ``POST /v1/admin/upstream/curl`` ile teslim eder.

Wrapper tarafındaki mevcut uç bunu zaten anlıyor: ``parse_curl`` çerez, kimlik,
``mode``, referer yolu ve (varsa) captcha alanını çıkarır, çalışan servise
yeniden deploy gerekmeden uygular.

Güvenlik
--------
* Ham çerez/token **hiçbir zaman ekrana basılmaz**; yalnız maskeli özet ve
  uzunluk yazılır.
* Varsayılan davranış **kuru çalıştırma**dır: hiçbir yere istek gönderilmez.
  Teslimat için açıkça ``--send`` gerekir.
* Profil dizini sizin makinanızda kalır; bu betik şifre saklamaz.
* reCAPTCHA/captcha **aşılmaz**: tarayıcı gerçek kullanıcı gibi çalışır, token'ı
  sitenin kendi sayfası üretir.

Kullanım
--------
::

    pip install "apiwrapper[browser]" && playwright install chromium

    # 1) İlk sefer: profille aç, açılan pencerede Google/şifre ile giriş yap
    python -m scripts.capture_curl --profile .cache/browser-profile

    # 2) Yakala ve merkeze gönder (deploy yok)
    python -m scripts.capture_curl --profile .cache/browser-profile --send \
        --wrapper https://apiwrapper.onrender.com --api-key <ADMIN_KEY>

Yol sohbet ucu gibi görünmüyorsa gönderim yapılmaz (yanlış isteği uygulamak
oturumu bozar); ``--allow-path-change`` ile bilinçli olarak zorlanabilir.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import shlex
import sys
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.upstream.curl_import import looks_like_stream_path

#: Yakalanacak isteğin gövde alanı olarak kabul edilenler. Gövdede bunlardan
#: biri yoksa istek sohbet isteği sayılmaz (ör. telemetri POST'u).
BODY_MARKERS: tuple[str, ...] = (
    "modelAId",
    "userMessage",
    "recaptchaV3Token",
    "recaptchaV2Token",
)

#: `<script>` içindeki reCAPTCHA public site key biçimi (gizli değildir).
_LOGIN_HINTS = ("login", "signin", "sign-in", "/auth", "welcome")


@dataclass(slots=True)
class CapturedRequest:
    """Yakalanan tek bir isteğin, gizli değer içermeyen özeti + ham cURL'ü."""

    url: str
    method: str
    curl: str
    host: str = ""
    path: str = ""
    #: Gövdedeki alan adları (değer yok).
    body_fields: list[str] = field(default_factory=list)
    has_cookie: bool = False
    has_authorization: bool = False
    #: İstekte `apikey` başlığı görüldüyse maskeli özeti.
    apikey_masked: str = ""

    @property
    def looks_like_stream(self) -> bool:
        return looks_like_stream_path(self.path)


def mask(value: str) -> str:
    """Gizli değeri log/çıktı için maskeler (kısa değer tamamen gizlenir)."""
    text = (value or "").strip()
    if len(text) <= 8:
        return "***" if text else ""
    return f"{text[:3]}***{text[-2:]}"


def _cookie_header(headers: dict[str, str]) -> str:
    return str(headers.get("cookie") or "")


def build_curl(
    url: str,
    headers: dict[str, str],
    body: str,
) -> str:
    """Yakalanan isteği, hedefin beklediği biçimde tek satırlık cURL'e çevirir.

    ``content-length``/``host``/``content-encoding`` gibi taşınmaması gereken
    başlıklar atlanır: elle gönderilen gövde uzunluğu farklı olabilir ve yanlış
    ``content-length`` isteği bozar.
    """
    skip = {"content-length", "host", "connection", "content-encoding", "transfer-encoding"}
    parts = ["curl", shlex.quote(url)]
    for name, value in headers.items():
        lowered = name.lower()
        if lowered in skip or not str(value).strip():
            continue
        parts.append("-H")
        parts.append(shlex.quote(f"{lowered}: {value}"))
    if body:
        parts.append("--data-raw")
        parts.append(shlex.quote(body))
    return " ".join(parts)


async def capture(
    *,
    profile: str,
    headless: bool,
    wait_ms: int,
    timeout_ms: int,
    chat_path: str,
    prompt: str,
) -> tuple[CapturedRequest | None, dict[str, Any]]:
    """Profili açar, mesaj gönderir ve sohbet isteğini yakalar.

    Dönüş: (yakalanan istek ya da None, gizli değer içermeyen teşhis sözlüğü).
    """
    try:
        from playwright.async_api import async_playwright  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - kurulum hatası
        raise SystemExit(
            "Playwright yok. Kurulum: pip install 'apiwrapper[browser]' "
            "&& playwright install chromium"
        ) from exc

    diagnostic: dict[str, Any] = {
        "final_url": "",
        "final_path": "",
        "login_redirect": False,
        "message_sent": False,
        "candidates_seen": 0,
        "apikey_seen": False,
        "apikey_masked": "",
        "notes": [],
    }
    captured: CapturedRequest | None = None

    playwright = await async_playwright().start()
    context = None
    try:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=profile,
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            locale="tr-TR",
        )

        async def on_request(request: Any) -> None:
            nonlocal captured
            try:
                headers = await _all_headers(request)
            except Exception:  # pragma: no cover - savunmacı
                return
            if not captured:
                # `apikey` başlığı herhangi bir istekte geçebilir; ilk görüldüğü
                # yerde maskeli olarak raporlanır (değerin kendisi yazılmaz).
                key = str(headers.get("apikey") or "").strip()
                if key:
                    diagnostic["apikey_seen"] = True
                    diagnostic["apikey_masked"] = mask(key)

            if str(getattr(request, "method", "")).upper() != "POST":
                return
            body = _post_data(request)
            if not body or not any(marker in body for marker in BODY_MARKERS):
                return
            diagnostic["candidates_seen"] += 1
            if captured is not None:
                return

            url = str(getattr(request, "url", "") or "")
            split = urlsplit(url)
            candidate = CapturedRequest(
                url=url,
                method="POST",
                curl=build_curl(url, headers, body),
                host=split.netloc,
                path=split.path,
                body_fields=sorted(_body_field_names(body)),
                has_cookie=bool(_cookie_header(headers)),
                has_authorization=bool(headers.get("authorization")),
                apikey_masked=mask(headers.get("apikey") or ""),
            )
            # Sohbet akış ucuna benzemeyen adayı yakalama: yanlış uç uygulanırsa
            # çalışan oturum bozulur (bu kural wrapper'daki `apply_curl` ile aynı).
            if candidate.looks_like_stream:
                captured = candidate
            else:
                diagnostic["notes"].append(
                    f"akış dışı POST atlandı: {candidate.path}"
                )

        page = await context.new_page()
        page.on("request", on_request)

        origin = _origin_from_chat_path(chat_path)
        target = origin + chat_path if origin else chat_path
        await page.goto(target, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(wait_ms)

        diagnostic["final_url"] = str(getattr(page, "url", "") or "")
        diagnostic["final_path"] = urlsplit(diagnostic["final_url"]).path
        diagnostic["login_redirect"] = any(
            hint in diagnostic["final_path"].lower() for hint in _LOGIN_HINTS
        )

        if not diagnostic["login_redirect"]:
            diagnostic["message_sent"] = await _try_send(page, prompt, wait_ms)
            # Akış isteği ilk beklemede gelmemiş olabilir.
            for _ in range(3):
                if captured is not None:
                    break
                await page.wait_for_timeout(wait_ms)
    finally:
        if context is not None:
            with contextlib.suppress(Exception):
                await context.close()
        await playwright.stop()

    return captured, diagnostic


async def _all_headers(request: Any) -> dict[str, str]:
    """İsteğin **tüm** başlıklarını küçük harfli sözlük olarak döndürür.

    Playwright `request.headers` çerezleri dışarıda bırakabilir; `all_headers`
    onları da verir. Eski sürümlerde `all_headers` yoktur.
    """
    getter = getattr(request, "all_headers", None)
    raw = await getter() if getter is not None else request.headers
    return {str(k).lower(): str(v) for k, v in dict(raw or {}).items()}


def _post_data(request: Any) -> str:
    data = getattr(request, "post_data", None)
    return data if isinstance(data, str) else ""


def _body_field_names(body: str) -> set[str]:
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return set()
    if isinstance(parsed, dict):
        return {str(key) for key in parsed}
    return set()


def _origin_from_chat_path(chat_path: str) -> str:
    """Sohbet yolu tam URL ise kökenini döndürür, değilse boş metin."""
    if "://" not in chat_path:
        return ""
    split = urlsplit(chat_path)
    return f"{split.scheme}://{split.netloc}" if split.netloc else ""


async def _try_send(page: Any, prompt: str, wait_ms: int) -> bool:
    """Editörü bulup mesajı gönderir; DOM tanınmazsa False döner."""
    editors = (
        "textarea:not([disabled])",
        '[contenteditable="true"]',
        ".ProseMirror",
        '[data-slate-editor="true"]',
        '[data-lexical-editor="true"]',
        '[role="textbox"]',
    )
    for selector in editors:
        try:
            locator = page.locator(selector).first
            if not await locator.count():
                continue
            if not await locator.is_visible():
                continue
            await locator.click(timeout=3000)
            await locator.fill(prompt, timeout=3000)
            for button_selector in (
                'button[type="submit"]',
                'button[aria-label*="send" i]',
                'button[aria-label*="gönder" i]',
                'button[title*="send" i]',
            ):
                try:
                    button = page.locator(button_selector).first
                    if not await button.count() or not await button.is_visible():
                        continue
                    if not await button.is_enabled():
                        continue
                    await button.click(timeout=2000)
                    await page.wait_for_timeout(wait_ms)
                    return True
                except Exception:
                    continue
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(wait_ms)
            return True
        except Exception:
            continue
    return False


def send_to_wrapper(
    curl: str,
    *,
    wrapper: str,
    api_key: str,
    allow_path_change: bool = False,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Yakalanan cURL'ü merkezdeki wrapper'a uygular (yeniden deploy gerekmez)."""
    base = wrapper.rstrip("/")
    response = httpx.post(
        f"{base}/v1/admin/upstream/curl",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"curl": curl, "allow_path_change": allow_path_change},
        timeout=timeout,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": response.text[:300]}
    return {"status_code": response.status_code, "response": payload}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.capture_curl",
        description=(
            "Yerel tarayıcıyla giriş yapıp gerçek sohbet isteğini cURL olarak "
            "yakalar ve (isteğe bağlı) merkezdeki wrapper'a teslim eder."
        ),
    )
    parser.add_argument(
        "--profile",
        default=".cache/browser-profile",
        help="Kalıcı Chromium profil dizini (oturum burada saklanır).",
    )
    parser.add_argument(
        "--chat-path",
        default="",
        help=(
            "Açılacak sohbet sayfası. Tam URL de verilebilir "
            "(ör. https://ornek.com/c/abc). Boşsa --origin gerekir."
        ),
    )
    parser.add_argument("--origin", default="", help="Sohbet sayfasının kökeni.")
    parser.add_argument("--prompt", default="merhaba", help="Gönderilecek mesaj.")
    parser.add_argument(
        "--wait-ms", type=int, default=4000, help="Her bekleme adımı (ms)."
    )
    parser.add_argument(
        "--timeout-ms", type=int, default=30000, help="Sayfa açma zaman aşımı (ms)."
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Pencereyi gösterme. Cloudflare challenge'larında ÖNERİLMEZ.",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="Yakalanan cURL'ü wrapper'a gönder (varsayılan: yalnız göster).",
    )
    parser.add_argument("--wrapper", default="", help="Wrapper kök adresi.")
    parser.add_argument("--api-key", default="", help="Admin API anahtarı.")
    parser.add_argument(
        "--allow-path-change",
        action="store_true",
        help="Yol sohbet ucu gibi görünmese de uygula (dikkatli kullanın).",
    )
    parser.add_argument(
        "--out", default="", help="Yakalanan cURL'ü bu dosyaya yaz (opsiyonel)."
    )
    args = parser.parse_args(argv)

    if not args.chat_path and not args.origin:
        parser.error("--chat-path (tam URL) veya --origin vermelisiniz.")

    chat_path = args.chat_path
    if not chat_path and args.origin:
        chat_path = args.origin.rstrip("/") + "/"

    captured, diagnostic = _run_capture(args, chat_path)

    print(json.dumps(diagnostic, ensure_ascii=False, indent=2))

    if captured is None:
        print(
            "\nYakalama başarısız: sohbet isteği görülmedi.\n"
            "* Pencerede giriş yaptığınızdan emin olun (--profile ile aynı dizin).\n"
            "* Mesajı elle gönderin; akış isteği gelince betik yakalar.\n"
            "* Cloudflare engeli varsa pencereyi görünür çalıştırın (--headless YOK).",
            file=sys.stderr,
        )
        return 2

    print(
        f"\nYakalandı: POST {captured.host}{captured.path}\n"
        f"  gövde alanları : {', '.join(captured.body_fields) or '-'}\n"
        f"  cookie         : {'var' if captured.has_cookie else 'YOK'}\n"
        f"  authorization  : {'var' if captured.has_authorization else 'yok'}\n"
        f"  apikey         : {captured.apikey_masked or 'görülmedi'}\n"
        f"  akış ucu mu    : {'evet' if captured.looks_like_stream else 'HAYIR'}"
    )

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(captured.curl)
        print(f"  cURL dosyası   : {args.out}")

    if not args.send:
        print(
            "\nKuru çalıştırma: hiçbir yere gönderilmedi.\n"
            "Teslimat için: --send --wrapper <ADRES> --api-key <ADMIN_KEY>"
        )
        return 0

    if not args.wrapper or not args.api_key:
        parser.error("--send için --wrapper ve --api-key zorunlu.")

    result = send_to_wrapper(
        captured.curl,
        wrapper=args.wrapper,
        api_key=args.api_key,
        allow_path_change=args.allow_path_change,
    )
    print("\nTeslimat: " + json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if 200 <= int(result["status_code"]) < 300 else 1


def _run_capture(
    args: argparse.Namespace, chat_path: str
) -> tuple[CapturedRequest | None, dict[str, Any]]:
    import asyncio

    return asyncio.run(
        capture(
            profile=args.profile,
            headless=bool(args.headless),
            wait_ms=int(args.wait_ms),
            timeout_ms=int(args.timeout_ms),
            chat_path=chat_path,
            prompt=str(args.prompt),
        )
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
