# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
"""실측 속성값 픽스처만 사용하는 조회 경로 검증."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import mcp_server, naver_client

_FIXTURE_PATH = _ROOT / "tests" / "fixtures" / "category_attribute_values_50000830.json"
_NOTE = "2026-08-12 카테고리 50000830 1건으로 확인. 다른 카테고리 형태는 미확인."


def _fixture_values() -> list[dict]:
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


def test_client_uses_measured_external_endpoint_and_query_parameters():
    """실측 픽스처를 본문으로 써서 외부 경로와 두 쿼리 파라미터를 검증한다."""
    fixture_values = _fixture_values()
    captured: dict = {}
    response = mock.Mock()
    response.status_code = 200

    def capture(url, **kwargs):
        captured["url"] = url
        captured["params"] = kwargs.get("params")
        return response

    with mock.patch.object(naver_client, "get_token", return_value="fixture-token"):
        with mock.patch.object(naver_client.requests, "get", side_effect=capture):
            with mock.patch.object(
                naver_client, "_json_or_text_response", return_value=fixture_values
            ):
                status_code, body = naver_client.get_category_attribute_values("50000830", 10011015)

    assert status_code == 200
    assert body == fixture_values
    assert captured["url"] == (
        f"{naver_client.BASE}/external/v1/product-attributes/attribute-values"
    )
    assert captured["params"] == {"categoryId": "50000830", "attributeSeq": "10011015"}


def test_mcp_preserves_measured_attribute_value_list_and_groups_by_attribute_seq(monkeypatch):
    """125건 원본·10개 속성 묶음·핏 5건을 픽스처에서 직접 검증한다."""
    fixture_values = _fixture_values()

    def fixture_api(category_id, attribute_seq, tk=None):
        assert category_id == "50000830"
        assert attribute_seq == "10011015"
        return 200, fixture_values

    monkeypatch.setattr(naver_client, "get_category_attribute_values", fixture_api)

    result = mcp_server.get_category_attribute_values("50000830", 10011015)
    grouped = {}
    for value in result["attribute_values"]:
        grouped.setdefault(value["attributeSeq"], []).append(value)

    assert result["ok"] is True
    assert result["status_code"] == 200
    assert result["schema_verified"] is True
    assert result["note"] == _NOTE
    assert result["raw_body"] == fixture_values
    assert result["attribute_values"] == fixture_values
    assert result["raw_body_truncated"] is False
    assert result["error"] is None
    assert len(result["attribute_values"]) == 125
    assert len(grouped) == 10
    assert all(
        set(value) == {"attributeSeq", "attributeValueSeq", "minAttributeValue", "exposureOrder"}
        for value in result["attribute_values"]
    )
    assert [value["minAttributeValue"] for value in grouped[10011015]] == [
        "슬림핏",
        "기본핏",
        "루즈핏/오버핏",
        "머슬핏",
        "기타",
    ]


def test_mcp_tool_remains_registered_with_eleven_tools():
    """속성값 MCP 도구가 실제 런타임 목록에 보이며 도구 수가 11개다."""
    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = {tool.name for tool in tools}

    assert "get_category_attribute_values" in names
    assert len(tools) == 11
