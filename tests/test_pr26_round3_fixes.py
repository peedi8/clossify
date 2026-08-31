# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""PR #26 3라운드 수리 4건을 시험으로 고정한다 (회귀 방지 전용).

소스 코드 수정 없이 시험만 추가한다 — 고정되지 않은 수리는 다음 수리가
조용히 되돌릴 수 있으므로 시험으로 못 박는다.

① 빈 배송비가 준비 단계를 터뜨리지 않는다 (build_payload 경계).
② 대화형(실제 승인) 미리보기 경로에 설정 유래 보고 필드가 뜬다.
③ camelCase 별칭(deliveryFee)을 설정 점검이 안다.
④ 승인 화면 편집이 올바른 자리(top-level / notice)로 간다.
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

from clossify import mcp_server, naver_client, preview, register

# =========================================================================== #
# 공통 픽스처.
# =========================================================================== #


def _base_fee(payload: dict) -> int | None:
    """payload → originProduct.deliveryInfo.deliveryFee.baseFee."""
    return (
        payload.get("originProduct", {})
        .get("deliveryInfo", {})
        .get("deliveryFee", {})
        .get("baseFee")
    )


def _build_payload(p: dict, cfg: dict) -> dict:
    """notice_config 를 cfg 로 고정하고 build_payload 실행. 네트워크 없음."""
    p = dict(p)
    if not str(p.get("as_tel") or "").strip():
        p["as_tel"] = "02-0000-0000"
    with (
        mock.patch.object(naver_client, "_notice_config", return_value=cfg),
        mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
    ):
        return naver_client.build_payload(p, "<html></html>", ["http://x.png"])


# =========================================================================== #
# ① 빈 배송비가 준비 단계를 터뜨리지 않는다.
#
# build_payload 경계에서 재라 (실측 그대로):
#   | 입력              | 기대 baseFee |
#   |-------------------|--------------|
#   | delivery_fee=None  | 설정값(7700)  |
#   | delivery_fee=""    | 설정값(7700)  |
#   | delivery_fee="   " | 설정값(7700)  |
#   | delivery_fee=5000  | 5000         |
#   | delivery_fee=0     | 0            |
#
# 과거엔 int(None) 이 터졌다. 예외가 나지 않는 것도 단언한다.
# register.py 의 두 자리 (정규화 result · payload product) 경유 경로를 덮는다.
# =========================================================================== #


class TestEmptyDeliveryFeeDoesNotExplode:
    """빈/공백 배송비가 예외를 일으키지 않고 설정 폴백으로 떨어지는지 확인.

    과거 결함: ``int(None)`` 이 터져서 준비 단계가 중단되었다.
    현재: 빈 값은 "생략" 으로 해석되어 config 폴백이 발동한다.
    """

    @pytest.mark.parametrize(
        "empty_value",
        [None, "", "   "],
        ids=["None", "empty_string", "whitespace_only"],
    )
    def test_empty_fee_falls_back_to_config(self, empty_value):
        """delivery_fee 가 None/빈문자열/공백 → 예외 없이 설정값(7700)."""
        p = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
            "made_in": "중국",
            "delivery_fee": empty_value,
        }
        cfg = {
            "origin_area_code": "04",
            "origin_content": "중국",
            "delivery_fee": 7700,
        }
        # 예외가 나지 않는 것 자체가 핵심 단언 (과거엔 int(None) 터짐).
        payload = _build_payload(p, cfg)
        assert (
            _base_fee(payload) == 7700
        ), f"빈 delivery_fee({empty_value!r}) 가 config 7700 으로 폴백해야 함"

    def test_explicit_int_fee_respected(self):
        """delivery_fee=5000 (정수 명시) → 5000."""
        p = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
            "made_in": "중국",
            "delivery_fee": 5000,
        }
        cfg = {"origin_area_code": "04", "origin_content": "중국", "delivery_fee": 7700}
        payload = _build_payload(p, cfg)
        assert _base_fee(payload) == 5000

    def test_explicit_zero_fee_respected(self):
        """delivery_fee=0 (무료배송 명시) → 0. config 가 덮어쓰지 않는다."""
        p = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
            "made_in": "중국",
            "delivery_fee": 0,
        }
        cfg = {"origin_area_code": "04", "origin_content": "중국", "delivery_fee": 7700}
        payload = _build_payload(p, cfg)
        assert _base_fee(payload) == 0


class TestEmptyDeliveryFeeInRegisterNormalize:
    """register.py 의 정규화 두 자리를 경유할 때 빈 배송비가 예외를 일으키지
    않고 config 폴백 경로로 들어가는지 확인.

    두 자리:
      1. ``_build_product_dict`` — 준비 단계 상품 dict 정규화.
      2. ``_build_register_product_dict`` — 등록 단계가 쓸 상품 dict 조립.

    이 두 함수는 delivery_fee 키가 있을 때 값을 그대로 전달한다.
    빈 값이 그대로 전달되면 _resolve_delivery_fee_with_slot 이 "생략" 으로
    처리하여 config 폴백이 발동한다 — 이 흐름이 끝까지 살아 있어야 한다.
    """

    def test_build_product_dict_passes_empty_fee_through(self):
        """_build_product_dict 에 빈 delivery_fee 를 주어도 예외 없이 dict 가 나온다.

        값이 None/빈문자열이어도 result["delivery_fee"] 에 그대로 들어가는 것이
        현재 규약(해석기가 None/"" 을 "생략" 으로 본다) — 이 자리가 터지면
        준비 단계가 중단된다.
        """
        d = {
            "name": "테스트상품",
            "salePrice": 30000,
            "categoryId": "50000000",
            "delivery_fee": None,
        }
        # 예외 없이 dict 가 반환되어야 함 (과거엔 int(None) 터짐).
        result = register._build_product_dict(d, None, None)
        assert isinstance(result, dict)
        # delivery_fee 키가 있고 값이 그대로 전달됨 (해석기가 생략 처리).
        assert "delivery_fee" in result
        # 실제 build_payload 까지 통과하는지 확인.
        cfg = {
            "origin_area_code": "04",
            "origin_content": "중국",
            "delivery_fee": 7700,
        }
        payload = _build_payload(result, cfg)
        assert _base_fee(payload) == 7700

    def test_build_register_product_dict_passes_empty_fee_through(self):
        """_build_register_product_dict 에 빈 delivery_fee 를 주어도 예외 없이 나온다.

        이 함수는 빈 값(raw_fee 가 None 이거나 빈 문자열)이면 키를 아예 넣지
        않는다 — config 폴백이 발동하는 자리다. int() 변환으로 터지면 안 됨.
        """
        d = {
            "name": "테스트상품",
            "salePrice": 30000,
            "categoryId": "50000000",
            "delivery_fee": "",
        }
        # 예외 없이 dict 가 반환되어야 함.
        result = register._build_register_product_dict(d, "테스트상품", "50000000")
        assert isinstance(result, dict)
        # 빈 값이므로 키가 없어야 함 → config 폴백 경로.
        assert "delivery_fee" not in result or not str(result.get("delivery_fee")).strip()
        # build_payload 까지 통과.
        cfg = {
            "origin_area_code": "04",
            "origin_content": "중국",
            "delivery_fee": 7700,
        }
        payload = _build_payload(result, cfg)
        assert _base_fee(payload) == 7700

    def test_build_register_product_dict_with_whitespace_fee(self):
        """_build_register_product_dict 에 공백 delivery_fee 도 예외 없이 통과."""
        d = {
            "name": "테스트상품",
            "salePrice": 30000,
            "categoryId": "50000000",
            "delivery_fee": "   ",
        }
        result = register._build_register_product_dict(d, "테스트상품", "50000000")
        assert isinstance(result, dict)
        # 공백은 빈 값으로 취급 — 키가 없어야 함.
        assert "delivery_fee" not in result or not str(result.get("delivery_fee")).strip()


# =========================================================================== #
# ② 대화형(실제 승인) 미리보기에 설정 유래가 뜬다.
#
# _build_preview_api_payload → _collect_notice_rows 경로로, 설정에 네 값이 다
# 있을 때:
#   origin_content | 설정원산지  | 설정 기본값
#   importer       | 설정수입사  | 설정 기본값
#   manufacturer   | 설정제조사  | 설정 기본값
#   delivery_fee   | 7700       | 설정 기본값
#
# 반례도 필수: api_payload=None 이면 네 행이 "미제공" 으로 떨어진다.
# 설정은 반드시 몽키패치한다 (naver_client._notice_config).
# =========================================================================== #


class TestInteractivePreviewShowsConfigProvenance:
    """대화형 승인 경로의 미리보기에 설정 유래 보고 필드가 뜨는지 확인.

    보기 전용 경로로 때우지 마라 — _build_preview_api_payload 가
    notice_filled_from_config 목록을 만들고, _collect_notice_rows 가 그 목록을
    받아 "설정 기본값" 출처를 표시한다.
    """

    def test_all_four_n7_fields_show_config_source(self):
        """설정에 네 값이 다 있을 때 미리보기에 '설정 기본값' 출처로 뜬다."""
        cfg_notice = {
            "origin_area_code": "04",
            "origin_content": "설정원산지",
            "importer": "설정수입사",
            "manufacturer": "설정제조사",
            "delivery_fee": 7700,
            "as_tel": "02-0000-0000",
        }
        # prepared payload 의 product dict — 네 설정 유래 보고 필드 모두 없음 (config 유래).
        resolved_payload = {
            "product": {
                "name": "테스트상품",
                "categoryId": "50000000",
                "salePrice": 30000,
                "origin_code": "04",
            }
        }
        with mock.patch.object(naver_client, "_notice_config", return_value=cfg_notice):
            api_payload = mcp_server._build_preview_api_payload(resolved_payload)
        # api_payload 가 만들어져야 함 (None 이면 보기 전용 폴백).
        assert api_payload is not None, "_build_preview_api_payload 가 None 을 반환"
        notice_filled = api_payload.get("notice_filled_from_config") or []
        for field in ("origin_content", "importer", "manufacturer", "delivery_fee"):
            assert field in notice_filled, f"{field} 가 notice_filled 에 없음: {notice_filled!r}"

        # _collect_notice_rows 가 "설정 기본값" 출처를 표시하는지 확인.
        with mock.patch.object(naver_client, "_notice_config", return_value=cfg_notice):
            rows = preview._collect_notice_rows(resolved_payload["product"], notice_filled)
        row_map = {r["field"]: r for r in rows}
        expected = {
            "origin_content": "설정원산지",
            "importer": "설정수입사",
            "manufacturer": "설정제조사",
            "delivery_fee": "7700",
        }
        for field, expected_value in expected.items():
            assert field in row_map, f"{field} 행이 없음: {list(row_map.keys())!r}"
            assert (
                row_map[field]["source"] == "설정 기본값"
            ), f"{field} 출처가 '설정 기본값' 이 아님: {row_map[field]['source']!r}"
            assert (
                row_map[field]["value"] == expected_value
            ), f"{field} 값이 {expected_value!r} 이 아님: {row_map[field]['value']!r}"

    def test_api_payload_none_drops_to_missing(self):
        """반례: api_payload=None 이면 네 행이 '미제공' 으로 떨어진다.

        이것이 이번에 고친 결함의 모습이다 — 시험이 그 차이를 잡아야 한다.
        """
        cfg_notice = {
            "origin_area_code": "04",
            "origin_content": "설정원산지",
            "importer": "설정수입사",
            "manufacturer": "설정제조사",
            "delivery_fee": 7700,
        }
        product = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
        }
        # api_payload 가 None 이면 notice_filled 가 빈 리스트로 떨어진다.
        with mock.patch.object(naver_client, "_notice_config", return_value=cfg_notice):
            rows = preview._collect_notice_rows(product, [])
        row_map = {r["field"]: r for r in rows}
        for field in ("origin_content", "importer", "manufacturer", "delivery_fee"):
            assert field in row_map, f"{field} 행이 없음"
            assert row_map[field]["source"] == "미제공", (
                f"{field} 가 api_payload=None 인데 '미제공' 이 아님: "
                f"{row_map[field]['source']!r}"
            )

    def test_config_monkeypatch_isolates_from_local_config(self):
        """몽키패치하지 않으면 개발자 로컬 설정을 읽어 기계마다 결과가 달라진다.

        본 시험은 몽키패치로 cfg_notice 를 고정하므로 로컬 설정과 무관하게
        항상 같은 결과를 낸다 — 설정 의존 시험 금지 원칙.
        """
        cfg_notice = {
            "origin_area_code": "04",
            "origin_content": "고정원산지",
            "delivery_fee": 7700,
        }
        product = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
        }
        with mock.patch.object(naver_client, "_notice_config", return_value=cfg_notice):
            rows = preview._collect_notice_rows(product, ["origin_content", "delivery_fee"])
        row_map = {r["field"]: r for r in rows}
        assert row_map["origin_content"]["value"] == "고정원산지"
        assert row_map["origin_content"]["source"] == "설정 기본값"
        assert row_map["delivery_fee"]["value"] == "7700"
        assert row_map["delivery_fee"]["source"] == "설정 기본값"


# =========================================================================== #
# ③ camelCase 별칭을 설정 점검이 안다.
#
# _POLICY_CONFIG_ALIASES 경유로:
#   - smartstore_notice_defaults.deliveryFee = 4000 → 정책 공백에 delivery_fee 없음
#   - 둘 다 없음 → 정책 공백에 delivery_fee 있음
#   - delivery_fee = 4000 (snake) → 공백 없음
#   - read_existing=True 에서 camelCase 설정이 불일치 점검 대상이 된다
#     (설정 4000 vs 상품 3500 → 불일치 보고)
# =========================================================================== #


class TestCamelCaseAliasInPolicyCheck:
    """camelCase deliveryFee 별칭이 _POLICY_CONFIG_ALIASES 를 통해 인식되는지 확인.

    판매자가 deliveryFee(camelCase) 로 설정하면 등록은 그 값을 쓰면서
    check_config 가 "미설정" 이라 진단하는 모순을 막는다.
    """

    def test_camelcase_fee_not_in_policy_gaps(self):
        """deliveryFee=4000 (camelCase) → 정책 공백에 delivery_fee 가 없어야 함."""
        from clossify import mcp_server as ms

        cfg = {
            "smartstore_notice_defaults": {
                "origin_area_code": "04",
                "origin_content": "중국",
                "as_tel": "070-0000-0000",
                "as_guide": "안내문",
                "manufacturer": "제조사",
                "importer": "수입사",
                "returnCostReason": "반품비",
                "noRefundReason": "환불불가",
                "qualityAssuranceStandard": "품질기준",
                "compensationProcedure": "보상절차",
                "troubleShootingContents": "고장대처",
                "deliveryFee": 4000,
            }
        }
        gaps = ms._diagnose_policy_gaps(cfg)
        assert (
            "smartstore_notice_defaults.delivery_fee" not in gaps
        ), f"camelCase deliveryFee 가 있는데 정책 공백에 등장: {gaps!r}"

    def test_both_absent_shows_in_policy_gaps(self):
        """둘 다 없음 → 정책 공백에 delivery_fee 가 있어야 함."""
        from clossify import mcp_server as ms

        cfg = {
            "smartstore_notice_defaults": {
                "origin_area_code": "04",
                "origin_content": "중국",
                "as_tel": "070-0000-0000",
                "as_guide": "안내문",
                "manufacturer": "제조사",
                "importer": "수입사",
                "returnCostReason": "반품비",
                "noRefundReason": "환불불가",
                "qualityAssuranceStandard": "품질기준",
                "compensationProcedure": "보상절차",
                "troubleShootingContents": "고장대처",
                # delivery_fee / deliveryFee 모두 없음.
            }
        }
        gaps = ms._diagnose_policy_gaps(cfg)
        assert (
            "smartstore_notice_defaults.delivery_fee" in gaps
        ), f"둘 다 없는데 정책 공백에 delivery_fee 가 없음: {gaps!r}"

    def test_snake_case_fee_not_in_policy_gaps(self):
        """delivery_fee=4000 (snake) → 공백에 없어야 함."""
        from clossify import mcp_server as ms

        cfg = {
            "smartstore_notice_defaults": {
                "origin_area_code": "04",
                "origin_content": "중국",
                "as_tel": "070-0000-0000",
                "as_guide": "안내문",
                "manufacturer": "제조사",
                "importer": "수입사",
                "returnCostReason": "반품비",
                "noRefundReason": "환불불가",
                "qualityAssuranceStandard": "품질기준",
                "compensationProcedure": "보상절차",
                "troubleShootingContents": "고장대처",
                "delivery_fee": 4000,
            }
        }
        gaps = ms._diagnose_policy_gaps(cfg)
        assert (
            "smartstore_notice_defaults.delivery_fee" not in gaps
        ), f"snake_case delivery_fee 가 있는데 정책 공백에 등장: {gaps!r}"


class TestCamelCaseAliasInReadExistingDrift:
    """read_existing=True 에서 camelCase 설정이 불일치 점검 대상이 되는지 확인.

    설정 deliveryFee=4000 vs 기존 상품 baseFee=3500 → 불일치(drift) 보고.
    camelCase 를 별칭으로 인식하지 못하면 "미설정" 으로 봐서 제안만 하고
    불일치를 놓친다.
    """

    def test_camelcase_config_drift_reported(self, tmp_path, monkeypatch):
        """설정 deliveryFee=4000, 기존 상품 baseFee=3500 → drift 보고."""
        import json

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps(
                {
                    "naver": {
                        "client_id": "id",
                        "client_secret": "secret",
                        "store_url_slug": "slug",
                    },
                    "smartstore_notice_defaults": {
                        "origin_area_code": "04",
                        "origin_content": "중국",
                        "as_tel": "070-0000-0000",
                        "deliveryFee": 4000,
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_file))

        product_body = {
            "originProduct": {
                "originProductNo": "existing-camel",
                "name": "테스트",
                "deliveryInfo": {
                    "deliveryFee": {"baseFee": 3500},
                },
                "detailAttribute": {
                    "afterServiceInfo": {"afterServiceTelephoneNumber": "070-0000-0000"},
                    "originAreaInfo": {"originAreaCode": "04", "content": "중국"},
                },
            },
        }

        def _mock_search(*a, **kw):
            return 200, {"products": [{"originProductNo": "existing-camel", "name": "테스트"}]}

        def _mock_get(*a, **kw):
            return 200, product_body

        with (
            mock.patch.object(naver_client, "search_products", side_effect=_mock_search),
            mock.patch.object(naver_client, "get_product", side_effect=_mock_get),
        ):
            result = mcp_server.check_config(read_existing=True)

        drift_keys = [d["config_key"] for d in result["drift_from_existing"]]
        assert (
            "smartstore_notice_defaults.delivery_fee" in drift_keys
        ), f"camelCase 설정의 불일치가 보고되지 않음: {drift_keys!r}"


# =========================================================================== #
# ④ 승인 화면 편집이 올바른 자리로 간다.
#
# _apply_approval_edits 로:
#   - 고시.delivery_fee → 결과의 top-level delivery_fee (notice 아님), 정수
#   - 고시.origin_content · 고시.importer · 고시.manufacturer → 각각 top-level
#   - 고시.returnCostReason → notice 딕셔너리에 남는다 (회귀 방지)
#   - 끝까지 재라: 편집한 배송비가 최종 payload baseFee 에 반영되는 것까지
# =========================================================================== #


class TestApprovalEditsGoToTopLevel:
    """승인 화면 편집이 top-level 상품 키로 가는지 확인.

    감리 ④: delivery_fee·origin_content·importer·manufacturer 는 미리보기
    고시 표에 "고시.<field>" 로 등장하지만, 해석기가 top-level 상품 키에서
    읽는다 (고시 본문이 아님). notice 딕셔너리에 넣으면 해석기가 안 본다.
    """

    def test_delivery_fee_edit_goes_to_toplevel_int(self):
        """고시.delivery_fee → top-level delivery_fee, 정수."""
        edits = {"고시.delivery_fee": "6000"}
        result = mcp_server._apply_approval_edits(edits)
        assert result["delivery_fee"] == 6000
        assert isinstance(result["delivery_fee"], int), "정수여야 함"
        # notice 딕셔너리에는 남지 않아야 함.
        assert result["notice"] is None or "delivery_fee" not in (result["notice"] or {})

    def test_origin_content_edit_goes_to_toplevel(self):
        """고시.origin_content → top-level origin_content."""
        edits = {"고시.origin_content": "베트남"}
        result = mcp_server._apply_approval_edits(edits)
        assert result["origin_content"] == "베트남"
        assert result["notice"] is None or "origin_content" not in (result["notice"] or {})

    def test_importer_edit_goes_to_toplevel(self):
        """고시.importer → top-level importer."""
        edits = {"고시.importer": "(주)새수입사"}
        result = mcp_server._apply_approval_edits(edits)
        assert result["importer"] == "(주)새수입사"
        assert result["notice"] is None or "importer" not in (result["notice"] or {})

    def test_manufacturer_edit_goes_to_toplevel(self):
        """고시.manufacturer → top-level manufacturer."""
        edits = {"고시.manufacturer": "(주)새제조사"}
        result = mcp_server._apply_approval_edits(edits)
        assert result["manufacturer"] == "(주)새제조사"
        assert result["notice"] is None or "manufacturer" not in (result["notice"] or {})

    def test_return_cost_reason_stays_in_notice(self):
        """고시.returnCostReason → notice 딕셔너리에 남는다 (회귀 방지).

        이 필드는 해석기가 notice 본문에서 읽으므로 notice 에 들어가야 함.
        """
        edits = {"고시.returnCostReason": "반품비 안내문"}
        result = mcp_server._apply_approval_edits(edits)
        assert result["notice"] is not None
        assert result["notice"].get("returnCostReason") == "반품비 안내문"
        # top-level 에 returnCostReason 키가 없어야 함.
        assert "returnCostReason" not in result


class TestApprovalEditDeliveryFeeReachesBaseFee:
    """끝까지 재라: 편집한 배송비가 최종 payload baseFee 에 반영되는지 확인.

    _apply_approval_edits 가 반환한 delivery_fee 를 상품 dict 에 넣고
    build_payload 를 돌렸을 때 baseFee 가 그 값이 나와야 함.
    """

    def test_edited_fee_reflected_in_final_basefee(self):
        """고시.delivery_fee 편집 → top-level delivery_fee → baseFee 반영."""
        edits = {"고시.delivery_fee": "6000"}
        translated = mcp_server._apply_approval_edits(edits)
        # 번역된 delivery_fee 를 상품 dict 에 넣어 build_payload 실행.
        p = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
            "made_in": "중국",
            "delivery_fee": translated["delivery_fee"],
        }
        cfg = {
            "origin_area_code": "04",
            "origin_content": "중국",
            "delivery_fee": 7700,
        }
        payload = _build_payload(p, cfg)
        assert (
            _base_fee(payload) == 6000
        ), "편집한 배송비(6000) 가 최종 payload baseFee 에 반영되어야 함"

    def test_edited_fee_with_comma_parsed_as_int(self):
        """쉼표가 포함된 배송비 편집("6,000원") → 정수 6000 으로 파싱."""
        edits = {"고시.delivery_fee": "6,000원"}
        translated = mcp_server._apply_approval_edits(edits)
        assert translated["delivery_fee"] == 6000
