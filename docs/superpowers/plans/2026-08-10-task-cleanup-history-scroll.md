# Task Cleanup and History Scroll Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add terminal-task bulk cleanup and prevent recent-history polling from resetting scroll position.

**Architecture:** Keep policy helpers in `task-ui.js`, persistence mutations in FastAPI, and DOM orchestration in `app.js`. Bulk cleanup is atomic on the backend; history scroll is preserved across the existing markup refresh.

**Tech Stack:** FastAPI, vanilla JavaScript, Node test runner, pytest.

## Global Constraints

- Do not remove `pending` or `running` tasks.
- Use inline confirmation; do not add a modal.
- Do not reset history scroll during polling.

---

### Task 1: Terminal task bulk cleanup

**Files:**
- Modify: `apps/media-extractor/tests/test_task_persistence.py`
- Modify: `apps/media-extractor/app/main.py`
- Modify: `apps/media-extractor/static/task-ui.test.js`
- Modify: `apps/media-extractor/static/task-ui.js`
- Modify: `apps/media-extractor/static/index.html`
- Modify: `apps/media-extractor/static/app.js`
- Modify: `apps/media-extractor/static/app.css`

**Interfaces:**
- Produces: `DELETE /api/tasks/completed -> {status, deleted, ids}`
- Produces: `DrillTaskUi.canClearCompletedTasks(tasks) -> boolean`

- [ ] **Step 1: Write failing backend and frontend tests**

```python
response = TestClient(main_module.app).delete("/api/tasks/completed")
assert response.json()["ids"] == ["done", "failed"]
assert "running" in main_module.TASK_JOBS
```

```javascript
assert.equal(canClearCompletedTasks([{status: "running"}]), false);
assert.equal(canClearCompletedTasks([{status: "success"}]), true);
```

- [ ] **Step 2: Run tests and confirm missing endpoint/helper failures**

Run: `python -m pytest tests/test_task_persistence.py -k clear_completed`
Run: `node --test static/task-ui.test.js`

- [ ] **Step 3: Implement endpoint, helper, heading control, inline confirmation, and styles**

The endpoint filters `TASK_JOBS` by `status in {"success", "error"}`, deletes those IDs while holding `TASKS_LOCK`, calls `_write_task_jobs()` once, and returns the IDs. The UI removes returned IDs from `taskRecords` and persists local state.

- [ ] **Step 4: Run both focused suites and confirm they pass**

### Task 2: Preserve recent-history scroll

**Files:**
- Modify: `apps/media-extractor/static/task-ui.test.js`
- Modify: `apps/media-extractor/static/task-ui.js`
- Modify: `apps/media-extractor/static/app.js`

**Interfaces:**
- Produces: `DrillTaskUi.historyScrollTarget(previous, scrollHeight, clientHeight, reset) -> number`

- [ ] **Step 1: Write failing scroll-policy tests**

```javascript
assert.equal(historyScrollTarget(240, 1000, 300, false), 240);
assert.equal(historyScrollTarget(900, 1000, 300, false), 700);
assert.equal(historyScrollTarget(240, 1000, 300, true), 0);
```

- [ ] **Step 2: Run Node tests and confirm the helper is missing**

- [ ] **Step 3: Implement the helper and use it in `loadHistory({resetScroll=false})`**

Capture `scrollTop` before replacing HTML. Restore it after rendering; use `loadHistory({resetScroll:true})` only after explicit cache clearing.

- [ ] **Step 4: Run Node tests, syntax check, focused backend tests, and browser verification**

