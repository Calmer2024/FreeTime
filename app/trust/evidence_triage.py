"""Semantic evidence triage normalization and adaptive portfolio selection."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from app.trust.evidence_policy import MODEL_SOURCE_ROLES, profile_evidence


ALLOWED_RELATIONS = {"supports", "refutes", "context", "propagation", "irrelevant"}
ALLOWED_DIRECTNESS = {"direct", "indirect"}
ALLOWED_STRENGTHS = {"decisive", "strong", "moderate", "weak", "none"}
ALLOWED_RECOMMENDATIONS = {"include", "review", "exclude"}

COMPACT_ROLE_CODES = {
    "C": "canonical_primary",
    "O": "official_primary",
    "I": "institutional",
    "A": "academic",
    "M": "authoritative_media",
    "G": "general_media",
    "R": "reference",
    "S": "self_media",
    "U": "unknown",
}
COMPACT_RELATION_CODES = {
    "s": "supports",
    "r": "refutes",
    "c": "context",
    "p": "propagation",
    "x": "irrelevant",
}
COMPACT_DIRECTNESS_CODES = {"d": "direct", "i": "indirect"}
COMPACT_STRENGTH_CODES = {
    4: "decisive",
    3: "strong",
    2: "moderate",
    1: "weak",
    0: "none",
}

STRENGTH_SCORES = {
    "decisive": 100,
    "strong": 80,
    "moderate": 45,
    "weak": 10,
    "none": -100,
}
ROLE_SCORES = {
    "canonical_primary": 30,
    "official_primary": 30,
    "institutional": 20,
    "academic": 20,
    "authoritative_media": 14,
    "general_media": 5,
    "reference": 2,
    "unknown": 0,
    "self_media": -20,
}
RECOMMENDATION_SCORES = {"include": 20, "review": 0, "exclude": -100}


def _unique_strings(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    result = []
    for value in values:
        normalized = str(value).strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def decode_compact_triage_rows(
    rows: Any,
    source_ids: list[str],
    claim_ids: list[str],
) -> list[dict[str, Any]]:
    """Expand terse model rows into the validated triage assessment contract."""
    if not isinstance(rows, list):
        return []
    known_source_ids = set(source_ids)
    known_claim_ids = set(claim_ids)
    decoded = []
    seen_source_ids = set()
    for row in rows:
        if not isinstance(row, list) or len(row) != 4:
            continue
        source_id = str(row[0] or "")
        if source_id not in known_source_ids or source_id in seen_source_ids:
            continue
        seen_source_ids.add(source_id)
        role = COMPACT_ROLE_CODES.get(str(row[1] or "").upper(), "unknown")

        relations = []
        relevant_claim_ids = []
        raw_relations = row[2] if isinstance(row[2], list) else []
        for relation_row in raw_relations:
            if not isinstance(relation_row, list) or len(relation_row) != 3:
                continue
            claim_id = str(relation_row[0] or "")
            relation = COMPACT_RELATION_CODES.get(str(relation_row[1] or "").lower())
            directness = COMPACT_DIRECTNESS_CODES.get(
                str(relation_row[2] or "").lower()
            )
            if (
                claim_id not in known_claim_ids
                or relation is None
                or directness is None
                or any(item["claim_id"] == claim_id for item in relations)
            ):
                continue
            relations.append(
                {
                    "claim_id": claim_id,
                    "relation": relation,
                    "directness": directness,
                }
            )
            if relation != "irrelevant":
                relevant_claim_ids.append(claim_id)

        strength_value = row[3] if isinstance(row[3], int) else -1
        strength = COMPACT_STRENGTH_CODES.get(strength_value, "none")
        factual_relations = {
            item["relation"] for item in relations if item["relation"] != "irrelevant"
        }
        if not relevant_claim_ids or strength == "none":
            recommendation = "exclude"
        elif strength in {"decisive", "strong"}:
            recommendation = "include"
        else:
            recommendation = "review"
        if role == "self_media" and not factual_relations.issubset(
            {"propagation", "context"}
        ):
            recommendation = "exclude"

        decoded.append(
            {
                "source_id": source_id,
                "source_role": role,
                "relevant_claim_ids": relevant_claim_ids,
                "claim_relations": relations,
                "evidence_strength": strength,
                "recommendation": recommendation,
                "reason": "",
            }
        )
    return decoded


def semantic_assessments_for_report(
    sources: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Reuse triage labels as report-stage source assessments."""
    assessments = []
    for source in sources:
        source_id = str(source.get("id") or "")
        semantic = source.get("semantic_assessment", {})
        source_role = str(semantic.get("source_role") or "unknown")
        for relation in semantic.get("claim_relations", []):
            if not isinstance(relation, dict):
                continue
            claim_id = str(relation.get("claim_id") or "")
            relation_value = str(relation.get("relation") or "irrelevant")
            directness = str(relation.get("directness") or "indirect")
            if not source_id or not claim_id or relation_value == "irrelevant":
                continue
            assessments.append(
                {
                    "source_id": source_id,
                    "claim_id": claim_id,
                    "source_role": source_role,
                    "relation": relation_value,
                    "directness": directness,
                }
            )
    return assessments


def decode_report_source_labels(
    rows: Any,
    source_ids: list[str],
    claim_ids: list[str],
) -> list[dict[str, str]]:
    """Validate compact final-model labels for the sources it actually cites."""
    if not isinstance(rows, list):
        return []
    known_source_ids = set(source_ids)
    known_claim_ids = set(claim_ids)
    labels = []
    seen_pairs = set()
    for row in rows:
        if not isinstance(row, list) or len(row) != 5:
            continue
        source_id = str(row[0] or "")
        claim_id = str(row[1] or "")
        pair = (source_id, claim_id)
        role = COMPACT_ROLE_CODES.get(str(row[2] or "").upper())
        relation = COMPACT_RELATION_CODES.get(str(row[3] or "").lower())
        directness = COMPACT_DIRECTNESS_CODES.get(str(row[4] or "").lower())
        if (
            source_id not in known_source_ids
            or claim_id not in known_claim_ids
            or pair in seen_pairs
            or role is None
            or relation is None
            or directness is None
        ):
            continue
        seen_pairs.add(pair)
        labels.append(
            {
                "source_id": source_id,
                "claim_id": claim_id,
                "source_role": role,
                "relation": relation,
                "directness": directness,
            }
        )
    return labels


def prepare_evidence_sources(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten the deduplicated pool and attach stable IDs and query routing hints."""
    targets_by_query: dict[str, list[str]] = {}
    for target in evidence.get("query_targets", []):
        if not isinstance(target, dict):
            continue
        query = str(target.get("query") or "").strip()
        if query:
            targets_by_query[query] = _unique_strings(target.get("claim_ids"))

    sources = []
    for tier_key in ("tier1_high_trust", "tier2_supporting", "tier3_isolated"):
        for item in evidence.get(tier_key, []):
            profiled = item if item.get("evidence_profile") else profile_evidence(item)
            source = dict(profiled)
            source["id"] = f"E{len(sources) + 1:03d}"
            source["target_claim_ids"] = targets_by_query.get(
                str(source.get("search_query") or ""), []
            )
            sources.append(source)
    return sources


def _fallback_assessment(source: dict[str, Any], claim_ids: set[str]) -> dict[str, Any]:
    profile = source.get("evidence_profile", {})
    targets = [
        claim_id
        for claim_id in _unique_strings(source.get("target_claim_ids"))
        if claim_id in claim_ids
    ]
    eligible = bool(profile.get("selection_eligible"))
    return {
        "source_id": source["id"],
        "source_role": str(profile.get("source_role") or "unknown"),
        "relevant_claim_ids": targets,
        "claim_relations": [
            {
                "claim_id": claim_id,
                "relation": "context",
                "directness": "indirect",
            }
            for claim_id in targets
        ],
        "evidence_strength": "weak" if eligible and targets else "none",
        "recommendation": "review" if eligible and targets else "exclude",
        "reason": "Semantic triage unavailable; retained only as a routing candidate.",
        "triage_status": "fallback",
    }


def normalize_triage_assessments(
    sources: list[dict[str, Any]],
    raw_assessments: Any,
    claim_ids: list[str],
) -> list[dict[str, Any]]:
    """Validate model labels and merge them with deterministic source constraints."""
    known_claim_ids = set(claim_ids)
    known_source_ids = {str(source.get("id")) for source in sources}
    raw_by_id = {}
    if isinstance(raw_assessments, list):
        for raw in raw_assessments:
            if not isinstance(raw, dict):
                continue
            source_id = str(raw.get("source_id") or "")
            if source_id in known_source_ids:
                raw_by_id[source_id] = raw

    assessed_sources = []
    for source in sources:
        normalized_source = dict(source)
        source_id = str(source["id"])
        raw = raw_by_id.get(source_id)
        if raw is None:
            normalized_source["semantic_assessment"] = _fallback_assessment(
                source, known_claim_ids
            )
            assessed_sources.append(normalized_source)
            continue

        role = str(raw.get("source_role") or "unknown")
        if role not in MODEL_SOURCE_ROLES:
            role = "unknown"

        relations = []
        seen_relation_claims = set()
        raw_relations = raw.get("claim_relations", [])
        if isinstance(raw_relations, list):
            for relation_item in raw_relations:
                if not isinstance(relation_item, dict):
                    continue
                claim_id = str(relation_item.get("claim_id") or "")
                relation = str(relation_item.get("relation") or "irrelevant")
                directness = str(relation_item.get("directness") or "indirect")
                if claim_id not in known_claim_ids or claim_id in seen_relation_claims:
                    continue
                if relation not in ALLOWED_RELATIONS:
                    relation = "irrelevant"
                if directness not in ALLOWED_DIRECTNESS:
                    directness = "indirect"
                relations.append(
                    {
                        "claim_id": claim_id,
                        "relation": relation,
                        "directness": directness,
                    }
                )
                seen_relation_claims.add(claim_id)

        relevant_claim_ids = [
            claim_id
            for claim_id in _unique_strings(raw.get("relevant_claim_ids"))
            if claim_id in known_claim_ids
        ]
        for relation in relations:
            if (
                relation["relation"] != "irrelevant"
                and relation["claim_id"] not in relevant_claim_ids
            ):
                relevant_claim_ids.append(relation["claim_id"])

        strength = str(raw.get("evidence_strength") or "none")
        if strength not in ALLOWED_STRENGTHS:
            strength = "none"
        recommendation = str(raw.get("recommendation") or "review")
        if recommendation not in ALLOWED_RECOMMENDATIONS:
            recommendation = "review"

        profile = source.get("evidence_profile", {})
        mechanical_role = str(profile.get("source_role") or "unknown")
        evidence_type = str(profile.get("evidence_type") or "")
        if mechanical_role == "self_media" or evidence_type == "commentary":
            role = "self_media"
            for relation in relations:
                if relation["relation"] not in {"propagation", "context", "irrelevant"}:
                    relation["relation"] = "context"
                    relation["directness"] = "indirect"
            if not any(item["relation"] == "propagation" for item in relations):
                strength = "none"
                recommendation = "exclude"

        if not relevant_claim_ids or not any(
            relation["relation"] != "irrelevant" for relation in relations
        ):
            relevant_claim_ids = []
            strength = "none"
            recommendation = "exclude"

        normalized_source["semantic_assessment"] = {
            "source_id": source_id,
            "source_role": role,
            "relevant_claim_ids": relevant_claim_ids,
            "claim_relations": relations,
            "evidence_strength": strength,
            "recommendation": recommendation,
            "reason": str(raw.get("reason") or "").strip()[:240],
            "triage_status": "assessed",
        }
        assessed_sources.append(normalized_source)
    return assessed_sources


def semantic_candidate_score(source: dict[str, Any], claim_id: str | None = None) -> int:
    assessment = source.get("semantic_assessment", {})
    strength = str(assessment.get("evidence_strength") or "none")
    recommendation = str(assessment.get("recommendation") or "exclude")
    role = str(assessment.get("source_role") or "unknown")
    relations = assessment.get("claim_relations", [])
    if claim_id:
        relations = [item for item in relations if item.get("claim_id") == claim_id]

    relation_score = -30
    for item in relations:
        relation = item.get("relation")
        direct = item.get("directness") == "direct"
        if relation in {"supports", "refutes"}:
            relation_score = max(relation_score, 25 if direct else 12)
        elif relation == "propagation":
            relation_score = max(relation_score, 15 if direct else 8)
        elif relation == "context":
            relation_score = max(relation_score, 0)

    mechanical_score = int(
        source.get("evidence_profile", {}).get("selection_score") or 0
    )
    return (
        STRENGTH_SCORES.get(strength, -100)
        + ROLE_SCORES.get(role, 0)
        + RECOMMENDATION_SCORES.get(recommendation, -100)
        + relation_score
        + max(-5, min(10, mechanical_score))
    )


def _source_cost(source: dict[str, Any]) -> int:
    return len(str(source.get("title") or "")) + min(
        700, len(str(source.get("snippet") or ""))
    )


def _source_fingerprint(source: dict[str, Any]) -> str:
    title = str(source.get("title") or "").lower()
    title = re.sub(r"^.*?\[(?:exa|openalex|arxiv|semantic scholar)\]\s*", "", title)
    title = re.split(r"\(pdf\)", title, maxsplit=1, flags=re.IGNORECASE)[0]
    normalized_title = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", title)
    if len(normalized_title) >= 20:
        return f"title:{normalized_title}"
    canonical_url = str(source.get("url") or "").split("?", 1)[0].rstrip("/")
    return f"url:{canonical_url}"


def select_triaged_evidence(
    sources: list[dict[str, Any]],
    claim_ids: list[str],
    max_candidates: int | None = None,
    snippet_budget_chars: int | None = None,
) -> list[dict[str, Any]]:
    """Select a claim-balanced portfolio using semantic labels and a text budget."""
    if max_candidates is None:
        max_candidates = min(24, max(12, len(claim_ids) * 5))
    if snippet_budget_chars is None:
        snippet_budget_chars = min(18000, max(9000, len(claim_ids) * 4000))

    eligible = []
    for source in sources:
        assessment = source.get("semantic_assessment", {})
        if assessment.get("recommendation") == "exclude":
            continue
        if assessment.get("evidence_strength") == "none":
            continue
        if not assessment.get("relevant_claim_ids"):
            continue
        eligible.append(source)

    selected = []
    selected_urls = set()
    selected_fingerprints = set()
    used_chars = 0

    def add(source: dict[str, Any]) -> bool:
        nonlocal used_chars
        canonical_url = str(source.get("url") or "").split("?", 1)[0].rstrip("/")
        fingerprint = _source_fingerprint(source)
        if (
            not canonical_url
            or canonical_url in selected_urls
            or fingerprint in selected_fingerprints
        ):
            return False
        cost = _source_cost(source)
        if selected and used_chars + cost > snippet_budget_chars:
            return False
        if len(selected) >= max_candidates:
            return False
        selected.append(source)
        selected_urls.add(canonical_url)
        selected_fingerprints.add(fingerprint)
        used_chars += cost
        return True

    # Guarantee claim coverage before global ranking. Prefer direct counterevidence
    # and direct support, then diversify source roles within each claim.
    per_claim_target = 3
    for claim_id in claim_ids:
        claim_candidates = [
            source
            for source in eligible
            if claim_id
            in source.get("semantic_assessment", {}).get("relevant_claim_ids", [])
        ]
        claim_candidates.sort(
            key=lambda source: semantic_candidate_score(source, claim_id),
            reverse=True,
        )
        chosen_roles = set()
        claim_selected = 0
        for source in claim_candidates:
            role = source.get("semantic_assessment", {}).get("source_role")
            if role in chosen_roles and claim_selected >= 2:
                continue
            if add(source):
                chosen_roles.add(role)
                claim_selected += 1
            if claim_selected >= per_claim_target:
                break

    # Preserve strong primary/technical evidence. Repeated reporting remains
    # available in the ledger but should not fill the full-text budget.
    ranked = sorted(eligible, key=semantic_candidate_score, reverse=True)
    for source in ranked:
        assessment = source.get("semantic_assessment", {})
        strength = assessment.get("evidence_strength")
        role = assessment.get("source_role")
        if strength in {"decisive", "strong"} and role in {
            "canonical_primary",
            "official_primary",
            "institutional",
            "academic",
        }:
            add(source)

    minimum_candidates = min(max_candidates, max(6, len(claim_ids) * 2))
    if len(selected) < minimum_candidates:
        for source in ranked:
            add(source)
            if len(selected) >= minimum_candidates:
                break
    return selected


def compact_evidence_ledger(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep all semantic decisions visible without repeating full snippets."""
    ledger = []
    for source in sources:
        assessment = source.get("semantic_assessment", {})
        ledger.append(
            {
                "id": source.get("id"),
                "title": " ".join(str(source.get("title") or "").split())[:120],
                "host": (urlsplit(str(source.get("url") or "")).hostname or "").lower(),
                "source_role": assessment.get("source_role", "unknown"),
                "relevant_claim_ids": assessment.get("relevant_claim_ids", []),
                "claim_relations": assessment.get("claim_relations", []),
                "evidence_strength": assessment.get("evidence_strength", "none"),
                "recommendation": assessment.get("recommendation", "exclude"),
                "reason": assessment.get("reason", ""),
                "triage_status": assessment.get("triage_status", "fallback"),
            }
        )
    return ledger


def compact_ledger_for_prompt(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Minimize the all-source ledger passed to the final model."""
    return [
        {
            key: item.get(key)
            for key in (
                "id",
                "host",
                "source_role",
                "relevant_claim_ids",
                "claim_relations",
                "evidence_strength",
                "recommendation",
                "triage_status",
            )
        }
        for item in compact_evidence_ledger(sources)
    ]
