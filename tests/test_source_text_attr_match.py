# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""전문 텍스트 기반 속성 자동 매칭(4단 결정론) 인수 테스트.

워크오더(전문 텍스트 속성 자동 매칭) 대응:

  (a) 전문 "오븐 사용 가능" + 후보 "오븐사용가능" → 자동.
  (b) 전문 "뚜껑 미포함" + 후보 "뚜껑포함" → 차단(사유 표기) /
      후보 "뚜껑미포함" → 자동(부정형 라벨 특칙).
  (c) 전문 "전자레인지 사용 불가" + 후보 "전자레인지사용가능" → 차단.
  (d) 동의어: 전문 "세라믹" → 후보 "도자기/세라믹" 자동.
  (e) source_text 없으면 기존 동작과 동일(회귀).
  (f) 후보 밖 주장(텍스트에만 있는 말)은 아무것도 안 찍음.

suggest 경로는 전부 순수 함수·모킹 호출이다(외부 호출 0).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import attribute_suggestions, register

_ATTACH_MOCK = {
    "urls": ["http://cdn/test/img0.png"],
    "rejected": [],
    "notes": [],
}


def _attrs(*seqs: int) -> list[dict]:
    return [
        {
            "attributeSeq": seq,
            "attributeName": f"속성{seq}",
            "attributeTypeCodeName": "주요",
            "attributeClassificationCodeName": "선택형",
            "attributeValueMaxMatchingCount": 1,
        }
        for seq in seqs
    ]


def _vals(seq: int, *labels: str) -> list[dict]:
    return [
        {
            "attributeSeq": seq,
            "attributeValueSeq": seq * 100 + index,
            "minAttributeValue": label,
            "exposureOrder": index,
        }
        for index, label in enumerate(labels)
    ]


def _suggest(product, attributes, values):
    return attribute_suggestions.suggest_category_attributes(product, attributes, values)


def _by_label(row: dict) -> dict[str, dict]:
    return {item["minAttributeValue"]: item for item in row.get("blocked") or []}


# =========================================================================== #
# 인수 (a) — 접미("사용가능") 정규화 포함 일치 → 자동.
# =========================================================================== #
def test_a_oven_usable_auto():
    suggestions = _suggest(
        {"name": "머그", "source_text": "오븐 사용 가능한 세라믹 머그입니다."},
        _attrs(1),
        _vals(1, "오븐사용가능", "오븐사용불가"),
    )
    row = suggestions[0]
    assert row["status"] == "matched"
    assert row["selected"][0]["minAttributeValue"] == "오븐사용가능"
    assert row["selected"][0]["evidence"].startswith("source_text[")
    # "오븐사용불가" 후보는 긍정 일치가 아니므로 자동 채용되지 않는다.
    assert [s["minAttributeValue"] for s in row["selected"]] == ["오븐사용가능"]


# =========================================================================== #
# 인수 (b) — 부정 가드 차단 + 부정형 라벨 특칙 자동.
# =========================================================================== #
def test_b_lid_excluded_blocks_inclusion_but_negative_label_auto():
    suggestions = _suggest(
        {"name": "머그", "source_text": "뚜껑 미포함. 본체만 판매합니다."},
        _attrs(1),
        _vals(1, "뚜껑포함", "뚜껑미포함"),
    )
    row = suggestions[0]
    # 특칙: "뚜껑미포함" 라벨 자체의 부정 토큰은 가드에서 제외 → 긍정 매칭.
    assert row["status"] == "matched"
    assert row["selected"][0]["minAttributeValue"] == "뚜껑미포함"
    # 가드: "뚜껑포함" 은 "뚜껑 미포함" 전문에서 차단되고 사유가 표기된다.
    blocked = _by_label(row)
    assert "뚜껑포함" in blocked, "부정 가드 차단이 조용히 사라졌다"
    assert "부정 근접" in blocked["뚜껑포함"]["reason"]
    assert blocked["뚜껑포함"]["evidence"].startswith("source_text[")


# =========================================================================== #
# 인수 (c) — "불가" 앞뒤 창 차단.
# =========================================================================== #
def test_c_microwave_unusable_blocks_usable_label():
    suggestions = _suggest(
        {"name": "머그", "source_text": "전자레인지 사용 불가 제품입니다."},
        _attrs(1),
        _vals(1, "전자레인지사용가능"),
    )
    row = suggestions[0]
    assert row["status"] == "unknown", "차단값이 자동 채용되면 안 된다"
    blocked = _by_label(row)
    assert "전자레인지사용가능" in blocked
    assert blocked["전자레인지사용가능"]["reason"] == "부정 근접(불가)"


# =========================================================================== #
# 인수 (d) — 동의어 시드(도자기-세라믹).
# =========================================================================== #
def test_d_synonym_ceramic_matches_pottery_slash_label():
    suggestions = _suggest(
        {"name": "머그", "source_text": "고급 세라믹 소재입니다."},
        _attrs(1),
        _vals(1, "도자기/세라믹"),
    )
    row = suggestions[0]
    assert row["status"] == "matched"
    assert row["selected"][0]["minAttributeValue"] == "도자기/세라믹"


def test_d_synonym_stainless_matches_sten_label():
    suggestions = _suggest(
        {"name": "컵", "source_text": "스텐 소재 텀블러."},
        _attrs(1),
        _vals(1, "스테인리스"),
    )
    assert suggestions[0]["status"] == "matched"


# =========================================================================== #
# 인수 (e) — source_text 없으면 기존 동작과 동일(회귀).
# =========================================================================== #
def test_e_no_source_text_identical_behavior():
    attributes = _attrs(1, 2)
    values = _vals(1, "도자기") + _vals(2, "오븐사용가능")
    without = _suggest({"name": "도자기 머그", "detail": ""}, attributes, values)
    # source_text 키가 없으면 결과 구조에 blocked 키도 없다(기존 형태 그대로).
    assert all("blocked" not in row for row in without)
    assert without[0]["status"] == "matched"  # name 문자 일치(기존 경로)
    assert without[1]["status"] == "unknown"  # 전문 없으면 오븐 자동 없음
    # 빈 문자열 source_text 도 없는 것과 동일.
    with_blank = _suggest(
        {"name": "도자기 머그", "detail": "", "source_text": "   "},
        attributes,
        values,
    )
    assert with_blank == without


# =========================================================================== #
# 인수 (f) — 후보 밖 주장은 아무것도 안 찍음.
# =========================================================================== #
def test_f_out_of_vocabulary_claims_mark_nothing():
    suggestions = _suggest(
        {"name": "머그", "source_text": "금속 광택이 나는 유리 제품. 식기세척기 반응 좋음."},
        _attrs(1),
        _vals(1, "도자기/세라믹", "오븐사용가능"),
    )
    row = suggestions[0]
    assert row["status"] == "unknown"
    assert row["selected"] == []
    assert not row.get("blocked"), "후보 밖 주장을 차단 표기한 것도 찍기다"


# =========================================================================== #
# prepare_listing 연동 — source_text 전달 + 차단 사유 노출(조용한 생략 금지).
# =========================================================================== #
def test_prepare_passes_source_text_and_surfaces_blocked(monkeypatch, tmp_path):
    monkeypatch.setattr("clossify.images.attach_images", lambda s: _ATTACH_MOCK)
    from clossify import common

    monkeypatch.setattr(common, "PREPARED_DIR", tmp_path / "prepared")
    captured: list[dict] = []

    def fake_attr_fn(category_id, product_text):
        captured.append(dict(product_text))
        # 실 매처를 그대로 태운다(외부 호출 모킹 — 순수 함수 재사용).
        suggestions = _suggest(
            product_text,
            _attrs(11, 12),
            _vals(11, "도자기/세라믹", "뚜껑포함", "뚜껑미포함") + _vals(12, "오븐사용가능"),
        )
        return {"ok": True, "error": None, "suggestions": suggestions}

    payload = register.prepare_listing(
        {
            "name": "세라믹 머그",
            "salePrice": 15000,
            "image_sources": ["a.png"],
            "category_id": "50002366",
            "source_text": "도자기 머그. 뚜껑 미포함. 오븐 사용 가능.",
        },
        attributes_fn=fake_attr_fn,
    )
    # (1) source_text 가 매처 입력 그대로 흘렀다.
    assert captured[0]["source_text"] == "도자기 머그. 뚜껑 미포함. 오븐 사용 가능."
    # (2) 긍정 일치만 자동 채용 대상(attributes_suggestion) 에 들어간다.
    assert payload["attributes_suggestion"], "긍정 일치 자동 제안 없음"
    # (3) 차단 사유가 별도 키로 전부 드러난다(조용한 생략 금지).
    blocked = payload["attributes_suggestion_blocked"]
    blocked_labels = {item["minAttributeValue"] for item in blocked}
    assert "뚜껑포함" in blocked_labels, f"차단 누락: {blocked}"
    assert all("부정 근접" in item["reason"] for item in blocked)
    # (4) 차단값은 자동 채용 목록에 절대 없다.
    adopted_seqs = {
        (item["attributeSeq"], item["attributeValueSeq"])
        for item in payload["attributes_suggestion"]
    }
    for item in blocked:
        assert (item["attributeSeq"], item["attributeValueSeq"]) not in adopted_seqs
    # (5) needs_user 에도 차단 사유가 표기된다.
    attrs_hints = [
        n for n in payload["needs_user"] if isinstance(n, dict) and n.get("field") == "attributes"
    ]
    assert attrs_hints and any("차단" in h["why"] for h in attrs_hints)


def test_prepare_without_source_text_calls_fn_without_key(monkeypatch, tmp_path):
    monkeypatch.setattr("clossify.images.attach_images", lambda s: _ATTACH_MOCK)
    from clossify import common

    monkeypatch.setattr(common, "PREPARED_DIR", tmp_path / "prepared")
    captured: list[dict] = []

    def fake_attr_fn(category_id, product_text):
        captured.append(dict(product_text))
        return {"ok": True, "error": None, "suggestions": []}

    register.prepare_listing(
        {
            "name": "머그",
            "salePrice": 15000,
            "image_sources": ["a.png"],
            "category_id": "50002366",
        },
        attributes_fn=fake_attr_fn,
    )
    assert "source_text" not in captured[0], "source_text 없는데 키가 실렸다"


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
