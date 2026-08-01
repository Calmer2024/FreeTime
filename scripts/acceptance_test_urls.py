from __future__ import annotations

import argparse
import asyncio
import json
import time

import httpx


async def test_url(client: httpx.AsyncClient, url: str) -> dict[str, object]:
    started = time.perf_counter()
    try:
        response = await client.post(
            "/api/analyze",
            json={"url": url, "mode": "auto", "refresh": True},
        )
        try:
            payload = response.json()
        except json.JSONDecodeError:
            return {
                "url": url,
                "ok": False,
                "status_code": response.status_code,
                "error": response.text[:500] or "服务返回空的非 JSON 响应",
                "elapsed_seconds": round(time.perf_counter() - started, 2),
            }
        if response.is_error:
            return {
                "url": url,
                "ok": False,
                "status_code": response.status_code,
                "error": payload.get("detail", payload),
                "elapsed_seconds": round(time.perf_counter() - started, 2),
            }
        coverage = payload.get("coverage") or {}
        structured = payload.get("structured_data") or {}
        return {
            "url": url,
            "ok": True,
            "status_code": response.status_code,
            "resolved_url": (payload.get("metadata") or {}).get("webpage_url"),
            "title": (payload.get("metadata") or {}).get("title"),
            "duration_seconds": (payload.get("metadata") or {}).get(
                "duration_seconds"
            ),
            "strategy": payload.get("strategy"),
            "coverage_status": coverage.get("status"),
            "speech_percent": coverage.get("speech_percent"),
            "screen_text_percent": coverage.get("screen_text_percent"),
            "case_id": structured.get("case_id"),
            "atomic_claims": len(structured.get("原子主张") or []),
            "news_facts": len(structured.get("新闻事实") or []),
            "implicit_opinions": len(structured.get("隐性观点") or []),
            "estimated_cost_cny": payload.get("estimated_cost_cny"),
            "elapsed_seconds": round(time.perf_counter() - started, 2),
        }
    except Exception as exc:
        return {
            "url": url,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_seconds": round(time.perf_counter() - started, 2),
        }


async def main(urls: list[str], base_url: str) -> None:
    timeout = httpx.Timeout(1800)
    async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as client:
        tasks = [asyncio.create_task(test_url(client, url)) for url in urls]
        for task in asyncio.as_completed(tasks):
            print(json.dumps(await task, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("urls", nargs="+")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    arguments = parser.parse_args()
    asyncio.run(main(arguments.urls, arguments.base_url))
