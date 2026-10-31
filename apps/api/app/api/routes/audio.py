"""Audio processing endpoint."""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from threading import BoundedSemaphore
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from app.api.deps import get_models
from app.core.audio_models import ModelLoadError, ModelRegistry
from app.core.config import get_settings
from app.pipelines.meeting_pipeline import process_audio
from app.schemas.transcript import AudioProcessResponse
from app.services.audio.preprocess import InvalidAudioError


logger = logging.getLogger(__name__)
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
router = APIRouter(prefix="/audio", tags=["audio"])


@lru_cache(maxsize=1)
def get_inference_slots() -> BoundedSemaphore:
    return BoundedSemaphore(get_settings().audio_max_concurrent_inferences)


async def _run_pipeline(data: bytes, filename: str, models: ModelRegistry) -> AudioProcessResponse:
    """Run blocking model inference outside the request event loop."""
    slots = get_inference_slots()
    if not slots.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="Audio inference is busy; retry later", headers={"Retry-After": "5"})

    def run() -> AudioProcessResponse:
        try:
            return process_audio(data, filename, models)
        finally:
            # Cancelling an HTTP request does not stop the inference thread.
            slots.release()

    try:
        future = asyncio.get_running_loop().run_in_executor(None, run)
    except RuntimeError:
        slots.release()
        raise
    # Observe failures even if the HTTP request was cancelled while inference ran.
    future.add_done_callback(lambda completed: completed.exception() if not completed.cancelled() else None)
    return await asyncio.shield(future)


@router.post(
    "/process",
    response_model=AudioProcessResponse,
    summary="Transcribe uploaded audio and identify speakers",
    responses={400: {"description": "Invalid audio"}, 413: {"description": "Upload too large"},
               429: {"description": "Audio inference capacity reached; retry later"}, 503: {"description": "Model unavailable"}},
)
async def process_audio_upload(
    file: Annotated[UploadFile, File(description="Audio file supported by FFmpeg (up to 100 MiB, 30 minutes)")],
    models: Annotated[ModelRegistry, Depends(get_models)],
) -> AudioProcessResponse:
    """Run VAD, language-routed ASR, and speaker diarization on one audio file."""
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Audio file exceeds 100 MiB")
    try:
        audio_bytes = await file.read(MAX_UPLOAD_BYTES + 1)
    finally:
        await file.close()
    if len(audio_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Audio file exceeds 100 MiB")
    try:
        return await _run_pipeline(audio_bytes, file.filename or "audio", models)
    except InvalidAudioError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    except ModelLoadError as error:
        logger.error("Model unavailable for audio processing: %s", error.role, exc_info=True)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"{error.role} model unavailable") from error
