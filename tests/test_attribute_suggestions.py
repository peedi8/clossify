# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""실응답 속성 픽스처로만 검증하는 문자 일치 속성 후보 제시 시험."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify.attribute_suggestions import suggest_category_attributes

_FIXTURES = _ROOT / "tests" / "fixtures"
_ATTRIBUTES_PATH = _FIXTURES / "category_attributes_50000830.json"
_VALUES_PATH = _FIXTURES / "category_attribute_values_50000830.json"
_PRODUCT_NAME = "브이엔에프 특양면 헬스오버핏반팔 두꺼운반팔"


def _fixture_inputs() -> tuple[list[dict], list[dict]]:
    """실측 카테고리 50000830의 속성·속성값 원문만 읽는다."""
    return (
        json.loads(_ATTRIBUTES_PATH.read_text(encoding="utf-8")),
        json.loads(_VALUES_PATH.read_text(encoding="utf-8")),
    )


def _by_attribute_name(suggestions: list[dict]) -> dict[str, dict]:
    return {item["attributeName"]: item for item in suggestions}


def test_literal_name_match_has_real_name_position_and_main_attributes_come_first():
    """반팔은 실 상품명 원문 위치로만 일치하고 주요 속성이 먼저 나온다."""
    attributes, values = _fixture_inputs()

    suggestions = suggest_category_attributes(_PRODUCT_NAME, attributes, values)
    by_name = _by_attribute_name(suggestions)
    sleeve = by_name["소매기장"]

    assert sleeve["status"] == "matched"
    assert sleeve["selected"] == [
        {
            "attributeValueSeq": 10574793,
            "minAttributeValue": "반팔",
            "evidence": "name[15:17]:'반팔'",
        }
    ]
    assert sleeve["candidates"][-1] == {
        "attributeValueSeq": 10030656,
        "minAttributeValue": "기타",
    }
    types = [item["attributeTypeCodeName"] for item in suggestions]
    assert types == sorted(types, key=lambda value: 0 if value == "주요" else 1)


def test_fit_is_unknown_for_name_only_and_matches_only_when_detail_has_the_literal():
    """머슬핏은 이름에 없으면 비우고, 상세 원문에 있을 때만 선택한다."""
    attributes, values = _fixture_inputs()

    name_only = _by_attribute_name(suggest_category_attributes(_PRODUCT_NAME, attributes, values))[
        "핏"
    ]
    with_detail = _by_attribute_name(
        suggest_category_attributes(
            {"name": _PRODUCT_NAME, "detail": "운동 시 편안한 머슬핏 실루엣"},
            attributes,
            values,
        )
    )["핏"]

    assert name_only["status"] == "unknown"
    assert name_only["selected"] == []
    assert with_detail["status"] == "matched"
    assert with_detail["selected"] == [
        {
            "attributeValueSeq": 11439691,
            "minAttributeValue": "머슬핏",
            "evidence": "detail[9:12]:'머슬핏'",
        }
    ]


def test_short_catch_all_value_requires_a_word_boundary_to_avoid_false_positive():
    """`기타상품`의 부분 문자열 `기타`는 후보로 고르지 않는다."""
    attributes, values = _fixture_inputs()

    suggestions = suggest_category_attributes("기타상품", attributes, values)

    assert all(
        selected["minAttributeValue"] != "기타"
        for suggestion in suggestions
        for selected in suggestion["selected"]
    )
    assert all(suggestion["status"] == "unknown" for suggestion in suggestions)


def test_single_select_uses_exposure_order_and_reports_truncation():
    """실측 핏 선택형의 두 문자 일치는 노출 순서 첫 값 하나로 잘린다."""
    attributes, values = _fixture_inputs()

    fit = _by_attribute_name(suggest_category_attributes("기본핏 슬림핏", attributes, values))["핏"]

    assert fit["status"] == "matched"
    assert [selected["minAttributeValue"] for selected in fit["selected"]] == ["슬림핏"]
    assert fit["truncated"] is True


def test_zero_multi_select_limit_is_conservatively_one_and_reports_truncation():
    """실측 `종류`의 0 상한은 무제한으로 추측하지 않고 한 개만 제시한다."""
    attributes, values = _fixture_inputs()

    kind = _by_attribute_name(
        suggest_category_attributes("맨투맨(스웨트셔츠) 피케티셔츠", attributes, values)
    )["종류"]

    assert [selected["minAttributeValue"] for selected in kind["selected"]] == ["피케티셔츠"]
    assert kind["truncated"] is True


def test_no_literal_match_returns_all_unknown_without_an_exception():
    """실측 후보와 무관한 상품명은 전부 unknown이며 후보 목록은 유지한다."""
    attributes, values = _fixture_inputs()

    suggestions = suggest_category_attributes("완전히무관한상품명", attributes, values)

    assert len(suggestions) == len(attributes)
    assert all(item["status"] == "unknown" for item in suggestions)
    assert all(item["selected"] == [] for item in suggestions)
    assert all(item["candidates"] for item in suggestions)
