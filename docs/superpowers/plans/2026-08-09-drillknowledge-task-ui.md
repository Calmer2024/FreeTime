# DrillKnowledge Task UI Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the large download feedback and in-content task queue with compact top-bar UI, persistently delete completed task records, and prevent parallel task completion from conflicting with a user-selected result.

**Architecture:** Add a small framework-free JavaScript state controller that owns result-presentation decisions and can be tested directly with Node. Keep `app.js` responsible for rendering and API calls, move the queue markup into a fixed top-bar popover, and add a guarded FastAPI delete endpoint for persisted terminal tasks.

**Tech Stack:** Vanilla JavaScript, HTML/CSS, Node built-in test runner, Python 3.10+, FastAPI, pytest.

## Global Constraints

- Do not change extraction concurrency, result payloads, pipeline behavior, or downloaded file contents.
- Do not build an installer, change application versions, publish, or update the installed FreeTime application before user acceptance.
- Preserve all pre-existing uncommitted workspace changes. Because the target files already contain user changes, do not create implementation commits that would accidentally include them.
- Success and error are terminal task states; pending and running tasks cannot be deleted.
- The task popover maximum height is 360px and must also fit within the current viewport.

---

## File Structure

- `apps/media-extractor/static/task-ui.js`: testable result-presentation state controller and terminal-task predicate.
- `apps/media-extractor/static/task-ui.test.js`: Node tests for presentation locking, reset behavior, and terminal state rules.
- `apps/media-extractor/static/app.js`: integrate controller, top-bar popover actions, persistent delete flow, and compact download messages.
- `apps/media-extractor/static/index.html`: top-bar trigger/popover markup and script loading.
- `apps/media-extractor/static/app.css`: popover, scroll container, compact download feedback, responsive layout, and delete controls.
- `apps/media-extractor/tests/test_task_persistence.py`: API regression tests for terminal task deletion.
- `apps/media-extractor/app/main.py`: guarded `DELETE /api/tasks/{task_id}` endpoint.

### Task 1: Persisted terminal-task deletion

**Files:**
- Modify: `apps/media-extractor/tests/test_task_persistence.py`
- Modify: `apps/media-extractor/app/main.py`

**Interfaces:**
- Consumes: `TASK_JOBS`, `TASKS_LOCK`, `_write_task_jobs()`.
- Produces: `DELETE /api/tasks/{task_id}` returning `{"status": "deleted", "id": task_id}`.

- [ ] **Step 1: Write failing API tests**

Add tests that create isolated task storage and assert:

```python
def test_completed_task_can_be_deleted_and_stays_deleted(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "TASKS_FILE", tmp_path / "tasks.json")
    main_module.TASK_JOBS.clear()
    main_module._update_task_job("done", status="success", progress=100)

    response = TestClient(main_module.app).delete("/api/tasks/done")

    assert response.status_code == 200
    assert response.json() == {"status": "deleted", "id": "done"}
    assert "done" not in main_module.TASK_JOBS
    assert "done" not in (tmp_path / "tasks.json").read_text(encoding="utf-8")


def test_running_task_cannot_be_deleted(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "TASKS_FILE", tmp_path / "tasks.json")
    main_module.TASK_JOBS.clear()
    main_module._update_task_job("running", status="running", progress=30)

    response = TestClient(main_module.app).delete("/api/tasks/running")

    assert response.status_code == 409
    assert "running" in main_module.TASK_JOBS


def test_missing_task_delete_returns_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "TASKS_FILE", tmp_path / "tasks.json")
    main_module.TASK_JOBS.clear()

    response = TestClient(main_module.app).delete("/api/tasks/missing")

    assert response.status_code == 404
```

- [ ] **Step 2: Run tests and verify RED**

Run from `apps/media-extractor`:

```powershell
python -m pytest tests/test_task_persistence.py -k "delete" -q
```

Expected: all new tests fail because the route returns 405 Method Not Allowed.

- [ ] **Step 3: Implement the guarded delete endpoint**

Add after `get_task_job`:

```python
@app.delete("/api/tasks/{task_id}", include_in_schema=False)
async def delete_task_job(task_id: str) -> dict[str, str]:
    with TASKS_LOCK:
        item = TASK_JOBS.get(task_id)
        if not item:
            raise HTTPException(status_code=404, detail="任务不存在")
        if item.get("status") not in {"success", "error"}:
            raise HTTPException(status_code=409, detail="运行中的任务不能删除")
        del TASK_JOBS[task_id]
        _write_task_jobs()
    return {"status": "deleted", "id": task_id}
```

- [ ] **Step 4: Run focused tests and verify GREEN**

```powershell
python -m pytest tests/test_task_persistence.py -k "delete" -q
```

Expected: 3 passed.

### Task 2: Result-presentation controller

**Files:**
- Create: `apps/media-extractor/static/task-ui.js`
- Create: `apps/media-extractor/static/task-ui.test.js`
- Modify: `apps/media-extractor/static/index.html`
- Modify: `apps/media-extractor/static/app.js`

**Interfaces:**
- Produces global/module export `DrillTaskUi.createResultPresentation()` with `reset()`, `selectByUser()`, `claimAutomaticResult()`, and `hasVisibleResult()`.
- Produces `DrillTaskUi.isTerminalTask(task)`.

- [ ] **Step 1: Write failing controller tests**

Create Node tests covering the real public API:

```javascript
const test = require("node:test");
const assert = require("node:assert/strict");
const { createResultPresentation, isTerminalTask } = require("./task-ui.js");

test("only the first completion claims automatic result presentation", () => {
  const state = createResultPresentation();
  assert.equal(state.claimAutomaticResult(), true);
  assert.equal(state.claimAutomaticResult(), false);
});

test("user selection prevents later automatic result replacement", () => {
  const state = createResultPresentation();
  state.selectByUser("task-b");
  assert.equal(state.claimAutomaticResult(), false);
  assert.equal(state.selectedTaskId(), "task-b");
});

test("new submission resets presentation locking", () => {
  const state = createResultPresentation();
  state.selectByUser("task-b");
  state.reset();
  assert.equal(state.claimAutomaticResult(), true);
  assert.equal(state.selectedTaskId(), null);
});

test("only success and error tasks are terminal", () => {
  assert.equal(isTerminalTask({ status: "success" }), true);
  assert.equal(isTerminalTask({ status: "error" }), true);
  assert.equal(isTerminalTask({ status: "running" }), false);
  assert.equal(isTerminalTask({ status: "pending" }), false);
});
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
node --test apps/media-extractor/static/task-ui.test.js
```

Expected: fail because `task-ui.js` does not exist.

- [ ] **Step 3: Implement the minimal UMD-compatible controller**

Implement a factory that tracks `automaticResultShown` and `userSelectedTaskId`, exports through `module.exports` in Node, and assigns `globalThis.DrillTaskUi` in the browser. `claimAutomaticResult()` returns false after either an automatic claim or a user selection.

- [ ] **Step 4: Integrate presentation state**

- Load `/static/task-ui.js` before `/static/app.js`.
- Create one controller in `app.js`.
- On a new form submission call `reset()` and activate loading.
- In `runTask`, render a successful result only when `claimAutomaticResult()` returns true.
- On task “查看”, call `selectByUser(task.id)` then render its result.
- At the beginning of `render(data)`, remove `loading.active`, stop its timer when appropriate, and keep the result as the only active workbench child.
- Never reactivate the main loading component merely because another task remains pending or running.

- [ ] **Step 5: Run controller tests and JavaScript syntax checks**

```powershell
node --test apps/media-extractor/static/task-ui.test.js
node --check apps/media-extractor/static/task-ui.js
node --check apps/media-extractor/static/app.js
```

Expected: all commands exit 0.

### Task 3: Top-bar task popover and terminal record deletion

**Files:**
- Modify: `apps/media-extractor/static/index.html`
- Modify: `apps/media-extractor/static/app.css`
- Modify: `apps/media-extractor/static/app.js`

**Interfaces:**
- Consumes: `DrillTaskUi.isTerminalTask`, `DELETE /api/tasks/{task_id}`.
- Produces: `#task-queue-trigger`, `#task-queue`, `#task-list`, per-row `[data-task-view-id]` and `[data-task-delete-id]` actions.

- [ ] **Step 1: Extend behavior tests for task summaries**

Add pure helpers to `task-ui.js` and failing assertions that `summarizeTasks(tasks)` returns active count, completed count, total count, and visibility. This catches badge/count regressions without testing source text.

- [ ] **Step 2: Run the Node test and verify RED**

```powershell
node --test apps/media-extractor/static/task-ui.test.js
```

Expected: fail because `summarizeTasks` is missing.

- [ ] **Step 3: Implement summary behavior and top-bar markup**

- Add `summarizeTasks(tasks)` to the module.
- Move the task queue out of `.dk-content-inner` and into `.dk-header-inner`.
- Add a circular trigger with `aria-expanded="false"` and `aria-controls="task-queue"`.
- Keep the task panel hidden when no records exist; otherwise toggle it from the trigger without affecting content layout.

- [ ] **Step 4: Implement bounded popover styling**

- Position the panel below the top-right trigger with a z-index above content.
- Use `width: min(440px, calc(100vw - 32px))`.
- Use `max-height: min(360px, calc(100vh - 88px))` on the panel and `overflow-y: auto` on the list.
- Preserve compact status, metrics, view buttons, responsive row wrapping, and subtle scrollbars.
- Show a small active-count badge on the circular trigger.

- [ ] **Step 5: Implement persistent delete interaction**

- Render delete only for terminal tasks.
- On first click replace actions with compact inline confirmation.
- On confirmation call `DELETE /api/tasks/{id}`.
- Remove the matching task from `taskRecords`, persist local storage, and rerender only after a successful response.
- On failure keep the row and render a compact row-level error.
- If the last record is deleted, close and hide the panel and trigger.

- [ ] **Step 6: Run Node tests and syntax checks**

```powershell
node --test apps/media-extractor/static/task-ui.test.js
node --check apps/media-extractor/static/app.js
```

Expected: all tests and checks pass.

### Task 4: Compact download feedback and full verification

**Files:**
- Modify: `apps/media-extractor/static/index.html`
- Modify: `apps/media-extractor/static/app.css`
- Modify: `apps/media-extractor/static/app.js`

**Interfaces:**
- Consumes: existing `setDownloadButtonState`, `/api/download/batch`, and download result payloads.
- Produces: compact `#download-status` live text adjacent to resource actions.

- [ ] **Step 1: Write a failing formatter test**

Add and test `formatBatchDownloadFeedback({ kind, itemCount, saved, failed, directory, message })` in `task-ui.js` with literal expected strings for loading, success, partial failure, and error. The loading case must produce `保存中 N` for the button label and a blank detail string.

- [ ] **Step 2: Run the Node test and verify RED**

```powershell
node --test apps/media-extractor/static/task-ui.test.js
```

Expected: fail because the formatter is missing.

- [ ] **Step 3: Implement compact feedback**

- Move `#download-status` into `.resource-actions`, set `aria-live="polite"`, and render it as one-line muted text rather than a bordered block.
- During batch download, change the button label to `保存中 N`; do not show separate loading detail.
- On success or failure, restore the button after the existing delay and show one-line detail containing saved count, failed count when nonzero, and directory or error.
- Keep individual resource/thumbnail download behavior compatible with the same compact status region.

- [ ] **Step 4: Run focused and full automated verification**

```powershell
node --test apps/media-extractor/static/task-ui.test.js
node --check apps/media-extractor/static/task-ui.js
node --check apps/media-extractor/static/app.js
python -m pytest apps/media-extractor/tests/test_task_persistence.py -q
python -m pytest apps/media-extractor/tests -q
npm test --prefix desktop
```

Expected: all commands exit 0 with no test failures.

- [ ] **Step 5: Inspect the final diff and forbidden outputs**

```powershell
git diff --check
git status --short
```

Confirm no installer, release artifact, version change, or installed-application update was produced.

- [ ] **Step 6: Start the local development preview**

Start the FastAPI development server on an available localhost port using the workspace Python environment. Open the DrillKnowledge page in the in-app browser or provide the exact localhost URL. Do not invoke Electron packaging or installer commands.

- [ ] **Step 7: Manual acceptance scenarios**

Verify in the local preview:

1. Submit two tasks with different completion times.
2. Open the top-right task popover and confirm its list scrolls within the height limit.
3. Click “查看” on the first completed task while another remains active; confirm no loading/result collision and no later overwrite.
4. Delete a completed task, refresh, and confirm it does not return.
5. Trigger one-click download and confirm only compact button/live-text feedback appears.
