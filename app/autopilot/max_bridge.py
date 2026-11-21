"""Durumsuz (stateless) `max` konuşma köprüsü.

Orkestratörün `max` sohbet moduna her turda **tek bir bağımsız istek** atmasını
sağlar; `max` tarafında kesinlikle konuşma geçmişi (`conversation_id`) tutulmaz.

Üç çalışma kipini destekler:
1. ``ExternalHttpMaxBridge``: Canlı Render veya yerel ApiWrapper sunucusuna
   HTTP üzerinden (``/v1/chat/completions`` veya ``/v1/ask``) bağlanır.
2. ``InProcessMaxBridge``: Aynı FastAPI süreci içindeki ``CompletionService``'i
   doğrudan çağırır (ağa çıkmaz, durumsuz çağrı garantisi verir).
3. ``DirectCurlMaxBridge``: Ayrı bir sunucu gerektirmeden doğrudan ikinci bir
   Google hesabından alınmış ``MAX_CURL`` ile bellekte izole bir `max` istemcisi
   kurar ve çalıştırır.
"""

from __future__ import annotations

import abc
import json
from typing import Literal

import httpx

from app.autopilot.curl_parse import extract_all_max_identities, extract_identity_subject
from app.core.config import Settings
from app.core.logging import get_logger
from app.schemas.openai import ChatCompletionRequest, ChatMessage
from app.services.account import build_account_pool
from app.services.completion_service import Admission, CompletionService
from app.services.lanes import LaneSet
from app.services.model_registry import ModelEntry, ModelRegistry
from app.services.recaptcha import create_recaptcha_provider
from app.services.recaptcha.base import RecaptchaProvider
from app.services.session_manager import SessionManager
from app.upstream.client import UpstreamClient, error_excerpt
from app.upstream.curl_runtime import apply_upstream_curl

logger = get_logger(__name__)


class MaxBridgeError(RuntimeError):
    """`max` katmanından yanıt alınamadığında fırlatılır."""


def compose_single_turn_max_prompt(system_prompt: str, user_prompt: str) -> str:
    """`system` ve `user` bölümlerini tek bir saf kullanıcı isteminde birleştirir.

    Hedef sitedeki web `max` arayüzü tek turluk düz kullanıcı metni bekler. İki ayrı
    mesaj (`system` + `user`) gönderildiğinde `prompt_builder` metni yapay bir
    `System: ... User: ... Assistant:` transkriptine sarmalar; tek `user` mesajı
    olarak birleştirmek bu yapay sarmalamayı sıfırlar ve talimat uyumunu güçlendirir.
    """
    sys_part = (system_prompt or "").strip()
    usr_part = (user_prompt or "").strip()
    if sys_part and usr_part:
        return f"{sys_part}\n\n---\n\n{usr_part}"
    return usr_part or sys_part


class MaxBridge(abc.ABC):
    """Durumsuz `max` çağrı arayüzü."""

    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    def account_identity(self) -> str:
        """Biliniyorsa `max` oturumunun JWT `sub` kimliği (hesap çakışma kontrolü için)."""
        return ""

    def account_identities(self) -> frozenset[str]:
        """Yapılandırılmış tüm `max` hesap yuvalarının JWT `sub` kimlikleri."""
        single = self.account_identity()
        return frozenset({single}) if single else frozenset()

    @abc.abstractmethod
    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        """Durumsuz tek turluk tamamlama isteği gönderir ve düz metin cevabı döndürür."""


class ExternalHttpMaxBridge(MaxBridge):
    """Dış ApiWrapper sunucusuna (`/v1/chat/completions` veya `/v1/ask`) bağlanır."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        model: str = "max",
        endpoint_mode: Literal["chat", "ask"] = "chat",
        timeout_seconds: float = 240.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        cleaned = base_url.strip().rstrip("/")
        if cleaned.endswith("/v1"):
            cleaned = cleaned[:-3]
        if not cleaned.startswith(("http://", "https://")):
            raise ValueError("MAX_BASE_URL 'http://' veya 'https://' ile başlamalıdır.")
        if not api_key or not api_key.strip():
            raise ValueError("MAX_API_KEY boş olamaz.")

        self.base_url = cleaned
        self.api_key = api_key.strip()
        self.model = (model or "max").strip()
        self.endpoint_mode = endpoint_mode
        self.timeout_seconds = timeout_seconds
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def startup(self) -> None:
        if self._client is not None:
            return
        kwargs: dict[str, object] = {
            "base_url": self.base_url,
            "headers": {
                "Authorization": f"Bearer {self.api_key}",
                "x-apiwrapper-source": "autopilot",
            },
            "timeout": httpx.Timeout(self.timeout_seconds, connect=15.0),
            "trust_env": False,
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        self._client = httpx.AsyncClient(**kwargs)  # type: ignore[arg-type]

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("ExternalHttpMaxBridge başlatılmadı; startup() çağırın.")
        return self._client

    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        if self.endpoint_mode == "ask":
            return await self._complete_via_ask(
                system_prompt=system_prompt, user_prompt=user_prompt
            )
        return await self._complete_via_chat(
            system_prompt=system_prompt, user_prompt=user_prompt
        )

    async def _complete_via_chat(self, *, system_prompt: str, user_prompt: str) -> str:
        combined_prompt = compose_single_turn_max_prompt(system_prompt, user_prompt)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "user", "content": combined_prompt},
            ],
            "stream": False,
        }
        try:
            response = await self.client.post("/v1/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise MaxBridgeError(f"Dış max servisine ulaşılamadı: {type(exc).__name__}") from exc

        if response.status_code != 200:
            raise MaxBridgeError(
                f"Dış max servisi HTTP {response.status_code} döndürdü: "
                f"{error_excerpt(response.text)}"
            )

        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise MaxBridgeError("Dış max servisi okunamayan OpenAI JSON yanıtı döndürdü.") from exc

        if not isinstance(content, str) or not content.strip():
            raise MaxBridgeError("Dış max servisi boş yanıt döndürdü.")
        return content.strip()

    async def _complete_via_ask(self, *, system_prompt: str, user_prompt: str) -> str:
        """`/v1/ask` SSE uç noktasını kullanarak (30 sn `wait` nabızlı) cevap alır."""
        combined_prompt = compose_single_turn_max_prompt(system_prompt, user_prompt)
        payload = {"prompt": combined_prompt, "model": self.model}
        request = self.client.build_request("POST", "/v1/ask", json=payload)
        try:
            response = await self.client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise MaxBridgeError(
                f"Dış max /v1/ask ucuna ulaşılamadı: {type(exc).__name__}"
            ) from exc

        try:
            if response.status_code != 200:
                raw = (await response.aread())[:2048].decode("utf-8", errors="replace")
                raise MaxBridgeError(
                    f"Dış max /v1/ask HTTP {response.status_code} döndürdü: {error_excerpt(raw)}"
                )
            async for line in response.aiter_lines():
                stripped = line.strip()
                if not stripped.startswith("data:"):
                    continue
                body = stripped[5:].strip()
                if not body:
                    continue
                try:
                    event = json.loads(body)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                status = event.get("status")
                if status == "wait":
                    continue
                if status == "done":
                    # `/v1/ask` sözleşmesi yanıtı `"text"` alanında döner (`ask_line`);
                    # geriye dönük uyumluluk için `"answer"` da yedek olarak kontrol edilir.
                    answer = str(event.get("text") or event.get("answer") or "").strip()
                    if not answer:
                        raise MaxBridgeError("Dış max /v1/ask boş yanıt ('text') döndürdü.")
                    return answer
                if status == "problem":
                    reason = event.get("reason") or "unknown"
                    raise MaxBridgeError(f"Dış max /v1/ask 'problem' bildirdi (reason={reason}).")
        finally:
            await response.aclose()

        raise MaxBridgeError("Dış max /v1/ask akışı nihai yanıt vermeden kapandı.")


class InProcessMaxBridge(MaxBridge):
    """Aynı süreçteki `CompletionService` üzerinden durumsuz tamamlama yapar."""

    def __init__(self, service: CompletionService, *, model: str = "max") -> None:
        self.service = service
        self.model = (model or "max").strip()

    def account_identity(self) -> str:
        settings = self.service.settings
        return extract_identity_subject(settings.upstream_cookie, settings.upstream_access_token)

    def account_identities(self) -> frozenset[str]:
        return extract_all_max_identities(self.service.settings)

    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        # `conversation_id=None` ve `client_identity=None` ile her tur yeni `chat_id` alır;
        # tek `user` mesajı ile `System:/User:/Assistant:` sarmalaması olmadan saf metin gider.
        combined_prompt = compose_single_turn_max_prompt(system_prompt, user_prompt)
        req = ChatCompletionRequest(
            model=self.model,
            messages=[
                ChatMessage(role="user", content=combined_prompt),
            ],
            stream=False,
            conversation_id=None,
        )
        try:
            resp = await self.service.create_completion(
                req,
                client_identity=None,
                admission=Admission(label="autopilot"),
            )
        except Exception as exc:
            raise MaxBridgeError(f"İç max servisi hatası ({type(exc).__name__}): {exc}") from exc

        content = resp.choices[0].message.content if resp.choices else None
        if not isinstance(content, str) or not content.strip():
            raise MaxBridgeError("İç max servisi boş yanıt döndürdü.")
        return content.strip()


class DirectCurlMaxBridge(MaxBridge):
    """Doğrudan ayrı bir `MAX_CURL` ile bellekte izole `max` motoru çalıştırır."""

    def __init__(self, raw_max_curl: str) -> None:
        if not raw_max_curl or not raw_max_curl.strip():
            raise ValueError("MAX_CURL metni boş.")
        base = Settings(  # type: ignore[call-arg]
            _env_file=None,
            upstream_curl=raw_max_curl.strip(),
            session_stateless=True,
            session_reuse=False,
        )
        settings, detected_model_id = apply_upstream_curl(base)
        if not detected_model_id:
            raise ValueError(
                "MAX_CURL gövdesinde 'modelAId' bulunamadı; tamamlanmış bir "
                "'create-evaluation' isteğinin cURL'ünü verin."
            )
        self.settings = settings
        self._upstream = UpstreamClient(settings)
        self._recaptcha: RecaptchaProvider = create_recaptcha_provider(settings)
        self._sessions = SessionManager(settings, warmup=self._upstream.warmup)
        self._registry = ModelRegistry([ModelEntry(id="max", upstream_id=detected_model_id)])
        self._registry.set_default("max")
        self._pool = build_account_pool(settings)
        self._lanes = LaneSet.from_settings(settings, self._pool)
        self._service = CompletionService(
            settings=settings,
            upstream=self._upstream,
            recaptcha=self._recaptcha,
            sessions=self._sessions,
            registry=self._registry,
            accounts=self._pool,
            lanes=self._lanes,
        )
        self._inner = InProcessMaxBridge(self._service, model="max")

    def account_identity(self) -> str:
        return extract_identity_subject(
            self.settings.upstream_cookie, self.settings.upstream_access_token
        )

    def account_identities(self) -> frozenset[str]:
        return extract_all_max_identities(self.settings)

    async def startup(self) -> None:
        await self._upstream.startup()
        await self._recaptcha.startup()

    async def shutdown(self) -> None:
        await self._sessions.clear()
        await self._recaptcha.shutdown()
        await self._upstream.shutdown()

    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        return await self._inner.complete(system_prompt=system_prompt, user_prompt=user_prompt)
