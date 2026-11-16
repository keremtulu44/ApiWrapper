"""Proje anahtarları: anahtar ↔ proje etiketi + kullanım muhasebesi.

Wrapper başka projelere açıldığında ilk ihtiyaç "hangi proje ne kadar kullandı"
sorusunun cevabıdır. Bu modül yalnız onu karşılar:

* Anahtar **değeri** burada tutulmaz ve loglanmaz; kimlik `client_fingerprint()`
  (`cli_<sha256[:12]>`) üzerinden kurulur — mevcut gizlilik kuralı aynen korunur.
  Listelemede anahtar `mask_value()` ile maskelenir.
* Politika (model izni, günlük bütçe, kuyruk) bilerek **yok**: 1-2 tüketicili
  ölçekte anahtar başına hız sınırı (`app/core/middleware.py`) yeterli. İhtiyaç
  doğarsa genişleme noktası `ProjectUsage`/`snapshot()`, yeni bir uç nokta değil.
* Sayaçlar bellek içidir: restart'ta sıfırlanır (Free Render'da kalıcı disk yok).
  Kalıcı muhasebe gerekiyorsa `/metrics` dışarıda toplanır (Prometheus).
* Etiketler `API_KEY_LABELS` alanında (`cli_x:etiket,cli_y:etiket`) taşınır ve
  mevcut state/env düzeniyle kalıcı olur — yeni bir depo gerekmez.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any

from app.core.config import Settings
from app.core.errors import InvalidRequestError
from app.core.logging import get_logger, mask_value
from app.core.metrics import metrics
from app.core.security import ANONYMOUS, client_fingerprint
from app.upstream.state_store import save_runtime_state

logger = get_logger(__name__)

#: Etiket `,` ve `:` taşıyamaz — ikisi de eşlemenin ayıracı.
_FORBIDDEN_LABEL_CHARS = ",:"
_LABEL_MAX_CHARS = 40
_KEY_PREFIX = "sk-"

#: Auth kapalıyken (`ANONYMOUS`) gelen istekler bu kimlikle muhasebeye girer.
ANONYMOUS_KEY_ID = ANONYMOUS


@dataclass(slots=True)
class ProjectUsage:
    """Bir projenin bellek içi kullanım sayaçları."""

    requests: int = 0
    tokens: int = 0
    errors: int = 0
    last_used: float | None = None


def normalize_label(raw: str) -> str:
    """Etiketi depolanabilir biçime getirir; boşalırsa `""` döner.

    Ayraçlar (``,``/`:`) ve boşluk yığınları tek `-` olur, uzunluk sınırlanır:
    eşleme tek bir env alanında taşındığı için biçim bozulursa tüm etiketler
    kayardı.
    """
    text = (raw or "").strip()
    for char in _FORBIDDEN_LABEL_CHARS:
        text = text.replace(char, "-")
    text = "-".join(part for part in text.split() if part)
    return text[:_LABEL_MAX_CHARS].strip("-")


def parse_labels(raw: str) -> dict[str, str]:
    """`cli_x:etiket,cli_y:etiket` → `{key_id: label}` (bozuk parçalar atlanır)."""
    labels: dict[str, str] = {}
    for part in (raw or "").split(","):
        key_id, separator, label = part.strip().partition(":")
        if not separator or not key_id or not label.strip():
            continue
        labels[key_id.strip().lower()] = normalize_label(label)
    return {key_id: label for key_id, label in labels.items() if label}


def format_labels(labels: dict[str, str]) -> str:
    """`{key_id: label}` → env/state alanına yazılacak metin."""
    return ",".join(f"{key_id}:{label}" for key_id, label in sorted(labels.items()))


class ProjectRegistry:
    """Anahtar parmak izi ↔ proje etiketi + kullanım sayaçları.

    Kayıt **anahtar değerini saklamaz**: liste her zaman `settings.api_keys`
    üzerinden anlık kurulur, böylece iptal edilen anahtar kendiliğinden listeden
    düşer ve iki ayrı kaynak birbirinden ayrılamaz.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._labels: dict[str, str] = {}
        self._usage: dict[str, ProjectUsage] = {}
        if settings is not None:
            self._labels = parse_labels(settings.api_key_labels)

    # ---------------------------------------------------------------- etiket
    def label_for(self, key_id: str | None) -> str:
        """Proje etiketi; atanmamışsa `(etiketsiz)`, kimliksizse `(kimliksiz)`."""
        ident = (key_id or "").strip()
        if not ident or ident == ANONYMOUS_KEY_ID:
            return "(kimliksiz)"
        return self._labels.get(ident.lower(), "(etiketsiz)")

    def bind_label(self, key_id: str, label: str) -> str:
        """Etiketi atar ve normalize edilmiş hâlini döndürür."""
        clean = normalize_label(label)
        if not clean:
            raise InvalidRequestError(
                "label must contain at least one letter or digit.",
                code="invalid_label",
            )
        ident = (key_id or "").strip().lower()
        if not ident:
            raise InvalidRequestError("key_id is required.", code="invalid_key_id")
        self._labels[ident] = clean
        return clean

    def export_labels(self) -> str:
        return format_labels(self._labels)

    # ------------------------------------------------------------ muhasebe
    def record(
        self, key_id: str | None, *, tokens: int = 0, error: bool = False
    ) -> None:
        """Bir isteği proje hanesine yazar (istenir/hata/token).

        Yalnız sayaç ve metrik üretir; isteğin kendisine dokunmaz, bu yüzden
        hata durumunda bile akışı bozmaz. Metrik etiketi **etikettir, anahtar
        değil** — cardinality yönetici tarafından kontrol edilir (1-2 proje).
        """
        ident = ((key_id or "").strip() or ANONYMOUS_KEY_ID).lower()
        usage = self._usage.setdefault(ident, ProjectUsage())
        usage.requests += 1
        usage.tokens += max(0, int(tokens or 0))
        if error:
            usage.errors += 1
        usage.last_used = time.time()

        project = self.label_for(ident)
        metrics.inc("apiwrapper_project_requests_total", labels={"project": project})
        if error:
            metrics.inc("apiwrapper_project_errors_total", labels={"project": project})
        if tokens:
            metrics.inc(
                "apiwrapper_project_tokens_total",
                value=float(max(0, int(tokens))),
                labels={"project": project},
            )

    # ------------------------------------------------------- üretim / iptal
    def mint(self, settings: Settings, label: str) -> tuple[str, str]:
        """Yeni proje anahtarı üretir: `(anahtar_değeri, key_id)`.

        Anahtar değeri **yalnız bu dönüşte** görünür; depoya, log'a ve env'e
        düz metin olarak yalnız `API_KEYS` listesine girer (mevcut düzen).
        Redeploy gerekmez: doğrulama `settings.api_keys`'i anlık okur.
        """
        clean = normalize_label(label)
        if not clean:
            raise InvalidRequestError(
                "label must contain at least one letter or digit.",
                code="invalid_label",
            )
        existing = set(settings.api_keys)
        candidate = ""
        for _ in range(5):
            proposal = _KEY_PREFIX + secrets.token_urlsafe(24)
            if proposal not in existing:
                candidate = proposal
                break
        if not candidate:  # pragma: no cover - 32 bayt rastgelelikte pratik değil
            raise InvalidRequestError(
                "Could not generate a unique API key; retry.", code="key_generation_failed"
            )

        settings.api_keys.append(candidate)
        key_id = client_fingerprint(candidate)
        self._labels[key_id.lower()] = clean
        settings.api_key_labels = self.export_labels()
        if not any(key.strip() for key in settings.admin_api_keys):
            # `ADMIN_API_KEYS` boşken **her** anahtar admin sayılır (geriye dönük
            # uyumluluk, `require_admin`). Tüketiciye verilen anahtar o durumda
            # çerezi değiştirebilir ve başka anahtar üretebilir — bu uyarı
            # tam olarak o yüzden üretim anında yazılır.
            logger.warning(
                "project_key_minted_without_admin_separation",
                key_id=key_id,
                hint="ADMIN_API_KEYS boş: üretilen anahtar /v1/admin/* erişimine de sahip.",
            )
        save_runtime_state(settings, source="project_key_minted")
        logger.info(
            "project_key_minted",
            key_id=key_id,
            label=clean,
            masked_key=mask_value(candidate),
            total_keys=len(settings.api_keys),
        )
        return candidate, key_id

    def revoke(self, settings: Settings, key_id: str) -> str:
        """Anahtarı listeden düşürür; maskelenmiş anahtarı döndürür.

        Son anahtar iptal edilemez: wrapper'ı herkese kapatır ve geri almak
        için Render env'ine elle müdahale gerekir.
        """
        target = (key_id or "").strip().lower()
        if not target:
            raise InvalidRequestError("key_id is required.", code="invalid_key_id")
        matches = [key for key in settings.api_keys if client_fingerprint(key) == target]
        if not matches:
            raise InvalidRequestError(
                f"No API key matches key_id '{target}'.", code="key_not_found"
            )
        if len(matches) >= len(settings.api_keys):
            raise InvalidRequestError(
                "Refusing to revoke the last API key: the wrapper would reject "
                "every consumer. Add another key first.",
                code="last_key_cannot_be_revoked",
            )

        revoked = set(matches)
        settings.api_keys = [key for key in settings.api_keys if key not in revoked]
        if settings.admin_api_keys:
            # Admin listesi ayrı tutulur: yalnız istemci anahtarını düşürmek,
            # iptal edilen anahtarla `/v1/admin/*` erişimini açık bırakırdı.
            settings.admin_api_keys = [
                key for key in settings.admin_api_keys if key not in revoked
            ]
        label = self._labels.pop(target, "")
        self._usage.pop(target, None)
        settings.api_key_labels = self.export_labels()
        save_runtime_state(settings, source="project_key_revoked")
        logger.info(
            "project_key_revoked",
            key_id=target,
            label=label,
            remaining_keys=len(settings.api_keys),
        )
        return mask_value(matches[0])

    # ------------------------------------------------------------- listeleme
    def snapshot(self, settings: Settings) -> list[dict[str, Any]]:
        """Projeleri kullanım sayaçlarıyla listeler (anahtar değeri maskeli)."""
        # `require_admin` ile aynı kural: `ADMIN_API_KEYS` boşsa **her** anahtar
        # admin sayılır. Alanı "listede yazıyor mu" diye doldurmak, tam da riskin
        # olduğu durumda (ayrım yok) "bu anahtar admin değil" yalanını söylerdi.
        separated = any(item.strip() for item in settings.admin_api_keys)
        rows: list[dict[str, Any]] = []
        for key in settings.api_keys:
            key_id = client_fingerprint(key)
            usage = self._usage.get(key_id.lower()) or ProjectUsage()
            rows.append(
                {
                    "key_id": key_id,
                    "label": self.label_for(key_id),
                    "masked_key": mask_value(key),
                    "is_admin": (not separated) or key in settings.admin_api_keys,
                    "requests": usage.requests,
                    "tokens": usage.tokens,
                    "errors": usage.errors,
                    "last_used": usage.last_used,
                }
            )
        rows.sort(key=lambda row: row["label"])
        return rows
