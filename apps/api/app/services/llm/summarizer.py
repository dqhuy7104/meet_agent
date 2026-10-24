"""Bounded Vietnamese summaries of untrusted meeting transcripts."""

import json
from pathlib import Path

from pydantic import Field, ValidationError

from app.core.rag_config import ChatSelection, RagError, StrictModel
from app.schemas.transcript import TranscriptSegment
from app.services.rag.providers import ModelClient


PROMPT_VERSION = "meeting_summary_v1"
SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "meeting_summary_v1.txt").read_text()


class SummaryOutput(StrictModel):
    summary: str = Field(min_length=1, max_length=10000)


def _pieces(lines: list[str], max_bytes: int) -> list[str]:
    pieces = []
    current = ""
    for line in lines:
        for character in line + "\n":
            if len((current + character).encode("utf-8")) > max_bytes:
                pieces.append(current)
                current = ""
            current += character
    if current:
        pieces.append(current)
    return pieces


async def summarize_transcript(segments: list[TranscriptSegment], selection: ChatSelection,
                               client: ModelClient) -> str:
    """Summarize all nonblank segments within the selected model's context budget."""
    lines = [f"[{segment.start:.1f}-{segment.end:.1f}] {segment.speaker}: {segment.text.strip()}"
             for segment in segments if segment.text.strip()]
    if not lines:
        return ""
    input_bytes = max(256, (selection.model.context_tokens - selection.model.output_tokens - 768) * 2)
    summaries = _pieces(lines, input_bytes)
    for _ in range(5):
        results = []
        for text in summaries:
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps({"transcript": text}, ensure_ascii=False)},
            ]
            raw = await client.generate(selection, messages)
            try:
                summary = SummaryOutput.model_validate_json(raw).summary.strip()
                if not summary:
                    raise ValueError()
            except (ValueError, ValidationError):
                raise RagError("invalid_provider_response") from None
            results.append(summary)
        if len(results) == 1:
            return results[0]
        summaries = _pieces(results, input_bytes)
    raise RagError("summary_too_long")
