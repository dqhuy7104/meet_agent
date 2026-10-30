import { request } from './api';

export type SystemPrompts = { summary_system_prompt: string; rag_system_prompt: string };
const endpoint = '/api/admin/system-prompts';
export const promptApi = {
  get: (signal?: AbortSignal) => request<SystemPrompts>(endpoint, {signal}),
  save: (prompts: SystemPrompts, signal?: AbortSignal) => request<SystemPrompts>(endpoint, {
    method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(prompts), signal,
  }),
};
