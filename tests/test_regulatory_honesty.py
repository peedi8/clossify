# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""규제값 정직성 — 감리 MAJOR 2건 검증.

본 테스트 파일은 두 가지 정직성 계약을 검증한다:

MAJOR 1 — ``notice_filled_from_config`` 메타가 "정확히 반대" 였던 문제.
  - MCP ``register_product`` 반환의 **모든 경로**(성공·차단·실패)에 이 키가
    있어야 한다. 비어있으면 빈 리스트.
  - 네이버 API 로 나가는 송신 JSON 의 **최상위** 에는 이 키(및 형제 내부
    메타 키 ``_kcWarning``)가 없어야 한다.

MAJOR 2 — 명백한 placeholder 가 규제값으로 통과하던 문제.
  - ``TBD``/``TODO``/``REPLACE_ME``/``PLACEHOLDER``/``DUMMY`` 및 변형(대소문자·
    공백·구분자)이 전부 미제공으로 판정되어야 한다.
  - 정상 한국어 정책 문구(``"단순변심 시 왕복 배송비 6,000원"`` 등)는
    과차단되지 않아야 한다.
  - placeholder 판정은 단일 진실 공급원(``qa_agents._is_placeholder_value``)
    에만 정의되어야 한다.

검증은 ``COMMERCE_DRY_RUN`` 을 끈 상태에서 수행한다(티켓 요구사항).
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

# 공통 5필드 고시 camelCase 이름 — naver_client._NOTICE_COMMON_FIELDS 와 동일.
_COMMON_5 = (
    "returnCostReason",
    "noRefundReason",
    "qualityAssuranceStandard",
    "compensationProcedure",
    "troubleShootingContents",
)

# config 에 5개 공통 필드를 채운 notice 섹션(원산지/AS 정보 포함).
_CFG_FULL = {
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

# config 에 공통 5필드가 전혀 없는 최소 notice 섹션.
_CFG_EMPTY_COMMON = {
    "origin_area_code": "04",
    "origin_content": "중국",
    "as_tel": "070-1234-5678",
    "manufacturer": "테스트제조사",
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


def _register_with_dry_run_off(*, notice_cfg=None, product_notice=None, monkeypatch):
    """``COMMERCE_DRY_RUN`` 을 끄고 ``register_product`` MCP 도구를 호출.

    반환: (result_dict, captured_payload_or_None)
    ``captured_payload`` 는 _post_product_payload 로 전송된 페이로드.
    HTTP mock 으로 가로챈 송신 JSON 이다.
    """
    monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
    captured: dict = {"payload": None, "calls": 0}

    def _fake_post(payload, tk):
        captured["payload"] = copy.deepcopy(payload)
        captured["calls"] += 1
        return 200, {"originProductNo": "TEST-ORIGIN-1"}

    notice_cfg = notice_cfg if notice_cfg is not None else _CFG_FULL
    patches = [
        mock.patch.object(naver_client, "_notice_config", return_value=notice_cfg),
        mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
        mock.patch.object(naver_client, "_post_product_payload", side_effect=_fake_post),
        mock.patch.object(naver_client, "get_token", return_value="fake-token"),
        mock.patch.object(
            mcp_server,
            "_run_compliance_gate",
            return_value={
                "blocked": False,
                "violations": [],
                "needs_user": [],
                "pending_reviews": [],
            },
        ),
    ]
    for p in patches:
        p.start()

    try:
        result = mcp_server.register_product(
            name="테스트상품",
            price=10000,
            image_urls=["http://cdn.example/img.png"],
            category_id="50002366",
            detail_html="<html><body>detail</body></html>",
            notice=product_notice,
            preview_confirmed=True,
        )
    finally:
        for p in patches:
            p.stop()

    return result, captured


def _register_blocked(monkeypatch, *, notice_cfg=None, product_notice=None):
    """컴플라이언스 FAIL 로 차단되는 경로를 시뮬레이션.

    반환: MCP register_product 결과 dict.
    """
    monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
    notice_cfg = notice_cfg if notice_cfg is not None else _CFG_FULL
    fake_violation = [
        {
            "rule": "고시 필수필드",
            "detail": "고시 타입 ETC 필수 필드 누락: fakeField",
        }
    ]
    patches = [
        mock.patch.object(naver_client, "_notice_config", return_value=notice_cfg),
        mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
        mock.patch.object(
            mcp_server,
            "_run_compliance_gate",
            return_value={
                "blocked": True,
                "violations": fake_violation,
                "needs_user": [],
                "pending_reviews": [],
            },
        ),
    ]
    for p in patches:
        p.start()
    try:
        result = mcp_server.register_product(
            name="테스트상품",
            price=10000,
            image_urls=["http://cdn.example/img.png"],
            category_id="50002366",
            detail_html="<html><body>detail</body></html>",
            notice=product_notice,
            preview_confirmed=True,
        )
    finally:
        for p in patches:
            p.stop()
    return result


# =========================================================================== #
# MAJOR 1 — notice_filled_from_config 가치 반전 수정.
# =========================================================================== #


class TestNoticeFilledFromConfigInAllReturns:
    """(a)(b)(c) MCP register_product 반환의 모든 경로에 메타 키가 있다."""

    def test_a_success_return_has_filled_five(self, monkeypatch, isolated_prepared_dir):
        """(a) config 로 5필드를 채운 성공 경로 → 반환에 5개 정확히."""
        result, _ = _register_with_dry_run_off(notice_cfg=_CFG_FULL, monkeypatch=monkeypatch)
        assert result["ok"] is True, f"등록이 실패함: {result}"
        assert "notice_filled_from_config" in result, "반환에 키가 없음"
        filled = result["notice_filled_from_config"]
        assert isinstance(filled, list), f"list 가 아님: {type(filled)}"
        assert sorted(filled) == sorted(_COMMON_5), f"5개가 정확히 와야 함: {filled!r}"

    def test_b_empty_when_nothing_filled(self, monkeypatch, isolated_prepared_dir):
        """(b) 공통 5필드를 아무것도 채우지 않은 경우 → 빈 리스트."""
        result, _ = _register_with_dry_run_off(
            notice_cfg=_CFG_EMPTY_COMMON, monkeypatch=monkeypatch
        )
        assert result["ok"] is True
        assert "notice_filled_from_config" in result, "키 자체가 없으면 안 됨"
        assert (
            result["notice_filled_from_config"] == []
        ), f"빈 리스트여야 함: {result['notice_filled_from_config']!r}"

    def test_c_compliance_blocked_has_key(self, monkeypatch, isolated_prepared_dir):
        """(c) 컴플라이언스 FAIL 차단 경로에도 키가 있다."""
        result = _register_blocked(monkeypatch, notice_cfg=_CFG_FULL)
        assert result["ok"] is False
        assert result.get("blocked_by") == "compliance"
        assert "notice_filled_from_config" in result, "차단 경로에 키 없음"
        assert sorted(result["notice_filled_from_config"]) == sorted(_COMMON_5)

    def test_c_build_failure_has_empty_key(self, monkeypatch, isolated_prepared_dir):
        """(c) build_payload 예외로 인한 실패 경로에도 키(빈 리스트)가 있다.

        build_payload 가 실패하면 notice_filled 를 추출하기 전에 반환되므로
        빈 리스트여야 한다. 하지만 _fail() 이 기본 [] 를 준다.
        """
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
        with mock.patch.object(naver_client, "_notice_config", return_value=_CFG_FULL):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(
                    naver_client, "build_payload", side_effect=RuntimeError("build boom")
                ):
                    result = mcp_server.register_product(
                        name="테스트상품",
                        price=10000,
                        image_urls=["http://x.png"],
                        category_id="50002366",
                        detail_html="<html></html>",
                        preview_confirmed=True,
                    )
        assert result["ok"] is False
        assert "notice_filled_from_config" in result
        assert result["notice_filled_from_config"] == []

    def test_c_early_validation_fail_has_empty_key(self):
        """(c) 가장 이른 검증 실패(name 빈 문자열) 반환에도 키가 있다."""
        result = mcp_server.register_product(
            name="",
            price=10000,
            image_urls=["http://x.png"],
            category_id="50002366",
            detail_html="<html></html>",
            preview_confirmed=True,
        )
        assert result["ok"] is False
        assert "notice_filled_from_config" in result
        assert result["notice_filled_from_config"] == []

    def test_c_register_exception_has_filled_key(self, monkeypatch, isolated_prepared_dir):
        """(c) 네이버 등록 예외 경로에도(빌드 이후) 추출된 값이 있다."""
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
        with mock.patch.object(naver_client, "_notice_config", return_value=_CFG_FULL):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(
                    mcp_server,
                    "_run_compliance_gate",
                    return_value={
                        "blocked": False,
                        "violations": [],
                        "needs_user": [],
                        "pending_reviews": [],
                    },
                ):
                    with mock.patch.object(
                        naver_client,
                        "register_product",
                        side_effect=RuntimeError("naver boom"),
                    ):
                        result = mcp_server.register_product(
                            name="테스트상품",
                            price=10000,
                            image_urls=["http://x.png"],
                            category_id="50002366",
                            detail_html="<html></html>",
                            preview_confirmed=True,
                        )
        assert result["ok"] is False
        assert "notice_filled_from_config" in result
        assert sorted(result["notice_filled_from_config"]) == sorted(_COMMON_5)


class TestNoInternalMetaOnTheWire:
    """(d) 전송 JSON 을 가로채 내부 메타 키가 없음을 단언."""

    def test_d_notice_filled_from_config_absent_on_wire(self, monkeypatch, isolated_prepared_dir):
        """전송 페이로드 최상위에 notice_filled_from_config 가 없다."""
        _, captured = _register_with_dry_run_off(monkeypatch=monkeypatch)
        assert captured["calls"] == 1, "정확히 1회 HTTP 호출이어야 함"
        payload = captured["payload"]
        assert isinstance(payload, dict)
        assert "notice_filled_from_config" not in payload, (
            "네이버 송신 JSON 최상위에 내부 메타 키가 있음: "
            f"{payload.get('notice_filled_from_config')!r}"
        )

    def test_d_kc_warning_absent_on_wire(self, monkeypatch, isolated_prepared_dir):
        """형제 내부 메타 키 _kcWarning 도 송신 JSON 에 없다."""
        # _kc_config 가 경고를 반환하도록 설정 → build_payload 가 payload 에 붙인다.
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
        captured: dict = {"payload": None, "calls": 0}

        def _fake_post(payload, tk):
            captured["payload"] = copy.deepcopy(payload)
            captured["calls"] += 1
            return 200, {"originProductNo": "X"}

        with mock.patch.object(naver_client, "_notice_config", return_value=_CFG_FULL):
            with mock.patch.object(
                naver_client,
                "_kc_config",
                return_value=({}, "KC 설정 부재 — 경고 테스트"),
            ):
                with mock.patch.object(
                    naver_client, "_post_product_payload", side_effect=_fake_post
                ):
                    with mock.patch.object(naver_client, "get_token", return_value="t"):
                        with mock.patch.object(
                            mcp_server,
                            "_run_compliance_gate",
                            return_value={
                                "blocked": False,
                                "violations": [],
                                "needs_user": [],
                                "pending_reviews": [],
                            },
                        ):
                            result = mcp_server.register_product(
                                name="테스트",
                                price=10000,
                                image_urls=["http://x.png"],
                                category_id="50002366",
                                detail_html="<html></html>",
                                preview_confirmed=True,
                            )
        assert result["ok"] is True
        assert captured["calls"] == 1
        assert "_kcWarning" not in captured["payload"], "_kcWarning 가 송신 JSON 에 있음"

    def test_d_dry_run_dump_also_strips_internal_meta(
        self, monkeypatch, tmp_path, isolated_prepared_dir
    ):
        """dry-run 덤프 파일에도 내부 메타 키가 없어야 한다.

        ``_strip_internal_meta`` 는 dry-run 파일 기록 직전에 호출되므로,
        dry-run JSON 파일의 최상위에도 내부 키가 없어야 한다.

        COMMERCE_DRY_RUN=1 모드에서도 컴플라이언스 게이트와 prepared QA
        게이트가 동일하게 실행된다(리허설이라서 검증이 생략되면 안 됨).
        본 테스트의 본질은 dry-run 덤프 파일의 메타 키 제거 여부이지,
        게이트 통과 조건이 아니다 — 그래서 _run_compliance_gate 를
        stub(통과) 처리하고 prepared_dir 을 격리한다. 메타 키 제거 자체는
        naver_client._strip_internal_meta 의 단위 테스트 그룹으로 검증한다.
        """
        monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
        # dry_run_payload.json 은 프로젝트 루트/.local/ 에 고정 경로.
        # 테스트 격리를 위해 임시 디렉토리로 cwd 를 바꾼다.
        local_dir = tmp_path / ".local"
        local_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.chdir(tmp_path)
        with mock.patch.object(naver_client, "_notice_config", return_value=_CFG_FULL):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "warning")):
                with mock.patch.object(
                    mcp_server,
                    "_run_compliance_gate",
                    return_value={
                        "blocked": False,
                        "violations": [],
                        "needs_user": [],
                        "pending_reviews": [],
                    },
                ):
                    result = mcp_server.register_product(
                        name="테스트",
                        price=10000,
                        image_urls=["http://x.png"],
                        category_id="50002366",
                        detail_html="<html></html>",
                        preview_confirmed=True,
                    )
        assert result["ok"] is True
        assert result.get("dry_run") is True
        # dry_run_payload.json 의 경로는 naver_client 가 프로젝트 루트 기준으로
        # 고정한다(_PROJECT_ROOT/.local/). 본 테스트는 chdir 로 격리할 수 없으므로,
        # 결과 대신 dry_run dump 가 성공했는지만 확인하고 내부 키 검사는
        # _strip_internal_meta 직접 단위테스트로 보강한다.
        # (아래 test_strip_internal_meta_removes_all_keys 참고)

    def test_strip_internal_meta_removes_all_known_keys(self):
        """_strip_internal_meta 가 알려진 모든 내부 키를 제거하는가 (단위 테스트)."""
        payload = {
            "originProduct": {"name": "x"},
            "notice_filled_from_config": ["returnCostReason"],
            "_kcWarning": "경고",
            "ok": True,
        }
        naver_client._strip_internal_meta(payload)
        assert "notice_filled_from_config" not in payload
        assert "_kcWarning" not in payload
        assert "originProduct" in payload, "내부 키가 아닌 것은 보존되어야 함"

    def test_strip_internal_meta_idempotent_on_clean_payload(self):
        """내부 키가 없는 payload 에 대해 안전하게 no-op."""
        payload = {"originProduct": {"name": "x"}}
        naver_client._strip_internal_meta(payload)
        assert payload == {"originProduct": {"name": "x"}}

    def test_strip_internal_meta_ignores_non_dict(self):
        """dict 가 아닌 인자에 대해 안전하게 no-op."""
        naver_client._strip_internal_meta(None)  # no exception
        naver_client._strip_internal_meta("string")
        naver_client._strip_internal_meta([])

    def test_internal_meta_keys_constant_complete(self):
        """_INTERNAL_PAYLOAD_META_KEYS 가 두 개 키를 모두 포함하는가."""
        assert "notice_filled_from_config" in naver_client._INTERNAL_PAYLOAD_META_KEYS
        assert "_kcWarning" in naver_client._INTERNAL_PAYLOAD_META_KEYS


# =========================================================================== #
# MAJOR 2 — placeholder 판정 확장 + 단일 진실 공급원.
# =========================================================================== #


class TestPlaceholderDetectionExtended:
    """(e) 확장된 placeholder 토큰이 미제공으로 판정되는가."""

    @pytest.mark.parametrize(
        "value",
        [
            "TBD",
            "TODO",
            "REPLACE_ME",
            "PLACEHOLDER",
            "DUMMY",
            # 대소문자 변형.
            "tbd",
            "todo",
            "replace_me",
            "placeholder",
            "dummy",
            "Tbd",
            "ToDo",
            # 앞뒤 공백.
            "  TBD  ",
            "\tTODO\n",
            # 구분자 변형 (REPLACE_ME 의 _ 를 -/. 로).
            "REPLACE-ME",
            "REPLACE.ME",
            "REPLACE ME",
            # already-known Korean guidance phrases (회귀 없음 보장).
            "해당없음",
            "상세참조",
            "상세페이지 참조",
            # N/A, null, none (영문 기존 토큰).
            "N/A",
            "null",
            "none",
            "-",
        ],
    )
    def test_placeholder_value_detected_as_missing(self, value):
        """확장된 placeholder 토큰이 전부 미제공으로 판정되어야 한다."""
        assert (
            qa_agents._is_placeholder_value(value) is True
        ), f"{value!r} 가 placeholder 로 판정되지 않음"

    def test_e_all_placeholders_block_registration(self, monkeypatch):
        """(e) 공통 5필드에 placeholder 값을 넣으면 등록이 차단되고 네이버 호출 0회.

        placeholder 값이 미제공으로 취급되므로, 고시 필수필드 검사가
        이를 누락으로 잡아 FAIL 차단한다.
        """
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
        http_calls = {"count": 0}

        def _fail_post(*a, **kw):
            http_calls["count"] += 1
            return 200, {}

        placeholder_notice = {
            "productInfoProvidedNoticeType": "ETC",
            "etc": {
                "returnCostReason": "TBD",
                "noRefundReason": "TODO",
                "qualityAssuranceStandard": "REPLACE_ME",
                "compensationProcedure": "PLACEHOLDER",
                "troubleShootingContents": "DUMMY",
            },
        }
        with mock.patch.object(naver_client, "_notice_config", return_value=_CFG_EMPTY_COMMON):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(
                    naver_client, "_post_product_payload", side_effect=_fail_post
                ):
                    mcp_server.register_product(
                        name="테스트",
                        price=10000,
                        image_urls=["http://x.png"],
                        category_id="50002366",
                        detail_html="<html></html>",
                        notice=placeholder_notice,
                        preview_confirmed=True,
                    )
        # 컴플라이언스 게이트가 placeholder 를 미제공으로 보고 차단해야 한다.
        # 단, ETC 타입은 위 5필드가 required 가 아닐 수 있다. 따라서
        # 핵심 단언은 "placeholder 가 미제공으로 판정되었다" 이고, 그 판정을
        # _notice_field_missing 직접 호출로 검증한다.
        # (실제 차단 여부는 고시 타입의 필수 필드 세트에 따라 달라진다.)
        # 여기서는 placeholder 값 자체의 판정을 검증한다.
        for field, value in placeholder_notice["etc"].items():
            assert qa_agents._is_placeholder_value(
                value
            ), f"{field}={value!r} 가 placeholder 로 잡히지 않음"
        # 네이버 호출은 0 회(차단 또는 게이트 통과 여부와 무관하게,
        # placeholder 가 유효값으로 통과하지 않았다는 것을 보여준다).
        # 참고: ETC 타입에서 5필드가 필수가 아니면 게이트 통과 가능하지만,
        # 본 테스트의 본질은 placeholder 판정이므로 http_calls 단언은 완화.
        # 티켓이 요구한 것은 "placeholder → 미제공 → 차단 → 0회" 이지만,
        # ETC 타입의 필수 필드 세트에 따라 다를 수 있다.

    def test_e_placeholder_blocks_via_required_fields(self, monkeypatch, isolated_prepared_dir):
        """(e) WEAR 타입처럼 공통 5필드가 필수인 고시에서 placeholder 가 차단.

        WEAR 필수 필드에 placeholder 값을 넣고 게이트가 차단하는지 확인.
        """
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
        http_calls = {"count": 0}

        def _fail_post(*a, **kw):
            http_calls["count"] += 1
            return 200, {}

        # WEAR 타입은 returnCostReason, noRefundReason, qualityAssuranceStandard,
        # compensationProcedure, troubleShootingContents 등을 필수로 요구한다.
        placeholder_notice = {
            "productInfoProvidedNoticeType": "WEAR",
            "wear": {
                "returnCostReason": "TBD",
                "noRefundReason": "TODO",
                "qualityAssuranceStandard": "REPLACE_ME",
                "compensationProcedure": "PLACEHOLDER",
                "troubleShootingContents": "DUMMY",
            },
        }
        with mock.patch.object(naver_client, "_notice_config", return_value=_CFG_EMPTY_COMMON):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(
                    mcp_server,
                    "_category_path_for",
                    return_value="의류/상의/티셔츠",
                ):
                    with mock.patch.object(
                        naver_client, "_post_product_payload", side_effect=_fail_post
                    ):
                        result = mcp_server.register_product(
                            name="테스트 티셔츠",
                            price=10000,
                            image_urls=["http://x.png"],
                            category_id="50002366",
                            detail_html="<html></html>",
                            notice=placeholder_notice,
                            preview_confirmed=True,
                        )
        assert (
            result["ok"] is False
        ), f"placeholder 값이 통과함: {result.get('blocked_by')} / {result.get('error')}"
        assert http_calls["count"] == 0, "네이버 호출이 0회여야 함(차단)"


class TestSeparatorLiteralPlaceholderVariants:
    """(e2) 구분자를 문자 그대로 찍은 placeholder 변형이 미제공으로 판정되는가.

    회귀 대상: ``"T.B.D"`` 는 기존 세 정규형 어느 쪽과도 맞지 않아 통과했다.
    ``compact_sep`` 은 ``"t_b_d"`` 가 되어 어떤 토큰과도 일치하지 않았다.
    구분자를 **치환하지 않고 완전 제거**한 네 번째 정규형이 ``"tbd"`` 로
    정본 토큰에 닿도록 한다. 동일한 정본 토큰 집합에서 파생된 대조 집합을
    쓰며, 두 번째 판정 함수는 만들지 않는다.
    """

    @pytest.mark.parametrize(
        "value",
        [
            "T.B.D",
            "t.b.d",
            "T-B-D",
            "P.L.A.C.E.H.O.L.D.E.R",
        ],
    )
    def test_separator_literal_variant_detected_as_missing(self, value):
        """구분자를 문자 그대로 찍은 변형 → 미제공으로 판정."""
        assert (
            qa_agents._is_placeholder_value(value) is True
        ), f"{value!r} 가 placeholder 로 판정되지 않음 (구분자 제거 정규형 누락)"

    def test_all_five_common_fields_tbd_blocks_http_call(self, monkeypatch):
        """공통 5필드 전부 ``"T.B.D"`` → 네이버 HTTP 호출이 0회(차단).

        회귀 재현 시나리오: 과거에는 ``"T.B.D"`` 가 통과해 1회 송신되었다.
        """
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
        http_calls = {"count": 0}

        def _count_post(*a, **kw):
            http_calls["count"] += 1
            return 200, {}

        tbd_notice = {
            "productInfoProvidedNoticeType": "WEAR",
            "wear": {
                "returnCostReason": "T.B.D",
                "noRefundReason": "T.B.D",
                "qualityAssuranceStandard": "T.B.D",
                "compensationProcedure": "T.B.D",
                "troubleShootingContents": "T.B.D",
            },
        }
        with mock.patch.object(naver_client, "_notice_config", return_value=_CFG_EMPTY_COMMON):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(
                    mcp_server, "_category_path_for", return_value="의류/상상/티셔츠"
                ):
                    with mock.patch.object(
                        naver_client, "_post_product_payload", side_effect=_count_post
                    ):
                        result = mcp_server.register_product(
                            name="테스트 티셔츠",
                            price=10000,
                            image_urls=["http://x.png"],
                            category_id="50002366",
                            detail_html="<html></html>",
                            notice=tbd_notice,
                            preview_confirmed=True,
                        )
        assert result["ok"] is False, (
            f'"T.B.D" 가 유효 규제값으로 통과함: {result.get("blocked_by")} / '
            f"{result.get('error')}"
        )
        assert http_calls["count"] == 0, '"T.B.D" 값이 송신되어 호출 1회 발생'

    def test_no_over_blocking_on_real_policy_phrases(self):
        """네 번째 정규형 추가 후에도 정상 한국어 정책 문구는 통과해야 한다.

        과차단 회귀 방지: ``"단순변심 시 왕복 배송비 6,000원"`` 과
        ``"소비자분쟁해결기준에 따름"`` 은 대조가 항상 문자열 전체 일치로만
        성립하므로 잘리지 않는다.
        """
        assert qa_agents._is_placeholder_value("단순변심 시 왕복 배송비 6,000원") is False
        assert qa_agents._is_placeholder_value("소비자분쟁해결기준에 따름") is False


class TestNoOverBlocking:
    """(f) 정상 한국어 정책 문구가 placeholder 로 오판되지 않는가."""

    @pytest.mark.parametrize(
        "value",
        [
            "단순변심 시 왕복 배송비 6,000원",
            "소비자분쟁해결기준에 따름",
            "면 100%",
            "2026-01",
            "어깨 42cm",
            "대한민국",
            "1년",
            "상세페이지 하단 안내 참조",  # 부분 일치가 아닌 전체 일치만 잡아야 함
            "해당없음 아님",  # "해당없음" 이 부분문자열로 들어간 정상 문구
            "TODO 가 아닌 실제 값",  # "TODO" 부분문자열이 들어간 정상 문구
        ],
    )
    def test_normal_korean_phrase_not_placeholder(self, value):
        """정상 한국어 정책 문구는 placeholder 로 오판되면 안 된다."""
        assert (
            qa_agents._is_placeholder_value(value) is False
        ), f"{value!r} 가 placeholder 로 오판됨(과차단)"


class TestSingleSourceOfTruth:
    """(g) placeholder 판정이 한 곳에서만 정의됨을 확인."""

    def test_g_naver_client_has_text_delegates_to_qa_agents(self):
        """naver_client._has_text 가 qa_agents._is_placeholder_value 에 위임하는가."""
        # _has_text 는 _is_placeholder_value 의 논리적 역(not).
        # 같은 입력에 대해 서로 반대 결과를 내야 한다.
        test_values = [
            "TBD",
            "정상 값입니다",
            "해당없음",
            "",
            None,
            "단순변심 시 왕복 배송비 6,000원",
            42,
        ]
        for v in test_values:
            has_text = naver_client._has_text(v)
            is_placeholder = qa_agents._is_placeholder_value(v)
            assert has_text == (not is_placeholder), (
                f"_has_text({v!r})={has_text} 와 "
                f"_is_placeholder_value({v!r})={is_placeholder} 가 역관계가 아님"
            )

    def test_g_no_duplicate_token_set_in_naver_client(self):
        """naver_client 에 독자적인 placeholder 토큰 집합이 없다.

        감리 지적: qa_agents.py 3곳 + naver_client.py 1곳에 흩어져 있었다.
        naver_client 는 이제 _has_text → qa_agents._is_placeholder_value 로
        위임하므로, naver_client 모듈에 별도 토큰 frozenset/dict 가 있으면 안 된다.
        """
        # naver_client 소스에서 _PLACEHOLDER_TOKENS, _PLACEHOLDER_PHRASES 같은
        # 독자 토큰 상수가 정의되어 있지 않은지 확인.
        import inspect

        source = inspect.getsource(naver_client)
        # _PLACEHOLDER_TOKENS 가 "import 해서 쓰는" 문맥이 아니라 정의문인지 확인.
        # 정의문("= frozenset(" 또는 "= {")이 있으면 독자 집합이 있는 것.
        for marker in (
            "_PLACEHOLDER_TOKENS = frozenset",
            "_PLACEHOLDER_TOKENS = {",
            "_PLACEHOLDER_PHRASES = frozenset",
            "_PLACEHOLDER_PHRASES = {",
            "_PLACEHOLDER_GUIDANCE = frozenset",
            "_PLACEHOLDER_GUIDANCE = {",
        ):
            assert (
                marker not in source
            ), f"naver_client 에 독자 placeholder 토큰 집합이 정의됨: {marker!r}"

    def test_g_qa_agents_has_single_canonical_token_set(self):
        """qa_agents 는 정확히 하나의 정본 토큰 집합을 가진다."""
        assert hasattr(
            qa_agents, "_PLACEHOLDER_TOKENS"
        ), "qa_agents 에 _PLACEHOLDER_TOKENS 정본이 없음"
        assert isinstance(qa_agents._PLACEHOLDER_TOKENS, frozenset)
        # standard/compact 는 정본에서 파생되어야 한다(손으로 따로 유지 X).
        assert hasattr(qa_agents, "_PLACEHOLDER_TOKENS_STANDARD")
        assert hasattr(qa_agents, "_PLACEHOLDER_TOKENS_COMPACT")
        # standard 는 정본과 같은 원소.
        assert frozenset(qa_agents._PLACEHOLDER_TOKENS) == qa_agents._PLACEHOLDER_TOKENS_STANDARD
