const $ = (id) => document.getElementById(id);
const strategyLabels = { subtitle: "完整字幕", asr: "分段 ASR", visual: "全模态", hybrid: "音画联合", metadata: "元数据" };
const videoTypeLabels = { speech_dominant: "口播主导", text_dominant: "文字主导", event_footage: "现场事件", mixed: "多模态", low_information: "低信息", unknown: "待判断" };
const coverageStatusLabels = { structured_ready: "结构化完成", partial: "部分覆盖", needs_review: "需要复核", no_structured_information: "无结构化信息", unavailable: "不可用", metadata_only: "仅元数据", complete: "完整" };
let clock;
let refreshNext = false;
let historyByKey = {};
let currentResult = null;
let currentCacheKey = null;

refreshIcons();
loadHistory();

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

function selectInputRoute() {
  const rawInput = $("url").value.trim();
  const url = extractValidHttpUrl(rawInput);
  if (url) return { kind: "url", url };
  if (rawInput) return { kind: "text", text: rawInput };
  return { kind: "empty" };
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

$("form").addEventListener("submit", async (event) => {
  event.preventDefault();
  currentCacheKey = null;
  const started = performance.now();
  clearInterval(clock);
  $("workbench").hidden = false;
  $("result").classList.remove("active");
  $("error").classList.remove("active");
  $("loading").classList.add("active");
  $("submit").disabled = true;
  const route = selectInputRoute();
  const analyzingText = route.kind === "text";
  $("loading-label").textContent = analyzingText ? "分析文字并核实信源" : $("mode").value === "visual" ? "提取全模态内容并核实信源" : "提取内容并核实信源";
  clock = setInterval(() => {
    $("timer").textContent = `${((performance.now() - started) / 1000).toFixed(1)}s`;
  }, 100);
  try {
    if (route.kind === "empty") {
      throw new Error("请粘贴链接、分享文本，或直接输入需要核验的文字");
    }
    let response;
    if (route.kind === "text") {
      const body = new FormData();
      body.append("title", route.text.slice(0, 200));
      body.append("text", route.text);
      body.append("verify", "true");
      response = await fetch("/api/analyze/upload", { method: "POST", body });
    } else {
      response = await fetch("/api/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: route.url, input_kind: "auto", mode: $("mode").value, refresh: refreshNext, verify: true })
      });
    }
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "提取失败");
    render(data);
    loadHistory();
  } catch (error) {
    $("error").textContent = error.message;
    $("error").classList.add("active");
  } finally {
    refreshNext = false;
    clearInterval(clock);
    $("loading").classList.remove("active");
    $("submit").disabled = false;
  }
});

function showThumbnail(url, title) {
  const thumbnail = $("thumbnail");
  const placeholder = $("thumbnail-placeholder");
  thumbnail.onload = () => {
    thumbnail.hidden = false;
    placeholder.hidden = true;
  };
  thumbnail.onerror = () => {
    thumbnail.hidden = true;
    placeholder.hidden = false;
    thumbnail.removeAttribute("src");
  };
  thumbnail.alt = title ? `${title}封面` : "内容封面";
  if (!url) {
    thumbnail.onerror();
    return;
  }
  placeholder.hidden = false;
  thumbnail.hidden = true;
  thumbnail.src = url;
}

function render(data) {
  currentResult = data;
  const meta = data.metadata;
  showThumbnail(meta.thumbnail, meta.title);
  $("platform").textContent = meta.platform;
  $("video-title").textContent = meta.title;
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

  const plan = data.extraction_plan || {};
  const coverage = data.coverage || {};
  const structured = data.structured_data || {
    case_id: "unstructured", "内容主题": "未识别内容主题",
    "原子主张": [], "隐性观点": []
  };
  $("plan-badges").innerHTML = [
    [meta.content_type === "image_carousel" ? "images" : "video", meta.content_type === "image_carousel" ? "图文" : "视频"],
    ["scan-search", videoTypeLabels[plan.video_type] || plan.video_type || "未分类"],
    ["layers-3", plan.highest_cost_level || "—"],
    ["circle-check", coverageStatusLabels[coverage.status] || coverage.status || "未知"],
    ["fingerprint", structured.case_id || "unstructured"]
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

  const groups = [
    ["内容主题", [structured["内容主题"]].filter(Boolean)],
    ["原子主张", structured["原子主张"] || []],
    ["隐性观点", structured["隐性观点"] || []]
  ];
  $("structured-output").innerHTML = groups.map(([name, values]) =>
    `<section class="structured-group"><h3>${escapeHtml(name)}</h3>${
      values.length
        ? `<ul>${values.map(value => `<li>${escapeHtml(value)}</li>`).join("")}</ul>`
        : '<div class="structured-empty">暂无</div>'
    }</section>`
  ).join("");
  $("json-output").textContent = JSON.stringify(structured, null, 2);
  renderVerificationSafely(data.verification, structured);
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
  renderStructuredInput(data);
  $("workbench").hidden = false;
  $("result").classList.add("active");
  const verificationStatus = data.verification?.status;
  switchResultView(verificationStatus && verificationStatus !== "skipped" ? "verification" : "overview");
  refreshIcons();
  $("content-scroll").scrollTo({ top: 0, behavior: "smooth" });
}

function fullPipelineMilliseconds(data) {
  const extraction = Number(data.extraction_milliseconds) ||
    (data.timings || []).reduce((total, item) => total + Number(item.milliseconds || 0), 0);
  const verification = Number(data.verification?.timings?.total_seconds || 0) * 1000;
  return Number(data.full_pipeline_milliseconds) || Math.round(extraction + verification);
}

function renderStructuredInput(data) {
  const inputText = data.structured_input_text || data.full_source_text || "";
  const inputChars = Number(data.structured_input_chars) || inputText.length;
  const usedFallback = !data.structured_input_text && Boolean(data.full_source_text);
  const notes = [
    `实际输入 ${inputChars} 字符`,
    data.structured_input_truncated ? "已达到服务端输入上限，后续内容未发送" : "未截断",
    usedFallback ? "历史缓存：根据完整原文还原" : "服务端记录的实际结构化输入"
  ];
  $("llm-structured-input-meta").textContent = notes.join(" · ");
  $("llm-structured-input").textContent = inputText || "当前记录没有可展示的结构化模型输入";
}

function renderPipelineTimings(data) {
  const extractionMilliseconds = Number(data.extraction_milliseconds) ||
    (data.timings || []).reduce((total, item) => total + Number(item.milliseconds || 0), 0);
  const verificationMilliseconds = Math.round(Number(data.verification?.timings?.total_seconds || 0) * 1000);
  const totalMilliseconds = fullPipelineMilliseconds(data);
  const extractionTraceItems = (data.timings || []).map(item => ({
    phase: "信息提取", name: item.name, milliseconds: Number(item.milliseconds || 0), kind: "extraction"
  }));
  const verificationTraceItems = Object.entries(data.verification?.timings?.stages || {}).map(([name, seconds]) => ({
    phase: "信源核实", name: stageLabel(name), milliseconds: Math.round(Number(seconds || 0) * 1000), kind: "verification"
  }));
  const traceItems = [...extractionTraceItems, ...verificationTraceItems];
  const traceHtml = traceItems.map(item =>
    `<div class="trace-item trace-${item.kind}"><span>${escapeHtml(item.phase)} · ${escapeHtml(item.name)}</span><strong>${formatDuration(item.milliseconds)}</strong></div>`
  ).join("");
  const totalsHtml = `<div class="trace-item trace-total"><span>全流程 · 总时长</span><strong>${formatDuration(totalMilliseconds)}</strong></div>`;
  $("trace").innerHTML = traceHtml + totalsHtml;
  $("process-trace").innerHTML = traceHtml + totalsHtml;
  $("full-pipeline-summary").innerHTML = [
    ["信息提取", extractionMilliseconds],
    ["信源核实", verificationMilliseconds],
    ["全流程", totalMilliseconds]
  ].map(([label, milliseconds], index) => `<div class="pipeline-summary-item ${index === 2 ? "primary" : ""}"><span>${escapeHtml(label)}</span><strong>${formatDuration(milliseconds)}</strong></div>`).join("");
}

function renderVerificationSafely(verification, structured) {
  const requiredContainers = ["trust-hero", "claim-checks", "evidence-list", "trust-audit-body"];
  if (requiredContainers.some(id => !$(id))) {
    console.error("[MiMo Trust] 页面资源版本不一致，缺少信源核实容器");
    return;
  }
  try {
    renderVerification(verification, structured);
  } catch (error) {
    console.error("[MiMo Trust] 信源核实结果渲染失败", error);
    $("trust-hero").innerHTML = `<div class="trust-empty-state">
      <span class="verdict-mark verdict-error"><i data-lucide="triangle-alert" aria-hidden="true"></i></span>
      <div><p class="trust-kicker">结果展示失败</p><h2>核验数据已返回，但页面无法解析</h2>
      <button class="secondary-button" type="button" onclick="window.location.reload()"><i data-lucide="refresh-cw" aria-hidden="true"></i>刷新页面资源</button></div>
    </div>`;
    $("claim-checks").innerHTML = '<div class="history-empty">请刷新页面后重试</div>';
    $("evidence-list").innerHTML = '<div class="history-empty">请刷新页面后重试</div>';
    $("trust-audit-body").innerHTML = '<div class="history-empty">当前没有可展示的信源审计记录</div>';
  }
}

function renderVerification(verification, structured) {
  if (!verification || verification.status === "failed" || verification.status === "skipped") {
    const failed = verification?.status === "failed";
    const message = verification?.message || "当前结果尚未执行信源核实。";
    $("trust-hero").innerHTML = `<div class="trust-empty-state">
      <span class="verdict-mark ${failed ? "verdict-error" : "verdict-pending"}"><i data-lucide="${failed ? "triangle-alert" : "search-check"}" aria-hidden="true"></i></span>
      <div><p class="trust-kicker">${failed ? "核验未完成" : "等待核验"}</p><h2>${escapeHtml(message)}</h2>
      ${verification?.status !== "skipped" ? `<button class="secondary-button" type="button" onclick="retryVerification()"><i data-lucide="${failed ? "refresh-cw" : "search-check"}" aria-hidden="true"></i>${failed ? "重试信源核实" : "开始信源核实"}</button>` : ""}</div>
    </div>`;
    $("claim-checks").innerHTML = '<div class="history-empty">暂无逐项结论</div>';
    $("evidence-list").innerHTML = '<div class="history-empty">暂无引用证据</div>';
    $("trust-audit-body").innerHTML = '<div class="history-empty">当前没有可展示的信源审计记录</div>';
    refreshIcons();
    return;
  }

  const checks = verification.claim_checks || [];
  const evidence = verification.evidence_used || [];
  const evidenceById = Object.fromEntries(evidence.map(item => [item.id, item]));
  const verdictClass = verdictTone(verification.overall_verdict);
  $("trust-hero").innerHTML = `<div class="trust-verdict ${verdictClass}">
    <span class="verdict-mark"><i data-lucide="${verdictIcon(verification.overall_verdict)}" aria-hidden="true"></i></span>
    <div class="trust-verdict-copy"><p class="trust-kicker">综合判定</p><h2>${escapeHtml(verification.overall_verdict || "证据不足")}</h2>
      <p>${escapeHtml(verification.conclusion || "核验已完成。")}</p></div>
    <div class="trust-stats"><div><strong>${checks.length}</strong><span>项主张</span></div><div><strong>${Number(verification.evidence_reviewed_count || 0)}</strong><span>条已审阅</span></div><div><strong>${Number(verification.evidence_selected_count || 0)}</strong><span>条入选</span></div></div>
  </div>`;

  $("claim-checks").innerHTML = checks.map(check => {
    const sources = (check.source_ids || []).map(id => evidenceById[id]).filter(Boolean);
    return `<article class="claim-card">
      <div class="claim-card-head"><span class="claim-id">${escapeHtml(check.claim_id)}</span><span class="claim-category">${escapeHtml(check.category)}</span><span class="claim-verdict ${verdictTone(check.verdict)}">${escapeHtml(check.verdict)}</span></div>
      <h3>${escapeHtml(check.claim)}</h3><p>${escapeHtml(check.basis)}</p>
      ${sources.length ? `<div class="claim-sources">${sources.map(source => sourceLink(source, true)).join("")}</div>` : '<div class="claim-no-source">本项没有达到引用门槛的直接证据</div>'}
    </article>`;
  }).join("") || '<div class="history-empty">没有可展示的逐项结论</div>';

  const cited = [...new Set((verification.source_ids || []).map(id => evidenceById[id]).filter(Boolean))];
  $("evidence-list").innerHTML = cited.map(source => {
    const profile = source.evidence_profile || {};
    return `<article class="evidence-card"><div class="evidence-meta"><span>${escapeHtml(source.id)}</span><span>${escapeHtml(source.tier || "")}</span><span>${escapeHtml(profile.source_role || source.provider || "来源")}</span></div>
      <h3>${sourceLink(source, false)}</h3><p>${escapeHtml(source.snippet || "无摘要")}</p></article>`;
  }).join("") || '<div class="history-empty">最终报告未引用证据；请查看逐项结论中的证据缺口。</div>';

  const timing = verification.timings || {};
  const stages = timing.stages || {};
  const plan = verification.search_plan || {};
  const uncertainties = verification.uncertainties || [];
  $("trust-audit-body").innerHTML = `<div class="audit-metrics">
    ${Object.entries(stages).map(([name, seconds]) => `<div><span>${escapeHtml(stageLabel(name))}</span><strong>${Number(seconds).toFixed(2)}s</strong></div>`).join("")}
    <div><span>总耗时</span><strong>${Number(timing.total_seconds || 0).toFixed(2)}s</strong></div>
  </div>
  <div class="audit-block"><h3>检索计划</h3><p>${escapeHtml(plan.reasoning || "使用保底检索计划")}</p><ol>${(plan.web_queries || []).map(query => `<li>${escapeHtml(query)}</li>`).join("")}</ol></div>
  <div class="audit-block"><h3>不确定性</h3>${uncertainties.length ? `<ul>${uncertainties.map(item => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : "<p>无额外不确定性说明。</p>"}</div>
  <div class="audit-foot">案例 ${escapeHtml(verification.case_id)} · 运行 ${escapeHtml(verification.run_id)}</div>`;
}

async function retryVerification() {
  if (!currentResult?.structured_data) return;
  $("trust-hero").innerHTML = '<div class="trust-empty-state"><span class="spinner" aria-hidden="true"></span><div><p class="trust-kicker">正在重新核验</p><h2>检索并评估多源证据</h2></div></div>';
  try {
    const response = await fetch("/api/verify", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ structured_data: currentResult.structured_data, cache_key: currentCacheKey }) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "核验失败");
    currentResult.verification = data;
    renderVerification(data, currentResult.structured_data);
    if (currentCacheKey) loadHistory();
  } catch (error) {
    renderVerification({ status: "failed", message: error.message }, currentResult.structured_data);
  }
  refreshIcons();
}

function sourceLink(source, compact) {
  const url = /^https?:\/\//i.test(source.url || "") ? source.url : "";
  const label = compact ? `${source.id} · ${source.title || source.url}` : (source.title || source.url || source.id);
  return url ? `<a href="${escapeAttribute(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}<i data-lucide="external-link" aria-hidden="true"></i></a>` : `<span>${escapeHtml(label)}</span>`;
}

function verdictTone(verdict) {
  if (["属实", "基本属实"].includes(verdict)) return "verdict-positive";
  if (["虚假", "误导"].includes(verdict)) return "verdict-negative";
  if (["部分属实"].includes(verdict)) return "verdict-mixed";
  return "verdict-pending";
}

function verdictIcon(verdict) {
  if (["属实", "基本属实"].includes(verdict)) return "badge-check";
  if (["虚假", "误导"].includes(verdict)) return "badge-x";
  if (verdict === "部分属实") return "circle-dot-dashed";
  return "circle-help";
}

function stageLabel(name) {
  return ({ search_plan: "检索规划", retrieval: "多源检索", evidence_triage: "证据初筛", report_generation: "结论生成" })[name] || name;
}

function formatDuration(milliseconds) {
  const value = Math.max(0, Number(milliseconds) || 0);
  if (value < 1000) return `${Math.round(value)} ms`;
  if (value < 60000) return `${(value / 1000).toFixed(value < 10000 ? 2 : 1)}s`;
  const minutes = Math.floor(value / 60000);
  const seconds = Math.round((value % 60000) / 1000);
  return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
}

async function loadHistory() {
  try {
    const response = await fetch("/api/videos");
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "读取失败");
    if (!data.items.length) {
      historyByKey = {};
      $("history-list").innerHTML = '<div class="history-empty">暂无提取记录</div>';
      return;
    }
    historyByKey = Object.fromEntries(data.items.map(item => [item.cache_key, item.result]));
    $("history-list").innerHTML = data.items.map(item => {
      const result = item.result;
      const date = new Date(item.created_at).toLocaleString("zh-CN");
      const coverage = result.coverage || {};
      const verdict = result.verification?.overall_verdict;
      return `<div class="history-item">
        <div>
          <p class="history-title">${escapeHtml(result.metadata.title)}</p>
          <div class="history-meta">${escapeHtml(result.metadata.platform)} / ${escapeHtml(strategyLabels[result.strategy] || result.strategy)} / ${escapeHtml(coverageStatusLabels[coverage.status] || coverage.status || "未知")}${verdict ? ` / 核验：${escapeHtml(verdict)}` : " / 待核验"} / ${escapeHtml((result.structured_data || {}).case_id || "unstructured")} / ${escapeHtml(date)}${item.expired ? " / 已过期" : ""}</div>
        </div>
        <div class="history-actions">
          <button type="button" onclick="viewStored('${item.cache_key}')"><i data-lucide="eye" aria-hidden="true"></i>查看</button>
          <button type="button" onclick="reanalyze('${item.cache_key}', '${encodeURIComponent(result.metadata.webpage_url)}')"><i data-lucide="rotate-ccw" aria-hidden="true"></i>重跑</button>
          <button class="delete-button" type="button" aria-label="删除记录" title="删除" onclick="deleteVideo('${item.cache_key}')"><i data-lucide="trash-2" aria-hidden="true"></i></button>
        </div>
      </div>`;
    }).join("");
    refreshIcons();
  } catch (error) {
    $("history-list").innerHTML = `<div class="history-empty">${escapeHtml(error.message)}</div>`;
  }
}

async function deleteVideo(cacheKey) {
  if (!window.confirm("删除这条记录？")) return;
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
  if (!window.confirm("清空全部缓存？")) return;
  const response = await fetch("/api/videos", { method: "DELETE" });
  if (response.ok) {
    $("result").classList.remove("active");
    $("workbench").hidden = true;
    loadHistory();
  }
});

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

function renderCleanedArticle(article) {
  if (!article.trim()) {
    $("cleaned-article").innerHTML = '<div class="article-empty">暂无可整理的全文</div>';
    return;
  }
  const sections = article.split(/\n{2,}/).map(item => item.trim()).filter(Boolean);
  $("cleaned-article").innerHTML = sections.map(section => {
    const lines = section.split("\n").map(item => item.trim()).filter(Boolean);
    const hasHeading = lines.length > 1 && lines[0].length <= 20 && !/[。！？!?]$/.test(lines[0]);
    const heading = hasHeading ? `<h3>${escapeHtml(lines.shift())}</h3>` : "";
    const paragraphs = lines.length ? lines : [section];
    return `<section class="article-section">${heading}${paragraphs.map(
      paragraph => `<p>${escapeHtml(paragraph)}</p>`
    ).join("")}</section>`;
  }).join("");
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
