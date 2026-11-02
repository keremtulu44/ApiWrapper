"""FastAPI bağımlılıkları — uygulama state'inden servis çözümü."""

from __future__ import annotations

from fastapi import Request

from app.core.config import Settings
from app.services.completion_service import CompletionService
from app.services.model_registry import ModelRegistry
from app.services.recaptcha.base import RecaptchaProvider
from app.services.session_manager import SessionManager
from app.upstream.client import UpstreamClient


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_registry(request: Request) -> ModelRegistry:
    return request.app.state.registry


def get_completion_service(request: Request) -> CompletionService:
    return request.app.state.completion_service


def get_upstream(request: Request) -> UpstreamClient:
    return request.app.state.upstream


def get_sessions(request: Request) -> SessionManager:
    return request.app.state.sessions


def get_recaptcha(request: Request) -> RecaptchaProvider:
    return request.app.state.recaptcha
