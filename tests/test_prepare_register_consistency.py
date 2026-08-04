# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""준비 단계(prepare_listing)와 등록 단계(register_product) 가 같은 상품을
같은 의미로 보는지 검증한다.

두 단계가 다른 것을 보는 근본 원인 두 가지를 고정한 뒤, 아래 7가지를 단언한다:

  (a) 정상 상품이 준비 단계를 통과한다 — 원산지/AS 누락 위반이 없다.
  (b) 공통 5필드를 config 에만 넣어도 준비 단계가 누락으로 보고하지 않는다.
  (c) 진짜로 원산지가 없으면 여전히 차단되되, 예외가 아니라 needs_user 로 나온다.
  (d) 정상 이미지 1장이면 원본 이미지 위반이 없다 (오탐 회귀 방지).
  (e) 51자 이름 + FAIL prepared → http_calls == 0, prepared_qa_gate 차단.
  (f) 50자 이하 이름의 기존 경로는 동작이 같다.
  (g) 자동 채움이 찾은 prepared 와 게이트가 판정한 prepared 가 같은 것이다.
"""

from __future__ import annotations

from unittest import mock

import pytest

from clossify import common, mcp_server, naver_client, qa_agents, register

# ---------------------------------------------------------------------------
# 공통 픽스처·상수.
# ---------------------------------------------------------------------------

_CLOTHING_CATEGORY = "50021299"

# 정상 상품용 notice 오버라이드 (WEAR 고시 필수필드 충족).
_WEAR_NOTICE_OK = {
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

# 원산지가 빠진 config (진짜 누락 케이스용).
_NOTICE_CFG_NO_ORIGIN = {
    "as_tel": "070-1234-5678",
    "manufacturer": "테스트제조사",
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


def _compliance_rules(payload):
    """prepared payload 의 compliance agent 위반 rule 이름 리스트를 반환."""
    qa = payload.get("qa") if isinstance(payload, dict) else None
    if not isinstance(qa, dict):
        return []
    rules = []
    for agent_row in qa.get("agents") or []:
        if not isinstance(agent_row, dict):
            continue
        if str(agent_row.get("agent") or "") != "compliance":
            continue
        for v in agent_row.get("violations") or []:
            if isinstance(v, dict):
                rules.append(str(v.get("rule") or ""))
    return rules


def _needs_user_fields(payload):
    """prepared payload 의 needs_user 항목에서 field 이름만 추출."""
    fields = []
    for item in payload.get("needs_user") or []:
        if isinstance(item, dict):
            fields.append(str(item.get("field") or ""))
    return fields


# ---------------------------------------------------------------------------
# (a) 정상 상품이 준비 단계를 통과한다.
#     완전한 WEAR 고시 + config 에 원산지/AS → 원산지·AS 누락 위반이 없다.
# ---------------------------------------------------------------------------


def test_a_normal_product_prepare_no_origin_as_violations(isolated_prepared_dir):
    d = {
        "name": "정상테스트니트",
        "salePrice": 30000,
        "image_sources": ["a.png"],
        "category_id": _CLOTHING_CATEGORY,
        "notice": _WEAR_NOTICE_OK,
    }
    with mock.patch.object(naver_client, "_notice_config", return_value=_NOTICE_CFG_FULL):
        with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
            with mock.patch.object(
                common, "cfg", return_value={"smartstore_notice_defaults": _NOTICE_CFG_FULL}
            ):
                payload = register.prepare_listing(d, attach_fn=_fake_attach_ok)

    rules = _compliance_rules(payload)
    assert "원산지 누락" not in rules, f"원산지 누락 위반이 있다: {rules}"
    assert "A/S 연락처 누락" not in rules, f"A/S 연락처 누락 위반이 있다: {rules}"


# ---------------------------------------------------------------------------
# (b) 공통 5필드를 config 에만 넣은 경우 누락으로 보고하지 않는다.
# ---------------------------------------------------------------------------


def test_b_common_five_fields_from_config_not_reported(isolated_prepared_dir):
    # notice 에 공통 5필드를 주지 않는다 — config 에만 있다.
    d = {
        "name": "공통필드테스트니트",
        "salePrice": 30000,
        "image_sources": ["a.png"],
        "category_id": _CLOTHING_CATEGORY,
        "notice": _WEAR_NOTICE_OK,  # WEAR 필수필드만 있고 공통 5필드는 없음
    }
    with mock.patch.object(naver_client, "_notice_config", return_value=_NOTICE_CFG_FULL):
        with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
            with mock.patch.object(
                common, "cfg", return_value={"smartstore_notice_defaults": _NOTICE_CFG_FULL}
            ):
                payload = register.prepare_listing(d, attach_fn=_fake_attach_ok)

    rules = _compliance_rules(payload)
    # 공통 5필드(return_cost_reason 등) 가 config 에서 채워졌으므로 누락 위반이 없어야 한다.
    assert (
        "고시 필수필드" not in rules
    ), f"공통 5필드를 config 에서 채웠는데 고시 필수필드 위반이 있다: {rules}"


# ---------------------------------------------------------------------------
# (c) 진짜로 원산지가 없으면 여전히 차단되되, 예외가 아니라 needs_user 로 나온다.
# ---------------------------------------------------------------------------


def test_c_missing_origin_blocks_as_needs_user_not_exception(isolated_prepared_dir):
    d = {
        "name": "원산지누락니트",
        "salePrice": 30000,
        "image_sources": ["a.png"],
        "category_id": _CLOTHING_CATEGORY,
        "notice": _WEAR_NOTICE_OK,
    }
    # 원산지가 없는 config. 준비 단계에서 예외가 아니라 컴플라이언스 FAIL 로 번역되어야 한다.
    with mock.patch.object(naver_client, "_notice_config", return_value=_NOTICE_CFG_NO_ORIGIN):
        with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
            with mock.patch.object(
                common, "cfg", return_value={"smartstore_notice_defaults": _NOTICE_CFG_NO_ORIGIN}
            ):
                # 예외 없이 payload 가 반환되어야 한다.
                payload = register.prepare_listing(d, attach_fn=_fake_attach_ok)

    rules = _compliance_rules(payload)
    # 빌더가 원산지 부재로 예외를 던지고, 그것이 컴플라이언스 위반으로 번역되었을 수도 있고,
    # 빌드는 됐지만 origin_content 가 비어서 "원산지 누락" 위반이 나왔을 수도 있다.
    # 어느 쪽이든 컴플라이언스 FAIL 이어야 한다.
    qa = payload.get("qa") if isinstance(payload, dict) else {}
    # compliance 가 FAIL 이거나 전체가 FAIL/PENDING 이어야 한다.
    comp_verdict = qa_agents.PASS
    for row in qa.get("agents") or []:
        if isinstance(row, dict) and row.get("agent") == "compliance":
            comp_verdict = qa_agents._clamp_verdict(row.get("verdict"))
    assert (
        comp_verdict == qa_agents.FAIL
    ), f"원산지가 없을 때 compliance 가 FAIL 이어야 한다 (현재={comp_verdict}, rules={rules})"
    # needs_user 에 해당 항목이 있어야 한다 (예외가 아니라 사용자 입력 요청).
    needs = _needs_user_fields(payload)
    assert len(needs) > 0, f"원산지 누락 시 needs_user 가 비어 있으면 안 된다 (rules={rules})"


# ---------------------------------------------------------------------------
# (d) 정상 이미지 1장이면 원본 이미지 위반이 없다 (오탐 회귀 방지).
# ---------------------------------------------------------------------------


def test_d_single_valid_image_no_original_image_violation(isolated_prepared_dir):
    d = {
        "name": "이미지오탐방지니트",
        "salePrice": 30000,
        "image_sources": ["only.png"],
        "category_id": _CLOTHING_CATEGORY,
        "notice": _WEAR_NOTICE_OK,
    }
    with mock.patch.object(naver_client, "_notice_config", return_value=_NOTICE_CFG_FULL):
        with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
            with mock.patch.object(
                common, "cfg", return_value={"smartstore_notice_defaults": _NOTICE_CFG_FULL}
            ):
                payload = register.prepare_listing(d, attach_fn=_fake_attach_ok)

    rules = _compliance_rules(payload)
    assert (
        "원본 이미지" not in rules
    ), f"정상 이미지 1장인데 원본 이미지 위반이 있다 (오탐 회귀): {rules}"


# ---------------------------------------------------------------------------
# (e) 51자 이름 + FAIL prepared → http_calls == 0, prepared_qa_gate 차단.
#     COMMERCE_DRY_RUN 끈 상태에서 게이트가 실제로 도는 조건에서 단언한다.
# ---------------------------------------------------------------------------


def test_e_51char_name_fail_prepared_blocks_at_qa_gate(isolated_prepared_dir, monkeypatch):
    # 51자 이름 — 50자 컷 대상.
    long_name = "가" * 51
    price = 30000

    # 원본 이름 기준 product_key (준비 단계가 쓰는 키).
    pkey = register.make_product_key(long_name, price)

    # FAIL 난 prepared payload 를 직접 주입한다.
    fail_qa = qa_agents.aggregate_qa_results(
        [
            qa_agents._qa_agent_result(
                "compliance",
                qa_agents.FAIL,
                [{"rule": "테스트위반", "severity": qa_agents.FAIL, "detail": "FAIL_PREPARED"}],
                "FAIL_PREPARED",
            )
        ]
    )
    prepared = {
        "product_key": pkey,
        "product": {
            "name": long_name,
            "categoryId": _CLOTHING_CATEGORY,
            "salePrice": price,
        },
        "images": {
            "listing_urls": ["http://cdn/test/img0.png"],
            "detail_urls": ["http://cdn/test/img0.png"],
        },
        "detail_html": "<p>FAIL_PREPARED</p>",
        "qa": fail_qa,
        "needs_llm": [],
        "needs_user": [],
        "version": common.PREPARED_PAYLOAD_VERSION,
    }
    register.write_prepared_payload(prepared)

    # COMMERCE_DRY_RUN 이 꺼져 있어야 게이트가 실제로 돈다.
    monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)

    naver_calls = []
    monkeypatch.setattr(
        naver_client,
        "register_product",
        lambda payload: naver_calls.append(payload) or (200, {}),
    )
    # 컴플라이언스 게이트 통과용 mock (이 테스트의 관심사는 prepared QA 게이트).
    monkeypatch.setattr(
        mcp_server,
        "_run_compliance_gate",
        lambda *a, **kw: {
            "blocked": False,
            "violations": [],
            "needs_user": [],
            "pending_reviews": [],
        },
    )
    monkeypatch.setattr(naver_client, "_notice_config", lambda: _NOTICE_CFG_FULL)
    monkeypatch.setattr(naver_client, "_kc_config", lambda: ({}, ""))

    result = mcp_server.register_product(
        name=long_name,
        price=price,
        category_id=_CLOTHING_CATEGORY,
        # image_urls 와 detail_html 을 생략 → 자동 채움이 prepared 에서 가져온다.
    )

    assert (
        len(naver_calls) == 0
    ), f"51자 이름 + FAIL prepared 가 네이버 API 에 도달했다 (우회): {len(naver_calls)}회"
    assert (
        result.get("blocked_by") == "prepared_qa_gate"
    ), f"차단 사유가 prepared_qa_gate 여야 한다 (현재={result.get('blocked_by')})"


# ---------------------------------------------------------------------------
# (f) 50자 이하 이름의 기존 경로는 동작이 같다.
# ---------------------------------------------------------------------------


def test_f_short_name_fail_prepared_also_blocks(isolated_prepared_dir, monkeypatch):
    short_name = "50자이하정상이름"
    price = 30000
    pkey = register.make_product_key(short_name, price)

    fail_qa = qa_agents.aggregate_qa_results(
        [
            qa_agents._qa_agent_result(
                "compliance",
                qa_agents.FAIL,
                [{"rule": "테스트위반", "severity": qa_agents.FAIL, "detail": "FAIL_PREPARED"}],
                "FAIL_PREPARED",
            )
        ]
    )
    prepared = {
        "product_key": pkey,
        "product": {
            "name": short_name,
            "categoryId": _CLOTHING_CATEGORY,
            "salePrice": price,
        },
        "images": {
            "listing_urls": ["http://cdn/test/img0.png"],
            "detail_urls": ["http://cdn/test/img0.png"],
        },
        "detail_html": "<p>FAIL_PREPARED</p>",
        "qa": fail_qa,
        "needs_llm": [],
        "needs_user": [],
        "version": common.PREPARED_PAYLOAD_VERSION,
    }
    register.write_prepared_payload(prepared)

    monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
    naver_calls = []
    monkeypatch.setattr(
        naver_client,
        "register_product",
        lambda payload: naver_calls.append(payload) or (200, {}),
    )
    monkeypatch.setattr(
        mcp_server,
        "_run_compliance_gate",
        lambda *a, **kw: {
            "blocked": False,
            "violations": [],
            "needs_user": [],
            "pending_reviews": [],
        },
    )
    monkeypatch.setattr(naver_client, "_notice_config", lambda: _NOTICE_CFG_FULL)
    monkeypatch.setattr(naver_client, "_kc_config", lambda: ({}, ""))

    result = mcp_server.register_product(
        name=short_name,
        price=price,
        category_id=_CLOTHING_CATEGORY,
    )

    assert len(naver_calls) == 0
    assert result.get("blocked_by") == "prepared_qa_gate"


# ---------------------------------------------------------------------------
# (g) 자동 채움이 찾은 prepared 와 게이트가 판정한 prepared 가 같은 것이다.
#     51자 이름에서 키가 하나임을 검증한다.
# ---------------------------------------------------------------------------


def test_g_single_key_for_autofill_and_gate_51char(isolated_prepared_dir, monkeypatch):
    """51자 이름에서 자동 채움과 게이트가 같은 product_key 를 쓰는지 검증.

    키가 두 개면 자동 채움은 prepared 를 찾고(원본 이름 키) 게이트는 못 찾고(절단 이름 키) -
    이 우회가 다시 생기지 않는지 확인한다.
    """
    long_name = "나" * 51
    price = 30000
    # 원본 이름 기준 키 하나만 prepared 에 저장한다.
    pkey_original = register.make_product_key(long_name, price)
    # 절단 이름 기준 키 (이전 버그 경로 — 이 키로는 prepared 가 없어야 한다).
    truncated_name = long_name[: naver_client.MAX_PRODUCT_NAME_LEN]
    pkey_truncated = register.make_product_key(truncated_name, price)
    assert (
        pkey_original != pkey_truncated
    ), "전제 확인: 51자 이름에서 원본 키와 절단 키는 달라야 한다"

    fail_qa = qa_agents.aggregate_qa_results(
        [
            qa_agents._qa_agent_result(
                "compliance",
                qa_agents.FAIL,
                [{"rule": "테스트위반", "severity": qa_agents.FAIL, "detail": "FAIL"}],
                "FAIL",
            )
        ]
    )
    prepared = {
        "product_key": pkey_original,
        "product": {
            "name": long_name,
            "categoryId": _CLOTHING_CATEGORY,
            "salePrice": price,
        },
        "images": {
            "listing_urls": ["http://cdn/test/x.png"],
            "detail_urls": ["http://cdn/test/x.png"],
        },
        "detail_html": "<p>FAIL</p>",
        "qa": fail_qa,
        "needs_llm": [],
        "needs_user": [],
        "version": common.PREPARED_PAYLOAD_VERSION,
    }
    register.write_prepared_payload(prepared)

    monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
    naver_calls = []
    monkeypatch.setattr(
        naver_client,
        "register_product",
        lambda payload: naver_calls.append(payload) or (200, {}),
    )
    monkeypatch.setattr(
        mcp_server,
        "_run_compliance_gate",
        lambda *a, **kw: {
            "blocked": False,
            "violations": [],
            "needs_user": [],
            "pending_reviews": [],
        },
    )
    monkeypatch.setattr(naver_client, "_notice_config", lambda: _NOTICE_CFG_FULL)
    monkeypatch.setattr(naver_client, "_kc_config", lambda: ({}, ""))

    # image_urls 를 생략 → 자동 채움이 동작한다.
    # 자동 채움이 원본 키로 prepared 를 찾고, 게이트도 같은 키로 판정하면
    # FAIL 이 차단된다. 두 키가 다르면 게이트가 prepared 를 못 찾아 우회된다.
    result = mcp_server.register_product(
        name=long_name,
        price=price,
        category_id=_CLOTHING_CATEGORY,
    )

    # 자동 채움이 찾은 prepared 의 detail_html 가 채워졌는지 확인 (autofill hit).
    assert "detail_html" in result.get("filled_from_prepared", []), (
        "자동 채움이 prepared 에서 detail_html 를 가져오지 못했다 "
        "(원본 키로 prepared 를 찾지 못한 것일 수 있다)"
    )
    # 게이트가 같은 prepared 를 판정했는지 확인 — FAIL 이 차단되어야 한다.
    assert result.get("blocked_by") == "prepared_qa_gate", (
        f"게이트가 자동 채움이 찾은 것과 같은 prepared 를 판정하지 못했다 "
        f"(blocked_by={result.get('blocked_by')})"
    )
    assert len(naver_calls) == 0
