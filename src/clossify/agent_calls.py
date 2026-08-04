# Clossify — Naver SmartStore listing automation.
# Copyright (c) 2026 3rdhand. Licensed under the Sustainable Use License v1.0.
# You may use and modify this software for your own internal business or personal
# purposes. Providing it to others — including as a hosted or paid service — is
# permitted only free of charge and for non-commercial purposes. See LICENSE.md.
"""판단 위임 계약 구성.

원본 ``sourcing.py`` 의 에이전트 프롬프트 조립 로직을 이식한다. 단,
**LLM 을 직접 호출하지 않는다** — 대신 :func:`common._llm_hint()` 가 정의한
표준 위임 디스크립터를 반환하여 MCP 호스트 LLM 이 실행하도록 한다.

이 모듈이 담당하는 두 가지 판단 위임:
  1. **naming_agent** — 한국어 SEO 상품명 생성 (``naming_agent.md`` +
     ``COMPLIANCE_RULES.md`` 규칙 적용)
  2. **qa_copy_agent** — 카피/텍스트 품질 판단 (``QA_AGENTS.md`` +
     ``COMPLIANCE_RULES.md`` + ``COPY_GUIDE.md`` 규칙 적정성 판단)

**프롬프트 조립 순서 보존 원칙** (프롬프트 축약 금지):
원본 ``sourcing.py`` 의 프롬프트 조립 단계를 그대로 유지한다.
  1. 페르소나/역할 선언
  2. 준거 규칙 교차 참조 (COMPLIANCE_RULES 섹션 + 에이전트 md)
  3. 형식/길이 지시 (유닛 수, 자수, 구조 템플릿)
  4. 선택/제외 지시 (저관련어 컷, 색상/배치 제한)
  5. 동의어 중복 접기 지시
  6. 타 카테고리 스펙 용어 금지
  7. 출력 계약 (JSON 키)
  8. ``<*.md>...</*.md>`` 규칙 블록 (COMPLIANCE_RULES → 에이전트 md 순)
  9. ``INPUT JSON:`` + 페이로드

응답 정규화는 :mod:`copywriting` 의 기존 함수를 재사용한다
(``_normalize_naming_result``, ``_normalize_qa_result`` 등 — 중복 구현 금지).

의존 방향: ``copywriting`` (상위) → ``agent_calls`` (본 모듈).
``common``, ``text_props`` 등은 어디서든 import 가능.
"""

from __future__ import annotations

import json

from . import common
from .copywriting import (
    _agent_rules_bundle,
    _normalize_naming_result,
)

# ---------------------------------------------------------------------------
# 공용 헬퍼: 에이전트 규칙 텍스트 로더.
#
# ``copywriting._agent_rules_bundle()`` 을 재사용한다 — 패키지 내 2종 스키마/로더
# 금지 원칙. 단일 진실 공급원.
# ---------------------------------------------------------------------------


def _agent_rule_text(filename):
    """단일 ``agents/*.md`` 파일의 원문 텍스트를 반환.

    ``copywriting._agent_rules_bundle`` 은 파일별 캐시를 유지하므로, 본 함수는
    그 캐시를 통해 읽는 얇은 래퍼다. 읽기 실패 시 빈 문자열을 반환하여
    호출자가 항상 프롬프트를 조립할 수 있도록 한다.
    """
    try:
        return str(_agent_rules_bundle(filename) or "")
    except FileNotFoundError:
        return ""


def _rules_dict(*filenames):
    """복수 ``agents/*.md`` 파일을 ``{filename: text}`` 사전으로 로드.

    누락 파일은 빈 문자열로 채운다. 원본 ``qa_copy`` 가 ``_agent_rule_text``
    을 3회 호출해 사전을 만들던 패턴을 보존한다.
    """
    return {name: _agent_rule_text(name) for name in filenames}


# ---------------------------------------------------------------------------
# naming_agent — 판단 위임 계약 (프롬프트 축약 금지).
#
# 원본은 ``_agent_llm_json`` → ``_llm_generate`` (CLI 디스패치) 를 호출했다.
# 본 이식판은 ``common._llm_hint()`` 디스크립터를 반환하여 MCP 호스트 LLM 이
# 실행하도록 위임한다. 호스트가 회신한 JSON 은 호출자가
# :func:`copywriting._normalize_naming_result` 로 정규화한다.
# ---------------------------------------------------------------------------


def naming_agent(source_title, props, category_path):
    """네이밍 에이전트 판단 위임 디스크립터 반환.

    원본 프롬프트 조립 순서를 축약 없이 보존한다:
      1. 페르소나: "You are the Naming Agent."
      2. 준거 규칙 교차 참조: ``COMPLIANCE_RULES §7, §9`` + ``naming_agent.md``
      3. 형식/길이: 6-9 공백 유닛, 약 40-50 한국어 자, 앞가중치 구조
      4. 선택 지시: 저관련어 컷, 색상 ≤1, 배치 명사 ≤1
      5. 동의어 중복 접기 (예시 포함)
      6. 타 카테고리 스펙 용어 금지 (예시 포함)
      7. 출력 JSON 키: ``title, dropped, kept_keywords, story_terms``
      8. ``<COMPLIANCE_RULES.md>...</COMPLIANCE_RULES.md>`` 블록
      9. ``<naming_agent.md>...</naming_agent.md>`` 블록
      10. ``INPUT JSON:`` + 페이로드

    Args:
        source_title: 한국어 원제목 (비어있으면 ``ValueError``).
        props: 상품 속성 구조 (dict/list).
        category_path: 카테고리 경로 문자열.

    Returns:
        ``llm_hint`` 디스크립터 dict. 호스트 LLM 은 ``instruction`` 을 실행하여
        naming-result JSON 을 반환해야 한다. 호출자는 결과를
        :func:`copywriting._normalize_naming_result` 로 정규화한다.

    Raises:
        ValueError: ``source_title`` 이 빈 문자열/공백인 경우.
    """
    if not str(source_title or "").strip():
        raise ValueError("source_title must be a non-empty string for naming_agent")
    from .text_props import _flatten_prop_terms

    rules = _rules_dict("COMPLIANCE_RULES.md", "naming_agent.md")
    payload = {
        "source_title": str(source_title or ""),
        "props": _flatten_prop_terms(props, limit=30, clean=False),
        "category_path": str(category_path or ""),
    }
    instruction = (
        "You are the Naming Agent. Read and follow the markdown rules below; "
        "they are the source of truth. For naming, apply COMPLIANCE_RULES §7 "
        "and §9 and the naming_agent.md schema. Generate a readable Korean "
        "SEO phrase, not a keyword dump: 6-9 whitespace units, about 40-50 "
        "Korean chars, front-loaded with the best product-relevant core "
        "phrase such as material+product or use+product. Use structure "
        "[core product type] [2-3 style/features] [product synonym/name] "
        "[one placement/use context]. Do not include every high-volume "
        "keyword; cut lower relevance terms when the unit cap is reached. "
        "Allow at most one color only when explicit in product context, and "
        "at most one placement noun after the core. Collapse duplicate "
        "interior-context words such as 인테리어/거실인테리어/사무실인테리어/"
        "홈데코/소품샵 into one. Use only this product type, use, material, "
        "style, and adjacent interior context; never add other-category spec "
        "terms such as lighting 조도/루멘/색온도 for non-lighting products, "
        "appliance 소비전력/용량 specs for non-appliances, furniture terms "
        "such as 접이식테이블/의자 for non-furniture, or shoe size 250-290 "
        "for non-shoes. Return JSON only with keys: title, dropped, "
        "kept_keywords, story_terms.\n\n"
        f"<COMPLIANCE_RULES.md>\n{rules['COMPLIANCE_RULES.md']}\n"
        "</COMPLIANCE_RULES.md>\n\n"
        f"<naming_agent.md>\n{rules['naming_agent.md']}\n"
        "</naming_agent.md>\n\n"
        f"INPUT JSON:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )
    return common._llm_hint(
        "naming_agent",
        input=payload,
        instruction=instruction,
    )


def normalize_naming_response(data, source_title, props, category_path):
    """호스트 LLM 의 naming 회신을 정규화 (copywriting 재사용).

    ``copywriting._normalize_naming_result`` 의 얇은 래퍼. 패키지 내 2종
    정규화 스키마 금지 원칙을 지킨다. ``data`` 가 dict 가 아니거나 유효하지
    않은 title 이면 fallback 결과로 떨어진다.
    """
    return _normalize_naming_result(data, source_title, props, category_path)


# ---------------------------------------------------------------------------
# qa_copy_agent — 카피/텍스트 QA 판단 위임 계약.
#
# 원본은 ``_agent_llm_json(prompt, purpose="copy_qa")`` 로 LLM 을 호출했다.
# 본 이식판은 ``common._llm_hint()`` 디스크립터를 반환한다. 호스트가
# 회신한 ``{verdict, violations, summary}`` JSON 은 호출자가
# :mod:`qa_agents` 의 정규화 함수로 처리한다.
# ---------------------------------------------------------------------------


def qa_copy_agent(name, context, detail_text):
    """카피 QA 판단 위임 디스크립터 반환.

    원본 프롬프트 조립 순서를 축약 없이 보존한다:
      1. 페르소나: "You are qa_copy, the copy/text QA agent."
      2. 준거 규칙 교차 참조: ``QA_AGENTS.md qa_copy`` + ``COMPLIANCE_RULES
         §4/§7/§13`` + ``COPY_GUIDE``
      3. 하이브리드 카피 규칙 명시 (구 감성금지 체크리스트 무효화)
      4. 점검 항목 열거 (제목 순서/금지어, 타카테고리 스펙, 동의어 중복,
         옵션 번역 충실도, 빈 마케팅, 가짜 통계, 금지 표현)
      5. 이미지는 보조 자료로만 사용 지시
      6. 출력 JSON 키: ``verdict, violations, summary``
      7. ``<COMPLIANCE_RULES.md>...</COMPLIANCE_RULES.md>`` 블록
      8. ``<QA_AGENTS.md>...</QA_AGENTS.md>`` 블록
      9. ``<COPY_GUIDE.md>...</COPY_GUIDE.md>`` 블록
      10. ``INPUT JSON:`` + 페이로드

    Args:
        name: 상품명 문자열.
        context: QA 컨텍스트 dict (옵션, props, 카테고리 등).
        detail_text: 상세페이지 텍스트 (최대 5000자).

    Returns:
        ``llm_hint`` 디스크립터 dict.
    """
    rules = _rules_dict("COMPLIANCE_RULES.md", "QA_AGENTS.md", "COPY_GUIDE.md")
    payload = {
        "name": str(name or ""),
        "context": context if isinstance(context, dict) else {},
        "detail_text": str(detail_text or "")[:5000],
    }
    instruction = (
        "You are qa_copy, the copy/text QA agent. Apply QA_AGENTS.md qa_copy "
        "plus COMPLIANCE_RULES §4/§7/§13 and COPY_GUIDE. If older checklist "
        "wording bans all emotional body copy, supersede it with the current "
        "hybrid rule: body/DETAIL/option-card copy may mix fresh concrete "
        "mood with functional benefits. Do not warn or fail concrete "
        "product-context mood words by themselves. Check title order/banned "
        "terms, title off-category spec terms, same-meaning synonym "
        "duplication such as 화병/꽃병/플라워베이스/vase, faithful option "
        "translation, empty marketing with no product content, fake "
        "statistics, and forbidden claims. Use the image only as visual "
        "support. Return JSON only with keys: verdict, violations, "
        "summary.\n\n"
        f"<COMPLIANCE_RULES.md>\n{rules['COMPLIANCE_RULES.md']}\n"
        "</COMPLIANCE_RULES.md>\n\n"
        f"<QA_AGENTS.md>\n{rules['QA_AGENTS.md']}\n</QA_AGENTS.md>\n\n"
        f"<COPY_GUIDE.md>\n{rules['COPY_GUIDE.md']}\n</COPY_GUIDE.md>\n\n"
        f"INPUT JSON:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )
    return common._llm_hint(
        "copy_qa",
        input=payload,
        instruction=instruction,
    )


__all__ = [
    "_agent_rule_text",
    "_rules_dict",
    "naming_agent",
    "normalize_naming_response",
    "qa_copy_agent",
]
