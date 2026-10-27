import { request as sessionRequest } from "./api";

export type BotStatus =
  | "queued" | "joining" | "waiting_admission" | "recording"
  | "finalizing" | "completed" | "interrupted" | "cancelled";

export type BotSession = {
  id: string;
  project_id?: string;
  status: BotStatus;
  failure_code: string | null;
  created_at: string;
  started_at: string | null;
  ended_at: string | null;
  finalized_part_count: number;
  capture_incomplete: boolean;
  meeting_id: string | null;
  stop_requested: boolean;
};

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  return sessionRequest<T>(`/api/bot-sessions${path}`, options);
}

export const botApi = {
  create: (url: string, title: string, projectId?: string) => request<BotSession>("", {
    method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({ url, title: title || null, project_id: projectId }),
  }),
  active: (signal?: AbortSignal) => request<BotSession | undefined>("/active", { signal }),
  recent: (signal?: AbortSignal) => request<BotSession | undefined>("/recent", { signal }),
  get: (id: string, signal?: AbortSignal) => request<BotSession>(`/${encodeURIComponent(id)}`, { signal }),
  stop: (id: string) => request<BotSession>(`/${encodeURIComponent(id)}/stop`, { method: "POST" }),
  processParts: (id: string) => request<BotSession>(`/${encodeURIComponent(id)}/process-parts`, { method: "POST" }),
  discard: (id: string) => request<void>(`/${encodeURIComponent(id)}`, { method: "DELETE" }),
};
