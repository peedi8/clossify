# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""미리보기에서 '이 구성을 템플릿으로' (GUI 관문).

본 파일은 "미리보기를 본 뒤 **재준비 없이** prepared payload 를 템플릿으로
저장하는 경로" 의 과업 (a)-(h) 를 검증한다. 핵심 위험:

  - **재준비/네트워크 금지**: prepared 가 이미 만들어졌으므로, 저장 경로에서
    이미지 재업로드·카테고리 재조회·고시 타입 재추론 이 일어나면 안 된다.
    네이버 API 라이브 호출은 **0회** 여야 한다.
  - **출처 기록**: 규제값이므로 템플릿 엔트리에 "이 값이 어디서 왔는지" 가
    ``prepared:<product_key>`` 형태로 기록되어야 한다.
  - **완전성 형식 일치**: ``get_product`` 경로의 template_saved.completeness 와
    **같은 형식**({filled_count, type_field_total, missing_fields}) 이어야 한다.
  - **안전**: 비밀값·상품명·가격·이미지·재고는 어떤 형태로든 담기지 않는다.
  - **view_only 패널 보호**: 보기 전용 HTML 은 ``<script>``·``<button>``·
    ``<input>``·``onclick``·``addEventListener`` 가 **0개**.
  - **interactive 브라우저 창 보호**: ``fetch(`` 가 **0개**. 승인 폼(POST +
    hidden 토큰) 은 그대로.
  - **MCP 도구 수 유지**: 7개. 새 도구를 만들지 않는다.
  - **이름 없으면 저장 안 함**: ``save_prepared_as_template`` 가 빈 문자열이면
    ``template_saved`` 는 None (암묵 저장 금지).

모든 테스트는 ``common.STATE_DIR`` 을 ``tmp_path`` 로 격리한다. 네이버 라이브
호출은 0회.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import pytest

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import (
    common,
    listing_templates,
    mcp_server,
    naver_client,
    preview,
    qa_agents,
    register,
)


# --------------------------------------------------------------------------- #
# 공통 픽스처 / 헬퍼.
# --------------------------------------------------------------------------- #
@pytest.fixture
def isolated_state_dir(tmp_path, monkeypatch):
    """``common.STATE_DIR`` 을 tmp_path 로 격리.

    ``listing_templates.templates_path`` 가 호출 시점에 ``common.STATE_DIR`` 을
    읽으므로, monkeypatch 로 교체한다. ``PREPARED_DIR`` 도 함께 격리한다.
    """
    fake_state = tmp_path / ".local"
    fake_state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(common, "STATE_DIR", fake_state)
    monkeypatch.setattr(common, "LOCAL_DIR", fake_state)
    prepared = fake_state / "prepared"
    prepared.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(common, "PREPARED_DIR", prepared)
    return fake_state


# notice_config mock — 원산지/AS/제조사 + 공통 필드.
_NOTICE_MOCK = {
    "origin_area_code": "04",
    "origin_content": "중국",
    "as_tel": "070-1234-5678",
    "manufacturer": "테스트제조사",
}


def _make_prepared_payload(
    pkey: str,
    *,
    notice_type: str = "ETC",
    notice_fields: dict | None = None,
    name: str = "상품명-비밀-이값은-템플릿에-없어야함",
    sale_price: int = 99000,
    image_urls: list[str] | None = None,
) -> dict:
    """단위 테스트용 prepared payload dict 를 만들어 디스크에 쓴다.

    본 헬퍼는 ``register.prepare_listing`` 을 거치지 **않**는다 — prepared 가
    이미 있다는 전제 아래, 본 티켓의 경로가 재준비 없이 payload 를 읽는 것을
    증명하기 위함이다. 네이버 API 호출을 일체 하지 않는다.
    """
    base_fields = {
        "returnCostReason": "단순변심 반품 배송비 구매자부담",
        "noRefundReason": "주문제작 상품 청약철회 제한",
        "qualityAssuranceStandard": "관련 법령에 따름",
        "compensationProcedure": "소비자분쟁해결기준",
        "troubleShootingContents": "A/S 책임자: 070-0000-0000",
    }
    if notice_fields:
        base_fields.update(notice_fields)
    node_key = notice_type.lower() if notice_type else "etc"
    product = {
        "name": name,
        "salePrice": sale_price,
        "categoryId": "50021299",
        "stockQuantity": 50,
        "tags": ["비밀태그-이값도-템플릿에-없어야함"],
        "notice": {
            "productInfoProvidedNoticeType": notice_type,
            node_key: dict(base_fields),
        },
        "return_cost_reason": base_fields["returnCostReason"],
        "as_tel": "070-1234-5678",
        "origin_code": "04",
        "made_in": "중국",
        "manufacturer": "테스트제조사",
        # 비밀값 — 어떤 형태든 담기면 안 된다.
        "client_secret": "PREPARED-SECRET-CANARY",
        "access_token": "PREPARED-TOKEN-CANARY",
    }
    agent_rows = [
        qa_agents._qa_agent_result("image", "PASS", [], "PASS"),
        qa_agents._qa_agent_result("copy", "PASS", [], "PASS"),
    ]
    qa = qa_agents.aggregate_qa_results(agent_rows)
    payload = {
        "product_key": pkey,
        "version": common.PREPARED_PAYLOAD_VERSION,
        "product": product,
        "images": {
            "listing_urls": image_urls or ["http://cdn.example/비밀이미지.png"],
            "detail_urls": [],
        },
        "detail_html": "<html><body>비밀상세</body></html>",
        "qa": qa,
        "needs_llm": [],
        "needs_user": [],
    }
    register.write_prepared_payload(payload)
    return payload


def _make_prepared_payload_no_qa(pkey: str, **kwargs) -> dict:
    """QA 가 없는 최소 prepared payload (QA 게이트 전 단계 검증용)."""
    payload = _make_prepared_payload(pkey, **kwargs)
    payload["qa"] = qa_agents.aggregate_qa_results([])
    register.write_prepared_payload(payload)
    return payload


# =========================================================================== #
# (a) 네트워크 호출 0회 — prepared 를 템플릿으로 저장할 때 라이브/재준비 없음.
# =========================================================================== #
class TestNetworkZeroOnPreparedToTemplate:
    """submit_reviews(save_prepared_as_template=...) 가 네트워크를 쓰지 않는가."""

    def test_zero_naver_live_calls_during_template_save(self, isolated_state_dir, monkeypatch):
        """prepared → template 저장 중 네이버 API 라이브 호출은 0회."""
        # naver_client.get_product / register_product / upload_images 를 카운팅
        # 목으로 대체 — 라이브가 아닌 목만 호출되어야 한다.
        get_calls = {"count": 0}
        register_calls = {"count": 0}
        upload_calls = {"count": 0}

        def _fake_get(*a, **kw):
            get_calls["count"] += 1
            return 200, {}

        def _fake_register(*a, **kw):
            register_calls["count"] += 1
            return 200, {}

        def _fake_upload(*a, **kw):
            upload_calls["count"] += 1
            return {"urls": [], "rejected": [], "notes": []}

        monkeypatch.setattr(naver_client, "get_product", _fake_get)
        monkeypatch.setattr(naver_client, "register_product", _fake_register)
        # upload_images 는 naver_client 가 아닌 별도 모듈에 있을 수 있으나,
        # 라이브 경로 자체가 닫혀야 한다 — 최소한 naver_client 호출은 0.
        pkey = register.make_product_key("네트워크0회", 10000)
        _make_prepared_payload(pkey)
        result = mcp_server.submit_reviews(
            product_key=pkey,
            reviews=[{"agent": "image", "verdict": "PASS"}],
            save_prepared_as_template="네트워크0회-템플릿",
        )
        assert result["ok"] is True, f"submit_reviews 실패: {result.get('error')}"
        ts = result.get("template_saved")
        assert ts is not None and ts["ok"] is True, f"템플릿 저장 실패: {ts}"
        # 네이버 API 라이브 호출은 0회.
        assert get_calls["count"] == 0, "템플릿 저장 중 get_product 라이브 호출 발생"
        assert register_calls["count"] == 0, "템플릿 저장 중 register_product 라이브 호출 발생"


# =========================================================================== #
# (b) 출처 기록 — source.origin_product_no 가 prepared:<product_key> 형태.
# =========================================================================== #
class TestSourceProvenanceRecorded:
    """템플릿 엔트리에 출처(prepared:<product_key>) 가 기록되는가."""

    def test_source_origin_product_no_is_prepared_prefix(self, isolated_state_dir, monkeypatch):
        pkey = register.make_product_key("출처테스트", 10000)
        _make_prepared_payload(pkey)
        result = mcp_server.submit_reviews(
            product_key=pkey,
            reviews=[{"agent": "image", "verdict": "PASS"}],
            save_prepared_as_template="출처-기록",
        )
        ts = result["template_saved"]
        assert ts["ok"] is True
        assert ts.get("source_recorded") is True
        # 저장 엔트리 파일에 source.origin_product_no 가 prepared:<key> 형태.
        store = json.loads(listing_templates.templates_path().read_text(encoding="utf-8"))
        entry = next(
            (t for t in store["templates"] if t["name"] == "출처-기록"),
            None,
        )
        assert entry is not None
        source = entry.get("source") or {}
        assert source.get("origin_product_no", "").startswith("prepared:")
        assert (
            pkey in source["origin_product_no"]
        ), f"origin_product_no 에 product_key 가 없음: {source.get('origin_product_no')}"
        # read_at 도 있어야 한다(ISO 시각 문자열).
        assert source.get("read_at")


# =========================================================================== #
# (c) 완전성 형식 일치 — {filled_count, type_field_total, missing_fields}.
# =========================================================================== #
class TestCompletenessFormatMatch:
    """template_saved.completeness 가 get_product 경로와 같은 형식인가."""

    def test_completeness_has_three_keys(self, isolated_state_dir, monkeypatch):
        pkey = register.make_product_key("완전성형식", 10000)
        _make_prepared_payload(pkey, notice_type="ETC")
        result = mcp_server.submit_reviews(
            product_key=pkey,
            reviews=[{"agent": "image", "verdict": "PASS"}],
            save_prepared_as_template="완전성-형식",
        )
        ts = result["template_saved"]
        comp = ts.get("completeness")
        assert comp is not None, "template_saved 에 completeness 가 없음"
        assert set(comp.keys()) >= {
            "filled_count",
            "type_field_total",
            "missing_fields",
        }, f"completeness 키 불일치: {comp.keys()}"
        assert isinstance(comp["filled_count"], int)
        assert isinstance(comp["type_field_total"], int)
        assert isinstance(comp["missing_fields"], list)

    def test_completeness_filled_count_positive_for_etc(self, isolated_state_dir, monkeypatch):
        """ETC 공통 5필드가 채워진 prepared → filled_count > 0."""
        pkey = register.make_product_key("완전성-양수", 10000)
        _make_prepared_payload(pkey, notice_type="ETC")
        result = mcp_server.submit_reviews(
            product_key=pkey,
            reviews=[{"agent": "image", "verdict": "PASS"}],
            save_prepared_as_template="완전성-양수",
        )
        ts = result["template_saved"]
        comp = ts["completeness"]
        assert comp["filled_count"] > 0, f"ETC 공통 5필드가 채워졌는데 filled_count=0: {comp}"
        # ETC 정본 필드 수(>0).
        assert comp["type_field_total"] > 0


# =========================================================================== #
# (d) 비밀값/상품특정값 제외 — whitelist 가 동작한다.
# =========================================================================== #
class TestSecretsAndProductValuesExcluded:
    """prepared 의 비밀값/상품명/가격/이미지/재고가 템플릿에 담기지 않는가."""

    def test_product_name_price_stock_image_not_in_template(self, isolated_state_dir, monkeypatch):
        pkey = register.make_product_key("비밀값제외", 10000)
        _make_prepared_payload(pkey)
        mcp_server.submit_reviews(
            product_key=pkey,
            reviews=[{"agent": "image", "verdict": "PASS"}],
            save_prepared_as_template="비밀-제외",
        )
        raw = listing_templates.templates_path().read_text(encoding="utf-8")
        # 상품명·가격·재고·이미지URL·태그 가 템플릿 파일에 없다.
        assert "상품명-비밀-이값은-템플릿에-없어야함" not in raw
        assert "99000" not in raw  # salePrice
        assert "http://cdn.example/비밀이미지.png" not in raw
        assert "비밀태그-이값도-템플릿에-없어야함" not in raw

    def test_secret_tokens_not_in_template(self, isolated_state_dir, monkeypatch):
        pkey = register.make_product_key("토큰제외", 10000)
        _make_prepared_payload(pkey)
        mcp_server.submit_reviews(
            product_key=pkey,
            reviews=[{"agent": "image", "verdict": "PASS"}],
            save_prepared_as_template="토큰-제외",
        )
        raw = listing_templates.templates_path().read_text(encoding="utf-8")
        assert "PREPARED-SECRET-CANARY" not in raw
        assert "PREPARED-TOKEN-CANARY" not in raw

    def test_return_cost_reason_stored(self, isolated_state_dir, monkeypatch):
        """반면에 안전한 규제값(return_cost_reason 등)은 담겨야 한다."""
        pkey = register.make_product_key("규제값포함", 10000)
        _make_prepared_payload(pkey)
        mcp_server.submit_reviews(
            product_key=pkey,
            reviews=[{"agent": "image", "verdict": "PASS"}],
            save_prepared_as_template="규제값-포함",
        )
        raw = listing_templates.templates_path().read_text(encoding="utf-8")
        assert "단순변심 반품 배송비 구매자부담" in raw


# =========================================================================== #
# (e) view_only HTML 제약 — script/button/input/onclick/addEventListener 0개.
# =========================================================================== #
class TestViewOnlyHtmlConstraints:
    """view_only 모드의 "템플릿 저장 안내" 가 HTML 계약을 깨지 않는가."""

    def test_view_only_has_template_save_phrase(self):
        """view_only HTML 에 '이 구성을 템플릿으로' 안내가 있다."""
        payload = {
            "product": {
                "name": "X",
                "salePrice": 1000,
                "categoryId": "50021299",
                "notice": {"etc": {"returnCostReason": "x"}},
            },
            "images": {"listing_urls": []},
            "detail_html": "",
        }
        html = preview.render_preview_html(payload, product_key="votemplate1", mode="view_only")
        assert "템플릿으로 저장할 수 있" in html, "view_only HTML 에 템플릿 저장 안내가 없음"

    def test_view_only_still_zero_dead_ui(self):
        """안내가 추가돼도 script/button/input/onclick/addEventListener 는 0개."""
        payload = {
            "product": {
                "name": "X",
                "salePrice": 1000,
                "categoryId": "50021299",
                "notice": {"etc": {"returnCostReason": "x"}},
            },
            "images": {"listing_urls": []},
            "detail_html": "",
        }
        html = preview.render_preview_html(payload, product_key="votemplate2", mode="view_only")
        assert "<script" not in html.lower(), "view_only HTML 에 <script> 가 있음"
        assert "<button" not in html.lower(), "view_only HTML 에 <button> 이 있음"
        assert "<input" not in html.lower(), "view_only HTML 에 <input> 이 있음"
        assert not re.search(
            r"\sonclick\s*=", html, re.IGNORECASE
        ), "view_only HTML 에 onclick 이 있음"
        assert "addEventListener" not in html, "view_only HTML 에 addEventListener 텍스트가 있음"

    def test_view_only_no_form_element(self):
        """``<form>`` 도 0개 — 패널은 폼을 제출할 수 없다."""
        payload = {
            "product": {
                "name": "X",
                "salePrice": 1000,
                "notice": {"etc": {"returnCostReason": "x"}},
            },
            "images": {"listing_urls": []},
            "detail_html": "",
        }
        html = preview.render_preview_html(payload, product_key="votemplate3", mode="view_only")
        assert "<form" not in html.lower()


# =========================================================================== #
# (f) interactive HTML 제약 — fetch 0개, 승인 폼(POST + hidden) 유지.
# =========================================================================== #
class TestInteractiveHtmlConstraints:
    """interactive 모드의 "템플릿 저장 안내 바" 가 회귀를 일으키지 않는가."""

    def test_interactive_has_template_save_phrase(self):
        """interactive HTML 에 '이 구성을 템플릿으로' 안내 바가 있다."""
        payload = {
            "product": {
                "name": "X",
                "salePrice": 1000,
                "categoryId": "50021299",
                "notice": {"etc": {"returnCostReason": "x"}},
            },
            "images": {"listing_urls": []},
            "detail_html": "",
        }
        html = preview.render_preview_html(payload, product_key="inttemplate1", mode="interactive")
        assert "템플릿으로 저장할 수 있" in html

    def test_interactive_zero_fetch(self):
        """interactive HTML 전체에 ``fetch(`` 가 0개(회귀 금지)."""
        payload = {
            "product": {
                "name": "X",
                "salePrice": 1000,
                "notice": {"etc": {"returnCostReason": "x"}},
            },
            "images": {"listing_urls": []},
            "detail_html": "",
        }
        html = preview.render_preview_html(
            payload,
            product_key="inttemplate2",
            approval_token="dummy",
            approval_port=54321,
            mode="interactive",
        )
        assert "fetch(" not in html, "interactive HTML 에 fetch() 가 있음 — 회귀"

    def test_interactive_approval_form_preserved(self):
        """승인 폼(POST + hidden 토큰) 이 그대로 있다(회귀)."""
        payload = {
            "product": {
                "name": "X",
                "salePrice": 1000,
                "notice": {"etc": {"returnCostReason": "x"}},
            },
            "images": {"listing_urls": []},
            "detail_html": "",
        }
        html = preview.render_preview_html(
            payload,
            product_key="inttemplate3",
            approval_token="dummytoken",
            approval_port=54321,
            mode="interactive",
        )
        assert '<form id="approval-form"' in html
        assert 'method="POST"' in html
        assert 'name="token"' in html
        assert 'type="hidden"' in html

    def test_interactive_template_bar_has_no_new_form_or_script(self):
        """템플릿 안내 바가 새 폼/스크립트/입력을 만들지 않는다.

        클립보드 패턴을 **재사용** 한다 — 새 전송 경로가 없다. 안내 바 자체는
        순수 텍스트(<span>/<em>) 다.
        """
        payload = {
            "product": {
                "name": "X",
                "salePrice": 1000,
                "notice": {"etc": {"returnCostReason": "x"}},
            },
            "images": {"listing_urls": []},
            "detail_html": "",
        }
        html = preview.render_preview_html(payload, product_key="inttemplate4", mode="interactive")
        # 템플릿 안내 바만 추출 — 배경색 스타일(#eef6ff) 로 구별되는 두 번째
        # edit-bar 다. 첫 번째 edit-bar([수정사항 복사]) 의 버튼과 구별하기 위해
        # "이 구성을 템플릿으로" 문구가 있는 블록만 잡는다.
        match = re.search(
            r'<div class="edit-bar"[^>]*style="background:#eef6ff[^>]*>.*?</div>',
            html,
            re.DOTALL,
        )
        assert match is not None, "템플릿 저장 안내 바를 찾을 수 없음"
        bar = match.group(0)
        assert "이 구성을 템플릿으로" in bar, "추출한 바가 템플릿 안내 바가 아님"
        assert "<form" not in bar.lower(), "템플릿 안내 바에 <form> 이 있음 — 새 전송 경로"
        assert "<script" not in bar.lower(), "템플릿 안내 바에 <script> 가 있음"
        assert "<button" not in bar.lower(), "템플릿 안내 바에 <button> 이 있음"
        assert "<input" not in bar.lower(), "템플릿 안내 바에 <input> 이 있음"


# =========================================================================== #
# (g) MCP 도구 11개 유지.
# =========================================================================== #
class TestMcpToolCountUnchanged:
    """현재 MCP 도구 표면은 11개다."""

    EXPECTED_NAMES = frozenset(
        {
            "check_config",
            "upload_images",
            "register_product",
            "get_product",
            "delete_product",
            "prepare_listing",
            "submit_reviews",
            "manage_products",
            "get_category_attributes",
            "get_category_attribute_values",
            "suggest_product_attributes",
        }
    )

    def test_exactly_eleven_tools(self):
        tools = mcp_server.mcp.list_tools()
        if hasattr(tools, "__await__"):
            try:
                tools = asyncio.run(tools)
            except RuntimeError:
                tools = asyncio.get_event_loop().run_until_complete(tools)
        assert len(tools) == 11, f"도구 수가 11이 아님: {len(tools)}"

    def test_tool_names_unchanged(self):
        tools = mcp_server.mcp.list_tools()
        if hasattr(tools, "__await__"):
            try:
                tools = asyncio.run(tools)
            except RuntimeError:
                tools = asyncio.get_event_loop().run_until_complete(tools)
        names = {getattr(t, "name", None) for t in tools}
        assert names == self.EXPECTED_NAMES, f"도구 이름 불일치: {names}"


# =========================================================================== #
# (h) 이름 없으면 저장 안 함 — template_saved 는 None (암묵 저장 금지).
# =========================================================================== #
class TestNoSaveWithoutName:
    """save_prepared_as_template 가 빈 문자열이면 저장을 시도하지 않는다."""

    def test_empty_name_template_saved_is_none(self, isolated_state_dir, monkeypatch):
        pkey = register.make_product_key("이름없음", 10000)
        _make_prepared_payload(pkey)
        result = mcp_server.submit_reviews(
            product_key=pkey,
            reviews=[{"agent": "image", "verdict": "PASS"}],
            # save_prepared_as_template 생략 (= "")
        )
        assert result["ok"] is True
        # template_saved 가 None — 저장 시도 자체를 안 했다.
        assert result.get("template_saved") is None
        # 파일이 아예 없다.
        assert not listing_templates.templates_path().is_file()

    def test_whitespace_name_template_saved_is_none(self, isolated_state_dir, monkeypatch):
        pkey = register.make_product_key("공백이름", 10000)
        _make_prepared_payload(pkey)
        result = mcp_server.submit_reviews(
            product_key=pkey,
            reviews=[{"agent": "image", "verdict": "PASS"}],
            save_prepared_as_template="   ",
        )
        assert result.get("template_saved") is None
        assert not listing_templates.templates_path().is_file()

    def test_submit_reviews_contract_preserved_without_save(self, isolated_state_dir, monkeypatch):
        """이름 없을 때 기존 submit_reviews 규약(ok/qa/gate_allowed) 유지."""
        pkey = register.make_product_key("규약유지", 10000)
        _make_prepared_payload(pkey)
        result = mcp_server.submit_reviews(
            product_key=pkey,
            reviews=[{"agent": "image", "verdict": "PASS"}],
        )
        # 기존 키가 그대로 있다.
        assert "ok" in result
        assert "qa" in result
        assert "gate_allowed" in result
        assert "error" in result
        # template_saved 키도 있지만 값은 None.
        assert "template_saved" in result
        assert result["template_saved"] is None


# =========================================================================== #
# 부가: transform_prepared_to_template_input 직접 검증.
# =========================================================================== #
class TestTransformPreparedToTemplateInput:
    """listing_templates.transform_prepared_to_template_input 단위 검증."""

    def test_returns_ok_for_valid_dict(self):
        product = {
            "notice": {
                "productInfoProvidedNoticeType": "ETC",
                "etc": {
                    "returnCostReason": "단순변심 반품비 구매자부담",
                    "noRefundReason": "주문제작 청약철회 제한",
                },
            },
            "as_tel": "070-1234-5678",
        }
        result = listing_templates.transform_prepared_to_template_input(product)
        assert result["ok"] is True
        assert result["notice_type"] == "ETC"
        assert isinstance(result["product"], dict)
        comp = result["completeness"]
        assert comp["filled_count"] >= 2

    def test_returns_not_ok_for_non_dict(self):
        result = listing_templates.transform_prepared_to_template_input("not-a-dict")
        assert result["ok"] is False
        assert result["reason"]

    def test_completeness_format_matches_transform_product(self):
        """transform_product_to_template_input 과 같은 completeness 형식."""
        product = {
            "notice": {
                "productInfoProvidedNoticeType": "ETC",
                "etc": {"returnCostReason": "x"},
            }
        }
        r1 = listing_templates.transform_prepared_to_template_input(product)
        # transform_product_to_template_input 은 API 모양(originProduct.detailAttribute)을
        # 받으므로, 직접 같은 데이터를 API 모양으로 만들어 호출한다.
        api_shaped = {
            "originProduct": {
                "detailAttribute": {
                    "productInfoProvidedNotice": {
                        "productInfoProvidedNoticeType": "ETC",
                        "etc": {"returnCostReason": "x"},
                    }
                }
            }
        }
        r2 = listing_templates.transform_product_to_template_input(api_shaped)
        assert set(r1["completeness"].keys()) == set(
            r2["completeness"].keys()
        ), f"completeness 형식 불일치: {r1['completeness'].keys()} vs {r2['completeness'].keys()}"
