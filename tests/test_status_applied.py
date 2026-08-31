# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""등록 후 판매상태(statusType) 보정 및 노출상태(displayStatusType) 검증.

실등록에서 발견된 두 결함:

  ② ``status="SUSPENSION"`` 일 때 노출값으로 ``"OFF"`` 를 보내면 네이버 API 가
     ``NotValidEnum`` 으로 거절한다. 살아있는 API 로 등록 성공이 확인된 정답은
     ``"SUSPENSION"`` 이다.

  ③ 생성 시점의 ``originProduct.statusType`` 이 무시되고 기본값(SALE)로 저장되는
     경우가 있다. 판매중지로 올리려던 판매자가 판매중인 상품을 갖게 되는데도
     도구가 ``ok=True`` 로 보고하는 조용한 잘못된 상태다. 수정 후에는:
       - 응답 statusType 이 요청값과 다르면 ``update_product`` 로 맞추고
         ``get_product`` 로 재확인한다.
       - 그래도 다르면 ``ok=False`` 로 보고한다 (조용한 성공 금지).
       - 반환에 항상 ``requested_status`` 와 ``applied_status`` 를 싣는다.
       - ``status="SALE"`` 이고 응답도 SALE 이면 추가 호출이 일어나지 않는다.

본 테스트는 (f)~(i):

  (f) ``status="SUSPENSION"`` → 노출값이 ``"SUSPENSION"`` 으로 나간다(``"OFF"`` 아님).
  (g) 생성 응답 상태가 요청과 다르면 **보정 호출이 일어나고**, 최종 상태가 반환에 실린다.
  (h) 보정 후에도 다르면 **``ok=False``**.
  (i) ``status="SALE"`` 이고 응답도 SALE 이면 **추가 호출 0회**.

``COMMERCE_DRY_RUN`` 은 끈 상태로, 실제 네이버 HTTP 호출은 mock 으로 차단하고
호출 횟수를 센다.
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

from clossify import common, mcp_server, naver_client

# ============================================================================
# 공통 픽스처·헬퍼.
# ============================================================================

# 알 수 없는 카테고리 — 경로 조회가 빈 문자열로 떨어져 ETC 로 판정된다.
# 이 카테고리는 KC 필요 여부도 확정 불가(불명) 상태가 되므로, KC 검사를
# 통과하려면 _kc_config 가 빈 블록이 아니라 실제 KC 신고값을 반환해야 한다.
# 하지만 status 보정 테스트의 본질은 status 이므로, KC 는 mock 으로 채운다.
_GENERAL_CATEGORY = "99999999"

_NOTICE_CFG_FULL = {
    "origin_area_code": "04",
    "origin_content": "중국",
    "as_tel": "070-1234-5678",
    "manufacturer": "테스트제조사",
    "return_cost_reason": "단순변심 반품비용 구매자부담",
    "no_refund_reason": "주문제작 청약철회 제한",
    "quality_assurance_standard": "관련법에 따름",
    "compensation_procedure": "소비자분쟁해결기준",
    "trouble_shooting_contents": "고객센터 문의",
}

# ETC 고시 타입의 필수 필드(modelName, certificateDetails) 를 채운 notice 본문.
# status 보정 경로 테스트가 컴플라이언스 게이트(ETC 필수필드 누락) 에 막혀
# 도달하지 못하는 것을 막기 위함이다. 본 테스트의 대상은 status 보정이지
# 고시 필드 검증이 아니다.
_ETC_NOTICE_BODY = {
    "productInfoProvidedNoticeType": "ETC",
    # itemName(품명) 은 상품명에서 자동으로 뽑지 않는다(사용자 결정
    # 2026-08-26) — ETC 필수필드를 완비해 게이트에 막히지 않게 명시로 준다.
    "etc": {
        "itemName": "테스트품목",
        "modelName": "TEST-MODEL-1",
        "certificateDetails": "KW 인증",
    },
}


def _make_product(**overrides) -> dict:
    base = {
        "name": "상태테스트상품",
        "categoryId": _GENERAL_CATEGORY,
        "salePrice": 10000,
        "origin_code": "05",
        "made_in": "한국",
        # ETC 고시 타입의 필수 필드(modelName, certificateDetails) 를 채운다.
        # 컴플라이언스 게이트가 ETC 필수필드 누락으로 차단하면 status 보정 경로
        # 자체에 도달할 수 없다 — 본 테스트의 대상은 status 보정이지 고시 필드
        # 검증이 아니다.
        "modelName": "TEST-MODEL-1",
        "cert_detail": "KW 인증",
    }
    base.update(overrides)
    return base


def _patch_cfg():
    return (
        mock.patch.object(naver_client, "_notice_config", return_value=_NOTICE_CFG_FULL),
        mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
    )


def _build_payload(p: dict, status: str = "SALE") -> dict:
    with _patch_cfg()[0], _patch_cfg()[1]:
        return naver_client.build_payload(p, "<html></html>", ["http://x/img.png"], status=status)


def _patch_register_chain(
    monkeypatch,
    *,
    created_status: str,
    verified_status: str,
    update_calls: list | None = None,
    get_calls: list | None = None,
    register_calls: list | None = None,
    origin_no: str = "ORIGIN-X",
    channel_no: str = "CH-X",
    update_status_code: int = 200,
):
    """naver_client HTTP 계층(register/update/get)을 mock 으로 차단.

    - 생성 응답의 statusType 은 ``created_status``.
    - 보정 후 get_product 재확인 시 반환되는 statusType 은 ``verified_status``.
    - ``update_status_code``: 보정 PUT 이 반환할 HTTP 상태. 실 API 와 달리
      이 mock 은 단편 본문({"statusType": ...} 만) 을 **거부**한다 —
      채널상품 PUT 은 리소스 교체이므로 originProduct 가 없는 본문은
      400 을 반환한다(실등록에서 무시되는 것과 동일한 효과를 테스트에서
      재현). 이 가드가 없으면 단편 회귀가 200 으로 통과한다.
    """
    if update_calls is None:
        update_calls = []
    if get_calls is None:
        get_calls = []
    if register_calls is None:
        register_calls = []

    def _fake_register(payload):
        register_calls.append(payload)
        return (
            200,
            {
                "originProductNo": origin_no,
                "channelProductNo": channel_no,
                "originProduct": {"statusType": created_status},
            },
        )

    def _fake_update(cn, p):
        update_calls.append((cn, p))
        # 실 API 와 동일하게: originProduct 없는 단편 본문은 거부(400).
        # 이것이 없으면 단편 회귀가 200 으로 통과한다 (실측에서는 무시됨).
        if not isinstance(p, dict) or not isinstance(p.get("originProduct"), dict):
            return 400, {"code": "BAD_REQUEST", "message": "originProduct required"}
        return update_status_code, {}

    def _fake_get(no):
        get_calls.append(no)
        # 보정 경로가 read-mutate-send 전체 본문을 보내려면 get_product 가
        # 전체 리소스(originProduct + smartstoreChannelProduct) 를 반환해야 한다.
        return 200, {
            "originProduct": {"statusType": verified_status, "originProductNo": no},
            "smartstoreChannelProduct": {
                "channelProductDisplayStatusType": verified_status,
                "channelProductNo": channel_no,
            },
        }

    monkeypatch.setattr(naver_client, "register_product", _fake_register)
    monkeypatch.setattr(naver_client, "update_product", _fake_update)
    monkeypatch.setattr(naver_client, "get_product", _fake_get)
    monkeypatch.setattr(naver_client, "get_token", lambda: "t")
    monkeypatch.setattr(naver_client, "_notice_config", lambda: _NOTICE_CFG_FULL)
    monkeypatch.setattr(naver_client, "_kc_config", lambda: ({}, ""))
    # 컴플라이언스 게이트의 원산지 불일치 검사가 config_origin_content 와
    # payload_origin_content 를 대조한다. common.cfg() 도 같은 값을 보도록 맞춘다.
    monkeypatch.setattr(common, "cfg", lambda: {"smartstore_notice_defaults": _NOTICE_CFG_FULL})


# ============================================================================
# (f) status="SUSPENSION" → channelProductDisplayStatusType == "SUSPENSION"
#     (과거의 잘못된 값 "OFF" 가 NotValidEnum 으로 거절되던 회귀).
# ============================================================================
class TestSuspensionDisplayValue:
    def test_suspension_display_is_suspension(self):
        """status=SUSPENSION 일 때 노출 기본값은 "SUSPENSION" 이다 ("OFF" 아님)."""
        payload = _build_payload(_make_product(), status="SUSPENSION")
        block = payload["smartstoreChannelProduct"]
        assert block["channelProductDisplayStatusType"] == "SUSPENSION", (
            "status=SUSPENSION 일 때 display 기본값은 SUSPENSION 이어야 한다 "
            "(OFF 는 NotValidEnum 으로 거절됨 — 실측 확인)."
        )

    def test_sale_display_is_on(self):
        """status=SALE 일 때 노출 기본값은 ON (등록 성공 확인된 값). 회귀 방지."""
        payload = _build_payload(_make_product(), status="SALE")
        block = payload["smartstoreChannelProduct"]
        assert block["channelProductDisplayStatusType"] == "ON"


# ============================================================================
# (g) 생성 응답의 statusType 이 요청과 다르면 보정 호출이 일어나고, 최종 상태가
#     반환에 실린다. update_product 와 get_product 가 각각 한 번씩 호출된다.
# ============================================================================
class TestStatusCorrectionHappens:
    def test_mismatch_triggers_correction_call(self, monkeypatch):
        """요청=SUSPENSION, 응답=SALE → update_product 호출 후 get_product 재확인."""
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)

        update_calls: list = []
        get_calls: list = []
        register_calls: list = []
        _patch_register_chain(
            monkeypatch,
            created_status="SALE",
            verified_status="SUSPENSION",
            update_calls=update_calls,
            get_calls=get_calls,
            register_calls=register_calls,
            origin_no="ORIGIN-1",
            channel_no="CH-1",
        )

        result = mcp_server.register_product(
            name="상태보정테스트",
            price=10000,
            category_id=_GENERAL_CATEGORY,
            image_urls=["http://x/img.png"],
            detail_html="<html></html>",
            status="SUSPENSION",
            notice=_ETC_NOTICE_BODY,
            preview_confirmed=True,
        )

        # 보정(update_product) 과 재확인(get_product) 이 각각 1회.
        assert (
            len(update_calls) == 1
        ), f"보정(update_product) 이 1회 일어나야 한다: {len(update_calls)}회"
        # get_product 는 2회: 보정 전 리소스 읽기(read-mutate-send 의 read) 와
        # 보정 후 재확인. 예전 단편 PUT 시절에는 1회(재확인만) 였으나, 전체
        # 본문 전송을 위해 리소스를 먼저 읽어야 한다 (실측 기반 정답).
        assert (
            len(get_calls) == 2
        ), f"get_product 가 2회 일어나야 한다 (read + re-verify): {len(get_calls)}회"
        # 보정 페이로드는 **전체 리소스** 여야 한다 — 단편({"statusType":...} 만)
        # 은 네이버 API 가 무시한다(실등록에서 확인). 따라서 본문에 originProduct
        # 딕셔너리가 들어있고 그 안의 statusType 이 요청값이어야 한다.
        sent_channel, sent_payload = update_calls[0]
        assert sent_channel == "CH-1"
        assert isinstance(sent_payload.get("originProduct"), dict), (
            "보정 PUT 본문은 전체 originProduct 를 포함해야 한다 — 단편 본문은 "
            "네이버 API 에 의해 무시된다(실측 확인)."
        )
        assert sent_payload["originProduct"].get("statusType") == "SUSPENSION"
        # smartstoreChannelProduct 도 전체 본문에 포함되어야 한다 (display 필드).
        assert isinstance(sent_payload.get("smartstoreChannelProduct"), dict)
        assert (
            sent_payload["smartstoreChannelProduct"].get("channelProductDisplayStatusType")
            == "SUSPENSION"
        )
        # 반환에 요청값·실제값이 모두 실린다.
        assert result["requested_status"] == "SUSPENSION"
        assert result["applied_status"] == "SUSPENSION", "보정 후 최종 상태가 반환에 실려야 한다"
        # 보정으로 맞춰졌으므로 ok=True.
        assert result["ok"] is True
        # 성공 경로에서는 status_correction_error 가 None 이어야 한다.
        assert (
            result.get("status_correction_error") is None
        ), "보정이 성공하면 status_correction_error 는 None 이어야 한다."

    def test_status_corrected_flag_set(self, monkeypatch):
        """보정이 성공적으로 적용되면 status_corrected=True."""
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)

        _patch_register_chain(
            monkeypatch,
            created_status="SALE",
            verified_status="SUSPENSION",
            origin_no="ORIGIN-2",
            channel_no="CH-2",
        )

        result = mcp_server.register_product(
            name="보정플래그테스트",
            price=10000,
            category_id=_GENERAL_CATEGORY,
            image_urls=["http://x/img.png"],
            detail_html="<html></html>",
            status="SUSPENSION",
            notice=_ETC_NOTICE_BODY,
            preview_confirmed=True,
        )
        assert result.get("status_corrected") is True
        assert result.get("status_correction_attempted") is True


# ============================================================================
# (h) 보정 후에도 다르면 ok=False (조용한 성공 금지).
# ============================================================================
class TestStillMismatchReportsFailure:
    def test_persistent_mismatch_ok_false(self, monkeypatch):
        """요청=SUSPENSION, 응답=SALE, 보정 후에도 SALE → ok=False."""
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)

        _patch_register_chain(
            monkeypatch,
            created_status="SALE",
            verified_status="SALE",  # 보정 후에도 SALE 로 유지.
            origin_no="ORIGIN-3",
            channel_no="CH-3",
        )

        result = mcp_server.register_product(
            name="지속불일치테스트",
            price=10000,
            category_id=_GENERAL_CATEGORY,
            image_urls=["http://x/img.png"],
            detail_html="<html></html>",
            status="SUSPENSION",
            notice=_ETC_NOTICE_BODY,
            preview_confirmed=True,
        )

        assert (
            result["ok"] is False
        ), "보정 후에도 상태가 다르면 ok=False 여야 한다 (조용한 성공 금지)."
        assert result["requested_status"] == "SUSPENSION"
        assert result["applied_status"] == "SALE"
        assert result.get("status_correction_attempted") is True
        assert result.get("status_corrected") is False
        # 보정 시도는 했지만 여전히 불일치 — PUT 자체는 200 으로 성공했으므로
        # status_correction_error 는 None 이다 (예외가 아니라 상태가 안 바뀐 것).
        assert result.get("status_correction_error") is None, (
            "보정 PUT 이 200 을 반환했으면 status_correction_error 는 None 이다 "
            "(ok=False 는 상태가 바뀌지 않아서이지 예외가 아니다)."
        )

    def test_requested_and_applied_always_present(self, monkeypatch):
        """반환에 requested_status/applied_status 가 항상 싣는다."""
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)

        _patch_register_chain(
            monkeypatch,
            created_status="SALE",
            verified_status="SALE",
            origin_no="ORIGIN-4",
            channel_no="CH-4",
        )

        result = mcp_server.register_product(
            name="항상싣기테스트",
            price=10000,
            category_id=_GENERAL_CATEGORY,
            image_urls=["http://x/img.png"],
            detail_html="<html></html>",
            status="SUSPENSION",
            notice=_ETC_NOTICE_BODY,
            preview_confirmed=True,
        )
        assert "requested_status" in result
        assert "applied_status" in result


# ============================================================================
# (i) status="SALE" 이고 응답도 SALE 이면 추가 호출(update/get) 이 0회다.
# ============================================================================
class TestNoExtraCallWhenAlreadyMatching:
    def test_matching_sale_no_correction(self, monkeypatch):
        """요청=SALE, 응답=SALE → update_product/get_product 호출 0회."""
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)

        update_calls: list = []
        get_calls: list = []
        _patch_register_chain(
            monkeypatch,
            created_status="SALE",
            verified_status="SALE",
            update_calls=update_calls,
            get_calls=get_calls,
            origin_no="ORIGIN-5",
            channel_no="CH-5",
        )

        result = mcp_server.register_product(
            name="추가호출0회테스트",
            price=10000,
            category_id=_GENERAL_CATEGORY,
            image_urls=["http://x/img.png"],
            detail_html="<html></html>",
            status="SALE",
            notice=_ETC_NOTICE_BODY,
            preview_confirmed=True,
        )

        assert (
            len(update_calls) == 0
        ), f"요청=응답=SALE 일 때 update_product 가 0회여야 한다: {len(update_calls)}회"
        assert (
            len(get_calls) == 0
        ), f"요청=응답=SALE 일 때 get_product 가 0회여야 한다: {len(get_calls)}회"
        assert result["ok"] is True
        assert result["requested_status"] == "SALE"
        assert result["applied_status"] == "SALE"
        assert result.get("status_correction_attempted") is False
        # 보정 시도 자체가 없었으므로 status_correction_error 도 None 이다.
        assert result.get("status_correction_error") is None


# ============================================================================
# (j) 보정 PUT 이 거부(non-2xx) 되면 status_correction_error 에 사유가 실린다.
#     ok=False 와 함께 사유를 남겨야 판매자가 어찌할 바를 안다 (사유 삼킴 금지).
# ============================================================================
class TestCorrectionFailureReportsError:
    def test_correction_put_rejected_sets_error(self, monkeypatch):
        """보정 PUT 이 500 을 반환하면 status_correction_error 에 사유가 실린다."""
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)

        _patch_register_chain(
            monkeypatch,
            created_status="SALE",
            verified_status="SALE",
            origin_no="ORIGIN-6",
            channel_no="CH-6",
            update_status_code=500,
        )

        result = mcp_server.register_product(
            name="보정실패테스트",
            price=10000,
            category_id=_GENERAL_CATEGORY,
            image_urls=["http://x/img.png"],
            detail_html="<html></html>",
            status="SUSPENSION",
            notice=_ETC_NOTICE_BODY,
            preview_confirmed=True,
        )

        # 보정 PUT 이 500 → 예외 경로로 status_correction_error 설정.
        assert result.get("status_correction_attempted") is True
        assert result.get("status_corrected") is False
        assert result.get("status_correction_error") is not None, (
            "보정 PUT 이 거부되면 status_correction_error 에 사유가 실려야 한다 "
            "(ok=False 만 남기면 판매자가 어찌할 바를 알 수 없다)."
        )
        # 사유 텍스트에 HTTP 상태가 드러나야 한다.
        assert (
            "500" in result["status_correction_error"]
        ), "status_correction_error 에 HTTP 상태(500) 가 포함되어야 한다."
        # ok=False (보정 실패로 상태가 맞춰지지 않았다).
        assert result["ok"] is False
        assert result["requested_status"] == "SUSPENSION"
        assert result["applied_status"] == "SALE"


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
