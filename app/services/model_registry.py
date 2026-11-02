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


@dataclass(slots=True)
class ModelEntry:
    """Tek bir model tanımı."""

    id: str
    upstream_id: str
    owned_by: str = "apiwrapper"
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    created: int = field(default_factory=lambda: int(time.time()))


class ModelRegistry:
    """YAML dosyasından model tanımlarını yükler ve çözümler."""

    def __init__(self, entries: list[ModelEntry] | None = None) -> None:
        self._entries: dict[str, ModelEntry] = {}
        self._index: dict[str, ModelEntry] = {}
        self._default_id: str | None = None
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
        for item in models:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id", "")).strip()
            upstream_id = str(item.get("upstream_id", "")).strip()
            if not model_id or not upstream_id:
                logger.warning("model_entry_skipped", entry=item)
                continue
            aliases = item.get("aliases") or []
            registry.add(
                ModelEntry(
                    id=model_id,
                    upstream_id=upstream_id,
                    owned_by=str(item.get("owned_by", "apiwrapper")),
                    aliases=[str(a).strip() for a in aliases if str(a).strip()],
                    description=str(item.get("description", "")),
                )
            )
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

    def list_models(self) -> list[ModelEntry]:
        return sorted(self._entries.values(), key=lambda e: e.id)

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, model: object) -> bool:
        return isinstance(model, str) and model.strip().lower() in self._index
