const test = require("node:test");
const assert = require("node:assert/strict");
const {
  createResultPresentation,
  isTerminalTask,
  summarizeTasks,
  formatBatchDownloadFeedback,
  showResultContent,
  taskActionState,
  taskErrorMessage,
  buildMarkdownExportPayload,
  taskStateIndicator,
  canClearCompletedTasks,
  historyScrollTarget,
  withoutCompletedTasks,
  taskClearActionKey,
  historyItemsSignature,
} = require("./task-ui.js");

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

test("showing a task result removes the extraction transition", () => {
  const loadingClasses = new Set(["active"]);
  const resultClasses = new Set();
  const loading = {
    classList: {
      remove: value => loadingClasses.delete(value),
    },
  };
  const result = {
    classList: {
      add: value => resultClasses.add(value),
    },
  };

  showResultContent({ loading, result });

  assert.equal(loadingClasses.has("active"), false);
  assert.equal(resultClasses.has("active"), true);
});

test("only success and error tasks are terminal", () => {
  assert.equal(isTerminalTask({ status: "success" }), true);
  assert.equal(isTerminalTask({ status: "error" }), true);
  assert.equal(isTerminalTask({ status: "running" }), false);
  assert.equal(isTerminalTask({ status: "pending" }), false);
});

test("terminal task deletion switches only the matching action slot", () => {
  const task = { id: "done", status: "success", result: { id: 1 } };
  assert.deepEqual(taskActionState(task, null), {
    canView: true,
    canDelete: true,
    confirmingDelete: false,
  });
  assert.deepEqual(taskActionState(task, "done"), {
    canView: true,
    canDelete: true,
    confirmingDelete: true,
  });
});

test("running tasks cannot enter delete confirmation", () => {
  assert.deepEqual(taskActionState({ id: "active", status: "running" }, "active"), {
    canView: false,
    canDelete: false,
    confirmingDelete: false,
  });
});

test("delete request error takes precedence over extraction error", () => {
  assert.equal(taskErrorMessage({ error: "提取失败", deleteError: "删除失败" }), "删除失败");
  assert.equal(taskErrorMessage({ error: "提取失败" }), "提取失败");
  assert.equal(taskErrorMessage({}), "");
});

test("task summary separates active and completed records", () => {
  assert.deepEqual(summarizeTasks([
    { status: "pending" },
    { status: "running" },
    { status: "success" },
    { status: "error" },
  ]), {
    active: 2,
    completed: 2,
    total: 4,
    visible: true,
  });
  assert.deepEqual(summarizeTasks([]), {
    active: 0,
    completed: 0,
    total: 0,
    visible: false,
  });
});

test("batch download feedback keeps loading compact", () => {
  assert.deepEqual(formatBatchDownloadFeedback({ kind: "loading", itemCount: 2 }), {
    buttonLabel: "保存中 2",
    detail: "",
  });
});

test("batch download feedback reports success and partial failure", () => {
  assert.deepEqual(formatBatchDownloadFeedback({
    kind: "success", saved: 2, failed: 0, directory: "D:\\Downloads",
  }), {
    buttonLabel: "已完成",
    detail: "已保存 2 个资源到 D:\\Downloads",
  });
  assert.deepEqual(formatBatchDownloadFeedback({
    kind: "error", saved: 1, failed: 1, directory: "D:\\Downloads",
  }), {
    buttonLabel: "部分失败",
    detail: "已保存 1 个资源到 D:\\Downloads，1 个失败",
  });
});

test("batch download feedback preserves request errors", () => {
  assert.deepEqual(formatBatchDownloadFeedback({
    kind: "error", message: "目录不可写",
  }), {
    buttonLabel: "下载失败",
    detail: "目录不可写",
  });
});

test("markdown export uses the polished article and md filename", () => {
  assert.deepEqual(buildMarkdownExportPayload({
    metadata: { title: "Harness：Agent / 实践" },
    cleaned_article: "# Harness\n\n## 核心\n正文",
  }), {
    content: "# Harness\n\n## 核心\n正文",
    filename: "Harness：Agent _ 实践.md",
    project_name: "Harness：Agent / 实践",
  });
});

test("running task uses a stable css spinner instead of a replaced svg", () => {
  assert.equal(
    taskStateIndicator("running"),
    '<span class="task-spinner" aria-hidden="true"></span>'
  );
  assert.match(taskStateIndicator("success"), /data-lucide="circle-check"/);
});

test("bulk cleanup is available only when terminal tasks exist", () => {
  assert.equal(canClearCompletedTasks([{ status: "running" }, { status: "pending" }]), false);
  assert.equal(canClearCompletedTasks([{ status: "running" }, { status: "success" }]), true);
  assert.equal(canClearCompletedTasks([{ status: "error" }]), true);
});

test("history refresh preserves and bounds scroll unless reset is explicit", () => {
  assert.equal(historyScrollTarget(240, 1000, 300, false), 240);
  assert.equal(historyScrollTarget(900, 1000, 300, false), 700);
  assert.equal(historyScrollTarget(240, 1000, 300, true), 0);
});

test("bulk cleanup also removes terminal tasks that only exist locally", () => {
  assert.deepEqual(withoutCompletedTasks([
    { id: "running", status: "running" },
    { id: "done", status: "success" },
    { id: "failed", status: "error" },
    { id: "pending", status: "pending" },
  ]).map(task => task.id), ["running", "pending"]);
});

test("task cleanup control key changes only with interaction state", () => {
  assert.equal(taskClearActionKey({ completed: 2 }, false, ""), "ready:");
  assert.equal(taskClearActionKey({ completed: 9 }, false, ""), "ready:");
  assert.equal(taskClearActionKey({ completed: 2 }, true, ""), "confirm");
  assert.equal(taskClearActionKey({ completed: 0 }, false, ""), "hidden");
});

test("history signature changes only when rendered history changes", () => {
  const items = [{ cache_key: "a", created_at: 10, expired: false }];
  assert.equal(historyItemsSignature(items), "a:10:false");
  assert.equal(historyItemsSignature([...items]), "a:10:false");
  assert.equal(
    historyItemsSignature([{ cache_key: "a", created_at: 11, expired: false }]),
    "a:11:false",
  );
});
