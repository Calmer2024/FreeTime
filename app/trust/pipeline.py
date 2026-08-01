"""
pipeline_demo.py  —  MimoTrust 信源核验全流程端到端 PipeLine 脚本

【流程节点】
  1. 输入阶段 : 从 --input 读取通用案例 JSON
  2. 节点一 : 调用 MiMo LLM 生成网页、学术与百科检索计划
  3. 节点二 : 并发调用 Exa、OpenAlex、ArXiv、Wikipedia 等检索轨道
  4. 节点三 : 逐项核验原子主张、新闻事实和隐性观点
  5. 每次运行在 data/cases/<case_id>/runs/<run_id>/ 保存完整中间产物

【运行条件】
  - 在环境变量或 .env 中设置 MIMO_API_KEY 和 EXA_API_KEY
  - open-webSearch daemon 仅作为 Exa 不可用时的可选兜底
"""

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List
from openai import AsyncOpenAI

# 导入 demo_search 中的并发检索核心逻辑
from app.trust.demo_search import (
    load_env_value,
    run_concurrent_multi_source_search,
)
from app.trust.case_workspace import CaseRunWorkspace
from app.trust.evidence_policy import (
    apply_model_source_assessments,
    build_evidence_contracts,
    find_contract_satisfying_sources,
    has_decisive_source,
    has_propagation_evidence,
    has_sufficient_news_evidence,
    is_unstated_exclusivity_nitpick,
    profile_evidence_collection,
    requires_independent_corroboration,
    revise_event_contract_type,
    satisfies_evidence_contract,
)
from app.trust.evidence_triage import (
    compact_evidence_ledger,
    compact_ledger_for_prompt,
    decode_compact_triage_rows,
    decode_report_source_labels,
    normalize_triage_assessments,
    prepare_evidence_sources,
    select_triaged_evidence,
    semantic_assessments_for_report,
)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ==========================================
# 1. 配置参数
# ==========================================
MIMO_API_KEY = load_env_value("MIMO_API_KEY")
MIMO_BASE_URL = load_env_value("MIMO_BASE_URL") or "https://api.xiaomimimo.com/v1"
MIMO_PLANNING_MODEL = load_env_value("MIMO_PLANNING_MODEL") or "mimo-v2.5"
MIMO_TRIAGE_MODEL = load_env_value("MIMO_TRIAGE_MODEL") or "mimo-v2.5"
MIMO_REPORT_MODEL = load_env_value("MIMO_REPORT_MODEL") or "mimo-v2.5-pro"
MIMO_REPORT_THINKING = (
    "enabled"
    if (load_env_value("MIMO_REPORT_THINKING") or "disabled").lower() == "enabled"
    else "disabled"
)
MIMO_TIMEOUT_SECONDS = 75.0
TRIAGE_BATCH_SIZE = 12
TRIAGE_TIMEOUT_SECONDS = 60.0

REPORT_RISK_POSTURE_PROMPT = (
    "本系统服务于短视频和社交媒体内容的快速风险粗筛，首要目标是在用户转发前尽早暴露"
    "潜在虚假或误导风险。采取保守放行策略：只有证据链满足门槛时才能判为属实；当来源"
    "匿名、低质、疑似营销号、同稿转载、相互循环引用、缺少原始来源，或关键细节无法核验"
    "时，即使说法看似合理，也应标为待核实或缺乏证据。允许真实但暂时缺少可验证信源的"
    "信息被保守拦截为待核实，不得为了减少误报而放宽属实门槛。此风险偏好不授权在没有"
    "权威直接反证时判定虚假、捏造或误导，也不得推断传播者的主观造谣意图。"
)

LLM_CALL_TIMINGS: List[Dict[str, Any]] = []


def record_llm_timing(
    stage: str,
    started_at: float,
    completion,
    *,
    requested_model: str,
    thinking: str,
) -> None:
    usage = getattr(completion, "usage", None)
    completion_details = getattr(usage, "completion_tokens_details", None)
    elapsed_seconds = time.perf_counter() - started_at
    metric = {
        "stage": stage,
        "requested_model": requested_model,
        "response_model": getattr(completion, "model", None),
        "thinking": thinking,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "reasoning_tokens": getattr(completion_details, "reasoning_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }
    LLM_CALL_TIMINGS.append(metric)
    print(
        f"⏱️ {stage} LLM: {elapsed_seconds:.2f}s | "
        f"tokens={metric['total_tokens']}"
    )


def record_failed_llm_timing(
    stage: str,
    started_at: float,
    error: Exception,
    *,
    requested_model: str,
    thinking: str,
) -> None:
    elapsed_seconds = time.perf_counter() - started_at
    metric = {
        "stage": stage,
        "requested_model": requested_model,
        "thinking": thinking,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "status": "error",
        "error_type": type(error).__name__,
        "error": str(error)[:240],
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
    }
    LLM_CALL_TIMINGS.append(metric)
    print(f"⚠️ {stage} LLM failed after {elapsed_seconds:.2f}s: {error}")


def get_openai_client() -> AsyncOpenAI:
    if not MIMO_API_KEY:
        raise RuntimeError("未设置 MIMO_API_KEY 环境变量")
    return AsyncOpenAI(
        api_key=MIMO_API_KEY,
        base_url=MIMO_BASE_URL,
        timeout=MIMO_TIMEOUT_SECONDS,
        max_retries=0,
    )


def summarize_usage(
    llm_calls: List[Dict[str, Any]], search_timings: Dict[str, Any]
) -> Dict[str, Any]:
    """Aggregate already-reported usage without making additional API calls."""
    token_fields = ("prompt_tokens", "completion_tokens", "total_tokens")
    llm_summary = {field: 0 for field in token_fields}
    complete_usage_calls = 0
    for call in llm_calls:
        if all(isinstance(call.get(field), int) for field in token_fields):
            complete_usage_calls += 1
        for field in token_fields:
            value = call.get(field)
            if isinstance(value, int):
                llm_summary[field] += value

    llm_summary["reasoning_tokens"] = sum(
        int(call["reasoning_tokens"])
        for call in llm_calls
        if isinstance(call.get("reasoning_tokens"), int)
    )

    exa_cost = round(float(search_timings.get("reported_cost_dollars") or 0), 6)
    llm_summary.update(
        {
            "call_count": len(llm_calls),
            "calls_with_complete_usage": complete_usage_calls,
            "usage_complete": complete_usage_calls == len(llm_calls),
            "cost_dollars": None,
            "cost_note": "MiMo token pricing is not reported by the API or configured locally.",
        }
    )
    return {
        "llm": llm_summary,
        "search": {"exa_cost_dollars": exa_cost},
        "known_external_cost_dollars": exa_cost,
        "known_cost_scope": "Exa only; MiMo monetary cost is unavailable.",
    }


# ==========================================
# 2. 节点一：根据案例构造通用检索计划
# ==========================================
def _unique_strings(values: Any, limit: int) -> List[str]:
    if not isinstance(values, list):
        return []
    result = []
    for value in values:
        normalized = str(value).strip()
        if normalized and normalized not in result:
            result.append(normalized)
        if len(result) >= limit:
            break
    return result


def target_web_query_count(case_input: Dict[str, Any]) -> int:
    claim_count = sum(
        len(case_input.get(key, []))
        for key in ("原子主张", "新闻事实", "隐性观点")
    )
    return min(8, max(5, claim_count + 2))


def fallback_web_queries(
    case_input: Dict[str, Any], limit: int | None = None
) -> List[str]:
    query_limit = limit or target_web_query_count(case_input)
    topic = case_input.get("内容主题") or "该事件"
    claims = [
        *case_input.get("原子主张", []),
        *case_input.get("新闻事实", []),
        *case_input.get("隐性观点", []),
    ]
    candidates = [f"{claim} 权威来源 事实核查" for claim in claims]
    candidates.extend([
        f"{topic} 官方通报",
        f"{topic} 现行规定",
        f"{topic} 科学研究",
        f"{topic} 事实核查",
        f"{topic} 新闻来源",
    ])
    return _unique_strings(candidates, query_limit)


def _is_commentary_query(query: str) -> bool:
    commentary_markers = (
        "为什么",
        "为何",
        "原因分析",
        "舆情",
        "网友",
        "怎么看",
        "动机",
        "影响分析",
    )
    primary_markers = (
        "官方",
        "原始",
        "公告",
        "公示",
        "数据",
        "原文",
        "通报",
        "法规",
        "论文",
        "研究",
    )
    return any(marker in query for marker in commentary_markers) and not any(
        marker in query for marker in primary_markers
    )


def normalize_search_plan(
    raw_plan: Dict[str, Any], case_input: Dict[str, Any]
) -> Dict[str, Any]:
    query_limit = target_web_query_count(case_input)
    web_queries = [
        query
        for query in _unique_strings(raw_plan.get("web_queries"), query_limit)
        if not _is_commentary_query(query)
    ]
    for fallback_query in fallback_web_queries(case_input, query_limit):
        if len(web_queries) >= query_limit:
            break
        if fallback_query not in web_queries:
            web_queries.append(fallback_query)

    encyclopedia_topics = []
    raw_topics = raw_plan.get("encyclopedia_topics", [])
    if isinstance(raw_topics, list):
        for topic in raw_topics[:3]:
            if isinstance(topic, str):
                normalized = {"title": topic.strip(), "language": "zh"}
            elif isinstance(topic, dict):
                normalized = {
                    "title": str(topic.get("title", "")).strip(),
                    "language": str(topic.get("language", "zh")).strip() or "zh",
                }
            else:
                continue
            if normalized["title"]:
                encyclopedia_topics.append(normalized)

    claim_items = build_claim_items(case_input)
    claim_ids = {item["claim_id"] for item in claim_items}
    query_targets = []
    raw_targets = raw_plan.get("query_targets", [])
    if isinstance(raw_targets, list):
        for item in raw_targets:
            if not isinstance(item, dict):
                continue
            query = str(item.get("query", "")).strip()
            target_ids = [
                str(claim_id)
                for claim_id in item.get("claim_ids", [])
                if str(claim_id) in claim_ids
            ] if isinstance(item.get("claim_ids"), list) else []
            if query in web_queries and target_ids:
                query_targets.append(
                    {
                        "query": query,
                        "claim_ids": target_ids,
                        "intent": str(item.get("intent", "verification")),
                    }
                )

    targeted_queries = {item["query"] for item in query_targets}
    for index, query in enumerate(web_queries):
        if query in targeted_queries or not claim_items:
            continue
        claim_id = claim_items[min(index, len(claim_items) - 1)]["claim_id"]
        intent = (
            "primary_source"
            if any(marker in query for marker in ("官方", "原始", "公告", "数据", "原文"))
            else "verification"
        )
        query_targets.append(
            {"query": query, "claim_ids": [claim_id], "intent": intent}
        )

    return {
        "reasoning": str(raw_plan.get("reasoning", "")).strip()[:180],
        "web_queries": web_queries[:query_limit],
        "academic_queries": _unique_strings(
            raw_plan.get("academic_queries"), 2
        ),
        "encyclopedia_topics": encyclopedia_topics,
        "query_targets": query_targets,
        "evidence_contracts": build_evidence_contracts(
            claim_items,
            raw_contracts=raw_plan.get("evidence_contracts"),
        ),
    }


async def step1_construct_search_plan(
    case_input: Dict[str, Any], workspace: CaseRunWorkspace
) -> Dict[str, Any]:
    print("\n" + "=" * 70)
    print("📍 阶段 1: 根据案例构造通用检索计划...")
    print("=" * 70)

    client = get_openai_client()

    current_year = datetime.now(timezone.utc).year
    query_limit = target_web_query_count(case_input)
    query_example = json.dumps(
        [f"查询{index}" for index in range(1, query_limit + 1)],
        ensure_ascii=False,
    )
    system_prompt = (
        "你是信息检索与事实核查专家。根据任意领域的结构化案例，生成通用检索计划。\n"
        "【输出要求】\n"
        f"1. web_queries 必须恰好{query_limit}条中文自然语言查询，覆盖每一条原子主张、新闻事实和隐性观点。"
        "所有查询会一次性并发执行，不设计后续补搜。\n"
        f"2. 当前年份是{current_year}年。必须把‘今年、去年、往年、近日’解析成明确年份或时间范围，不能自行跳到无关年份。\n"
        "3. 至少一半网页查询用于寻找主管机构、原始公告、原始数据、法规原文、研究原文或一手通报。"
        "定量或比较主张必须在查询中写明年份、地区、对象、指标和统计口径，并优先寻找原始表格。\n"
        "4. 含‘多地、10余所、大多数、增长50%’等数量表述时，查询必须寻找可枚举清单或原始汇总。"
        "在事实数据确认前，不搜索原因分析、舆情评论、网友观点或动机猜测。\n"
        "5. 新闻事件查询必须包含关键人物/地点/事件；法规主张必须搜索具体管辖地、规则名称和现行状态；"
        "医学或科学主张必须搜索作用机制、权威机构和同行评议证据。\n"
        "6. academic_queries 提供0至2条英文查询，仅在学术证据有价值时生成。\n"
        "7. encyclopedia_topics 提供0至3个百科实体，language 使用 zh 或 en。\n"
        "8. query_targets 必须覆盖全部web_queries；claim_ids使用A1/N1/I1格式；intent使用primary_source或verification。\n"
        "9. evidence_contracts 必须逐项分析全部claim_id。claim_type从general_factual、current_event、historical_event、"
        "reported_claim、award_record、quantitative、quantitative_comparison、causal、legal、medical_scientific、"
        "interpretive_claim、context_integrity中选择。numeric_roles从year、date、amount、count、rate、rank、measurement、identifier中选择；"
        "年份、日期、编号不是定量指标。required_evidence只使用primary_source、canonical_record、historical_record、"
        "propagation_evidence、independent_corroboration、domain_authority、resolved_time_scope、structured_data、"
        "same_scope_comparison、enumeration_or_primary_summary、scope_evidence、original_context。\n"
        "10. 对‘网传称/视频称/有报道称’区分两件事：传播内容是否存在，以及内容本身是否真实；历史事件不套用突发新闻的双媒体门槛。\n"
        "reported_claim只用于‘某帖子、传言或报道是否确实存在/传播’这一元主张。句子即使提到‘研究表明、论文发表、机构称’，"
        "只要核验目标是研究结论或事实内容本身，就不能标为reported_claim。\n"
        "新闻事实只是输入分组，不代表事情发生在近期。人物生平、科学实验、发现过程、获奖经历等科学史或传记事件，"
        "即使句子没有写年份，也应标为historical_event，并至少安排一条原始论文、当事人记录或权威历史资料查询。\n"
        "11. 不得引入输入中不存在的领域、对象或案例。reasoning 不超过120个汉字。\n"
        "12. 只输出以下 JSON，不要输出 Markdown：\n"
        "{\n"
        '  "reasoning": "检索计划说明",\n'
        f'  "web_queries": {query_example},\n'
        '  "academic_queries": ["English academic query"],\n'
        '  "encyclopedia_topics": [{"title": "实体", "language": "zh"}],\n'
        '  "query_targets": [{"query": "查询1", "claim_ids": ["A1"], "intent": "primary_source"}],\n'
        '  "evidence_contracts": [{"claim_id": "A1", "claim_type": "award_record", '
        '"numeric_roles": ["year"], "risk_flags": [], "required_evidence": ["canonical_record"]}]\n'
        "}"
    )

    user_prompt = f"案例输入：\n{json.dumps(case_input, ensure_ascii=False, indent=2)}"

    try:
        llm_started_at = time.perf_counter()
        completion = await client.chat.completions.create(
            model=MIMO_PLANNING_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            response_format={"type": "json_object"},
            max_completion_tokens=1400,
            extra_body={"thinking": {"type": "disabled"}},
        )
        record_llm_timing(
            "query_construction",
            llm_started_at,
            completion,
            requested_model=MIMO_PLANNING_MODEL,
            thinking="disabled",
        )

        raw_plan = json.loads(completion.choices[0].message.content or "{}")
        search_plan = normalize_search_plan(raw_plan, case_input)
        search_plan["raw_model_plan"] = raw_plan

        print(f"💡 LLM 分析逻辑: {search_plan.get('reasoning', '无')}")
        print("🔍 网页检索 Query:")
        for idx, q in enumerate(search_plan["web_queries"], 1):
            print(f"   [{idx}] {q}")
        output_path = workspace.write_json("01_search_plan.json", search_plan)
        print(f"💾 检索计划已保存至: {output_path}")
        return search_plan

    except Exception as e:
        print(f"❌ 阶段 1 异常: {e}，使用通用保底检索计划")
        search_plan = normalize_search_plan({}, case_input)
        search_plan["reasoning"] = f"LLM检索计划失败，使用逐项主张保底查询：{e}"
        workspace.write_json("01_search_plan.json", search_plan)
        return search_plan


# ==========================================
# 3. 节点二：执行多源并发检索与分级隔离
# ==========================================
async def step2_execute_search(
    search_plan: Dict[str, Any], workspace: CaseRunWorkspace
) -> Dict[str, Any]:
    print("\n" + "=" * 70)
    print("📍 阶段 2: 正在进行全异步并发跨源检索与 Tier 自动隔离...")
    print("=" * 70)

    search_output = await run_concurrent_multi_source_search(search_plan)
    retrieval = {
        "search_plan": search_plan,
        "provider_batches": search_output["provider_batches"],
        "timings": search_output["timings"],
    }
    evidence = profile_evidence_collection({
        "tier1_high_trust": search_output["tier1_high_trust"],
        "tier2_supporting": search_output["tier2_supporting"],
        "tier3_isolated": search_output["tier3_isolated"],
        "evidence_contracts": search_plan.get("evidence_contracts", []),
        "query_targets": search_plan.get("query_targets", []),
        "counts": {
            "tier1": len(search_output["tier1_high_trust"]),
            "tier2": len(search_output["tier2_supporting"]),
            "tier3": len(search_output["tier3_isolated"]),
            "total_fetched": search_output["total_fetched"],
        },
    })
    retrieval_path = workspace.write_json("02_retrieval.json", retrieval)
    evidence_path = workspace.write_json("03_evidence.json", evidence)
    print(f"\n💾 原始检索已保存至: {retrieval_path}")
    print(f"💾 分级证据已保存至: {evidence_path}")
    return {"evidence": evidence, "timings": search_output["timings"]}


async def _triage_evidence_batch(
    client: AsyncOpenAI,
    batch_index: int,
    sources: List[Dict[str, Any]],
    claims_with_contracts: List[Dict[str, Any]],
) -> Dict[str, Any]:
    stage = f"evidence_triage_batch_{batch_index}"
    system_prompt = (
        "你是事实核查流水线的证据初筛器。只分析给定主张和搜索摘要，不生成最终真假结论，"
        "也不得用模型记忆补充摘要中没有的事实。逐条判断来源身份，以及它相对于每个主张的"
        "相关性、支持/反驳/背景/传播关系和直接性。搜索路由 target_claim_ids 只是提示，不是"
        "相关性结论；必须发现跨主张证据，并把误召回标为 irrelevant。未知域名不等于低质，"
        "特别检查隐性观点中的绝对化和跨场景外推：如果原始研究直接限定了实验对象、条件或"
        "适用范围，而隐性观点把它扩大到日常、全部或完全失效，应将该原始研究同时标为对该"
        "隐性观点 refutes/direct，因为它直接提供了范围边界证据。"
        "可根据 URL、标题和摘要识别陌生官方、规范或学术来源；但自媒体和评论不能作为事实"
        "支持，只能在适用时证明传播。必须为每个 source_id 返回一行，不写理由。"
        "只输出紧凑 JSON：{\"rows\":[[source_id,role,relations,strength]]}。"
        "role编码：C规范原始、O官方原始、I机构、A学术、M权威媒体、G普通媒体、"
        "R参考资料、S自媒体、U未知。relations是零到多个[claim_id,relation,directness]；"
        "relation编码：s支持、r反驳、c背景、p仅证明传播、x无关；directness编码："
        "d直接、i间接。strength编码：4决定性、3强、2中、1弱、0不可用。"
        "示例：{\"rows\":[[\"E001\",\"A\",[[\"A1\",\"s\",\"d\"]],3],"
        "[\"E002\",\"U\",[],0]]}。不得输出其他字段或解释。"
    )
    prompt_sources = []
    for source in sources:
        prompt_sources.append(
            {
                "id": source.get("id"),
                "title": str(source.get("title") or "")[:180],
                "url": source.get("url"),
                "snippet": str(source.get("snippet") or "")[:800],
                "provider": source.get("provider"),
                "search_query": source.get("search_query"),
                "target_claim_ids": source.get("target_claim_ids", []),
                "mechanical_profile": source.get("evidence_profile", {}),
            }
        )
    user_prompt = json.dumps(
        {"claims": claims_with_contracts, "sources": prompt_sources},
        ensure_ascii=False,
    )

    started_at = time.perf_counter()
    completion = None
    try:
        completion = await asyncio.wait_for(
            client.chat.completions.create(
                model=MIMO_TRIAGE_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.0,
                max_completion_tokens=900,
                response_format={"type": "json_object"},
                extra_body={"thinking": {"type": "disabled"}},
            ),
            timeout=TRIAGE_TIMEOUT_SECONDS,
        )
        raw_output = json.loads(completion.choices[0].message.content or "{}")
        raw_rows = raw_output.get("rows", [])
        if not isinstance(raw_rows, list):
            raise ValueError("triage response rows must be a list")
        raw_assessments = decode_compact_triage_rows(
            raw_rows,
            [str(source["id"]) for source in sources],
            [str(claim["claim_id"]) for claim in claims_with_contracts],
        )
        record_llm_timing(
            stage,
            started_at,
            completion,
            requested_model=MIMO_TRIAGE_MODEL,
            thinking="disabled",
        )
        return {
            "batch_index": batch_index,
            "status": "ok",
            "source_ids": [source["id"] for source in sources],
            "raw_model_output": raw_output,
            "assessments": raw_assessments,
        }
    except Exception as exc:
        if completion is None:
            record_failed_llm_timing(
                stage,
                started_at,
                exc,
                requested_model=MIMO_TRIAGE_MODEL,
                thinking="disabled",
            )
        else:
            record_llm_timing(
                stage,
                started_at,
                completion,
                requested_model=MIMO_TRIAGE_MODEL,
                thinking="disabled",
            )
            LLM_CALL_TIMINGS[-1].update(
                {
                    "status": "invalid_response",
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:240],
                }
            )
        return {
            "batch_index": batch_index,
            "status": "error",
            "source_ids": [source["id"] for source in sources],
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
            "raw_model_output": None,
            "assessments": [],
        }


async def step3_assess_evidence(
    case_input: Dict[str, Any],
    evidence: Dict[str, Any],
    workspace: CaseRunWorkspace,
) -> Dict[str, Any]:
    print("\n" + "=" * 70)
    print("阶段 3: 并发调用 MiMo 对全部候选进行 claim 级语义初筛...")
    print("=" * 70)

    sources = prepare_evidence_sources(evidence)
    claim_items = build_claim_items(case_input)
    contracts_by_id = {
        str(item.get("claim_id")): item
        for item in evidence.get("evidence_contracts", [])
        if isinstance(item, dict) and item.get("claim_id")
    }
    claims_with_contracts = [
        {
            **claim,
            "evidence_contract": contracts_by_id.get(claim["claim_id"], {}),
        }
        for claim in claim_items
    ]
    batches = [
        sources[index : index + TRIAGE_BATCH_SIZE]
        for index in range(0, len(sources), TRIAGE_BATCH_SIZE)
    ]
    client = get_openai_client()
    batch_results = await asyncio.gather(
        *(
            _triage_evidence_batch(client, index, batch, claims_with_contracts)
            for index, batch in enumerate(batches, 1)
        )
    )
    raw_assessments = [
        assessment
        for batch in batch_results
        for assessment in batch.get("assessments", [])
    ]
    assessed_sources = normalize_triage_assessments(
        sources,
        raw_assessments,
        [claim["claim_id"] for claim in claim_items],
    )
    ledger = compact_evidence_ledger(assessed_sources)
    status_counts = Counter(
        item.get("semantic_assessment", {}).get("triage_status", "fallback")
        for item in assessed_sources
    )
    recommendation_counts = Counter(
        item.get("semantic_assessment", {}).get("recommendation", "exclude")
        for item in assessed_sources
    )
    triage_result = {
        "case_id": workspace.case_id,
        "evidence_counts": evidence.get("counts", {}),
        "claim_count": len(claim_items),
        "candidate_count": len(sources),
        "batch_count": len(batch_results),
        "successful_batch_count": sum(
            batch.get("status") == "ok" for batch in batch_results
        ),
        "status_counts": dict(status_counts),
        "recommendation_counts": dict(recommendation_counts),
        "evidence_contracts": evidence.get("evidence_contracts", []),
        "query_targets": evidence.get("query_targets", []),
        "batches": batch_results,
        "evidence_ledger": ledger,
        "sources": assessed_sources,
    }
    output_path = workspace.write_json("03_evidence_assessed.json", triage_result)
    print(
        f"语义初筛完成: {len(sources)} 条候选, "
        f"{triage_result['successful_batch_count']}/{len(batch_results)} 批成功"
    )
    print(f"语义初筛产物: {output_path}")
    return triage_result


# ==========================================
# 4. 节点三：基于证据进行 LLM 核验并合成诊断报告
# ==========================================
def select_diverse_evidence(
    items: List[Dict[str, Any]], limit: int
) -> List[Dict[str, Any]]:
    """Prefer one strong Exa result per query before filling remaining slots."""
    candidates = [item for item in items if item.get("snippet") and item.get("url")]
    selected: List[Dict[str, Any]] = []
    selected_ids = set()
    seen_queries = set()

    for item in candidates:
        search_query = item.get("search_query")
        if not search_query or search_query in seen_queries:
            continue
        selected.append(item)
        selected_ids.add(id(item))
        seen_queries.add(search_query)
        if len(selected) >= limit:
            return selected

    for item in candidates:
        if id(item) in selected_ids:
            continue
        selected.append(item)
        if len(selected) >= limit:
            break
    return selected


def collect_report_source_ids(report_data: Dict[str, Any]) -> List[str]:
    source_ids: List[str] = []
    claim_checks = report_data.get("claim_checks", [])
    if isinstance(claim_checks, list):
        for item in claim_checks:
            if not isinstance(item, dict):
                continue
            item_source_ids = item.get("source_ids", [])
            if not isinstance(item_source_ids, list):
                continue
            for source_id in item_source_ids:
                normalized = str(source_id)
                if normalized not in source_ids:
                    source_ids.append(normalized)

    legacy_source_ids = report_data.get("source_ids", [])
    if isinstance(legacy_source_ids, list):
        for source_id in legacy_source_ids:
            normalized = str(source_id)
            if normalized not in source_ids:
                source_ids.append(normalized)
    return source_ids[:8]


def build_claim_items(case_input: Dict[str, Any]) -> List[Dict[str, str]]:
    claim_items = []
    groups = (
        ("A", "原子主张", case_input.get("原子主张", [])),
        ("N", "新闻事实", case_input.get("新闻事实", [])),
        ("I", "隐性观点", case_input.get("隐性观点", [])),
    )
    for prefix, category, claims in groups:
        for index, claim in enumerate(claims, 1):
            claim_items.append(
                {
                    "claim_id": f"{prefix}{index}",
                    "category": category,
                    "claim": claim,
                }
            )
    return claim_items


def derive_overall_verdict(claim_checks: List[Dict[str, Any]]) -> str:
    verdicts = [str(item.get("verdict")) for item in claim_checks]
    negative = {"捏造", "虚假"}
    positive = {"属实", "部分属实"}

    if any(verdict in negative for verdict in verdicts):
        if any(verdict in positive for verdict in verdicts):
            return "部分属实"
        return "谣言/虚假信息"
    if "误导" in verdicts:
        return "误导"
    if "部分属实" in verdicts:
        return "部分属实"
    if verdicts and all(verdict == "属实" for verdict in verdicts):
        return "属实"
    return "证据不足"


def normalize_report_data(
    raw_report: Dict[str, Any],
    claim_items: List[Dict[str, str]],
    evidence_by_id: Dict[str, Dict[str, str]],
    evidence_contracts: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    raw_checks = raw_report.get("claim_checks", [])
    checks_by_id = {
        str(item.get("claim_id")): item
        for item in raw_checks
        if isinstance(item, dict) and item.get("claim_id")
    } if isinstance(raw_checks, list) else {}
    contracts_by_id = {
        str(item.get("claim_id")): item
        for item in evidence_contracts or []
        if isinstance(item, dict) and item.get("claim_id")
    }

    normalized_checks = []
    news_verification_failed = False
    allowed_verdicts = {
        "捏造",
        "虚假",
        "误导",
        "待核实",
        "缺乏证据",
        "部分属实",
        "属实",
    }
    strong_verdicts = {"捏造", "虚假", "误导"}
    for expected in claim_items:
        raw_item = checks_by_id.get(expected["claim_id"], {})
        verdict = str(raw_item.get("verdict", "缺乏证据"))
        if verdict not in allowed_verdicts:
            verdict = "缺乏证据"
        basis = str(raw_item.get("basis", "现有证据不足以完成核验。"))
        raw_source_ids = raw_item.get("source_ids", [])
        valid_source_ids = []
        if isinstance(raw_source_ids, list):
            for source_id in raw_source_ids[:2]:
                normalized_id = str(source_id)
                if normalized_id in evidence_by_id and normalized_id not in valid_source_ids:
                    valid_source_ids.append(normalized_id)
        reviewed_source_ids = list(valid_source_ids)
        contract = revise_event_contract_type(
            contracts_by_id.get(expected["claim_id"]),
            raw_item.get("claim_type"),
        )
        positive_verdicts = {"属实", "部分属实"}
        is_reported_claim = bool(
            contract and contract.get("claim_type") == "reported_claim"
        )
        required_relations = (
            {"refutes"}
            if verdict in strong_verdicts
            else ({"propagation"} if is_reported_claim else {"supports"})
        )
        if (
            contract
            and verdict in strong_verdicts | positive_verdicts
            and not satisfies_evidence_contract(
                contract,
                valid_source_ids,
                evidence_by_id,
                required_relations=required_relations,
            )
        ):
            replacement_source_ids = find_contract_satisfying_sources(
                contract,
                evidence_by_id,
                required_relations,
            )
            if replacement_source_ids:
                for source_id in replacement_source_ids:
                    if source_id not in reviewed_source_ids:
                        reviewed_source_ids.append(source_id)
                valid_source_ids = replacement_source_ids

        if is_reported_claim and verdict in positive_verdicts:
            has_decisive = has_propagation_evidence(
                valid_source_ids,
                evidence_by_id,
                claim_id=expected["claim_id"],
            )
        else:
            has_decisive = has_decisive_source(
                valid_source_ids,
                evidence_by_id,
                claim_id=expected["claim_id"],
                required_relations=required_relations,
            )
        discrepancy_anchor = str(raw_item.get("discrepancy_anchor") or "").strip()
        anchor_was_requested = "discrepancy_anchor" in raw_item
        anchor_is_grounded = bool(
            discrepancy_anchor and discrepancy_anchor in expected["claim"]
        )
        has_material_refutation = has_decisive_source(
            valid_source_ids,
            evidence_by_id,
            claim_id=expected["claim_id"],
            required_relations={"refutes"},
        )
        non_material_partial = raw_item.get("material_discrepancy") is False
        ungrounded_partial = (
            raw_item.get("material_discrepancy") is True
            and anchor_was_requested
            and (not anchor_is_grounded or not has_material_refutation)
        )
        if (
            verdict == "部分属实"
            and (non_material_partial or ungrounded_partial)
            and has_decisive
        ):
            verdict = "属实"
            basis = (
                "现有直接证据支持原主张，所谓差异无法锚定到当前主张原文。"
                if ungrounded_partial
                else "现有直接证据支持原主张，补充差异不影响事实成立。"
            )
        if (
            verdict == "部分属实"
            and has_decisive
            and is_unstated_exclusivity_nitpick(
                expected["claim"],
                f"{basis} {raw_item.get('discrepancy') or ''}",
            )
        ):
            verdict = "属实"
            basis = "现有直接证据支持原主张；补充说明未构成对原主张的实质反驳。"
        if verdict in strong_verdicts and not has_decisive:
            verdict = "缺乏证据"
            basis = "缺少能够直接支持强判定的权威直接证据。"
        news_requires_corroboration = expected["category"] == "新闻事实" and (
            contract is None or requires_independent_corroboration(contract)
        )
        if news_requires_corroboration and not has_sufficient_news_evidence(
            valid_source_ids,
            evidence_by_id,
            claim_id=expected["claim_id"],
        ):
            if verdict != "缺乏证据":
                verdict = "待核实"
                basis = (
                    "现有依据仅为非官方或可能转载的报道，缺少官方通报或两个可确认独立的权威来源。"
                )
            news_verification_failed = True

        contract_flags = set(contract.get("risk_flags", [])) if contract else set()
        requires_strict_contract = bool(
            contract_flags & {"quantified", "comparative", "enumeration", "causal"}
        ) or bool(
            contract
            and contract.get("claim_type")
            in {
                "legal",
                "medical_scientific",
                "award_record",
                "historical_event",
                "context_integrity",
            }
        )
        if (
            verdict in strong_verdicts | positive_verdicts
            and requires_strict_contract
            and not satisfies_evidence_contract(
                contract,
                valid_source_ids,
                evidence_by_id,
                required_relations=(
                    {"refutes"} if verdict in strong_verdicts else {"supports"}
                ),
            )
        ):
            verdict = "缺乏证据"
            basis = "现有来源未满足该主张所需的原始数据、统一口径或领域权威证据。"

        if verdict in {"待核实", "缺乏证据"}:
            valid_source_ids = []

        normalized_checks.append(
            {
                **expected,
                "verdict": verdict,
                "basis": basis,
                "source_ids": valid_source_ids,
                "reviewed_source_ids": reviewed_source_ids,
                "effective_claim_type": (
                    contract.get("claim_type") if contract else None
                ),
            }
        )

    uncertainties = raw_report.get("uncertainties", [])
    if not isinstance(uncertainties, list):
        uncertainties = []
    overall_verdict = derive_overall_verdict(normalized_checks)

    normalized = {
        "overall_verdict": overall_verdict,
        "claim_checks": normalized_checks,
        "uncertainties": [str(item) for item in uncertainties[:3] if item],
    }
    if news_verification_failed:
        news_uncertainty = "新闻事件缺少官方通报或可确认独立的一手权威来源。"
        if news_uncertainty not in normalized["uncertainties"]:
            normalized["uncertainties"] = (
                [news_uncertainty, *normalized["uncertainties"]]
            )[:3]
    verdict_counts = Counter(item["verdict"] for item in normalized_checks)
    verdict_order = (
        "捏造",
        "虚假",
        "误导",
        "部分属实",
        "属实",
        "待核实",
        "缺乏证据",
    )
    verdict_summary = "、".join(
        f"{verdict_counts[verdict]}项{verdict}"
        for verdict in verdict_order
        if verdict_counts[verdict]
    )
    normalized["conclusion"] = (
        f"共核验{len(normalized_checks)}项：{verdict_summary}。"
        if verdict_summary
        else "现有证据不足以形成完整结论。"
    )
    normalized["source_ids"] = collect_report_source_ids(normalized)
    return normalized


def render_report_markdown(
    report_data: Dict[str, Any],
    evidence_by_id: Dict[str, Dict[str, str]],
    topic: str = "",
) -> str:
    lines = []
    if topic:
        lines.extend([f"# {topic}", ""])
    lines.extend([
        "## 核验结论",
        (
            f"**{report_data.get('overall_verdict', '待核验')}**："
            f"{report_data.get('conclusion', '证据不足，暂无法形成可靠结论。')}"
        ),
        "",
        "## 逐项核对",
    ])

    claim_checks = report_data.get("claim_checks", [])
    if not isinstance(claim_checks, list):
        claim_checks = []
    check_count = 0
    for item in claim_checks:
        if not isinstance(item, dict):
            continue
        claim = str(item.get("claim", "相关主张")).strip()
        category = str(item.get("category", "主张")).strip()
        verdict = str(item.get("verdict", "证据不足")).strip()
        basis = str(item.get("basis", "现有摘要无法直接支持。"))
        lines.append(f"- **{category} · {verdict}**：{claim}。{basis}")
        check_count += 1
    if check_count == 0:
        lines.append("- **证据不足**：现有摘要无法直接完成逐项核对。")

    lines.extend(["", "## 关键依据"])
    source_ids = collect_report_source_ids(report_data)
    valid_sources = []
    for source_id in source_ids[:8]:
        source = evidence_by_id.get(str(source_id))
        if source and source not in valid_sources:
            valid_sources.append(source)
    if valid_sources:
        for source in valid_sources:
            assessments = source.get("claim_assessments", {})
            is_propagation_only = bool(assessments) and all(
                assessment.get("relation") == "propagation"
                for assessment in assessments.values()
            )
            prefix = "传播样本，仅证明说法存在：" if is_propagation_only else ""
            lines.append(f"- {prefix}[{source['title']}]({source['url']})")
    else:
        lines.append("- 现有证据未形成可直接引用的来源。")

    lines.extend(["", "## 不确定项"])
    uncertainties = report_data.get("uncertainties", [])
    if not isinstance(uncertainties, list):
        uncertainties = []
    cleaned_uncertainties = [str(item).strip() for item in uncertainties[:3] if item]
    if cleaned_uncertainties:
        lines.extend(f"- {item}" for item in cleaned_uncertainties)
    else:
        lines.append("- 无")

    return "\n".join(lines)


async def step4_generate_report(
    case_input: Dict[str, Any],
    evidence: Dict[str, Any],
    workspace: CaseRunWorkspace,
) -> Dict[str, Any]:
    print("\n" + "=" * 70)
    print("📍 阶段 4: 调用 MiMo LLM 进行证据推理与最终诊断报告合成...")
    print("=" * 70)

    client = get_openai_client()

    claim_items = build_claim_items(case_input)
    evidence_contracts = evidence.get("evidence_contracts", [])
    contracts_by_id = {
        str(item.get("claim_id")): item
        for item in evidence_contracts
        if isinstance(item, dict) and item.get("claim_id")
    }
    claims_with_contracts = [
        {
            **claim,
            "evidence_contract": contracts_by_id.get(claim["claim_id"], {}),
        }
        for claim in claim_items
    ]
    evidence_items = []
    evidence_by_id: Dict[str, Dict[str, str]] = {}
    assessed_sources = evidence.get("sources", [])
    selected_candidates = select_triaged_evidence(
        assessed_sources,
        [claim["claim_id"] for claim in claim_items],
    )
    for item in selected_candidates:
        tier = {
            "TIER_1": "T1",
            "TIER_2": "T2",
            "TIER_3": "T3",
        }.get(str(item.get("tier")), "T3")
        source_id = str(item.get("id"))
        profile = item.get("evidence_profile", {})
        semantic_assessment = item.get("semantic_assessment", {})
        if any(
            relation.get("directness") == "direct"
            for relation in semantic_assessment.get("claim_relations", [])
        ):
            snippet_limit = 700
        elif profile.get("evidence_type") in {
            "research",
            "primary_document",
            "structured_data",
        }:
            snippet_limit = 560
        else:
            snippet_limit = 420
        normalized = {
            "id": source_id,
            "tier": tier,
            "title": " ".join(
                str(item.get("title", "未命名来源")).split()
            )[:140],
            "url": str(item.get("url", "")),
            "snippet": str(item.get("snippet", ""))[:snippet_limit],
            "provider": item.get("provider"),
            "search_query": item.get("search_query"),
            "published_date": item.get("published_date"),
            "target_claim_ids": semantic_assessment.get(
                "relevant_claim_ids", item.get("target_claim_ids", [])
            ),
            "evidence_profile": profile,
            "semantic_assessment": semantic_assessment,
        }
        evidence_items.append(normalized)
        evidence_by_id[source_id] = normalized

    compact_ledger = evidence.get("evidence_ledger", [])
    prompt_ledger = compact_ledger_for_prompt(assessed_sources)
    prompt_evidence_items = []
    for item in evidence_items:
        semantic = item.get("semantic_assessment", {})
        prompt_evidence_items.append(
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "url": item.get("url"),
                "snippet": item.get("snippet"),
                "published_date": item.get("published_date"),
                "target_claim_ids": item.get("target_claim_ids", []),
                "semantic_assessment": {
                    key: semantic.get(key)
                    for key in (
                        "source_role",
                        "relevant_claim_ids",
                        "claim_relations",
                        "evidence_strength",
                    )
                },
            }
        )

    current_date = datetime.now(timezone.utc).date().isoformat()
    system_prompt = (
        f"你是严谨、直截了当的通用事实核查员。当前日期是{current_date}。"
        f"{REPORT_RISK_POSTURE_PROMPT}"
        "可以使用常识理解术语、时间、来源身份和语义边界，"
        "但事实判定只能依据本次给定证据，不得用模型记忆补充缺失事实或替代引用。"
        "必须按照claim_id逐项核验全部原子主张、新闻事实和隐性观点，不回避明确结论。"
        "候选中的semantic_assessment已由全量初筛给出来源角色、claim关系和直接性；"
        "请复核其与摘要是否一致，只为实际引用来源输出紧凑source_labels。"
        "self_media和commentary只能说明说法正在传播，不能作为事实结论的关键依据。"
        "新闻报道只能证明事件报道内容，不能单独证明医学机制或法规效力。"
        "判定规则：权威证据直接反驳的具体事实可判为‘虚假’；凭空编造且被证据否定的机制可判为‘捏造’；"
        "以偏概全、混淆概念或跨对象外推可判为‘误导’；仅仅未找到证据时判为‘缺乏证据’，"
        "不能仅凭缺少证据判定捏造。可称某说法为‘谣言/虚假信息’，但不得推断传播者的主观造谣意图。"
        "不得把一个对象、剂量、暴露途径、地区或时间的证据外推到另一个对象。机制证据不能单独证明实际事件。"
        "医学健康主张优先使用官方卫生机构、毒理资料和同行评议研究；法规主张必须核对官方文本、管辖地区、"
        "适用场景和有效状态，‘多地规定’至少需要两个不同地区的官方直接证据，否则只能部分支持或证据不足。"
        "新闻事实不得因为搜到一篇描述相同的文章就判为属实。匿名、缺少时间地点、没有原始采访或只有聚合转载的事件应判为‘待核实’；"
        "判为属实至少需要一个直接官方来源，或两个来源域名不同、内容不重复的T1权威媒体来源。多个循环转载不算独立证据。"
        "但historical_event可以由原始论文、当事人记录或权威历史资料直接证明；reported_claim只核验相关说法是否实际传播，"
        "不能把‘存在这条网传内容’偷换成‘网传内容本身属实’。"
        "每项都必须检查evidence_contract。定量比较必须有原始或结构化数据且统计口径一致；"
        "‘多地、10余所、大多数’等数量主张必须有可枚举清单或官方原始汇总，否则判为缺乏证据。"
        "只判断claim字段明确表达的命题。原主张没有‘唯一、完全、仅凭、最终’等限定时，不得自行添加这些限定再以其不成立为由降级。"
        "非实质性的背景补充写入basis，但不改变属实判定。对隐性观点中的绝对化、跨范围外推或偷换因果，"
        "若权威证据直接显示真实范围更窄，应判为误导，而不是仅写缺乏证据。"
        "部分属实必须在basis和discrepancy中明确指出原主张哪一个实质性部分错误或未获支持，并将material_discrepancy设为true；"
        "同时给出discrepancy_anchor，它必须是从当前claim逐字复制的一段连续原文，禁止引用其他claim中的词句；"
        "source_ids中还必须同时包含支持其余部分的直接证据，以及直接反驳该锚点的权威证据；"
        "后者在semantic_assessment中的relation必须为refutes。若只有supports/context而没有直接refutes，不得判部分属实；"
        "若只是术语解释、目的的同义改写或非实质背景差异，material_discrepancy必须为false并判为属实。"
        "不要写动机或道德评价。每项判定必须在自身source_ids中列出1至2个直接证据；"
        "判为捏造、虚假或误导时至少需要一个权威直接证据，否则降低判定强度。"
        "claim_checks还必须给出复核后的claim_type；只允许在current_event、historical_event、reported_claim三者之间纠正事件类型，"
        "不得降低其他主张类型的证据门槛。canonical_record必须来自奖项、机构、法规等事实的原始发布者，次级机构转述不算。"
        "target_claim_ids只是搜索路由提示，semantic_assessment.relevant_claim_ids才是初筛后的适用范围。"
        "强证据契约尚未满足时，必须检查全部完整候选中是否存在原始发布者、原始论文或规范记录。"
        "只输出 JSON，不输出 Markdown、URL 或额外解释。JSON 字段必须为："
        "overall_verdict（属实、谣言/虚假信息、误导、部分属实、证据不足之一）；conclusion（不超过80字）；"
        "claim_checks（数量必须与输入待核验项一致，每项含claim_id、claim_type、verdict、basis、"
        "material_discrepancy、discrepancy_anchor、discrepancy、source_ids；"
        "verdict从捏造、虚假、误导、待核实、缺乏证据、部分属实、属实中选择；basis不超过70字；"
        "source_ids只能使用给定id）；"
        "source_labels必须为每个claim_checks.source_ids引用返回一行"
        "[source_id,claim_id,role,relation,directness]，用于紧凑复核初筛标签。"
        "role编码C规范原始、O官方原始、I机构、A学术、M权威媒体、G普通媒体、"
        "R参考、S自媒体、U未知；relation编码s支持、r反驳、c背景、p传播、x无关；"
        "directness编码d直接、i间接。必须相对于当前claim完整语义判断；若claim含完全、"
        "唯一或排他归因，而来源直接证明存在另一制度性来源，应标为r/d。"
        "uncertainties（最多2项，每项不超过45字）。"
    )
    system_prompt += (
        "你还会收到全部搜索结果的紧凑证据账本，以及经过语义初筛后保留的完整候选。"
        "账本用于了解检索覆盖、反证和排除原因，不能仅凭账本形成或引用事实结论；"
        "source_ids只能引用完整候选证据中存在的id。初筛标签是可复核建议，不是最终事实。"
        "对于隐性观点的绝对化或跨场景外推，原始研究若直接给出更窄的对象、实验条件或适用"
        "范围，它就是可引用的 scope_evidence，应对该隐性观点评为 refutes/direct；不要只引用"
        "媒体对研究范围的二手解释。"
    )

    user_prompt = (
        "案例主题与待核验项：\n"
        f"{json.dumps({'内容主题': case_input.get('内容主题'), 'claims': claims_with_contracts}, ensure_ascii=False)}\n\n"
        "候选证据及多维画像：\n"
        f"{json.dumps(prompt_evidence_items, ensure_ascii=False)}\n\n"
        "全部搜索结果的紧凑证据账本（仅供覆盖审计，不可直接引用）：\n"
        f"{json.dumps(prompt_ledger, ensure_ascii=False)}"
    )

    try:
        llm_started_at = time.perf_counter()
        completion = await client.chat.completions.create(
            model=MIMO_REPORT_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_completion_tokens=3000,
            response_format={"type": "json_object"},
            extra_body={"thinking": {"type": MIMO_REPORT_THINKING}},
        )
        record_llm_timing(
            "report_generation",
            llm_started_at,
            completion,
            requested_model=MIMO_REPORT_MODEL,
            thinking=MIMO_REPORT_THINKING,
        )

        raw_report = json.loads(completion.choices[0].message.content or "{}")
        triage_source_assessments = semantic_assessments_for_report(
            selected_candidates
        )
        report_source_labels = decode_report_source_labels(
            raw_report.get("source_labels", []),
            list(evidence_by_id),
            [claim["claim_id"] for claim in claim_items],
        )
        corrected_pairs = {
            (item["source_id"], item["claim_id"]) for item in report_source_labels
        }
        effective_source_assessments = [
            item
            for item in triage_source_assessments
            if (item["source_id"], item["claim_id"]) not in corrected_pairs
        ] + report_source_labels
        evidence_by_id = apply_model_source_assessments(
            evidence_by_id,
            effective_source_assessments,
            claim_checks=raw_report.get("claim_checks", []),
        )
        report_data = normalize_report_data(
            raw_report,
            claim_items,
            evidence_by_id,
            evidence_contracts=evidence_contracts,
        )
        report_md = render_report_markdown(
            report_data,
            evidence_by_id,
            topic=case_input.get("内容主题", ""),
        )

        # Persist the final structured and rendered report.
        step3_result = {
            "case_id": workspace.case_id,
            "input": case_input,
            "evidence_counts": evidence.get("evidence_counts", {}),
            "evidence_reviewed_count": len(compact_ledger),
            "evidence_selected_count": len(evidence_items),
            "evidence_used": list(evidence_by_id.values()),
            "evidence_ledger": compact_ledger,
            "evidence_contracts": evidence_contracts,
            "raw_model_report": raw_report,
            "effective_source_assessments": effective_source_assessments,
            "structured_report": report_data,
            "final_report": report_md,
        }
        json_path = workspace.write_json("04_report.json", step3_result)
        markdown_path = workspace.write_text("04_report.md", report_md)

        print(f"\n💾 阶段 4 最终诊断报告已成功保存至:")
        print(f"   - JSON 路径: {json_path}")
        print(f"   - Markdown 路径: {markdown_path}")

        return step3_result

    except Exception as e:
        print(f"❌ 阶段 4 发生异常: {e}")
        raise


# ==========================================
# 5. 主流程入口
# ==========================================
async def run_pipeline(
    input_path: Path,
    data_root: Path = Path("data/cases"),
) -> CaseRunWorkspace:
    """Run one case and persist every stage in an isolated run directory."""
    LLM_CALL_TIMINGS.clear()
    workspace, case_input = CaseRunWorkspace.create(input_path, data_root=data_root)
    print(f"启动 MimoTrust 流水线: {workspace.case_id}/{workspace.run_id}")
    print(f"本次运行目录: {workspace.run_dir}")

    pipeline_started_at = time.perf_counter()
    stage_timings: Dict[str, float] = {}
    search_result: Dict[str, Any] = {}
    triage_result: Dict[str, Any] = {}

    try:
        stage_started_at = time.perf_counter()
        search_plan = await step1_construct_search_plan(case_input, workspace)
        stage_timings["search_plan"] = round(
            time.perf_counter() - stage_started_at, 3
        )

        stage_started_at = time.perf_counter()
        search_result = await step2_execute_search(search_plan, workspace)
        stage_timings["retrieval"] = round(
            time.perf_counter() - stage_started_at, 3
        )

        stage_started_at = time.perf_counter()
        triage_result = await step3_assess_evidence(
            case_input, search_result["evidence"], workspace
        )
        stage_timings["evidence_triage"] = round(
            time.perf_counter() - stage_started_at, 3
        )

        stage_started_at = time.perf_counter()
        report_result = await step4_generate_report(
            case_input, triage_result, workspace
        )
        stage_timings["report_generation"] = round(
            time.perf_counter() - stage_started_at, 3
        )

        total_seconds = round(time.perf_counter() - pipeline_started_at, 3)
        report_markdown = report_result["final_report"]
        usage_summary = summarize_usage(
            LLM_CALL_TIMINGS, search_result.get("timings", {})
        )
        timing_report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "case_id": workspace.case_id,
            "run_id": workspace.run_id,
            "total_seconds": total_seconds,
            "stages": stage_timings,
            "llm_calls": list(LLM_CALL_TIMINGS),
            "search": search_result.get("timings", {}),
            "usage_summary": usage_summary,
            "output": {
                "report_characters": len(report_markdown),
                "web_query_count": len(search_plan.get("web_queries", [])),
                "academic_query_count": len(
                    search_plan.get("academic_queries", [])
                ),
                "encyclopedia_topic_count": len(
                    search_plan.get("encyclopedia_topics", [])
                ),
                "evidence_reviewed_count": report_result.get(
                    "evidence_reviewed_count", 0
                ),
                "evidence_selected_count": report_result.get(
                    "evidence_selected_count", 0
                ),
            },
        }
        workspace.write_json("05_timings.json", timing_report)
        workspace.finalize(
            {
                "status": "completed",
                "total_seconds": total_seconds,
                "overall_verdict": report_result["structured_report"].get(
                    "overall_verdict", "证据不足"
                ),
            }
        )

        print("\n流水线耗时汇总")
        for stage, elapsed in stage_timings.items():
            print(f"   {stage}: {elapsed:.2f}s")
        print(f"   total: {total_seconds:.2f}s")
        print(f"完整产物目录: {workspace.run_dir}")
        print("\n" + "=" * 70)
        print("流水线执行成功，最终报告如下：")
        print("=" * 70 + "\n")
        print(report_markdown)
        return workspace
    except Exception as exc:
        total_seconds = round(time.perf_counter() - pipeline_started_at, 3)
        timing_report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "case_id": workspace.case_id,
            "run_id": workspace.run_id,
            "status": "failed",
            "total_seconds": total_seconds,
            "stages": stage_timings,
            "llm_calls": list(LLM_CALL_TIMINGS),
            "search": search_result.get("timings", {}),
            "usage_summary": summarize_usage(
                LLM_CALL_TIMINGS, search_result.get("timings", {})
            ),
        }
        workspace.write_json("05_timings.json", timing_report)
        workspace.write_json(
            "run_error.json",
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "case_id": workspace.case_id,
                "run_id": workspace.run_id,
                "total_seconds": total_seconds,
                "stages": stage_timings,
                "llm_calls": list(LLM_CALL_TIMINGS),
                "search": search_result.get("timings", {}),
                "usage_summary": summarize_usage(
                    LLM_CALL_TIMINGS, search_result.get("timings", {})
                ),
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
        workspace.finalize(
            {
                "status": "failed",
                "total_seconds": total_seconds,
                "error_type": type(exc).__name__,
            }
        )
        print(f"流水线失败，诊断产物保存在: {workspace.run_dir}")
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the generic MimoTrust fact-checking pipeline."
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Path to a case input JSON file.",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path("data/cases"),
        help="Root directory for canonical case inputs and run artifacts.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(run_pipeline(args.input, args.data_root))


if __name__ == "__main__":
    main()
