# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""등록 결과(record) 저장·조회 및 채널상품번호 노출 검증.

실등록에서 발견된 결함: 네이버 상태 변경은 **채널상품번호**
(``smartstoreChannelProductNo``) 로 한다. 그 번호는 **등록 응답에만** 들어
있고 ``get_product``(origin-products 조회) 응답에는 없다. 등록 직후에 그
번호를 디스크에 남겨두지 않으면 이후 그 상품을 다시 손댈 방법이 사라진다.

본 테스트는 (a)~(h):

  (a) 등록 성공 시 기록 파일이 생기고 ``channel_product_no`` 가 응답값과
      일치한다.
  (b) 반환에 ``channel_product_no`` 키가 있다(성공·실패 양쪽 모두 키 존재).
  (c) 응답에 채널번호가 없으면 그 사실이 반환에 드러난다(조용한 누락 금지).
  (d) 요청 상태 ≠ 응답 상태일 때 **보정 호출이 채널번호로** 일어나고 최종
      상태가 반영된다.
  (e) 보정 후에도 다르면 ``ok=False``.
  (f) 요청 상태 = 응답 상태면 **추가 호출 0회**.
  (g) 저장된 기록을 ``product_key`` 로 다시 읽을 수 있다.
  (h) 등록이 실패(비 2xx)하면 기록을 남기지 않는다.

``COMMERCE_DRY_RUN`` 은 끈 상태로, 실제 네이버 HTTP 호출은 mock 으로 차단하고
호출 횟수를 센다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import common, mcp_server, naver_client
from clossify import register as register_mod

# ============================================================================
# 공통 픽스처·헬퍼.
# ============================================================================

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
_ETC_NOTICE_BODY = {
    "productInfoProvidedNoticeType": "ETC",
    "etc": {"modelName": "TEST-MODEL-REC", "certificateDetails": "KW 인증"},
}


@pytest.fixture
def isolated_prepared_dir(tmp_path, monkeypatch):
    """common.PREPARED_DIR 을 tmp_path 로 격리.

    register_mod._prepared_dir() 는 호출 시점에 Path(common.PREPARED_DIR) 을
    읽으므로, monkeypatch 가 테스트 동안 유효하다.
    """
    fake_dir = tmp_path / "prepared"
    fake_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(common, "PREPARED_DIR", fake_dir)
    return fake_dir


def _patch_register_chain(
    monkeypatch,
    *,
    created_status: str = "SALE",
    verified_status: str = "SALE",
    update_calls: list | None = None,
    get_calls: list | None = None,
    register_calls: list | None = None,
    origin_no: str = "ORIGIN-REC",
    channel_no: str | None = "CH-REC",
    register_status_code: int = 200,
    register_body: dict | None = None,
):
    """naver_client HTTP 계층(register/update/get)을 mock 으로 차단.

    - ``register_body`` 가 주어지면 그것을 등록 응답으로 그대로 쓴다
      (채널번호 누락 케이스 재현용).
    - 그렇지 않으면 ``origin_no``/``channel_no``/``created_status`` 로
      합성한다.
    """
    if update_calls is None:
        update_calls = []
    if get_calls is None:
        get_calls = []
    if register_calls is None:
        register_calls = []

    def _fake_register(payload):
        register_calls.append(payload)
        if register_body is not None:
            return register_status_code, register_body
        body = {
            "originProductNo": origin_no,
            "originProduct": {"statusType": created_status},
        }
        if channel_no is not None:
            body["channelProductNo"] = channel_no
        return register_status_code, body

    def _fake_update(cn, p):
        update_calls.append((cn, p))
        # 실 API 와 동일하게: originProduct 없는 단편 본문은 거부(400).
        # 이것이 없으면 단편 회귀가 200 으로 통과한다 (실측에서는 무시됨).
        if not isinstance(p, dict) or not isinstance(p.get("originProduct"), dict):
            return 400, {"code": "BAD_REQUEST", "message": "originProduct required"}
        return 200, {}

    def _fake_get(no):
        get_calls.append(no)
        # 보정 경로가 read-mutate-send 전체 본문을 보내려면 get_product 가
        # 전체 리소스(originProduct + smartstoreChannelProduct) 를 반환해야 한다.
        return 200, {
            "originProduct": {"statusType": verified_status, "originProductNo": no},
            "smartstoreChannelProduct": {
                "channelProductDisplayStatusType": verified_status,
                "channelProductNo": channel_no or "",
            },
        }

    monkeypatch.setattr(naver_client, "register_product", _fake_register)
    monkeypatch.setattr(naver_client, "update_product", _fake_update)
    monkeypatch.setattr(naver_client, "get_product", _fake_get)
    monkeypatch.setattr(naver_client, "get_token", lambda: "t")
    monkeypatch.setattr(naver_client, "_notice_config", lambda: _NOTICE_CFG_FULL)
    monkeypatch.setattr(naver_client, "_kc_config", lambda: ({}, ""))
    monkeypatch.setattr(common, "cfg", lambda: {"smartstore_notice_defaults": _NOTICE_CFG_FULL})


# ============================================================================
# (a) 등록 성공 시 기록 파일이 생기고 channel_product_no 가 응답값과 일치.
# ============================================================================
class TestRecordFileCreated:
    def test_record_file_matches_response(self, isolated_prepared_dir, monkeypatch):
        """성공 등록 → registration_record.json 생성, channel_product_no 일치."""
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
        _patch_register_chain(
            monkeypatch,
            created_status="SALE",
            verified_status="SALE",
            origin_no="ORIGIN-A",
            channel_no="CH-A-VALUE",
        )

        result = mcp_server.register_product(
            name="기록파일테스트",
            price=10000,
            category_id=_GENERAL_CATEGORY,
            image_urls=["http://x/img.png"],
            detail_html="<html></html>",
            status="SALE",
            notice=_ETC_NOTICE_BODY,
            preview_confirmed=True,
        )

        assert result["ok"] is True
        assert result["channel_product_no"] == "CH-A-VALUE"

        # 기록 파일이 실제로 디스크에 있다. product_key 는 호출자가 준
        # 이름+가격 으로 유도된다(명시 product_key 를 주지 않았으므로).
        pkey = register_mod.make_product_key("기록파일테스트", 10000)
        record_path = register_mod._registration_record_path(pkey)
        assert record_path.exists(), f"기록 파일이 있어야 한다: {record_path}"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        assert record["channel_product_no"] == "CH-A-VALUE"
        assert record["origin_product_no"] == "ORIGIN-A"
        assert record["product_key"] == pkey


# ============================================================================
# (b) 반환에 channel_product_no 키가 있다 (성공·실패 양쪽 모두 키 존재).
# ============================================================================
class TestChannelNoKeyAlwaysPresent:
    def test_key_present_on_success(self, isolated_prepared_dir, monkeypatch):
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
        _patch_register_chain(
            monkeypatch,
            origin_no="ORIGIN_B1",
            channel_no="CH_B1",
        )
        result = mcp_server.register_product(
            name="성공반환키",
            price=10000,
            category_id=_GENERAL_CATEGORY,
            image_urls=["http://x/img.png"],
            detail_html="<html></html>",
            status="SALE",
            notice=_ETC_NOTICE_BODY,
            preview_confirmed=True,
        )
        assert "channel_product_no" in result
        assert result["channel_product_no"] == "CH_B1"

    def test_key_present_on_failure(self, isolated_prepared_dir, monkeypatch):
        """검증 실패(빈 이름) 반환에도 channel_product_no 키가 있다."""
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
        result = mcp_server.register_product(
            name="   ",
            price=10000,
            category_id=_GENERAL_CATEGORY,
            image_urls=["http://x/img.png"],
            detail_html="<html></html>",
            preview_confirmed=True,
        )
        assert "channel_product_no" in result
        assert result["channel_product_no"] is None
        assert result["missing_channel_no"] is True

    def test_key_present_on_non_2xx(self, isolated_prepared_dir, monkeypatch):
        """비 2xx 응답에도 channel_product_no 키가 있다(None)."""
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
        _patch_register_chain(
            monkeypatch,
            register_status_code=500,
            register_body={"code": "INTERNAL"},
            channel_no=None,
        )
        result = mcp_server.register_product(
            name="실패반환키",
            price=10000,
            category_id=_GENERAL_CATEGORY,
            image_urls=["http://x/img.png"],
            detail_html="<html></html>",
            status="SALE",
            notice=_ETC_NOTICE_BODY,
            preview_confirmed=True,
        )
        assert "channel_product_no" in result
        assert result["channel_product_no"] is None
        assert result["missing_channel_no"] is True


# ============================================================================
# (c) 응답에 채널번호가 없으면 그 사실이 반환에 드러난다 (조용한 누락 금지).
# ============================================================================
class TestMissingChannelNoSurfaced:
    def test_missing_channel_no_flagged(self, isolated_prepared_dir, monkeypatch):
        """등록은 성공(2xx) 했으나 응답에 채널번호가 없으면 missing_channel_no=True."""
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
        _patch_register_chain(
            monkeypatch,
            created_status="SALE",
            verified_status="SALE",
            origin_no="ORIGIN-C",
            channel_no=None,  # 응답에 채널번호 없음.
        )

        result = mcp_server.register_product(
            name="채널번호누락테스트",
            price=10000,
            category_id=_GENERAL_CATEGORY,
            image_urls=["http://x/img.png"],
            detail_html="<html></html>",
            status="SALE",
            notice=_ETC_NOTICE_BODY,
            preview_confirmed=True,
        )

        # 등록 자체는 성공(SALE=SALE 이므로 보정 없음).
        assert result["ok"] is True
        # 하지만 채널번호가 없다는 사실이 드러난다.
        assert result["channel_product_no"] is None
        assert result["missing_channel_no"] is True, (
            "응답에 채널번호가 없으면 missing_channel_no=True 로 드러나야 한다 "
            "(조용한 누락 금지 — 이후 수정이 불가능해진다는 뜻이므로)."
        )


# ============================================================================
# (d) 요청 상태 ≠ 응답 상태일 때 보정 호출이 채널번호로 일어나고 최종 상태가
#     반영된다.
# ============================================================================
class TestStatusCorrectionUsesChannelNo:
    def test_correction_called_with_channel_no(self, isolated_prepared_dir, monkeypatch):
        """요청=SUSPENSION, 응답=SALE → update_product 가 채널번호로 호출된다."""
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)

        update_calls: list = []
        get_calls: list = []
        _patch_register_chain(
            monkeypatch,
            created_status="SALE",
            verified_status="SUSPENSION",
            update_calls=update_calls,
            get_calls=get_calls,
            origin_no="ORIGIN-D",
            channel_no="CH-D-VALUE",
        )

        result = mcp_server.register_product(
            name="채널번호보정테스트",
            price=10000,
            category_id=_GENERAL_CATEGORY,
            image_urls=["http://x/img.png"],
            detail_html="<html></html>",
            status="SUSPENSION",
            notice=_ETC_NOTICE_BODY,
            preview_confirmed=True,
        )

        # 보정이 채널번호로 일어난다.
        assert len(update_calls) == 1, "보정(update_product) 이 1회 일어나야 한다"
        sent_channel, sent_payload = update_calls[0]
        assert sent_channel == "CH-D-VALUE", (
            "보정 호출은 등록 응답의 채널상품번호로 해야 한다 "
            "(get_product 응답에는 그 번호가 없다)."
        )
        # 보정 PUT 본문은 전체 originProduct 를 포함한다 — 단편은 네이버 API
        # 가 무시한다(실측 확인).
        assert isinstance(sent_payload.get("originProduct"), dict)
        assert sent_payload["originProduct"].get("statusType") == "SUSPENSION"
        # 최종 상태가 반영된다.
        assert result["applied_status"] == "SUSPENSION"
        assert result["ok"] is True


# ============================================================================
# (e) 보정 후에도 다르면 ok=False (조용한 성공 금지).
# ============================================================================
class TestStillMismatchReportsFailure:
    def test_persistent_mismatch_ok_false(self, isolated_prepared_dir, monkeypatch):
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)

        _patch_register_chain(
            monkeypatch,
            created_status="SALE",
            verified_status="SALE",  # 보정 후에도 SALE.
            origin_no="ORIGIN-E",
            channel_no="CH-E",
        )

        result = mcp_server.register_product(
            name="지속불일치기록",
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


# ============================================================================
# (f) 요청 상태 = 응답 상태면 추가 호출 0회.
# ============================================================================
class TestNoExtraCallWhenAlreadyMatching:
    def test_matching_sale_no_correction(self, isolated_prepared_dir, monkeypatch):
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)

        update_calls: list = []
        get_calls: list = []
        _patch_register_chain(
            monkeypatch,
            created_status="SALE",
            verified_status="SALE",
            update_calls=update_calls,
            get_calls=get_calls,
            origin_no="ORIGIN-F",
            channel_no="CH-F",
        )

        result = mcp_server.register_product(
            name="추가호출0회기록",
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
        assert result.get("status_correction_attempted") is False


# ============================================================================
# (g) 저장된 기록을 product_key 로 다시 읽을 수 있다.
# ============================================================================
class TestReadRecordByKey:
    def test_read_by_product_key(self, isolated_prepared_dir, monkeypatch):
        """등록 후 read_registration_record(product_key=...) 로 같은 기록을 읽는다."""
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
        _patch_register_chain(
            monkeypatch,
            created_status="SALE",
            verified_status="SALE",
            origin_no="ORIGIN-G",
            channel_no="CH-G",
        )

        result = mcp_server.register_product(
            name="기록조회테스트",
            price=10000,
            category_id=_GENERAL_CATEGORY,
            image_urls=["http://x/img.png"],
            detail_html="<html></html>",
            status="SALE",
            notice=_ETC_NOTICE_BODY,
            preview_confirmed=True,
        )
        assert result["ok"] is True

        # product_key 는 호출자가 준 이름+가격 으로 유도된다(명시 키를 주지
        # 않았으므로). 같은 방식으로 유도해 기록을 다시 읽는다.
        pkey = register_mod.make_product_key("기록조회테스트", 10000)
        assert pkey, "product_key 유도에 실패하지 않아야 한다"

        # 헬퍼로 다시 읽기 — 이후 수정 기능이 올라탈 자리.
        record = register_mod.read_registration_record(product_key=pkey)
        assert record is not None, "저장된 기록을 product_key 로 읽을 수 있어야 한다"
        assert record["channel_product_no"] == "CH-G"
        assert record["origin_product_no"] == "ORIGIN-G"
        assert record["product_key"] == pkey

    def test_read_by_origin_product_no(self, isolated_prepared_dir, monkeypatch):
        """origin_product_no 로도 기록을 찾을 수 있다 (순회 폴백)."""
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
        _patch_register_chain(
            monkeypatch,
            created_status="SALE",
            verified_status="SALE",
            origin_no="ORIGIN_G2",
            channel_no="CH_G2",
        )

        result = mcp_server.register_product(
            name="원산번호조회",
            price=10000,
            category_id=_GENERAL_CATEGORY,
            image_urls=["http://x/img.png"],
            detail_html="<html></html>",
            status="SALE",
            notice=_ETC_NOTICE_BODY,
            preview_confirmed=True,
        )
        assert result["ok"] is True

        record = register_mod.read_registration_record(origin_product_no="ORIGIN_G2")
        assert record is not None, "origin_product_no 로 기록을 찾을 수 있어야 한다"
        assert record["channel_product_no"] == "CH_G2"


# ============================================================================
# (h) 등록이 실패(비 2xx)하면 기록을 남기지 않는다.
# ============================================================================
class TestNoRecordOnFailure:
    def test_no_record_on_non_2xx(self, isolated_prepared_dir, monkeypatch):
        """비 2xx 응답 → registration_record.json 이 생기지 않는다."""
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
        _patch_register_chain(
            monkeypatch,
            register_status_code=400,
            register_body={"code": "Bad Request"},
            channel_no=None,
        )

        result = mcp_server.register_product(
            name="실패시기록없음",
            price=10000,
            category_id=_GENERAL_CATEGORY,
            image_urls=["http://x/img.png"],
            detail_html="<html></html>",
            status="SALE",
            notice=_ETC_NOTICE_BODY,
            preview_confirmed=True,
        )

        assert result["ok"] is False
        assert result["channel_product_no"] is None
        assert result.get("registration_record") is None

        # 기록 파일이 디스크에 없다. product_key 를 같은 방식으로 유도해 확인.
        pkey = register_mod.make_product_key("실패시기록없음", 10000)
        record_path = register_mod._registration_record_path(pkey)
        assert not record_path.exists(), (
            "등록 실패 시 기록 파일이 남으면 안 된다 "
            "(실패한 등록을 수정 가능한 것으로 오인시킨다)."
        )


# ============================================================================
# (i) 실등록 관측 응답 형태: smartstoreChannelProductNo 가 최상위 키.
#     종전에는 channelProductNo 만 찾아 이 형태에서 None 을 돌려줬다 — 상태
#     변경에 필요한 번호가 응답에 있음에도 디스크에 남지 않았다.
# ============================================================================
class TestRealObservedSmartstoreKey:
    def test_extractor_recognises_top_level_smartstore_key(self):
        """실등록 관측 응답을 _extract_channel_product_no 가 인식한다.

        관측된 응답: originProductNo 와 smartstoreChannelProductNo 가 나란히
        최상위에 있다. 종전 코드는 이 키를 몰라 None 을 반환했다.
        """
        body = {
            "originProductNo": 13637961866,
            "smartstoreChannelProductNo": 13698239397,
            "originProduct": {"statusType": "SALE"},
        }
        assert register_mod._extract_channel_product_no(body) == 13698239397

    def test_registration_persists_smartstore_channel_no(self, isolated_prepared_dir, monkeypatch):
        """smartstoreChannelProductNo 형태 응답 → channel_product_no 설정·기록.

        실관측 응답 형태로 등록했을 때: 반환의 channel_product_no 가 채워지고,
        missing_channel_no 가 False 이며, 기록 파일에 그 번호가 남는다.
        """
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
        _patch_register_chain(
            monkeypatch,
            created_status="SALE",
            verified_status="SALE",
            origin_no=13637961866,
            register_body={
                "originProductNo": 13637961866,
                "smartstoreChannelProductNo": 13698239397,
                "originProduct": {"statusType": "SALE"},
            },
        )

        result = mcp_server.register_product(
            name="스마트스토어채널번호테스트",
            price=10000,
            category_id=_GENERAL_CATEGORY,
            image_urls=["http://x/img.png"],
            detail_html="<html></html>",
            status="SALE",
            notice=_ETC_NOTICE_BODY,
            preview_confirmed=True,
        )

        assert result["ok"] is True
        assert result["channel_product_no"] == 13698239397, (
            "실등록 관측 응답의 smartstoreChannelProductNo 가 channel_product_no "
            "로 잡혀야 한다 (종전엔 None 이었다)."
        )
        assert result["missing_channel_no"] is False

        pkey = register_mod.make_product_key("스마트스토어채널번호테스트", 10000)
        record_path = register_mod._registration_record_path(pkey)
        assert record_path.exists(), "채널번호가 있으므로 기록 파일이 있어야 한다"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        # write_registration_record 은 str() 로 정규화해 저장하므로 문자열 비교.
        assert record["channel_product_no"] == "13698239397"
        assert record["origin_product_no"] == 13637961866

    def test_legacy_fallback_shapes_still_resolve(self):
        """종전에 지원하던 폴백 형태들이 여전히 해석된다 (회귀 방지)."""
        # 최상위 channelProductNo.
        assert (
            register_mod._extract_channel_product_no({"channelProductNo": "CH-DIRECT"})
            == "CH-DIRECT"
        )
        # 중첩 channelProduct.channelProductNo.
        assert (
            register_mod._extract_channel_product_no(
                {"channelProduct": {"channelProductNo": "CH-NESTED-CP"}}
            )
            == "CH-NESTED-CP"
        )
        # 중첩 originProduct.channelProductNo.
        assert (
            register_mod._extract_channel_product_no(
                {"originProduct": {"channelProductNo": "CH-NESTED-OP"}}
            )
            == "CH-NESTED-OP"
        )
        # 중첩 smartstoreChannelProduct.channelProductNo.
        assert (
            register_mod._extract_channel_product_no(
                {"smartstoreChannelProduct": {"channelProductNo": "CH-NESTED-SS"}}
            )
            == "CH-NESTED-SS"
        )
        # 어느 키도 없으면 None.
        assert register_mod._extract_channel_product_no({"originProductNo": 1}) is None


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
