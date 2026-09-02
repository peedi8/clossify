# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""실측 카테고리 속성값에서 문자 일치 후보만 제시하는 순수 함수.

이 모듈은 네트워크, 설정, 전송 payload에 접근하지 않는다. 입력으로 받은
속성·속성값과 상품명/상세 텍스트만 비교해 사람이 고를 후보를 만든다.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

# 실측값 ``기타``는 두 글자짜리 포괄 라벨이다. ``기타상품`` 같은 한 단어 안의
# 부분 문자열까지 고르면 오탐이 된다. 따라서 이 *표시명 그대로* 앞뒤 단어 경계를
# 확인한다. 동의어·유추를 추가하지 않으며, 독립된 ``기타``라는 원문 일치는 유지한다.
_SHORT_CATCH_ALL_VALUES_REQUIRING_BOUNDARY = frozenset({"기타"})

# ---------------------------------------------------------------------------
# 전문 텍스트(source_text) 4단 결정론 매처 — 워크오더 기반 구현.
# LLM 0, 임베딩 0, 닫힌 어휘 매칭. 상품명·옵션명 경로(기존 문자 일치)와
# 독립적으로 source_text 필드에만 적용된다.
# ---------------------------------------------------------------------------

# 단계 2 — 정규화: 공백·구분자를 압축한 대조본을 만든다(원문 위치 사상 포함).
_SOURCE_TEXT_SEPARATOR_RE = re.compile(r"[\s\-_/·,()]+")

# 단계 4 — 부정 가드 토큰. 긴 토큰이 먼저 오도록 정렬(포함 관계 방지).
_SOURCE_TEXT_NEGATIVE_TOKENS: tuple[str, ...] = (
    "미포함",
    "비대상",
    "불가",
    "금지",
    "없음",
    "않",
    "없",
)

# 부정 가드 창 크기(일치 지점 앞뒤 각각, 계약상 6~10자 중 8자 채택).
_SOURCE_TEXT_GUARD_WINDOW = 8

# 단계 3 — 극성 접미. 접미를 *무조건 떼지 않는다*: 라벨의 극성(긍정 "포함/
# 가능" vs 부정 "미포함")을 보존해 판정한다. 접미를 떼어 "뚜껑포함" 과
# "뚜껑미포함" 이 같은 키("뚜껑")로 합쳐지면 부정형 라벨 특칙이 정반대로
# 작동한다(실물 오신고: 뚜껑미포함 전문에 뚜껑포함 자동 채용).
_POLARITY_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("사용가능", "positive"),
    ("가능", "positive"),
    ("미포함", "negative"),
    ("포함", "positive"),
)

# 극성 라벨의 스템 직후 창에서 극성 표지를 찾는다(한국어 어순상 극성 표지는
# 스템 뒤에 온다: "뚜껑 미포함", "오븐 사용 가능", "전자레인지 사용 불가").
_POLARITY_AFTER_WINDOW = 6

# 질문 접미 방어(속성명 오염). facts 의 이름(name)은 질문(라벨)이지 주장이
# 아니다. 통짜 source_text 에 "컵 뚜껑 포함 여부 = 뚜껑 미포함" 처럼 질문
# 문구가 값 앞에 붙어 오면, "뚜껑 포함" 이라는 *이름* 이 긍정 일치해 값
# "미포함" 을 뒤집는 오염이 생긴다(실물 실증). 라벨 일치 뒤에 곧바로
# "여부" 가 오면 속성명 문맥으로 보고 "여부" 뒤 실제 값까지 창을 넓혀
# 극성을 재판정한다.
_QUESTION_SUFFIX = "여부"
_QUESTION_MARKERS: tuple[str, ...] = (
    "미포함",
    "포함",
    "사용",
    "가능",
    "불가",
)
# "여부" 뒤 실제 값을 읽는 확장 창("포함 여부 = 뚜껑 미포함" 의 값 부분).
_QUESTION_VALUE_WINDOW = 10

# 스템 직후 긍정 표지(닫힌 어휘). 부정 토큰을 먼저 검사하므로 "불가능" 은
# 부정("불가")이 이긴다.
_POSITIVE_MARKERS: tuple[str, ...] = (
    "포함",
    "가능",
    "있음",
    "있",
)

# 단계 6 — 동의어 최소 시드(코드 상수). 확장은 데이터 파일로 뺄 수 있는
# 구조(그룹 튜플)로 둔다. 이번엔 시드만.
_SOURCE_TEXT_SYNONYM_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"도자기", "세라믹"}),
    frozenset({"스텐", "스테인리스"}),
)

# 접미·컴포넌트 키로 쓰기에 의미 있는 최소 길이(2자 미만은 오탐 방지 제외).
_SOURCE_TEXT_MIN_KEY_LENGTH = 2


def _product_fields(product_text: Any) -> tuple[tuple[str, str], ...]:
    """상품 입력을 근거 표시에 쓰는 ``(필드명, 원문)`` 쌍으로 고정한다.

    문자열 입력은 상품명으로 취급한다. 매핑 입력은 ``name`` 과 ``detail`` 만
    읽는다. 다른 필드를 추측하거나 결합하지 않아 근거의 출처가 흐려지지 않는다.
    """
    if isinstance(product_text, Mapping):
        return (
            ("name", str(product_text.get("name") or "")),
            ("detail", str(product_text.get("detail") or "")),
        )
    return (("name", str(product_text or "")),)


def _is_word_char(character: str) -> bool:
    """한글·영숫자를 같은 단어의 구성 문자로 본다."""
    return character.isalnum()


def _literal_position(field_text: str, value: str) -> int | None:
    """원문 문자열 일치의 첫 위치를 반환하고, ``기타`` 오탐만 좁힌다."""
    start = field_text.find(value)
    while start >= 0:
        end = start + len(value)
        needs_boundary = value in _SHORT_CATCH_ALL_VALUES_REQUIRING_BOUNDARY
        before_is_word = start > 0 and _is_word_char(field_text[start - 1])
        after_is_word = end < len(field_text) and _is_word_char(field_text[end])
        if not needs_boundary or (not before_is_word and not after_is_word):
            return start
        start = field_text.find(value, start + 1)
    return None


def _exposure_sort_key(value: Mapping[str, Any], original_index: int) -> tuple[int, int, int]:
    """명시된 노출 순서가 먼저 오고, 없는 순서는 원본 순서를 보존한다."""
    exposure_order = value.get("exposureOrder")
    if isinstance(exposure_order, int) and not isinstance(exposure_order, bool):
        return (0, exposure_order, original_index)
    return (1, 0, original_index)


def _matching_limit(attribute: Mapping[str, Any]) -> int:
    """실측 계약의 선택 상한을 보수적으로 해석한다."""
    if attribute.get("attributeClassificationCodeName") == "선택형":
        return 1

    # ``attributeValueMaxMatchingCount == 0``의 의미는 실응답만으로 확인되지
    # 않았다. 무제한으로 추측하지 않고 1개만 제시한다.
    maximum = attribute.get("attributeValueMaxMatchingCount")
    if isinstance(maximum, int) and not isinstance(maximum, bool) and maximum > 0:
        return maximum
    return 1


def _candidate_item(value: Mapping[str, Any]) -> dict[str, Any]:
    """반환 계약에 필요한 실측 속성값 필드만 복사한다."""
    return {
        "attributeValueSeq": value.get("attributeValueSeq"),
        "minAttributeValue": value.get("minAttributeValue"),
    }


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    """구분자를 압축한 대조본과 원문 위치 사상을 함께 만든다.

    반환된 ``offsets[i]`` 는 압축본 i 번째 글자의 원문 인덱스다. 부정 가드
    창을 원문 위에서 잡기 위해 사상이 필요하다(압축본만 쓰면 위치 복원 불가).
    """
    normalized_chars: list[str] = []
    offsets: list[int] = []
    for index, character in enumerate(text):
        if _SOURCE_TEXT_SEPARATOR_RE.fullmatch(character):
            continue
        # 영문 대소문자 차이는 구분자와 같은 격의 노이즈다.
        normalized_chars.append(character.lower())
        offsets.append(index)
    return "".join(normalized_chars), offsets


def _synonym_variants(key: str) -> list[str]:
    """동의어 시드 그룹으로 키의 교체 변형을 만든다(시드 상수뿐)."""
    variants: list[str] = []
    for group in _SOURCE_TEXT_SYNONYM_GROUPS:
        for member in sorted(group):
            if member in key:
                for other in sorted(group):
                    if other != member:
                        variant = key.replace(member, other)
                        if variant not in variants:
                            variants.append(variant)
    return variants


def _label_match_keys(label: str) -> list[str]:
    """극성 없는(중립) 라벨의 대조 키 후보를 만든다.

    순서 원칙: ① 정규화 라벨 전체(가장 구체적) → ② 구분자로 쪼갠 대체
    컴포넌트(예: "도자기/세라믹" 의 "세라믹") → ③ 각각의 동의어 변형.
    첫 일치 키가 판정을 결정한다(포괄 키로 뒤집지 않는다 — fail-safe).
    극성 접미("포함/미포함/사용가능/가능")는 여기서 떼지 않는다 — 극성
    라벨은 ``_match_polar_label`` 경로로 간다(오매칭 방지 계약).
    """
    normalized, _ = _normalize_with_map(label)
    keys: list[str] = [normalized]
    for component in _SOURCE_TEXT_SEPARATOR_RE.split(label):
        stripped = component.strip().lower()
        if len(stripped) >= _SOURCE_TEXT_MIN_KEY_LENGTH and stripped not in keys:
            keys.append(stripped)
    expanded: list[str] = []
    for key in keys:
        expanded.append(key)
        for variant in _synonym_variants(key):
            if variant not in expanded:
                expanded.append(variant)
    return expanded


def _stem_and_polarity(normalized_label: str) -> tuple[str, str | None]:
    """극성 접미를 인식해 ``(스템, 극성)`` 을 반환한다.

    "뚜껑포함"→("뚜껑", 긍정), "뚜껑미포함"→("뚜껑", 부정), "오븐사용가능"→
    ("오븐", 긍정). 접미가 없으면 ``(라벨 전체, None)`` — 중립 경로.
    접미가 붙은 라벨은 극성을 떼어 버리지 않고 스템+극성 쌍으로 보존하므로
    서로 반대인 두 라벨이 절대 같은 키로 합쳐지지 않는다.
    """
    for suffix, polarity in _POLARITY_SUFFIXES:
        if normalized_label.endswith(suffix) and len(normalized_label) - len(suffix) >= (
            _SOURCE_TEXT_MIN_KEY_LENGTH
        ):
            return normalized_label[: -len(suffix)], polarity
    return normalized_label, None


def _first_token(text: str, tokens: tuple[str, ...]) -> str | None:
    """닫힌 어휘 중 텍스트에 있는 첫 토큰을 반환한다(없으면 ``None``)."""
    return next((token for token in tokens if token in text), None)


def _text_polarity(text: str) -> tuple[str, str] | None:
    """텍스트 창의 극성을 ``(극성, 표지)`` 로 판정한다(부정 우선)."""
    negative = _first_token(text, _SOURCE_TEXT_NEGATIVE_TOKENS)
    if negative is not None:
        return ("negative", negative)
    positive = _first_token(text, _POSITIVE_MARKERS)
    if positive is not None:
        return ("positive", positive)
    return None


def _question_value_offset(text_after_match: str) -> int | None:
    """일치 지점 직후가 “접미+여부” 질문 문맥이면 값 시작 오프셋을 반환한다.

    ``"...포함 여부"`` / ``"...사용 여부"`` / ``"...여부"``(접미 생략) 패턴을
    속성명(질문) 문맥으로 인정한다. 질문은 주장이 아니므로 이 오프셋 뒤의
    실제 값으로 극성을 재판정한다.
    """
    for marker in _QUESTION_MARKERS:
        if text_after_match.startswith(marker + _QUESTION_SUFFIX):
            return len(marker) + len(_QUESTION_SUFFIX)
    if text_after_match.startswith(_QUESTION_SUFFIX):
        return len(_QUESTION_SUFFIX)
    return None


def _match_neutral_label(
    field_name: str,
    field_text: str,
    label: str,
    normalized_text: str,
    offsets: list[int],
) -> dict[str, Any] | None:
    """중립 라벨(극성 접미 없음)의 기존 4단 판정 — 부정 가드 그대로.

    질문 접미 방어만 추가: 일치 뒤에 곧바로 "여부" 가 오면 속성명 문맥으로
    보고 이 일치로 자동 판정하지 않는다. "여부" 뒤 실제 값 창에서 부정
    토큰이 보이면 차단, 극성 표지가 없으면 다음 일치로 넘어간다.

    출현이 여러 개면 깨끗한(부정 가드에 안 걸리는) 출현이 자동 근거가
    된다(실물: "세라믹 뚜껑 미포함 ... 재질: 세라믹" — 인접 부정 오차).
    첫 키에서 출현을 전부 봤는데 전부 차단이면 차단으로 확정한다(fail-safe).
    """
    for key in _label_match_keys(label):
        # 1글자 키는 전문 산문에서 오탐률이 급등한다 — 최소 길이 미달 키는
        # 자동 판정 재료에서 뺀다(후보로 강등).
        if len(key) < _SOURCE_TEXT_MIN_KEY_LENGTH:
            continue
        position = normalized_text.find(key)
        blocked_result: dict[str, Any] | None = None
        found_any = False
        while position >= 0:
            found_any = True
            after_start = position + len(key)
            if normalized_text[after_start:].startswith(_QUESTION_SUFFIX):
                value_start = after_start + len(_QUESTION_SUFFIX)
                value_text = normalized_text[value_start : value_start + _QUESTION_VALUE_WINDOW]
                judged = _text_polarity(value_text)
                if judged is not None and judged[0] == "negative":
                    original_start = offsets[position]
                    original_end = offsets[position + len(key) - 1] + 1
                    blocked_result = blocked_result or {
                        "status": "blocked",
                        "label": label,
                        "evidence": f"{field_name}[{original_start}:{original_end}]:'{label}'",
                        "reason": f"부정 근접({judged[1]})",
                    }
                # 질문 문맥인데 값에 극성 표지가 없으면 증거 없음 — 건너뛴다.
                position = normalized_text.find(key, position + 1)
                continue
            original_start = offsets[position]
            original_end = offsets[position + len(key) - 1] + 1
            window = field_text[
                max(0, original_start - _SOURCE_TEXT_GUARD_WINDOW) : original_end
                + _SOURCE_TEXT_GUARD_WINDOW
            ]
            evidence = f"{field_name}[{original_start}:{original_end}]:'{label}'"
            guard_token = _first_token(window, _SOURCE_TEXT_NEGATIVE_TOKENS)
            if guard_token is not None:
                blocked_result = blocked_result or {
                    "status": "blocked",
                    "label": label,
                    "evidence": evidence,
                    "reason": f"부정 근접({guard_token})",
                }
                position = normalized_text.find(key, position + 1)
                continue
            return {
                "status": "auto",
                "label": label,
                "evidence": evidence,
                "reason": None,
            }
        # 이 키의 출현이 전부 가드에 걸렸다 — 차단 확정(다음 키로 흐리지 않는다).
        if found_any and blocked_result is not None:
            return blocked_result
    return None


def _polar_decision(
    field_name: str,
    label: str,
    polarity: str,
    offsets: list[int],
    position: int,
    key: str,
    found: str,
    token: str,
) -> dict[str, Any]:
    """전문 극성(``found``)과 라벨 극성(``polarity``) 대조로 판정을 내린다."""
    original_start = offsets[position]
    original_end = offsets[position + len(key) - 1] + 1
    evidence = f"{field_name}[{original_start}:{original_end}]:'{label}'"
    if found == "negative":
        # 전문이 부정 — 부정 라벨만 자동, 긍정 라벨은 차단.
        if polarity == "negative":
            return {"status": "auto", "label": label, "evidence": evidence, "reason": None}
        return {
            "status": "blocked",
            "label": label,
            "evidence": evidence,
            "reason": f"부정 근접({token})",
        }
    # 전문이 긍정 — 긍정 라벨만 자동, 부정 라벨은 차단.
    if polarity == "positive":
        return {"status": "auto", "label": label, "evidence": evidence, "reason": None}
    return {
        "status": "blocked",
        "label": label,
        "evidence": evidence,
        "reason": f"긍정 근접({token})",
    }


def _match_polar_label(
    field_name: str,
    label: str,
    stem: str,
    polarity: str,
    normalized_text: str,
    offsets: list[int],
) -> dict[str, Any] | None:
    """극성 라벨 판정 — 전문 극성과 라벨 극성이 같을 때만 자동.

    스템 직후 창에서 극성 표지를 읽는다(한국어 어순). 부정 토큰 우선:
    "불가능" 은 긍정("가능")이 아니라 부정("불가")으로 판정된다.

    질문 접미 방어: 스템(또는 스템+접미) 일치 뒤에 곧바로 "여부" 가 오면
    그 일치는 속성명 문맥("...포함 여부")이다 — 이름의 "포함" 이 값의
    "미포함" 을 뒤집는 오염(실물 실증)을 막는다. "여부" 뒤 실제 값까지
    창을 넓혀 극성을 재판정하고, 값에 극성 표지가 없으면 그 출현은
    건너뛴다 — 증거 없는 자동도 차단도 하지 않는다.
    """
    for key in [stem, *_synonym_variants(stem)]:
        if len(key) < _SOURCE_TEXT_MIN_KEY_LENGTH:
            continue
        position = normalized_text.find(key)
        while position >= 0:
            after_start = position + len(key)
            after = normalized_text[after_start : after_start + _POLARITY_AFTER_WINDOW]
            question_offset = _question_value_offset(after)
            if question_offset is not None:
                value_start = after_start + question_offset
                value_text = normalized_text[value_start : value_start + _QUESTION_VALUE_WINDOW]
                judged = _text_polarity(value_text)
                if judged is not None:
                    return _polar_decision(
                        field_name,
                        label,
                        polarity,
                        offsets,
                        position,
                        key,
                        judged[0],
                        judged[1],
                    )
                position = normalized_text.find(key, position + 1)
                continue
            judged = _text_polarity(after)
            if judged is not None:
                return _polar_decision(
                    field_name,
                    label,
                    polarity,
                    offsets,
                    position,
                    key,
                    judged[0],
                    judged[1],
                )
            position = normalized_text.find(key, position + 1)
    return None


def match_source_label(field_name: str, field_text: str, label: str) -> dict[str, Any] | None:
    """전문 텍스트 위에서 라벨 1개의 4단 결정론 매칭을 판정한다.

    극성 접미("포함/미포함/사용가능/가능")가 있는 라벨은 스템 직후 극성
    표지와 라벨 극성의 일치로 판정한다(``_match_polar_label``). 그 외
    중립 라벨은 기존 부정 가드 판정을 그대로 쓴다(``_match_neutral_label``).

    Returns:
        일치가 없으면 ``None``. 있으면 판정 딕셔너리:

        * ``status="auto"`` — 명시 긍정 일치(자동 채용 대상).
        * ``status="blocked"`` — 부정 가드 차단(후보 강등, 사유 표기 대상).
        * 공통 키: ``label``(원형 라벨), ``evidence``, ``reason``.
    """
    normalized_label, _ = _normalize_with_map(label)
    if not normalized_label:
        return None
    normalized_text, offsets = _normalize_with_map(field_text)
    stem, polarity = _stem_and_polarity(normalized_label)
    if polarity is None:
        return _match_neutral_label(field_name, field_text, label, normalized_text, offsets)
    return _match_polar_label(field_name, label, stem, polarity, normalized_text, offsets)


def suggest_category_attributes(
    product_text: str | Mapping[str, Any],
    attributes: Sequence[Mapping[str, Any]],
    attribute_values: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """상품 원문에 문자 그대로 있는 카테고리 속성값 후보를 제시한다.

    Args:
        product_text: 상품명 문자열 또는 ``{"name": str, "detail": str}`` 매핑.
        attributes: 카테고리 속성 원본 목록.
        attribute_values: 카테고리 속성값 원본 목록.

    Returns:
        각 속성의 후보·선택 결과. 일치한 표시명이 없으면 ``status`` 는
        ``"unknown"`` 이고 ``selected`` 는 빈 리스트다.
    """
    fields = _product_fields(product_text)
    # 전문 텍스트(source_text) 는 기존 문자 일치 경로에 넣지 않고 4단 결정론
    # 매처(match_source_label) 로만 판정한다 — 회귀 계약: source_text 가 없으면
    # 아래 경로 전체가 기존 동작과 동일하다.
    source_field: tuple[str, str] | None = None
    if isinstance(product_text, Mapping):
        _source_text = product_text.get("source_text")
        if isinstance(_source_text, str) and _source_text.strip():
            source_field = ("source_text", _source_text)
    values_by_attribute: dict[Any, list[tuple[int, Mapping[str, Any]]]] = {}
    for value_index, value in enumerate(attribute_values):
        if isinstance(value, Mapping):
            values_by_attribute.setdefault(value.get("attributeSeq"), []).append(
                (value_index, value)
            )

    ordered_attributes = [
        (attribute_index, attribute)
        for attribute_index, attribute in enumerate(attributes)
        if isinstance(attribute, Mapping)
    ]
    ordered_attributes.sort(
        key=lambda item: (
            0 if item[1].get("attributeTypeCodeName") == "주요" else 1,
            item[0],
        )
    )

    suggestions: list[dict[str, Any]] = []
    for _, attribute in ordered_attributes:
        attribute_seq = attribute.get("attributeSeq")
        attribute_values_for_seq = values_by_attribute.get(attribute_seq, [])
        sorted_values = sorted(
            attribute_values_for_seq,
            key=lambda item: _exposure_sort_key(item[1], item[0]),
        )
        candidates = [_candidate_item(value) for _, value in sorted_values]

        matches: list[tuple[Mapping[str, Any], str]] = []
        blocked: list[dict[str, Any]] = []
        for _, value in sorted_values:
            display_name = value.get("minAttributeValue")
            if not isinstance(display_name, str) or not display_name:
                continue
            literal_matched = False
            for field_name, field_text in fields:
                position = _literal_position(field_text, display_name)
                if position is not None:
                    end = position + len(display_name)
                    evidence = f"{field_name}[{position}:{end}]:'{display_name}'"
                    matches.append((value, evidence))
                    literal_matched = True
                    break
            # 상품명·옵션명 경로에서 이미 일치했으면 전문 재판정하지 않는다.
            if source_field is not None and not literal_matched:
                verdict = match_source_label(source_field[0], source_field[1], display_name)
                if verdict is not None:
                    if verdict["status"] == "auto":
                        matches.append((value, verdict["evidence"]))
                    else:
                        blocked.append(
                            {
                                "attributeValueSeq": value.get("attributeValueSeq"),
                                "minAttributeValue": display_name,
                                "evidence": verdict["evidence"],
                                "reason": verdict["reason"],
                            }
                        )

        limit = _matching_limit(attribute)
        selected_matches = matches[:limit]
        selected = [
            {
                **_candidate_item(value),
                "evidence": evidence,
            }
            for value, evidence in selected_matches
        ]
        row = {
            "attributeSeq": attribute_seq,
            "attributeName": attribute.get("attributeName"),
            "attributeTypeCodeName": attribute.get("attributeTypeCodeName"),
            "classification": attribute.get("attributeClassificationCodeName"),
            "status": "matched" if selected else "unknown",
            "selected": selected,
            "candidates": candidates,
            "truncated": len(matches) > len(selected_matches),
        }
        # 부정 가드로 차단(후보 강등)된 속성값 — 조용한 생략 금지(워크오더 5).
        if blocked:
            row["blocked"] = blocked
        suggestions.append(row)

    return suggestions


__all__ = ["match_source_label", "suggest_category_attributes"]
