from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    mimo_api_key: str = os.getenv("MIMO_API_KEY", "").strip()
    mimo_base_url: str = os.getenv(
        "MIMO_BASE_URL", "https://api.xiaomimimo.com/v1"
    ).rstrip("/")
    summary_model: str = os.getenv("MIMO_SUMMARY_MODEL", "mimo-v2.5")
    asr_model: str = os.getenv("MIMO_ASR_MODEL", "mimo-v2.5-asr")
    max_duration_seconds: int = int(
        os.getenv("MAX_VIDEO_DURATION_SECONDS", "1800")
    )
    max_transcript_chars: int = int(os.getenv("MAX_TRANSCRIPT_CHARS", "50000"))
    cache_ttl_seconds: int = int(os.getenv("CACHE_TTL_SECONDS", "86400"))
    ytdlp_cookies_file: str = os.getenv("YTDLP_COOKIES_FILE", "").strip()
    ytdlp_user_agent: str = os.getenv("YTDLP_USER_AGENT", "").strip()
    douyin_auto_cookies: bool = (
        os.getenv("DOUYIN_AUTO_COOKIES", "true").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    douyin_cookie_max_age_seconds: int = int(
        os.getenv("DOUYIN_COOKIE_MAX_AGE_SECONDS", "1800")
    )
    douyin_browser_profile_dir: str = os.getenv(
        "DOUYIN_BROWSER_PROFILE_DIR", ".cache/douyin-browser-profile"
    ).strip()
    douyin_browser_headless: bool = (
        os.getenv("DOUYIN_BROWSER_HEADLESS", "true").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    asr_chunk_seconds: int = int(os.getenv("ASR_CHUNK_SECONDS", "75"))
    asr_concurrency: int = int(os.getenv("ASR_CONCURRENCY", "3"))
    video_visual_fps: float = float(os.getenv("VIDEO_VISUAL_FPS", "0.2"))
    keyframe_scene_threshold: float = float(
        os.getenv("KEYFRAME_SCENE_THRESHOLD", "0.28")
    )
    keyframe_period_seconds: int = int(
        os.getenv("KEYFRAME_PERIOD_SECONDS", "30")
    )
    keyframe_max_frames: int = int(os.getenv("KEYFRAME_MAX_FRAMES", "16"))
    full_visual_escalation: bool = (
        os.getenv("FULL_VISUAL_ESCALATION", "true").strip().lower()
        in {"1", "true", "yes", "on"}
    )


settings = Settings()
