"""Request-owned execution: all blocking storage work stays off the API loop."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field
import threading
import time
from app.core.database_config import DatabaseSettings, DatabaseUnavailableError
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
    def __init__(self, settings: RagSettings, database: DatabaseSettings) -> None:
        self.settings = settings
        self.database = database
        self.registry = ProviderRegistry(settings)

    async def _invoke(self, action: str, *arguments):
        deadline = RequestDeadline(expires_at=time.monotonic() + (180 if action == 'summarize' else 60))
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='rag-request')
        future = executor.submit(self._serve, deadline, action, arguments)
        try:
            return await asyncio.wrap_future(future)
        finally:
            deadline.cancelled.set()
            # The owning thread closes its own resources. Cancellation never joins it.
            executor.shutdown(wait=False, cancel_futures=True)

    def _serve(self, deadline: RequestDeadline, action: str, arguments: tuple):
        import httpx
        import psycopg
        from app.db.session import open_database, validate_database
        from app.services.meetings.repository import MeetingRepository
        from app.services.rag.providers import ModelClient
        from app.services.rag.query import QueryService
        from app.services.rag.repository import RagRepository

        async def run():
            deadline.remaining()
            try:
                with open_database(self.database, timeout_seconds=min(5, deadline.remaining())) as connection:
                    def before_query():
                        remaining = deadline.remaining()
                        connection.execute("SELECT set_config('statement_timeout',%s,false)",
                            (str(max(1, int(min(10, remaining)*1000))),))
                    before_query()
                    validate_database(connection)
                    repository = RagRepository(connection)
                    meetings = MeetingRepository(connection)
                    class Source:
                        def get_detail(inner, meeting_id):
                            before_query()
                            detail = meetings.get_detail(meeting_id)
                            deadline.remaining()
                            return detail
                        def completed_meeting_ids(inner):
                            before_query()
                            return meetings.completed_meeting_ids()
                    class Database:
                        async def call(inner, method, *args):
                            before_query()
                            result = getattr(repository, method)(*args)
                            deadline.remaining()
                            return result
                    async with httpx.AsyncClient() as http:
                        client = ModelClient(self.registry, http, deadline.remaining)
                        service = QueryService(self.settings, self.registry, Source(), Database(), client)
                        return await getattr(service, action)(*arguments)
            except (psycopg.Error, DatabaseUnavailableError):
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

    async def summarize(self, meeting_id: str) -> dict[str, str]:
        return await self._invoke('summarize', meeting_id)

    async def evaluation_state(self, profile_id: str, provider_id: str, model_id: str):
        return await self._invoke('evaluation_state', profile_id, provider_id, model_id)
