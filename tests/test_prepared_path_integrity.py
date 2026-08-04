# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""prepared 경로의 무결성을 검증한다.

두 가지 근본 결함을 고정한다:

  (1) prepared 이미지에 무효 항목이 섞여 있을 때 조용히 걸러내는 대신
      거부해야 한다. 조용한 필터링은 2번 이미지를 대표 이미지로
      승격시키는 조용한 치환을 유발한다.
  (2) 같은 이름·가격의 SKU 가 여러 개일 때 유도 키 충돌로 마지막 prepared
      가 앞의 것을 덮어쓰는 조용한 오등록을 막아야 한다. 명시 product_key
      인자로 모호성을 제거하고, 유도 경로에서는 무엇을 어디서 가져왔는지
      응답에 드러낸다.

검증 항목:

  (a) prepared 이미지가 ``["   ", "https://cdn/b.jpg"]`` → 거부, 네이버
      호출 0회. MCP 등록과 ``register_prepared_listing`` 양쪽 모두.
  (b) 거부 사유에 prepared 이미지 문제임이 드러난다.
  (c) prepared 이미지가 전부 유효하면 기존대로 정상 진행(회귀 없음).
  (d) 같은 이름·가격의 두 상품에서 명시 ``product_key`` 를 주면 올바른
      prepared 가 쓰인다 (전송 페이로드의 상세HTML·이미지로 대조).
  (e) 키를 명시하지 않은 기존 호출이 여전히 동작한다(하위호환).
  (f) 이름+가격 유도로 prepared 를 가져왔을 때 무엇을 어디서 가져왔는지
      반환값에 드러난다(조용한 치환 없음).
"""

from __future__ import annotations

import pytest

from clossify import common, mcp_server, naver_client, qa_agents, register

# ---------------------------------------------------------------------------
# 공통 픽스처·상수.
# ---------------------------------------------------------------------------

_CLOTHING_CATEGORY = "50021299"

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


def _pass_qa():
    """통과 QA 집계 결과를 반환한다(차단 항목 없음)."""
    return qa_agents.aggregate_qa_results(
        [
            qa_agents._qa_agent_result("image", qa_agents.PASS, [], "PASS"),
            qa_agents._qa_agent_result("copy", qa_agents.PASS, [], "PASS"),
            qa_agents._qa_agent_result("compliance", qa_agents.PASS, [], "PASS"),
        ]
    )


@pytest.fixture
def isolated_prepared_dir(tmp_path, monkeypatch):
    """common.PREPARED_DIR 을 tmp_path 로 격리."""
    fake_dir = tmp_path / "prepared"
    fake_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(common, "PREPARED_DIR", fake_dir)
    return fake_dir


def _make_prepared_payload(
    product_key,
    name,
    price,
    listing_urls,
    detail_html,
    *,
    qa=None,
):
    """테스트용 prepared payload dict 를 만든다."""
    return {
        "product_key": product_key,
        "product": {
            "name": name,
            "categoryId": _CLOTHING_CATEGORY,
            "salePrice": price,
            "options": [],
            "tags": [],
            "notice": {},
            "origin_code": "",
            "manufacturer": "",
            "importer": "",
            "as_tel": "",
            "as_guide": "",
            "courier": "CJGLS",
            "delivery_fee": 3000,
        },
        "images": {
            "listing_urls": list(listing_urls),
            "detail_urls": list(listing_urls),
        },
        "detail_html": detail_html,
        "scene": {},
        "needs_llm": [],
        "needs_user": [],
        "qa": qa if qa is not None else _pass_qa(),
        "status": "SALE",
        "version": common.PREPARED_PAYLOAD_VERSION,
    }


def _setup_gate_and_naver(monkeypatch, naver_calls):
    """게이트 통과 + 네이버 호출 가로채기 + config mock."""
    monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
    monkeypatch.setattr(
        naver_client,
        "register_product",
        lambda payload: naver_calls.append(payload) or (200, {"originProductNo": "TEST_NO"}),
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


# ---------------------------------------------------------------------------
# (a) prepared 이미지가 ["   ", "https://cdn/b.jpg"] → 거부, 네이버 호출 0회.
#     MCP 등록과 register_prepared_listing 양쪽 다.
# ---------------------------------------------------------------------------


def test_a_mcp_rejects_mixed_valid_invalid_prepared_images(isolated_prepared_dir, monkeypatch):
    """MCP register_product 가 prepared 의 무효 혼합 이미지를 거부한다."""
    name = "혼합이미지테스트"
    price = 30000
    pkey = register.make_product_key(name, price)
    prepared = _make_prepared_payload(
        pkey,
        name,
        price,
        listing_urls=["   ", "https://cdn/second.jpg"],
        detail_html="<p>DETAIL</p>",
    )
    register.write_prepared_payload(prepared)

    naver_calls: list = []
    _setup_gate_and_naver(monkeypatch, naver_calls)

    result = mcp_server.register_product(
        name=name,
        price=price,
        category_id=_CLOTHING_CATEGORY,
        # image_urls 생략 → prepared 에서 자동 채움.
        preview_confirmed=True,
    )

    assert result["ok"] is False, "무효 혼합 이미지가 거부되어야 한다"
    assert len(naver_calls) == 0, f"네이버 API 가 호출되었다 (우회): {len(naver_calls)}회"


def test_a_register_prepared_rejects_mixed_valid_invalid_images(isolated_prepared_dir, monkeypatch):
    """register.register_prepared_listing 도 무효 혼합 이미지를 거부한다."""
    name = "혼합이미지직접등록"
    price = 30000
    pkey = register.make_product_key(name, price)
    prepared = _make_prepared_payload(
        pkey,
        name,
        price,
        listing_urls=["   ", "https://cdn/second.jpg"],
        detail_html="<p>DETAIL</p>",
    )
    register.write_prepared_payload(prepared)

    naver_calls: list = []
    monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
    monkeypatch.setattr(
        naver_client,
        "register_product",
        lambda payload: naver_calls.append(payload) or (200, {"originProductNo": "TEST_NO"}),
    )
    monkeypatch.setattr(naver_client, "_notice_config", lambda: _NOTICE_CFG_FULL)
    monkeypatch.setattr(naver_client, "_kc_config", lambda: ({}, ""))

    with pytest.raises(ValueError):
        register.register_prepared_listing({"name": name, "salePrice": price})
    assert (
        len(naver_calls) == 0
    ), f"register_prepared_listing 이 네이버 API 를 호출했다: {len(naver_calls)}회"


# ---------------------------------------------------------------------------
# (b) 거부 사유에 prepared 이미지 문제임이 드러난다.
# ---------------------------------------------------------------------------


def test_b_mcp_rejection_reason_mentions_prepared_images(isolated_prepared_dir, monkeypatch):
    """MCP 거부 메시지에 prepared 이미지 문제임이 명시된다."""
    name = "거부사유확인"
    price = 30000
    pkey = register.make_product_key(name, price)
    prepared = _make_prepared_payload(
        pkey,
        name,
        price,
        listing_urls=["   ", "https://cdn/second.jpg"],
        detail_html="<p>DETAIL</p>",
    )
    register.write_prepared_payload(prepared)

    naver_calls: list = []
    _setup_gate_and_naver(monkeypatch, naver_calls)

    result = mcp_server.register_product(
        name=name,
        price=price,
        category_id=_CLOTHING_CATEGORY,
        preview_confirmed=True,
    )

    assert result["ok"] is False
    error_msg = str(result.get("error") or "")
    assert (
        "prepared" in error_msg.lower() or "이미지" in error_msg
    ), f"거부 사유에 prepared 이미지 문제가 드러나야 한다 (현재={error_msg!r})"


# ---------------------------------------------------------------------------
# (c) prepared 이미지가 전부 유효하면 기존대로 정상 진행(회귀 없음).
# ---------------------------------------------------------------------------


def test_c_valid_prepared_images_proceed_normally(isolated_prepared_dir, monkeypatch):
    """전부 유효한 prepared 이미지는 거부 없이 정상 진행한다."""
    name = "정상이미지회귀"
    price = 30000
    pkey = register.make_product_key(name, price)
    prepared = _make_prepared_payload(
        pkey,
        name,
        price,
        listing_urls=["https://cdn/first.jpg", "https://cdn/second.jpg"],
        detail_html="<p>VALID_DETAIL</p>",
    )
    register.write_prepared_payload(prepared)

    naver_calls: list = []
    _setup_gate_and_naver(monkeypatch, naver_calls)

    result = mcp_server.register_product(
        name=name,
        price=price,
        category_id=_CLOTHING_CATEGORY,
        preview_confirmed=True,
    )

    assert result["ok"] is True, f"전부 유효한 prepared 이미지인데 거부되었다: {result}"
    assert len(naver_calls) == 1, f"네이버 API 가 1회 호출되어야 한다 (현재={len(naver_calls)}회)"
    # 대표 이미지가 첫 번째 URL 이어야 한다 (조용한 승격 회귀 확인).
    sent = naver_calls[0]
    rep_url = (
        sent.get("originProduct", {})
        .get("images", {})
        .get("representativeImage", {})
        .get("url", "")
    )
    assert (
        rep_url == "https://cdn/first.jpg"
    ), f"대표 이미지가 첫 번째 URL 이 아니다 (조용한 승격 회귀): {rep_url!r}"
    assert "detail_html" in result.get("filled_from_prepared", [])
    assert "image_urls" in result.get("filled_from_prepared", [])


# ---------------------------------------------------------------------------
# (d) 같은 이름·가격의 두 상품에서 명시 product_key 를 주면 올바른 prepared
#     가 쓰인다 (전송 페이로드의 상세HTML·이미지로 대조).
# ---------------------------------------------------------------------------


def test_d_explicit_product_key_selects_correct_prepared(isolated_prepared_dir, monkeypatch):
    """명시 product_key 로 같은 이름·가격 충돌에서 올바른 prepared 를 고른다."""
    shared_name = "동일상품명옵션"
    shared_price = 50000

    pkey_a = register.make_product_key(shared_name + "_A", shared_price)
    pkey_b = register.make_product_key(shared_name + "_B", shared_price)

    prepared_a = _make_prepared_payload(
        pkey_a,
        shared_name,
        shared_price,
        listing_urls=["https://cdn/product_a.jpg"],
        detail_html="<p>PRODUCT_A_DETAIL</p>",
    )
    prepared_b = _make_prepared_payload(
        pkey_b,
        shared_name,
        shared_price,
        listing_urls=["https://cdn/product_b.jpg"],
        detail_html="<p>PRODUCT_B_DETAIL</p>",
    )
    register.write_prepared_payload(prepared_a)
    register.write_prepared_payload(prepared_b)

    naver_calls: list = []
    _setup_gate_and_naver(monkeypatch, naver_calls)

    # 명시적으로 B 의 키를 준다 — 유도 키가 아니라 B 의 prepared 가 쓰여야 한다.
    result = mcp_server.register_product(
        name=shared_name,
        price=shared_price,
        category_id=_CLOTHING_CATEGORY,
        product_key=pkey_b,
        preview_confirmed=True,
    )

    assert result["ok"] is True, f"명시 product_key 등록이 실패했다: {result}"
    assert len(naver_calls) == 1
    sent = naver_calls[0]
    sent_detail = sent.get("originProduct", {}).get("detailContent", "")
    sent_rep_url = (
        sent.get("originProduct", {})
        .get("images", {})
        .get("representativeImage", {})
        .get("url", "")
    )
    assert (
        "PRODUCT_B_DETAIL" in sent_detail
    ), f"B 의 상세HTML 이 전송되어야 한다 (현재={sent_detail!r})"
    assert (
        sent_rep_url == "https://cdn/product_b.jpg"
    ), f"B 의 대표 이미지가 전송되어야 한다 (현재={sent_rep_url!r})"


# ---------------------------------------------------------------------------
# (e) 키를 명시하지 않은 기존 호출이 여전히 동작한다(하위호환).
# ---------------------------------------------------------------------------


def test_e_no_explicit_key_backward_compatible(isolated_prepared_dir, monkeypatch):
    """product_key 를 주지 않아도 name+price 유도로 동작한다 (하위호환)."""
    name = "하위호환이름"
    price = 25000
    pkey = register.make_product_key(name, price)
    prepared = _make_prepared_payload(
        pkey,
        name,
        price,
        listing_urls=["https://cdn/legacy.jpg"],
        detail_html="<p>LEGACY_DETAIL</p>",
    )
    register.write_prepared_payload(prepared)

    naver_calls: list = []
    _setup_gate_and_naver(monkeypatch, naver_calls)

    result = mcp_server.register_product(
        name=name,
        price=price,
        category_id=_CLOTHING_CATEGORY,
        # product_key 생략 — 하위호환 경로.
        preview_confirmed=True,
    )

    assert result["ok"] is True, f"하위호환 경로가 실패했다: {result}"
    assert len(naver_calls) == 1
    assert "detail_html" in result.get("filled_from_prepared", [])


# ---------------------------------------------------------------------------
# (f) 이름+가격 유도로 prepared 를 가져왔을 때 무엇을 어디서 가져왔는지
#     반환값에 드러난다(조용한 치환 없음).
# ---------------------------------------------------------------------------


def test_f_derived_key_surfaces_prepared_lookup(isolated_prepared_dir, monkeypatch):
    """유도 키 경로에서 prepared_lookup 이 어디서 무엇을 가져왔는지 드러낸다."""
    name = "유도키추적이름"
    price = 40000
    pkey = register.make_product_key(name, price)
    prepared = _make_prepared_payload(
        pkey,
        name,
        price,
        listing_urls=["https://cdn/tracked.jpg"],
        detail_html="<p>TRACKED_DETAIL</p>",
    )
    register.write_prepared_payload(prepared)

    naver_calls: list = []
    _setup_gate_and_naver(monkeypatch, naver_calls)

    result = mcp_server.register_product(
        name=name,
        price=price,
        category_id=_CLOTHING_CATEGORY,
        # product_key 생략 → 유도 키 사용.
        preview_confirmed=True,
    )

    assert result["ok"] is True, f"유도 키 경로 등록이 실패했다: {result}"
    lookup = result.get("prepared_lookup") or {}
    assert lookup, "유도 키를 썼을 때 prepared_lookup 이 비어 있으면 안 된다"
    assert (
        lookup.get("key") == pkey
    ), f"prepared_lookup.key 가 유도 키와 일치해야 한다 (현재={lookup.get('key')!r})"
    assert (
        lookup.get("source") == "derived"
    ), f"prepared_lookup.source 가 'derived' 여야 한다 (현재={lookup.get('source')!r})"
    assert (
        lookup.get("name") == name
    ), f"prepared_lookup.name 이 가져온 상품명이어야 한다 (현재={lookup.get('name')!r})"
    assert (
        lookup.get("salePrice") == price
    ), f"prepared_lookup.salePrice 가 가져온 가격이어야 한다 (현재={lookup.get('salePrice')!r})"
