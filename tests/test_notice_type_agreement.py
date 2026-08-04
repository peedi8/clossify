# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""준비 단계(prepare_listing)와 등록 단계(register_product) 가 같은 상품·같은
카테고리에서 **동일한 고시 타입** 을 추론하는지 검증한다.

회귀 방지:
  (a) 핵심 — 카테고리 ``50001060`` 으로 ``prepare_listing`` → needs_user 를 모두
      채워 ``register_product`` 호출 → 고시 필수필드 누락으로 거부되지 *않는다*.
      준비 단계가 알려준 것만 채우면 등록이 되어야 한다.
  (b) 같은 카테고리에서 두 단계가 추론한 고시 타입이 동일하다 (FURNITURE).
  (c) 경로가 ETC 로 떨어지는 카테고리에서도 두 단계가 동일하다.
  (d) 알 수 없는/미확정 카테고리에서 준비 단계가 예외로 죽지 않는다.
  (e) 휴리스틱 표가 한 곳에만 정의되어 있다 (모듈 두 곳에서 같은 객체 참조).

``COMMERCE_DRY_RUN`` 은 끈 상태로, 실제 네이버 HTTP 호출은 mock 으로 차단하고
호출 횟수를 센다.
"""

from __future__ import annotations

from unittest import mock

import pytest

from clossify import common, mcp_server, naver_client, qa_agents, register
from clossify.text_props import CATEGORY_PATH_NOTICE_HINTS

# ---------------------------------------------------------------------------
# 공통 픽스처·상수.
# ---------------------------------------------------------------------------

# 이 카테고리가 회귀 범위다. 실등록에서 prepare 는 ETC, register 는 FURNITURE
# 를 내서 8개 필수필드가 갑자기 누락된 것처럼 보였다.
_FURNITURE_CATEGORY = "50001060"

# config 의 smartstore_notice_defaults 섹션 — 원산지·AS·공통 5필드 모두 채움.
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


def _fake_attach_ok(sources):
    """images.attach_images 대체 — 항상 URL 리스트 반환."""
    urls = [f"http://cdn/test/img{i}.png" for i in range(len(sources))]
    return {"urls": urls, "rejected": [], "notes": []}


@pytest.fixture
def isolated_prepared_dir(tmp_path, monkeypatch):
    """common.PREPARED_DIR 을 tmp_path 로 격리."""
    fake_dir = tmp_path / "prepared"
    fake_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(common, "PREPARED_DIR", fake_dir)
    return fake_dir


def _patch_notice_cfg():
    """원산지/AS/공통 5필드가 모두 채워진 config 컨텍스트를 만든다."""
    return (
        mock.patch.object(naver_client, "_notice_config", return_value=_NOTICE_CFG_FULL),
        mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
        mock.patch.object(
            common, "cfg", return_value={"smartstore_notice_defaults": _NOTICE_CFG_FULL}
        ),
    )


# ---------------------------------------------------------------------------
# (e) 휴리스틱 표가 한 곳에만 정의되어 있다.
#     ``qa_agents`` 와 ``naver_client`` 가 ``text_props`` 의 같은 객체를 참조.
# ---------------------------------------------------------------------------


def test_e_hints_table_is_single_shared_object():
    """두 소비자(qa_agents, naver_client)가 text_props 의 동일 객체를 본다.

    과거에는 두 모듈에 각각 사본이 있었고 주석으로만 "동일" 이라고 표시했다.
    사본은 inevitably 갈라진다 — 단일 진실 공급원을 참조하는지 단언한다.
    """
    # naver_client 모듈이 _resolve_notice_type 안에서 참조하는 이름은
    # 모듈 스코프에 바인딩된 CATEGORY_PATH_NOTICE_HINTS 여야 한다 (사본이 아님).
    assert hasattr(naver_client, "CATEGORY_PATH_NOTICE_HINTS"), (
        "naver_client 가 CATEGORY_PATH_NOTICE_HINTS 를 가져오지 않는다 — "
        "사본이 남아 있을 수 있다"
    )
    # 정본(text_props)과 동일 객체(identity)인지 확인.
    assert (
        naver_client.CATEGORY_PATH_NOTICE_HINTS is CATEGORY_PATH_NOTICE_HINTS
    ), "naver_client 가 text_props 의 정본 테이블을 직접 참조하지 않는다"
    # 과거 사본 이름(_CATEGORY_PATH_NOTICE_HINTS)이 두 모듈에 남아있지 않은지 확인.
    assert not hasattr(
        naver_client, "_CATEGORY_PATH_NOTICE_HINTS"
    ), "naver_client 에 과거 사본(_CATEGORY_PATH_NOTICE_HINTS)이 남아 있다"
    assert not hasattr(
        qa_agents, "_CATEGORY_PATH_NOTICE_HINTS"
    ), "qa_agents 에 과거 사본(_CATEGORY_PATH_NOTICE_HINTS)이 남아 있다"


# ---------------------------------------------------------------------------
# (b) 같은 카테고리(50001060)에서 두 단계가 동일한 타입(FURNITURE)을 낸다.
# ---------------------------------------------------------------------------


def test_b_both_stages_infer_furniture_for_50001060():
    """카테고리 50001060 은 양쪽 모두 FURNITURE 로 추론되어야 한다."""
    cat_path = register._category_path_for(_FURNITURE_CATEGORY)
    # 카테고리 메타가 이 ID 를 알면 "가구" 경로가 나와야 한다.
    assert "가구" in cat_path, (
        f"카테고리 {_FURNITURE_CATEGORY} 의 경로에 '가구' 가 없다: {cat_path!r}. "
        "data 파일이 이 ID 를 모르면 테스트 전제가 성립하지 않는다."
    )

    # 준비 단계의 추론 경로: prepare_listing 이 컴플라이언스 컨텍스트에 넣는
    # category_path/name 으로부터 _infer_notice_type 호출.
    prepare_type = qa_agents._infer_notice_type(
        {"category_path": cat_path, "category_name": cat_path}
    )

    # 등록 단계의 추론 경로: mcp_server._build_compliance_context 가 동일한
    # lookup 으로 만든 컨텍스트.
    _, register_ctx = mcp_server._build_compliance_context(
        "테스트가구", _FURNITURE_CATEGORY, {"originProduct": {}}
    )
    register_type = qa_agents._infer_notice_type(register_ctx)

    assert prepare_type == "FURNITURE", f"준비 단계 추론이 FURNITURE 가 아니다: {prepare_type!r}"
    assert register_type == "FURNITURE", f"등록 단계 추론이 FURNITURE 가 아니다: {register_type!r}"
    assert prepare_type == register_type


# ---------------------------------------------------------------------------
# (c) 경로가 ETC 로 떨어지는 카테고리에서도 두 단계가 동일하다.
# ---------------------------------------------------------------------------


def test_c_both_stages_agree_on_etc_for_unknown_path_category(monkeypatch):
    """category_path 조회가 빈 문자열로 떨어지면 양쪽 모두 ETC 로 합의한다.

    회귀 방지: 과거에는 준비 단계가 경로를 아예 넘기지 않아 ETC 로,
    등록 단계는 경로를 조회해 FURNITURE 등으로 보는 불일치가 있었다.
    이제 양쪽이 같은 lookup 을 쓰므로, 조회가 빈 문자열이면 양쪽 다 빈 문자열이다.
    """
    # category_path 조회가 빈 문자열을 반환하도록 강제 — 양쪽이 같은 값을 보게.
    monkeypatch.setattr(register, "_category_path_for", lambda cid: "")
    monkeypatch.setattr(mcp_server, "_category_path_for", lambda cid: "")

    prepare_type = qa_agents._infer_notice_type(
        {"category_id": "00000000", "category_path": "", "category_name": ""}
    )
    _, register_ctx = mcp_server._build_compliance_context(
        "테스트ETC", "00000000", {"originProduct": {}}
    )
    register_type = qa_agents._infer_notice_type(register_ctx)

    assert prepare_type == "ETC"
    assert register_type == "ETC"
    assert prepare_type == register_type


# ---------------------------------------------------------------------------
# (d) 알 수 없는/미확정 카테고리에서 준비 단계가 예외로 죽지 않는다.
# ---------------------------------------------------------------------------


def test_d_prepare_listing_survives_unknown_category(isolated_prepared_dir):
    """category_meta 가 모르는 ID 거나 데이터가 깨져도 prepare_listing 은
    예외로 죽지 않고 ETC 경로로 진행되어야 한다 (fail-closed 규칙 위반 X).
    """
    unknown_id = "99999999"
    d = {
        "name": "알수없음카테고리테스트",
        "salePrice": 10000,
        "image_sources": ["a.png"],
        "category_id": unknown_id,
    }
    # category_path_for 자체는 이미 try/except 로 빈 문자열을 반환하므로
    # 예외가 밖으로 나오지 않는다. 다른 의존(config) 만 mock.
    with (
        mock.patch.object(naver_client, "_notice_config", return_value=_NOTICE_CFG_FULL),
        mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
        mock.patch.object(
            common, "cfg", return_value={"smartstore_notice_defaults": _NOTICE_CFG_FULL}
        ),
    ):
        # 예외 없이 payload 가 반환되어야 한다.
        payload = register.prepare_listing(d, attach_fn=_fake_attach_ok)

    assert isinstance(payload, dict)
    assert payload.get("product_key"), "prepare_listing 이 product_key 를 반환해야 한다"


# ---------------------------------------------------------------------------
# (a) 핵심 — 50001060 으로 prepare → needs_user 채워 register → 필수필드
#     누락으로 거부되지 않는다.
# ---------------------------------------------------------------------------


def test_a_prepare_needs_user_satisfies_register_for_50001060(isolated_prepared_dir, monkeypatch):
    """준비 단계가 알려준 needs_user 만 채우면 등록이 고시 필수필드 누락으로
    거부되지 않아야 한다.

    재현 시나리오(회귀 전):
      prepare_listing  → ETC 판정 → needs_user 0건 → 게이트 통과 안내
      register_product → FURNITURE 판정 → 필수 8개 누락 → 거부
      → 판매자가 엉뚱한 질문에 답하고 통과했다고 안내받은 뒤 등록에서 막힘.

    수정 후에는 두 단계가 같은 카테고리 경로 lookup 을 쓰므로 같은 타입을 내고,
    준비 단계의 needs_user 가 등록 단계의 요구를 충족시킨다.
    """
    name = "핵심회귀가구"
    price = 50000

    # --- 1단계: prepare_listing 으로 needs_user 확인 ---
    d_prepare = {
        "name": name,
        "salePrice": price,
        "image_sources": ["a.png", "b.png"],
        "category_id": _FURNITURE_CATEGORY,
        "notice": {},  # 고시 본문 비움 → prepare 가 필수 필드를 needs_user 로 알림
    }
    with _patch_notice_cfg()[0], _patch_notice_cfg()[1], _patch_notice_cfg()[2]:
        prepared = register.prepare_listing(d_prepare, attach_fn=_fake_attach_ok)

    # 준비 단계의 컴플라이언스 위반에서 "고시 필수필드" 가 있어야 한다 —
    # FURNITURE 의 필수 필드가 notice 가 비어 있으므로 누락으로 보고된다.
    qa = prepared.get("qa") or {}
    comp_violations = []
    for row in qa.get("agents") or []:
        if isinstance(row, dict) and row.get("agent") == "compliance":
            comp_violations = row.get("violations") or []
            break
    field_rule_details = [
        str(v.get("detail") or "")
        for v in comp_violations
        if isinstance(v, dict) and str(v.get("rule") or "") == "고시 필수필드"
    ]
    assert field_rule_details, (
        "준비 단계가 FURNITURE 필수필드 누락을 보고하지 않는다 — "
        "고시 타입이 아직 ETC 로 떨어지고 있을 수 있다 (수정 미적용)."
    )
    # detail 형태: "고시 타입 FURNITURE 필수 필드 누락: certificationType, color, ..."
    joined_details = " ".join(field_rule_details)
    assert (
        "FURNITURE" in joined_details
    ), f"준비 단계가 FURNITURE 가 아닌 타입으로 보고 있다: {joined_details!r}"

    # --- 2단계: needs_user 가 알려준 필드를 모두 채운 notice 구성 ---
    # 준비 단계의 needs_user 에서 필드 이름을 추출한다.
    # prepare_listing 의 needs_user "field" 는 rule 이름("고시 필수필드")이고,
    # 실제 필드 이름은 "why"(= compliance violation detail) 안에
    # "고시 타입 FURNITURE 필수 필드 누락: certificationType, color, ..." 형태로
    # 들어 있다. 등록 단계(mcp_server._run_compliance_gate)가 detail 에서
    # "누락:" 뒤의 필드 이름들을 파싱하는 것과 동일한 방식으로 추출한다.
    needs_user = prepared.get("needs_user") or []
    assert needs_user, "준비 단계가 needs_user 를 비워두면 안 된다 (회귀)."
    filled_fields: list[str] = []
    for item in needs_user:
        if not isinstance(item, dict):
            continue
        why = str(item.get("why") or "")
        if "누락:" in why:
            after = why.split("누락:", 1)[1]
            for field in after.split(","):
                field = field.strip()
                if field:
                    filled_fields.append(field)
    # FURNITURE 필수 필드가 한 개 이상 있어야 한다.
    assert filled_fields, f"needs_user 에서 필드 이름을 추출할 수 없다. needs_user={needs_user}"

    # 추론된 FURNITURE node 키로 본문을 만든다.
    spec = qa_agents._notice_type_spec("FURNITURE") or {}
    node_key = spec.get("node") or "furniture"
    filled_body = {field: f"테스트값-{field}" for field in filled_fields}
    filled_notice = {
        "productInfoProvidedNoticeType": "FURNITURE",
        node_key: filled_body,
    }

    # --- 3단계: 채운 notice 로 다시 prepare 해서 게이트를 통과시킨다 ---
    # 회귀 시나리오의 핵심: 처음 prepare 는 notice 가 비어 있어 compliance FAIL.
    # needs_user 가 알려주는 필드를 채운 notice 로 다시 prepare 하면, 같은 카테고리
    # 경로 추론으로 FURNITURE 필수 필드 검사가 되고 — 모두 채워졌으므로 compliance
    # 가 PASS 된다. 이것이 "준비 단계가 알려준 것만 채우면 된다" 의 검증이다.
    pkey = prepared["product_key"]
    d_refill = {
        "name": name,
        "salePrice": price,
        "image_sources": ["a.png", "b.png"],
        "category_id": _FURNITURE_CATEGORY,
        "notice": filled_notice,
    }
    with _patch_notice_cfg()[0], _patch_notice_cfg()[1], _patch_notice_cfg()[2]:
        prepared_refill = register.prepare_listing(d_refill, attach_fn=_fake_attach_ok)
    # 같은 product_key 를 써야 한다 (같은 상품의 재준비).
    assert (
        prepared_refill["product_key"] == pkey
    ), "같은 상품의 재준비인데 product_key 가 다르다 — 키 유도가 비결정론적이다."
    # compliance 위반이 "고시 필수필드" 룰이면 안 된다 — 모두 채웠으므로.
    qa_refill = prepared_refill.get("qa") or {}
    for row in qa_refill.get("agents") or []:
        if isinstance(row, dict) and row.get("agent") == "compliance":
            for v in row.get("violations") or []:
                if isinstance(v, dict) and str(v.get("rule") or "") == "고시 필수필드":
                    pytest.fail(
                        f"needs_user 를 모두 채웠는데 컴플라이언스가 고시 필수필드 "
                        f"누락으로 FAIL: {v.get('detail')}. "
                        "준비 단계가 알려준 필드 세트가 등록 단계 요구와 다르다 (회귀)."
                    )

    # --- 4단계: PENDING image/copy 를 submit_reviews 로 해소 ---
    # (register_product 의 prepared QA 게이트가 PENDING/FAIL 을 차단하므로)
    submit_reviews_ok = mcp_server.submit_reviews(
        pkey,
        [
            {"agent": "image", "verdict": "PASS", "violations": [], "summary": "test pass"},
            {"agent": "copy", "verdict": "PASS", "violations": [], "summary": "test pass"},
        ],
    )
    # 병합 후 게이트가 허용해야 한다.
    assert submit_reviews_ok.get(
        "gate_allowed"
    ), f"submit_reviews 후 게이트가 차단되어 있다: {submit_reviews_ok}"

    # --- 5단계: register_product 호출 (HTTP 는 mock) ---
    # COMMERCE_DRY_RUN 끔 — 실제 네이버 호출 경로를 타되, HTTP 만 mock.
    monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
    naver_calls = []
    monkeypatch.setattr(
        naver_client,
        "register_product",
        lambda payload: naver_calls.append(payload)
        or (200, {"originProduct": {"originProductNo": "TEST-ORIGIN-1"}}),
    )
    monkeypatch.setattr(
        naver_client,
        "get_product",
        lambda origin_no: (200, {"originProduct": {"originProductNo": origin_no}}),
    )
    # config mock 유지.
    monkeypatch.setattr(naver_client, "_notice_config", lambda: _NOTICE_CFG_FULL)
    monkeypatch.setattr(naver_client, "_kc_config", lambda: ({}, ""))
    monkeypatch.setattr(common, "cfg", lambda: {"smartstore_notice_defaults": _NOTICE_CFG_FULL})

    result = mcp_server.register_product(
        name=name,
        price=price,
        category_id=_FURNITURE_CATEGORY,
        product_key=pkey,
        notice=filled_notice,
        preview_confirmed=True,
    )

    # 핵심 단언: 고시 필수필드 누락으로 거부되지 않는다.
    blocked_by = result.get("blocked_by")
    assert blocked_by != "compliance", (
        f"준비 단계 needs_user 를 모두 채웠는데 등록 단계가 컴플라이언스로 거부했다. "
        f"blocked_by={blocked_by}, violations={result.get('violations')}, "
        f"needs_user={result.get('needs_user')}"
    )
    # 네이버 API 로 실제로 도달했는지 확인 — 거부가 아니라 성공 경로.
    assert len(naver_calls) == 1, (
        f"네이버 API 호출이 1회여야 한다 (거부가 아님): {len(naver_calls)}회, " f"result={result}"
    )
