"""Shared API dependencies."""

from app.core.audio_models import ModelRegistry, get_model_registry


async def get_models() -> ModelRegistry:
    """Provide the process-local lazy model registry to API routes."""
    return get_model_registry()
