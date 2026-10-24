import type { ApiMeeting, MeetingStatus } from "../types/meeting";

import type { SpeakerIdentity, VoiceIdentificationStatus } from "../types/voice";

export type MeetingListResponse = { items: ApiMeeting[]; total: number; limit: number; offset: number };
export type BackendSearchHit = { meeting_id: string; title: string | null; speaker: string; speaker_name: string | null; start: number; end: number; text: string };
export type BackendSegment = { start: number; end: number; speaker: string; text: string };
export type BackendMeeting = ApiMeeting & { voice_identification_status: VoiceIdentificationStatus; speaker_identities: SpeakerIdentity[]; failure_code: string | null; transcript: string | null; vad_segments: {start:number;end:number}[]; diarization_segments: {start:number;end:number;speaker:string}[]; transcript_segments: BackendSegment[]; audio_files: { ordinal: number; original_filename: string; duration_seconds: number | null; start_seconds: number | null }[] };
export type MeetingSummary = { summary: string; provider_id: string; model_id: string; prompt_version: string };
export class ApiError extends Error { constructor(public status: number, message: string) { super(message); } }
export async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options);
  if (!response.ok) { const body = await response.json().catch(() => null) as { detail?: string } | null; throw new ApiError(response.status, body?.detail ?? "Không thể kết nối thư viện meeting local."); }
  return response.status === 204 ? undefined as T : response.json() as Promise<T>;
}
export const meetingApi = {
  list: (signal?: AbortSignal) => request<MeetingListResponse>("/api/meetings?limit=20&offset=0", { signal }),
  get: (id: string, signal?: AbortSignal) => request<BackendMeeting>(`/api/meetings/${encodeURIComponent(id)}`, { signal }),
  summary: (id: string, signal?: AbortSignal) => request<MeetingSummary>(`/api/chat/meetings/${encodeURIComponent(id)}/summary`, { method: "POST", signal }),
  upload: (files: File[], title: string) => { const form = new FormData(); files.forEach((file) => form.append("files", file)); if (title) form.append("title", title); return request<{meeting_id:string}>("/api/meetings", { method: "POST", body: form, headers: { "Idempotency-Key": crypto.randomUUID() } }); },
  retry: (id: string) => request(`/api/meetings/${encodeURIComponent(id)}/retry`, { method: "POST" }),
  remove: (id: string) => request<void>(`/api/meetings/${encodeURIComponent(id)}`, { method: "DELETE" }),
  audioUrl: (id: string, ordinal = 0) => `/api/meetings/${encodeURIComponent(id)}/audio/${ordinal}`,
};
