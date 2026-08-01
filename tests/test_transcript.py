import json
import asyncio
import re
from pathlib import Path

from app.cache import ResultCache
from app.mimo import (
    MimoError,
    STRUCTURED_INFORMATION_SCHEMA,
    _parse_json_content,
    _recommended_claim_density,
    _structured_quality_issues,
    _validate_structured_result,
    structure_information,
)
from app.security import (
    UnsafeUrlError,
    canonicalize_video_url,
    extract_video_url,
    resolve_video_input,
    validate_video_url,
)
from app.models import KeyframeEvidence, StructuredInformation
from app.pipeline import (
    AudioChunk,
    _classify_video,
    _clean_source_article,
    _estimate_visual_cost_cny,
    _has_usable_spoken_text,
    _local_structured_information,
    _needs_visual_fallback,
    _parse_douyin_note_html,
    _prefer_bilibili_backup_urls,
    _structured_information_from_model_result,
    _structured_reading_result,
    _transcript_retention_percent,
    _transcribe_chunks,
)
from app.transcript import (
    choose_subtitle,
    local_extractive_summary,
    parse_subtitle,
    parse_subtitle_document,
)


def test_parse_youtube_json3() -> None:
    payload = json.dumps(
        {"events": [{"segs": [{"utf8": "第一句。"}]}, {"segs": [{"utf8": "第二句。"}]}]}
    )
    assert parse_subtitle(payload, "json3") == "第一句。\n第二句。"


def test_clean_source_article_removes_transport_noise_and_duplicates() -> None:
    source = """[发布上下文]
标题：测试标题
作者：测试作者
简介：这是一段发布说明。

[语音/字幕]
[00:00:01] 第一条完整陈述。
[00:00:04] 第一条完整陈述。
[00:00:08] 第二条完整陈述。

[自适应关键帧 OCR 与观察]
[图片 1][屏幕文字] 画面中的关键文字。
[图片 2][屏幕文字] 画面中的关键文字。
"""
    article = _clean_source_article(source, "测试标题")

    assert "标题：" not in article
    assert "作者：" not in article
    assert "00:00" not in article
    assert "[图片" not in article
    assert "[屏幕文字]" not in article
    assert article.count("第一条完整陈述") == 1
    assert article.count("画面中的关键文字") == 1
    assert "发布内容" in article
    assert "口播与字幕" in article


def test_clean_source_article_preserves_long_continuous_chinese_text() -> None:
    sentences = [
        f"第{index}部分保留标记，" + "这是需要完整保留的连续中文正文" * 12 + "。"
        for index in range(1, 9)
    ]
    source = "[语音/字幕]\n[00:00:00-00:05:00] " + "".join(sentences)

    article = _clean_source_article(source, "长文本测试")

    for index in range(1, 9):
        assert f"第{index}部分保留标记" in article
    compact_article = re.sub(r"\s+", "", article)
    assert "".join(sentences) in compact_article
    assert _transcript_retention_percent("".join(sentences), article) == 100


def test_empty_asr_chunk_does_not_report_full_audio_coverage(monkeypatch) -> None:
    async def fake_transcribe_audio(path: Path):
        return {
            "text": "",
            "finish_reason": "stop",
            "processed_seconds": 75,
        }

    monkeypatch.setattr("app.pipeline.transcribe_audio", fake_transcribe_audio)
    transcript, coverage, missing, processed = asyncio.run(
        _transcribe_chunks(
            [AudioChunk(path=Path("empty.mp3"), start=0, end=75)],
            duration=75,
        )
    )

    assert transcript == ""
    assert coverage == 0
    assert processed == 0
    assert missing == ["00:00:00-00:01:15"]


def test_structured_reading_result_normalizes_summary_punctuation() -> None:
    structured = StructuredInformation(
        case_id="punctuation-case",
        content_topic="这是内容主题。",
        atomic_claims=[
            "敌敌畏经皮肤吸收可导致人体有机磷中毒。",
            "某项法规禁止在食品中使用该化学成分！",
        ],
        news_facts=[],
        implicit_opinions=[],
    )

    summary, _, _ = _structured_reading_result(structured)

    assert summary == (
        "这是内容主题。核心主张：敌敌畏经皮肤吸收可导致人体有机磷中毒；"
        "某项法规禁止在食品中使用该化学成分。"
    )
    assert "。。" not in summary
    assert "。；" not in summary
    assert "！；" not in summary
    assert "？；" not in summary


def test_parse_bilibili_json() -> None:
    payload = json.dumps({"body": [{"content": "你好"}, {"content": "世界"}]})
    assert parse_subtitle(payload, "json") == "你好\n世界"


def test_choose_human_subtitle_before_automatic() -> None:
    info = {
        "subtitles": {"en": [{"ext": "vtt", "url": "human"}]},
        "automatic_captions": {"zh": [{"ext": "json3", "url": "auto"}]},
    }
    selected = choose_subtitle(info)
    assert selected
    assert selected["url"] == "human"
    assert selected["automatic"] is False


def test_local_summary_has_points() -> None:
    text = (
        "这段视频介绍了一种新的信息核验方法。"
        "系统先提取视频字幕，再识别需要核验的主张。"
        "随后系统检索公开来源，并向用户展示证据。"
    )
    result = local_extractive_summary(text, "信息核验")
    assert result["summary"]
    assert result["key_points"]


def test_reject_unknown_host() -> None:
    try:
        validate_video_url("https://example.com/video")
    except UnsafeUrlError:
        pass
    else:
        raise AssertionError("unknown host should be rejected")


def test_visual_cost_estimate_is_nonzero() -> None:
    cost = _estimate_visual_cost_cny(180, 1280, 720)
    assert 0.001 < cost < 0.2


def test_subtitle_document_reports_coverage() -> None:
    payload = json.dumps(
        {
            "events": [
                {
                    "tStartMs": 90000,
                    "dDurationMs": 10000,
                    "segs": [{"utf8": "结尾内容"}],
                }
            ]
        }
    )
    text, end = parse_subtitle_document(payload, "json3")
    assert text == "结尾内容"
    assert end == 100


def test_canonicalize_tracking_urls() -> None:
    assert canonicalize_video_url(
        "https://www.bilibili.com/video/BV1U13g6rE39/?spm_id_from=333"
    ) == "https://www.bilibili.com/video/BV1U13g6rE39/"
    assert canonicalize_video_url(
        "https://www.youtube.com/watch?v=abc123&utm_source=x"
    ) == "https://www.youtube.com/watch?v=abc123"
    assert canonicalize_video_url(
        "https://www.iesdouyin.com/share/video/7452319896035167528/?region=CN"
    ) == "https://www.douyin.com/video/7452319896035167528"
    assert canonicalize_video_url(
        "https://www.douyin.com/jingxuan?modal_id=7452319896035167528"
    ) == "https://www.douyin.com/video/7452319896035167528"
    assert canonicalize_video_url(
        "https://www.iesdouyin.com/share/note/7666771519209435382/?region=CN"
    ) == "https://www.douyin.com/note/7666771519209435382"


def test_extract_url_from_mobile_share_text() -> None:
    assert extract_video_url(
        "3.14 复制打开抖音，看看【示例】 https://v.douyin.com/ifk5aGcn/ 复制此链接"
    ) == "https://v.douyin.com/ifk5aGcn/"
    assert extract_video_url(
        "【哔哩哔哩】一个视频 https://b23.tv/N7oqARv。"
    ) == "https://b23.tv/N7oqARv"


def test_resolve_short_link_and_reject_unsafe_redirect(monkeypatch) -> None:
    def fake_dns(hostname, *args, **kwargs):
        address = "127.0.0.1" if hostname == "private.b23.tv" else "8.8.8.8"
        return [(None, None, None, None, (address, 443))]

    monkeypatch.setattr(
        "app.security.socket.getaddrinfo",
        fake_dns,
    )

    class Response:
        def __init__(self, location: str):
            self.status_code = 302
            self.headers = {"location": location}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url):
            if url == "https://b23.tv/N7oqARv":
                return Response(
                    "https://www.bilibili.com/video/BV1chtHzSESN/?spm_id_from=333"
                )
            return Response("http://private.b23.tv/private")

    monkeypatch.setattr("app.security.httpx.Client", Client)
    assert resolve_video_input(
        "【哔哩哔哩】一个视频 https://b23.tv/N7oqARv"
    ) == "https://www.bilibili.com/video/BV1chtHzSESN/"

    try:
        resolve_video_input("https://v.douyin.com/bad/")
    except UnsafeUrlError as exc:
        assert "非公网地址" in str(exc)
    else:
        raise AssertionError("private redirect should be rejected")


def test_cache_can_list_and_delete(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    cache = ResultCache(ttl_seconds=1)
    cache.set(
        "key",
        {
            "protocol_version": ResultCache.SCHEMA_VERSION,
            "summary": "persisted",
        },
    )
    rows = cache.list()
    assert rows[0]["result"]["summary"] == "persisted"
    assert cache.delete("key") == 1
    assert cache.list() == []


def test_cache_purges_payloads_from_previous_protocol(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cache = ResultCache(ttl_seconds=60)
    cache.set("legacy", {"summary": "old payload"})
    cache.set(
        "current",
        {
            "protocol_version": ResultCache.SCHEMA_VERSION,
            "summary": "current payload",
        },
    )

    reloaded = ResultCache(ttl_seconds=60)

    assert reloaded.get("legacy") is None
    assert reloaded.get("current") is not None


def test_cache_key_includes_pipeline_version(monkeypatch) -> None:
    original = ResultCache.PIPELINE_VERSION
    before = ResultCache.key("https://example.com/video", "auto")
    monkeypatch.setattr(ResultCache, "PIPELINE_VERSION", "next-pipeline")
    after = ResultCache.key("https://example.com/video", "auto")

    assert original == "lossless-fulltext-v1"
    assert before != after


def test_bilibili_prefers_platform_backup_cdn(monkeypatch) -> None:
    class Response:
        text = (
            'window.__INITIAL_STATE__={"videoData":{"bvid":"BV1","cid":42}};'
            "(function()"
        )

        @staticmethod
        def json():
            return {
                "data": {
                    "dash": {
                        "video": [],
                        "audio": [
                            {
                                "id": 30232,
                                "backupUrl": [
                                    "https://edge.example/audio",
                                    "https://upos.example/audio",
                                ],
                            }
                        ],
                    }
                }
            }

    monkeypatch.setattr("app.pipeline.httpx.get", lambda *args, **kwargs: Response())
    info = {"id": "BV1", "formats": [{"format_id": "30232", "url": "https://mcdn"}]}
    _prefer_bilibili_backup_urls(info, "https://www.bilibili.com/video/BV1/")
    assert info["formats"][0]["url"] == "https://upos.example/audio"


def test_structured_information_uses_exact_downstream_keys() -> None:
    structured = StructuredInformation.model_validate(
        {
            "case_id": "watermelon-seed-rumor",
            "内容主题": "无籽西瓜食品安全谣言核验",
            "原子主张": ["家长尽量不要给孩子吃无籽西瓜"],
            "新闻事实": ["市场上存在无籽西瓜"],
            "隐性观点": ["人工培育的农作物不安全"],
        }
    )
    dumped = structured.model_dump(by_alias=True)
    assert set(dumped) == {
        "case_id",
        "内容主题",
        "原子主张",
        "新闻事实",
        "隐性观点",
    }
    assert "claims" not in dumped


def test_structured_information_schema_enforces_item_quality_without_count_caps() -> None:
    properties = STRUCTURED_INFORMATION_SCHEMA["properties"]
    for name in ("原子主张", "新闻事实", "隐性观点"):
        assert "maxItems" not in properties[name]
        assert properties[name]["items"]["minLength"] >= 8
        assert "\\u4e00" in properties[name]["items"]["pattern"]


def test_structured_information_does_not_keyword_filter_semantics() -> None:
    structured = StructuredInformation.model_validate(
        {
            "case_id": "strict-filter-case",
            "内容主题": "敌敌畏接触事故",
            "原子主张": [
                "加强监管很重要并且需要持续推进",
            ],
            "新闻事实": [
                "某媒体记者发表了相关评论",
            ],
            "隐性观点": [
                "记者认为事故需要引起重视",
            ],
        }
    )

    assert structured.atomic_claims == ["加强监管很重要并且需要持续推进"]
    assert structured.news_facts == ["某媒体记者发表了相关评论"]
    assert structured.implicit_opinions == ["记者认为事故需要引起重视"]


def test_structural_normalization_preserves_model_content() -> None:
    result = _validate_structured_result(
        {
            "case_id": "黎明-糖丸-筹款-疫苗",
            "内容主题": "黎明为儿童糖丸疫苗筹款的传闻核实",
            "原子主张": [
                "黎明在慈善晚宴通过危险表演为疫苗项目筹集善款",
                "相关善款用于采购疫苗并让中国儿童免费获得糖丸疫苗",
            ],
            "新闻事实": "内容声称黎明通过演唱会和慈善表演筹集善款购买糖丸疫苗",
        }
    )

    assert result["case_id"].startswith("case-")
    assert result["新闻事实"] == [
        "内容声称黎明通过演唱会和慈善表演筹集善款购买糖丸疫苗"
    ]
    assert result["隐性观点"] == []


def test_pipeline_ignores_model_usage_metadata_when_validating_protocol() -> None:
    structured = _structured_information_from_model_result(
        {
            "case_id": "li-ming-polio-vaccine",
            "内容主题": "黎明为内地儿童筹集疫苗善款的传闻",
            "原子主张": ["黎明筹集善款购买了小儿麻痹症疫苗"],
            "新闻事实": ["视频讲述黎明参与儿童疫苗筹款的事件"],
            "隐性观点": [],
            "_usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }
    )

    assert structured.case_id == "li-ming-polio-vaccine"
    assert structured.atomic_claims == ["黎明筹集善款购买了小儿麻痹症疫苗"]


def test_structured_protocol_accepts_all_grounded_items_without_count_caps() -> None:
    payload = {
        "case_id": "multi-event-case",
        "内容主题": "包含多项独立主张、事实和观点的综合内容",
        "原子主张": [
            f"这是需要独立核验的第{index}项具体原子主张"
            for index in range(1, 6)
        ],
        "新闻事实": [
            f"这是内容明确陈述的第{index}项独立新闻事实"
            for index in range(1, 4)
        ],
        "隐性观点": [
            f"这是根据完整上下文识别的第{index}项隐性观点"
            for index in range(1, 5)
        ],
    }

    structured = StructuredInformation.model_validate(payload)

    assert len(structured.atomic_claims) == 5
    assert len(structured.news_facts) == 3
    assert len(structured.implicit_opinions) == 4
    for field in ("原子主张", "新闻事实", "隐性观点"):
        assert "maxItems" not in STRUCTURED_INFORMATION_SCHEMA["properties"][field]


def test_structured_quality_gate_detects_fragmented_enumeration() -> None:
    issues = _structured_quality_issues(
        {
            "原子主张": [
                "黎明为筹款举办了近百场演唱会",
                "黎明最终筹措到三百五十万美元",
                "黎明完成了相关慈善筹款活动",
                "这笔善款全部用于采购疫苗",
                "疫苗让大量儿童获得免费接种",
            ],
            "新闻事实": [],
            "隐性观点": [],
        }
    )

    assert issues
    assert any("流水账" in issue or "代词" in issue for issue in issues)


def test_structured_quality_gate_detects_cross_claim_number_duplication() -> None:
    issues = _structured_quality_issues(
        {
            "原子主张": [
                "某人在一场活动中筹集到350万美元善款",
                "某人将筹集到的350万美元用于采购医疗物资",
            ],
            "新闻事实": [],
            "隐性观点": [],
        }
    )

    assert any("重复同一关键数字" in issue for issue in issues)


def test_structured_quality_gate_allows_many_independent_complete_claims() -> None:
    issues = _structured_quality_issues(
        {
            "原子主张": [
                "卫生机构发布了疫苗有效性的独立调查结论",
                "监管部门公布了食品添加剂的最新限量标准",
                "大学团队测量了当地地下水中的重金属浓度",
                "气象部门记录了沿海地区的极端降雨数据",
                "法院判决确认涉案企业承担相应民事责任",
                "研究人员报告新材料在高温环境下保持稳定",
            ],
            "新闻事实": [],
            "隐性观点": [],
        }
    )

    assert issues == []


def test_claim_density_is_adaptive_and_never_a_fixed_global_cap() -> None:
    assert _recommended_claim_density(
        "短视频口播内容",
        {"duration_seconds": 85, "image_count": 0},
    ) == 2
    assert _recommended_claim_density(
        "长篇内容" * 3000,
        {"duration_seconds": 1800, "image_count": 0},
    ) >= 6
    assert _recommended_claim_density(
        "图文内容",
        {"duration_seconds": 0, "image_count": 24},
    ) == 8


def test_spoken_text_short_circuits_visual_fallback() -> None:
    transcript = "这是一段包含具体事件和完整观点陈述的有效口播转写文本。"

    assert _has_usable_spoken_text(transcript) is True
    assert (
        _needs_visual_fallback(
            transcript=transcript,
            is_image_carousel=False,
            mode="auto",
        )
        is False
    )


def test_missing_spoken_text_uses_visual_fallback() -> None:
    assert (
        _needs_visual_fallback(
            transcript="",
            is_image_carousel=False,
            mode="auto",
        )
        is True
    )


def test_carousel_and_explicit_visual_mode_keep_visual_path() -> None:
    transcript = "这是一段包含具体事件和完整观点陈述的有效口播转写文本。"

    assert _needs_visual_fallback(
        transcript=transcript,
        is_image_carousel=True,
        mode="auto",
    )
    assert _needs_visual_fallback(
        transcript=transcript,
        is_image_carousel=False,
        mode="visual",
    )


def test_text_dominant_video_routing() -> None:
    plan = _classify_video(
        transcript="",
        keyframes=[
            KeyframeEvidence(
                frame_index=1,
                timestamp_seconds=0,
                ocr_text=["突发新闻：" + "重要文字" * 20],
                visual_observations=["一张新闻截图"],
                frame_type="first_frame",
            )
        ],
        duration=30,
        audio_activity=0,
    )
    assert plan.video_type == "text_dominant"
    assert "screen_text" in plan.active_modalities
    assert "speech" not in plan.active_modalities


def test_parse_douyin_note_embedded_payload() -> None:
    detail = {
        "awemeId": "7666771519209435382",
        "awemeType": 68,
        "desc": "一条图文作品",
        "authorInfo": {"nickname": "作者"},
        "images": [
            {
                "width": 864,
                "height": 1918,
                "urlList": ["https://p3-pc-sign.douyinpic.com/image.webp"],
            },
            {
                "width": 1080,
                "height": 1440,
                "urlList": ["https://p9-pc-sign.douyinpic.com/image2.webp"],
            },
        ],
    }
    chunk = f'7:{json.dumps({"aweme": {"statusCode": 0, "detail": detail}})}'
    html = (
        "<script>self.__pace_f.push([1,"
        f"{json.dumps(chunk)}"
        "])</script>"
    )
    info = _parse_douyin_note_html(
        html, "https://www.douyin.com/note/7666771519209435382"
    )
    assert info["id"] == "7666771519209435382"
    assert info["title"] == "一条图文作品"
    assert info["uploader"] == "作者"
    assert len(info["note_images"]) == 2
    assert info["extractor_key"] == "DouyinNote"


def test_malformed_json_is_wrapped_as_mimo_error() -> None:
    try:
        _parse_json_content('{"内容主题":"测试","原子主张":["主张",]}')
    except MimoError:
        pass
    else:
        raise AssertionError("malformed model JSON should be a MimoError")


def test_structured_information_json_is_repaired_once(monkeypatch) -> None:
    calls = []

    async def fake_completion(payload, timeout=120):
        calls.append(payload)
        if len(calls) == 1:
            return {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {
                            "content": (
                                '{"case_id":"test-case","内容主题":"截断",'
                                '"原子主张":['
                            )
                        },
                    }
                ]
            }
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": json.dumps(
                            {
                                "case_id": "test-case",
                                "内容主题": "已修复",
                                "原子主张": ["敌敌畏接触皮肤可能导致人体中毒"],
                                "新闻事实": [],
                                "隐性观点": [],
                            },
                            ensure_ascii=False,
                        )
                    },
                }
            ],
            "usage": {"completion_tokens": 20},
        }

    monkeypatch.setattr("app.mimo._completion", fake_completion)
    result = asyncio.run(structure_information("输入", {"title": "标题"}))
    assert result["内容主题"] == "已修复"
    assert len(calls) == 2
    assert calls[0]["response_format"]["type"] == "json_schema"
    assert calls[1]["response_format"]["type"] == "json_schema"
    assert calls[0]["response_format"]["json_schema"]["strict"] is True


def test_semantic_recompression_only_replaces_a_strictly_better_result(
    monkeypatch,
) -> None:
    fragmented = {
        "case_id": "fundraising-case",
        "内容主题": "原始主题内容",
        "原子主张": [
            "某人通过多场活动开展筹款",
            "某人最终筹集到一笔大额善款",
            "某人完成了这次公益筹款活动",
            "这笔善款被用于采购相关物资",
            "相关物资最终送达受助人群",
        ],
        "新闻事实": [],
        "隐性观点": [],
    }
    still_fragmented = {
        **fragmented,
        "内容主题": "不应采用的重压缩结果",
    }
    responses = [fragmented, still_fragmented]

    async def fake_completion(payload, timeout=120):
        content = responses.pop(0)
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": json.dumps(content, ensure_ascii=False),
                    },
                }
            ],
            "usage": {"completion_tokens": 20},
        }

    monkeypatch.setattr("app.mimo._completion", fake_completion)
    result = asyncio.run(structure_information("输入证据", {"title": "标题"}))

    assert result["内容主题"] == "原始主题内容"


def test_semantic_recompression_accepts_compact_self_contained_result(
    monkeypatch,
) -> None:
    fragmented = {
        "case_id": "fundraising-case",
        "内容主题": "原始主题内容",
        "原子主张": [
            "某人通过多场活动开展筹款",
            "某人最终筹集到一笔大额善款",
            "某人完成了这次公益筹款活动",
            "这笔善款被用于采购相关物资",
            "相关物资最终送达受助人群",
        ],
        "新闻事实": [],
        "隐性观点": [],
    }
    compact = {
        "case_id": "fundraising-case",
        "内容主题": "采用的紧凑主题内容",
        "原子主张": [
            "某人通过多场活动筹集大额善款，并将善款用于采购和发放相关物资"
        ],
        "新闻事实": [],
        "隐性观点": [],
    }
    responses = [fragmented, compact]

    async def fake_completion(payload, timeout=120):
        content = responses.pop(0)
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": json.dumps(content, ensure_ascii=False),
                    },
                }
            ],
            "usage": {"completion_tokens": 20},
        }

    monkeypatch.setattr("app.mimo._completion", fake_completion)
    result = asyncio.run(structure_information("输入证据", {"title": "标题"}))

    assert result["内容主题"] == "采用的紧凑主题内容"
    assert len(result["原子主张"]) == 1


def test_local_structured_fallback_is_valid_and_deduplicated() -> None:
    result = _local_structured_information(
        source_text=(
            "[语音/字幕]\n[00:00:00-00:01:15] 某机构宣布将在明年发布产品。\n"
            "[图片 1][屏幕文字] 某机构宣布将在明年发布产品。"
        ),
        title="产品发布消息",
        webpage_url="https://www.example.com/video/1",
    )
    assert result.content_topic == "产品发布消息"
    assert result.atomic_claims == []
    assert result.news_facts == []
    assert result.implicit_opinions == []
