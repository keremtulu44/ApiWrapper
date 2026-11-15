"""Model kayıt defteri: `config/models.yaml` → OpenAI model adı ↔ upstream modelAId."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.core.errors import ModelNotFoundError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Bilinen modality değerleri. Liste **kısıtlayıcı değil**: hedef yeni bir mod
#: eklerse (`video`, `agent` …) kayıt defteri onu olduğu gibi geçirir, yalnızca
#: yazım hatası ihtimaline karşı uyarı loglanır. Sert doğrulama yeni bir modu
#: kullanılamaz hâle getirirdi.
KNOWN_MODALITIES = frozenset({"chat", "search", "image"})

#: Modality yazılmadığında kullanılan değer (eski davranışın birebir karşılığı).
DEFAULT_MODALITY = "chat"

#: YAML'da bayrak yanlışlıkla metin yazıldığında ("false" metni Python'da doğru
#: sayılır) da doğru çözülsün diye açık liste.
_FALSEY = frozenset({"false", "0", "no", "hayir", "hayır", "kapali", "kapalı"})


def _as_flag(value: Any, default: bool = True) -> bool:
    """YAML değerini bool'a çevirir; boş/eksik değer `default` olur."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return default if not text else text not in _FALSEY


@dataclass(slots=True)
class ModelEntry:
    """Tek bir model tanımı."""

    id: str
    upstream_id: str
    owned_by: str = "apiwrapper"
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    created: int = field(default_factory=lambda: int(time.time()))
    #: Gövdedeki `modality` alanı. Aynı `modelAId` üç modu da taşıyor; modu
    #: seçen şey bu alan + referer yoludur (ölçüldü: chat/web/görsel cURL'lerinde
    #: çerez, deployment damgası, `mode` ve `modelAId` birebir aynıydı).
    modality: str = DEFAULT_MODALITY
    #: Bu moda ait referer yolu (örn. `/search/direct`). Boşsa ayarlardaki
    #: `UPSTREAM_REFERER_PATH` kullanılır.
    referer_path: str = ""
    #: `False` ise kayıt **vitrinde görünmez** (`/v1/models` listesi) ama
    #: çalışmaya devam eder: `model=<ad>` açıkça istendiğinde çözülür. İkincil/
    #: deneysel modları istemcinin model listesinden düşürmek için kullanılır.
    listed: bool = True


class ModelRegistry:
    """YAML dosyasından model tanımlarını yükler ve çözümler."""

    def __init__(self, entries: list[ModelEntry] | None = None) -> None:
        self._entries: dict[str, ModelEntry] = {}
        self._index: dict[str, ModelEntry] = {}
        self._default_id: str | None = None
        #: Belge düzeyindeki `upstream_id`'den **miras alan** kayıtlar. Yeni bir
        #: cURL uygulandığında kimliğin kime yazılacağını bu küme belirler; kendi
        #: `upstream_id`'sini açıkça taşıyan kayıt burada yoktur ve dokunulmaz.
        self._inherited_ids: set[str] = set()
        for entry in entries or []:
            self.add(entry)

    # ------------------------------------------------------------- loading
    @classmethod
    def from_file(cls, path: str | Path) -> ModelRegistry:
        file_path = Path(path)
        if not file_path.is_file():
            raise FileNotFoundError(
                f"Models file not found: {file_path}. "
                "Create it (see config/models.yaml) or set MODELS_FILE."
            )
        with file_path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ModelRegistry:
        registry = cls()
        models = raw.get("models") or []
        if not isinstance(models, list):
            raise ValueError("'models' key must be a list in the models file.")
        # Belge düzeyindeki `upstream_id`, kayıtların miras aldığı ortak modelAId'dir.
        # Chat/search/image modları aynı modelAId'i kullanır (ölçüldü); kimliği üç
        # kayda birden yazmak, `curl_to_env.py` tek satırı güncellediğinde ikisinin
        # bayat kalması demekti. Kayıt kendi `upstream_id`'sini taşıyorsa o önceliklidir.
        shared_upstream_id = str(raw.get("upstream_id") or "").strip()
        for item in models:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id", "")).strip()
            own_upstream_id = str(item.get("upstream_id", "")).strip()
            upstream_id = own_upstream_id or shared_upstream_id
            if not model_id or not upstream_id:
                logger.warning("model_entry_skipped", entry=item)
                continue
            aliases = item.get("aliases") or []
            modality = str(item.get("modality") or DEFAULT_MODALITY).strip().lower()
            if modality and modality not in KNOWN_MODALITIES:
                # Reddedilmez: hedef yeni bir mod eklediyse çalışmaya devam etsin.
                logger.warning(
                    "model_modality_unknown",
                    model=model_id,
                    modality=modality,
                    known=sorted(KNOWN_MODALITIES),
                )
            registry.add(
                ModelEntry(
                    id=model_id,
                    upstream_id=upstream_id,
                    owned_by=str(item.get("owned_by", "apiwrapper")),
                    aliases=[str(a).strip() for a in aliases if str(a).strip()],
                    description=str(item.get("description", "")),
                    modality=modality or DEFAULT_MODALITY,
                    referer_path=str(item.get("referer_path") or "").strip(),
                    listed=_as_flag(item.get("listed"), default=True),
                )
            )
            if not own_upstream_id:
                registry._inherited_ids.add(model_id)
        default = raw.get("default")
        if default:
            registry.set_default(str(default))
        if not registry._entries:
            raise ValueError("Models file contains no valid model entries.")
        logger.info("models_loaded", count=len(registry._entries))
        return registry

    # ---------------------------------------------------------------- crud
    def add(self, entry: ModelEntry) -> None:
        self._entries[entry.id] = entry
        self._index[entry.id.lower()] = entry
        for alias in entry.aliases:
            self._index[alias.lower()] = entry
        if self._default_id is None:
            self._default_id = entry.id

    def set_default(self, model_id: str) -> None:
        entry = self._index.get(model_id.lower())
        if entry is None:
            logger.warning("default_model_unknown", model=model_id)
            return
        self._default_id = entry.id

    def apply_shared_upstream_id(self, model_id: str) -> list[str]:
        """cURL'den çıkan `modelAId`'i ortak kimliği miras alan kayıtlara yazar.

        Gövdedeki kimlik kayıt defterinden gelir (`completion_service` →
        `entry.upstream_id`); `settings.upstream_captured_model_id` yalnızca
        teşhistir. Aynı `modelAId` üç modu da taşıdığı için (15 Kasım ölçümü)
        yeni bir cURL uygulandığında üçünün birden güncellenmesi gerekir —
        aksi hâlde varsayılan model yeni kimlikle çalışırken `web` ve `image`
        dosyadaki bayat id'yi gönderir ve hata sessiz kalır.

        Kendi `upstream_id`'sini açıkça yazan kayıt miras almaz, dokunulmaz
        (bir mod bilinçli olarak farklı bir model kullanabilir).

        Miras bilgisi yoksa — belge düzeyinde kimlik yok ya da kayıt defteri
        elle kurulmuş — eski davranış korunur: yalnız varsayılan kayıt güncellenir.

        Yeni kimliğe bağlanan model adlarını döndürür (değer zaten aynıysa da
        listelenir; log "bu kimlik hangi modları taşıyor" sorusuna cevap versin).
        """
        new_id = (model_id or "").strip()
        if not new_id:
            return []
        targets = [
            self._entries[entry_id]
            for entry_id in self._inherited_ids
            if entry_id in self._entries
        ]
        if not targets:
            try:
                targets = [self.resolve(None)]
            except ModelNotFoundError:
                return []
        for entry in targets:
            entry.upstream_id = new_id
        return sorted(entry.id for entry in targets)

    # ------------------------------------------------------------- lookups
    def resolve(self, model: str | None) -> ModelEntry:
        """Model adını (veya alias'ını) çözer; bulunamazsa 404 fırlatır."""
        if not model:
            if self._default_id is None:
                raise ModelNotFoundError("No models are configured.")
            return self._entries[self._default_id]
        entry = self._index.get(model.strip().lower())
        if entry is None:
            available = ", ".join(sorted(self._entries)[:20])
            raise ModelNotFoundError(
                f"The model '{model}' does not exist. Available models: {available}",
                param="model",
            )
        return entry

    def list_models(self, listed_only: bool = False) -> list[ModelEntry]:
        """Kayıtları ada göre sıralı döndürür.

        `listed_only=True` yalnız vitrine çıkacak kayıtları verir (`/v1/models`).
        `listed: false` işaretli mod silinmez: `resolve()` onu çözmeye devam
        eder, yani istemci adı açıkça yazdığında mod çalışır. Teşhis
        (`scripts/compare_curl.py`) ve sözleşme testleri varsayılanla — tüm
        kayıtlar — çalıştığı için ikincil modlar orada görünür kalır.
        """
        entries = (entry for entry in self._entries.values()
                   if entry.listed or not listed_only)
        return sorted(entries, key=lambda e: e.id)

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, model: object) -> bool:
        return isinstance(model, str) and model.strip().lower() in self._index
