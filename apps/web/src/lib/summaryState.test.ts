import { expect, it } from 'vitest';
import { ApiError } from './api';
import { shouldRetrySummary } from './summaryState';

it('retries only a concurrent in-progress summary within the wait budget', () => {
  const generating = new ApiError(409, 'summary_generating');
  expect(shouldRetrySummary(generating, 0)).toBe(true);
  expect(shouldRetrySummary(generating, 99)).toBe(false);
  expect(shouldRetrySummary(new ApiError(503, 'provider_unavailable'), 0)).toBe(false);
  expect(shouldRetrySummary(new ApiError(409, 'meeting_changed'), 0)).toBe(false);
});
