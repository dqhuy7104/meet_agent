export type MeetingStatus = "completed" | "processing" | "queued" | "failed";
export type ApiMeeting = { id: string; title: string | null; original_filename: string; created_at: string; updated_at: string; status: MeetingStatus; attempt_count: number; duration_seconds: number | null };
