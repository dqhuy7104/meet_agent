"""Shared API dependencies."""

from app.core.audio_models import ModelRegistry, get_model_registry


async def get_models() -> ModelRegistry:
    """Provide the process-local lazy model registry to API routes."""
    return get_model_registry()


async def get_rag_service():
    """Keep PG work in one request-owned thread; SQLite stays on the event loop."""
    import asyncio
    import os
    from concurrent.futures import ThreadPoolExecutor
    import httpx
    import psycopg
    from app.core.config import get_settings
    from app.core.rag_config import load_rag_settings, ProviderRegistry, RagError
    from app.db.session import initialize_meeting_store, open_meeting_db
    from app.db.rag_session import open_rag_db
    from app.services.meetings.repository import MeetingRepository
    from app.services.rag.repository import RagRepository
    from app.services.rag.providers import ModelClient
    from app.services.rag.query import QueryService

    settings = load_rag_settings(os.environ)
    if not settings.enabled:
        raise RagError('rag_disabled')
    registry = ProviderRegistry(settings)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='rag-request')
    loop = asyncio.get_running_loop()
    connection = None
    sqlite = None
    try:
        try:
            connection = await loop.run_in_executor(executor, open_rag_db, settings)
        except psycopg.Error:
            raise RagError('index_unavailable') from None
        repository = RagRepository(connection)
        class Database:
            async def call(self, method, *args):
                try:
                    return await loop.run_in_executor(executor, getattr(repository, method), *args)
                except psycopg.Error:
                    raise RagError('index_unavailable') from None
        data_dir = get_settings().meeting_data_dir
        initialize_meeting_store(data_dir)
        sqlite = open_meeting_db(data_dir)
        async with httpx.AsyncClient() as http:
            yield QueryService(settings, registry, MeetingRepository(sqlite), Database(), ModelClient(registry, http))
    finally:
        if sqlite:
            sqlite.close()
        if connection:
            await loop.run_in_executor(executor, connection.close)
        await asyncio.to_thread(executor.shutdown, True)
