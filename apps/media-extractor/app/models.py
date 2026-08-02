from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)


StructuredSentence = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=8,
        max_length=300,
        pattern=r".*[一-鿿].*",
    ),
]


class AnalyzeRequest(BaseModel):
    url: str = Field(min_length=1, max_length=10_000)
    input_kind: Literal["auto", "article", "platform"] = "auto"
    mode: Literal["auto", "visual"] = "auto"
    refresh: bool = False


class VideoMetadata(BaseModel):
    platform: str
    content_type: Literal[
        "video", "image_carousel", "article", "upload_bundle"
    ] = "video"
    image_count: int = 0
    source_subtype: str | None = None
    source_context: dict[str, Any] = Field(default_factory=dict)
    title: str
    uploader: str | None = None
    duration_seconds: float | None = None
    thumbnail: str | None = None
    webpage_url: str


class StageTiming(BaseModel):
    name: str
    milliseconds: int


class StructuredInformation(BaseModel):
    """Strict downstream information-extraction contract."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    case_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    content_topic: str = Field(alias="内容主题", min_length=1, max_length=200)
    atomic_claims: list[StructuredSentence] = Field(alias="原子主张")
    implicit_opinions: list[StructuredSentence] = Field(alias="隐性观点")

    @field_validator("atomic_claims", "implicit_opinions", mode="before")
    @classmethod
    def normalize_items(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        normalized: list[object] = []
        seen: set[str] = set()
        for item in value:
            identity = str(item).strip()
            if identity in seen:
                continue
            seen.add(identity)
            normalized.append(item)
        return normalized


class KeyframeEvidence(BaseModel):
    frame_index: int
    timestamp_seconds: float
    ocr_text: list[str] = Field(default_factory=list)
    visual_observations: list[str] = Field(default_factory=list)
    frame_type: Literal[
        "scene_change", "periodic", "first_frame", "image_slide", "unknown"
    ] = "unknown"


class CostStep(BaseModel):
    level: Literal["L0", "L1", "L2", "L3"]
    name: str
    executed: bool
    reason: str


class ExtractionPlan(BaseModel):
    video_type: Literal[
        "speech_dominant",
        "text_dominant",
        "event_footage",
        "mixed",
        "low_information",
        "unknown",
    ] = "unknown"
    active_modalities: list[
        Literal["speech", "screen_text", "visual", "post_context", "provenance"]
    ] = Field(default_factory=list)
    highest_cost_level: Literal["L0", "L1", "L2", "L3"] = "L0"
    reasons: list[str] = Field(default_factory=list)


class CoverageInfo(BaseModel):
    status: Literal[
        "structured_ready",
        "partial",
        "needs_review",
        "no_structured_information",
        "unavailable",
        "metadata_only",
        "complete",
    ]
    audio_percent: float = 0
    speech_percent: float = 0
    text_retention_percent: float = 0
    screen_text_percent: float = 0
    scene_percent: float = 0
    visual_analyzed: bool = False
    post_context_captured: bool = False
    provenance_checked: bool = False
    subtitle_source: Literal["human", "automatic", "none"] = "none"
    missing_ranges: list[str] = Field(default_factory=list)
    critical_gaps: list[str] = Field(default_factory=list)


class AnalyzeResponse(BaseModel):
    protocol_version: Literal["structured-information-v4"] = "structured-information-v4"
    request_id: str
    cached: bool
    strategy: Literal["subtitle", "asr", "visual", "hybrid", "metadata"]
    metadata: VideoMetadata
    summary: str
    key_points: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    coverage_note: str
    transcript: str | None = None
    transcript_excerpt: str | None = None
    transcript_chars: int = 0
    full_source_text: str | None = None
    structured_input_text: str = ""
    structured_input_chars: int = 0
    structured_input_truncated: bool = False
    cleaned_article: str = ""
    timings: list[StageTiming] = Field(default_factory=list)
    orchestration_timings: list[StageTiming] = Field(default_factory=list)
    extraction_milliseconds: int = 0
    full_pipeline_milliseconds: int = 0
    estimated_cost_cny: float = 0
    coverage: CoverageInfo = Field(
        default_factory=lambda: CoverageInfo(status="metadata_only")
    )
    visual_notes: list[str] = Field(default_factory=list)
    keyframes: list[KeyframeEvidence] = Field(default_factory=list)
    structured_data: StructuredInformation = Field(
        default_factory=lambda: StructuredInformation(
            case_id="unstructured",
            content_topic="未识别内容主题",
            atomic_claims=[],
            implicit_opinions=[],
        )
    )
    extraction_plan: ExtractionPlan = Field(default_factory=ExtractionPlan)
    cost_trace: list[CostStep] = Field(default_factory=list)
    extracted_at: datetime = Field(default_factory=datetime.now)


class StoredVideo(BaseModel):
    cache_key: str
    created_at: datetime
    expired: bool
    result: AnalyzeResponse


class StoredVideoList(BaseModel):
    items: list[StoredVideo]
    total: int


class DeleteResponse(BaseModel):
    deleted: int
