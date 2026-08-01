from __future__ import annotations

import html
import json
import re
from collections import Counter
from typing import Any


TIMESTAMP_RE = re.compile(
    r"^(?:\d{2}:)?\d{2}:\d{2}[.,]\d{3}\s+-->\s+.+$", re.MULTILINE
)
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"[ \t]+")
SENTENCE_RE = re.compile(r"(?<=[。！？!?；;])|\n+")


def _dedupe_lines(lines: list[str]) -> str:
    result: list[str] = []
    previous = ""
    for line in lines:
        line = SPACE_RE.sub(" ", html.unescape(TAG_RE.sub("", line))).strip()
        if not line or line in {"WEBVTT", "Kind: captions", "Language: zh"}:
            continue
        if line.isdigit() or "-->" in line:
            continue
        if line == previous:
            continue
        if previous and line.startswith(previous):
            result[-1] = line
        else:
            result.append(line)
        previous = result[-1]
    return "\n".join(result)


def parse_subtitle_document(payload: str, extension: str) -> tuple[str, float]:
    extension = extension.lower()
    if extension in {"json", "json3"}:
        data = json.loads(payload)
        if isinstance(data, dict) and isinstance(data.get("body"), list):
            body = data["body"]
            text = _dedupe_lines([str(item.get("content", "")) for item in body])
            end = max(
                (float(item.get("to") or item.get("from") or 0) for item in body),
                default=0,
            )
            return text, end
        events = data.get("events", []) if isinstance(data, dict) else []
        text = _dedupe_lines(
            [
                "".join(segment.get("utf8", "") for segment in event.get("segs", []))
                for event in events
            ]
        )
        end = max(
            (
                (float(event.get("tStartMs") or 0) + float(event.get("dDurationMs") or 0))
                / 1000
                for event in events
            ),
            default=0,
        )
        return text, end

    cleaned = TIMESTAMP_RE.sub("", payload)
    ends = re.findall(
        r"-->\s*(?:(\d{2}):)?(\d{2}):(\d{2})[.,](\d{3})", payload
    )
    end = 0.0
    for hours, minutes, seconds, millis in ends:
        end = max(
            end,
            int(hours or 0) * 3600
            + int(minutes) * 60
            + int(seconds)
            + int(millis) / 1000,
        )
    return _dedupe_lines(cleaned.splitlines()), end


def parse_subtitle(payload: str, extension: str) -> str:
    return parse_subtitle_document(payload, extension)[0]


def choose_subtitle(info: dict[str, Any]) -> dict[str, Any] | None:
    language_priority = (
        "zh-Hans",
        "zh-CN",
        "zh",
        "zh-Hant",
        "zh-TW",
        "en",
        "en-US",
        "en-GB",
    )
    sources = [
        (info.get("subtitles") or {}, False),
        (info.get("automatic_captions") or {}, True),
    ]
    for source, automatic in sources:
        if not source:
            continue
        languages = sorted(
            source,
            key=lambda lang: (
                language_priority.index(lang)
                if lang in language_priority
                else len(language_priority)
            ),
        )
        for language in languages:
            formats = source.get(language) or []
            for preferred_ext in ("json3", "json", "vtt", "srt"):
                selected = next(
                    (
                        item
                        for item in formats
                        if item.get("ext", "").lower() == preferred_ext
                        and item.get("url")
                    ),
                    None,
                )
                if selected:
                    return {
                        **selected,
                        "language": language,
                        "automatic": automatic,
                    }
    return None


def local_extractive_summary(
    transcript: str, title: str, description: str = ""
) -> dict[str, Any]:
    text = transcript.strip() or description.strip()
    if not text:
        return {
            "summary": f"视频标题为《{title}》，但未取得可用于概括的字幕或正文。",
            "key_points": [],
            "topics": [title] if title else [],
        }

    raw_sentences = [item.strip() for item in SENTENCE_RE.split(text) if item.strip()]
    sentences: list[str] = []
    buffer = ""
    for item in raw_sentences:
        buffer = f"{buffer}，{item}".strip("，") if buffer else item
        if len(buffer) >= 42 or re.search(r"[。！？!?]$", item):
            sentences.append(buffer)
            buffer = ""
    if buffer:
        sentences.append(buffer)
    sentences = [item for item in sentences if len(item) >= 15]
    if not sentences:
        sentences = [text[:300]]

    normalized = re.sub(r"\s+", "", text)
    stop_terms = {
        "一个",
        "这个",
        "我们",
        "大家",
        "现在",
        "就是",
        "可以",
        "那么",
        "时候",
        "这里",
        "这样",
        "什么",
    }
    grams = Counter(
        normalized[index : index + 2]
        for index in range(max(0, len(normalized) - 1))
        if not re.search(r"[\W\d_]", normalized[index : index + 2])
        and normalized[index : index + 2] not in stop_terms
    )
    title_terms = {
        title[index : index + 2]
        for index in range(max(0, len(title) - 1))
        if not re.search(r"[\W\d_]", title[index : index + 2])
    }
    scored: list[tuple[float, int, str]] = []
    for index, sentence in enumerate(sentences[:120]):
        compact = re.sub(r"\s+", "", sentence)
        unique_terms = {
            compact[pos : pos + 2] for pos in range(max(0, len(compact) - 1))
        }
        score = sum(
            grams.get(compact[pos : pos + 2], 0)
            for pos in range(max(0, len(compact) - 1))
        ) / max(len(compact) ** 0.7, 1)
        score += len(title_terms.intersection(unique_terms)) * 3
        score += 5 / (index + 3)
        if re.search(r"介绍|讲解|解释|展示|讨论|总结|核心|主要", sentence):
            score += 4
        scored.append((score, index, sentence))

    selected = sorted(sorted(scored, reverse=True)[:4], key=lambda item: item[1])
    key_points = [item[2][:180] for item in selected]
    topics = [term for term, _ in grams.most_common(8) if len(term) == 2][:5]
    summary = "；".join(key_points[:2])
    return {
        "summary": summary[:360],
        "key_points": key_points,
        "topics": topics,
    }
