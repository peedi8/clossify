# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""네이버 태그 추천/제한 API 연동 검증.

본 테스트가 다루는 계약 (a)-(f):

  (a) 추천 성공 + 제한 검사 성공 → ``final_tags`` 가 제한 필터링을 거쳤다.
  (b) 사용자 태그가 제한 판정 → ``needs_user`` 에 알림, **삭제하지 않는다**.
  (c) 추천 API 실패 → fail-open (예외 없음), ``tags_meta.error`` 에 사유.
  (d) 제한 API 실패 → fail-open (예외 없음), ``tags_meta.error`` 에 사유.
  (e) "니트" 함정 — 추천 목록에 있으면서 동시에 제한 → ``final_tags`` 에 없다.
  (f) ``MAX_SELLER_TAGS`` (10) 초과 분은 잘린다.

모든 테스트는 ``recommend_fn``/``restricted_fn`` 을 통해 mock 을 주입한다 —
실호출 없음(테스트 외부 호출 차단 계약). 측정된 픽스처를 그대로 쓴다(아래 측정 계약 블록).
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

from clossify import common, naver_client, register

# --------------------------------------------------------------------------- #
# 2026-08-10 실측 계약 — 네이버 태그 API 응답 픽스처 (측정값 verbatim).
# --------------------------------------------------------------------------- #
# recommend-tags?keyword=니트 → 200 [{"code":877,"text":"니트"}, ...]
# restricted-tags?tags=니트,가디건,... → 200 [{"tag":"...","restricted":bool}, ...]
# 실측 해석: 단독 일반명사(니트·가디건) 와 금지어(쩐다) 가 restricted:true,
# 복합 태그(니트가디건·우드슬랩) 가 restricted:false.

_FIXTURE_RECOMMEND_NIT = [
    {"code": 877, "text": "니트"},
    {"code": 878, "text": "가디건"},
    {"code": 879, "text": "니트가디건"},
    {"code": 880, "text": "니트조끼"},
]

_FIXTURE_RESTRICTED = {
    "니트": True,
    "가디건": True,
    "니트가디건": False,
    "니트조끼": False,
    "우드슬랩": False,
    "쩐다": True,
}


def _restricted_response(tags):
    """측정된 restricted-tags 응답 형태를 반환: ``[{"tag": str, "restricted": bool}]``."""
    return [
        {"tag": t, "restricted": _FIXTURE_RESTRICTED.get(t, False)}
        for t in tags
        if str(t or "").strip()
    ]


# --------------------------------------------------------------------------- #
# Mock 함수 팩토리 — lambda 대신 def 함수를 반환(린트 E731 회피).
# --------------------------------------------------------------------------- #


def _make_recommend_ok(items):
    """추천 API 200 응답을 반환하는 mock 함수를 만든다."""

    def recommend_fn(keyword):
        return 200, list(items)

    return recommend_fn


def _make_recommend_fail(status_code, body):
    """추천 API 실패(지정 status_code/body) 를 반환하는 mock 함수를 만든다."""

    def recommend_fn(keyword):
        return status_code, body

    return recommend_fn


def _make_recommend_raise(exc):
    """추천 API 호출 시 예외를 발생시키는 mock 함수를 만든다."""

    def recommend_fn(keyword):
        raise exc

    return recommend_fn


def _make_restricted_ok():
    """제한 API 200 응답(측정 픽스처 기반) 을 반환하는 mock 함수를 만든다."""

    def restricted_fn(tags):
        return 200, _restricted_response(tags)

    return restricted_fn


def _make_restricted_all_false():
    """제한 API 200 응답(모두 restricted:false) 을 반환하는 mock 함수를 만든다."""

    def restricted_fn(tags):
        return 200, [{"tag": t, "restricted": False} for t in tags]

    return restricted_fn


def _make_restricted_fail(status_code, body):
    """제한 API 실패(지정 status_code/body) 를 반환하는 mock 함수를 만든다."""

    def restricted_fn(tags):
        return status_code, body

    return restricted_fn


def _make_restricted_raise(exc):
    """제한 API 호출 시 예외를 발생시키는 mock 함수를 만든다."""

    def restricted_fn(tags):
        raise exc

    return restricted_fn


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


_CLOTHING_CATEGORY = "50021299"

_WEAR_NOTICE_OK = {
    "productInfoProvidedNoticeType": "WEAR",
    "wear": {
        "material": "면 100%",
        "color": "블랙",
        "size": "FREE",
        "caution": "물 세탁 가능",
        "packDateText": "2024-01-01",
        "warrantyPolicy": "구매 후 7일 이내 교환 가능",
    },
}

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


def _needs_user_fields(payload):
    """prepared payload 의 needs_user 항목에서 field 이름만 추출."""
    fields = []
    for item in payload.get("needs_user") or []:
        if isinstance(item, dict):
            fields.append(str(item.get("field") or ""))
    return fields


def _needs_user_entry(payload, field):
    """특정 field 이름의 needs_user 항목을 반환 (없으면 None)."""
    for item in payload.get("needs_user") or []:
        if isinstance(item, dict) and item.get("field") == field:
            return item
    return None


def _run_prepare_with_tags(d_extra, *, recommend_fn, restricted_fn, isolated_prepared_dir):
    """prepare_listing 을 실행하고 payload 를 반환 (config mock 포함)."""
    d = {
        "name": d_extra.get("name", "태그테스트"),
        "salePrice": d_extra.get("salePrice", 30000),
        "image_sources": d_extra.get("image_sources", ["a.png"]),
        "category_id": d_extra.get("category_id", _CLOTHING_CATEGORY),
        "notice": d_extra.get("notice", _WEAR_NOTICE_OK),
        "tags": d_extra.get("tags", []),
    }
    for key in (
        "brand",
        "brand_name",
        "brandName",
        "category_name",
        "category_path",
        "categoryPath",
    ):
        if key in d_extra:
            d[key] = d_extra[key]
    with (
        mock.patch.object(naver_client, "_notice_config", return_value=_NOTICE_CFG_FULL),
        mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
        mock.patch.object(
            common, "cfg", return_value={"smartstore_notice_defaults": _NOTICE_CFG_FULL}
        ),
    ):
        return register.prepare_listing(
            d,
            attach_fn=_fake_attach_ok,
            recommend_fn=recommend_fn,
            restricted_fn=restricted_fn,
        )


class TestFieldDuplicatePrecheckReachability:
    """prepare_listing 응답 meta 까지 필드 중복 제거가 도달하는지 확인."""

    def test_prepare_listing_removes_and_reports_field_duplicates(self, isolated_prepared_dir):
        payload = _run_prepare_with_tags(
            {
                "name": "여성 니트 풀오버",
                "brand": "WarmCo",
                "category_name": "패션의류 > 여성의류 > 스웨터",
                "tags": ["니트", "WARMCO", "스웨터", "보온"],
            },
            recommend_fn=_make_recommend_ok([]),
            restricted_fn=_make_restricted_ok(),
            isolated_prepared_dir=isolated_prepared_dir,
        )

        assert payload["product"]["tags"] == ["보온"]
        assert payload["tags_meta"]["field_duplicates"] == [
            {"tag": "니트", "reason": "name"},
            {"tag": "WARMCO", "reason": "brand"},
            {"tag": "스웨터", "reason": "category"},
        ]


# --------------------------------------------------------------------------- #
# (a) 추천 성공 + 제한 검사 성공 → final_tags 가 제한 필터링을 거친다.
# --------------------------------------------------------------------------- #


class TestRecommendAndRestrictedFiltered:
    """추천 태그 중 restricted:false 인 것만 final_tags 에 들어간다."""

    def test_a_recommended_restricted_true_excluded_from_final_tags(self):
        """(a) 추천 "니트"(restricted:true) → final_tags 에 없다."""
        recommend_fn = _make_recommend_ok(_FIXTURE_RECOMMEND_NIT)
        restricted_fn = _make_restricted_ok()
        result = register._resolve_tags(
            "니트",
            [],
            recommend_fn=recommend_fn,
            restricted_fn=restricted_fn,
        )
        final = result["final_tags"]
        # 사용자 태그가 없으므로 final_tags 는 추천(restricted:false) 만.
        # "니트" 와 "가디건" 은 restricted:true → 제외.
        assert (
            "니트" not in final
        ), f"추천이지만 restricted:true 인 '니트' 가 final_tags 에 있음: {final}"
        assert (
            "가디건" not in final
        ), f"추천이지만 restricted:true 인 '가디건' 가 final_tags 에 있음: {final}"
        # "니트가디건"/"니트조끼" 는 restricted:false → 포함.
        assert "니트가디건" in final, f"restricted:false 인 추천이 final_tags 에 없음: {final}"
        assert "니트조끼" in final, f"restricted:false 인 추천이 final_tags 에 없음: {final}"

    def test_a_restricted_lookup_ok_flag(self):
        """(a) 추천/제한 조회 모두 ok=True 로 기록된다."""
        recommend_fn = _make_recommend_ok(_FIXTURE_RECOMMEND_NIT)
        restricted_fn = _make_restricted_ok()
        result = register._resolve_tags(
            "니트",
            [],
            recommend_fn=recommend_fn,
            restricted_fn=restricted_fn,
        )
        assert result["recommend_lookup"]["ok"] is True
        assert result["restricted_lookup"]["ok"] is True
        assert result["error"] is None

    def test_a_user_tags_priority_over_recommended(self):
        """(a) 사용자 태그가 항상 우선 — 추천이 같은 태그를 줘도 중복 추가 안 함."""
        recommend_fn = _make_recommend_ok([{"code": 1, "text": "우드슬랩"}])
        restricted_fn = _make_restricted_ok()
        result = register._resolve_tags(
            "테스트",
            ["우드슬랩"],
            recommend_fn=recommend_fn,
            restricted_fn=restricted_fn,
        )
        # 사용자 태그 "우드슬랩" 이 final_tags 에 있고, 추천에서 온 "우드슬랩" 은
        # 중복으로 추가되지 않는다.
        assert result["final_tags"].count("우드슬랩") == 1
        # recommended_tags 에는 없어야 함 (사용자가 이미 줬으므로).
        assert "우드슬랩" not in result["recommended_tags"]


# --------------------------------------------------------------------------- #
# (b) 사용자 태그가 제한 판정 → needs_user 알림, 삭제하지 않는다.
# --------------------------------------------------------------------------- #


class TestUserTagRestrictedNotDeleted:
    """사용자가 직접 준 태그가 제한이어도 final_tags 에 그대로 남는다."""

    def test_b_user_restricted_tag_stays_in_final_tags(self, isolated_prepared_dir):
        """(b) 사용자 태그 "니트"(restricted:true) → final_tags 에 그대로 있어야 함.

        사용자 태그는 제한이어도 삭제하지 않는다 — 등록 단계의 백스톱이
        최종적으로 처리한다. 준비 단계는 알림만 올린다.
        """
        recommend_fn = _make_recommend_ok([])
        restricted_fn = _make_restricted_ok()
        payload = _run_prepare_with_tags(
            {
                "name": "니트제한테스트",
                "tags": ["니트"],
                # 이 시험은 필드 중복이 아닌 제한어 처리만 검증한다.
                "category_name": "패션의류 > 여성의류 > 티셔츠",
            },
            recommend_fn=recommend_fn,
            restricted_fn=restricted_fn,
            isolated_prepared_dir=isolated_prepared_dir,
        )
        tags_meta = payload.get("tags_meta") or {}
        product_tags = payload.get("product", {}).get("tags") or []
        # 사용자 태그 "니트" 가 product.tags 에 그대로 있어야 한다 (삭제 금지).
        assert (
            "니트" in product_tags
        ), f"사용자 태그 '니트' 가 삭제됨 (삭제 금지): product.tags={product_tags}"
        # tags_meta.restricted 에 source="user" 로 기록되어야 한다.
        user_restricted = [
            r for r in (tags_meta.get("restricted") or []) if r.get("source") == "user"
        ]
        assert any(r["tag"] == "니트" for r in user_restricted), (
            f"tags_meta 에 사용자 제한 태그 '니트' 가 source=user 로 없음: "
            f"{tags_meta.get('restricted')}"
        )

    def test_b_user_restricted_tag_needs_user_notification(self, isolated_prepared_dir):
        """(b) 사용자 제한 태그가 needs_user 에 tags_restricted 로 알려진다."""
        recommend_fn = _make_recommend_ok([])
        restricted_fn = _make_restricted_ok()
        payload = _run_prepare_with_tags(
            {
                "name": "니트알림테스트",
                "tags": ["니트"],
                # 이 시험은 필드 중복이 아닌 제한어 처리만 검증한다.
                "category_name": "패션의류 > 여성의류 > 티셔츠",
            },
            recommend_fn=recommend_fn,
            restricted_fn=restricted_fn,
            isolated_prepared_dir=isolated_prepared_dir,
        )
        entry = _needs_user_entry(payload, "tags_restricted")
        assert (
            entry is not None
        ), f"needs_user 에 tags_restricted 항목이 없음: {_needs_user_fields(payload)}"
        why_text = str(entry.get("why") or "")
        assert "니트" in why_text, f"tags_restricted 알림에 '니트' 가 없음: {why_text}"
        # "삭제하지 않음" 이라는 의미가 why 에 있어야 한다.
        assert (
            "삭제하지 않" in why_text or "백스톱" in why_text
        ), f"tags_restricted 알림이 '삭제하지 않음/백스톱' 언급이 없음: {why_text}"


# --------------------------------------------------------------------------- #
# (c) 추천 API 실패 → fail-open (예외 없음), error 에 사유.
# --------------------------------------------------------------------------- #


class TestRecommendApiFailureFailOpen:
    """추천 API 가 실패해도 _resolve_tags 본체가 죽지 않는다."""

    def test_c_recommend_400_fail_open_user_tags_preserved(self):
        """(c) 추천 API 400 → 예외 없음, 사용자 태그만으로 진행, error 기록."""
        recommend_fn = _make_recommend_fail(400, {"message": "입력정보가 올바르지 않습니다"})
        restricted_fn = _make_restricted_ok()
        result = register._resolve_tags(
            "니트",
            ["우드슬랩"],
            recommend_fn=recommend_fn,
            restricted_fn=restricted_fn,
        )
        # 예외 없이 결과 반환.
        assert isinstance(result, dict)
        # 사용자 태그가 final_tags 에 그대로.
        assert "우드슬랩" in result["final_tags"]
        # recommend_lookup.ok 가 False.
        assert result["recommend_lookup"]["ok"] is False
        assert result["recommend_lookup"]["status_code"] == 400
        # error 에 사유 기록.
        assert result["error"] is not None
        assert "추천" in result["error"]

    def test_c_recommend_network_exception_fail_open(self):
        """(c) 추천 API 예외(ConnectionError 등) → fail-open."""
        recommend_fn = _make_recommend_raise(ConnectionError("네트워크 끊김"))
        restricted_fn = _make_restricted_ok()
        result = register._resolve_tags(
            "니트",
            ["우드슬랩"],
            recommend_fn=recommend_fn,
            restricted_fn=restricted_fn,
        )
        assert "우드슬랩" in result["final_tags"]
        assert result["recommend_lookup"]["ok"] is False
        assert result["error"] is not None
        assert "추천" in result["error"]

    def test_c_recommend_failure_needs_user_notification(self, isolated_prepared_dir):
        """(c) 추천 API 실패 → needs_user 에 tags_lookup_failed 알림."""
        recommend_fn = _make_recommend_fail(500, {"message": "서버 오류"})
        restricted_fn = _make_restricted_ok()
        payload = _run_prepare_with_tags(
            {"name": "추천실패테스트", "tags": ["우드슬랩"]},
            recommend_fn=recommend_fn,
            restricted_fn=restricted_fn,
            isolated_prepared_dir=isolated_prepared_dir,
        )
        entry = _needs_user_entry(payload, "tags_lookup_failed")
        assert (
            entry is not None
        ), f"needs_user 에 tags_lookup_failed 항목이 없음: {_needs_user_fields(payload)}"
        why_text = str(entry.get("why") or "")
        assert (
            "추천" in why_text or "조회" in why_text
        ), f"tags_lookup_failed 알림에 사유가 부족함: {why_text}"


# --------------------------------------------------------------------------- #
# (d) 제한 API 실패 → fail-open (예외 없음), error 에 사유.
# --------------------------------------------------------------------------- #


class TestRestrictedApiFailureFailOpen:
    """제한 API 가 실패해도 _resolve_tags 본체가 죽지 않는다."""

    def test_d_restricted_400_fail_open(self):
        """(d) 제한 API 400 → 예외 없음, 추천 태그가 그대로 들어감(제한 검사 생략)."""
        recommend_fn = _make_recommend_ok([{"code": 1, "text": "니트가디건"}])
        restricted_fn = _make_restricted_fail(400, {"message": "입력정보가 올바르지 않습니다"})
        result = register._resolve_tags(
            "니트",
            [],
            recommend_fn=recommend_fn,
            restricted_fn=restricted_fn,
        )
        assert isinstance(result, dict)
        # 제한 검사가 실패했으므로 추천 태그가 제한 검사 없이 들어간다.
        # (제한 검사를 통과한 것은 아님 — fail-open 계약)
        assert "니트가디건" in result["final_tags"]
        assert result["restricted_lookup"]["ok"] is False
        assert result["restricted_lookup"]["status_code"] == 400
        assert result["error"] is not None
        assert "제한" in result["error"]

    def test_d_restricted_network_exception_fail_open(self):
        """(d) 제한 API 예외 → fail-open."""
        recommend_fn = _make_recommend_ok([{"code": 1, "text": "니트가디건"}])
        restricted_fn = _make_restricted_raise(TimeoutError("타임아웃"))
        result = register._resolve_tags(
            "니트",
            [],
            recommend_fn=recommend_fn,
            restricted_fn=restricted_fn,
        )
        assert isinstance(result, dict)
        assert "니트가디건" in result["final_tags"]
        assert result["restricted_lookup"]["ok"] is False
        assert result["error"] is not None
        assert "제한" in result["error"]

    def test_d_restricted_failure_needs_user_notification(self, isolated_prepared_dir):
        """(d) 제한 API 실패 → needs_user 에 tags_lookup_failed 알림."""
        recommend_fn = _make_recommend_ok([{"code": 1, "text": "니트가디건"}])
        restricted_fn = _make_restricted_fail(503, "Service Unavailable")
        payload = _run_prepare_with_tags(
            {"name": "제한실패테스트", "tags": ["우드슬랩"]},
            recommend_fn=recommend_fn,
            restricted_fn=restricted_fn,
            isolated_prepared_dir=isolated_prepared_dir,
        )
        entry = _needs_user_entry(payload, "tags_lookup_failed")
        assert (
            entry is not None
        ), f"needs_user 에 tags_lookup_failed 항목이 없음: {_needs_user_fields(payload)}"
        why_text = str(entry.get("why") or "")
        assert (
            "제한" in why_text or "조회" in why_text
        ), f"tags_lookup_failed 알림에 사유가 부족함: {why_text}"


# --------------------------------------------------------------------------- #
# (e) "니트" 함정 — 추천 목록에 있으면서 동시에 제한 → final_tags 에 없다.
# --------------------------------------------------------------------------- #


class TestNitTrapRecommendedAndRestricted:
    """니트는 추천(code 877) 이면서 동시에 restricted:true 다.

    추천 결과를 그대로 쓰지 말고 반드시 제한 검사를 통과시켜야 한다.
    본 테스트 그룹은 그 함정이 우회되지 않는지 검증한다.
    """

    def test_e_nit_recommended_but_not_in_final_tags(self):
        """(e) "니트" 는 추천 + restricted:true → final_tags 에 없다."""
        recommend_fn = _make_recommend_ok(_FIXTURE_RECOMMEND_NIT)
        restricted_fn = _make_restricted_ok()
        result = register._resolve_tags(
            "니트",
            [],
            recommend_fn=recommend_fn,
            restricted_fn=restricted_fn,
        )
        assert "니트" not in result["final_tags"], (
            f"'니트' 가 추천이면서 restricted:true 인데 final_tags 에 있음: "
            f"{result['final_tags']}"
        )
        # 새 결정론 사전 검사가 제한어 API보다 먼저 상품명 중복을 제거·보고한다.
        assert {"tag": "니트", "reason": "name"} in result["field_duplicates"]

    def test_e_nit_not_in_recommended_tags(self):
        """(e) "니트" 는 recommended_tags(최종에 들어간 추천) 에 없다."""
        recommend_fn = _make_recommend_ok(_FIXTURE_RECOMMEND_NIT)
        restricted_fn = _make_restricted_ok()
        result = register._resolve_tags(
            "니트",
            [],
            recommend_fn=recommend_fn,
            restricted_fn=restricted_fn,
        )
        assert "니트" not in result["recommended_tags"], (
            f"restricted:true 인 '니트' 가 recommended_tags 에 있음: "
            f"{result['recommended_tags']}"
        )

    def test_e_nit_trap_in_prepare_listing(self, isolated_prepared_dir):
        """(e) prepare_listing 전체 흐름에서 "니트" 함정이 우회되지 않는다."""
        recommend_fn = _make_recommend_ok(_FIXTURE_RECOMMEND_NIT)
        restricted_fn = _make_restricted_ok()
        payload = _run_prepare_with_tags(
            {"name": "니트함정테스트", "tags": []},
            recommend_fn=recommend_fn,
            restricted_fn=restricted_fn,
            isolated_prepared_dir=isolated_prepared_dir,
        )
        product_tags = payload.get("product", {}).get("tags") or []
        assert (
            "니트" not in product_tags
        ), f"prepare_listing 이 '니트' 함정을 우회함 (product.tags 에 있음): {product_tags}"
        # restricted:false 인 추천은 들어가야 함.
        assert (
            "니트가디건" in product_tags
        ), f"restricted:false 인 추천 '니트가디건' 이 product.tags 에 없음: {product_tags}"


# --------------------------------------------------------------------------- #
# (f) MAX_SELLER_TAGS (10) 초과 분은 잘린다.
# --------------------------------------------------------------------------- #


class TestMaxSellerTagsCap:
    """final_tags 는 MAX_SELLER_TAGS 를 초과하지 않는다."""

    def test_f_final_tags_capped_at_max(self):
        """(f) 추천 태그가 아무리 많아도 MAX_SELLER_TAGS 개를 넘지 않는다."""
        # 15개의 restricted:false 추천 태그.
        many_recommend = [{"code": i, "text": f"태그{i}"} for i in range(1, 16)]
        recommend_fn = _make_recommend_ok(many_recommend)
        restricted_fn = _make_restricted_all_false()
        result = register._resolve_tags(
            "테스트",
            [],
            recommend_fn=recommend_fn,
            restricted_fn=restricted_fn,
        )
        assert len(result["final_tags"]) <= register.MAX_SELLER_TAGS, (
            f"final_tags 가 MAX_SELLER_TAGS({register.MAX_SELLER_TAGS}) 를 초과: "
            f"{len(result['final_tags'])}개"
        )

    def test_f_user_tags_fill_first_then_recommended(self):
        """(f) 사용자 태그 8개 + 추천 5개 → 사용자 8 + 추천 2 = 10."""
        user_tags = [f"사용자{i}" for i in range(1, 9)]  # 8개
        recommend = [{"code": i, "text": f"추천{i}"} for i in range(1, 6)]  # 5개
        recommend_fn = _make_recommend_ok(recommend)
        restricted_fn = _make_restricted_all_false()
        result = register._resolve_tags(
            "테스트",
            user_tags,
            recommend_fn=recommend_fn,
            restricted_fn=restricted_fn,
        )
        assert len(result["final_tags"]) == register.MAX_SELLER_TAGS, (
            f"final_tags 가 MAX_SELLER_TAGS 와 다름: {len(result['final_tags'])}개 "
            f"(기대 {register.MAX_SELLER_TAGS})"
        )
        # 사용자 태그가 먼저(우선).
        assert result["final_tags"][:8] == user_tags
        # 추천은 남은 2 슬롯만.
        assert len(result["recommended_tags"]) == 2

    def test_f_max_seller_tags_constant(self):
        """(f) MAX_SELLER_TAGS 상수가 10(관례 기반 상한) 이다."""
        assert (
            register.MAX_SELLER_TAGS == 10
        ), f"MAX_SELLER_TAGS 가 10이 아님: {register.MAX_SELLER_TAGS}"
