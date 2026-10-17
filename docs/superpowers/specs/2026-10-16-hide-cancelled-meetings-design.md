# Hide cancelled meetings in the web client

## Goal

Remove a meeting from the visible local web library immediately after a
successful delete request, while the API retains its cancelled audit metadata.

## Behaviour

The delete control is available for every visible meeting state, including
`processing`. On a successful `DELETE /api/meetings/{id}`, the client removes
that meeting from its in-memory list without waiting for a follow-up request.
When the library initially loads or reloads, it filters `cancelled` meetings
before rendering rows, dashboard lists, and the displayed total.

If deletion fails, the row stays visible and the existing inline error is
shown. The API contract and retained server metadata do not change.

## Tests

Add focused client-level tests if the project test tooling supports them;
otherwise run the existing TypeScript build to validate the status type and
rendering changes. Verify that a processing row exposes delete and that a
cancelled item is not rendered after reload.
