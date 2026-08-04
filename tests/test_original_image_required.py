"""원본 이미지 최소 1장 강제 (fail-closed) 검증 테스트.

유효한 원본 이미지가 1장도 없으면 페이로드를 만들지도, 네이버 API 를 호출하지도
않는 것을 검증한다. 이미지가 "존재하는가" 만을 판정하는 진입 게이트(entry gate)다.
이미지의 진위·출처·내용은 판별하지 않는다 (탐지 게이트가 아님).

반례 전수:
  (a) build_payload 가 8종 무효 입력 각각에 ValueError (IndexError/TypeError 아님).
  (b) mcp_server.register_product 가 8종 각각에 ok=False, requests.post 0회.
      고시 필드는 모두 정상값으로 채운 상태로 테스트 — 이미지 때문에 막혔음을 증명.
  (c) 실패 사유에 이미지 관련 rule/메시지가 실제로 포함되는지 단언.
  (d) naver_client.register_product 에 대표이미지 url 이 빈 페이로드 → ValueError + POST 0회.
  (e) 혼합 입력 ["https://cdn/a.jpg", ""] → 거부 (조용한 필터링 아님).
  (f) 양성 케이스: 유효 URL 1장 + 고시 정상값 → 이미지 사유로는 막히지 않음.
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

# --------------------------------------------------------------------------- #
# 테스트용 공통 픽스처.
# --------------------------------------------------------------------------- #

# 의류 카테고리 (KC 불필요, WEAR 고시 타입).
_CLOTHING_CATEGORY = "50021299"

# 고시 config mock: origin 이 설정된 정상 config.
_NOTICE_CFG_WITH_ORIGIN = {
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

# WEAR 고시 필수 필드를 모두 채운 notice override.
# 이것을 제공하면 고시 필드 사유로는 막히지 않는다.
_WEAR_NOTICE_COMPLETE = {
    "productInfoProvidedNoticeType": "WEAR",
    "etc": {
        "material": "면 100%",
        "color": "블랙",
        "size": "FREE",
        "caution": "물 세탁 가능",
        "packDateText": "2024-01-01",
        "warrantyPolicy": "구매 후 7일 이내 교환 가능",
    },
}


def _make_call_recorder(call_log: list):
    """naver_client.register_product 호출을 기록하는 mock factory."""

    def _recorder(*args, **kwargs):
        call_log.append({"args": args, "kwargs": kwargs})
        return (200, {"originProductNo": "test-origin-no-123"})

    return _recorder


# 무효 입력 8종 + 혼합 1종.
# 각 항목은 (설명, 입력값) 튜플.
_INVALID_IMAGE_INPUTS = [
    ("empty_list", []),
    ("single_empty_string", [""]),
    ("single_whitespace", ["   "]),
    ("single_tab_newline", ["\t\n"]),
    ("single_none", [None]),
    ("none", None),
    ("string_not_list", "문자열"),
    ("single_int", [123]),
]

# 혼합 입력: 유효 + 무효.
_MIXED_INPUT = ("mixed_valid_invalid", ["https://cdn/a.jpg", ""])


# --------------------------------------------------------------------------- #
# (a) build_payload 가 무효 입력에 ValueError 를 발생시키는가.
# --------------------------------------------------------------------------- #
class TestBuildPayloadRejectsInvalidImages:
    """build_payload 진입 게이트: 무효 이미지 → ValueError (IndexError/TypeError 아님)."""

    @pytest.mark.parametrize(
        "label,images",
        _INVALID_IMAGE_INPUTS + [_MIXED_INPUT],
        ids=[item[0] for item in _INVALID_IMAGE_INPUTS + [_MIXED_INPUT]],
    )
    def test_raises_value_error(self, label, images):
        """모든 무효 입력에 대해 ValueError 를 발생시켜야 한다."""
        product = {
            "name": "테스트니트",
            "categoryId": _CLOTHING_CATEGORY,
            "salePrice": 30000,
        }
        with mock.patch.object(
            naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
        ):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with pytest.raises(ValueError) as exc_info:
                    naver_client.build_payload(product, "<html></html>", images)
                # ValueError 이어야 함 — IndexError/TypeError 가 아님.
                assert "원본 이미지" in str(
                    exc_info.value
                ), f"에러 메시지에 '원본 이미지' 가 없음: {exc_info.value}"


# --------------------------------------------------------------------------- #
# (b) mcp_server.register_product 가 무효 입력에 ok=False, POST 0회.
# 고시 필드를 모두 정상값으로 채운 상태로 테스트.
# --------------------------------------------------------------------------- #
class TestMcpRegisterRejectsInvalidImages:
    """mcp_server.register_product: 무효 이미지 → ok=False, 네이버 API 0회 호출."""

    @pytest.mark.parametrize(
        "label,image_urls",
        _INVALID_IMAGE_INPUTS,
        ids=[item[0] for item in _INVALID_IMAGE_INPUTS],
    )
    def test_rejects_with_ok_false_and_zero_posts(self, label, image_urls):
        """무효 이미지 입력 → ok=False, requests.post 0회 호출."""
        naver_calls: list = []
        # requests.post 를 직접 mock 하여 네트워크 호출이 0회임을 보장한다.
        with mock.patch.object(
            naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
        ):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(
                    common,
                    "cfg",
                    return_value={
                        "smartstore_notice_defaults": {
                            "origin_area_code": "04",
                            "origin_content": "중국",
                        },
                    },
                ):
                    with mock.patch.object(
                        naver_client,
                        "register_product",
                        side_effect=_make_call_recorder(naver_calls),
                    ):
                        result = mcp_server.register_product(
                            name="테스트니트",
                            price=30000,
                            image_urls=image_urls,
                            category_id=_CLOTHING_CATEGORY,
                            detail_html="<html><body>상세</body></html>",
                            notice=_WEAR_NOTICE_COMPLETE,
                            preview_confirmed=True,
                        )

        # ok=False 여야 함.
        assert result["ok"] is False, f"{label}: ok 가 False 여야 함"
        # 네이버 API (naver_client.register_product) 가 호출되지 않아야 함.
        assert (
            len(naver_calls) == 0
        ), f"{label}: 네이버 API 호출이 {len(naver_calls)}회 발생 (0회여야 함)"

    # (c) 실패 사유에 이미지 관련 rule/메시지가 실제로 포함되는지.
    @pytest.mark.parametrize(
        "label,image_urls",
        _INVALID_IMAGE_INPUTS,
        ids=[item[0] for item in _INVALID_IMAGE_INPUTS],
    )
    def test_error_message_mentions_image(self, label, image_urls):
        """실패 사유(error) 에 '이미지' 관련 메시지가 포함되어야 한다."""
        with mock.patch.object(
            naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
        ):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(
                    common,
                    "cfg",
                    return_value={
                        "smartstore_notice_defaults": {
                            "origin_area_code": "04",
                            "origin_content": "중국",
                        },
                    },
                ):
                    with mock.patch.object(
                        naver_client, "register_product", return_value=(200, {})
                    ):
                        result = mcp_server.register_product(
                            name="테스트니트",
                            price=30000,
                            image_urls=image_urls,
                            category_id=_CLOTHING_CATEGORY,
                            detail_html="<html><body>상세</body></html>",
                            notice=_WEAR_NOTICE_COMPLETE,
                            preview_confirmed=True,
                        )

        error_text = str(result.get("error") or "")
        assert "이미지" in error_text, f"{label}: error 에 '이미지' 가 없음: {error_text!r}"


# --------------------------------------------------------------------------- #
# (d) naver_client.register_product 에 대표이미지 url 이 빈 페이로드를 직접 넣음.
# --------------------------------------------------------------------------- #
class TestNaverRegisterRejectsEmptyRepresentativeImage:
    """naver_client.register_product: 대표 이미지 URL 빈 페이로드 → ValueError + POST 0회."""

    def test_empty_rep_url_raises_value_error(self):
        """대표 이미지 URL 이 빈 문자열인 페이로드 → ValueError."""
        payload = {
            "originProduct": {
                "images": {
                    "representativeImage": {"url": ""},
                    "optionalImages": [],
                },
            },
        }
        post_calls: list = []
        with mock.patch("requests.post", side_effect=lambda *a, **kw: post_calls.append(a)):
            with pytest.raises(ValueError) as exc_info:
                naver_client.register_product(payload, tk="fake-token")
            assert "대표 이미지" in str(exc_info.value) or "원본 이미지" in str(exc_info.value)
        # requests.post 가 0회여야 함.
        assert len(post_calls) == 0, f"requests.post 가 {len(post_calls)}회 호출됨 (0회여야 함)"

    def test_whitespace_rep_url_raises_value_error(self):
        """대표 이미지 URL 이 공백인 페이로드 → ValueError."""
        payload = {
            "originProduct": {
                "images": {
                    "representativeImage": {"url": "   "},
                    "optionalImages": [],
                },
            },
        }
        post_calls: list = []
        with mock.patch("requests.post", side_effect=lambda *a, **kw: post_calls.append(a)):
            with pytest.raises(ValueError):
                naver_client.register_product(payload, tk="fake-token")
        assert len(post_calls) == 0

    def test_none_rep_url_raises_value_error(self):
        """대표 이미지 URL 이 None 인 페이로드 → ValueError."""
        payload = {
            "originProduct": {
                "images": {
                    "representativeImage": {"url": None},
                    "optionalImages": [],
                },
            },
        }
        post_calls: list = []
        with mock.patch("requests.post", side_effect=lambda *a, **kw: post_calls.append(a)):
            with pytest.raises(ValueError):
                naver_client.register_product(payload, tk="fake-token")
        assert len(post_calls) == 0

    def test_missing_rep_url_key_raises_value_error(self):
        """representativeImage 키 자체가 없는 페이로드 → ValueError."""
        payload = {
            "originProduct": {
                "images": {},
            },
        }
        post_calls: list = []
        with mock.patch("requests.post", side_effect=lambda *a, **kw: post_calls.append(a)):
            with pytest.raises(ValueError):
                naver_client.register_product(payload, tk="fake-token")
        assert len(post_calls) == 0


# --------------------------------------------------------------------------- #
# (e) 혼합 입력 ["https://cdn/a.jpg", ""] → 거부 (조용한 필터링 아님).
# --------------------------------------------------------------------------- #
class TestMixedInputRejected:
    """유효 + 무효 혼합 입력은 조용히 필터링하지 않고 거부한다."""

    def test_build_payload_rejects_mixed(self):
        """build_payload 가 ["https://cdn/a.jpg", ""] 를 ValueError 로 거부."""
        product = {
            "name": "테스트니트",
            "categoryId": _CLOTHING_CATEGORY,
            "salePrice": 30000,
        }
        with mock.patch.object(
            naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
        ):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with pytest.raises(ValueError) as exc_info:
                    naver_client.build_payload(product, "<html></html>", ["https://cdn/a.jpg", ""])
                # 혼합 입력 거부 메시지에 "원본 이미지" 포함.
                assert "원본 이미지" in str(exc_info.value)

    def test_mcp_register_rejects_mixed_with_zero_posts(self):
        """mcp_server.register_product 가 혼합 입력을 거부하고 POST 0회."""
        naver_calls: list = []
        with mock.patch.object(
            naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
        ):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(
                    common,
                    "cfg",
                    return_value={
                        "smartstore_notice_defaults": {
                            "origin_area_code": "04",
                            "origin_content": "중국",
                        },
                    },
                ):
                    with mock.patch.object(
                        naver_client,
                        "register_product",
                        side_effect=_make_call_recorder(naver_calls),
                    ):
                        result = mcp_server.register_product(
                            name="테스트니트",
                            price=30000,
                            image_urls=["https://cdn/a.jpg", ""],
                            category_id=_CLOTHING_CATEGORY,
                            detail_html="<html><body>상세</body></html>",
                            notice=_WEAR_NOTICE_COMPLETE,
                            preview_confirmed=True,
                        )
        assert result["ok"] is False
        assert len(naver_calls) == 0, "혼합 입력 시 네이버 API 호출 금지"
        error_text = str(result.get("error") or "")
        assert "이미지" in error_text


# --------------------------------------------------------------------------- #
# (f) 양성 케이스: 유효 URL 1장 + 고시 정상값 → 이미지 사유로는 막히지 않음.
# --------------------------------------------------------------------------- #
class TestValidImagePassesImageGate:
    """유효 URL 1장 + 고시 정상값은 이미지 사유로 막히지 않아야 한다."""

    def test_valid_single_url_not_blocked_by_image(self):
        """유효 URL 1장 → 이미지 rule 로 인한 차단이 없어야 함.

        다른 사유(예: 고시 필드)로 막히는 것과 구분되게 단언한다.
        notice 를 완비하면 통과해야 한다.
        """
        naver_calls: list = []
        with mock.patch.object(
            naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
        ):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(
                    common,
                    "cfg",
                    return_value={
                        "smartstore_notice_defaults": {
                            "origin_area_code": "04",
                            "origin_content": "중국",
                        },
                    },
                ):
                    with mock.patch.object(
                        naver_client,
                        "register_product",
                        side_effect=_make_call_recorder(naver_calls),
                    ):
                        result = mcp_server.register_product(
                            name="테스트니트",
                            price=30000,
                            image_urls=["https://cdn/test.jpg"],
                            category_id=_CLOTHING_CATEGORY,
                            detail_html="<html><body>상세</body></html>",
                            notice=_WEAR_NOTICE_COMPLETE,
                            preview_confirmed=True,
                        )
        # 통과해야 함.
        assert result["ok"] is True, f"유효 이미지 1장 + 고시 완비 시 통과해야 함: {result}"
        # 네이버 API 가 1회 호출되어야 함.
        assert (
            len(naver_calls) == 1
        ), f"유효 이미지 시 네이버 API 가 1회 호출되어야 함: {len(naver_calls)}"
        # error 에 '이미지' 사유가 없어야 함.
        error_text = str(result.get("error") or "")
        assert "원본 이미지" not in error_text

    def test_valid_multiple_urls_not_blocked_by_image(self):
        """유효 URL 여러 장 → 이미지 rule 로 인한 차단이 없어야 함."""
        naver_calls: list = []
        with mock.patch.object(
            naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
        ):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(
                    common,
                    "cfg",
                    return_value={
                        "smartstore_notice_defaults": {
                            "origin_area_code": "04",
                            "origin_content": "중국",
                        },
                    },
                ):
                    with mock.patch.object(
                        naver_client,
                        "register_product",
                        side_effect=_make_call_recorder(naver_calls),
                    ):
                        result = mcp_server.register_product(
                            name="테스트니트",
                            price=30000,
                            image_urls=[
                                "https://cdn/a.jpg",
                                "https://cdn/b.jpg",
                                "https://cdn/c.jpg",
                            ],
                            category_id=_CLOTHING_CATEGORY,
                            detail_html="<html><body>상세</body></html>",
                            notice=_WEAR_NOTICE_COMPLETE,
                            preview_confirmed=True,
                        )
        assert result["ok"] is True, f"유효 이미지 3장 시 통과해야 함: {result}"
        assert len(naver_calls) == 1


# --------------------------------------------------------------------------- #
# 보너스: qa_agents 컴플라이언스 검사에서 원본 이미지 rule 이 별도로 발동하는가.
# --------------------------------------------------------------------------- #
class TestComplianceOriginalImageRule:
    """qa_agents._compliance_code_check 가 원본 이미지 rule 을 별도로 내는가."""

    def test_empty_rep_url_triggers_original_image_rule(self):
        """대표 이미지 URL 이 비었을 때 rule='원본 이미지' FAIL 위반이 나는가."""
        from clossify import qa_agents

        payload = {
            "originProduct": {
                "images": {
                    "representativeImage": {"url": ""},
                },
                "detailAttribute": {},
            },
        }
        result = qa_agents._compliance_code_check("테스트", {}, api_payload=payload)
        violations = result.get("violations") or []
        image_rules = [v for v in violations if str(v.get("rule") or "") == "원본 이미지"]
        assert len(image_rules) > 0, f"rule='원본 이미지' 위반이 없음: {violations}"
        # severity 가 FAIL 이어야 함.
        for v in image_rules:
            assert str(v.get("severity") or "").upper() == qa_agents.FAIL

    def test_valid_rep_url_does_not_trigger_original_image_rule(self):
        """대표 이미지 URL 이 유효하면 rule='원본 이미지' 위반이 없어야 함."""
        from clossify import qa_agents

        payload = {
            "originProduct": {
                "images": {
                    "representativeImage": {"url": "https://cdn/test.jpg"},
                },
                "detailAttribute": {},
            },
        }
        result = qa_agents._compliance_code_check("테스트", {}, api_payload=payload)
        violations = result.get("violations") or []
        image_rules = [v for v in violations if str(v.get("rule") or "") == "원본 이미지"]
        assert (
            len(image_rules) == 0
        ), f"유효 이미지인데 rule='원본 이미지' 위반이 나옴: {image_rules}"


# --------------------------------------------------------------------------- #
# (g) 회귀 가드: api_payload 없이 호출하면 "원본 이미지" 위반이 없어야 한다.
# 페이로드를 관측할 수 없는 경로(prepare_listing)에서 "없음" 으로 단정해
# 정상 상품을 영구 차단하는 과잉 차단 회귀를 잡는다.
# --------------------------------------------------------------------------- #
class TestComplianceNoImageRuleWithoutPayload:
    """api_payload 가 없으면 rule='원본 이미지' 위반이 하나도 없어야 한다."""

    def test_no_image_rule_when_api_payload_omitted(self):
        """api_payload 를 주지 않으면 '원본 이미지' 위반이 없어야 한다."""
        from clossify import qa_agents

        result = qa_agents._compliance_code_check("테스트", {"category_id": "50021299"})
        violations = result.get("violations") or []
        image_rules = [v for v in violations if str(v.get("rule") or "") == "원본 이미지"]
        assert (
            len(image_rules) == 0
        ), f"api_payload 가 없는데 '원본 이미지' 위반이 나옴: {image_rules}"

    def test_no_image_rule_when_api_payload_none(self):
        """api_payload 로 None 을 명시해도 '원본 이미지' 위반이 없어야 한다."""
        from clossify import qa_agents

        result = qa_agents._compliance_code_check(
            "테스트", {"category_id": "50021299"}, api_payload=None
        )
        violations = result.get("violations") or []
        image_rules = [v for v in violations if str(v.get("rule") or "") == "원본 이미지"]
        assert (
            len(image_rules) == 0
        ), f"api_payload=None 인데 '원본 이미지' 위반이 나옴: {image_rules}"

    def test_no_image_rule_when_api_payload_not_dict(self):
        """api_payload 가 dict 가 아니면 '원본 이미지' 위반이 없어야 한다."""
        from clossify import qa_agents

        result = qa_agents._compliance_code_check(
            "테스트", {"category_id": "50021299"}, api_payload="not-a-dict"
        )
        violations = result.get("violations") or []
        image_rules = [v for v in violations if str(v.get("rule") or "") == "원본 이미지"]
        assert (
            len(image_rules) == 0
        ), f"api_payload 가 dict 가 아닌데 '원본 이미지' 위반이 나옴: {image_rules}"


# --------------------------------------------------------------------------- #
# (h) 엔드투엔드 회귀 가드: prepare_listing 을 유효 이미지 1장으로 실행했을 때
# compliance 위반 목록에 rule='원본 이미지' 가 없어야 한다.
# --------------------------------------------------------------------------- #
class TestPrepareListingNoImageOverblock:
    """prepare_listing 경로에서 '원본 이미지' 과잉 차단이 없어야 한다."""

    def test_prepare_listing_has_no_original_image_violation(self, tmp_path, monkeypatch):
        """유효 이미지 1장으로 prepare_listing 실행 시 '원본 이미지' 위반 없음."""
        from clossify import common, register

        # prepared 디렉터리를 임시 경로로 격리.
        monkeypatch.setattr(common, "PREPARED_DIR", str(tmp_path))

        # attach_fn 주입: 정상 이미지 1장이 업로드된 상황을 흉내낸다.
        valid_url = "https://cdn.example.com/photo.jpg"

        def _fake_attach(sources):
            return {"urls": [valid_url], "rejected": []}

        payload = register.prepare_listing(
            {
                "name": "테스트니트",
                "salePrice": 30000,
                "image_sources": [valid_url],
                "categoryId": _CLOTHING_CATEGORY,
                "notice": _WEAR_NOTICE_COMPLETE,
            },
            attach_fn=_fake_attach,
        )

        # QA 집계 결과에서 '원본 이미지' rule 위반이 없어야 한다.
        qa = payload.get("qa") or {}
        all_violations = qa.get("violations") or []
        image_rules = [v for v in all_violations if str(v.get("rule") or "") == "원본 이미지"]
        assert (
            len(image_rules) == 0
        ), f"prepare_listing 에서 '원본 이미지' 위반이 나옴(과잉 차단): {image_rules}"
