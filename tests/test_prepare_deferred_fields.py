"""준비 단계(prepare_listing) 의 지연 고시 필드(deferred_notice_fields) 처리 검증.

본 테스트가 다루는 계약 (a)-(f):

  (a) 판매자가 ``deferred_notice_fields`` 로 선언한 고시 필드는 준비 단계의
      컴플라이언스 게이트에서 "고시 필수필드 누락" 위반에서 제외된다 —
      준비가 ``FAIL`` 로 끊기지 않는다 (본 결함의 핵심).
  (b) ``deferred_notice_fields`` 선언 없이 빈 고시 필드가 있으면 컴플라이언스가
      여전히 ``FAIL`` 차단한다 (회귀 — 게이트가 느슨해지지 않음).
  (c) 원산지(origin) 필드는 판매자가 미루기로 선언해도 거부된다 —
      ``needs_user`` 에 거부 사실이 드러난다 (조용한 누락 금지).
  (d) boolean/date 타입 필드는 미루기로 선언해도 거부된다 —
      ``needs_user`` 에 거부 사실이 드러난다.
  (e) 준비 단계와 등록 단계가 같은 ``deferred_notice_fields`` 로 같은
      컴플라이언스 결과를 낸다 (두 단계 합의).
  (f) allowlist 밖 필드명은 미루기에서 거부된다 —
      ``needs_user`` 에 거부 사실이 드러난다.

모든 테스트는 tmp_path 기반 격리와 monkeypatch 로 네이버 API 호출을 차단한다.
준비 단계는 ``register.prepare_listing`` 을 직접 호출한다 — 컴플라이언스 결과는
payload 의 ``qa.agents`` 중 ``compliance`` 항목에서 읽는다.
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

from clossify import common, naver_client, qa_agents, register


# --------------------------------------------------------------------------- #
# 공통 픽스처 및 헬퍼.
# --------------------------------------------------------------------------- #
@pytest.fixture
def isolated_prepared_dir(tmp_path, monkeypatch):
    """common.PREPARED_DIR 을 tmp_path 로 격리."""
    fake_dir = tmp_path / "prepared"
    fake_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(common, "PREPARED_DIR", fake_dir)
    return fake_dir


def _fake_attach_ok(sources):
    """images.attach_images 대체 — 항상 URL 리스트 반환."""
    urls = [f"http://cdn/test/img{i}.png" for i in range(len(sources))]
    return {"urls": urls, "rejected": [], "notes": []}


# 의류 카테고리 (WEAR 고시 타입).
_CLOTHING_CATEGORY = "50021299"

# notice_config mock: origin 이 설정된 정상 config.
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


def _common_cfg_origin():
    """_compliance_code_check 의 common.cfg 직접 읽기를 위한 mock 값."""
    return {
        "smartstore_notice_defaults": {
            "origin_area_code": "04",
            "origin_content": "중국",
        },
    }


# WEAR 고시 본문 — material 만 빠진 partial notice.
# material 을 테스트 대상(미루기 또는 누락) 으로 쓴다.
def _wear_notice_without_material(extra: dict | None = None) -> dict:
    body = {
        "color": "블랙",
        "size": "FREE",
        "manufacturer": "테스트제조사",
        "caution": "물 세탁 가능",
        "packDateText": "2024-01-01",
        "warrantyPolicy": "구매 후 7일 이내 교환 가능",
        "afterServiceDirector": "070-1234-5678",
        "returnCostReason": "단순변심 반품비용 구매자부담",
        "noRefundReason": "주문제작 청약철회 제한",
        "qualityAssuranceStandard": "관련법에 따름",
        "compensationProcedure": "소비자분쟁해결기준",
        "troubleShootingContents": "고객센터 문의",
    }
    if extra:
        body.update(extra)
    return {
        "productInfoProvidedNoticeType": "WEAR",
        "wear": body,
    }


def _make_compliant_wear_payload(extra_body: dict | None = None) -> dict:
    """컴플라이언스 게이트를 통과하는 WEAR 페이로드를 반환.

    ``naver_client.build_payload`` 의 mock 반환값으로 쓴다 — 준비 단계의
    컴플라이언스 검사가 등록 단계와 동일한 페이로드를 보게 한다.
    """
    wear_body = {
        "material": "면 100%",
        "color": "블랙",
        "size": "FREE",
        "manufacturer": "테스트제조사",
        "caution": "물 세탁 가능",
        "packDateText": "2026-01",
        "warrantyPolicy": "구매 후 7일 교환 가능",
        "afterServiceDirector": "070-1234-5678",
        "returnCostReason": "단순변심 반품비용 구매자부담",
        "noRefundReason": "주문제작 청약철회 제한",
        "qualityAssuranceStandard": "관련법에 따름",
        "compensationProcedure": "소비자분쟁해결기준",
        "troubleShootingContents": "고객센터 문의",
    }
    if extra_body:
        wear_body.update(extra_body)
    return {
        "originProduct": {
            "originProductNo": "test-no",
            "images": {
                "representativeImage": {
                    "url": "http://cdn/test/representative.png",
                },
            },
            "detailAttribute": {
                "productInfoProvidedNotice": {
                    "productInfoProvidedNoticeType": "WEAR",
                    "wear": wear_body,
                },
                "originAreaInfo": {
                    "originAreaCode": "04",
                    "content": "중국",
                },
                "afterServiceInfo": {
                    "afterServiceTelephoneNumber": "070-1234-5678",
                },
            },
        },
    }


def _compliance_agent_from_payload(payload: dict) -> dict | None:
    """payload 의 qa.agents 에서 compliance 항목을 찾아 반환."""
    qa = payload.get("qa") or {}
    for row in qa.get("agents") or []:
        if isinstance(row, dict) and row.get("agent") == "compliance":
            return row
    return None


def _compliance_verdict(payload: dict) -> str:
    """payload 의 compliance verdict 를 반환 (없으면 빈 문자열)."""
    row = _compliance_agent_from_payload(payload)
    if row is None:
        return ""
    return qa_agents._clamp_verdict(row.get("verdict"), default="")


def _compliance_violation_details(payload: dict) -> list[str]:
    """payload 의 compliance violation detail 문자열 리스트를 반환."""
    row = _compliance_agent_from_payload(payload)
    if row is None:
        return []
    return [
        str(v.get("detail") or "") for v in (row.get("violations") or []) if isinstance(v, dict)
    ]


def _setup_config_mocks():
    """준비 단계의 config 의존을 mock 하는 context manager 체인을 반환.

    ``with _setup_config_mocks():`` 형태로 쓴다.
    """
    return (
        mock.patch.object(naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN),
        mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
        mock.patch.object(common, "cfg", return_value=_common_cfg_origin()),
    )


def _run_prepare(d: dict, *, build_payload_fn=None) -> dict:
    """준비 단계를 실행하고 payload 를 반환.

    ``build_payload_fn`` 이 주어지면 그것으로 ``naver_client.build_payload`` 를
    대체한다. 주어지지 않으면 ``_make_compliant_wear_payload`` 를 반환하는 기본
    빌더를 쓴다(컴플라이언스 통과용).
    """
    if build_payload_fn is None:

        def build_payload_fn(*a, **kw):
            return _make_compliant_wear_payload()

    with (
        mock.patch.object(naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN),
        mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
        mock.patch.object(common, "cfg", return_value=_common_cfg_origin()),
        mock.patch.object(naver_client, "build_payload", side_effect=build_payload_fn),
    ):
        return register.prepare_listing(d, attach_fn=_fake_attach_ok)


# --------------------------------------------------------------------------- #
# (a) deferred_notice_fields 선언 → 준비 단계 컴플라이언스 통과.
# --------------------------------------------------------------------------- #
class TestDeferredFieldsPassPrepareCompliance:
    """판매자가 미루기로 선언한 필드는 준비 단계 게이트에서 제외된다."""

    def test_a_material_deferred_passes_prepare_compliance(self, isolated_prepared_dir):
        """(a) material 을 deferred_notice_fields 로 선언 → compliance FAIL 아님.

        핵심 결함: 과거에는 준비 단계가 deferred_notice_fields 를 컴플라이언스에
        넘기지 않아, 판매자가 미루기로 선언한 필드가 여전히 "고시 필수필드 누락"
        FAIL 을 일으켰다. 본 테스트는 그 결함이 수정되었는지 검증한다.
        """

        def build_fn(*a, **kw):
            # material 이 빈 자리인 WEAR 페이로드 반환 (컴플라이언스가 material
            # 누락을 지적할 조건).
            return _make_compliant_wear_payload(extra_body={"material": ""})

        d = {
            "name": "준비미루기통과상품",
            "salePrice": 30000,
            "image_sources": ["a.png"],
            "category_id": _CLOTHING_CATEGORY,
            "notice": _wear_notice_without_material(),  # material 없음
            "deferred_notice_fields": ["material"],
        }
        payload = _run_prepare(d, build_payload_fn=build_fn)

        # compliance 가 FAIL 이 아니어야 한다.
        verdict = _compliance_verdict(payload)
        assert verdict != qa_agents.FAIL, (
            f"material 을 미루기로 선언했는데 compliance 가 FAIL 임: {verdict}\n"
            f"violations: {_compliance_violation_details(payload)}"
        )

    def test_a_deferred_fields_stored_in_payload(self, isolated_prepared_dir):
        """(a) 미루기로 선언한 필드가 payload 에 저장된다."""

        def build_fn(*a, **kw):
            return _make_compliant_wear_payload(extra_body={"material": ""})

        d = {
            "name": "미루기저장상품",
            "salePrice": 31000,
            "image_sources": ["a.png"],
            "category_id": _CLOTHING_CATEGORY,
            "notice": _wear_notice_without_material(),
            "deferred_notice_fields": ["material"],
        }
        payload = _run_prepare(d, build_payload_fn=build_fn)
        assert (
            payload.get("deferred_notice_fields") == ["material"]
        ), f"payload 에 deferred_notice_fields 가 저장되어야 함: {payload.get('deferred_notice_fields')}"

    def test_a_no_missing_field_violation_when_deferred(self, isolated_prepared_dir):
        """(a) compliance violation 에 '필수 필드 누락' 이 없어야 한다."""

        def build_fn(*a, **kw):
            return _make_compliant_wear_payload(extra_body={"material": ""})

        d = {
            "name": "미루기누락없음상품",
            "salePrice": 32000,
            "image_sources": ["a.png"],
            "category_id": _CLOTHING_CATEGORY,
            "notice": _wear_notice_without_material(),
            "deferred_notice_fields": ["material"],
        }
        payload = _run_prepare(d, build_payload_fn=build_fn)
        details = _compliance_violation_details(payload)
        # "material" 이 누락으로 지적되지 않아야 한다.
        assert not any(
            "material" in d and "누락" in d for d in details
        ), f"material 이 미루기로 선언됐는데 누락 위반에 있음: {details}"


# --------------------------------------------------------------------------- #
# (b) deferred_notice_fields 선언 없이 빈 필드 → 여전히 FAIL (회귀).
# --------------------------------------------------------------------------- #
class TestNoDeferralStillFailsCompliance:
    """미루기 선언 없이 빈 고시 필드가 있으면 준비 단계 compliance FAIL."""

    def test_b_blank_field_without_deferral_fails(self, isolated_prepared_dir):
        """(b) material 이 빈데 deferred_notice_fields 선언 없음 → FAIL."""

        def build_fn(*a, **kw):
            return _make_compliant_wear_payload(extra_body={"material": ""})

        d = {
            "name": "미루기없음실패상품",
            "salePrice": 33000,
            "image_sources": ["a.png"],
            "category_id": _CLOTHING_CATEGORY,
            "notice": _wear_notice_without_material(),
            # deferred_notice_fields 생략 — 빈 칸은 차단.
        }
        payload = _run_prepare(d, build_payload_fn=build_fn)
        verdict = _compliance_verdict(payload)
        assert verdict == qa_agents.FAIL, (
            f"미루기 없이 빈 필드인데 compliance 가 FAIL 이 아님: {verdict}\n"
            f"violations: {_compliance_violation_details(payload)}"
        )

    def test_b_blank_field_violation_mentions_field(self, isolated_prepared_dir):
        """(b) FAIL 위반에 빈 필드명이 포함되어야 한다."""

        def build_fn(*a, **kw):
            return _make_compliant_wear_payload(extra_body={"material": ""})

        d = {
            "name": "미루기없음위반상품",
            "salePrice": 34000,
            "image_sources": ["a.png"],
            "category_id": _CLOTHING_CATEGORY,
            "notice": _wear_notice_without_material(),
        }
        payload = _run_prepare(d, build_payload_fn=build_fn)
        details = _compliance_violation_details(payload)
        assert any("material" in d for d in details), f"material 누락이 위반에 없음: {details}"

    def test_b_no_deferral_no_payload_storage(self, isolated_prepared_dir):
        """(b) 미루기 선언이 없으면 payload 에 deferred_notice_fields 가 없다."""

        def build_fn(*a, **kw):
            return _make_compliant_wear_payload()

        d = {
            "name": "미루기미선언상품",
            "salePrice": 35000,
            "image_sources": ["a.png"],
            "category_id": _CLOTHING_CATEGORY,
            "notice": _wear_notice_without_material(extra={"material": "면 100%"}),
        }
        payload = _run_prepare(d, build_payload_fn=build_fn)
        assert (
            "deferred_notice_fields" not in payload or not payload["deferred_notice_fields"]
        ), "미루기 선언 없는데 payload 에 deferred_notice_fields 가 있음"


# --------------------------------------------------------------------------- #
# (c) 원산지 필드 미루기 → 거부, needs_user 에 알림.
# --------------------------------------------------------------------------- #
class TestOriginFieldDeferredRejectedAtPrepare:
    """원산지 필드는 준비 단계에서 미루기 대상이 아니다."""

    def test_c_origin_field_not_in_payload_deferred(self, isolated_prepared_dir):
        """(c) madeIn 을 미루기로 선언해도 payload 의 deferred 에 들어가지 않는다."""

        def build_fn(*a, **kw):
            return _make_compliant_wear_payload()

        d = {
            "name": "원산지미루기거부상품",
            "salePrice": 36000,
            "image_sources": ["a.png"],
            "category_id": _CLOTHING_CATEGORY,
            "notice": _wear_notice_without_material(extra={"material": "면 100%"}),
            "deferred_notice_fields": ["madeIn"],  # 원산지 — 미루기 불가
        }
        payload = _run_prepare(d, build_payload_fn=build_fn)
        stored = payload.get("deferred_notice_fields") or []
        assert (
            "madeIn" not in stored
        ), f"원산지 필드가 payload 의 deferred 에 있음 (거부되어야 함): {stored}"

    def test_c_origin_field_rejection_in_needs_user(self, isolated_prepared_dir):
        """(c) 원산지 미루기 거부가 needs_user 에 알림으로 드러난다."""

        def build_fn(*a, **kw):
            return _make_compliant_wear_payload()

        d = {
            "name": "원산지거부알림상품",
            "salePrice": 37000,
            "image_sources": ["a.png"],
            "category_id": _CLOTHING_CATEGORY,
            "notice": _wear_notice_without_material(extra={"material": "면 100%"}),
            "deferred_notice_fields": ["madeIn"],
        }
        payload = _run_prepare(d, build_payload_fn=build_fn)
        needs_user = payload.get("needs_user") or []
        rejected_entries = [
            entry
            for entry in needs_user
            if isinstance(entry, dict) and entry.get("field") == "deferred_notice_fields_rejected"
        ]
        assert (
            len(rejected_entries) >= 1
        ), f"needs_user 에 deferred_notice_fields_rejected 가 없음: {needs_user}"
        why_text = str(rejected_entries[0].get("why") or "")
        assert "madeIn" in why_text, f"needs_user 의 거부 사유에 madeIn 필드명이 없음: {why_text}"

    def test_c_origin_area_info_content_also_rejected(self, isolated_prepared_dir):
        """(c) originAreaInfo.content 도 원산지로 거부된다."""

        def build_fn(*a, **kw):
            return _make_compliant_wear_payload()

        d = {
            "name": "원산지경로거부상품",
            "salePrice": 38000,
            "image_sources": ["a.png"],
            "category_id": _CLOTHING_CATEGORY,
            "notice": _wear_notice_without_material(extra={"material": "면 100%"}),
            "deferred_notice_fields": ["originAreaInfo.content"],
        }
        payload = _run_prepare(d, build_payload_fn=build_fn)
        stored = payload.get("deferred_notice_fields") or []
        assert "originAreaInfo.content" not in stored


# --------------------------------------------------------------------------- #
# (d) boolean/date 타입 필드 미루기 → 거부, needs_user 에 알림.
# --------------------------------------------------------------------------- #
class TestTypedFieldDeferredRejectedAtPrepare:
    """boolean/date 타입 필드는 준비 단계에서 미루기 대상이 아니다."""

    @pytest.mark.parametrize(
        "field",
        [
            "importDeclaration",  # boolean
            "releaseDate",  # date
            "geneticallyModified",  # boolean
            "packDate",  # date
        ],
    )
    def test_d_typed_field_not_in_payload_deferred(self, field, isolated_prepared_dir):
        """(d) boolean/date 필드를 미루기로 선언해도 payload 의 deferred 에 없다."""

        def build_fn(*a, **kw):
            return _make_compliant_wear_payload()

        d = {
            "name": f"타입미루기거부상품{field}",
            "salePrice": 39000,
            "image_sources": ["a.png"],
            "category_id": _CLOTHING_CATEGORY,
            "notice": _wear_notice_without_material(extra={"material": "면 100%"}),
            "deferred_notice_fields": [field],
        }
        payload = _run_prepare(d, build_payload_fn=build_fn)
        stored = payload.get("deferred_notice_fields") or []
        assert (
            field not in stored
        ), f"boolean/date 필드 {field} 가 payload 의 deferred 에 있음 (거부되어야 함): {stored}"

    def test_d_typed_field_rejection_in_needs_user(self, isolated_prepared_dir):
        """(d) boolean/date 필드 거부가 needs_user 에 알림으로 드러난다."""

        def build_fn(*a, **kw):
            return _make_compliant_wear_payload()

        d = {
            "name": "타입거부알림상품",
            "salePrice": 40000,
            "image_sources": ["a.png"],
            "category_id": _CLOTHING_CATEGORY,
            "notice": _wear_notice_without_material(extra={"material": "면 100%"}),
            "deferred_notice_fields": ["importDeclaration"],
        }
        payload = _run_prepare(d, build_payload_fn=build_fn)
        needs_user = payload.get("needs_user") or []
        rejected_entries = [
            entry
            for entry in needs_user
            if isinstance(entry, dict) and entry.get("field") == "deferred_notice_fields_rejected"
        ]
        assert (
            len(rejected_entries) >= 1
        ), f"needs_user 에 deferred_notice_fields_rejected 가 없음: {needs_user}"
        why_text = str(rejected_entries[0].get("why") or "")
        assert (
            "importDeclaration" in why_text
        ), f"needs_user 의 거부 사유에 importDeclaration 필드명이 없음: {why_text}"


# --------------------------------------------------------------------------- #
# (e) 준비 단계와 등록 단계의 컴플라이언스 합의.
# --------------------------------------------------------------------------- #
class TestPrepareRegisterComplianceConsistency:
    """같은 deferred_notice_fields 로 준비/등록 단계가 같은 판정을 낸다."""

    def test_e_same_deferred_same_result_pass(self, isolated_prepared_dir):
        """(e) material 미루기 → 준비 compliance 와 등록 compliance 모두 같은 판정.

        준비 단계의 컴플라이언스 검사가 등록 단계와 같은 함수
        (``_compliance_code_check``) 에 같은 인자(``deferred_notice_fields``) 를
        넘기면 결과가 같아야 한다. 본 테스트는 그 합의를 검증한다.
        """
        deferred = ["material"]

        def build_fn(*a, **kw):
            return _make_compliant_wear_payload(extra_body={"material": ""})

        # --- 준비 단계 실행 ---
        d = {
            "name": "준비등록합의상품",
            "salePrice": 41000,
            "image_sources": ["a.png"],
            "category_id": _CLOTHING_CATEGORY,
            "notice": _wear_notice_without_material(),
            "deferred_notice_fields": deferred,
        }
        payload = _run_prepare(d, build_payload_fn=build_fn)
        prepare_verdict = _compliance_verdict(payload)

        # --- 등록 단계와 동일한 검사 수행 ---
        # 준비 단계가 만든 tentative_payload 로 동일한 compliance 검사.
        api_payload = _make_compliant_wear_payload(extra_body={"material": ""})
        with mock.patch.object(common, "cfg", return_value=_common_cfg_origin()):
            register_result = qa_agents._compliance_code_check(
                "준비등록합의상품",
                {"category_id": _CLOTHING_CATEGORY},
                api_payload=api_payload,
                deferred_notice_fields=deferred,
            )
        register_verdict = qa_agents._clamp_verdict(register_result.get("verdict"), default="")

        assert prepare_verdict == register_verdict, (
            f"준비({prepare_verdict}) 와 등록({register_verdict}) compliance 판정 불일치\n"
            f"준비 violations: {_compliance_violation_details(payload)}\n"
            f"등록 violations: {[str(v.get('detail')) for v in register_result.get('violations') or []]}"
        )

    def test_e_both_pass_when_deferred(self, isolated_prepared_dir):
        """(e) material 미루기 → 양쪽 모두 FAIL 이 아니다."""
        deferred = ["material"]

        def build_fn(*a, **kw):
            return _make_compliant_wear_payload(extra_body={"material": ""})

        d = {
            "name": "양쪽통과상품",
            "salePrice": 42000,
            "image_sources": ["a.png"],
            "category_id": _CLOTHING_CATEGORY,
            "notice": _wear_notice_without_material(),
            "deferred_notice_fields": deferred,
        }
        payload = _run_prepare(d, build_payload_fn=build_fn)
        prepare_verdict = _compliance_verdict(payload)
        assert prepare_verdict != qa_agents.FAIL, (
            f"준비 compliance FAIL: {prepare_verdict}\n"
            f"violations: {_compliance_violation_details(payload)}"
        )
        # 등록 단계도 같은 검사 → 같은 결과.
        api_payload = _make_compliant_wear_payload(extra_body={"material": ""})
        with mock.patch.object(common, "cfg", return_value=_common_cfg_origin()):
            register_result = qa_agents._compliance_code_check(
                "양쪽통과상품",
                {"category_id": _CLOTHING_CATEGORY},
                api_payload=api_payload,
                deferred_notice_fields=deferred,
            )
        register_verdict = qa_agents._clamp_verdict(register_result.get("verdict"), default="")
        assert register_verdict != qa_agents.FAIL, f"등록 compliance FAIL: {register_verdict}"

    def test_e_both_fail_when_no_deferral(self, isolated_prepared_dir):
        """(e) 미루기 없이 빈 필드 → 양쪽 모두 FAIL."""

        def build_fn(*a, **kw):
            return _make_compliant_wear_payload(extra_body={"material": ""})

        d = {
            "name": "양쪽실패상품",
            "salePrice": 43000,
            "image_sources": ["a.png"],
            "category_id": _CLOTHING_CATEGORY,
            "notice": _wear_notice_without_material(),
        }
        payload = _run_prepare(d, build_payload_fn=build_fn)
        prepare_verdict = _compliance_verdict(payload)
        assert prepare_verdict == qa_agents.FAIL

        api_payload = _make_compliant_wear_payload(extra_body={"material": ""})
        with mock.patch.object(common, "cfg", return_value=_common_cfg_origin()):
            register_result = qa_agents._compliance_code_check(
                "양쪽실패상품",
                {"category_id": _CLOTHING_CATEGORY},
                api_payload=api_payload,
                deferred_notice_fields=None,
            )
        register_verdict = qa_agents._clamp_verdict(register_result.get("verdict"), default="")
        assert register_verdict == qa_agents.FAIL
        assert prepare_verdict == register_verdict


# --------------------------------------------------------------------------- #
# (f) allowlist 밖 필드명 → 거부, needs_user 에 알림.
# --------------------------------------------------------------------------- #
class TestAllowlistOffListRejectedAtPrepare:
    """allowlist 밖 필드명은 준비 단계에서 미루기 대상이 아니다."""

    @pytest.mark.parametrize(
        "bad_field",
        [
            "madein",  # 오타(camelCase 아님)
            "country_of_origin",  # 별칭(snake_case)
            "totally_unknown_field",  # 완전 허구
        ],
    )
    def test_f_off_list_field_not_in_payload_deferred(self, bad_field, isolated_prepared_dir):
        """(f) allowlist 밖 필드는 payload 의 deferred 에 들어가지 않는다."""

        def build_fn(*a, **kw):
            return _make_compliant_wear_payload()

        d = {
            "name": f"허용밖거부상품{bad_field}",
            "salePrice": 44000,
            "image_sources": ["a.png"],
            "category_id": _CLOTHING_CATEGORY,
            "notice": _wear_notice_without_material(extra={"material": "면 100%"}),
            "deferred_notice_fields": [bad_field],
        }
        payload = _run_prepare(d, build_payload_fn=build_fn)
        stored = payload.get("deferred_notice_fields") or []
        assert (
            bad_field not in stored
        ), f"allowlist 밖 필드 {bad_field} 가 payload 의 deferred 에 있음 (거부되어야 함): {stored}"

    def test_f_off_list_field_rejection_in_needs_user(self, isolated_prepared_dir):
        """(f) allowlist 밖 필드 거부가 needs_user 에 알림으로 드러난다."""

        def build_fn(*a, **kw):
            return _make_compliant_wear_payload()

        d = {
            "name": "허용밖거부알림상품",
            "salePrice": 45000,
            "image_sources": ["a.png"],
            "category_id": _CLOTHING_CATEGORY,
            "notice": _wear_notice_without_material(extra={"material": "면 100%"}),
            "deferred_notice_fields": ["totally_unknown_field"],
        }
        payload = _run_prepare(d, build_payload_fn=build_fn)
        needs_user = payload.get("needs_user") or []
        rejected_entries = [
            entry
            for entry in needs_user
            if isinstance(entry, dict) and entry.get("field") == "deferred_notice_fields_rejected"
        ]
        assert (
            len(rejected_entries) >= 1
        ), f"needs_user 에 deferred_notice_fields_rejected 가 없음: {needs_user}"
        why_text = str(rejected_entries[0].get("why") or "")
        assert (
            "totally_unknown_field" in why_text
        ), f"needs_user 의 거부 사유에 필드명이 없음: {why_text}"

    def test_f_mixed_on_and_off_list_only_on_kept(self, isolated_prepared_dir):
        """(f) allowlist 내/외 필드가 섞여 있으면 내 것만 payload 에 남는다."""

        def build_fn(*a, **kw):
            return _make_compliant_wear_payload(extra_body={"material": ""})

        d = {
            "name": "섞인필드상품",
            "salePrice": 46000,
            "image_sources": ["a.png"],
            "category_id": _CLOTHING_CATEGORY,
            "notice": _wear_notice_without_material(extra={"color": ""}),
            # material(allowlist 안) + color(allowlist 안) + totally_unknown(밖).
            "deferred_notice_fields": ["material", "color", "totally_unknown_field"],
        }
        payload = _run_prepare(d, build_payload_fn=build_fn)
        stored = payload.get("deferred_notice_fields") or []
        # allowlist 안의 material, color 만 남아야 함.
        assert "material" in stored, "material 이 거부되면 안 됨 (allowlist 안)"
        assert "color" in stored, "color 가 거부되면 안 됨 (allowlist 안)"
        assert (
            "totally_unknown_field" not in stored
        ), f"allowlist 밖 필드가 payload 에 남음: {stored}"
        # needs_user 에 거부 알림이 있어야 함.
        needs_user = payload.get("needs_user") or []
        rejected_entries = [
            entry
            for entry in needs_user
            if isinstance(entry, dict) and entry.get("field") == "deferred_notice_fields_rejected"
        ]
        assert (
            len(rejected_entries) >= 1
        ), f"allowlist 밖 필드 거부 알림이 needs_user 에 없음: {needs_user}"
        why_text = str(rejected_entries[0].get("why") or "")
        assert "totally_unknown_field" in why_text
