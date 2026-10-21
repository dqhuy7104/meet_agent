"""FastAPI application entry point."""

from fastapi import FastAPI
from fastapi.responses import JSONResponse
import psycopg
from app.core.database_config import DatabaseUnavailableError

from app.api.routes.audio import router as audio_router
from app.api.routes.meetings import router as meetings_router
from app.api.routes.bot_sessions import router as bot_sessions_router
from app.api.routes.chat import router as chat_router

app = FastAPI(title="Meeting AI API")
app.include_router(audio_router, prefix="/api")
app.include_router(meetings_router, prefix="/api")
app.include_router(bot_sessions_router, prefix="/api")
app.include_router(chat_router, prefix="/api")


@app.exception_handler(DatabaseUnavailableError)
async def storage_unavailable(request, error: DatabaseUnavailableError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": error.code})


@app.exception_handler(psycopg.Error)
async def database_unavailable(request, error: psycopg.Error) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": "database_unavailable"})
