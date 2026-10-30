import { ApiError } from './api';

export function shouldRetrySummary(error: unknown, attempt: number): boolean {
  return error instanceof ApiError && error.status === 409 &&
    error.message === 'summary_generating' && attempt < 99;
}
