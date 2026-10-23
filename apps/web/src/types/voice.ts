export type VoiceIdentificationStatus = 'not_run' | 'completed' | 'failed' | 'disabled';
export type SpeakerIdentity = { speaker: string; profile_id: string | null; display_name: string | null; similarity: number | null; origin: 'automatic' | 'manual' };
export type VoiceProfile = { id: string; display_name: string; sample_count: number };
export type VoiceSample = { id: number; source: 'upload' | 'meeting'; source_meeting_id: string | null; source_speaker: string | null; fingerprint: string; created_at: string; fifo_position: number };
export type EnrollmentJob = { id: string; status: 'queued' | 'processing' | 'completed' | 'failed'; failure_code: string | null };
