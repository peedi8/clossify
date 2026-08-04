# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""전 카테고리 공통 고시 5필드를 설정에서 채우기.

검증 항목(a~f):

  (a) config 에 5개를 넣고 상품 입력에 없으면 페이로드 고시에 그 값이 들어간다.
  (b) ``notice_filled_from_config`` 가 실제로 채운 필드만 정확히 보고한다.
  (c) 명시값 우선 — 상품 입력에 값이 있으면 설정이 이기지 못한다(보고 목록에도 없다).
  (d) 설정값이 "" / 공백뿐이면 미설정 취급 — 채워지지 않고 기존처럼 누락으로 남는다.
  (e) 설정에 아무것도 없으면 기존 동작 그대로(누락 → 사용자 요청). 회귀 없음.
  (f) ``config.example.json`` 에 이 5개 키의 실값이 들어있지 않다(빈 문자열만).

이 값들은 규제 신고값이므로 코드가 임의로 만들지 않으며, 설정에서 채워진
필드는 페이로드 빌드 결과 메타(``notice_filled_from_config``)에 명시되어야 한다
— 묻지 않고 딸려가는 값이 조용히 있으면 잘못 신고된다.
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

from clossify import naver_client

# --------------------------------------------------------------------------- #
# 테스트용 공통 픽스처.
# --------------------------------------------------------------------------- #

# 공통 5필드의 고시 camelCase 이름 — 코드의 _NOTICE_COMMON_FIELDS 와 동일.
_COMMON_5 = (
    "returnCostReason",
    "noRefundReason",
    "qualityAssuranceStandard",
    "compensationProcedure",
    "troubleShootingContents",
)

# camelCase 키 → snake_case config 키. 본 테스트는 양쪽 키를 모두 검증한다.
_CAMEL_TO_SNAKE = {
    "returnCostReason": "return_cost_reason",
    "noRefundReason": "no_refund_reason",
    "qualityAssuranceStandard": "quality_assurance_standard",
    "compensationProcedure": "compensation_procedure",
    "troubleShootingContents": "trouble_shooting_contents",
}

# config 예시 문구(규제값 아님 — 테스트용 임의 식별 가능 문자열).
_CAMEL_CFG = {
    "origin_area_code": "04",
    "origin_content": "중국",
    "returnCostReason": "[CFG] returnCostReason 값",
    "noRefundReason": "[CFG] noRefundReason 값",
    "qualityAssuranceStandard": "[CFG] qualityAssuranceStandard 값",
    "compensationProcedure": "[CFG] compensationProcedure 값",
    "troubleShootingContents": "[CFG] troubleShootingContents 값",
}

# snake_case config 키로 동일한 값을 담은 사본 — 본 테스트는 양쪽 키 호환성을
# 그대로 유지하는지도 검증한다(기존 테스트가 snake_case 키에 의존함).
_SNAKE_CFG = {
    "origin_area_code": "04",
    "origin_content": "중국",
    "return_cost_reason": "[CFG-snake] returnCostReason 값",
    "no_refund_reason": "[CFG-snake] noRefundReason 값",
    "quality_assurance_standard": "[CFG-snake] qualityAssuranceStandard 값",
    "compensation_procedure": "[CFG-snake] compensationProcedure 값",
    "trouble_shooting_contents": "[CFG-snake] troubleShootingContents 값",
}


def _make_product(extra_product=None, notice_body=None, notice_type="ETC"):
    """테스트용 상품 dict. 공통 5필드는 기본적으로 제공하지 않는다."""
    p = {
        "name": "테스트상품",
        "categoryId": "50000000",
        "salePrice": 30000,
        "origin_code": "04",
        "made_in": "중국",
    }
    if extra_product:
        p.update(extra_product)
    if notice_type:
        p["notice"] = {"productInfoProvidedNoticeType": notice_type}
        if notice_body is not None:
            # ETC 타입의 노드 키 "etc" 에 본문을 넣는다.
            p["notice"]["etc"] = dict(notice_body)
    return p


def _build_payload(p, cfg):
    """notice_config 를 cfg 로 고정하고 build_payload 실행. 네트워크 없음."""
    with mock.patch.object(naver_client, "_notice_config", return_value=cfg):
        with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
            return naver_client.build_payload(p, "<html></html>", ["http://x.png"])


def _etc_body(payload):
    """payload → productInfoProvidedNotice 의 etc 노드 본문."""
    return (
        payload.get("originProduct", {})
        .get("detailAttribute", {})
        .get("productInfoProvidedNotice", {})
        .get("etc", {})
    )


# --------------------------------------------------------------------------- #
# (a) config 에 5개를 넣고 상품 입력에 없으면 페이로드 고시에 그 값이 들어간다.
# --------------------------------------------------------------------------- #
class TestConfigFillsCommonFive:
    """(a) 상품 입력 누락 시 config 값이 고시 본문에 들어가는가."""

    def test_camel_keys_filled(self):
        """camelCase config 키로 5개를 넣으면 본문에 그 값이 들어간다."""
        p = _make_product()
        payload = _build_payload(p, _CAMEL_CFG)
        body = _etc_body(payload)
        for field in _COMMON_5:
            assert (
                body.get(field) == _CAMEL_CFG[field]
            ), f"{field} 가 config(camelCase)에서 채워지지 않음: {body.get(field)!r}"

    def test_snake_keys_filled(self):
        """snake_case config 키로 5개를 넣어도 본문에 그 값이 들어간다
        (기존 호환성 회귀 없음)."""
        p = _make_product()
        payload = _build_payload(p, _SNAKE_CFG)
        body = _etc_body(payload)
        for field in _COMMON_5:
            snake = _CAMEL_TO_SNAKE[field]
            assert (
                body.get(field) == _SNAKE_CFG[snake]
            ), f"{field} 가 config(snake_case)에서 채워지지 않음: {body.get(field)!r}"


# --------------------------------------------------------------------------- #
# (b) notice_filled_from_config 가 실제로 채운 필드만 정확히 보고한다.
# --------------------------------------------------------------------------- #
class TestFilledFromConfigReporting:
    """(b) notice_filled_from_config 목록의 정확성."""

    def test_all_five_reported_when_all_from_config(self):
        """상품 입력에 5개 모두 없고 config 에 5개 모두 있으면 → 5개 모두 보고."""
        p = _make_product()
        payload = _build_payload(p, _CAMEL_CFG)
        reported = payload.get("notice_filled_from_config")
        assert reported is not None, "notice_filled_from_config 키가 없음"
        assert sorted(reported) == sorted(_COMMON_5), f"보고된 필드가 5개 전체가 아님: {reported!r}"

    def test_subset_reported_when_only_some_config(self):
        """config 에 2개만 있으면 → 그 2개만 보고(나머지는 보고에 없음)."""
        partial_cfg = dict(_CAMEL_CFG)
        # 3개를 "" 로 비운다(미설정).
        for field in (
            "qualityAssuranceStandard",
            "compensationProcedure",
            "troubleShootingContents",
        ):
            partial_cfg[field] = ""
        p = _make_product()
        payload = _build_payload(p, partial_cfg)
        reported = payload.get("notice_filled_from_config") or []
        assert sorted(reported) == [
            "noRefundReason",
            "returnCostReason",
        ], f"부분 채움 보고가 정확하지 않음: {reported!r}"

    def test_no_meta_key_when_nothing_filled(self):
        """아무것도 config 에서 채워지지 않았으면 → 메타 키 자체가 없다."""
        empty_cfg = {
            "origin_area_code": "04",
            "origin_content": "중국",
            # 공통 5필드 모두 누락.
        }
        p = _make_product()
        payload = _build_payload(p, empty_cfg)
        assert (
            "notice_filled_from_config" not in payload
        ), f"채운 게 없는데 메타가 있음: {payload.get('notice_filled_from_config')!r}"

    def test_meta_reports_only_common_five(self):
        """보고 목록은 공통 5필드 외에는 담지 않는다."""
        p = _make_product()
        payload = _build_payload(p, _CAMEL_CFG)
        reported = payload.get("notice_filled_from_config") or []
        extra = set(reported) - set(_COMMON_5)
        assert not extra, f"공통 5필드 외의 값이 보고됨: {extra!r}"


# --------------------------------------------------------------------------- #
# (c) 명시값 우선 — 상품 입력에 값이 있으면 설정이 이기지 못한다.
# --------------------------------------------------------------------------- #
class TestProductValueWins:
    """(c) 상품 입력 > config."""

    def test_product_input_overrides_config_in_body(self):
        """상품 입력의 공통 필드값이 config 보다 우선한다."""
        # ETC 노드(etc)에 사용자 본문으로 common 필드값을 준다.
        user_body = {
            "returnCostReason": "[USER] returnCostReason",
            "noRefundReason": "[USER] noRefundReason",
            "qualityAssuranceStandard": "[USER] qualityAssuranceStandard",
            "compensationProcedure": "[USER] compensationProcedure",
            "troubleShootingContents": "[USER] troubleShootingContents",
        }
        p = _make_product(notice_body=user_body)
        payload = _build_payload(p, _CAMEL_CFG)
        body = _etc_body(payload)
        for field in _COMMON_5:
            user_val = user_body[field]
            assert body.get(field) == user_val, (
                f"{field}: 상품 입력값이 config 에게 지면 안 됨 — "
                f"got={body.get(field)!r} want={user_val!r}"
            )

    def test_product_value_excluded_from_filled_report(self):
        """상품 입력에 값이 있으면 그 필드는 notice_filled_from_config 에 없다."""
        # 5개 중 2개만 상품 입력에 제공.
        user_body = {
            "returnCostReason": "[USER] returnCostReason",
            "noRefundReason": "[USER] noRefundReason",
        }
        p = _make_product(notice_body=user_body)
        payload = _build_payload(p, _CAMEL_CFG)
        reported = payload.get("notice_filled_from_config") or []
        # 상품 입력이 준 2개는 보고에서 제외, 나머지 3개만 보고.
        assert (
            "returnCostReason" not in reported
        ), f"상품 입력이 우선인데 returnCostReason 이 보고됨: {reported!r}"
        assert (
            "noRefundReason" not in reported
        ), f"상품 입력이 우선인데 noRefundReason 이 보고됨: {reported!r}"
        assert sorted(reported) == [
            "compensationProcedure",
            "qualityAssuranceStandard",
            "troubleShootingContents",
        ], f"보고 목록이 예상과 다름: {reported!r}"

    def test_top_level_product_keys_also_win(self):
        """상품 입력의 top-level(common) 키가 config 보다 우선하는가.

        build_payload 의 병합은 p.get("return_cost_reason")/p.get("returnCostReason")
        도 후보로 보므로, top-level common 키도 명시값 우선 대상이다.
        """
        p = _make_product(
            extra_product={
                "returnCostReason": "[USER-TOP] returnCostReason",
                "quality_assurance_standard": "[USER-TOP] qualityAssuranceStandard",
            }
        )
        payload = _build_payload(p, _CAMEL_CFG)
        body = _etc_body(payload)
        assert body.get("returnCostReason") == "[USER-TOP] returnCostReason"
        assert body.get("qualityAssuranceStandard") == "[USER-TOP] qualityAssuranceStandard"
        reported = payload.get("notice_filled_from_config") or []
        assert "returnCostReason" not in reported
        assert "qualityAssuranceStandard" not in reported


# --------------------------------------------------------------------------- #
# (d) 설정값이 "" / 공백뿐이면 미설정 취급 — 채워지지 않고 누락으로 남는다.
# --------------------------------------------------------------------------- #
class TestEmptyConfigTreatedAsUnset:
    """(d) 빈 문자열·공백만 있는 config 값은 미설정으로 본다."""

    def test_empty_string_config_not_filled(self):
        """config 값이 "" 이면 → 본문에 값이 없고 보고 목록에도 없다."""
        empty_cfg = {
            "origin_area_code": "04",
            "origin_content": "중국",
            "returnCostReason": "",
            "noRefundReason": "",
            "qualityAssuranceStandard": "",
            "compensationProcedure": "",
            "troubleShootingContents": "",
        }
        p = _make_product()
        payload = _build_payload(p, empty_cfg)
        body = _etc_body(payload)
        for field in _COMMON_5:
            assert field not in body, f"{field} 가 빈 config 값으로 채워짐: {body.get(field)!r}"
        assert (
            "notice_filled_from_config" not in payload
        ), f"빈 config 만 있는데 보고됨: {payload.get('notice_filled_from_config')!r}"

    def test_whitespace_only_config_not_filled(self):
        """config 값이 공백만 있으면 → 미설정 취급, 채워지지 않는다."""
        ws_cfg = {
            "origin_area_code": "04",
            "origin_content": "중국",
            "returnCostReason": "   ",
            "noRefundReason": "\t\n",
            "qualityAssuranceStandard": " ",
            "compensationProcedure": "",
            "troubleShootingContents": "",
        }
        p = _make_product()
        payload = _build_payload(p, ws_cfg)
        body = _etc_body(payload)
        for field in _COMMON_5:
            assert (
                field not in body
            ), f"{field} 가 공백-only config 값으로 채워짐: {body.get(field)!r}"
        assert "notice_filled_from_config" not in payload

    def test_mixed_empty_and_nonempty_only_reports_nonempty(self):
        """빈 값과 비어있지 않은 값이 섞인 config 는 비어있지 않은 값만 보고한다."""
        mixed_cfg = {
            "origin_area_code": "04",
            "origin_content": "중국",
            "returnCostReason": "채워진 값",
            "noRefundReason": "",
            "qualityAssuranceStandard": "   ",
            "compensationProcedure": "또 채워진 값",
            "troubleShootingContents": "",
        }
        p = _make_product()
        payload = _build_payload(p, mixed_cfg)
        reported = payload.get("notice_filled_from_config") or []
        assert sorted(reported) == [
            "compensationProcedure",
            "returnCostReason",
        ], f"빈/공백 값이 보고에 끼어들면 안 됨: {reported!r}"


# --------------------------------------------------------------------------- #
# (e) 설정에 아무것도 없으면 기존 동작 그대로(누락 → 사용자 요청). 회귀 없음.
# --------------------------------------------------------------------------- #
class TestNoConfigNoRegression:
    """(e) config 에 공통 5필드 자체가 없으면 기존 동작을 유지한다."""

    def test_no_common_keys_in_config_is_silent(self):
        """config 에 공통 5필드 키 자체가 없으면 → 본문 누락, 보고도 없음."""
        minimal_cfg = {
            "origin_area_code": "04",
            "origin_content": "중국",
        }
        p = _make_product()
        payload = _build_payload(p, minimal_cfg)
        body = _etc_body(payload)
        for field in _COMMON_5:
            assert field not in body
        assert "notice_filled_from_config" not in payload

    def test_empty_config_dict_no_regression(self):
        """config 섹션이 빈 dict 이어도 회귀 없음."""
        p = _make_product()
        payload = _build_payload(p, {})
        body = _etc_body(payload)
        for field in _COMMON_5:
            assert field not in body
        assert "notice_filled_from_config" not in payload


# --------------------------------------------------------------------------- #
# (f) config.example.json 에 이 5개 키의 실값이 들어있지 않다(빈 문자열만).
#     코드가 규제 문구를 배포하지 않는다는 보장.
# --------------------------------------------------------------------------- #
class TestExampleConfigNoRealValues:
    """(f) config.example.json 의 공통 5필드는 빈 문자열만 가져야 한다."""

    def test_keys_exist_as_empty_strings(self):
        """5개 키가 존재하며 모두 빈 문자열이어야 한다(규제 문구 배포 금지)."""
        example_path = _PROJECT_ROOT / "config.example.json"
        with example_path.open(encoding="utf-8") as f:
            cfg = json.load(f)
        section = cfg.get("smartstore_notice_defaults", {})
        for field in _COMMON_5:
            assert field in section, (
                f"config.example.json 에 {field} 키가 없음 — " f"사용자가 발견할 수 없는 설정 자리."
            )
            value = section[field]
            assert value == "", (
                f"config.example.json 의 {field} 가 빈 문자열이 아님: {value!r} — "
                f"코드가 규제 신고 문구를 배포하는 금지 사항."
            )

    def test_no_snake_alias_present_in_example(self):
        """config.example.json 에 snake_case 별칭 자리를 만들지 않는다.

        티켓이 명시: '키 이름은 고시 필드명 그대로 쓴다(별칭 만들지 말 것)'.
        예시 파일에 별칭이 있으면 사용자가 어느 쪽을 써야 할지 갈라진다.
        """
        example_path = _PROJECT_ROOT / "config.example.json"
        with example_path.open(encoding="utf-8") as f:
            cfg = json.load(f)
        section = cfg.get("smartstore_notice_defaults", {})
        for snake in (
            "return_cost_reason",
            "no_refund_reason",
            "quality_assurance_standard",
            "compensation_procedure",
            "trouble_shooting_contents",
        ):
            assert snake not in section, (
                f"config.example.json 에 snake_case 별칭 {snake!r} 가 있음 — " f"별칭 금지."
            )
