from fastapi.testclient import TestClient

from app.main import app


URL = "https://www.bilibili.com/video/BV1U13g6rE39/"


response = TestClient(app).post(
    "/api/analyze",
    json={"url": URL, "mode": "auto", "refresh": True},
)
print("status", response.status_code, flush=True)
data = response.json()
structured = data.get("structured_data") or {}
print(
    {
        "strategy": data.get("strategy"),
        "coverage": (data.get("coverage") or {}).get("status"),
        "audio_percent": (data.get("coverage") or {}).get("audio_percent"),
        "video_type": (data.get("extraction_plan") or {}).get("video_type"),
        "cost_level": (data.get("extraction_plan") or {}).get(
            "highest_cost_level"
        ),
        "case_id": structured.get("case_id"),
        "atomic_claims": len(structured.get("原子主张") or []),
        "news_facts": len(structured.get("新闻事实") or []),
        "implicit_opinions": len(structured.get("隐性观点") or []),
        "keyframes": len(data.get("keyframes", [])),
        "transcript_chars": data.get("transcript_chars"),
        "estimated_cost_cny": data.get("estimated_cost_cny"),
        "timings": data.get("timings"),
    },
    flush=True,
)
