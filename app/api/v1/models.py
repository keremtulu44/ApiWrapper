"""`/v1/models` uç noktaları."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_registry
from app.core.security import api_key_dependency
from app.schemas.openai import ModelCard, ModelList
from app.services.model_registry import ModelRegistry

router = APIRouter(tags=["models"], dependencies=[Depends(api_key_dependency)])


def _to_card(entry) -> ModelCard:
    return ModelCard(id=entry.id, created=entry.created, owned_by=entry.owned_by)


@router.get("/models", response_model=ModelList, summary="List available models")
async def list_models(registry: ModelRegistry = Depends(get_registry)) -> ModelList:
    # `listed_only=True`: `listed: false` işaretli ikincil modlar istemcinin
    # model listesinde görünmez (Open WebUI/Cursor açılır listesi bozuk bir modu
    # önermesin) ama `resolve()` çalışmaya devam eder — ad açıkça istenirse mod
    # kullanılır ve `GET /v1/models/{id}` onu döndürür.
    return ModelList(
        data=[_to_card(entry) for entry in registry.list_models(listed_only=True)]
    )


@router.get("/models/{model_id:path}", response_model=ModelCard, summary="Retrieve a model")
async def retrieve_model(
    model_id: str, registry: ModelRegistry = Depends(get_registry)
) -> ModelCard:
    return _to_card(registry.resolve(model_id))
