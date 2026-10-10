"""FastAPI application entry point."""

from fastapi import FastAPI

from app.api.routes.audio import router as audio_router

app = FastAPI(title="Meeting AI API")
app.include_router(audio_router, prefix="/api")
