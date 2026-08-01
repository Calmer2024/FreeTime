"""Generic evidence contracts, source profiling, and report candidate selection."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlsplit


OFFICIAL_HOST_SUFFIXES = (
    "gov.cn",
    "gov",
    "mil.cn",
    "who.int",
    "fda.gov",
    "cdc.gov",
    "epa.gov",
    "nih.gov",
    "usda.gov",
    "un.org",
    "unicef.org",
    "unicef.cn",
    "unesco.org",
)
INSTITUTIONAL_HOST_SUFFIXES = ("edu.cn", "ac.cn", "org.cn")
ACADEMIC_HOST_SUFFIXES = (
    "doi.org",
    "arxiv.org",
    "semanticscholar.org",
    "acpjournals.org",
    "nature.com",
    "science.org",
    "sciencedirect.com",
    "springer.com",
    "wiley.com",
)
AUTHORITATIVE_NEWS_HOSTS = (
    "news.cn",
    "xinhuanet.com",
    "people.com.cn",
    "cctv.com",
    "chinanews.com.cn",
    "cnr.cn",
    "gmw.cn",
    "ce.cn",
)
INSTITUTIONAL_TITLE_MARKERS = (
    "教育考试院",
    "招生办公室",
    "招生委员会",
    "人民法院",
    "人民检察院",
    "卫生健康委员会",
    "疾病预防控制中心",
    "研究所",
    "大学招生",
    "学院招生",
)
COMMENTARY_MARKERS = (
    "滑铁卢",
    "崩盘",
    "遇冷",
    "引发热议",
    "网友",
    "莫非",
    "原因分析",
    "怎么看",
)
EXCLUSIVITY_MARKERS = (
    "完全",
    "唯一",
    "仅凭",
    "仅靠",
    "全部",
    "纯粹",
    "一概",
    "必然",
    "百分之百",
    "高度精准",
    "完全自主",
)
REPORTED_CLAIM_MARKERS = (
    "网传",
    "传言称",
    "视频称",
    "帖子称",
    "有报道称",
    "网络消息称",
)
YEAR_PATTERN = re.compile(r"(?<!\d)((?:1[5-9]|20|21)\d{2})年")
ALLOWED_CLAIM_TYPES = {
    "general_factual",
    "current_event",
    "historical_event",
    "reported_claim",
    "award_record",
    "quantitative",
    "quantitative_comparison",
    "causal",
    "legal",
    "medical_scientific",
    "interpretive_claim",
    "context_integrity",
}
ALLOWED_RISK_FLAGS = {
    "relative_time",
    "historical",
    "reported_claim",
    "quantified",
    "comparative",
    "enumeration",
    "causal",
    "exclusive_scope",
    "context_sensitive",
}
ALLOWED_NUMERIC_ROLES = {
    "year",
    "date",
    "amount",
    "count",
    "rate",
    "rank",
    "measurement",
    "identifier",
}
ALLOWED_EVIDENCE_REQUIREMENTS = {
    "primary_source",
    "canonical_record",
    "historical_record",
    "propagation_evidence",
    "independent_corroboration",
    "domain_authority",
    "resolved_time_scope",
    "structured_data",
    "same_scope_comparison",
    "enumeration_or_primary_summary",
    "scope_evidence",
    "original_context",
}


def _host_matches(host: str, suffixes: tuple[str, ...]) -> bool:
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in suffixes)


def _unique_strings(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    result = []
    for value in values:
        normalized = str(value).strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _claim_risk_flags(claim: str, current_year: int) -> list[str]:
    flags = []
    if re.search(r"今年|去年|往年|近日|日前|当前|目前|现行|最新", claim):
        flags.append("relative_time")
    claim_without_years = YEAR_PATTERN.sub("", claim)
    if re.search(
        r"\d|[一二三四五六七八九十百千万两]+(?:余|多)?(?:个|所|地|名|人|项|倍|成)",
        claim_without_years,
    ):
        flags.append("quantified")
    if re.search(r"反超|超过|高于|低于|上涨|下降|下滑|增长|减少|稳定|相比|对比|排名|位次", claim):
        flags.append("comparative")
    if re.search(r"多地|多所|多个|多人|十余|\d+余|大多数|多数|超过\d+", claim):
        flags.append("enumeration")
    if re.search(r"导致|引发|因此|原因|归因|影响", claim):
        flags.append("causal")
    explicit_years = [int(value) for value in YEAR_PATTERN.findall(claim)]
    if explicit_years and min(explicit_years) <= current_year - 2:
        flags.append("historical")
    if any(marker in claim for marker in REPORTED_CLAIM_MARKERS):
        flags.append("reported_claim")
    if any(marker in claim for marker in EXCLUSIVITY_MARKERS):
        flags.append("exclusive_scope")
    if re.search(r"断章取义|原话|原文|截取|删减|上下文|完整语境", claim):
        flags.append("context_sensitive")
    return flags


def _claim_type(category: str, claim: str, risk_flags: list[str]) -> str:
    if re.search(r"诺贝尔|获奖|奖项|授予.{0,12}奖|荣获", claim):
        return "award_record"
    if "reported_claim" in risk_flags:
        return "reported_claim"
    if "historical" in risk_flags:
        return "historical_event"
    if "quantified" in risk_flags and "comparative" in risk_flags:
        return "quantitative_comparison"
    if re.search(r"法规|法律|条例|规定|禁止|违法|证据效力|债务|抵押", claim):
        return "legal"
    if re.search(r"中毒|疾病|医学|心理|健康|有害|治疗|症状|吸收", claim):
        return "medical_scientific"
    if category == "隐性观点":
        return "interpretive_claim"
    if "relative_time" in risk_flags:
        return "current_event"
    if "causal" in risk_flags:
        return "causal"
    if "quantified" in risk_flags:
        return "quantitative"
    return "general_factual"


def build_evidence_contracts(
    claim_items: list[dict[str, str]],
    raw_contracts: Any = None,
    current_year: int | None = None,
) -> list[dict[str, Any]]:
    """Build stable evidence requirements while preserving useful LLM hints."""
    year = current_year or datetime.now(timezone.utc).year
    raw_by_id = {
        str(item.get("claim_id")): item
        for item in raw_contracts or []
        if isinstance(item, dict) and item.get("claim_id")
    }
    contracts = []
    for claim_item in claim_items:
        claim_id = claim_item["claim_id"]
        claim = claim_item["claim"]
        category = claim_item["category"]
        raw = raw_by_id.get(claim_id, {})
        detected_risk_flags = _claim_risk_flags(claim, year)
        risk_flags = [
            flag
            for flag in _unique_strings(raw.get("risk_flags"))
            if flag in ALLOWED_RISK_FLAGS
        ]
        for flag in detected_risk_flags:
            if flag not in risk_flags:
                risk_flags.append(flag)

        raw_claim_type = str(raw.get("claim_type") or "")
        if (
            raw_claim_type == "reported_claim"
            and "reported_claim" not in detected_risk_flags
        ):
            raw_claim_type = ""
        claim_type = (
            raw_claim_type
            if raw_claim_type in ALLOWED_CLAIM_TYPES
            else _claim_type(category, claim, risk_flags)
        )
        numeric_roles = [
            role
            for role in _unique_strings(raw.get("numeric_roles"))
            if role in ALLOWED_NUMERIC_ROLES
        ]
        quantitative_roles = {"amount", "count", "rate", "rank", "measurement"}
        if YEAR_PATTERN.search(claim) and "year" not in numeric_roles:
            numeric_roles.append("year")
        if (
            "quantified" in detected_risk_flags
            and not any(role in quantitative_roles for role in numeric_roles)
        ):
            numeric_roles.append("measurement")
        if any(role in quantitative_roles for role in numeric_roles):
            if "quantified" not in risk_flags:
                risk_flags.append("quantified")
        elif (
            "quantified" not in detected_risk_flags
            and set(numeric_roles).issubset({"year", "date", "identifier"})
        ):
            risk_flags = [flag for flag in risk_flags if flag != "quantified"]

        required_evidence = [
            requirement
            for requirement in _unique_strings(raw.get("required_evidence"))
            if requirement in ALLOWED_EVIDENCE_REQUIREMENTS
        ]
        if claim_type not in {"award_record", "legal"}:
            required_evidence = [
                item for item in required_evidence if item != "canonical_record"
            ]
        if claim_type == "current_event":
            required_evidence = [
                item
                for item in required_evidence
                if item not in {"historical_record", "propagation_evidence"}
            ]
        elif claim_type == "historical_event":
            required_evidence = [
                item
                for item in required_evidence
                if item not in {"independent_corroboration", "propagation_evidence"}
            ]
        elif claim_type == "reported_claim":
            required_evidence = [
                item
                for item in required_evidence
                if item not in {"independent_corroboration", "historical_record"}
            ]
        else:
            required_evidence = [
                item for item in required_evidence if item != "propagation_evidence"
            ]
        if claim_type == "reported_claim":
            default_requirements = ["propagation_evidence"]
        else:
            default_requirements = ["primary_source"]
        if claim_type == "award_record":
            default_requirements.append("canonical_record")
        if claim_type == "historical_event":
            default_requirements.append("historical_record")
        if "relative_time" in risk_flags:
            default_requirements.append("resolved_time_scope")
        if claim_type in {"quantitative", "quantitative_comparison"}:
            default_requirements.append("structured_data")
        if "comparative" in risk_flags:
            default_requirements.append("same_scope_comparison")
        if "enumeration" in risk_flags:
            default_requirements.append("enumeration_or_primary_summary")
        if claim_type == "current_event":
            default_requirements.append("independent_corroboration")
        if claim_type in {"legal", "medical_scientific"}:
            default_requirements.append("domain_authority")
        if "exclusive_scope" in risk_flags:
            default_requirements.append("scope_evidence")
        if "context_sensitive" in risk_flags:
            default_requirements.append("original_context")
        for requirement in default_requirements:
            if requirement not in required_evidence:
                required_evidence.append(requirement)

        preferred_roles = _unique_strings(raw.get("preferred_source_roles"))
        for role in ("official_primary", "institutional", "academic"):
            if role not in preferred_roles:
                preferred_roles.append(role)

        contracts.append(
            {
                "claim_id": claim_id,
                "claim_type": claim_type,
                "risk_flags": risk_flags,
                "numeric_roles": numeric_roles,
                "required_evidence": required_evidence,
                "preferred_source_roles": preferred_roles,
                "reference_year": year if "relative_time" in risk_flags else None,
            }
        )
    return contracts


def _is_self_media(host: str, path: str) -> bool:
    return (
        (host.endswith("163.com") and "/dy/" in path)
        or host == "baijiahao.baidu.com"
        or (host.endswith("sohu.com") and path.startswith("/a/"))
        or host.endswith("toutiao.com")
    )


def _detect_evidence_type(
    title: str, snippet: str, url: str, provider: str = ""
) -> str:
    text = f"{title} {snippet}"
    host = (urlsplit(url).hostname or "").lower()
    numeric_tokens = re.findall(r"\d+(?:\.\d+)?", snippet)
    table_markers = snippet.count("|")
    if any(marker in title for marker in COMMENTARY_MARKERS):
        return "commentary"
    if provider in {"openalex", "arxiv", "semantic-scholar"} or _host_matches(
        host, ACADEMIC_HOST_SUFFIXES
    ):
        return "research"
    if table_markers >= 6 and len(numeric_tokens) >= 3:
        return "structured_data"
    if re.search(r"数据表|一览表|投档线|统计表|年报|财报|名单|清单", text) and len(numeric_tokens) >= 3:
        return "structured_data"
    if urlsplit(url).path.lower().endswith(".pdf") or re.search(
        r"公告|通知|通报|办法|条例|标准|指南|判决书", title
    ):
        return "primary_document"
    if re.search(r"论文|研究|摘要|doi|journal", text, re.IGNORECASE):
        return "research"
    return "reporting"


def profile_evidence(item: dict[str, Any]) -> dict[str, Any]:
    """Attach source-role and evidence-shape metadata without changing legacy tier."""
    profiled = dict(item)
    title = str(item.get("title") or "")
    snippet = str(item.get("snippet") or "")
    url = str(item.get("url") or "")
    provider = str(item.get("provider") or "")
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    evidence_type = _detect_evidence_type(title, snippet, url, provider)

    identity_basis = "unverified"
    if _is_self_media(host, path):
        source_role = "self_media"
        identity_basis = "url_pattern"
    elif _host_matches(host, OFFICIAL_HOST_SUFFIXES):
        source_role = "official_primary"
        identity_basis = "domain"
    elif _host_matches(host, INSTITUTIONAL_HOST_SUFFIXES):
        source_role = "institutional"
        identity_basis = "domain"
    elif (
        path.endswith(".pdf")
        and any(marker in title for marker in INSTITUTIONAL_TITLE_MARKERS)
    ):
        source_role = "institutional"
        identity_basis = "document_title"
    elif provider in {"openalex", "arxiv", "semantic-scholar"} or _host_matches(
        host, ACADEMIC_HOST_SUFFIXES
    ):
        source_role = "academic"
        identity_basis = "provider_or_domain"
    elif _host_matches(host, AUTHORITATIVE_NEWS_HOSTS):
        source_role = "authoritative_media"
        identity_basis = "domain"
    elif "wikipedia.org" in host:
        source_role = "reference"
        identity_basis = "domain"
    elif str(item.get("tier")) == "TIER_2":
        source_role = "general_media"
    else:
        source_role = "unverified"

    selection_eligible = bool(url and snippet) and source_role != "self_media"
    if evidence_type == "commentary" and source_role in {"general_media", "unverified"}:
        selection_eligible = False

    authority_scores = {
        "official_primary": 8,
        "institutional": 6,
        "academic": 5,
        "authoritative_media": 4,
        "general_media": 2,
        "reference": 1,
        "unverified": 0,
        "self_media": -8,
    }
    type_scores = {
        "structured_data": 4,
        "primary_document": 3,
        "research": 2,
        "reporting": 0,
        "commentary": -3,
    }
    provider_rank = int(item.get("provider_rank") or 5)
    selection_score = (
        authority_scores[source_role]
        + type_scores[evidence_type]
        + max(0, 4 - provider_rank)
    )
    if str(item.get("tier")) == "TIER_1":
        selection_score += 2
    elif str(item.get("tier")) == "TIER_2":
        selection_score += 1

    directness = "indirect"
    if source_role == "official_primary" and evidence_type in {
        "structured_data",
        "primary_document",
        "reporting",
    }:
        directness = "direct"
    elif source_role == "academic" and evidence_type == "research":
        directness = "direct"
    elif source_role == "institutional" and evidence_type in {
        "structured_data",
        "primary_document",
        "research",
    }:
        directness = "direct" if identity_basis == "domain" else "candidate_direct"

    profiled["evidence_profile"] = {
        "source_role": source_role,
        "evidence_type": evidence_type,
        "identity_basis": identity_basis,
        "directness": directness,
        "selection_eligible": selection_eligible,
        "selection_score": selection_score,
    }
    return profiled


def profile_evidence_collection(evidence: dict[str, Any]) -> dict[str, Any]:
    profiled = dict(evidence)
    for key in ("tier1_high_trust", "tier2_supporting", "tier3_isolated"):
        profiled[key] = [profile_evidence(item) for item in evidence.get(key, [])]
    return profiled


MODEL_SOURCE_ROLES = {
    "canonical_primary",
    "official_primary",
    "institutional",
    "academic",
    "authoritative_media",
    "general_media",
    "reference",
    "self_media",
    "unknown",
}
MODEL_SOURCE_RELATIONS = {
    "supports",
    "refutes",
    "context",
    "propagation",
    "irrelevant",
}
MODEL_DIRECTNESS = {"direct", "indirect"}
TRUSTED_SOURCE_ROLES = {
    "canonical_primary",
    "official_primary",
    "institutional",
    "academic",
    "authoritative_media",
}


def apply_model_source_assessments(
    evidence_by_id: dict[str, dict[str, Any]],
    raw_assessments: Any,
    claim_checks: Any = None,
) -> dict[str, dict[str, Any]]:
    """Merge controlled LLM source judgments while preserving hard safety vetoes."""
    assessed = {source_id: dict(source) for source_id, source in evidence_by_id.items()}
    if not isinstance(raw_assessments, list):
        return assessed

    checks_by_id = {
        str(item.get("claim_id")): item
        for item in claim_checks or []
        if isinstance(item, dict) and item.get("claim_id")
    }

    for raw in raw_assessments:
        if not isinstance(raw, dict):
            continue
        source_id = str(raw.get("source_id") or "")
        claim_id = str(raw.get("claim_id") or "")
        source = assessed.get(source_id)
        if not source or not claim_id:
            continue
        profiled = source if source.get("evidence_profile") else profile_evidence(source)
        source.update(profiled)
        profile = source["evidence_profile"]
        role = str(raw.get("source_role") or "unknown")
        if role == "canonical_record":
            role = "canonical_primary"
        relation = str(raw.get("relation") or "irrelevant")
        directness = str(raw.get("directness") or "indirect")
        if role not in MODEL_SOURCE_ROLES:
            role = "unknown"
        if relation not in MODEL_SOURCE_RELATIONS:
            claim_check = checks_by_id.get(claim_id, {})
            cited_source_ids = {
                str(value) for value in claim_check.get("source_ids", [])
            } if isinstance(claim_check.get("source_ids"), list) else set()
            verdict = str(claim_check.get("verdict") or "")
            if source_id in cited_source_ids and verdict in {
                "属实",
                "部分属实",
            }:
                relation = "supports"
            elif source_id in cited_source_ids and verdict in {
                "捏造",
                "虚假",
                "误导",
            }:
                relation = "refutes"
            else:
                relation = "irrelevant"
        if directness not in MODEL_DIRECTNESS:
            directness = "indirect"

        if profile["source_role"] == "self_media" or profile["evidence_type"] == "commentary":
            role = "self_media"
            directness = "indirect"
        elif role == "unknown" and profile["source_role"] in TRUSTED_SOURCE_ROLES:
            role = profile["source_role"]

        claim_assessments = dict(source.get("claim_assessments") or {})
        claim_assessments[claim_id] = {
            "source_role": role,
            "relation": relation,
            "directness": directness,
            "reason": str(raw.get("reason") or "")[:120],
        }
        source["claim_assessments"] = claim_assessments
    return assessed


def effective_source_assessment(
    source: dict[str, Any], claim_id: str | None = None
) -> dict[str, str]:
    profiled = source if source.get("evidence_profile") else profile_evidence(source)
    profile = profiled["evidence_profile"]
    model_assessment = (
        profiled.get("claim_assessments", {}).get(claim_id)
        if claim_id
        else None
    )
    if model_assessment:
        return dict(model_assessment)
    return {
        "source_role": str(profile["source_role"]),
        "relation": "",
        "directness": str(profile["directness"]),
        "reason": "",
    }


def select_evidence_candidates(
    evidence: dict[str, Any],
    limit: int = 8,
    query_targets: Any = None,
    candidates_per_claim: int = 3,
) -> list[dict[str, Any]]:
    """Build a claim-aware evidence portfolio across all legacy tiers."""
    targets_by_query: dict[str, list[str]] = {}
    claim_order: list[str] = []
    if isinstance(query_targets, list):
        for target in query_targets:
            if not isinstance(target, dict):
                continue
            query = str(target.get("query") or "")
            claim_ids = _unique_strings(target.get("claim_ids"))
            if not query or not claim_ids:
                continue
            targets_by_query[query] = claim_ids
            for claim_id in claim_ids:
                if claim_id not in claim_order:
                    claim_order.append(claim_id)

    candidates = []
    for key in ("tier1_high_trust", "tier2_supporting", "tier3_isolated"):
        for item in evidence.get(key, []):
            profiled = item if item.get("evidence_profile") else profile_evidence(item)
            if profiled["evidence_profile"]["selection_eligible"]:
                candidate = dict(profiled)
                candidate["target_claim_ids"] = targets_by_query.get(
                    str(candidate.get("search_query") or ""),
                    [],
                )
                candidates.append(candidate)

    candidates.sort(
        key=lambda item: (
            item["evidence_profile"]["selection_score"],
            -int(item.get("provider_rank") or 99),
        ),
        reverse=True,
    )
    selected = []
    selected_urls = set()
    seen_queries = set()

    def add(item: dict[str, Any]) -> bool:
        normalized_url = str(item.get("url") or "").split("?", 1)[0].rstrip("/")
        if normalized_url in selected_urls:
            for selected_item in selected:
                selected_url = str(selected_item.get("url") or "").split("?", 1)[0].rstrip("/")
                if selected_url != normalized_url:
                    continue
                merged_claim_ids = list(selected_item.get("target_claim_ids", []))
                for claim_id in item.get("target_claim_ids", []):
                    if claim_id not in merged_claim_ids:
                        merged_claim_ids.append(claim_id)
                selected_item["target_claim_ids"] = merged_claim_ids
                break
            return False
        selected.append(item)
        selected_urls.add(normalized_url)
        return True

    if claim_order:
        for claim_id in claim_order:
            covered = sum(
                claim_id in item.get("target_claim_ids", []) for item in selected
            )
            for item in candidates:
                if claim_id not in item.get("target_claim_ids", []):
                    continue
                add(item)
                covered = sum(
                    claim_id in selected_item.get("target_claim_ids", [])
                    for selected_item in selected
                )
                if covered >= candidates_per_claim or len(selected) >= limit:
                    break
            if len(selected) >= limit:
                return selected

        for item in candidates:
            add(item)
            if len(selected) >= limit:
                break
        return selected

    for item in candidates:
        query = str(item.get("search_query") or "")
        if query and query not in seen_queries:
            add(item)
            seen_queries.add(query)
        if len(selected) >= limit:
            return selected

    for item in candidates:
        add(item)
        if len(selected) >= limit:
            break
    return selected


def _normalized_source_text(source: dict[str, Any]) -> str:
    text = f"{source.get('title', '')} {source.get('snippet', '')}".lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)[:800]


def has_sufficient_event_evidence(
    source_ids: list[str],
    evidence_by_id: dict[str, dict[str, Any]],
    claim_id: str | None = None,
) -> bool:
    distinct_sources = []
    seen_urls = set()
    for source_id in source_ids:
        source = evidence_by_id.get(source_id)
        if not source:
            continue
        profiled = source if source.get("evidence_profile") else profile_evidence(source)
        url = str(profiled.get("url") or "").split("?", 1)[0].rstrip("/")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        distinct_sources.append(profiled)

    if any(
        effective_source_assessment(source, claim_id)["source_role"]
        in {"canonical_primary", "official_primary", "institutional", "academic"}
        and effective_source_assessment(source, claim_id)["directness"] == "direct"
        and effective_source_assessment(source, claim_id)["relation"]
        in {"", "supports"}
        for source in distinct_sources
    ):
        return True

    authoritative_sources = [
        source
        for source in distinct_sources
        if effective_source_assessment(source, claim_id)["source_role"]
        == "authoritative_media"
        and effective_source_assessment(source, claim_id)["relation"]
        in {"", "supports"}
    ]
    hosts = {
        (urlsplit(source.get("url", "")).hostname or "").lower()
        for source in authoritative_sources
    }
    if len(hosts) < 2:
        return False
    first_text = _normalized_source_text(authoritative_sources[0])
    return any(
        SequenceMatcher(None, first_text, _normalized_source_text(source)).ratio() < 0.88
        for source in authoritative_sources[1:]
    )


def has_decisive_source(
    source_ids: list[str],
    evidence_by_id: dict[str, dict[str, Any]],
    claim_id: str | None = None,
    required_relations: set[str] | None = None,
) -> bool:
    for source_id in source_ids:
        source = evidence_by_id.get(source_id)
        if not source:
            continue
        profiled = source if source.get("evidence_profile") else profile_evidence(source)
        assessment = effective_source_assessment(profiled, claim_id)
        if (
            required_relations
            and assessment["relation"]
            and assessment["relation"] not in required_relations
        ):
            continue
        if assessment["directness"] == "direct" and assessment["source_role"] in {
            "canonical_primary",
            "official_primary",
            "institutional",
            "academic",
        }:
            return True
        if (
            assessment["source_role"] == "authoritative_media"
            and assessment["directness"] == "direct"
        ):
            return True
    return False


def has_propagation_evidence(
    source_ids: list[str],
    evidence_by_id: dict[str, dict[str, Any]],
    claim_id: str | None = None,
) -> bool:
    """Confirm that a reported statement exists without endorsing its contents."""
    for source_id in source_ids:
        source = evidence_by_id.get(source_id)
        if not source or not source.get("url"):
            continue
        profiled = source if source.get("evidence_profile") else profile_evidence(source)
        assessment = effective_source_assessment(profiled, claim_id)
        if assessment["relation"] == "propagation":
            return True
    return False


def satisfies_evidence_contract(
    contract: dict[str, Any] | None,
    source_ids: list[str],
    evidence_by_id: dict[str, dict[str, Any]],
    required_relations: set[str] | None = None,
) -> bool:
    if not contract:
        return has_decisive_source(
            source_ids,
            evidence_by_id,
            required_relations=required_relations,
        )
    sources = []
    for source_id in source_ids:
        source = evidence_by_id.get(source_id)
        if source:
            sources.append(source if source.get("evidence_profile") else profile_evidence(source))
    if not sources:
        return False

    requirements = set(contract.get("required_evidence", []))
    profiles = [source["evidence_profile"] for source in sources]
    claim_id = str(contract.get("claim_id") or "") or None
    if "propagation_evidence" in requirements:
        return has_propagation_evidence(source_ids, evidence_by_id, claim_id)
    if "canonical_record" in requirements and not any(
        effective_source_assessment(source, claim_id)["source_role"]
        in {"canonical_primary", "official_primary"}
        and effective_source_assessment(source, claim_id)["directness"] == "direct"
        for source in sources
    ):
        return False
    if "structured_data" in requirements and not any(
        profile["evidence_type"] == "structured_data" for profile in profiles
    ):
        return False
    if "same_scope_comparison" in requirements and not any(
        profile["evidence_type"] in {"structured_data", "primary_document"}
        for profile in profiles
    ):
        return False
    if "enumeration_or_primary_summary" in requirements and not any(
        profile["source_role"] in {"official_primary", "institutional"}
        and profile["directness"] == "direct"
        and profile["evidence_type"] in {"structured_data", "primary_document"}
        for profile in profiles
    ):
        return False
    return has_decisive_source(
        source_ids,
        evidence_by_id,
        claim_id=claim_id,
        required_relations=required_relations,
    )


def requires_independent_corroboration(contract: dict[str, Any] | None) -> bool:
    return bool(
        contract
        and "independent_corroboration" in contract.get("required_evidence", [])
    )


def revise_event_contract_type(
    contract: dict[str, Any] | None, proposed_claim_type: Any
) -> dict[str, Any] | None:
    """Allow the report model to correct only event-family classification errors."""
    if not contract:
        return contract
    current_type = str(contract.get("claim_type") or "")
    proposed_type = str(proposed_claim_type or "")
    event_types = {"current_event", "historical_event", "reported_claim"}
    if current_type not in event_types or proposed_type not in event_types:
        return contract

    revised = dict(contract)
    requirements = list(contract.get("required_evidence", []))
    for requirement in (
        "independent_corroboration",
        "historical_record",
        "propagation_evidence",
    ):
        requirements = [item for item in requirements if item != requirement]
    if proposed_type == "current_event":
        requirements.append("independent_corroboration")
    elif proposed_type == "historical_event":
        requirements.append("historical_record")
    else:
        requirements = [item for item in requirements if item != "primary_source"]
        requirements.append("propagation_evidence")
    revised["claim_type"] = proposed_type
    revised["required_evidence"] = list(dict.fromkeys(requirements))
    return revised


def find_contract_satisfying_sources(
    contract: dict[str, Any] | None,
    evidence_by_id: dict[str, dict[str, Any]],
    required_relations: set[str],
) -> list[str]:
    """Find model-assessed alternatives when cited sources miss the contract."""
    if not contract:
        return []
    claim_id = str(contract.get("claim_id") or "")
    candidates = []
    for source_id, source in evidence_by_id.items():
        assessment = source.get("claim_assessments", {}).get(claim_id)
        if not assessment or assessment.get("relation") not in required_relations:
            continue
        candidates.append(source_id)

    for source_id in candidates:
        if satisfies_evidence_contract(
            contract,
            [source_id],
            evidence_by_id,
            required_relations=required_relations,
        ):
            return [source_id]
    return []


def is_unstated_exclusivity_nitpick(claim: str, basis: str) -> bool:
    """Detect a downgrade based only on an exclusivity claim the input never made."""
    if any(marker in claim for marker in EXCLUSIVITY_MARKERS):
        return False
    return bool(
        re.search(
            r"(?:"
            r"(?:并非|不是|非|未|不能).{0,18}(?:单凭|单一|唯一|全部|整个|完全|最终)"
            r"|(?:单凭|单一|唯一|全部|整个|完全|最终).{0,18}(?:证明|验证|手段|证据|原因|依据)"
            r")",
            basis,
        )
    )
