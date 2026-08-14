const $ = (id) => document.getElementById(id);
const strategyLabels = { subtitle: "完整字幕", asr: "分段 ASR", visual: "全模态", hybrid: "音画联合", metadata: "元数据" };
const videoTypeLabels = { speech_dominant: "口播主导", text_dominant: "文字主导", event_footage: "现场事件", mixed: "多模态", low_information: "低信息", unknown: "待判断" };
const coverageStatusLabels = { structured_ready: "已完成", partial: "部分覆盖", needs_review: "需要复核", no_structured_information: "待完善", unavailable: "不可用", metadata_only: "仅元数据", complete: "完整" };
let clock;
let refreshNext = false;
let historyByKey = {};
let historyItems = [];
let currentResult = null;
let currentCacheKey = null;
let currentArticleView = "polished";
let taskSequence = 0;
let taskRecords = [];
const TASK_STORAGE_KEY = "drillknowledge.tasks.v2";
let taskPollTimer = null;
let taskStartNoticeTimer = null;
let taskDeleteConfirmationId = null;
let taskClearConfirmation = false;
let taskClearError = "";
const taskPresentation = DrillTaskUi.createResultPresentation();

refreshIcons();
loadHistory();
restoreTasks();
setInterval(() => {
  if (taskRecords.some(task => task.status === "running" || task.status === "pending")) {
    updateTaskMetrics();
  }
}, 500);
$("toggle-task-queue")?.addEventListener("click", () => {
  const panel = $("task-queue");
  const expanded = panel.hidden;
  panel.hidden = !expanded;
  $("toggle-task-queue").setAttribute("aria-expanded", String(expanded));
  $("toggle-task-queue").setAttribute("aria-label", expanded ? "折叠并行任务" : "展开并行任务");
  $("toggle-task-queue").title = expanded ? "折叠并行任务" : "展开并行任务";
});
$("task-queue-control")?.addEventListener("click", event => event.stopPropagation());
document.addEventListener("click", () => setTaskQueueExpanded(false));
document.addEventListener("keydown", event => {
  if (event.key === "Escape") setTaskQueueExpanded(false);
});
$("toggle-history")?.addEventListener("click", () => {
  const section = document.querySelector(".history");
  const collapsed = section.classList.toggle("history-collapsed");
  $("toggle-history").setAttribute("aria-label", collapsed ? "展开最近提取" : "折叠最近提取");
  $("toggle-history").title = collapsed ? "展开最近提取" : "折叠最近提取";
});
$("history-search")?.addEventListener("input", renderHistoryItems);
$("history-source-filter")?.addEventListener("change", renderHistoryItems);
$("history-type-filter")?.addEventListener("change", renderHistoryItems);

// 一键复制功能
function copyToClipboard(text, button) {
  navigator.clipboard.writeText(text).then(() => {
    const originalText = button.querySelector("span").textContent;
    button.querySelector("span").textContent = "已复制!";
    button.classList.add("copied");
    setTimeout(() => {
      button.querySelector("span").textContent = originalText;
      button.classList.remove("copied");
    }, 2000);
  }).catch(err => {
    console.error("复制失败:", err);
  });
}

// 绑定复制按钮事件
document.addEventListener("DOMContentLoaded", () => {
  const copyContentBtn = $("copy-content");
  const copyArticleBtn = $("copy-article");

  if (copyContentBtn) {
    copyContentBtn.addEventListener("click", () => {
      if (!currentResult) return;
      const text = [
        currentResult.metadata.title,
        currentResult.summary,
        ...(currentResult.key_points || []),
        currentResult.cleaned_article || ""
      ].filter(Boolean).join("\n\n");
      copyToClipboard(text, copyContentBtn);
    });
  }

  if (copyArticleBtn) {
    copyArticleBtn.addEventListener("click", () => {
      const article = currentArticleView === "source" ? $("raw-source-text") : $("cleaned-article");
      if (!article) return;
      copyToClipboard(article.textContent, copyArticleBtn);
    });
  }

  setupDownloadThumbnail();
  setupDownloadExperience();
  setupMarkdownExport();
});

function extractValidHttpUrl(value) {
  const match = String(value || "").match(/https?:\/\/[^\s<>\]）)】]+/i);
  if (!match) return "";
  try {
    const parsed = new URL(match[0]);
    return ["http:", "https:"].includes(parsed.protocol) && parsed.hostname ? parsed.href : "";
  } catch (_error) {
    return "";
  }
}

function selectInputRoutes() {
  const rawInput = $("url").value.trim();
  if (!rawInput) return [];
  const lines = rawInput.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  if (lines.length > 1 && !lines.every(line => extractValidHttpUrl(line))) {
    return [{ kind: "text", text: rawInput }];
  }
  return lines.map(line => {
    const url = extractValidHttpUrl(line);
    return url ? { kind: "url", url } : { kind: "text", text: line };
  });
}

document.querySelectorAll("[data-result-view]").forEach(tab => {
  tab.addEventListener("click", () => switchResultView(tab.dataset.resultView));
  tab.addEventListener("keydown", event => {
    if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    const tabs = [...document.querySelectorAll("[data-result-view]")];
    const direction = event.key === "ArrowRight" ? 1 : -1;
    const next = tabs[(tabs.indexOf(tab) + direction + tabs.length) % tabs.length];
    switchResultView(next.dataset.resultView);
    next.focus();
  });
});

document.querySelectorAll("[data-article-view]").forEach(tab => {
  tab.addEventListener("click", () => switchArticleView(tab.dataset.articleView));
  tab.addEventListener("keydown", event => {
    if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    const tabs = [...document.querySelectorAll("[data-article-view]")];
    const direction = event.key === "ArrowRight" ? 1 : -1;
    const next = tabs[(tabs.indexOf(tab) + direction + tabs.length) % tabs.length];
    switchArticleView(next.dataset.articleView);
    next.focus();
  });
});

$("form").addEventListener("submit", async (event) => {
  event.preventDefault();
  currentCacheKey = null;
  taskPresentation.reset();
  const started = performance.now();
  const routes = selectInputRoutes();
  if (!routes.length) {
    setInlineError("请粘贴链接或输入文本后再开始提取");
    return;
  }
  clearInterval(clock);
  $("workbench").hidden = false;
  $("result").classList.remove("active");
  $("error").classList.remove("active");
  $("loading").classList.add("active");
  const mode = $("mode").value;
  const newTasks = routes.map(route => ({
    id: crypto.randomUUID ? crypto.randomUUID() : `task-${Date.now()}-${++taskSequence}`,
    route, mode, refresh: refreshNext, status: "pending", progress: 0,
    startedAt: Date.now(), completedAt: null, result: null, error: ""
  }));
  taskRecords = [...taskRecords, ...newTasks].slice(-100);
  $("url").value = "";
  persistTasks();
  renderTaskQueue();
  showTaskStartNotice(newTasks.length);
  $("loading-label").textContent = routes.length > 1
    ? `正在并行处理 ${routes.length} 个任务`
    : routes[0].kind === "text" ? "正在分析文字" : $("mode").value === "visual" ? "正在提取全模态内容" : "正在提取内容";
  clock = setInterval(() => {
    $("timer").textContent = `${((performance.now() - started) / 1000).toFixed(1)}s`;
  }, 100);
  try {
    await Promise.all(newTasks.map(task => runTask(task)));
    loadHistory();
  } catch (error) {
    setInlineError(error.message);
  } finally {
    refreshNext = false;
    clearInterval(clock);
    if (!taskRecords.some(task => ["pending", "running"].includes(task.status))) {
      $("loading").classList.remove("active");
    }
  }
});

function showTaskStartNotice(taskCount) {
  const notice = $("task-start-notice");
  if (!notice) return;
  clearTimeout(taskStartNoticeTimer);
  notice.textContent = DrillTaskUi.taskStartMessage(taskCount);
  notice.hidden = false;
  requestAnimationFrame(() => notice.classList.add("is-visible"));
  taskStartNoticeTimer = setTimeout(() => {
    notice.classList.remove("is-visible");
    setTimeout(() => { notice.hidden = true; }, 180);
  }, 2400);
}

async function runTask(task) {
  task.status = "running";
  task.progress = Math.max(8, task.progress || 0);
  persistTasks();
  renderTaskQueue();
  let requestTimeout = null;
  try {
    const controller = new AbortController();
    requestTimeout = setTimeout(() => controller.abort(), 35 * 60 * 1000);
    let response;
    if (task.route.kind === "text") {
      const body = new FormData();
      body.append("title", task.route.text.slice(0, 200));
      body.append("text", task.route.text);
      body.append("task_id", task.id);
      response = await fetch("/api/analyze/upload", { method: "POST", body, signal: controller.signal });
    } else {
      response = await fetch("/api/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: task.route.url, input_kind: "auto", mode: task.mode || "auto", refresh: Boolean(task.refresh), task_id: task.id }),
        signal: controller.signal,
      });
    }
    const data = await DrillTaskUi.readResponsePayload(response);
    if (!response.ok) throw new Error(data.detail || "提取失败");
    task.status = "success";
    task.progress = 100;
    task.completedAt = Date.now();
    task.result = data;
    persistTasks();
    renderTaskQueue();
    if (taskPresentation.claimAutomaticResult()) render(data);
  } catch (error) {
    task.status = "error";
    task.progress = 100;
    task.completedAt = Date.now();
    task.error = error.name === "AbortError" ? "任务长时间无响应，已自动结束，请重试" : (error.message || "提取失败");
    persistTasks();
    renderTaskQueue();
  } finally {
    clearTimeout(requestTimeout);
  }
}

function renderTaskQueue() {
  const control = $("task-queue-control");
  const trigger = $("toggle-task-queue");
  const panel = $("task-queue");
  const list = $("task-list");
  const summary = DrillTaskUi.summarizeTasks(taskRecords);
  if (!summary.visible) {
    control.hidden = true;
    setTaskQueueExpanded(false);
    list.innerHTML = "";
    return;
  }
  control.hidden = false;
  const labels = { pending: "排队中", running: "处理中", success: "已完成", error: "失败" };
  $("task-queue-summary").textContent = `${summary.active ? `${summary.active} 进行中 · ` : ""}${summary.completed}/${summary.total} 已结束`;
  renderTaskClearActions(summary);
  const badge = $("task-active-badge");
  badge.hidden = summary.active === 0;
  badge.textContent = summary.active > 9 ? "9+" : String(summary.active);
  badge.classList.toggle("is-active", summary.active > 0);
  trigger.classList.toggle("has-active-tasks", summary.active > 0);
  const signature = taskRecords.map(task => [
    task.id, task.status, Boolean(task.result), task.error || "", task.deleteError || ""
  ].join(":")).join("|") + `|confirm:${taskDeleteConfirmationId || ""}|clear:${taskClearConfirmation}`;
  if (list.dataset.signature === signature) {
    updateTaskMetrics();
    return;
  }
  list.dataset.signature = signature;
  list.innerHTML = taskRecords.map(task => {
    const name = task.route.kind === "url" ? task.route.url : task.route.text;
    const elapsed = Math.max(0, ((task.completedAt || Date.now()) - Number(task.startedAt || Date.now())) / 1000);
    const backendProgress = Number(task.progress || 0);
    const estimated = task.status === "running" ? Math.min(94, Math.max(backendProgress, 12 + Math.log2(elapsed + 1) * 12)) : backendProgress;
    const safeId = escapeHtml(task.id);
    const actionState = DrillTaskUi.taskActionState(task, taskDeleteConfirmationId);
    const errorMessage = DrillTaskUi.taskErrorMessage(task);
    const viewAction = actionState.canView
      ? `<button class="task-view" type="button" data-task-view-id="${safeId}"><i data-lucide="eye" aria-hidden="true"></i>查看</button>`
      : "";
    const deleteAction = actionState.confirmingDelete
      ? `<button class="task-action-confirm" type="button" data-task-delete-confirm-id="${safeId}" aria-label="确认删除 ${escapeAttribute(name)}" title="确认删除"><i data-lucide="check" aria-hidden="true"></i></button><button class="task-action-cancel" type="button" data-task-delete-cancel aria-label="取消删除 ${escapeAttribute(name)}" title="取消删除"><i data-lucide="x" aria-hidden="true"></i></button>`
      : actionState.canDelete
        ? `<button class="task-delete" type="button" data-task-delete-id="${safeId}" aria-label="删除 ${escapeAttribute(name)}" title="删除已完成记录"><i data-lucide="trash-2" aria-hidden="true"></i></button>`
        : "";
    const errorMarkup = errorMessage
      ? `<span class="task-error-message" title="${escapeAttribute(errorMessage)}">${escapeHtml(errorMessage)}</span>`
      : "";
    return `<div class="task-card task-${task.status}" data-task-card-id="${safeId}"><span class="task-state">${DrillTaskUi.taskStateIndicator(task.status)}${labels[task.status]}</span><span class="task-name">${escapeHtml(name)}</span><span class="task-metrics"><strong>${Math.round(estimated)}%</strong><span>${elapsed.toFixed(1)}s</span></span><span class="task-actions">${viewAction}${deleteAction}</span>${errorMarkup}</div>`;
  }).join("");
  list.querySelectorAll("[data-task-view-id]").forEach(button => button.addEventListener("click", () => {
    const task = taskRecords.find(item => item.id === button.dataset.taskViewId);
    if (task?.result) {
      taskPresentation.selectByUser(task.id);
      render(task.result);
      setTaskQueueExpanded(false);
    }
  }));
  list.querySelectorAll("[data-task-delete-id]").forEach(button => button.addEventListener("click", () => {
    taskDeleteConfirmationId = button.dataset.taskDeleteId;
    renderTaskQueue();
  }));
  list.querySelectorAll("[data-task-delete-cancel]").forEach(button => button.addEventListener("click", () => {
    taskDeleteConfirmationId = null;
    renderTaskQueue();
  }));
  list.querySelectorAll("[data-task-delete-confirm-id]").forEach(button => button.addEventListener("click", () => {
    deleteTaskRecord(button.dataset.taskDeleteConfirmId);
  }));
  refreshIcons();
}

function renderTaskClearActions(summary) {
  const slot = $("task-queue-clear-actions");
  if (!slot) return;
  const canClear = DrillTaskUi.canClearCompletedTasks(taskRecords);
  const actionKey = DrillTaskUi.taskClearActionKey(
    summary,
    taskClearConfirmation,
    taskClearError,
  );
  if (slot.dataset.state === actionKey) return;
  slot.dataset.state = actionKey;
  if (!canClear) {
    taskClearConfirmation = false;
    taskClearError = "";
    slot.innerHTML = "";
    return;
  }
  if (taskClearConfirmation) {
    slot.innerHTML = `<button class="task-clear-confirm" type="button" aria-label="确认清理已结束任务" title="确认清理"><i data-lucide="check" aria-hidden="true"></i></button><button class="task-clear-cancel" type="button" aria-label="取消清理" title="取消"><i data-lucide="x" aria-hidden="true"></i></button>`;
    slot.querySelector(".task-clear-confirm")?.addEventListener("click", clearCompletedTasks);
    slot.querySelector(".task-clear-cancel")?.addEventListener("click", () => {
      taskClearConfirmation = false;
      renderTaskQueue();
    });
  } else {
    slot.innerHTML = `<button class="task-clear" type="button" aria-label="一键清理已结束任务" title="${escapeAttribute(taskClearError || "清理已结束任务")}"><i data-lucide="trash-2" aria-hidden="true"></i></button>`;
    slot.querySelector(".task-clear")?.addEventListener("click", () => {
      taskClearConfirmation = true;
      taskClearError = "";
      renderTaskQueue();
    });
  }
  refreshIcons();
}

async function clearCompletedTasks() {
  try {
    const response = await fetch("/api/tasks/completed", { method: "DELETE" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "清理任务失败");
    taskRecords = DrillTaskUi.withoutCompletedTasks(taskRecords);
    taskDeleteConfirmationId = null;
    taskClearConfirmation = false;
    taskClearError = "";
    persistTasks();
    renderTaskQueue();
  } catch (error) {
    taskClearConfirmation = false;
    taskClearError = error.message || "清理任务失败";
    renderTaskQueue();
  }
}

function setTaskQueueExpanded(expanded) {
  const panel = $("task-queue");
  const trigger = $("toggle-task-queue");
  if (!panel || !trigger) return;
  panel.hidden = !expanded;
  trigger.setAttribute("aria-expanded", String(expanded));
  trigger.setAttribute("aria-label", expanded ? "折叠并行任务" : "展开并行任务");
  trigger.title = expanded ? "折叠并行任务" : "展开并行任务";
}

async function deleteTaskRecord(taskId) {
  const task = taskRecords.find(item => item.id === taskId);
  if (!task || !DrillTaskUi.isTerminalTask(task)) return;
  task.deleteError = "";
  try {
    const response = await fetch(`/api/tasks/${encodeURIComponent(taskId)}`, { method: "DELETE" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "删除任务失败");
    taskRecords = taskRecords.filter(item => item.id !== taskId);
    taskDeleteConfirmationId = null;
    persistTasks();
    renderTaskQueue();
  } catch (error) {
    task.deleteError = error.message || "删除任务失败";
    taskDeleteConfirmationId = null;
    renderTaskQueue();
  }
}

function updateTaskMetrics() {
  taskRecords.forEach(task => {
    const card = document.querySelector(`[data-task-card-id="${CSS.escape(task.id)}"]`);
    if (!card) return;
    const elapsed = Math.max(0, ((task.completedAt || Date.now()) - Number(task.startedAt || Date.now())) / 1000);
    const backendProgress = Number(task.progress || 0);
    const estimated = task.status === "running"
      ? Math.min(94, Math.max(backendProgress, 12 + Math.log2(elapsed + 1) * 12))
      : backendProgress;
    const progress = card.querySelector(".task-metrics strong");
    const duration = card.querySelector(".task-metrics span");
    if (progress) progress.textContent = `${Math.round(estimated)}%`;
    if (duration) duration.textContent = `${elapsed.toFixed(1)}s`;
  });
}

function persistTasks() {
  const compact = taskRecords.slice(-100).map(task => ({
    ...task,
    // 完整结果由后端任务记录持久化，避免大型文章撑爆 localStorage。
    result: null,
  }));
  try { localStorage.setItem(TASK_STORAGE_KEY, JSON.stringify(compact)); } catch (_error) {}
}

async function restoreTasks() {
  try {
    const local = JSON.parse(localStorage.getItem(TASK_STORAGE_KEY) || "[]");
    taskRecords = Array.isArray(local) ? local : [];
  } catch (_error) {
    taskRecords = [];
  }
  try {
    const response = await fetch("/api/tasks");
    const data = await response.json();
    const serverById = Object.fromEntries((data.items || []).map(item => [item.id, item]));
    taskRecords = taskRecords.map(task => mergeServerTask(task, serverById[task.id]));
    for (const item of data.items || []) {
      if (!taskRecords.some(task => task.id === item.id)) {
        taskRecords.push(mergeServerTask({
          id: item.id, route: item.route || { kind: "url", url: "恢复的任务" },
          startedAt: Number(item.started_at || Date.now() / 1000) * 1000,
        }, item));
      }
    }
    persistTasks();
    renderTaskQueue();
    startTaskPolling();
  } catch (_error) {
    renderTaskQueue();
  }
}

function mergeServerTask(task, server) {
  if (!server) return task;
  return {
    ...task,
    route: server.route || task.route,
    status: server.status || task.status,
    progress: Number(server.progress ?? task.progress ?? 0),
    startedAt: Number(server.started_at || task.startedAt / 1000 || Date.now() / 1000) * 1000,
    completedAt: ["success", "error"].includes(server.status) ? Number(server.updated_at || Date.now() / 1000) * 1000 : null,
    result: server.result || task.result || null,
    error: server.error || task.error || "",
  };
}

function startTaskPolling() {
  clearInterval(taskPollTimer);
  taskPollTimer = setInterval(async () => {
    const active = taskRecords.filter(task => ["pending", "running"].includes(task.status));
    if (!active.length) return;
    await Promise.all(active.map(async task => {
      try {
        const response = await fetch(`/api/tasks/${encodeURIComponent(task.id)}`);
        if (!response.ok) return;
        const server = await response.json();
        Object.assign(task, mergeServerTask(task, server));
      } catch (_error) {}
    }));
    persistTasks();
    renderTaskQueue();
    loadHistory();
  }, 1200);
}

function setInlineError(message) {
  $("error").textContent = message;
  $("error").classList.add("active");
}

function showThumbnail(url, title) {
  const thumbnail = $("thumbnail");
  const frame = document.querySelector(".thumbnail-frame");
  const placeholder = $("thumbnail-placeholder");
  const downloadBtn = $("download-thumbnail");
  thumbnail.onload = () => {
    frame?.classList.remove("image-loading");
    thumbnail.hidden = false;
    placeholder.hidden = true;
    if (downloadBtn) downloadBtn.hidden = false;
  };
  thumbnail.onerror = () => {
    frame?.classList.remove("image-loading");
    thumbnail.hidden = true;
    placeholder.hidden = false;
    thumbnail.removeAttribute("src");
    if (downloadBtn) downloadBtn.hidden = true;
  };
  thumbnail.alt = title ? `${title}封面` : "内容封面";
  if (!url) {
    thumbnail.onerror();
    return;
  }
  placeholder.hidden = false;
  frame?.classList.toggle("image-loading", Boolean(url));
  thumbnail.hidden = true;
  if (downloadBtn) downloadBtn.hidden = true;
  thumbnail.src = url;
}

function render(data) {
  DrillTaskUi.showResultContent({ loading: $("loading"), result: $("result") });
  currentResult = data;
  const meta = data.metadata;
  showThumbnail(meta.thumbnail, meta.title);
  $("platform").textContent = meta.platform;
  $("video-title").textContent = meta.title;
  const sourceLink = $("source-link");
  const sourceUrl = extractValidHttpUrl(meta.webpage_url);
  if (sourceLink) {
    sourceLink.hidden = !sourceUrl;
    sourceLink.href = sourceUrl || "#";
    const label = sourceLink.querySelector("span");
    if (label) label.textContent = meta.content_type === "video" ? "打开原视频" : "打开原内容";
  }
  const mediaSize = meta.content_type === "article"
    ? "文章"
    : meta.content_type === "upload_bundle"
      ? `多模态组合${meta.image_count ? ` / ${meta.image_count} 张图片` : ""}`
    : meta.content_type === "image_carousel"
    ? `${meta.image_count || 0} 张图片`
    : meta.duration_seconds
      ? `${Math.round(meta.duration_seconds / 60 * 10) / 10} 分钟`
      : "时长未知";
  $("meta").textContent = [meta.uploader, mediaSize].filter(Boolean).join(" / ");
  $("strategy").textContent = `${strategyLabels[data.strategy] || data.strategy}${data.cached ? " / 缓存" : ""}`;
  $("summary").textContent = data.summary;
  $("coverage").textContent = data.coverage_note;
  $("points").innerHTML = data.key_points.map((point, i) =>
    `<div class="point"><b>${String(i + 1).padStart(2, "0")}</b><span>${escapeHtml(point)}</span></div>`
  ).join("");
  $("topics").innerHTML = data.topics.map(topic => `<span class="topic">${escapeHtml(topic)}</span>`).join("");
  renderMediaGallery(data);
  renderResourceView(data);
  renderResources(data.resources || []);
  renderOpinion(data.opinion_assessment);
  const rawSource = data.transcript || data.full_source_text || data.transcript_excerpt || "";
  renderRawSource(rawSource);

  const plan = data.extraction_plan || {};
  const coverage = data.coverage || {};
  $("plan-badges").innerHTML = [
    [meta.content_type === "image_carousel" ? "images" : "video", meta.content_type === "image_carousel" ? "图文" : "视频"],
    ["scan-search", videoTypeLabels[plan.video_type] || plan.video_type || "未分类"],
    ["layers-3", plan.highest_cost_level || "—"],
    ["circle-check", coverageStatusLabels[coverage.status] || coverage.status || "未知"]
  ].map(([icon, text]) => `<span class="plan-badge"><i data-lucide="${icon}" aria-hidden="true"></i>${escapeHtml(text)}</span>`).join("");

  $("coverage-grid").innerHTML = [
    ["语音", coverage.speech_percent ?? coverage.audio_percent ?? 0],
    ["全文保留", coverage.text_retention_percent ?? 0],
    ["屏幕文字", coverage.screen_text_percent ?? 0],
    ["场景", coverage.scene_percent ?? 0],
    ["发布上下文", coverage.post_context_captured ? 100 : 0]
  ].map(([name, value]) =>
    `<div class="coverage-cell"><span>${escapeHtml(name)}</span><strong>${Number(value).toFixed(1)}%</strong></div>`
  ).join("");

  renderCleanedArticle(
    data.cleaned_article ||
    organizeSourceText(data.full_source_text || data.transcript || data.transcript_excerpt || "")
  );

  $("cost-ladder").innerHTML = (data.cost_trace || []).map(step =>
    `<div class="cost-step ${step.executed ? "executed" : ""}"><strong>${escapeHtml(step.level)}</strong>${escapeHtml(step.name)}<br>${escapeHtml(step.reason)}</div>`
  ).join("") || '<div class="history-empty">暂无成本记录</div>';

  $("keyframes").innerHTML = (data.keyframes || []).map(frame => {
    const texts = (frame.ocr_text || []).join(" / ") || "无关键文字";
    const observations = (frame.visual_observations || []).join(" / ");
    const position = frame.frame_type === "image_slide" ? `图片 ${frame.frame_index}` : formatTime(frame.timestamp_seconds);
    return `<div class="keyframe-row"><strong>${escapeHtml(position)}</strong> / ${escapeHtml(frame.frame_type)}<br>OCR：${escapeHtml(texts)}${observations ? `<br>画面：${escapeHtml(observations)}` : ""}</div>`;
  }).join("") || '<div class="history-empty">暂无关键帧结果</div>';

  renderPipelineTimings(data);
  $("workbench").hidden = false;
  switchResultView(data.image_only ? "resources" : "overview");
  switchArticleView("polished");
  refreshIcons();
  $("content-scroll").scrollTo({ top: 0, behavior: "smooth" });
}

function visibleExtractionMilliseconds(data) {
  const stages = data.timings || [];
  return stages.length
    ? stages.reduce((total, item) => total + Number(item.milliseconds || 0), 0)
    : Number(data.extraction_milliseconds || 0);
}

function fullPipelineMilliseconds(data) {
  const orchestration = (data.orchestration_timings || [])
    .reduce((total, item) => total + Number(item.milliseconds || 0), 0);
  return Number(data.full_pipeline_milliseconds) || Math.round(
    visibleExtractionMilliseconds(data) + orchestration
  );
}

function renderMediaGallery(data) {
  const gallery = $("media-gallery");
  const images = Array.isArray(data.original_images) ? data.original_images.filter(Boolean) : [];
  if (!gallery) return;
  gallery.hidden = images.length === 0;
  gallery.innerHTML = images.map((url, index) => {
    const safeUrl = escapeAttribute(`/api/remote-image?url=${encodeURIComponent(url)}`);
    const sourceUrl = escapeAttribute(url);
    const filename = `image-${String(index + 1).padStart(2, "0")}.jpg`;
    return `<div class="media-item"><a href="${safeUrl}" target="_blank" rel="noopener noreferrer"><img src="${safeUrl}" alt="原图 ${index + 1}" loading="lazy"></a><div class="media-item-overlay"><button class="media-item-download" type="button" data-image-url="${sourceUrl}" data-image-name="${filename}"><i data-lucide="download" aria-hidden="true"></i>下载原图</button></div></div>`;
  }).join("");
  gallery.querySelectorAll("[data-image-url]").forEach(button => button.addEventListener("click", () => downloadResource(button.dataset.imageUrl, button.dataset.imageName, button)));
}

function renderResourceView(data) {
  const tab = $("tab-resources");
  const panel = $("view-resources");
  const gallery = $("resource-gallery");
  const images = Array.isArray(data.original_images) ? data.original_images.filter(Boolean) : [];
  const visible = data.image_only || data.metadata?.content_type === "image_carousel";
  tab.hidden = !visible;
  panel.hidden = !visible;
  if (!visible) return;
  $("resource-view-note").textContent = data.image_only
    ? `检测为图片资源帖，共 ${images.length} 张原图；已跳过 OCR、视觉理解和信息整合。`
    : `共提取 ${images.length} 张图片，可点击预览或下载原图。`;
  gallery.innerHTML = images.map((url, index) => {
    const safeUrl = escapeAttribute(`/api/remote-image?url=${encodeURIComponent(url)}`);
    const sourceUrl = escapeAttribute(url);
    const filename = `resource-${String(index + 1).padStart(2, "0")}.jpg`;
    return `<figure class="resource-gallery-item image-loading"><a href="${safeUrl}" target="_blank" rel="noopener noreferrer"><img src="${safeUrl}" alt="原图 ${index + 1}" loading="lazy"></a><figcaption><span>原图 ${String(index + 1).padStart(2, "0")}</span><button class="resource-download" type="button" data-resource-url="${sourceUrl}" data-resource-name="${filename}"><i data-lucide="download" aria-hidden="true"></i>下载</button></figcaption></figure>`;
  }).join("") || '<div class="history-empty">暂无可下载图片</div>';
  gallery.querySelectorAll("[data-resource-url]").forEach(button => button.addEventListener("click", () => downloadResource(button.dataset.resourceUrl, button.dataset.resourceName, button)));
  gallery.querySelectorAll("img").forEach(image => {
    const finish = () => image.closest(".resource-gallery-item")?.classList.remove("image-loading");
    image.addEventListener("load", finish, { once: true });
    image.addEventListener("error", finish, { once: true });
    if (image.complete) finish();
  });
  refreshIcons();
}

function renderResources(resources) {
  const section = $("resources-section");
  const list = $("resources-list");
  const items = (resources || []).filter(item => item && item.url);
  section.hidden = items.length === 0;
  list.innerHTML = items.map(item => {
    const url = extractValidHttpUrl(item.url);
    if (!url) return "";
    const kind = item.kind || "other";
    return `<div class="resource-card"><div class="resource-card-main"><span class="resource-kind">${escapeHtml(kind)}</span><a href="${escapeAttribute(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.title || url)}</a>${item.description ? `<p>${escapeHtml(item.description)}</p>` : ""}</div>${item.downloadable ? `<button type="button" class="resource-download" data-resource-url="${escapeAttribute(url)}" data-resource-name="${escapeAttribute(item.title || "resource")}"><i data-lucide="download" aria-hidden="true"></i>下载</button>` : ""}</div>`;
  }).join("");
  list.querySelectorAll("[data-resource-url]").forEach(button => button.addEventListener("click", () => downloadResource(button.dataset.resourceUrl, button.dataset.resourceName, button)));
}

function renderOpinion(assessment) {
  const section = $("opinion-section");
  const target = $("opinion-assessment");
  if (!assessment) { section.hidden = true; target.innerHTML = ""; return; }
  section.hidden = false;
  const verdict = assessment.verdict || "insufficient_evidence";
  const risk = assessment.promotional_risk || "unknown";
  const labels = { useful: "较为实用", mixed: "价值有限，需甄别", marketing_heavy: "营销/夸大风险较高", insufficient_evidence: "证据不足" };
  const riskLabels = { low: "低", medium: "中", high: "高", unknown: "未知" };
  const sourceLinks = (assessment.sources || []).filter(url => extractValidHttpUrl(url)).map(url =>
    `<a href="${escapeAttribute(url)}" target="_blank" rel="noopener noreferrer"><i data-lucide="external-link" aria-hidden="true"></i>${escapeHtml(new URL(url).hostname)}</a>`
  ).join("");
  target.innerHTML = `<div class="opinion-badges"><span class="opinion-verdict verdict-${escapeAttribute(verdict)}">${escapeHtml(labels[verdict] || verdict)}</span><span class="opinion-risk risk-${escapeAttribute(risk)}">宣传风险：${escapeHtml(riskLabels[risk] || risk)}</span>${assessment.web_searched ? '<span class="opinion-web-badge"><i data-lucide="globe-2" aria-hidden="true"></i>已联网核验</span>' : ""}</div><p>${escapeHtml(assessment.usefulness || "")}</p>${(assessment.reasons || []).length ? `<h4>判断依据</h4><ul>${assessment.reasons.map(reason => `<li>${escapeHtml(reason)}</li>`).join("")}</ul>` : ""}${(assessment.advice || []).length ? `<h4>建议</h4><ul>${assessment.advice.map(item => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : ""}${sourceLinks ? `<div class="opinion-sources"><strong>参考来源</strong>${sourceLinks}</div>` : ""}`;
  refreshIcons();
}

function renderPipelineTimings(data) {
  const extractionMilliseconds = visibleExtractionMilliseconds(data);
  const totalMilliseconds = fullPipelineMilliseconds(data);
  const extractionTraceItems = (data.timings || []).map(item => ({
    phase: "信息提取", name: item.name, milliseconds: Number(item.milliseconds || 0), kind: "extraction"
  }));
  const orchestrationTraceItems = (data.orchestration_timings || []).map(item => ({
    phase: "全流程", name: item.name, milliseconds: Number(item.milliseconds || 0), kind: "orchestration"
  }));
  const orchestrationByName = Object.fromEntries(orchestrationTraceItems.map(item => [item.name, item]));
  const traceItems = [
    orchestrationByName["输入解析与安全展开"],
    ...extractionTraceItems,
    orchestrationByName["封面获取与转存"],
    orchestrationByName["其他编排开销"],
    ...orchestrationTraceItems.filter(item => ![
      "输入解析与安全展开", "封面获取与转存", "其他编排开销"
    ].includes(item.name))
  ].filter(Boolean);
  const traceHtml = traceItems.map(item =>
    `<div class="trace-item trace-${item.kind}"><span>${escapeHtml(item.phase)} · ${escapeHtml(item.name)}</span><strong>${formatDuration(item.milliseconds)}</strong></div>`
  ).join("");
  const totalsHtml = `<div class="trace-item trace-total"><span>全流程 · 总时长</span><strong>${formatDuration(totalMilliseconds)}</strong></div>`;
  $("trace").innerHTML = traceHtml + totalsHtml;
  $("process-trace").innerHTML = traceHtml + totalsHtml;
  $("full-pipeline-summary").innerHTML = [
    ["信息提取", extractionMilliseconds],
    ["全流程", totalMilliseconds],
    ["本次费用", Number(data.estimated_cost_cny || 0)]
  ].map(([label, value], index) => `<div class="pipeline-summary-item ${index === 1 ? "primary" : ""}"><span>${escapeHtml(label)}</span><strong>${index === 2 ? formatCost(value) : formatDuration(value)}</strong></div>`).join("");
}

function formatCost(value) {
  const cost = Math.max(0, Number(value) || 0);
  if (cost === 0) return "¥0.00000";
  return `¥${cost.toFixed(cost < 0.01 ? 5 : 4)}`;
}

function formatDuration(milliseconds) {
  const value = Math.max(0, Number(milliseconds) || 0);
  if (value < 1000) return `${Math.round(value)} ms`;
  if (value < 60000) return `${(value / 1000).toFixed(value < 10000 ? 2 : 1)}s`;
  const minutes = Math.floor(value / 60000);
  const seconds = Math.round((value % 60000) / 1000);
  return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
}

async function loadHistory({ resetScroll = false } = {}) {
  const list = $("history-list");
  const previousScrollTop = list.scrollTop;
  try {
    const response = await fetch("/api/videos");
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "读取失败");
    const historySignature = DrillTaskUi.historyItemsSignature(data.items);
    if (!data.items.length) {
      historyByKey = {};
      historyItems = [];
      updateHistoryFilters([]);
      list.dataset.signature = historySignature;
      list.innerHTML = '<div class="history-empty">暂无提取记录</div>';
      list.scrollTop = 0;
      return;
    }
    historyItems = data.items;
    historyByKey = Object.fromEntries(historyItems.map(item => [item.cache_key, item.result]));
    updateHistoryFilters(historyItems);
    if (list.dataset.signature === historySignature && !resetScroll) {
      renderHistoryItems();
      if (resetScroll) list.scrollTop = 0;
      return;
    }
    list.dataset.signature = historySignature;
    renderHistoryItems({ resetScroll, previousScrollTop });
  } catch (error) {
    list.innerHTML = `<div class="history-empty">${escapeHtml(error.message)}</div>`;
  }
}

function updateHistoryFilters(items) {
  const sources = [...new Set(items.map(item => DrillTaskUi.classifyHistoryItem(item).source))].sort();
  const types = [...new Set(items.map(item => DrillTaskUi.classifyHistoryItem(item).type))].sort();
  [["history-source-filter", "全部来源", sources], ["history-type-filter", "全部类型", types]].forEach(([id, all, values]) => {
    const select = $(id); if (!select) return;
    const selected = select.value || all;
    select.innerHTML = [all, ...values].map(value => `<option${value === selected ? " selected" : ""}>${escapeHtml(value)}</option>`).join("");
  });
}

function renderHistoryItems(options = {}) {
  const list = $("history-list");
  if (!list) return;
  const previousScrollTop = options.previousScrollTop ?? list.scrollTop;
  const items = DrillTaskUi.filterHistoryItems(
    historyItems, $("history-search")?.value, $("history-source-filter")?.value, $("history-type-filter")?.value,
  );
  if (!items.length) {
    list.innerHTML = `<div class="history-empty">${historyItems.length ? "没有匹配的记录" : "暂无提取记录"}</div>`;
    return;
  }
  list.innerHTML = items.map(item => {
      const result = item.result;
      const date = new Date(item.created_at).toLocaleString("zh-CN");
      const coverage = result.coverage || {};
      const classification = DrillTaskUi.classifyHistoryItem(item);
      return `<div class="history-item">
        <div>
          <p class="history-title">${escapeHtml(result.metadata.title)}</p>
          <div class="history-meta"><span class="history-tags"><span class="history-tag">${escapeHtml(classification.source)}</span><span class="history-tag">${escapeHtml(classification.type)}</span></span>${escapeHtml(strategyLabels[result.strategy] || result.strategy)} / ${escapeHtml(coverageStatusLabels[coverage.status] || coverage.status || "未知")} / ${escapeHtml(date)}${item.expired ? " / 已过期" : ""}</div>
        </div>
        <div class="history-actions">
          <button type="button" onclick="viewStored('${item.cache_key}')"><i data-lucide="eye" aria-hidden="true"></i>查看</button>
          <button type="button" onclick="reanalyze('${item.cache_key}', '${encodeURIComponent(result.metadata.webpage_url)}')"><i data-lucide="rotate-ccw" aria-hidden="true"></i>重跑</button>
          <button class="delete-button" type="button" aria-label="删除记录" title="删除" onclick="deleteVideo('${item.cache_key}', this)"><i data-lucide="trash-2" aria-hidden="true"></i></button>
        </div>
      </div>`;
    }).join("");
    list.scrollTop = DrillTaskUi.historyScrollTarget(
      previousScrollTop,
      list.scrollHeight,
      list.clientHeight,
      Boolean(options.resetScroll),
    );
    refreshIcons();
}

async function deleteVideo(cacheKey, trigger) {
  if (!trigger) return;
  const action = createInlineConfirmation(trigger, "删除这条记录？");
  if (!await action) return;
  const response = await fetch(`/api/videos/${encodeURIComponent(cacheKey)}`, { method: "DELETE" });
  if (response.ok) loadHistory();
}

function viewStored(cacheKey) {
  const result = historyByKey[cacheKey];
  if (!result) return;
  currentCacheKey = cacheKey;
  $("url").value = result.metadata.webpage_url;
  render({ ...result, cached: true });
}

function reanalyze(cacheKey, encodedUrl) {
  $("url").value = decodeURIComponent(encodedUrl);
  refreshNext = true;
  $("form").requestSubmit();
}

$("clear-history").addEventListener("click", async () => {
  const action = createInlineConfirmation($("clear-history"), "清空全部缓存？");
  if (!await action) return;
  const response = await fetch("/api/videos", { method: "DELETE" });
  if (response.ok) {
    $("result").classList.remove("active");
    $("workbench").hidden = true;
    loadHistory({ resetScroll: true });
  }
});

function createInlineConfirmation(trigger, message) {
  return new Promise(resolve => {
    if (trigger.dataset.inlineConfirm === "1") return resolve(false);
    trigger.dataset.inlineConfirm = "1";
    const icon = trigger.querySelector("[data-lucide]");
    const originalIcon = icon?.getAttribute("data-lucide") || "trash-2";
    if (icon) icon.setAttribute("data-lucide", "check");
    trigger.setAttribute("aria-label", "确认操作");
    trigger.title = message;
    refreshIcons();
    const finish = value => {
      trigger.dataset.inlineConfirm = "0";
      if (icon) icon.setAttribute("data-lucide", originalIcon);
      trigger.setAttribute("aria-label", "删除记录");
      trigger.removeAttribute("title");
      refreshIcons();
      resolve(value);
    };
    trigger.addEventListener("click", () => finish(true), { once: true });
    setTimeout(() => { if (trigger.dataset.inlineConfirm === "1") finish(false); }, 4000);
  });
}

function switchResultView(name) {
  document.querySelectorAll("[data-result-view]").forEach(tab => {
    const active = tab.dataset.resultView === name;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
  });
  document.querySelectorAll("[data-view-panel]").forEach(panel => {
    const active = panel.dataset.viewPanel === name;
    panel.hidden = !active;
    panel.classList.toggle("active", active);
  });
}

function switchArticleView(name) {
  currentArticleView = name === "source" ? "source" : "polished";
  document.querySelectorAll("[data-article-view]").forEach(tab => {
    const active = tab.dataset.articleView === currentArticleView;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
  });
  document.querySelectorAll("[data-article-panel]").forEach(panel => {
    const active = panel.dataset.articlePanel === currentArticleView;
    panel.hidden = !active;
    panel.classList.toggle("active", active);
  });
}

function renderRawSource(source) {
  const target = $("raw-source-text");
  const text = String(source || "").trim();
  if (!text) {
    target.innerHTML = '<div class="article-empty">暂无可展示的提取原文</div>';
    return;
  }
  const blocks = text.split(/\n\s*\n/).map(block => block.trim()).filter(Boolean);
  target.innerHTML = blocks.map(block => {
    const lines = block.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
    return `<p>${lines.map(line => linkifyPlainUrls(escapeHtml(line))).join("<br>")}</p>`;
  }).join("");
}

function renderCleanedArticle(article) {
  if (!article.trim()) {
    $("cleaned-article").innerHTML = '<div class="article-empty">暂无可整理的全文</div>';
    return;
  }
  $("cleaned-article").innerHTML = renderMarkdown(article);
}

function renderMarkdown(markdown) {
  const output = [];
  const lines = String(markdown || "").replace(/\r\n?/g, "\n").split("\n");
  let paragraph = [];
  let listType = "";
  let inCode = false;
  let code = [];
  const flushParagraph = () => {
    if (!paragraph.length) return;
    output.push(`<p>${renderInlineMarkdown(paragraph.join(" "))}</p>`);
    paragraph = [];
  };
  const closeList = () => {
    if (!listType) return;
    output.push(`</${listType}>`);
    listType = "";
  };
  lines.forEach(line => {
    const trimmed = line.trim();
    if (/^```/.test(trimmed)) {
      flushParagraph(); closeList();
      if (inCode) { output.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`); code = []; }
      inCode = !inCode;
      return;
    }
    if (inCode) { code.push(line); return; }
    if (!trimmed) { flushParagraph(); closeList(); return; }
    const heading = trimmed.match(/^(#{1,6})\s+(.+)$/);
    if (heading) { flushParagraph(); closeList(); const level = Math.min(6, heading[1].length + 1); output.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`); return; }
    const unordered = trimmed.match(/^[-*+]\s+(.+)$/);
    const ordered = trimmed.match(/^\d+[.)]\s+(.+)$/);
    if (unordered || ordered) {
      flushParagraph();
      const nextType = ordered ? "ol" : "ul";
      if (listType !== nextType) { closeList(); output.push(`<${nextType}>`); listType = nextType; }
      output.push(`<li>${renderInlineMarkdown((unordered || ordered)[1])}</li>`);
      return;
    }
    if (/^>\s?/.test(trimmed)) { flushParagraph(); closeList(); output.push(`<blockquote>${renderInlineMarkdown(trimmed.replace(/^>\s?/, ""))}</blockquote>`); return; }
    if (/^(?:---+|___+|\*\*\*+)$/.test(trimmed)) { flushParagraph(); closeList(); output.push("<hr>"); return; }
    closeList(); paragraph.push(trimmed);
  });
  if (inCode && code.length) output.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
  flushParagraph(); closeList();
  return `<section class="article-section markdown-body">${output.join("")}</section>`;
}

function renderInlineMarkdown(value) {
  let text = escapeHtml(value);
  const links = [];
  text = text.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (_match, label, url) => {
    const token = `\u0000LINK${links.length}\u0000`;
    links.push(`<a href="${escapeAttribute(url)}" target="_blank" rel="noopener noreferrer">${label}</a>`);
    return token;
  });
  text = linkifyPlainUrls(text);
  text = text.replace(/`([^`]+)`/g, "<code>$1</code>");
  text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  text = text.replace(/__([^_]+)__/g, "<strong>$1</strong>");
  text = text.replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, "<em>$1</em>");
  links.forEach((link, index) => { text = text.replace(`\u0000LINK${index}\u0000`, link); });
  return text;
}

function linkifyPlainUrls(escapedText) {
  return escapedText.replace(/https?:\/\/[^\s<]+/g, url => `<a href="${escapeAttribute(url)}" target="_blank" rel="noopener noreferrer">${url}</a>`);
}

function organizeSourceText(source) {
  const seen = new Set();
  const lines = source.split(/\r?\n/).map(line => line
    .replace(/^(?:\[[^\]]+\])+\s*/, "")
    .replace(/^\[?\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?\]?\s*/, "")
    .replace(/^(?:标题|作者)：\s*/, "")
    .replace(/^简介：\s*/, "")
    .trim()
  ).filter(line => {
    const key = line.replace(/[\s，。！？、；：,.!?;:]+/g, "").toLowerCase();
    if (key.length < 4 || seen.has(key) || line.endsWith("...") || line.endsWith("…")) return false;
    seen.add(key);
    return true;
  });
  return lines.join("\n\n");
}

function refreshIcons() {
  if (window.lucide?.createIcons) {
    window.lucide.createIcons();
  }
}

function escapeHtml(value) {
  const node = document.createElement("div");
  node.textContent = value ?? "";
  return node.innerHTML;
}

function escapeAttribute(value) {
  return escapeHtml(value).replace(/`/g, "&#96;");
}

function formatTime(seconds) {
  const value = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = String(Math.floor(value / 3600)).padStart(2, "0");
  const minutes = String(Math.floor(value % 3600 / 60)).padStart(2, "0");
  const remain = String(value % 60).padStart(2, "0");
  return `${hours}:${minutes}:${remain}`;
}

// ========== 资源下载功能 ==========

async function downloadResource(url, filename, trigger = null) {
  const source = String(url || "");
  if (!source) return;
  const thumbnailMatch = source.match(/^\/api\/thumbnails\/([a-f0-9]{64})$/i);
  if (thumbnailMatch) {
    setDownloadButtonState(trigger, "loading");
    setDownloadStatus("loading", "正在保存封面…");
    try {
      const response = await fetch(`/api/download/save-thumbnail/${thumbnailMatch[1]}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          filename: filename || "thumbnail.jpg",
          project_name: currentResult?.metadata?.title || "未命名项目",
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "封面保存失败");
      setDownloadStatus("success", `已保存到 ${data.path}`);
      setDownloadButtonState(trigger, "success");
    } catch (error) {
      setDownloadStatus("error", error.message || "封面保存失败");
      setDownloadButtonState(trigger, "error");
    }
    return;
  }
  setDownloadButtonState(trigger, "loading");
  setDownloadStatus("loading", "正在保存资源…");
  try {
    const response = await fetch("/api/download/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: source,
        filename: filename || "download",
        project_name: currentResult?.metadata?.title || "未命名项目",
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "下载失败");
    setDownloadStatus("success", `已保存到 ${data.path}`);
    setDownloadButtonState(trigger, "success");
  } catch (error) {
    setDownloadStatus("error", error.message || "下载失败");
    setDownloadButtonState(trigger, "error");
  }
}

function setDownloadButtonState(button, state, labelOverride = "") {
  if (!button) return;
  const icon = button.querySelector("[data-lucide]");
  if (!icon) return;
  const iconName = state === "loading" ? "loader-circle" : state === "success" ? "check" : state === "error" ? "circle-alert" : "download";
  icon.setAttribute("data-lucide", iconName);
  button.classList.toggle("is-loading", state === "loading");
  button.disabled = state === "loading";
  const label = button.querySelector("span");
  if (label) {
    if (!button.dataset.idleLabel) button.dataset.idleLabel = label.textContent;
    label.textContent = labelOverride || (state === "loading" ? "下载中" : state === "success" ? "已完成" : state === "error" ? "下载失败" : button.dataset.idleLabel);
  }
  refreshIcons();
  if (state !== "loading" && state !== "idle") {
    setTimeout(() => setDownloadButtonState(button, "idle"), 1800);
  }
}

function setDownloadStatus(kind, message, statusId = "download-status") {
  const status = $(statusId);
  if (!status) return;
  if (!message) {
    status.hidden = true;
    status.textContent = "";
    return;
  }
  status.hidden = false;
  status.className = `download-status ${kind}`;
  status.innerHTML = `${kind === "loading" ? '<span class="mini-spinner"></span>' : `<i data-lucide="${kind === "success" ? "circle-check" : "circle-alert"}" aria-hidden="true"></i>`}<span>${escapeHtml(message)}</span>`;
  refreshIcons();
}

function setupMarkdownExport() {
  const trigger = $("export-article-md");
  trigger?.addEventListener("click", async () => {
    const payload = DrillTaskUi.buildMarkdownExportPayload(currentResult);
    if (!payload.content.trim()) {
      return setDownloadStatus("error", "当前没有可导出的完整全文", "article-download-status");
    }
    setDownloadButtonState(trigger, "loading", "保存中");
    setDownloadStatus("loading", "正在保存 Markdown…", "article-download-status");
    try {
      const response = await fetch("/api/download/save-text", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Markdown 导出失败");
      setDownloadStatus("success", `已保存到 ${data.path}`, "article-download-status");
      setDownloadButtonState(trigger, "success", "已导出");
    } catch (error) {
      setDownloadStatus("error", error.message || "Markdown 导出失败", "article-download-status");
      setDownloadButtonState(trigger, "error", "导出失败");
    }
  });
}

function setupDownloadExperience() {
  const modal = $("download-settings-modal");
  const openModal = async () => {
    modal.hidden = false;
    try {
      const response = await fetch("/api/download-settings");
      const data = await response.json();
      $("download-directory").value = data.directory || "";
    } catch (_error) {}
    refreshIcons();
  };
  $("download-settings-button")?.addEventListener("click", openModal);
  modal?.querySelectorAll("[data-close-download-settings]").forEach(button => button.addEventListener("click", () => { modal.hidden = true; }));
  $("save-download-settings")?.addEventListener("click", async () => {
    const response = await fetch("/api/download-settings", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ directory: $("download-directory").value }),
    });
    const data = await response.json();
    if (!response.ok) return setDownloadStatus("error", data.detail || "目录保存失败");
    modal.hidden = true;
    setDownloadStatus("success", `下载目录已设为 ${data.directory}`);
  });
  $("open-download-folder")?.addEventListener("click", () => fetch("/api/download/open-folder", { method: "POST" }));
  $("download-all-resources")?.addEventListener("click", async () => {
    const trigger = $("download-all-resources");
    const images = Array.isArray(currentResult?.original_images) ? currentResult.original_images : [];
    const extra = (currentResult?.resources || []).filter(item => item.downloadable && item.url && !images.includes(item.url));
    const items = [
      ...images.map((url, index) => ({ url, filename: `resource-${String(index + 1).padStart(2, "0")}.jpg` })),
      ...extra.map((item, index) => ({ url: item.url, filename: item.title || `attachment-${index + 1}` })),
    ];
    if (!items.length) return setDownloadStatus("error", "当前没有可下载资源");
    const loadingFeedback = DrillTaskUi.formatBatchDownloadFeedback({ kind: "loading", itemCount: items.length });
    setDownloadButtonState(trigger, "loading", loadingFeedback.buttonLabel);
    setDownloadStatus("loading", loadingFeedback.detail);
    try {
      const response = await fetch("/api/download/batch", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          items,
          project_name: currentResult?.metadata?.title || "未命名项目",
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "批量下载失败");
      const feedback = DrillTaskUi.formatBatchDownloadFeedback({
        kind: data.failed ? "error" : "success",
        saved: data.saved,
        failed: data.failed,
        directory: data.directory,
      });
      setDownloadStatus(data.failed ? "error" : "success", feedback.detail);
      setDownloadButtonState(trigger, data.failed ? "error" : "success", feedback.buttonLabel);
    } catch (error) {
      const feedback = DrillTaskUi.formatBatchDownloadFeedback({ kind: "error", message: error.message || "批量下载失败" });
      setDownloadStatus("error", feedback.detail);
      setDownloadButtonState(trigger, "error", feedback.buttonLabel);
    }
  });
}

function setupDownloadThumbnail() {
  const btn = $("download-thumbnail");
  if (!btn) return;
  btn.addEventListener("click", () => {
    if (!currentResult) return;
    const meta = currentResult.metadata;
    const thumbnailUrl = meta.thumbnail;
    if (!thumbnailUrl) return;
    const ext = thumbnailUrl.split(".").pop()?.split("?")[0] || "jpg";
    const filename = `${meta.title || "thumbnail"}.${ext}`;
    downloadResource(thumbnailUrl, filename, btn);
  });
}
