# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""같은 이름·가격, 서로 다른 상품 간의 prepared 키 충돌을 검증한다.

핵심 결함: 이름·가격만으로 product_key 를 유도하면 색상만 다른 SKU 처럼
이름·가격이 같은 서로 다른 상품이 같은 키를 받는다. 그래서 두 번째 준비가
첫 번째를 조용히 덮고, 명시 키를 넘겨도 가리킬 대상이 하나뿐이라 아무것도
해결되지 않는다. 본 테스트는 그 근본 원인을 고정한 것을 단언한다.

검증 항목 (모두 COMMERCE_DRY_RUN 이 꺼진 상태에서, 전송 페이로드를 가로채
실제 내용으로 단언한다):

  (a) 같은 이름·가격, 다른 이미지·다른 카테고리 상품 둘을 준비 → 키가 다르다.
  (b) 완전히 같은 입력을 두 번 준비 → 키가 같다 (결정론).
  (c) 후보가 2개인 상태에서 product_key 없이 등록 → 거부, 네이버 호출 0회,
      사유에 product_key 지정 안내가 있다.
  (d) 같은 상황에서 명시 키를 주면 그 상품의 상세HTML·이미지가 전송된다
      (다른 상품 것이 아님을 전송 페이로드로 대조).
  (e) 후보가 1개면 키 없이도 기존대로 동작한다 (하위호환).
  (f) prepared_lookup 이 컴플라이언스 차단 경로에서도 None 이 아니다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import common, mcp_server, naver_client, qa_agents, register

# ---------------------------------------------------------------------------
# 공통 픽스처·상수.
# ---------------------------------------------------------------------------

_CLOTHING_CATEGORY = "50021299"
_KC_CATEGORY = "50000151"

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


def _fake_attach_for(urls_per_source):
    """source 인덱스별로 고정 CDN URL 을 반환하는 attach_images 대체.

    args:
        urls_per_source: source 개수만큼의 URL 문자열 리스트. source 순서대로
            각 source 에 대해 해당 URL 을 반환한다(1:1 매핑).
    """

    def _attach(sources):
        urls = [urls_per_source[i] for i in range(len(sources))]
        return {"urls": urls, "rejected": [], "notes": []}

    return _attach


def _make_prepared_payload(
    product_key,
    name,
    price,
    listing_urls,
    detail_html,
    *,
    category_id=_CLOTHING_CATEGORY,
    qa=None,
):
    """테스트용 prepared payload dict 를 만든다."""
    return {
        "product_key": product_key,
        "product": {
            "name": name,
            "categoryId": category_id,
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
    """게이트 통과 + 네이버 호출 가로채기 + config mock (DRY_RUN 꺼짐)."""
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
# (a) 같은 이름·가격, 다른 이미지·다른 카테고리 → 키가 다르다.
# ---------------------------------------------------------------------------


def test_a_distinct_inputs_get_distinct_keys(isolated_prepared_dir, monkeypatch):
    """이름·가격이 같아도 카테고리·이미지가 다르면 키가 다르다."""
    shared_name = "컬러반팔티"
    shared_price = 19000

    # 상품 A: 의류 카테고리, 이미지 소스 red.png
    monkeypatch.setattr(
        "clossify.images.attach_images",
        _fake_attach_for(["https://cdn/red_a.jpg"]),
    )
    payload_a = register.prepare_listing(
        {
            "name": shared_name,
            "salePrice": shared_price,
            "image_sources": ["red.png"],
            "category_id": _CLOTHING_CATEGORY,
            "notice": {
                "productInfoProvidedNoticeType": "WEAR",
                "etc": {
                    "material": "면 100%",
                    "color": "레드",
                    "size": "FREE",
                    "caution": "물세탁",
                    "packDateText": "2024-01-01",
                    "warrantyPolicy": "7일교환",
                },
            },
        }
    )

    # 상품 B: 가전(KC) 카테고리, 이미지 소스 blue.png
    monkeypatch.setattr(
        "clossify.images.attach_images",
        _fake_attach_for(["https://cdn/blue_b.jpg"]),
    )
    payload_b = register.prepare_listing(
        {
            "name": shared_name,
            "salePrice": shared_price,
            "image_sources": ["blue.png"],
            "category_id": _KC_CATEGORY,
        }
    )

    key_a = payload_a["product_key"]
    key_b = payload_b["product_key"]
    assert key_a != key_b, (
        f"이름·가격이 같고 카테고리·이미지가 다른데 키가 같다 (충돌): " f"A={key_a} B={key_b}"
    )


# ---------------------------------------------------------------------------
# (b) 완전히 같은 입력을 두 번 준비 → 키가 같다 (결정론).
# ---------------------------------------------------------------------------


def test_b_identical_inputs_get_identical_keys(isolated_prepared_dir, monkeypatch):
    """같은 입력을 두 번 준비하면 같은 키가 나온다 (결정론 유지)."""
    shared_name = "결정론체크니트"
    shared_price = 33000
    attach = _fake_attach_for(["https://cdn/determ_1.jpg", "https://cdn/determ_2.jpg"])
    monkeypatch.setattr("clossify.images.attach_images", attach)

    base = {
        "name": shared_name,
        "salePrice": shared_price,
        "image_sources": ["d1.png", "d2.png"],
        "category_id": _CLOTHING_CATEGORY,
        "notice": {
            "productInfoProvidedNoticeType": "WEAR",
            "etc": {
                "material": "면 100%",
                "color": "블랙",
                "size": "FREE",
                "caution": "물세탁",
                "packDateText": "2024-01-01",
                "warrantyPolicy": "7일교환",
            },
        },
    }

    payload_first = register.prepare_listing(dict(base))
    payload_second = register.prepare_listing(dict(base))

    assert (
        payload_first["product_key"] == payload_second["product_key"]
    ), "같은 입력인데 키가 다르다 (결정론 위반 — 재실행이 새 항목을 만든다)"


# ---------------------------------------------------------------------------
# (c) 후보 2개 + product_key 없이 등록 → 거부, 네이버 호출 0회,
#     사유에 product_key 지정 안내.
# ---------------------------------------------------------------------------


def test_c_ambiguous_candidates_refused_without_naver_call(isolated_prepared_dir, monkeypatch):
    """같은 이름·가격 후보가 2개일 때 키 없이 등록하면 거부한다 (조용한 선택 금지)."""
    shared_name = "애매한양말"
    shared_price = 9900

    # 후보 A — 의류 + red 이미지.
    monkeypatch.setattr(
        "clossify.images.attach_images", _fake_attach_for(["https://cdn/ambig_a.jpg"])
    )
    pa = register.prepare_listing(
        {
            "name": shared_name,
            "salePrice": shared_price,
            "image_sources": ["a.png"],
            "category_id": _CLOTHING_CATEGORY,
        }
    )
    key_a = pa["product_key"]

    # 후보 B — 가전 + blue 이미지 (이름·가격은 같음).
    monkeypatch.setattr(
        "clossify.images.attach_images", _fake_attach_for(["https://cdn/ambig_b.jpg"])
    )
    pb = register.prepare_listing(
        {
            "name": shared_name,
            "salePrice": shared_price,
            "image_sources": ["b.png"],
            "category_id": _KC_CATEGORY,
        }
    )
    key_b = pb["product_key"]
    assert key_a != key_b, "전제: 두 후보는 서로 다른 키를 받아야 한다"

    # 두 후보 모두 PASS QA 로 만든다 (차단 없이 후보 스캔 단계까지 도달하게).
    for pkey in (key_a, key_b):
        _p = register.load_prepared_payload(product_key=pkey)
        _p["qa"] = _pass_qa()
        register.write_prepared_payload(_p)

    naver_calls: list = []
    _setup_gate_and_naver(monkeypatch, naver_calls)

    result = mcp_server.register_product(
        name=shared_name,
        price=shared_price,
        category_id=_CLOTHING_CATEGORY,
        # product_key 생략 → 후보 2개 → 거부되어야 한다.
    )

    assert result["ok"] is False, "후보가 2개인데 조용히 진행되었다 (조용한 선택)"
    assert len(naver_calls) == 0, f"모호한 상태에서 네이버 API 가 호출되었다: {len(naver_calls)}회"
    # 사유에 product_key 지정 안내가 있어야 한다.
    blob = str(result)
    assert "product_key" in blob, f"거부 사유에 product_key 지정 안내가 없다: {result!r}"


# ---------------------------------------------------------------------------
# (d) 같은 상황에서 명시 키를 주면 그 상품의 상세HTML·이미지가 전송된다.
# ---------------------------------------------------------------------------


def test_d_explicit_key_sends_that_products_content(isolated_prepared_dir, monkeypatch):
    """후보가 2개여도 명시 키를 주면 해당 상품의 내용이 전송된다."""
    shared_name = "명시키양말"
    shared_price = 9900

    monkeypatch.setattr("clossify.images.attach_images", _fake_attach_for(["https://cdn/d_a.jpg"]))
    pa = register.prepare_listing(
        {
            "name": shared_name,
            "salePrice": shared_price,
            "image_sources": ["a.png"],
            "category_id": _CLOTHING_CATEGORY,
        }
    )
    key_a = pa["product_key"]
    html_a = pa["detail_html"]

    monkeypatch.setattr("clossify.images.attach_images", _fake_attach_for(["https://cdn/d_b.jpg"]))
    pb = register.prepare_listing(
        {
            "name": shared_name,
            "salePrice": shared_price,
            "image_sources": ["b.png"],
            "category_id": _KC_CATEGORY,
        }
    )
    key_b = pb["product_key"]
    html_b = pb["detail_html"]
    assert key_a != key_b

    for pkey in (key_a, key_b):
        _p = register.load_prepared_payload(product_key=pkey)
        _p["qa"] = _pass_qa()
        register.write_prepared_payload(_p)

    naver_calls: list = []
    _setup_gate_and_naver(monkeypatch, naver_calls)

    # 명시적으로 B 의 키를 준다 — A 것이 아닌 B 의 내용이 전송되어야 한다.
    result = mcp_server.register_product(
        name=shared_name,
        price=shared_price,
        category_id=_KC_CATEGORY,
        product_key=key_b,
    )

    assert result["ok"] is True, f"명시 키 등록이 실패했다: {result}"
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
        sent_detail == html_b
    ), f"B 의 상세HTML 이 전송되어야 한다 (현재={sent_detail!r}, 기대={html_b!r})"
    assert (
        sent_rep_url == "https://cdn/d_b.jpg"
    ), f"B 의 대표 이미지가 전송되어야 한다 (현재={sent_rep_url!r})"
    # A 의 내용이 전송되지 않았음을 확인.
    assert (
        html_a not in sent_detail or sent_detail != html_a
    ), "A 의 상세HTML 이 전송되었다 (잘못된 상품 선택)"


# ---------------------------------------------------------------------------
# (e) 후보가 1개면 키 없이도 기존대로 동작한다 (하위호환).
# ---------------------------------------------------------------------------


def test_e_single_candidate_works_without_explicit_key(isolated_prepared_dir, monkeypatch):
    """후보가 1개일 때 product_key 생략은 기존대로 동작한다."""
    name = "단일후보티"
    price = 27000

    monkeypatch.setattr(
        "clossify.images.attach_images", _fake_attach_for(["https://cdn/single.jpg"])
    )
    pa = register.prepare_listing(
        {
            "name": name,
            "salePrice": price,
            "image_sources": ["s.png"],
            "category_id": _CLOTHING_CATEGORY,
        }
    )
    pkey = pa["product_key"]
    _p = register.load_prepared_payload(product_key=pkey)
    _p["qa"] = _pass_qa()
    register.write_prepared_payload(_p)

    naver_calls: list = []
    _setup_gate_and_naver(monkeypatch, naver_calls)

    result = mcp_server.register_product(
        name=name,
        price=price,
        category_id=_CLOTHING_CATEGORY,
    )

    assert result["ok"] is True, f"단일 후보 하위호환이 실패했다: {result}"
    assert len(naver_calls) == 1


# ---------------------------------------------------------------------------
# (f) prepared_lookup 이 컴플라이언스 차단 경로에서도 None 이 아니다.
# ---------------------------------------------------------------------------


def test_f_prepared_lookup_present_on_compliance_block(isolated_prepared_dir, monkeypatch):
    """컴플라이언스 차단 응답에도 prepared_lookup 이 None 이 아니다."""
    name = "컴플차단티"
    price = 45000

    monkeypatch.setattr("clossify.images.attach_images", _fake_attach_for(["https://cdn/cb.jpg"]))
    pa = register.prepare_listing(
        {
            "name": name,
            "salePrice": price,
            "image_sources": ["cb.png"],
            "category_id": _CLOTHING_CATEGORY,
        }
    )
    pkey = pa["product_key"]

    naver_calls: list = []
    monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
    monkeypatch.setattr(
        naver_client,
        "register_product",
        lambda payload: naver_calls.append(payload) or (200, {"originProductNo": "X"}),
    )
    # 컴플라이언스 게이트가 무조건 차단하도록 mock.
    monkeypatch.setattr(
        mcp_server,
        "_run_compliance_gate",
        lambda *a, **kw: {
            "blocked": True,
            "violations": [{"rule": "고시 필수필드", "detail": "material 누락"}],
            "needs_user": [{"field": "material", "label": "소재", "why": "필수"}],
            "pending_reviews": [],
        },
    )
    monkeypatch.setattr(naver_client, "_notice_config", lambda: _NOTICE_CFG_FULL)
    monkeypatch.setattr(naver_client, "_kc_config", lambda: ({}, ""))

    result = mcp_server.register_product(
        name=name,
        price=price,
        category_id=_CLOTHING_CATEGORY,
    )

    assert result["ok"] is False
    assert result.get("blocked_by") == "compliance"
    lookup = result.get("prepared_lookup")
    assert lookup is not None, "컴플라이언스 차단 경로에서 prepared_lookup 이 None 이다"
    assert isinstance(lookup, dict)
    assert (
        lookup.get("key") == pkey
    ), f"prepared_lookup.key 가 prepared 의 키와 일치해야 한다 (현재={lookup.get('key')!r})"
