"""
demo_search.py  —  Phase 3 真实分级并发检索演示脚本
v7.0 - Exa 主搜索 + open-webSearch 条件兜底

[架构]
  通用/辟谣轨  : Exa Search API (auto + highlights)
                 → 单条失败或结果不足时使用 open-webSearch 兜底
  学术论文轨  : OpenAlex API + ArXiv API (免 Key，直连稳定)
  百科事实轨  : Wikipedia REST /page/summary/ (最稳定)
  学术补充轨  : Semantic Scholar (限流快速失败)

[前置依赖]
  启动 open-webSearch daemon:
  cd open-webSearch
  $env:ENABLE_CORS="true"; node build/index.js serve --port 3000
"""
import asyncio
import argparse
import html
import re
import sys
import xml.etree.ElementTree as ET
import urllib.parse
import time
import json
import os
from pathlib import Path
from typing import List, Dict, Any

import httpx
from bs4 import BeautifulSoup

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ──────────────────────────────────────────────────────────────────────────────
# 域名分级正则
# ──────────────────────────────────────────────────────────────────────────────
TIER1_PATTERN = re.compile(
    r"(gov\.cn|edu\.cn|piyao\.org\.cn|fact\.qq\.com|kepuchina\.cn"
    r"|cas\.cn|caas\.cn|moa\.gov\.cn|cctv\.com|xinhuanet\.com|people\.com\.cn"
    r"|news\.cn|ce\.cn|sciencenet\.cn|chinanews\.com\.cn"
    r"|who\.int|fda\.gov|cdc\.gov|epa\.gov|nih\.gov|usda\.gov)",
    re.IGNORECASE,
)
TIER2_PATTERN = re.compile(
    r"(thepaper\.cn|zhihu\.com|baidu\.com|baike\.baidu\.com"
    r"|163\.com|sohu\.com|sina\.com|qq\.com|cnki\.net"
    r"|wikipedia\.org|openalex\.org|doi\.org|arxiv\.org|semanticscholar\.org)",
    re.IGNORECASE,
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}

HEADERS_API = {
    "User-Agent": "MiMoTrust/5.0 (contact: trust@mimo.example.com)",
    "Accept": "application/json",
}

EXA_SEARCH_URL = "https://api.exa.ai/search"
EXA_RESULTS_PER_QUERY = 5
EXA_MIN_RESULTS_BEFORE_FALLBACK = 3
EXA_QUERY_TIMEOUT_SECONDS = 8.0


def load_env_value(name: str) -> str | None:
    """Read configuration from the process first, then the local .env file."""
    value = os.getenv(name)
    if value:
        return value

    env_file = Path(".env")
    if not env_file.exists():
        return None
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, candidate = line.split("=", 1)
        if key.strip() == name:
            return candidate.strip().strip('"').strip("'")
    return None


def classify_tier(source: str) -> str:
    if TIER1_PATTERN.search(source):
        return "TIER_1"
    if TIER2_PATTERN.search(source):
        return "TIER_2"
    return "TIER_3"


def canonicalize_result_url(url: str) -> str:
    """Normalize result identity without tracking/query-string variants."""
    parsed = urllib.parse.urlsplit(str(url).strip())
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    if not scheme or not hostname:
        return str(url).split("#", 1)[0].split("?", 1)[0].rstrip("/").lower()
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit((scheme, f"{hostname}{port}", path, "", ""))


# ──────────────────────────────────────────────────────────────────────────────
# 模块 A：OpenAlex 学术（最稳定，保留）
# ──────────────────────────────────────────────────────────────────────────────
async def search_openalex(
    client: httpx.AsyncClient, query: str, limit: int = 4
) -> List[Dict[str, Any]]:
    """OpenAlex 开放学术 API — 无 Key，限额宽松，最稳定"""
    url = (
        f"https://api.openalex.org/works"
        f"?search={urllib.parse.quote(query)}&per-page={limit}"
        f"&mailto=trust@mimo.example.com"
    )
    results = []
    try:
        resp = await client.get(url, headers=HEADERS_API, timeout=15.0)
        if resp.status_code == 200:
            for work in resp.json().get("results", []):
                title = work.get("title", "")
                doi = work.get("doi") or work.get("id") or ""
                year = work.get("publication_year", "")
                concepts = [c.get("display_name", "") for c in work.get("concepts", [])[:4]]
                snippet = f"【学术论文】{year}年。主题: {', '.join(concepts)}。"
                if title:
                    results.append({
                        "title": f"🎓 [OpenAlex] {title}",
                        "url": doi, "snippet": snippet, "tier": "TIER_2",
                        "provider": "openalex",
                        "search_query": query,
                    })
            print(f"  ✅ OpenAlex ({query[:40]!r}): {len(results)} 条")
        else:
            print(f"  ⚠️ OpenAlex {resp.status_code}")
    except Exception as e:
        print(f"  ❌ OpenAlex 异常: {e}")
    return results


# ──────────────────────────────────────────────────────────────────────────────
# 模块 B：ArXiv 学术（精确 query + https + follow_redirects）
# ──────────────────────────────────────────────────────────────────────────────
async def search_arxiv(
    client: httpx.AsyncClient, query: str, limit: int = 3
) -> List[Dict[str, Any]]:
    """Search ArXiv with a generic English academic query."""
    url = (
        f"https://export.arxiv.org/api/query"
        f"?search_query=all:{urllib.parse.quote(query)}"
        f"&start=0&max_results={limit}"
    )
    results = []
    try:
        resp = await client.get(
            url, headers=HEADERS_API, timeout=15.0, follow_redirects=True
        )
        if resp.status_code == 200:
            root = ET.fromstring(resp.text)
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            for entry in root.findall("atom:entry", ns):
                title_el = entry.find("atom:title", ns)
                summary_el = entry.find("atom:summary", ns)
                id_el = entry.find("atom:id", ns)
                if title_el is None:
                    continue
                title = title_el.text.strip().replace("\n", " ")
                summary = (summary_el.text or "").strip().replace("\n", " ")
                link = (id_el.text or "").strip()
                results.append({
                    "title": f"🔬 [ArXiv] {title}",
                    "url": link,
                    "snippet": f"摘要: {summary[:500]}...",
                    "tier": "TIER_2",
                    "provider": "arxiv",
                    "search_query": query,
                })
            print(f"  ✅ ArXiv ({query[:40]!r}): {len(results)} 条")
        else:
            print(f"  ⚠️ ArXiv {resp.status_code}")
    except Exception as e:
        print(f"  ❌ ArXiv 异常: {e}")
    return results


# ──────────────────────────────────────────────────────────────────────────────
# 模块 C：Semantic Scholar（保留请求，快速处理限流）
# ──────────────────────────────────────────────────────────────────────────────
async def search_semantic_scholar_safe(query: str, limit: int = 4) -> List[Dict[str, Any]]:
    """
    Semantic Scholar 单次调用。429 时立即返回，网络过慢时由外层预算截断。
    生产环境建议申请免费 API Key（可将限额提升至 1000 req/min）。
    """
    url = (
        f"https://api.semanticscholar.org/graph/v1/paper/search"
        f"?query={urllib.parse.quote(query)}&limit={limit}"
        f"&fields=title,abstract,year,citationCount,externalIds,url"
    )
    results = []
    try:
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.get(url, headers=HEADERS_API, timeout=3.0)
        if resp.status_code == 200:
            for paper in resp.json().get("data", []):
                title = paper.get("title", "")
                abstract = (paper.get("abstract") or "")[:200]
                year = paper.get("year", "")
                citations = paper.get("citationCount", 0)
                doi = paper.get("externalIds", {}).get("DOI", "")
                paper_url = paper.get("url") or (f"https://doi.org/{doi}" if doi else "")
                if title:
                    results.append({
                        "title": f"🎓 [Semantic Scholar] {title}",
                        "url": paper_url,
                        "snippet": f"【{year}年，被引{citations}次】{abstract}...",
                        "tier": "TIER_2",
                        "provider": "semantic-scholar",
                        "search_query": query,
                    })
            print(f"  ✅ Semantic Scholar: {len(results)} 条")
        elif resp.status_code == 429:
            print("  ⚠️ Semantic Scholar 429 - 已限流，立即跳过（建议申请免费 API Key）")
        else:
            print(f"  ⚠️ Semantic Scholar {resp.status_code}: {resp.text[:80]}")
    except Exception as e:
        print(f"  ❌ Semantic Scholar 异常: {e}")
    return results


# ──────────────────────────────────────────────────────────────────────────────
# 模块 D：Wikipedia REST API（稳定，保留）
# ──────────────────────────────────────────────────────────────────────────────
async def fetch_wikipedia(
    client: httpx.AsyncClient, title: str, lang: str = "zh"
) -> List[Dict[str, Any]]:
    encoded = urllib.parse.quote(title.replace(" ", "_"))
    url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{encoded}"
    results = []
    try:
        resp = await client.get(url, headers=HEADERS_API, timeout=12.0)
        if resp.status_code == 200:
            data = resp.json()
            extract = data.get("extract", "")
            page_url = data.get("content_urls", {}).get("desktop", {}).get("page", url)
            if extract and len(extract) > 20:
                results.append({
                    "title": f"📖 [维基百科] {data.get('title', title)}",
                    "url": page_url,
                    "snippet": extract[:300],
                    "tier": "TIER_2",
                    "provider": "wikipedia",
                    "search_query": title,
                })
            print(f"  ✅ Wikipedia ({title!r}): {'获取到' if results else '无内容'}")
        else:
            print(f"  ⚠️ Wikipedia {resp.status_code} ({title!r})")
    except Exception as e:
        print(f"  ❌ Wikipedia 异常 ({title!r}): {e}")
    return results


# ──────────────────────────────────────────────────────────────────────────────
# 模块 E：Exa 通用网页搜索（主搜索）
# ──────────────────────────────────────────────────────────────────────────────
async def search_exa(
    client: httpx.AsyncClient,
    api_key: str,
    query: str,
    limit: int = EXA_RESULTS_PER_QUERY,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    response = await client.post(
        EXA_SEARCH_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "query": query,
            "type": "auto",
            "numResults": limit,
            "userLocation": "CN",
            "contents": {"highlights": True},
        },
        timeout=12.0,
    )
    response.raise_for_status()
    payload = response.json()
    results = []
    for rank, item in enumerate(payload.get("results", []), 1):
        raw_url = str(item.get("url") or "")
        domain = urllib.parse.urlparse(raw_url).netloc.removeprefix("www.")
        raw_title = re.sub(
            r"\s+", " ", html.unescape(str(item.get("title") or ""))
        ).strip()
        if len(raw_title) > 120:
            raw_title = raw_title[:117].rstrip() + "..."
        title = raw_title or domain or "未命名网页"
        highlights = [
            html.unescape(str(text)).strip()
            for text in (item.get("highlights") or [])
            if str(text).strip()
        ]
        snippet = "\n".join(highlights[:2])[:700]
        if not raw_url or not snippet:
            continue
        results.append(
            {
                "title": f"🔎 [Exa] {title}",
                "url": raw_url,
                "snippet": snippet,
                "tier": classify_tier(raw_url),
                "provider": "exa",
                "search_query": query,
                "provider_rank": rank,
                "published_date": item.get("publishedDate"),
            }
        )

    cost = payload.get("costDollars") or {}
    print(f"  ✅ Exa ({query[:30]!r}): {len(results)} 条")
    return results, {
        "request_id": payload.get("requestId"),
        "search_type": payload.get("searchType"),
        "cost_dollars": float(cost.get("total") or 0),
    }


# ──────────────────────────────────────────────────────────────────────────────
# 模块 F：open-webSearch 本地 daemon（条件兜底）
#         前置依赖: cd open-webSearch && node build/index.js serve --port 3000
# ──────────────────────────────────────────────────────────────────────────────
OPEN_WEBSEARCH_URL = os.getenv(
    "OPEN_WEBSEARCH_URL",
    "http://127.0.0.1:3000",
).rstrip("/")


async def search_via_open_websearch(
    client: httpx.AsyncClient,
    query: str,
    engines: List[str] = None,
    limit: int = 8,
) -> List[Dict[str, Any]]:
    """
    通过本地 open-webSearch daemon 进行通用网页检索。
    支持引擎: sogou, duckduckgo, bing, baidu, brave, startpage 等。
    搜狗在国内网络环境下最稳定，DuckDuckGo 可补充英文/百科结果。
    """
    if engines is None:
        engines = ["sogou", "duckduckgo"]
    payload = {"query": query, "engines": engines, "limit": limit}
    results = []
    try:
        resp = await client.post(
            f"{OPEN_WEBSEARCH_URL}/search",
            json=payload,
            timeout=25.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "ok":
                for item in data.get("data", {}).get("results", []):
                    title = item.get("title", "")
                    raw_url = item.get("url", "")
                    snippet = item.get("description", "") or item.get("snippet", "")
                    engine = item.get("engine", "unknown")
                    tier = classify_tier(raw_url)
                    results.append({
                        "title": f"🔍 [{engine}] {title}",
                        "url": raw_url,
                        "snippet": snippet,
                        "tier": tier,
                        "provider": "open-websearch",
                        "search_query": query,
                    })
                # 报告 partialFailures
                partial_failures = data.get("data", {}).get("partialFailures", [])
                for pf in partial_failures:
                    print(f"    ⚠️ {pf.get('engine')} 部分失败: {pf.get('message', '')}")
                if not results and partial_failures:
                    details = "; ".join(
                        f"{pf.get('engine')}: {pf.get('message') or 'unknown error'}"
                        for pf in partial_failures
                    )
                    raise RuntimeError(details)
            print(
                f"  ✅ open-webSearch ({query[:30]!r}): "
                f"{len(results)} 条 | 引擎: {engines}"
            )
        else:
            print(f"  ⚠️ open-webSearch HTTP {resp.status_code} ({query[:30]!r})")
    except httpx.ConnectError:
        print(
            f"  ❌ open-webSearch 连接失败 — daemon 未启动？"
            f"\n     请运行: cd open-webSearch && node build/index.js serve --port 3000"
        )
        raise
    except Exception as e:
        print(f"  ❌ open-webSearch 异常: {type(e).__name__}: {e}")
        raise
    return results


async def check_open_websearch_health(client: httpx.AsyncClient) -> bool:
    """检测 open-webSearch daemon 是否在线"""
    try:
        resp = await client.get(f"{OPEN_WEBSEARCH_URL}/health", timeout=3.0)
        return resp.status_code == 200
    except Exception:
        return False


async def run_timed_search(
    label: str,
    operation,
    timeout_seconds: float,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Run one provider independently so a slow source cannot hold the batch."""
    started_at = time.perf_counter()
    status = "ok"
    error = None
    metadata: Dict[str, Any] = {}
    try:
        operation_result = await asyncio.wait_for(
            operation, timeout=timeout_seconds
        )
        if isinstance(operation_result, tuple):
            results, metadata = operation_result
        else:
            results = operation_result
        if not results:
            status = "empty"
    except asyncio.TimeoutError:
        results = []
        status = "timeout"
        error = f"exceeded {timeout_seconds:.1f}s budget"
    except Exception as exc:
        results = []
        status = "error"
        error = f"{type(exc).__name__}: {exc}"

    elapsed_seconds = time.perf_counter() - started_at
    timing = {
        "provider": label,
        "status": status,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "result_count": len(results),
        "timeout_seconds": timeout_seconds,
    }
    if error:
        timing["error"] = error
    timing.update(metadata)

    print(
        f"  [TIMING] {label}: {elapsed_seconds:.2f}s | "
        f"{status} | {len(results)} results"
    )
    return results, timing


# ──────────────────────────────────────────────────────────────────────────────
# 全异步并发调度引擎
# ──────────────────────────────────────────────────────────────────────────────
async def run_concurrent_multi_source_search(
    search_plan: Dict[str, Any],
) -> Dict[str, Any]:
    search_started_at = time.perf_counter()
    web_queries = list(search_plan.get("web_queries", []))
    academic_queries = list(search_plan.get("academic_queries", []))
    encyclopedia_topics = list(search_plan.get("encyclopedia_topics", []))
    exa_api_key = load_env_value("EXA_API_KEY")
    web_provider = "exa" if exa_api_key else "open-websearch"
    print(
        "\n🚀 开始全异步并发跨源检索 v7.0\n"
        "   学术轨  : OpenAlex + ArXiv (免Key直连)\n"
        "   百科轨  : Wikipedia REST (稳定)\n"
        f"   通用/辟谣轨: {web_provider}"
        " (Exa失败时按查询使用open-webSearch兜底)\n"
        "   学术补充 : Semantic Scholar (并发执行, 限流快速失败)\n"
    )
    print("📡 各模块状态日志：")

    async with httpx.AsyncClient(verify=False) as client:
        jobs = []

        # ── 通用/辟谣轨 ──────────────────────────────────────────────────────
        if exa_api_key:
            for index, query in enumerate(web_queries, 1):
                jobs.append((
                    f"exa:q{index}",
                    search_exa(client, exa_api_key, query),
                    EXA_QUERY_TIMEOUT_SECONDS,
                ))
        else:
            print("  ⚠️ 未配置 EXA_API_KEY，使用 open-webSearch 全量兜底")
            ows_alive = await check_open_websearch_health(client)
            if ows_alive:
                print(f"  ✅ open-webSearch daemon 在线 ({OPEN_WEBSEARCH_URL})")
                for index, query in enumerate(web_queries, 1):
                    jobs.append((
                        f"open-websearch:sogou+duckduckgo:q{index}",
                        search_via_open_websearch(
                            client, query, engines=["sogou", "duckduckgo"]
                        ),
                        8.0,
                    ))
            else:
                print("  ⚠️ open-webSearch daemon 不在线，通用网页搜索不可用")

        # ── 学术轨（OpenAlex + ArXiv）─────────────────────────────────────────
        for index, query in enumerate(academic_queries, 1):
            jobs.append((
                f"openalex:q{index}",
                search_openalex(client, query),
                10.0,
            ))
            jobs.append((
                f"arxiv:q{index}",
                search_arxiv(client, query),
                10.0,
            ))

        # ── 百科轨（Wikipedia REST）───────────────────────────────────────────
        for index, topic in enumerate(encyclopedia_topics, 1):
            if isinstance(topic, str):
                title, language = topic, "zh"
            else:
                title = str(topic.get("title", ""))
                language = str(topic.get("language", "zh"))
            if title:
                jobs.append((
                    f"wikipedia:q{index}",
                    fetch_wikipedia(client, title, language),
                    8.0,
                ))

        # Keep the provider in the search set, but do not let rate limiting
        # create a long tail for the other concurrent results.
        if academic_queries:
            jobs.append((
                "semantic-scholar:q1",
                search_semantic_scholar_safe(academic_queries[0], limit=4),
                3.5,
            ))

        timed_results = await asyncio.gather(*(
            run_timed_search(label, operation, timeout_seconds)
            for label, operation, timeout_seconds in jobs
        ))

        # Exa 正常时不混入低质量聚合结果；仅对失败或结果不足的 Query 兜底。
        fallback_queries = []
        if exa_api_key:
            for (label, _, _), (results, _) in zip(jobs, timed_results):
                if label.startswith("exa:") and len(results) < EXA_MIN_RESULTS_BEFORE_FALLBACK:
                    query_index = int(label.removeprefix("exa:q")) - 1
                    fallback_queries.append(
                        (query_index + 1, web_queries[query_index])
                    )

        if fallback_queries:
            ows_alive = await check_open_websearch_health(client)
            if ows_alive:
                print(
                    f"  ⚠️ Exa 有 {len(fallback_queries)} 条查询结果不足，"
                    "启动 open-webSearch 条件兜底"
                )
                fallback_jobs = [
                    (
                        f"open-websearch:fallback:q{index}",
                        search_via_open_websearch(
                            client, query, engines=["sogou", "duckduckgo"]
                        ),
                        8.0,
                    )
                    for index, query in fallback_queries
                ]
                fallback_results = await asyncio.gather(*(
                    run_timed_search(label, operation, timeout_seconds)
                    for label, operation, timeout_seconds in fallback_jobs
                ))
                timed_results.extend(fallback_results)
            else:
                print("  ⚠️ Exa 结果不足，但 open-webSearch 兜底不可用")

    all_results_list = [results for results, _ in timed_results]
    provider_timings = [timing for _, timing in timed_results]
    provider_batches = [
        {
            **timing,
            "results": results,
        }
        for results, timing in timed_results
    ]

    # ── 去重 + 分级汇总 ────────────────────────────────────────────────────────
    print("\n📋 去重与 Tier 分级汇总中...")
    tier1: List[Dict] = []
    tier2: List[Dict] = []
    tier3: List[Dict] = []
    seen: set = set()

    for item_list in all_results_list:
        for item in item_list:
            url_key = canonicalize_result_url(str(item.get("url") or ""))
            dedupe_key = url_key or str(item.get("title") or "").strip().lower()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            if item["tier"] == "TIER_1":
                tier1.append(item)
            elif item["tier"] == "TIER_2":
                tier2.append(item)
            else:
                tier3.append(item)

    total_seconds = time.perf_counter() - search_started_at
    slowest = sorted(
        provider_timings,
        key=lambda item: item["elapsed_seconds"],
        reverse=True,
    )[:5]
    print(f"\n⏱️ 搜索总耗时: {total_seconds:.2f}s")
    for item in slowest:
        print(
            f"   {item['provider']}: {item['elapsed_seconds']:.2f}s "
            f"({item['status']})"
        )

    return {
        "tier1_high_trust": tier1,
        "tier2_supporting": tier2,
        "tier3_isolated": tier3,
        "tier3_isolated_count": len(tier3),
        "total_fetched": len(seen),
        "timings": {
            "total_seconds": round(total_seconds, 3),
            "providers": provider_timings,
            "web_provider": web_provider,
            "reported_cost_dollars": round(
                sum(float(item.get("cost_dollars") or 0) for item in provider_timings),
                6,
            ),
        },
        "provider_batches": provider_batches,
    }


# ──────────────────────────────────────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="运行已生成的通用检索计划")
    parser.add_argument("--plan", required=True, type=Path, help="01_search_plan.json 路径")
    parser.add_argument("--output", type=Path, help="完整检索结果输出路径")
    args = parser.parse_args()
    test_plan = json.loads(args.plan.read_text(encoding="utf-8"))
    output = asyncio.run(run_concurrent_multi_source_search(test_plan))

    print("\n" + "=" * 70)
    print(f"📊 跨源检索完成！总计捕获 {output['total_fetched']} 个独立检索条目")
    print(f"🛡️  Tier 1 绝对权威源: {len(output['tier1_high_trust'])} 条")
    print(f"📘 Tier 2 中可信佐证: {len(output['tier2_supporting'])} 条")
    print(f"🚫 Tier 3 已隔离: {output['tier3_isolated_count']} 条")
    print("=" * 70)

    print("\n🎓【Tier 1 绝对权威证据链】:")
    for idx, item in enumerate(output["tier1_high_trust"], 1):
        print(f"\n[{idx}] {item['title']}")
        print(f"    URL: {item['url']}")
        print(f"    摘要: {item['snippet'][:160]}")

    print("\n📘【Tier 2 中可信佐证源】:")
    for idx, item in enumerate(output["tier2_supporting"][:8], 1):
        print(f"\n[{idx}] {item['title']}")
        print(f"    URL: {item['url']}")
        print(f"    摘要: {item['snippet'][:120]}")

    if output["tier3_isolated_count"] > 0:
        print(f"\n🚫 已隔离 {output['tier3_isolated_count']} 条低可信内容。")

    output_path = args.output or args.plan.with_name("02_retrieval_standalone.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n💾 完整检索结果已保存至: {output_path}")
