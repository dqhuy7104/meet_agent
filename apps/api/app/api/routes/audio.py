"""Audio processing endpoint."""

from __future__ import annotations

import logging
import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from app.api.deps import get_models
from app.core.audio_models import ModelLoadError, ModelRegistry
from app.pipelines.meeting_pipeline import process_audio
from app.schemas.transcript import AudioProcessResponse
from app.services.audio.preprocess import InvalidAudioError


logger = logging.getLogger(__name__)
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
router = APIRouter(prefix="/audio", tags=["audio"])


async def _run_pipeline(data: bytes, filename: str, models: ModelRegistry) -> AudioProcessResponse:
    """Run blocking model inference outside the request event loop."""
    return await asyncio.to_thread(process_audio, data, filename, models)


@router.post(
    "/process",
    response_model=AudioProcessResponse,
    summary="Transcribe uploaded audio and identify speakers",
    responses={400: {"description": "Invalid audio"}, 413: {"description": "Upload too large"}, 503: {"description": "Model unavailable"}},
)
async def process_audio_upload(
    file: Annotated[UploadFile, File(description="Audio file supported by FFmpeg (up to 25 MiB, 30 minutes)")],
    models: Annotated[ModelRegistry, Depends(get_models)],
) -> AudioProcessResponse:
    """Run Silero VAD, PhoWhisper ASR, and pyannote diarization on one audio file."""
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Audio file exceeds 25 MiB")
    try:
        data = await file.read(MAX_UPLOAD_BYTES + 1)
    finally:
        await file.close()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Audio file exceeds 25 MiB")
    try:
        return await _run_pipeline(data, file.filename or "audio", models)
    except InvalidAudioError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    except ModelLoadError as error:
        logger.error("Model unavailable for audio processing: %s", error.role, exc_info=True)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"{error.role} model unavailable") from error
