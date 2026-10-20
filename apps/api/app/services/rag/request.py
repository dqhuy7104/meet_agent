"""Request-owned execution: all blocking storage work stays off the API loop."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field
import threading
import time
from app.core.rag_config import RagError, RagSettings, ProviderRegistry
from app.schemas.chat import RagQuery


@dataclass
class RequestDeadline:
    expires_at: float = field(default_factory=lambda: time.monotonic() + 60)
    cancelled: threading.Event = field(default_factory=threading.Event)

    def remaining(self) -> float:
        remaining = self.expires_at - time.monotonic()
        if self.cancelled.is_set() or remaining <= 0:
            raise RagError('query_timeout', 504)
        return remaining


class RagRequestService:
    def __init__(self, settings: RagSettings) -> None:
        self.settings = settings
        self.registry = ProviderRegistry(settings)

    async def _invoke(self, action: str, *arguments):
        deadline = RequestDeadline()
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='rag-request')
        future = executor.submit(self._serve, deadline, action, arguments)
        try:
            return await asyncio.wrap_future(future)
        finally:
            deadline.cancelled.set()
            # The owning thread closes its own resources. Cancellation never joins it.
            executor.shutdown(wait=False, cancel_futures=True)

    def _serve(self, deadline: RequestDeadline, action: str, arguments: tuple):
        import sqlite3
        import httpx
        import psycopg
        from app.core.config import get_settings
        from app.db.rag_session import open_rag_db
        from app.db.session import open_meeting_db
        from app.services.meetings.repository import MeetingRepository
        from app.services.rag.providers import ModelClient
        from app.services.rag.query import QueryService
        from app.services.rag.repository import RagRepository

        async def run():
            deadline.remaining()
            try:
                with open_rag_db(self.settings) as connection:
                    deadline.remaining()
                    with closing(open_meeting_db(get_settings().meeting_data_dir, timeout=.25)) as sqlite:
                        sqlite.set_progress_handler(lambda: int(deadline.cancelled.is_set() or time.monotonic() >= deadline.expires_at), 1000)
                        repository = RagRepository(connection)
                        class Database:
                            async def call(self, method, *args):
                                remaining = deadline.remaining()
                                connection.execute("SELECT set_config('statement_timeout',%s,false)", (str(max(1, int(min(10, remaining)*1000))),))
                                return getattr(repository, method)(*args)
                        async with httpx.AsyncClient() as http:
                            client = ModelClient(self.registry, http, deadline.remaining)
                            service = QueryService(self.settings, self.registry, MeetingRepository(sqlite), Database(), client)
                            return await getattr(service, action)(*arguments)
            except (psycopg.Error, sqlite3.Error):
                deadline.remaining()
                raise RagError('index_unavailable') from None
        return asyncio.run(run())

    async def catalog(self):
        return await self._invoke('catalog')

    async def query(self, request: RagQuery):
        self.registry.resolve_chat(request.chat_provider_id, request.chat_model_id)
        self.registry.resolve_profile(request.embedding_profile_id)
        if request.k > self.settings.max_k:
            raise RagError('invalid_k', 422)
        return await self._invoke('query', request)

    async def evaluation_state(self, profile_id: str, provider_id: str, model_id: str):
        return await self._invoke('evaluation_state', profile_id, provider_id, model_id)
