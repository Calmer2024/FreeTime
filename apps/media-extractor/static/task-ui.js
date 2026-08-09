(function exposeDrillTaskUi(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.DrillTaskUi = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createDrillTaskUi() {
  function isTerminalTask(task) {
    return task?.status === "success" || task?.status === "error";
  }

  function createResultPresentation() {
    let automaticResultShown = false;
    let userSelectedTaskId = null;

    return {
      reset() {
        automaticResultShown = false;
        userSelectedTaskId = null;
      },
      selectByUser(taskId) {
        userSelectedTaskId = taskId || null;
      },
      selectedTaskId() {
        return userSelectedTaskId;
      },
      claimAutomaticResult() {
        if (automaticResultShown || userSelectedTaskId) return false;
        automaticResultShown = true;
        return true;
      },
      hasVisibleResult() {
        return automaticResultShown || Boolean(userSelectedTaskId);
      },
    };
  }

  function summarizeTasks(tasks) {
    const records = Array.isArray(tasks) ? tasks : [];
    const active = records.filter(task => task?.status === "pending" || task?.status === "running").length;
    const completed = records.filter(isTerminalTask).length;
    return { active, completed, total: records.length, visible: records.length > 0 };
  }

  function canClearCompletedTasks(tasks) {
    return (Array.isArray(tasks) ? tasks : []).some(isTerminalTask);
  }

  function withoutCompletedTasks(tasks) {
    return (Array.isArray(tasks) ? tasks : []).filter(task => !isTerminalTask(task));
  }

  function taskClearActionKey(summary, confirming = false, error = "") {
    if (!Number(summary?.completed || 0)) return "hidden";
    if (confirming) return "confirm";
    return `ready:${String(error || "")}`;
  }

  function historyScrollTarget(previous, scrollHeight, clientHeight, reset = false) {
    if (reset) return 0;
    const maximum = Math.max(0, Number(scrollHeight || 0) - Number(clientHeight || 0));
    return Math.min(Math.max(0, Number(previous || 0)), maximum);
  }

  function historyItemsSignature(items) {
    return (Array.isArray(items) ? items : []).map(item => [
      item?.cache_key || "",
      item?.created_at || "",
      Boolean(item?.expired),
    ].join(":")).join("|");
  }

  function taskActionState(task, confirmationTaskId) {
    const canDelete = isTerminalTask(task);
    return {
      canView: Boolean(task?.result),
      canDelete,
      confirmingDelete: canDelete && task?.id === confirmationTaskId,
    };
  }

  function taskErrorMessage(task) {
    return String(task?.deleteError || task?.error || "");
  }

  function buildMarkdownExportPayload(result) {
    const title = String(result?.metadata?.title || "完整全文").trim();
    const safeTitle = title
      .replace(/[<>:"/\\|?*\u0000-\u001f]+/g, "_")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, 80) || "完整全文";
    return {
      content: String(result?.cleaned_article || ""),
      filename: `${safeTitle}.md`,
      project_name: title || "未命名项目",
    };
  }

  function taskStateIndicator(status) {
    if (status === "running") {
      return '<span class="task-spinner" aria-hidden="true"></span>';
    }
    const icon = status === "success"
      ? "circle-check"
      : status === "error" ? "circle-x" : "clock-3";
    return `<i data-lucide="${icon}" aria-hidden="true"></i>`;
  }

  function formatBatchDownloadFeedback({
    kind,
    itemCount = 0,
    saved = 0,
    failed = 0,
    directory = "",
    message = "",
  } = {}) {
    if (kind === "loading") {
      return { buttonLabel: `保存中 ${itemCount}`, detail: "" };
    }
    if (message) {
      return { buttonLabel: "下载失败", detail: message };
    }
    const destination = directory ? `到 ${directory}` : "";
    const detail = `已保存 ${saved} 个资源${destination}${failed ? `，${failed} 个失败` : ""}`;
    return {
      buttonLabel: failed ? "部分失败" : "已完成",
      detail,
    };
  }

  function showResultContent({ loading, result }) {
    loading?.classList.remove("active");
    result?.classList.add("active");
  }

  return {
    createResultPresentation,
    isTerminalTask,
    summarizeTasks,
    canClearCompletedTasks,
    withoutCompletedTasks,
    taskClearActionKey,
    historyScrollTarget,
    historyItemsSignature,
    taskActionState,
    taskErrorMessage,
    buildMarkdownExportPayload,
    taskStateIndicator,
    formatBatchDownloadFeedback,
    showResultContent,
  };
}));
