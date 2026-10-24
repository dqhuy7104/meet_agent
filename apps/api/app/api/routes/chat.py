"""Loopback-only RAG endpoints with safe error bodies."""
import asyncio
from typing import Annotated
from fastapi import APIRouter, Depends, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from app.api.deps import get_rag_service
from app.api.routes.meetings import require_loopback
from app.core.rag_config import RagError
from app.schemas.chat import ProviderCatalog, RagAnswer, RagQuery, EvaluationState, MeetingSummary

QUERY_TIMEOUT_SECONDS = 60
SUMMARY_TIMEOUT_SECONDS = 190


class SafeRagRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def safe(request: Request):
            try:
                timeout = SUMMARY_TIMEOUT_SECONDS if request.url.path.endswith('/summary') else QUERY_TIMEOUT_SECONDS
                return await asyncio.wait_for(handler(request), timeout=timeout)
            except RequestValidationError:
                return JSONResponse(status_code=422, content={'detail': 'invalid_query'})
            except RagError as error:
                return JSONResponse(status_code=error.status, content={'detail': error.code})
            except asyncio.TimeoutError:
                return JSONResponse(status_code=504, content={'detail': 'query_timeout'})
        return safe


router = APIRouter(prefix='/chat', tags=['chat'], route_class=SafeRagRoute,
                   dependencies=[Depends(require_loopback)])


@router.get('/providers', response_model=ProviderCatalog)
async def providers(service=Depends(get_rag_service)) -> ProviderCatalog:
    return await service.catalog()


@router.post('/queries', response_model=RagAnswer)
async def query(request: RagQuery, service=Depends(get_rag_service)) -> RagAnswer:
    return await service.query(request)


@router.post('/meetings/{meeting_id}/summary', response_model=MeetingSummary)
async def meeting_summary(meeting_id: Annotated[str, Path(min_length=1, max_length=200)],
                          service=Depends(get_rag_service)) -> MeetingSummary:
    return MeetingSummary.model_validate(await service.summarize(meeting_id))


@router.get('/evaluation-state', response_model=EvaluationState)
async def evaluation_state(embedding_profile_id: str, chat_provider_id: str, chat_model_id: str,
                           service=Depends(get_rag_service)) -> dict:
    return await service.evaluation_state(embedding_profile_id, chat_provider_id, chat_model_id)
