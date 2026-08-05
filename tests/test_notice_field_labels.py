# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""고시 필드 라벨 데이터 파일(load) 및 조회 헬퍼 검증.

검증 시나리오:
  (a) **공통 5필드 라벨 존재**: 35개 고시 타입 전체에 등장하는 5개 필드가
      모두 라벨을 갖는다. 이들은 카테고리와 무관하게 모든 사용자가 첫 화면에서
      보는 필드이므로 하드 요구사항이다.
  (b) **고아 라벨 없음**: ``notice_field_labels.json`` 의 모든 키가
      ``notice_types.json`` 에 실제로 등장하는 필드다. 존재하지 않는 필드의
      라벨은 오타 또는 추측이다.
  (c) **라벨 보유 필드는 한국어 반환**: ``_notice_field_label`` 이 라벨이 있는
      필드에 대해 필드명(영어 camelCase)이 아닌 한국어 라벨을 반환한다.
  (d) **라벨 없는 필드는 필드명 폴백**: 회귀 방지. 라벨 없는 필드는 기존과
      동일하게 필드명 그대로, 힌트는 기본 문구로 떨어진다.
  (e) **라벨 총 개수 하한**: 기존 34 + 공통 5 = 39 이상. 이후 출처 기반
      수집으로 늘어날 수 있으므로 하한만 단언한다.
  (f) **로딩 캐싱**: 두 번째 호출부터는 파일을 다시 읽지 않는다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import mcp_server, naver_client

# --------------------------------------------------------------------------- #
# 공통 픽스처.
# --------------------------------------------------------------------------- #

_LABELS_PATH = _PROJECT_ROOT / "src" / "clossify" / "data" / "notice_field_labels.json"

# 35개 고시 타입 전체에 공통으로 등장하는 5개 필드 (하드 요구사항).
_COMMON_FIVE = (
    "returnCostReason",
    "noRefundReason",
    "qualityAssuranceStandard",
    "compensationProcedure",
    "troubleShootingContents",
)


def _load_labels_doc() -> dict:
    """data/notice_field_labels.json 을 매 호출마다 새로 읽어 반환한다.

    테스트 코드에서는 캐싱된 모듈 로더를 믿지 않고 디스크 원본을 직접 읽어
    데이터 무결성을 검증한다(단일 진실 공급원).
    """
    with open(_LABELS_PATH, encoding="utf-8") as f:
        return json.load(f)


def _all_notice_fields() -> set[str]:
    """notice_types.json 의 verified 35종에서 등장하는 모든 필드명 집합.

    고아 라벨 검증(b) 에 사용 — 라벨 파일의 키가 이 집합에 없으면 오타 또는
    추측이다.
    """
    specs = naver_client._load_notice_type_specs()
    fields: set[str] = set()
    for spec in specs:
        for name in spec.get("fields") or []:
            if isinstance(name, str) and name:
                fields.add(name)
    return fields


# --------------------------------------------------------------------------- #
# (a) 공통 5필드 전부 라벨 보유.
# --------------------------------------------------------------------------- #
class TestCommonFiveFieldsLabeled:
    """공통 5필드는 35개 타입 전체에 등장하므로 반드시 라벨이 있어야 한다."""

    def test_common_five_all_have_labels(self):
        """공통 5필드가 전부 notice_field_labels.json 에 존재한다."""
        doc = _load_labels_doc()
        labels = doc.get("labels") if isinstance(doc, dict) else None
        assert isinstance(labels, dict), "labels 노드가 dict 가 아님"
        missing = [f for f in _COMMON_FIVE if f not in labels]
        assert missing == [], (
            f"공통 5필드 중 라벨 없음: {missing}. 이 필드들은 모든 카테고리에서 "
            "사용자에게 노출되므로 하드 요구사항이다."
        )

    def test_common_five_labels_have_korean_label_and_hint(self):
        """공통 5필드의 각 라벨 항목이 label/hint 키를 갖추고 있다."""
        doc = _load_labels_doc()
        labels = doc["labels"]
        for field in _COMMON_FIVE:
            entry = labels[field]
            assert isinstance(entry, dict), f"{field} 항목이 dict 가 아님"
            label = entry.get("label")
            hint = entry.get("hint")
            assert (
                isinstance(label, str) and label.strip()
            ), f"{field} 의 label 이 비어있거나 문자열이 아님: {label!r}"
            assert (
                isinstance(hint, str) and hint.strip()
            ), f"{field} 의 hint 가 비어있거나 문자열이 아님: {hint!r}"


# --------------------------------------------------------------------------- #
# (b) 고아 라벨 없음 — 모든 라벨 키가 실제 필드다.
# --------------------------------------------------------------------------- #
class TestNoOrphanLabels:
    """notice_field_labels.json 의 키가 notice_types.json 에 존재하는 필드인지."""

    def test_all_label_keys_are_real_fields(self):
        """라벨 파일의 모든 키가 notice_types.json 의 필드로 관측된다."""
        labels_doc = _load_labels_doc()
        labels = labels_doc["labels"]
        real_fields = _all_notice_fields()
        orphans = sorted(set(labels) - real_fields)
        assert orphans == [], (
            f"고아 라벨(존재하지 않는 필드에 대한 라벨): {orphans}. "
            "출처 없이 지어낸 라벨이거나 오타일 수 있다."
        )


# --------------------------------------------------------------------------- #
# (c) 라벨 보유 필드는 한국어 라벨을 반환.
# --------------------------------------------------------------------------- #
class TestLabeledFieldReturnsKorean:
    """_notice_field_label 이 라벨 보유 필드에 대해 한국어를 반환하는지."""

    def test_labeled_field_returns_korean_label(self):
        """material 필드 → ('소재', ...) 처럼 한국어 라벨 반환."""
        # 캐시를 초기화해 새 로딩을 보장.
        mcp_server._notice_labels_cache = None
        label, hint = mcp_server._notice_field_label("material")
        assert label == "소재", f"material 라벨이 '소재'가 아님: {label!r}"
        assert hint and isinstance(hint, str)

    def test_common_field_returns_korean_label(self):
        """returnCostReason → ('반품/교환 배송비', ...) 한국어 반환."""
        mcp_server._notice_labels_cache = None
        label, hint = mcp_server._notice_field_label("returnCostReason")
        assert label == "반품/교환 배송비", f"returnCostReason 라벨 불일치: {label!r}"

    def test_every_labeled_field_returns_korean_not_fieldname(self):
        """라벨 파일의 모든 키에 대해 반환된 라벨이 필드명과 다르다."""
        mcp_server._notice_labels_cache = None
        labels_doc = _load_labels_doc()
        mismatches = []
        for field in labels_doc["labels"]:
            label, _ = mcp_server._notice_field_label(field)
            # 라벨이 필드명(영어 camelCase) 그대로라면 데이터가 로딩되지 않은 것.
            if label == field:
                mismatches.append(field)
        assert mismatches == [], f"필드명이 그대로 반환됨(데이터 미로딩 의심): {mismatches}"


# --------------------------------------------------------------------------- #
# (d) 라벨 없는 필드는 필드명 폴백 (회귀 방지).
# --------------------------------------------------------------------------- #
class TestUnlabeledFieldFallback:
    """라벨 없는 필드는 기존과 동일하게 필드명 그대로 떨어진다."""

    def test_unlabeled_field_returns_fieldname(self):
        """존재하지 않는 필드명 → (필드명 그대로, 기본 힌트)."""
        mcp_server._notice_labels_cache = None
        label, hint = mcp_server._notice_field_label("nonexistentFieldName_xyz")
        assert (
            label == "nonexistentFieldName_xyz"
        ), f"라벨 없는 필드가 필드명으로 폴백하지 않음: {label!r}"
        assert hint == "이 카테고리 고시 필수 항목입니다", f"기본 힌트 불일치: {hint!r}"

    def test_unlabeled_real_field_returns_fieldname(self):
        """notice_types.json 에는 있지만 라벨이 없는 실제 필드도 폴백한다.

        회귀 방지: 라벨이 아직 확보되지 않은 89개 필드는 영어 필드명으로
        떨어지는 것이 기존 동작이다. 이 동작이 깨지면 라벨 파일 오류로
        등록 흐름이 막힐 수 있다.
        """
        mcp_server._notice_labels_cache = None
        labels_doc = _load_labels_doc()
        labeled = set(labels_doc["labels"])
        real_fields = _all_notice_fields()
        unlabeled_real = sorted(real_fields - labeled)
        if not unlabeled_real:
            return  # 모든 필드에 라벨이 있는 드문 경우 — 폴백 경로 검사 불가.
        sample = unlabeled_real[0]
        label, hint = mcp_server._notice_field_label(sample)
        assert (
            label == sample
        ), f"라벨 없는 실제 필드가 필드명으로 폴백하지 않음: {sample} -> {label!r}"


# --------------------------------------------------------------------------- #
# (e) 라벨 총 개수 하한 (기존 34 + 공통 5 = 39).
# --------------------------------------------------------------------------- #
class TestLabelCountFloor:
    """라벨은 최소 39개(기존 34 + 공통 5)이어야 한다."""

    def test_at_least_39_labels(self):
        doc = _load_labels_doc()
        labels = doc["labels"]
        assert (
            len(labels) >= 39
        ), f"라벨이 39개 미만: {len(labels)}. 기존 34 + 공통 5 = 39 하한 위반."


# --------------------------------------------------------------------------- #
# (f) 로딩 캐싱 — 두 번째 호출은 디스크를 읽지 않는다.
# --------------------------------------------------------------------------- #
class TestLoadingCached:
    """_load_notice_field_labels 가 1회 로딩 후 캐싱되는지."""

    def test_second_call_does_not_read_disk(self):
        """두 번째 호출에서 open() 이 호출되지 않는다."""
        # 캐시 초기화.
        mcp_server._notice_labels_cache = None
        # 첫 호출 — 정상 로딩.
        mcp_server._load_notice_field_labels()
        # 두 번째 호출에서 내부 open() 이 호출되지 않아야 한다.
        with mock.patch("builtins.open", mock.mock_open()) as m:
            mcp_server._load_notice_field_labels()
            m.assert_not_called()

    def test_cache_is_populated_after_load(self):
        """로딩 후 _notice_labels_cache 가 None 이 아니다."""
        mcp_server._notice_labels_cache = None
        mcp_server._load_notice_field_labels()
        assert mcp_server._notice_labels_cache is not None
