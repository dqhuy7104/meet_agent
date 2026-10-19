"""Shared API dependencies."""
import os
from app.core.audio_models import ModelRegistry, get_model_registry
from app.core.rag_config import load_rag_settings, RagError
from app.services.rag.request import RagRequestService


async def get_models() -> ModelRegistry:
    """Provide the process-local lazy model registry to API routes."""
    return get_model_registry()


async def get_rag_service() -> RagRequestService:
    """Parse opt-in configuration without opening storage on the API event loop."""
    settings = load_rag_settings(os.environ)
    if not settings.enabled:
        raise RagError('rag_disabled')
    return RagRequestService(settings)
