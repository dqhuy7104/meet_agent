"""Loopback-only RAG endpoints with safe error bodies."""
import asyncio
from typing import Annotated
from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from app.api.deps import get_rag_service
from app.api.auth import CurrentAccount, require_account, require_csrf
from app.api.routes.meetings import require_loopback
from app.core.rag_config import RagError
from app.schemas.chat import ProviderCatalog, RagAnswer, RagQuery, EvaluationState, MeetingSummary, IndexStatus

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
async def providers(account: Annotated[CurrentAccount, Depends(require_account)], service=Depends(get_rag_service),
                    project_id: Annotated[str | None, Query(min_length=1, max_length=200)] = None) -> ProviderCatalog:
    return await service.catalog(account.id, project_id)


@router.get('/index-status', response_model=IndexStatus)
async def index_status(account: Annotated[CurrentAccount, Depends(require_account)], service=Depends(get_rag_service),
                       project_id: Annotated[str | None, Query(min_length=1, max_length=200)] = None) -> IndexStatus:
    return await service.index_status(account.id, project_id)


@router.post('/queries', response_model=RagAnswer, dependencies=[Depends(require_csrf)])
async def query(request: RagQuery, account: Annotated[CurrentAccount, Depends(require_account)], service=Depends(get_rag_service)) -> RagAnswer:
    return await service.query(request, account.id)


@router.post('/meetings/{meeting_id}/summary', response_model=MeetingSummary, dependencies=[Depends(require_csrf)])
async def meeting_summary(meeting_id: Annotated[str, Path(min_length=1, max_length=200)],
                          account: Annotated[CurrentAccount, Depends(require_account)], service=Depends(get_rag_service)) -> MeetingSummary:
    return MeetingSummary.model_validate(await service.summarize(meeting_id, account.id))


@router.get('/evaluation-state', response_model=EvaluationState)
async def evaluation_state(embedding_profile_id: str, chat_provider_id: str, chat_model_id: str,
                           account: Annotated[CurrentAccount, Depends(require_account)], service=Depends(get_rag_service),
                           context_weight: Annotated[float, Query(ge=0, le=1, allow_inf_nan=False)] = .8) -> dict:
    return await service.evaluation_state(embedding_profile_id, chat_provider_id, chat_model_id, account.id, context_weight)
