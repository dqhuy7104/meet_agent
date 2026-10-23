import { ApiError, request } from './api';
import type { EnrollmentJob, SpeakerIdentity, VoiceProfile, VoiceSample } from '../types/voice';
const profilePath = (id: string) => `/api/voice-profiles/${encodeURIComponent(id)}`;
const json = (method: string, body: unknown): RequestInit => ({method, headers: {'Content-Type':'application/json'}, body: JSON.stringify(body)});
export const voiceApi = {
  list: (signal?: AbortSignal) => request<VoiceProfile[]>('/api/voice-profiles', {signal}),
  create: (display_name: string) => request<VoiceProfile>('/api/voice-profiles', json('POST',{display_name})),
  rename: (id: string, display_name: string) => request<VoiceProfile>(profilePath(id), json('PATCH',{display_name})),
  remove: (id: string) => request<void>(profilePath(id), {method:'DELETE'}),
  samples: (id: string, signal?: AbortSignal) => request<VoiceSample[]>(`${profilePath(id)}/samples`,{signal}),
  removeSample: (id: string, sampleId: number) => request<void>(`${profilePath(id)}/samples/${sampleId}`,{method:'DELETE'}),
  upload: (id: string, file: Blob) => {
    const body = new FormData(); body.append('file',file);
    return request<EnrollmentJob>(`${profilePath(id)}/samples/upload`,{method:'POST',body});
  },
  fromMeeting: (id: string, meeting_id: string, speaker: string) => request<EnrollmentJob>(`${profilePath(id)}/samples/from-meeting`, json('POST',{meeting_id,speaker})),
  job: (id: string, signal?: AbortSignal) => request<EnrollmentJob>(`/api/voice-enrollment-jobs/${encodeURIComponent(id)}`,{signal}),
  correct: (meetingId: string, speaker: string, profile_id: string | null) => request<SpeakerIdentity>(`/api/meetings/${encodeURIComponent(meetingId)}/speaker-identity`, json('PUT',{speaker,profile_id})),
};
export function voiceError(cause: unknown): string {
  return cause instanceof ApiError ? cause.message : 'Không thể kết nối dịch vụ giọng nói local. Hãy thử lại.';
}
