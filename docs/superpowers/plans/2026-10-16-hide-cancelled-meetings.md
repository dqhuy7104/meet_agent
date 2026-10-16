# Hide Cancelled Meetings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove cancelled meetings from the visible React library immediately after deletion and on reload.

**Architecture:** Keep cancellation metadata server-side. The existing library hook owns the visible list, filters cancelled API records, and exposes an ID-based local removal callback; rows call it after a successful DELETE.

**Tech Stack:** React 19, TypeScript, Vite.

**Spec:** `docs/superpowers/specs/2026-10-16-hide-cancelled-meetings-design.md`

## Global Constraints

- Do not change the API cancellation/audit contract.
- Allow deletion from every visible meeting status.
- Keep failed deletion errors visible and do not remove that row.
- Validate with the existing `npm run build`; no web test runner is configured.

## Review Focus

- A processing meeting renders a delete action.
- Successful deletion removes only the matching row and adjusts the displayed total.
- Reloaded `cancelled` records are not rendered.
- A failed DELETE keeps the row displayed with its existing error.

---

### Task 1: Keep cancelled records out of the visible library

**Files:**
- Modify: `apps/web/src/App.tsx`

**Interfaces:**
- `useMeetingLibrary()` returns `removeFromLibrary(id: string): void` alongside the existing list/reload fields.
- `MeetingRow` calls `onRemoved(meeting.id)` after a successful API delete.

- [ ] Update the hook to filter `cancelled` records on load and derive the visible total from that filtered list.
- [ ] Update `MeetingRow` to show DELETE for processing meetings and pass its ID to the callback after success.
- [ ] Remove the matching item locally after success; preserve the row and inline error on failure.
- [ ] Run `cd apps/web && npm run build`; expect PASS.
- [ ] Commit with `fix(web): hide cancelled meetings after deletion`.

## Self-review

The single task owns rendering, local update, error preservation, and TypeScript verification; no API or backend behavior changes.
