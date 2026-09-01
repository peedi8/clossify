# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""상품속성 추천 MCP 도구의 픽스처 전용 회귀 테스트."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import mcp_server, naver_client

_FIXTURES = _ROOT / "tests" / "fixtures"
_ATTRIBUTES_PATH = _FIXTURES / "category_attributes_50000830.json"
_VALUES_PATH = _FIXTURES / "category_attribute_values_50000830.json"
_CATEGORY_ID = "50000830"
_PRODUCT_NAME = "브이앤에프 특양면 헬스오버핏반팔 두꺼운반팔"


def _fixture_inputs() -> tuple[list[dict], list[dict]]:
    return (
        json.loads(_ATTRIBUTES_PATH.read_text(encoding="utf-8")),
        json.loads(_VALUES_PATH.read_text(encoding="utf-8")),
    )


def _patch_fixture_responses(monkeypatch: pytest.MonkeyPatch) -> tuple[list[dict], list[dict]]:
    attributes, values = _fixture_inputs()

    def get_attributes(category_id: str):
        assert category_id == _CATEGORY_ID
        return 200, attributes

    def get_attribute_values(category_id: str, attribute_seq: int):
        assert category_id == _CATEGORY_ID
        assert attribute_seq == attributes[0]["attributeSeq"]
        return 200, values

    monkeypatch.setattr(naver_client, "get_category_attributes", get_attributes)
    monkeypatch.setattr(naver_client, "get_category_attribute_values", get_attribute_values)
    return attributes, values


def _by_name(suggestions: list[dict]) -> dict[str, dict]:
    return {suggestion["attributeName"]: suggestion for suggestion in suggestions}


def test_name_only_matches_material_and_sleeve_but_keeps_the_rest_unknown(monkeypatch):
    _patch_fixture_responses(monkeypatch)

    result = mcp_server.suggest_product_attributes(_CATEGORY_ID, _PRODUCT_NAME)
    by_name = _by_name(result["suggestions"])

    assert result["ok"] is True
    assert result["category_attributes_status_code"] == 200
    assert result["attribute_values_status_code"] == 200
    assert by_name["주요소재"]["status"] == "matched"
    assert by_name["소매기장"]["status"] == "matched"
    assert by_name["주요소재"]["selected"][0]["minAttributeValue"] == "면"
    assert by_name["소매기장"]["selected"][0]["minAttributeValue"] == "반팔"
    assert all(
        suggestion["status"] == "unknown"
        for name, suggestion in by_name.items()
        if name not in {"주요소재", "소매기장"}
    )
    assert "제안" in result["note"]
    assert "register_product" in result["note"]


def test_detail_text_adds_round_neck_and_muscle_fit_for_four_matches(monkeypatch):
    _patch_fixture_responses(monkeypatch)

    result = mcp_server.suggest_product_attributes(
        _CATEGORY_ID, _PRODUCT_NAME, detail_html="라운드넥 머슬핏"
    )
    matched = [
        suggestion["attributeName"]
        for suggestion in result["suggestions"]
        if suggestion["status"] == "matched"
    ]

    assert result["ok"] is True
    assert set(matched) == {"주요소재", "소매기장", "네크라인", "핏"}


def test_html_uses_visible_body_text_and_ignores_attribute_values(monkeypatch):
    _patch_fixture_responses(monkeypatch)

    result = mcp_server.suggest_product_attributes(
        _CATEGORY_ID,
        "속성값이 없는 상품명",
        detail_html='<p>머슬핏</p><img alt="면">',
    )
    by_name = _by_name(result["suggestions"])

    assert result["ok"] is True
    assert by_name["핏"]["status"] == "matched"
    assert by_name["핏"]["selected"][0]["evidence"].startswith("detail[")
    assert by_name["주요소재"]["status"] == "unknown"


def test_attribute_lookup_404_preserves_status_code_and_raw_body(monkeypatch):
    raw_body = {"code": "NOT_FOUND", "message": "category is missing"}

    monkeypatch.setattr(
        naver_client,
        "get_category_attributes",
        lambda category_id: (404, raw_body),
    )
    monkeypatch.setattr(
        naver_client,
        "get_category_attribute_values",
        lambda *args: pytest.fail("속성 목록 조회 실패 뒤 속성값 조회가 실행되면 안 됩니다."),
    )

    result = mcp_server.suggest_product_attributes(_CATEGORY_ID, _PRODUCT_NAME)

    assert result["ok"] is False
    assert result["stage"] == "category_attributes"
    assert result["status_code"] == 404
    assert result["raw_body"] == raw_body
    assert result["category_attributes_status_code"] == 404
    assert result["category_attributes_raw_body"] == raw_body
    assert result["suggestions"] is None


def test_empty_attribute_value_list_is_not_a_quiet_success(monkeypatch):
    attributes, _ = _fixture_inputs()
    monkeypatch.setattr(
        naver_client, "get_category_attributes", lambda category_id: (200, attributes)
    )
    monkeypatch.setattr(naver_client, "get_category_attribute_values", lambda *args: (200, []))

    result = mcp_server.suggest_product_attributes(_CATEGORY_ID, _PRODUCT_NAME)

    assert result["ok"] is False
    assert result["stage"] == "attribute_values"
    assert result["status_code"] == 200
    assert result["raw_body"] == []
    assert result["suggestions"] is None


def test_attribute_values_lookup_404_preserves_both_raw_bodies(monkeypatch):
    attributes, _ = _fixture_inputs()
    raw_body = {"code": "NOT_FOUND", "message": "attribute values are missing"}
    monkeypatch.setattr(
        naver_client, "get_category_attributes", lambda category_id: (200, attributes)
    )
    monkeypatch.setattr(
        naver_client, "get_category_attribute_values", lambda *args: (404, raw_body)
    )

    result = mcp_server.suggest_product_attributes(_CATEGORY_ID, _PRODUCT_NAME)

    assert result["ok"] is False
    assert result["stage"] == "attribute_values"
    assert result["status_code"] == 404
    assert result["raw_body"] == raw_body
    assert result["category_attributes_raw_body"] == attributes
    assert result["attribute_values_status_code"] == 404
    assert result["attribute_values_raw_body"] == raw_body


def test_mcp_registers_the_new_tool_and_includes_it_in_the_total_count():
    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = {tool.name for tool in tools}

    assert "suggest_product_attributes" in names
    assert len(tools) == 12
