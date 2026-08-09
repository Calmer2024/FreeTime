# DrillKnowledge Inline Task Actions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply the FreeTime inline CRUD rule to DrillKnowledge parallel tasks, use white task rows, and fix error-row layout without changing other applications or dropdowns.

**Architecture:** Extend the existing testable `task-ui.js` module with task-row view-model helpers, then render stable action slots and distinct error-message markup from `app.js`. CSS will separate task status selectors from error content selectors and keep action-slot dimensions fixed across default and confirmation states.

**Tech Stack:** Vanilla JavaScript, HTML/CSS, Node built-in test runner.

## Global Constraints

- Apply the CRUD rule only to the DrillKnowledge parallel task component.
- Do not modify existing dropdown menus, portal UI, Chaoxing UI, or unrelated modals.
- Do not build, publish, version-bump, or update the installed FreeTime application.
- Preserve all pre-existing uncommitted workspace changes and leave implementation changes uncommitted for user acceptance.
- Task rows use `#ffffff`; error messages use `.task-error-message`, not `.task-error`.

---

### Task 1: Testable task-row action state

**Files:**
- Modify: `apps/media-extractor/static/task-ui.test.js`
- Modify: `apps/media-extractor/static/task-ui.js`

**Interfaces:**
- Produces: `taskActionState(task, confirmationTaskId)` returning `canView`, `canDelete`, and `confirmingDelete`.
- Produces: `taskErrorMessage(task)` selecting request errors before extraction errors.

- [ ] **Step 1: Write failing tests**

```javascript
test("terminal task deletion switches only the matching action slot", () => {
  const task = { id: "done", status: "success", result: { id: 1 } };
  assert.deepEqual(taskActionState(task, null), {
    canView: true, canDelete: true, confirmingDelete: false,
  });
  assert.deepEqual(taskActionState(task, "done"), {
    canView: true, canDelete: true, confirmingDelete: true,
  });
});

test("running tasks cannot enter delete confirmation", () => {
  assert.deepEqual(taskActionState({ id: "active", status: "running" }, "active"), {
    canView: false, canDelete: false, confirmingDelete: false,
  });
});

test("delete request error takes precedence over extraction error", () => {
  assert.equal(taskErrorMessage({ error: "提取失败", deleteError: "删除失败" }), "删除失败");
  assert.equal(taskErrorMessage({ error: "提取失败" }), "提取失败");
});
```

- [ ] **Step 2: Run test and verify RED**

Run:

```powershell
node --test apps/media-extractor/static/task-ui.test.js
```

Expected: new tests fail because both helpers are missing.

- [ ] **Step 3: Implement minimal helpers**

Use `isTerminalTask(task)` for deletion eligibility, require `task.result` for viewing, and allow confirmation only when the task is terminal and its id matches `confirmationTaskId`.

- [ ] **Step 4: Run test and verify GREEN**

Run the same Node command; expect all tests to pass.

### Task 2: Stable inline CRUD action slot

**Files:**
- Modify: `apps/media-extractor/static/app.js`
- Modify: `apps/media-extractor/static/app.css`

**Interfaces:**
- Consumes: `DrillTaskUi.taskActionState`, `DrillTaskUi.taskErrorMessage`.
- Produces: `.task-actions`, `.task-action-confirm`, `.task-action-cancel`, and `.task-error-message` markup.

- [ ] **Step 1: Replace action markup**

- Keep the view action in its stable position.
- In default state render the delete icon.
- In confirmation state render check and X icon buttons directly in the same action slot.
- Remove the `.inline-confirmation` wrapper, message, and text buttons from task rows.
- Add accurate `aria-label` and `title` attributes for confirm and cancel.

- [ ] **Step 2: Separate error markup**

- Render at most one `.task-error-message` using `taskErrorMessage(task)`.
- Add the full escaped error as `title`.
- Keep the outer status class as `.task-card.task-error`.

- [ ] **Step 3: Stabilize CSS**

- Change `.task-card` background to `#ffffff`.
- Use a four-column first row: status, name, metrics, fixed-width actions.
- Give `.task-actions` a fixed width that fits view plus two icon buttons.
- Style confirm/cancel as ordinary icon buttons without a wrapper background or bubble.
- Use `.task-card.task-error .task-state` for status color.
- Use `.task-error-message` on row two with two-line clamping and no effect on the outer card grid placement.

- [ ] **Step 4: Run syntax and unit checks**

```powershell
node --test apps/media-extractor/static/task-ui.test.js
node --check apps/media-extractor/static/task-ui.js
node --check apps/media-extractor/static/app.js
```

Expected: all commands exit 0.

### Task 3: Browser regression and final verification

**Files:**
- No production file changes expected.

**Interfaces:**
- Verifies the rendered task component at the isolated localhost preview.

- [ ] **Step 1: Reload the isolated preview**

Use the existing localhost preview with synthetic tasks and reload after the code change.

- [ ] **Step 2: Verify visual behavior**

- Every `.task-card` has computed white background.
- Error cards retain full panel width.
- `.task-error-message` is limited to two lines and does not move the action slot.
- Clicking a delete icon replaces it with confirm and cancel icons in the same fixed-width slot.
- No `.inline-confirmation` element appears.
- Cancel restores the delete icon; confirm still performs persistent deletion.

- [ ] **Step 3: Run final checks**

```powershell
node --test apps/media-extractor/static/task-ui.test.js
node --check apps/media-extractor/static/task-ui.js
node --check apps/media-extractor/static/app.js
git diff --check
```

Expected: all commands exit 0. Confirm no installer or release artifact is created.
