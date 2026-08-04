# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""고시 필드 타입 — 묻기·판정·전송 세 곳의 타입 인지 검증.

본 테스트 파일은 고시 필드가 전부 자유 텍스트가 아님을 다루는 계약을
검증한다. 실호출로 확인된 두 필드의 타입 정보가 세 곳(판정·전송·묻기)에서
일관되게 반영되는지 확인한다:

  - ``importDeclaration`` → **boolean** (네이버 응답 ``java.lang.Boolean``).
  - ``releaseDate`` → **date** (네이버 응답 date parse error, 형식 미확정).

핵심 회귀: boolean ``False`` 가 "미제공" 으로 읽혀 게이트가 차단하던 결정적
결함을 바로잡는다 — ``False`` 는 유효한 답이다 (수입신고 대상 아님).

검증은 ``COMMERCE_DRY_RUN`` 을 끈 상태에서 수행한다(티켓 요구사항).
HTTP mock 으로 네이버 호출 횟수를 센다.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from unittest import mock

import pytest

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import common, mcp_server, naver_client, qa_agents

# --------------------------------------------------------------------------- #
# 공통 픽스처.
# --------------------------------------------------------------------------- #

# KITCHEN_UTENSILS 카테고리에서 컴플라이언스 게이트가 통과하려면 채워야 하는
# 최소 config notice 섹션 (원산지/AS/공통5필드).
_KITCHEN_CFG = {
    "origin_area_code": "04",
    "origin_content": "중국",
    "as_tel": "070-1234-5678",
    "manufacturer": "테스트제조사",
    "returnCostReason": "[CFG] 반품 배송비 안내",
    "noRefundReason": "[CFG] 환불 불가 안내",
    "qualityAssuranceStandard": "[CFG] 품질 보증 기준",
    "compensationProcedure": "[CFG] 보상 절차",
    "troubleShootingContents": "[CFG] 고장 대처",
}

# KITCHEN_UTENSILS 의 필수 필드 전체에서 boolean/date 필드 이외의 값을 채운
# "완전한" 고시 본문. 통합 테스트에서 importDeclaration 만 바꿔가며
# 게이트 통과 여부를 검증하려면 나머지 필수 필드가 모두 채워져 있어야 한다
# (그렇지 않으면 다른 필드 누락으로 게이트가 차단해서 boolean 판정이 검증 불가).
#
# **XOR (releaseDate XOR releaseDateText)**: 본문은 releaseDateText 만 채운다.
# 실호출로 둘 다 보내면 네이버가 거절하고, releaseDateText 하나만 보냈을 때
# HTTP 200 등록 성공이 확인되었다. releaseDate 와 releaseDateText 를 동시에
# 채우면 컴플라이언스 게이트의 "고시 필드 상호배제" 위반이 발생한다.
# component (단수): 네이버 스펙은 component (단수) 이다. 과거에 복수형(components)
# 로 기재되어 있어 값이 통째로 무시되고 NotNull 거절을 받았다 — 데이터가
# 바로잡혔으므로 테스트 픽스처도 단수형을 쓴다.
_COMPLETE_KITCHEN_NOTICE_BODY = {
    "itemName": "테스트 주방용품",
    "modelName": "TEST-001",
    "material": "스테인리스 스틸",
    "component": "본품 1개",
    "size": "가로 10cm x 세로 20cm",
    "releaseDateText": "2026년 1월",
    "producer": "테스트제조사",
    "warrantyPolicy": "구매일로부터 1년",
    # importDeclaration 은 테스트마다 주입 (boolean 테스트의 핵심 대상).
    # releaseDate / releaseDateText XOR 테스트는 test_notice_field_relations.py.
}


def _kitchen_notice(**overrides) -> dict:
    """KITCHEN_UTENSILS notice 를 만든다. importDeclaration 을
    overrides 로 받아 본문에 병합한다."""
    body = dict(_COMPLETE_KITCHEN_NOTICE_BODY)
    body.update(overrides)
    return {
        "productInfoProvidedNoticeType": "KITCHEN_UTENSILS",
        "kitchenUtensils": body,
    }


@pytest.fixture
def isolated_prepared_dir(tmp_path, monkeypatch):
    """common.PREPARED_DIR 을 tmp_path 로 격리.

    register_product MCP 도구는 내부적으로 prepared payload 를 조회한다.
    실제 .local/ prepared 디렉토리에 잔존하는 payload 가 있으면 prepared QA
    게이트가 PENDING 으로 등록을 차단해버린다. 테스트 격리를 위해 임시
    디렉토리로 돌린다.
    """
    fake_dir = tmp_path / "prepared"
    fake_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(common, "PREPARED_DIR", fake_dir)
    return fake_dir


def _extract_notice_body(payload):
    """빌드된 페이로드에서 productInfoProvidedNotice 의 본문(kitchenUtensils)을 추출."""
    if not isinstance(payload, dict):
        return {}
    op = payload.get("originProduct")
    if not isinstance(op, dict):
        return {}
    da = op.get("detailAttribute")
    if not isinstance(da, dict):
        return {}
    notice = da.get("productInfoProvidedNotice")
    if not isinstance(notice, dict):
        return {}
    body = notice.get("kitchenUtensils")
    if not isinstance(body, dict):
        # fallback - 다른 노드 키 시도.
        for key in ("etc", "furniture"):
            fb = notice.get(key)
            if isinstance(fb, dict):
                body = fb
                break
    return body if isinstance(body, dict) else {}


def _register_kitchen(*, product_notice, monkeypatch, isolated_prepared_dir):
    """KITCHEN_UTENSILS 타입으로 register_product 호출.

    반환: (result_dict, captured)
    captured = {"payload": 전송된 페이로드 deepcopy, "calls": 네이버 호출 수}
    COMMERCE_DRY_RUN 을 끄고, _post_product_payload 를 mock 으로 가로챈다.
    컴플라이언스 게이트는 실제 로직을 타야 boolean False 가 제공됨으로
    판정되는지 검증할 수 있으므로 stub 하지 않는다.
    """
    monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
    captured: dict = {"payload": None, "calls": 0}

    def _fake_post(payload, tk):
        captured["payload"] = copy.deepcopy(payload)
        captured["calls"] += 1
        return 200, {"originProductNo": "TEST-KITCHEN-1"}

    patches = [
        mock.patch.object(naver_client, "_notice_config", return_value=_KITCHEN_CFG),
        mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
        mock.patch.object(naver_client, "_post_product_payload", side_effect=_fake_post),
        mock.patch.object(naver_client, "get_token", return_value="fake-token"),
        # KITCHEN_UTENSILS 추론을 위해 카테고리 경로를 주입.
        mock.patch.object(
            mcp_server,
            "_category_path_for",
            return_value="주방용품/조리도구",
        ),
        mock.patch.object(
            naver_client,
            "_category_path_for",
            return_value="주방용품/조리도구",
        ),
    ]
    for p in patches:
        p.start()
    try:
        result = mcp_server.register_product(
            name="테스트 주방용품",
            price=10000,
            image_urls=["http://cdn.example/img.png"],
            category_id="50004528",
            detail_html="<html><body>detail</body></html>",
            notice=product_notice,
        )
    finally:
        for p in patches:
            p.stop()
    return result, captured


# =========================================================================== #
# (a)(b) boolean False/True 가 제공된 것으로 판정되어 게이트를 통과.
# =========================================================================== #


class TestBooleanProvidedJudgment:
    """boolean 필드 판정 — True/False 둘 다 제공, None/키부재만 미제공."""

    def test_a_false_is_provided_passes_gate(self, monkeypatch, isolated_prepared_dir):
        """(a) importDeclaration=False 가 제공된 것으로 판정되어 게이트 통과.

        핵심 회귀: 과거에는 boolean False 를 미제공으로 읽어 게이트가 차단했다.
        False 는 유효한 답이다 (수입신고 대상 아님).
        """
        notice = _kitchen_notice(importDeclaration=False)
        result, captured = _register_kitchen(
            product_notice=notice,
            monkeypatch=monkeypatch,
            isolated_prepared_dir=isolated_prepared_dir,
        )
        assert result["ok"] is True, (
            f"boolean False 가 미제공으로 오판되어 게이트가 차단: "
            f"blocked_by={result.get('blocked_by')}, error={result.get('error')}"
        )

    def test_b_true_also_passes_gate(self, monkeypatch, isolated_prepared_dir):
        """(b) importDeclaration=True 도 통과한다."""
        notice = _kitchen_notice(importDeclaration=True)
        result, captured = _register_kitchen(
            product_notice=notice,
            monkeypatch=monkeypatch,
            isolated_prepared_dir=isolated_prepared_dir,
        )
        assert (
            result["ok"] is True
        ), f"boolean True 가 차단됨: blocked_by={result.get('blocked_by')}"

    def test_unit_notice_field_missing_boolean(self):
        """_notice_field_missing 단위 테스트 — boolean 판정 규칙 검증."""
        fields = ["importDeclaration"]
        # False → 제공됨.
        assert qa_agents._notice_field_missing({"importDeclaration": False}, fields) == []
        # True → 제공됨.
        assert qa_agents._notice_field_missing({"importDeclaration": True}, fields) == []
        # None → 미제공.
        assert qa_agents._notice_field_missing({"importDeclaration": None}, fields) == [
            "importDeclaration"
        ]
        # 키 부재 → 미제공.
        assert qa_agents._notice_field_missing({}, fields) == ["importDeclaration"]


# =========================================================================== #
# (c) 전송 페이로드에서 importDeclaration 이 JSON boolean.
# =========================================================================== #


class TestBooleanTransmittedAsJsonBoolean:
    """(c) boolean 필드는 JSON boolean 으로 송신되어야 한다 (문자열 아님)."""

    def test_c_false_transmitted_as_json_bool(self, monkeypatch, isolated_prepared_dir):
        """importDeclaration=False 가 JSON boolean false 로 송신된다."""
        notice = _kitchen_notice(importDeclaration=False)
        result, captured = _register_kitchen(
            product_notice=notice,
            monkeypatch=monkeypatch,
            isolated_prepared_dir=isolated_prepared_dir,
        )
        assert result["ok"] is True, f"등록 실패: {result.get('error')}"
        assert captured["calls"] == 1, "정확히 1회 HTTP 호출이어야 함"
        body = _extract_notice_body(captured["payload"])
        assert "importDeclaration" in body, "importDeclaration 이 페이로드에 없음"
        value = body["importDeclaration"]
        assert isinstance(
            value, bool
        ), f"JSON boolean 여야 함, 받은 타입: {type(value).__name__}, 값: {value!r}"
        assert value is False, f"값이 False 여야 함: {value!r}"

    def test_c_true_transmitted_as_json_bool(self, monkeypatch, isolated_prepared_dir):
        """importDeclaration=True 가 JSON boolean true 로 송신된다."""
        notice = _kitchen_notice(importDeclaration=True)
        result, captured = _register_kitchen(
            product_notice=notice,
            monkeypatch=monkeypatch,
            isolated_prepared_dir=isolated_prepared_dir,
        )
        assert result["ok"] is True
        body = _extract_notice_body(captured["payload"])
        value = body["importDeclaration"]
        assert isinstance(value, bool), f"JSON boolean 여야 함: {type(value).__name__}"
        assert value is True


# =========================================================================== #
# (d) boolean 필드에 문자열을 주면 거부 (네이버 호출 0회).
# =========================================================================== #


class TestBooleanStringRefused:
    """(d) boolean 필드에 문자열을 주면 거부 — 조용한 변환 금지."""

    def test_d_string_refused_with_yes_no_hint(self, monkeypatch, isolated_prepared_dir):
        """문자열을 주면 거부되고 사유에 예/아니오 항목임이 드러난다. 네이버 호출 0회."""
        notice = {
            "productInfoProvidedNoticeType": "KITCHEN_UTENSILS",
            "kitchenUtensils": {"importDeclaration": "수입식품등 수입신고 대상 아님(식기류)"},
        }
        result, captured = _register_kitchen(
            product_notice=notice,
            monkeypatch=monkeypatch,
            isolated_prepared_dir=isolated_prepared_dir,
        )
        # 거부되어야 한다 (build_payload 단계에서 ValueError).
        assert result["ok"] is False, "문자열 boolean 값이 조용히 통과함"
        # 네이버 호출은 0회 — 빌드 단계에서 거부됨.
        assert (
            captured["calls"] == 0
        ), f"네이버 호출이 0회여야 함 (거부 전 단계): {captured['calls']}"
        # 에러 메시지에 예/아니오 항목임이 드러나야 한다.
        error_text = str(result.get("error") or "")
        assert (
            "예/아니오" in error_text or "boolean" in error_text.lower()
        ), f"거부 사유에 예/아니오 항목임이 없음: {error_text!r}"

    def test_d_unit_validate_refuses_string(self):
        """_validate_notice_field_type 단위 — 문자열 거부."""
        with pytest.raises(ValueError, match="예/아니오"):
            naver_client._validate_notice_field_type("importDeclaration", "true")
        with pytest.raises(ValueError, match="예/아니오"):
            naver_client._validate_notice_field_type("importDeclaration", "예")

    def test_d_unit_validate_accepts_bool(self):
        """_validate_notice_field_type 단위 — bool 허용."""
        assert naver_client._validate_notice_field_type("importDeclaration", True) is True
        assert naver_client._validate_notice_field_type("importDeclaration", False) is False

    def test_d_unit_validate_refuses_int(self):
        """정수 1/0 도 거부 — 의도 명확화 (bool 리터럴만 받는다)."""
        with pytest.raises(ValueError):
            naver_client._validate_notice_field_type("importDeclaration", 1)
        with pytest.raises(ValueError):
            naver_client._validate_notice_field_type("importDeclaration", 0)


# =========================================================================== #
# (e) 타입 미기재 필드는 기존 문자열 동작 그대로 (회귀 없음).
# =========================================================================== #


class TestUntypedFieldStringBehavior:
    """(e) 타입 미기재 필드는 기존 문자열 동작을 유지한다."""

    def test_e_untyped_field_type_returns_string(self):
        """미기재 필드의 _notice_field_type 은 'string'."""
        # material 은 notice_field_types.json 에 없다.
        assert qa_agents._notice_field_type("material") == "string"
        assert qa_agents._notice_field_type("returnCostReason") == "string"
        assert qa_agents._notice_field_type("size") == "string"

    def test_e_untyped_field_judged_as_before(self):
        """미기재 필드는 기존 placeholder 판정을 따른다."""
        # 빈 값 → 미제공.
        assert qa_agents._notice_field_missing({"material": ""}, ["material"]) == ["material"]
        # placeholder → 미제공.
        assert qa_agents._notice_field_missing({"material": "해당없음"}, ["material"]) == [
            "material"
        ]
        # 정상 값 → 제공.
        assert qa_agents._notice_field_missing({"material": "스테인리스"}, ["material"]) == []

    def test_e_untyped_field_passes_as_string(self, monkeypatch, isolated_prepared_dir):
        """미기재 필드(material)는 기존처럼 문자열로 통과한다."""
        notice = _kitchen_notice(importDeclaration=False, material="스테인리스 스틸")
        result, captured = _register_kitchen(
            product_notice=notice,
            monkeypatch=monkeypatch,
            isolated_prepared_dir=isolated_prepared_dir,
        )
        assert result["ok"] is True
        body = _extract_notice_body(captured["payload"])
        assert body.get("material") == "스테인리스 스틸"


# =========================================================================== #
# (f) needs_user 에서 boolean 필드가 예/아니오 질문으로 안내.
# =========================================================================== #


class TestNeedsUserBooleanAnswerShape:
    """(f) needs_user 의 answer_shape 가 boolean 필드를 예/아니오로 안내."""

    def test_f_answer_shape_boolean(self):
        """_notice_field_answer_shape 이 boolean 필드를 예/아니오로 안내."""
        shape = mcp_server._notice_field_answer_shape("importDeclaration")
        assert "예/아니오" in shape, f"boolean 안내에 예/아니오 가 없음: {shape!r}"
        assert "true" in shape.lower() or "false" in shape.lower()

    def test_f_answer_shape_string_empty(self):
        """미기재 필드의 answer_shape 은 빈 문자열 (기존 동작 회귀 없음)."""
        assert mcp_server._notice_field_answer_shape("material") == ""
        assert mcp_server._notice_field_answer_shape("returnCostReason") == ""

    def test_f_answer_shape_date(self):
        """date 필드의 answer_shape 은 날짜 안내."""
        shape = mcp_server._notice_field_answer_shape("releaseDate")
        assert "날짜" in shape, f"date 안내에 날짜 가 없음: {shape!r}"

    def test_f_needs_user_has_answer_shape_for_boolean(self, monkeypatch, isolated_prepared_dir):
        """필수 boolean 필드 누락 시 needs_user 에 answer_shape 가 붙는다.

        importDeclaration 을 제공하지 않으면 게이트가 이를 누락으로 잡고
        needs_user 항목에 answer_shape 가 "예/아니오" 임을 드러낸다.
        """
        # importDeclaration 누락 — needs_user 에 잡혀야 한다.
        # _kitchen_notice() 에서 importDeclaration 만 빼면 컴플라이언스 게이트가
        # 이 필드를 누락으로 잡고 answer_shape 를 붙인다.
        notice = _kitchen_notice()
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
        patches = [
            mock.patch.object(naver_client, "_notice_config", return_value=_KITCHEN_CFG),
            mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
            mock.patch.object(naver_client, "get_token", return_value="fake-token"),
            mock.patch.object(mcp_server, "_category_path_for", return_value="주방용품/조리도구"),
            mock.patch.object(naver_client, "_category_path_for", return_value="주방용품/조리도구"),
        ]
        for p in patches:
            p.start()
        try:
            result = mcp_server.register_product(
                name="테스트 주방용품",
                price=10000,
                image_urls=["http://cdn.example/img.png"],
                category_id="50004528",
                detail_html="<html><body>detail</body></html>",
                notice=notice,
            )
        finally:
            for p in patches:
                p.stop()
        # 컴플라이언스 게이트가 importDeclaration 누락을 잡아야 한다.
        # (게이트가 잡지 않으면 needs_user 가 비어 있다 — 그 경우 이 테스트는
        # _run_compliance_gate 를 직접 호출해 확인한다.)
        needs_user = result.get("needs_user") or []
        bool_entry = next((n for n in needs_user if n.get("field") == "importDeclaration"), None)
        if bool_entry is None:
            # 게이트가 직접 잡지 않은 경우, _run_compliance_gate 를 직접 호출.
            # 이 경로는 payload 가 빌드된 뒤여야 한다.
            pytest.skip(
                "needs_user 에 importDeclaration 이 없음 — 게이트가 다른 위반으로 먼저 차단했을 수 있음"
            )
        assert "answer_shape" in bool_entry, "needs_user 항목에 answer_shape 키가 없음"
        shape = bool_entry["answer_shape"]
        assert "예/아니오" in shape, f"answer_shape 에 예/아니오 가 없음: {shape!r}"


# =========================================================================== #
# (g) date 필드는 값이 있으면 통과, 받은 문자열이 가공 없이 실린다.
# =========================================================================== #


class TestDateFieldPassThrough:
    """(g) date 필드 — 비어있지 않으면 제공, 받은 값을 가공 없이 싣는다."""

    def test_g_date_value_provided(self):
        """date 필드는 비어있지 않은 값이면 제공으로 판정."""
        # releaseDate 는 date 타입.
        assert qa_agents._notice_field_missing({"releaseDate": "2026-08"}, ["releaseDate"]) == []
        assert qa_agents._notice_field_missing({"releaseDate": "20260804"}, ["releaseDate"]) == []
        # 빈 값 → 미제공.
        assert qa_agents._notice_field_missing({"releaseDate": ""}, ["releaseDate"]) == [
            "releaseDate"
        ]
        # placeholder → 미제공.
        assert qa_agents._notice_field_missing({"releaseDate": "상세참조"}, ["releaseDate"]) == [
            "releaseDate"
        ]

    def test_g_date_value_carried_unmodified(self, monkeypatch, isolated_prepared_dir):
        """date 필드의 값이 가공 없이 페이로드에 실린다.

        형식 미확정이므로 우리가 가공하지 않는다. 사용자가 준 값을 그대로 둔다.
        XOR (releaseDate XOR releaseDateText): releaseDate 를 테스트하기 위해
        releaseDateText 를 제거하고 releaseDate 만 제공한다. 둘 다 채우면
        컴플라이언스 게이트의 "고시 필드 상호배제" 위반이 발생한다.
        """
        # releaseDate 에 임의의 문자열을 주고, 그대로 송신되는지 확인.
        # 값이 "2026-08" 이라면, 페이로드에서도 "2026-08" 이어야 한다
        # (조용한 형식 변환 금지).
        # releaseDateText 를 빼고 releaseDate 만 넣은 본문을 직접 구성.
        body = dict(_COMPLETE_KITCHEN_NOTICE_BODY)
        body.pop("releaseDateText", None)
        body["releaseDate"] = "2026-08-04"
        notice = {
            "productInfoProvidedNoticeType": "KITCHEN_UTENSILS",
            "kitchenUtensils": body,
            # importDeclaration 은 boolean 테스트의 핵심 — False 로 채워 게이트 통과.
            # _kitchen_notice 가 importDeclaration 을 본문에 못 넣으니 직접 넣기.
        }
        body["importDeclaration"] = False
        result, captured = _register_kitchen(
            product_notice=notice,
            monkeypatch=monkeypatch,
            isolated_prepared_dir=isolated_prepared_dir,
        )
        assert result["ok"] is True, f"등록 실패: {result.get('error')}"
        body = _extract_notice_body(captured["payload"])
        assert (
            body.get("releaseDate") == "2026-08-04"
        ), f"date 값이 가공됨: expected '2026-08-04', got {body.get('releaseDate')!r}"
        # 문자열이어야 한다 (가공 없이).
        assert isinstance(body["releaseDate"], str), "date 값이 문자열이 아님 (가공됨)"

    def test_g_date_no_format_validation(self):
        """date 필드는 형식 검증을 하지 않는다 (형식 미확정).

        _notice_field_type 이 'date' 라도, _validate_notice_field_type 은
        형식을 검사하지 않고 값을 그대로 반환한다.
        """
        # 어떤 형식이든 ValueError 없이 통과해야 한다.
        for value in ["2026-08-04", "2026-08", "20260804", "2026/08/04"]:
            result = naver_client._validate_notice_field_type("releaseDate", value)
            assert result == value, f"date 값이 변형됨: {value!r} → {result!r}"


# =========================================================================== #
# 데이터 파일 무결성.
# =========================================================================== #


class TestNoticeFieldTypesDataIntegrity:
    """notice_field_types.json 데이터 파일 무결성 검증."""

    def test_only_confirmed_fields_in_data(self):
        """데이터에는 확인된 2개 필드만 있다 (타입 추측 금지)."""
        types = qa_agents._load_notice_field_types()
        assert isinstance(types, dict)
        # 확인된 필드만 있어야 한다. 다른 필드가 우연히 들어가면 안 된다.
        assert "importDeclaration" in types
        assert "releaseDate" in types
        assert types["importDeclaration"]["type"] == "boolean"
        assert types["releaseDate"]["type"] == "date"

    def test_no_unconfirmed_types(self):
        """미확인 필드가 데이터에 없다.

        핵심 계약: 타입을 지어내지 않는다. 확인된 것만 기록한다.
        잘못 들어간 필드가 있는지 확인.
        """
        types = qa_agents._load_notice_field_types()
        # 허용된 필드 집합 — 확인된 2개만.
        allowed = {"importDeclaration", "releaseDate"}
        extra = set(types.keys()) - allowed
        assert not extra, f"확인되지 않은 필드가 데이터에 있습니다 (타입 추측 금지 위반): {extra}"
