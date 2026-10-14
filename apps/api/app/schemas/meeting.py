"""Contracts for the local meeting library."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class MeetingAcceptedResponse(BaseModel):
    meeting_id: str
    title: str | None
    original_filename: str
    status: Literal["queued"]
    created_at: str
    updated_at: str
