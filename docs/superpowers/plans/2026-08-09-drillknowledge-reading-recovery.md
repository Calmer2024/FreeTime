# DrillKnowledge Reading Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover complete Markdown article generation when MiMo aborts a complex structured response, and make the remaining fallback state truthful.

**Architecture:** Reduce the first LLM boundary to a flat four-field reading-result Schema. Treat every non-`stop` finish reason as incomplete, retry once with a schema-aligned instruction, then let the existing pipeline preserve source text with an explicit degradation message.

**Tech Stack:** Python 3.11+, FastAPI/Pydantic, httpx, pytest/pytest-asyncio.

## Global Constraints

- Preserve the existing `AnalyzeResponse` and frontend contracts.
- Keep local resource extraction and the independent opinion assessment request.
- Do not publish, package, or modify the installed application.

---

### Task 1: Complete Reading Result Boundary

**Files:**
- Modify: `apps/media-extractor/app/mimo.py`
- Test: `apps/media-extractor/tests/test_transcript.py`

**Interfaces:**
- Consumes: `compose_readable_result(source_text: str, metadata: dict[str, Any]) -> dict[str, Any]`
- Produces: the same result mapping, populated with `summary`, `key_points`, `topics`, `article`, optional later `opinion_assessment`, and `_usage`.

- [ ] **Step 1: Write failing tests**

Add async tests whose fake `_completion` returns a complete real-shaped response with `finish_reason="abort"` and partial content on the first call, then a valid four-field JSON response with `finish_reason="stop"`. Assert two calls and the recovered article. Add a consecutive-abort test asserting `MimoError` contains `abort`. Inspect the captured first payload and assert its Schema properties are exactly `summary`, `key_points`, `topics`, and `article`.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest apps/media-extractor/tests/test_transcript.py -k "reading_result" -q`

Expected: FAIL because the current payload contains `resources` and `opinion_assessment`, and because `abort` is parsed instead of retried.

- [ ] **Step 3: Implement the minimal boundary**

Create a flat reading Schema, align the prompt field order, and add one internal attempt helper that accepts only `finish_reason="stop"`. Retry once for incomplete or malformed responses. Preserve the separate opinion assessment call and usage aggregation.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `python -m pytest apps/media-extractor/tests/test_transcript.py -k "reading_result" -q`

Expected: all selected tests pass.

### Task 2: Truthful Pipeline Degradation

**Files:**
- Modify: `apps/media-extractor/app/pipeline.py`
- Test: `apps/media-extractor/tests/test_transcript.py`

**Interfaces:**
- Consumes: `MimoError` from `compose_readable_result`.
- Produces: unchanged `AnalyzeResponse`, with `coverage.status="needs_review"`, preserved source article, and an explicit fallback note only when both attempts fail.

- [ ] **Step 1: Write the failing test**

Exercise the real pipeline with external extraction stages replaced by deterministic fixtures and `compose_readable_result` raising `MimoError("MiMo 生成未完成（abort）")`. Assert the note contains “全文整理失败，当前展示” and does not contain “已整理为可阅读文章”.

- [ ] **Step 2: Run the test and verify RED**

Run: `python -m pytest apps/media-extractor/tests/test_transcript.py -k "reading_degraded" -q`

Expected: FAIL because the existing note still says the content was organized as a readable article.

- [ ] **Step 3: Implement the minimal copy change**

Build the normal success sentence only when `reading_degraded` is false; otherwise state that extraction succeeded but LLM organization failed and the current article is the locally cleaned ASR/source text.

- [ ] **Step 4: Run focused and full verification**

Run: `python -m pytest apps/media-extractor/tests/test_transcript.py -q`

Run: `python -m pytest apps/media-extractor/tests -q`

Expected: zero failures.

### Task 3: Isolated Real-Link Verification

**Files:**
- No production file changes.

**Interfaces:**
- Consumes: `analyze("https://www.douyin.com/video/7641773828838772654", "auto")` under an isolated `FREETIME_DATA_DIR`.
- Produces: diagnostic output only.

- [ ] **Step 1: Run the actual chain**

Use the repository virtual environment, an isolated diagnostic data directory, and the existing read-only Douyin cookie file. Print only coverage status, finish symptoms, summary/key-point counts, article length, Markdown headings, and timings.

- [ ] **Step 2: Verify acceptance criteria**

Confirm coverage is not `needs_review`, summary and key points are populated, and the article begins with `#` and contains at least two `##` headings. If an external transient failure remains after the retry, report it accurately without claiming the functional fix is verified.
