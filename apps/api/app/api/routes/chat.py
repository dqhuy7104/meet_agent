"""Loopback-only RAG endpoints with safe error bodies."""
import asyncio
from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from app.api.deps import get_rag_service
from app.api.routes.meetings import require_loopback
from app.core.rag_config import RagError
from app.schemas.chat import ProviderCatalog, RagAnswer, RagQuery


class SafeRagRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def safe(request: Request):
            try:
                return await asyncio.wait_for(handler(request), timeout=60)
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
