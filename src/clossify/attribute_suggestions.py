# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""실측 카테고리 속성값에서 문자 일치 후보만 제시하는 순수 함수.

이 모듈은 네트워크, 설정, 전송 payload에 접근하지 않는다. 입력으로 받은
속성·속성값과 상품명/상세 텍스트만 비교해 사람이 고를 후보를 만든다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

# 실측값 ``기타``는 두 글자짜리 포괄 라벨이다. ``기타상품`` 같은 한 단어 안의
# 부분 문자열까지 고르면 오탐이 된다. 따라서 이 *표시명 그대로* 앞뒤 단어 경계를
# 확인한다. 동의어·유추를 추가하지 않으며, 독립된 ``기타``라는 원문 일치는 유지한다.
_SHORT_CATCH_ALL_VALUES_REQUIRING_BOUNDARY = frozenset({"기타"})


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
        for _, value in sorted_values:
            display_name = value.get("minAttributeValue")
            if not isinstance(display_name, str) or not display_name:
                continue
            for field_name, field_text in fields:
                position = _literal_position(field_text, display_name)
                if position is not None:
                    end = position + len(display_name)
                    evidence = f"{field_name}[{position}:{end}]:'{display_name}'"
                    matches.append((value, evidence))
                    break

        limit = _matching_limit(attribute)
        selected_matches = matches[:limit]
        selected = [
            {
                **_candidate_item(value),
                "evidence": evidence,
            }
            for value, evidence in selected_matches
        ]
        suggestions.append(
            {
                "attributeSeq": attribute_seq,
                "attributeName": attribute.get("attributeName"),
                "attributeTypeCodeName": attribute.get("attributeTypeCodeName"),
                "classification": attribute.get("attributeClassificationCodeName"),
                "status": "matched" if selected else "unknown",
                "selected": selected,
                "candidates": candidates,
                "truncated": len(matches) > len(selected_matches),
            }
        )

    return suggestions


__all__ = ["suggest_category_attributes"]
