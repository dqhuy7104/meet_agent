// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { SystemPromptSettings } from './SystemPromptSettings';
import { setCsrfToken } from '../lib/api';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); setCsrfToken(null); });

it('loads, saves both prompts with CSRF, and restores defaults', async () => {
  let saved = {summary_system_prompt: 'Old summary', rag_system_prompt: 'Old RAG'};
  const fetcher = vi.fn(async (_url: string, options?: RequestInit) => {
    if (options?.method === 'PUT') saved = JSON.parse(String(options.body));
    return {ok: true, status: 200, json: async () => saved};
  });
  vi.stubGlobal('fetch', fetcher);
  setCsrfToken('csrf');
  render(<SystemPromptSettings />);
  const summary = await screen.findByLabelText('System prompt tóm tắt');
  const rag = screen.getByLabelText('System prompt RAG');
  await waitFor(() => expect((summary as HTMLTextAreaElement).value).toBe('Old summary'));
  fireEvent.change(summary, {target: {value: 'New summary'}});
  fireEvent.change(rag, {target: {value: 'New RAG'}});
  fireEvent.click(screen.getByRole('button', {name: 'Lưu system prompt'}));
  await screen.findByText('Đã lưu system prompt.');
  expect(saved).toEqual({summary_system_prompt: 'New summary', rag_system_prompt: 'New RAG'});
  const call = fetcher.mock.calls.find(([, options]) => options?.method === 'PUT');
  expect(new Headers(call?.[1]?.headers).get('X-CSRF-Token')).toBe('csrf');
  fireEvent.click(screen.getByRole('button', {name: 'Khôi phục mặc định'}));
  expect((summary as HTMLTextAreaElement).value).toBe('');
  fireEvent.click(screen.getByRole('button', {name: 'Lưu system prompt'}));
  await waitFor(() => expect(saved).toEqual({summary_system_prompt: '', rag_system_prompt: ''}));
});

it('shows load errors and allows retry without saving an unloaded form', async () => {
  let failed = true;
  vi.stubGlobal('fetch', vi.fn(async () => ({ok: !failed, status: failed ? 503 : 200,
    json: async () => failed ? {detail: 'database_unavailable'} : {summary_system_prompt: '', rag_system_prompt: ''}})));
  render(<SystemPromptSettings />);
  await screen.findByRole('alert');
  expect(screen.queryByRole('button', {name: 'Lưu system prompt'})).toBeNull();
  failed = false;
  fireEvent.click(screen.getByRole('button', {name: 'Tải lại system prompt'}));
  await screen.findByRole('button', {name: 'Lưu system prompt'});
});

it('keeps edits when save fails and announces the failure', async () => {
  vi.stubGlobal('fetch', vi.fn(async (_url: string, options?: RequestInit) => ({
    ok: options?.method !== 'PUT', status: options?.method === 'PUT' ? 503 : 200,
    json: async () => options?.method === 'PUT' ? {detail: 'database_unavailable'} : {summary_system_prompt: '', rag_system_prompt: ''},
  })));
  render(<SystemPromptSettings />);
  const summary = await screen.findByLabelText('System prompt tóm tắt');
  fireEvent.change(summary, {target: {value: 'Keep edits'}});
  fireEvent.click(screen.getByRole('button', {name: 'Lưu system prompt'}));
  await screen.findByRole('alert');
  expect((summary as HTMLTextAreaElement).value).toBe('Keep edits');
});
