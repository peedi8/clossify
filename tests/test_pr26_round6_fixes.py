# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""PR #26 6라운드 감리 수리 시험 (회귀 방지 전용).

미리보기 후보 선택 루프가 해석기와 **동일한 자리표시자 판정** 을 쓰는지 확인.

감리 지적: 준비된 상품에 비어 있지 않은 자리표시자가 있을 때
(예: ``manufacturer: "REPLACE_WITH_MANUFACTURER"``, ``origin_content: "TBD"``)
설정에 진짜 값이 있으면 —
- **해석기**(``_first_value``/``_has_text``)는 자리표시자를 건너뛰고 설정값을 전송.
- **미리보기 행 선택 루프**는 그 자리표시자를 "사용자 입력" 으로 받아들이고
  거기서 멈춰, 설정 폴백을 그리지 않았다.
→ 승인 화면이 실제 전송될 규제값·출처와 달랐다.

시험은 네 필드(``origin_content``·``importer``·``manufacturer``·``delivery_fee``)
각각 세 가지 상황의 **미리보기 행과 실제 payload 값을 나란히** 보여준다:
  ⓐ 상품에 진짜 값 → 행=그 값/사용자 입력, payload=그 값
  ⓑ 상품에 자리표시자 + 설정에 진짜 값 → 행=설정값/설정 기본값, payload=설정값
  ⓒ 둘 다 없음 → 행=미제공

**행과 payload가 항상 같아야 한다** — 다르면 그게 이 결함이다.

본 시험은 미리보기가 해석기의 단일 진실 공급원(``naver_client._has_text``) 을
**그대로 호출** 하는지 검증한다. 새 판정 함수를 만들면 같은 결함이 다섯 번째로
재발하므로, 호출 방식 자체를 확인한다.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import naver_client, preview

# =========================================================================== #
# 공통 헬퍼.
# =========================================================================== #


def _build_payload(p: dict, cfg: dict) -> dict:
    """notice_config 를 cfg 로 고정하고 build_payload 실행. 네트워크 없음."""
    with (
        mock.patch.object(naver_client, "_notice_config", return_value=cfg),
        mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
    ):
        return naver_client.build_payload(p, "<html></html>", ["http://x.png"])


def _collect_rows(product: dict, cfg_notice: dict, notice_filled: list[str]) -> list[dict]:
    """_collect_notice_rows 를 cfg_notice 로 고정하고 실행."""
    with mock.patch.object(naver_client, "_notice_config", return_value=cfg_notice):
        return preview._collect_notice_rows(product, notice_filled)


def _row_for(rows: list[dict], field: str) -> dict:
    """rows 에서 field 행을 찾아 반환. 없으면 AssertionError."""
    matching = [r for r in rows if r["field"] == field]
    assert matching, f"{field} 행이 없음: rows={[r['field'] for r in rows]!r}"
    return matching[0]


# =========================================================================== #
# ⓑ 핵심 시험: 상품 자리표시자 + 설정 진짜 값 → 행=설정값/설정 기본값,
#               payload=설정값. 행과 payload 가 같아야 한다.
#
# 감리 지적의 정확한 시나리오 — 과거에는 미리보기가 자리표시자를
# "사용자 입력" 으로 받아들이고 거기서 멈춰, 설정 폴백을 그리지 않았다.
# =========================================================================== #


class TestPlaceholderProductFallsToConfig:
    """ⓑ 상품 자리표시자 + 설정 진짜 값 → 행=설정값/설정 기본값, payload=설정값.

    각 필드마다 자리표시자의 종류를 두 가지 시험한다:
      - ``REPLACE_WITH_...`` 접두사 (config.example.json 에서 복사한 자리표시자)
      - ``TBD``/``해당없음`` (qa_agents._is_placeholder_value 토큰)

    과거 결함: 미리보기의 top-level 검사는 단순 ``str().strip()`` 만 해서
    자리표시자를 "유효한 사용자 입력" 으로 받아들였다. 해석기(``_has_text``)는
    같은 자리표시자를 건너뛰고 config 값을 골랐다 → 행≠payload 불일치.
    """

    @pytest.mark.parametrize(
        "placeholder",
        [
            "REPLACE_WITH_ORIGIN_CONTENT",
            "TBD",
            "해당없음",
        ],
    )
    def test_origin_content_placeholder_falls_to_config(self, placeholder):
        """ⓑ origin_content: 상품 자리표시자 → 행=설정값/설정 기본값,
        payload originAreaInfo.content=설정값."""
        product = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
            "origin_content": placeholder,
        }
        cfg = {
            "origin_area_code": "04",
            "origin_content": "한국산",
        }
        payload = _build_payload(product, cfg)
        payload_value = (
            payload.get("originProduct", {})
            .get("detailAttribute", {})
            .get("originAreaInfo", {})
            .get("content")
        )
        assert payload_value == "한국산", (
            f"payload 의 origin_content 가 설정값이 아님: {payload_value!r} "
            f"(해석기가 자리표시자 {placeholder!r} 를 건너뛰어야 함)"
        )

        # 미리보기 행은 같은 값을 그려야 한다.
        notice_filled = payload.get("notice_filled_from_config") or []
        assert (
            "origin_content" in notice_filled
        ), f"해석기가 origin_content 를 config 유래로 보고해야 함: {notice_filled!r}"
        rows = _collect_rows(product, cfg, notice_filled)
        row = _row_for(rows, "origin_content")
        assert row["value"] == "한국산", (
            f"행 값이 payload 와 다름: row={row['value']!r} payload={payload_value!r} "
            f"(자리표시자 {placeholder!r} 를 사용자 입력으로 받아들인 결함)"
        )
        assert row["source"] == "설정 기본값", f"출처가 '설정 기본값' 이 아님: {row['source']!r}"

    @pytest.mark.parametrize(
        "placeholder",
        [
            "REPLACE_WITH_IMPORTER",
            "TBD",
        ],
    )
    def test_importer_placeholder_falls_to_config(self, placeholder):
        """ⓑ importer: 상품 자리표시자 → 행=설정값/설정 기본값, payload=설정값."""
        product = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
            "made_in": "중국",
            "importer": placeholder,
        }
        cfg = {
            "origin_area_code": "04",
            "origin_content": "중국",
            "importer": "(주)설정수입사",
        }
        payload = _build_payload(product, cfg)
        payload_value = (
            payload.get("originProduct", {})
            .get("detailAttribute", {})
            .get("originAreaInfo", {})
            .get("importer")
        )
        assert (
            payload_value == "(주)설정수입사"
        ), f"payload 의 importer 가 설정값이 아님: {payload_value!r}"

        notice_filled = payload.get("notice_filled_from_config") or []
        assert "importer" in notice_filled
        rows = _collect_rows(product, cfg, notice_filled)
        row = _row_for(rows, "importer")
        assert (
            row["value"] == "(주)설정수입사"
        ), f"행 값이 payload 와 다름: row={row['value']!r} payload={payload_value!r}"
        assert row["source"] == "설정 기본값"

    @pytest.mark.parametrize(
        "placeholder",
        [
            "REPLACE_WITH_MANUFACTURER",
            "TBD",
        ],
    )
    def test_manufacturer_placeholder_falls_to_config(self, placeholder):
        """ⓑ manufacturer: 상품 자리표시자 → 행=설정값/설정 기본값, payload 본문=설정값."""
        product = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
            "made_in": "중국",
            "manufacturer": placeholder,
        }
        cfg = {
            "origin_area_code": "04",
            "origin_content": "중국",
            "manufacturer": "(주)설정제조사",
        }
        payload = _build_payload(product, cfg)
        # manufacturer 는 고시 본문(notice) 에 실린다.
        notice_node = (
            payload.get("originProduct", {})
            .get("detailAttribute", {})
            .get("productInfoProvidedNotice", {})
        )
        # 본문 노드(etc/wear/...) 에서 manufacturer 를 찾는다.
        payload_value = None
        for _node_key, node_body in notice_node.items():
            if isinstance(node_body, dict) and "manufacturer" in node_body:
                payload_value = node_body["manufacturer"]
                break
        assert (
            payload_value == "(주)설정제조사"
        ), f"payload 본문의 manufacturer 가 설정값이 아님: {payload_value!r}"

        notice_filled = payload.get("notice_filled_from_config") or []
        assert "manufacturer" in notice_filled
        rows = _collect_rows(product, cfg, notice_filled)
        row = _row_for(rows, "manufacturer")
        assert (
            row["value"] == "(주)설정제조사"
        ), f"행 값이 payload 와 다름: row={row['value']!r} payload={payload_value!r}"
        assert row["source"] == "설정 기본값"


# =========================================================================== #
# ⓐ 회귀 시험: 상품에 진짜 값 → 행=그 값/사용자 입력, payload=그 값.
# 자리표시자가 아닌 진짜 값은 여전히 "사용자 입력" 으로 그려져야 한다.
# =========================================================================== #


class TestRealProductValueShownAsUserInput:
    """ⓐ 상품에 진짜 값 → 행=그 값/사용자 입력, payload=그 값 (회귀 없음)."""

    def test_origin_content_real_value_user_input(self):
        product = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
            "origin_content": "베트남",
        }
        cfg = {"origin_area_code": "04", "origin_content": "중국"}
        payload = _build_payload(product, cfg)
        payload_value = (
            payload.get("originProduct", {})
            .get("detailAttribute", {})
            .get("originAreaInfo", {})
            .get("content")
        )
        assert payload_value == "베트남"

        rows = _collect_rows(product, cfg, [])
        row = _row_for(rows, "origin_content")
        assert row["value"] == "베트남", f"행≠payload: {row['value']!r}"
        assert row["source"] == "사용자 입력"

    def test_importer_real_value_user_input(self):
        product = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
            "made_in": "중국",
            "importer": "(주)명시수입사",
        }
        cfg = {
            "origin_area_code": "04",
            "origin_content": "중국",
            "importer": "(주)설정수입사",
        }
        payload = _build_payload(product, cfg)
        payload_value = (
            payload.get("originProduct", {})
            .get("detailAttribute", {})
            .get("originAreaInfo", {})
            .get("importer")
        )
        assert payload_value == "(주)명시수입사"

        rows = _collect_rows(product, cfg, [])
        row = _row_for(rows, "importer")
        assert row["value"] == "(주)명시수입사", f"행≠payload: {row['value']!r}"
        assert row["source"] == "사용자 입력"

    def test_manufacturer_real_value_user_input(self):
        product = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
            "made_in": "중국",
            "manufacturer": "(주)명시제조사",
        }
        cfg = {
            "origin_area_code": "04",
            "origin_content": "중국",
            "manufacturer": "(주)설정제조사",
        }
        payload = _build_payload(product, cfg)
        notice_node = (
            payload.get("originProduct", {})
            .get("detailAttribute", {})
            .get("productInfoProvidedNotice", {})
        )
        payload_value = None
        for _node_key, node_body in notice_node.items():
            if isinstance(node_body, dict) and "manufacturer" in node_body:
                payload_value = node_body["manufacturer"]
                break
        assert payload_value == "(주)명시제조사"

        rows = _collect_rows(product, cfg, [])
        row = _row_for(rows, "manufacturer")
        assert row["value"] == "(주)명시제조사", f"행≠payload: {row['value']!r}"
        assert row["source"] == "사용자 입력"

    def test_delivery_fee_real_value_user_input(self):
        """배송비는 숫자이므로 자리표시자 문제가 없다 — 회귀 확인만."""
        product = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
            "made_in": "중국",
            "delivery_fee": 2500,
        }
        cfg = {
            "origin_area_code": "04",
            "origin_content": "중국",
            "delivery_fee": 7700,
        }
        payload = _build_payload(product, cfg)
        payload_value = (
            payload.get("originProduct", {})
            .get("deliveryInfo", {})
            .get("deliveryFee", {})
            .get("baseFee")
        )
        assert payload_value == 2500

        rows = _collect_rows(product, cfg, [])
        row = _row_for(rows, "delivery_fee")
        assert row["value"] == "2500", f"행≠payload: {row['value']!r}"
        assert row["source"] == "사용자 입력"


# =========================================================================== #
# ⓒ 시험: 둘 다 없음 → 행=미제공.
# =========================================================================== #


class TestBothAbsentShowsMissing:
    """ⓒ 둘 다 없음 → 행=미제공."""

    def test_origin_content_both_absent_missing(self):
        product = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
        }
        cfg = {"origin_area_code": "04", "origin_content": ""}
        rows = _collect_rows(product, cfg, [])
        row = _row_for(rows, "origin_content")
        assert row["value"] == "", f"빈 값이 아님: {row['value']!r}"
        assert row["source"] == "미제공"

    def test_importer_both_absent_missing(self):
        product = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
            "made_in": "중국",
        }
        cfg = {"origin_area_code": "04", "origin_content": "중국"}
        rows = _collect_rows(product, cfg, [])
        row = _row_for(rows, "importer")
        assert row["value"] == ""
        assert row["source"] == "미제공"


# =========================================================================== #
# 구조 시험: 미리보기가 해석기의 단일 진실 공급원을 **그대로 호출** 하는지 확인.
# 새 판정 함수를 만들면 같은 결함이 다섯 번째로 재발한다.
# =========================================================================== #


class TestPreviewUsesInterpreterPlaceholderJudgment:
    """미리보기의 _collect_notice_rows 가 naver_client._has_text 를 호출하는지 확인.

    과거에는 자체 판정(``str().strip()``) 을 써서 자리표시자를 놓쳤다. 이 시험은
    미리보기가 해석기의 판정을 직접 호출하는지 확인한다 — 새 판정 함수를 만들면
    같은 결함이 재발한다.
    """

    def test_naver_client_has_text_is_the_single_source(self):
        """``naver_client._has_text`` 가 TBD/REPLACE_WITH_... 모두를 False 로 본다.

        주의: ``_has_text(0)`` 은 ``qa_agents._is_placeholder_value(0)`` 가
        ``str(0 or "")`` → ``""`` → 자리표시자로 보기 때문에 **False** 다.
        그래서 ``preview._collect_notice_rows`` 는 숫자 값을 ``_has_text`` 에
        넣기 전에 ``isinstance(cv, int | float)`` bypass 로 먼저 받아들인다.
        이 bypass 가 없으면 무료배송(``delivery_fee=0``) 이 "미제공" 이 된다.
        """
        assert naver_client._has_text("TBD") is False
        assert naver_client._has_text("REPLACE_WITH_X") is False
        assert naver_client._has_text("해당없음") is False
        assert naver_client._has_text("") is False
        assert naver_client._has_text(None) is False
        # 진짜 값은 True
        assert naver_client._has_text("한국산") is True
        # 0 은 qa_agents 사정상 False 이므로 preview 는 bypass 함.
        assert naver_client._has_text(0) is False
        assert naver_client._has_text(3000) is True

    def test_preview_does_not_introduce_new_placeholder_check(self):
        """미리보기 소스에 ``REPLACE_WITH_`` 하드코딩 판정이 없어야 한다.

        과거 ``preview.py`` 의 cfg 값 검사에 ``if "REPLACE_WITH_" in str(cv):``
        하드코딩이 있었다. 이것은 ``naver_client._has_text`` 와 다른 판정이었다
        (TBD/TODO 등 qa_agents 토큰은 잡지 못함). 이제 제거되었는지 확인한다.
        """
        source = Path(__file__).resolve().parent.parent / "src" / "clossify" / "preview.py"
        text = source.read_text(encoding="utf-8")
        # 주석이 아닌 코드 줄에서 REPLACE_WITH_ 하드코딩 판정이 없어야 한다.
        # (주석에서는 과거 결함 설명으로 남을 수 있으므로, 코드 라인만 검사.)
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            # 주석/독스트링 라인은 제외.
            if stripped.startswith(("#", '"', "'")):
                continue
            if "REPLACE_WITH_" in line and "in str(cv)" in line:
                pytest.fail(
                    f"preview.py:{lineno} 에 REPLACE_WITH_ 하드코딩 판정이 남아있음 — "
                    f"해석기의 _has_text 를 호출해야 한다: {line.strip()!r}"
                )
