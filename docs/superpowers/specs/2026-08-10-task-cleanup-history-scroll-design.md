# Task Cleanup and History Scroll Design

## Goal

Add one-click cleanup for terminal parallel tasks and keep the recent-extraction list at the user's current scroll position while background polling refreshes data.

## Design

- Add a compact trash action to the parallel-task heading. It is enabled only when at least one `success` or `error` task exists.
- The action uses the existing in-place confirmation pattern: trash changes to confirm/cancel icons in the same location. No modal is introduced.
- Add `DELETE /api/tasks/completed`; it removes only terminal tasks under `TASKS_LOCK`, persists once, and returns the removed IDs. Pending and running tasks are never removed.
- `loadHistory()` captures `history-list.scrollTop` before replacing markup and restores the bounded position afterward. Background polling therefore cannot reset the list. An explicit clear operation requests a reset to zero.

## Error Handling and Tests

- A failed bulk request leaves local records intact and returns the cleanup control to its normal state.
- Backend tests prove terminal-only deletion and active-task preservation.
- Frontend unit tests prove cleanup eligibility and scroll restoration/reset behavior.

