from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import httpx

from app.config import settings
from app.models import StructuredInformation


class MimoError(RuntimeError):
    pass


STRUCTURED_INFORMATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "case_id": {
            "type": "string",
            "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$",
        },
        "内容主题": {"type": "string", "minLength": 1, "maxLength": 200},
        "原子主张": {
            "type": "array",
            "items": {
                "type": "string",
                "minLength": 8,
                "maxLength": 300,
                "pattern": r".*[\u4e00-\u9fff].*",
            },
            "uniqueItems": True,
        },
        "隐性观点": {
            "type": "array",
            "items": {
                "type": "string",
                "minLength": 8,
                "maxLength": 300,
                "pattern": r".*[\u4e00-\u9fff].*",
            },
            "uniqueItems": True,
        },
    },
    "required": ["case_id", "内容主题", "原子主张", "隐性观点"],
    "additionalProperties": False,
}


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.mimo_api_key}",
        "api-key": settings.mimo_api_key,
        "Content-Type": "application/json",
    }


def _parse_json_content(content: str) -> dict[str, Any]:
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", content, re.DOTALL)
    candidate = fenced.group(1) if fenced else content
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as first_error:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except json.JSONDecodeError as second_error:
                raise MimoError(
                    "MiMo 返回了语法损坏的 JSON"
                ) from second_error
        raise MimoError("MiMo 返回了无法解析的摘要格式") from first_error


def _structured_response_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "mimo_trust_information_v4",
            "strict": True,
            "schema": STRUCTURED_INFORMATION_SCHEMA,
        },
    }


def _choice_content(data: dict[str, Any]) -> tuple[str, str]:
    try:
        choice = data["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise MimoError("MiMo 返回缺少 choices/message 字段") from exc
    content = str(
        message.get("content") or message.get("reasoning_content") or ""
    ).strip()
    if not content:
        raise MimoError("MiMo 返回了空内容")
    return content, str(choice.get("finish_reason") or "")


def _validate_structured_result(result: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(result)
    topic = str(normalized.get("内容主题") or "").strip()
    raw_case_id = str(normalized.get("case_id") or "").strip().lower()
    normalized_case_id = re.sub(r"[^a-z0-9]+", "-", raw_case_id).strip("-")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", normalized_case_id):
        identity_seed = topic or raw_case_id or json.dumps(
            result, ensure_ascii=False, sort_keys=True
        )
        digest = hashlib.sha256(identity_seed.encode("utf-8")).hexdigest()[:12]
        normalized_case_id = f"case-{digest}"
    normalized["case_id"] = normalized_case_id

    for field_name in ("原子主张", "隐性观点"):
        if field_name not in normalized or normalized[field_name] is None:
            normalized[field_name] = []
        elif isinstance(normalized[field_name], str):
            text = normalized[field_name].strip()
            normalized[field_name] = [text] if text else []

    try:
        validated = StructuredInformation.model_validate(normalized)
    except Exception as exc:
        if hasattr(exc, "errors"):
            errors = [
                {
                    "field": ".".join(str(part) for part in item.get("loc", [])),
                    "type": item.get("type"),
                    "message": item.get("msg"),
                }
                for item in exc.errors(include_url=False)
            ]
            detail = json.dumps(errors, ensure_ascii=False)
        else:
            detail = str(exc)
        raise MimoError(f"结构化信息未通过本地 Schema 校验：{detail}") from exc
    return validated.model_dump(by_alias=True)


def _structured_quality_issues(result: dict[str, Any]) -> list[str]:
    """Detect fragmentation risk without imposing a result-count limit."""
    issues: list[str] = []
    anaphora = re.compile(r"^(?:这|这些|该|其|上述|前述|后者|其中)")
    for field_name in ("原子主张",):
        items = [
            re.sub(r"\s+", "", str(item))
            for item in result.get(field_name, [])
            if str(item).strip()
        ]
        if any(anaphora.match(item) for item in items):
            issues.append(f"{field_name}存在依赖上文的代词，条目不自足")
        if len(items) >= 4:
            short_items = [item for item in items if len(item) <= 36]
            leading_counts: dict[str, int] = {}
            for item in short_items:
                leading = item[:2]
                leading_counts[leading] = leading_counts.get(leading, 0) + 1
            if (
                len(short_items) >= 3
                and max(leading_counts.values(), default=0) >= 3
            ):
                issues.append(f"{field_name}疑似把同一主体事件拆成短句流水账")
            bigram_sets = [
                {item[index : index + 2] for index in range(len(item) - 1)}
                for item in items
            ]
            if any(
                len(left & right) / max(1, len(left | right)) >= 0.12
                for index, left in enumerate(bigram_sets)
                for right in bigram_sets[index + 1 :]
            ):
                issues.append(f"{field_name}存在高度重叠条目，可能属于同一核验任务")
        if field_name == "原子主张":
            numeric_owners: dict[str, int] = {}
            duplicated_numbers: set[str] = set()
            for index, item in enumerate(items):
                numbers = set(
                    re.findall(
                        r"\d+(?:\.\d+)?(?:万|亿|%|美元|元|名|个)?",
                        item,
                    )
                )
                for number in numbers:
                    if number in numeric_owners and numeric_owners[number] != index:
                        duplicated_numbers.add(number)
                    numeric_owners[number] = index
            if duplicated_numbers:
                issues.append("原子主张跨条重复同一关键数字，可能存在错误归并")
    for claim in result.get("原子主张", []):
        text = str(claim)
        describes_speaker = re.search(r"说话者|发布者|作者|视频.{0,8}(?:博主|主播|创作者)", text)
        describes_pragmatics = re.search(
            r"反讽|真实立场|字面(?:赞扬|崇拜|褒义)|语气|态度|隐含|"
            r"(?:表达|表示).{0,12}(?:质疑|讽刺|崇拜|赞扬)",
            text,
        )
        if describes_speaker and describes_pragmatics:
            issues.append(
                "原子主张包含作者语气、反讽或真实立场判断，应移入隐性观点"
            )
            break
    return issues


def _merge_usage(*usages: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        values = [
            int(usage.get(key) or 0)
            for usage in usages
            if isinstance(usage, dict)
        ]
        if values:
            merged[key] = sum(values)
    return merged


def _recommended_claim_density(
    source_text: str, metadata: dict[str, Any]
) -> int:
    """Estimate a soft salience budget; this is never used to truncate output."""
    meaningful_chars = len(re.sub(r"\s+", "", source_text))
    duration_seconds = float(metadata.get("duration_seconds") or 0)
    image_count = int(metadata.get("image_count") or 0)
    return max(
        2,
        (meaningful_chars + 2499) // 2500,
        (int(duration_seconds) + 299) // 300,
        (image_count + 2) // 3,
    )


async def _repair_structured_json(content: str, reason: str) -> dict[str, Any]:
    repair_payload = {
        "model": settings.summary_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是结构化 JSON 修复器。只修复语法、补齐结构和删除未完成的末尾项；"
                    "不得增加候选文本中不存在的新事实。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"失败原因：{reason or 'JSON 语法或结构错误'}。\n"
                    "请把下面候选结果修复为 mimo_trust_information_v4。"
                    "若文本被截断，只保留已经完整出现的信息：\n"
                    f"{content[:24_000]}"
                ),
            },
        ],
        "temperature": 0,
        "max_completion_tokens": 4000,
        "thinking": {"type": "disabled"},
        "response_format": _structured_response_format(),
    }
    repaired = await _completion(repair_payload, timeout=180)
    repaired_content, finish_reason = _choice_content(repaired)
    if finish_reason == "length":
        raise MimoError("JSON 修复结果再次被截断")
    return _validate_structured_result(_parse_json_content(repaired_content))


async def _recompress_structured_result(
    source_text: str,
    candidate: dict[str, Any],
    issues: list[str],
    recommended_density: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = {
        "model": settings.summary_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是核验任务语义重压缩器。修复碎片化，但不得删除独立的"
                    "关键核验对象，也不得增加原文没有的信息。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "当前结果存在以下质量风险："
                    f"{'；'.join(issues)}。\n"
                    f"当前内容的建议核心主张信息密度为 {recommended_density} 条；"
                    "这不是上限，只有新增条目带来独立核心事件或不同核验路径时"
                    "才超过。\n"
                    "请重新按同一主体、事件链和证据检索任务聚类。"
                    "同一资金链的筹集方式、金额、用途和直接结果应合成自足命题；"
                    "只有不同主体、不同核心事件或不同核验路径才拆分。"
                    "动机、问题背景和常识说明应删除，不要作为原子主张，"
                    "除非它本身就是内容标题和结论要求核验的中心事实。"
                    "同一长期行动的实施方式、累计成果、资金用途和直接受益结果"
                    "属于一个核验事件簇，必须合成一条，不得各列一条。"
                    "不得为了压缩改变事实归属：长期累计金额不能挂到某次具体活动，"
                    "同一数字或结果不得在多条主张中重复。"
                    "必须保留原结果中对反讽、反问、夸张和字面褒义与真实立场相反的"
                    "语用判断；不得把反讽式赞扬改写成作者真实赞扬。"
                    "描述作者态度、语气、反讽、质疑意图或真实立场的条目必须放入"
                    "隐性观点，不得留在原子主张。"
                    "具体活动只保留该场景明确发生的行为和目的；长期行动另行合并"
                    "主要实施方式、累计成果、用途与直接结果。"
                    "例如碎片“长期通过B累计X”和“X用于Y”必须改写为"
                    "“长期通过B累计X，并将X用于Y”，而不是保留两条。"
                    "使用最少的完整命题覆盖全部关键核验对象，不设固定条数。"
                    "每条必须脱离数组上下文也能独立理解，禁止以“这笔、这些、该、"
                    "其、上述”等代词指代另一条。修复后不得保留错误原因所描述的"
                    "短句流水账。\n"
                    f"候选结果：{json.dumps(candidate, ensure_ascii=False)}\n"
                    f"原文证据：{source_text[:24_000]}"
                ),
            },
        ],
        "temperature": 0,
        "max_completion_tokens": 4000,
        "thinking": {"type": "disabled"},
        "response_format": _structured_response_format(),
    }
    data = await _completion(payload, timeout=180)
    content, finish_reason = _choice_content(data)
    if finish_reason == "length":
        raise MimoError("语义重压缩结果达到 token 上限")
    result = _validate_structured_result(_parse_json_content(content))
    return result, data.get("usage") or {}


async def _completion(payload: dict[str, Any], timeout: float = 120) -> dict[str, Any]:
    if not settings.mimo_api_key:
        raise MimoError("未配置 MIMO_API_KEY")
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{settings.mimo_base_url}/chat/completions",
            headers=_headers(),
            json=payload,
        )
    if response.is_error:
        detail = response.text[:500]
        raise MimoError(f"MiMo API 请求失败（{response.status_code}）：{detail}")
    return response.json()


async def structure_information(
    source_text: str, metadata: dict[str, Any]
) -> dict[str, Any]:
    recommended_density = _recommended_claim_density(source_text, metadata)
    prompt = f"""
将原文压缩为供下游信源核实使用的 structured-information-v4。忠于输入，
不补充外部知识，不判断真假。

建议核心原子主张信息密度为 {recommended_density} 条；这不是上限。如果存在
额外条目，只有当删除它会遗漏独立核心事件、改变核心结论或需要不同核验路径时
才保留；否则必须与同一事件合并或删除。

先执行语用核对，再做事实结构化：
- 识别反讽、反问、夸张、戏仿，以及字面褒义与真实立场相反的表达；
- 必须综合标题、话题标签、体裁标签、前后文矛盾、法律或安全性质疑、反问句和结尾照应；
- 若存在反讽，隐性观点必须表达作者真正批评或质疑的对象，并在同一句中简述判断线索；
- 描述作者态度、语气、反讽、质疑意图或真实立场的内容只能放入隐性观点；
- 如果文本线索不足以确定真实立场，必须明确表述不确定性，不得编造确定态度；
- 反讽式话语不能作为事实性原子主张重复输出，但话语中被质疑的法律、数量、时间和事件命题仍应分别提取。

先按事件和证据检索路径聚类，再按重要性排序：
- 原子主张是最小充分、自足的核验任务，不是单谓语。同一长期行动的实施方式、
  累计金额、资金用途和直接受益结果合并；特定时间地点发生且需要独立信源的
  事件另列。不得为了满足建议密度改变时间、主体或因果归属：长期累计结果不能
  挂到某次具体活动，同一个数字或结果不得跨条重复。动机背景、普通知识和重复
  步骤不列为原子主张。
- 所有值得外部核验的事实性陈述都统一放入原子主张，不另设新闻事实或背景事实字段。
  仅用于铺垫、且不影响核心结论的背景应删除；不得输出 Schema 之外的字段。
- 隐性观点只保留有充分依据且影响叙事导向的核心潜台词；同义合并，无则 []。
- 两类数组没有 Schema 条数上限。每项8至300字、包含中文、语义完整；排除
  账号ID、界面元素、时间戳、OCR碎片和残句。

时间归因示例仅用于理解规则：
错误：["某人在一次具体活动中做了A并最终取得跨多年累计成果X",
"某人长期通过B累计X","X被用于Y"]
正确：["某人在一次具体活动中为总体目标做了A",
"某人长期通过B累计取得X，并将X用于Y"]
长期事件簇不能遗漏原文反复强调的关键实施方式。

输出必须严格符合服务端提供的 JSON Schema，目标结构为：
{{
  "case_id":"english-kebab-case",
  "内容主题":"中文主题",
  "原子主张":["完整主张"],
  "隐性观点":["内容隐含的立场或价值判断"]
}}

标题：{metadata.get("title", "")}
作者：{metadata.get("uploader", "")}
内容类型：{"图文轮播" if metadata.get("content_type") == "image_carousel" else "视频"}
简介：{metadata.get("description", "")[:1500]}
完整原文信息（可能包含字幕、OCR、画面观察和发布文案）：
{source_text[:settings.max_transcript_chars]}
""".strip()
    payload = {
        "model": settings.summary_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是 MiMo Trust 的信息结构化模块。"
                    "只转换输入内容，不执行真假判断。"
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_completion_tokens": 4000,
        "thinking": {"type": "disabled"},
        "response_format": _structured_response_format(),
    }
    data = await _completion(payload)
    content, finish_reason = _choice_content(data)
    primary_usage = data.get("usage") or {}
    try:
        if finish_reason == "length":
            raise MimoError("结构化信息输出达到 token 上限并被截断")
        result = _validate_structured_result(_parse_json_content(content))
    except MimoError as exc:
        result = await _repair_structured_json(content, str(exc))
    quality_issues = _structured_quality_issues(result)
    if quality_issues:
        try:
            recompressed, recompress_usage = await _recompress_structured_result(
                source_text, result, quality_issues, recommended_density
            )
            primary_usage = _merge_usage(primary_usage, recompress_usage)
            if len(_structured_quality_issues(recompressed)) < len(quality_issues):
                result = recompressed
        except MimoError:
            pass
    result["_usage"] = primary_usage
    return result


async def analyze_keyframes(
    frames: list[tuple[int, float, Path]],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Run OCR and grounded visual observation over adaptive keyframes."""
    prompt = """
逐张分析按时间顺序提供的视频关键帧。执行：
1. 原样提取有事实意义的屏幕文字（OCR），保留数字、日期、机构、地点；
2. 描述画面中直接可观察的事件，不推测地点、时间、身份或因果；
3. 判断帧主要类型：scene_change、periodic、first_frame 或 unknown；
4. 相邻帧文字相同可以保留，但不要凭空补全被遮挡文字。

输出纯 JSON：
{"frames":[{"frame_index":1,"timestamp_seconds":0,
"ocr_text":["屏幕原文"],"visual_observations":["直接观察"],
"frame_type":"first_frame"}],
"summary":"关键帧视觉概括","coverage_note":"OCR和画面覆盖说明"}
""".strip()
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for index, timestamp, path in frames:
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        mime = {
            ".png": "image/png",
            ".webp": "image/webp",
        }.get(path.suffix.lower(), "image/jpeg")
        content.extend(
            [
                {
                    "type": "text",
                    "text": f"关键帧 {index}，时间 {timestamp:.2f} 秒：",
                },
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{encoded}"},
                },
            ]
        )
    payload = {
        "model": settings.summary_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是 MiMo Trust 的关键帧 OCR 与视觉证据提取模块。"
                    "只报告可直接观察的信息。"
                ),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"视频标题：{metadata.get('title', '')}",
                    },
                    *content,
                ],
            },
        ],
        "temperature": 0.1,
        "max_completion_tokens": 2400,
        "thinking": {"type": "disabled"},
    }
    data = await _completion(payload, timeout=240)
    result = _parse_json_content(data["choices"][0]["message"]["content"])
    result["_usage"] = data.get("usage") or {}
    return result


async def transcribe_audio(audio_path: Path) -> dict[str, Any]:
    mime = "audio/mpeg" if audio_path.suffix.lower() == ".mp3" else "audio/wav"
    encoded = base64.b64encode(audio_path.read_bytes()).decode("ascii")
    payload = {
        "model": settings.asr_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {"data": f"data:{mime};base64,{encoded}"},
                    }
                ],
            }
        ],
        "asr_options": {"language": "auto"},
        "stream": False,
        "max_completion_tokens": 2000,
    }
    data = await _completion(payload, timeout=240)
    choice = data["choices"][0]
    usage = data.get("usage") or {}
    return {
        "text": choice["message"]["content"].strip(),
        "finish_reason": choice.get("finish_reason"),
        "processed_seconds": float(usage.get("seconds") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
    }


async def summarize_video(
    media_url_or_path: str | Path,
    metadata: dict[str, Any],
    fps: float = 0.2,
) -> dict[str, Any]:
    prompt = """
分析视频画面与屏幕文字，为后续信源核实提取视觉信息。
不要判断真假，不要用画面推测未出现的事实。输出纯 JSON：
{"summary":"视觉内容概括","key_points":["画面事件"],"topics":["主题词"],"on_screen_text":["关键屏幕文字"],"coverage_note":"视觉覆盖说明"}
""".strip()
    if isinstance(media_url_or_path, Path):
        raw = media_url_or_path.read_bytes()
        media_value = f"data:video/mp4;base64,{base64.b64encode(raw).decode('ascii')}"
    else:
        media_value = media_url_or_path
    payload = {
        "model": settings.summary_model,
        "messages": [
            {
                "role": "system",
                "content": "你是 MiMo Trust 的全模态视频内容概括模块。只概括，不核验。",
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "video_url",
                        "video_url": {"url": media_value},
                        "fps": fps,
                        "media_resolution": "default",
                    },
                    {
                        "type": "text",
                        "text": f"{prompt}\n视频标题：{metadata.get('title', '')}",
                    },
                ],
            },
        ],
        "temperature": 0.2,
        "max_completion_tokens": 700,
        "thinking": {"type": "disabled"},
    }
    data = await _completion(payload, timeout=240)
    content = data["choices"][0]["message"]["content"]
    result = _parse_json_content(content)
    result["_usage"] = data.get("usage") or {}
    return result
