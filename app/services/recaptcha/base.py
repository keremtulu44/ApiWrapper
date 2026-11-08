"""reCAPTCHA sağlayıcı arayüzü ve üretim teşhisi."""

from __future__ import annotations

import abc
import time

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.metrics import metrics

logger = get_logger(__name__)


class RecaptchaProvider(abc.ABC):
    """Token üreten sağlayıcıların ortak arayüzü.

    Doğrulama token'ları tek kullanımlıktır; eşzamanlı istekler de dahil
    her çağrı ayrı bir token üretir. Tarayıcı kaynağının kilidi sağlayıcıdadır.
    """

    name: str = "base"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        #: Teşhis için son üretim/red bilgisi. Token'ın kendisi saklanmaz.
        self.stats: dict[str, object] = {
            "generated": 0,
            "rejected": 0,
            "last_generated_at": None,
            "last_rejected_at": None,
            "last_error": None,
        }

    async def startup(self) -> None:
        """Opsiyonel kaynak hazırlığı (alt sınıflar geçersiz kılabilir)."""
        return None

    async def shutdown(self) -> None:
        """Opsiyonel kaynak temizliği (alt sınıflar geçersiz kılabilir)."""
        return None

    @abc.abstractmethod
    async def _produce_token(self) -> str:
        """Yeni bir token üretir. Alt sınıflar uygular."""

    async def get_token(self) -> str:
        try:
            token = await self._produce_token()
        except Exception as exc:
            self.stats["last_error"] = str(exc)[:300]
            raise
        self.stats["generated"] = int(str(self.stats["generated"])) + 1
        self.stats["last_generated_at"] = time.time()
        self.stats["last_error"] = None
        metrics.inc("apiwrapper_recaptcha_tokens_total", labels={"provider": self.name})
        logger.debug("recaptcha_token_generated", provider=self.name, length=len(token))
        return token

    def invalidate(self) -> None:
        """Token upstream tarafından reddedildiğinde çağrılır."""
        self.stats["rejected"] = int(str(self.stats["rejected"])) + 1
        self.stats["last_rejected_at"] = time.time()
        logger.info("recaptcha_token_invalidated", provider=self.name)
        self.reset_context()

    def reset_context(self) -> None:
        """Oturum kimliği döndüğünde token **üretim bağlamını** sıfırlar.

        `invalidate`'dan farkı: red sayacı artmaz — oturum yenilendiğinde token
        reddedilmiş olmaz, yalnızca eski kimlikle kurulmuş sayfa/çerez/oturum
        bir daha kullanılmamalıdır. Upstream `Set-Cookie` ile veya arka plan
        yenileyicisiyle kimlik döndüğünde `UpstreamClient` bu metodu çağırır;
        aksi halde HTTP istemi taze çerezle, CAPTCHA üreten bağlam ise ölü
        oturumla çalışmaya devam eder.
        """
        return None
