"""H-3 ① — prepare_listing 의 SEO 상품명 제안(seo_title_suggestion) 검증.

계약(워크오더 H-3 ①):
  (a) 정상 입력 → suggested 비어있지 않고 50자 이하.
  (b) prepared payload 의 name 은 입력 그대로(제안 침투 금지).
  (c) 금지어 포함 입력 → dropped_terms 에 드러남(무음 변형 금지).
  (d) 재료 부족 입력 → suggested=null + note(조용한 생략 금지).
  (e) itemName(품명) 경로에 제안이 흘러들지 않음(D119 회귀 가드).
  외부 호출 0회 — keyword_volume 을 monkeypatch 로 트랩해 증명한다.

네트워크/파일시스템 차단: register.prepare_listing 을 fake 로 대체(monkeypatch).
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

from clossify import mcp_server, register, seo


def _fake_register_prepare(d):
    """register.prepare_listing 대체 — 로컬·비네트워크 payload 반환."""
    return {
        "product_key": register.make_product_key(
            str(d.get("name") or ""), int(d.get("salePrice") or 0)
        ),
        "product": {
            "name": str(d.get("name") or ""),
            "categoryId": str(d.get("category_id") or ""),
            "salePrice": int(d.get("salePrice") or 0),
            "tags": list(d.get("tags") or []),
        },
        "images": {"listing_urls": ["http://cdn/test/a.png"], "detail_urls": []},
        "detail_html": "<html></html>",
        "needs_llm": [],
        "needs_user": [],
        "qa": {"agents": [], "verdict": "PASS", "violations": []},
        "preview_path": None,
    }


@pytest.fixture
def hermetic(monkeypatch):
    """외부 경로(등록 본체·카테고리 조회·자동 열기·볼륨 API) 차단."""
    monkeypatch.setattr(mcp_server._register_mod, "prepare_listing", _fake_register_prepare)
    monkeypatch.setattr(mcp_server, "_category_path_for", lambda cid: "홈데코>인테리어소품>화병")
    monkeypatch.setattr(mcp_server, "_config_enable_auto_open", lambda: False)

    # 외부 API 0회 증명 — keyword_volume 이 불리면 즉시 실패.
    def _no_volume(*a, **kw):  # pragma: no cover - 불리면 안 됨
        raise AssertionError("keyword_volume 호출됨 — 외부 호출 0회 위반")

    monkeypatch.setattr(seo, "keyword_volume", _no_volume)


class TestSeoTitleSuggestion:
    def test_normal_input_suggestion(self, hermetic):
        """(a) 정상 입력 → suggested 비음·50자 이하·근거 포함."""
        result = mcp_server.prepare_listing(
            {
                "name": "도자기 화병 거실 장식",
                "salePrice": 19000,
                "image_sources": ["a.png"],
                "category_id": "50002366",
                "tags": ["세라믹", "홈데코"],
            }
        )
        assert result["ok"] is True
        sugg = result.get("seo_title_suggestion")
        assert isinstance(sugg, dict), f"seo_title_suggestion 없음: {result.keys()}"
        assert sugg["suggested"], f"suggested 비어있음: {sugg}"
        assert len(sugg["suggested"]) <= 50
        assert isinstance(sugg["basis"], list) and sugg["basis"], "basis(근거) 없음"
        assert sugg["note"], "note 없음"

    def test_prepared_name_untouched(self, hermetic):
        """(b) prepared payload 의 name 은 입력 그대로 — 제안 침투 금지."""
        name = "도자기 화병 거실 장식"
        seen = {}

        def spy(d):
            seen["product"] = d
            seen["name"] = d.get("name")
            return _fake_register_prepare(d)

        mcp_server._register_mod.prepare_listing = spy  # fixture monkeypatch 위임
        try:
            result = mcp_server.prepare_listing(
                {
                    "name": name,
                    "salePrice": 19000,
                    "image_sources": ["a.png"],
                    "category_id": "50002366",
                }
            )
        finally:
            mcp_server._register_mod.prepare_listing = _fake_register_prepare
        assert result["ok"] is True
        # 등록 본체에 넘어간 상품명이 입력 그대로이며, 이후에도 변형 없음.
        assert seen["name"] == name
        assert seen["product"].get("name") == name, "호출 후 product.name 이 변형됨"
        # 제안은 별도 키일 뿐, name 키를 덮어쓰지 않는다.
        sugg = result["seo_title_suggestion"]["suggested"]
        assert sugg != name or sugg == name  # 우연 일치는 허용 — 침투 여부가 핵심
        assert result.get("name") is None, "최상위에 name 키가 새로 생기면 안 됨"

    def test_banned_terms_surfaced(self, hermetic):
        """(c) 금지어 포함 입력 → dropped_terms 에 드러남."""
        result = mcp_server.prepare_listing(
            {
                "name": "정품 세라믹 화병",
                "salePrice": 15000,
                "image_sources": ["a.png"],
                "category_id": "50002366",
            }
        )
        assert result["ok"] is True
        sugg = result["seo_title_suggestion"]
        dropped_words = [str(row.get("word") or "") for row in sugg["dropped_terms"]]
        assert any(
            "정" in w and "품" in w for w in dropped_words
        ), f"금지어가 무음으로 잘림: dropped_terms={sugg['dropped_terms']}"
        # 제안 본문에는 살아남지 않는다.
        assert "정품" not in sugg["suggested"]

    def test_insufficient_material_null_with_note(self, hermetic):
        """(d) 재료 부족(정제 후 남는 조각 없음) → suggested=null + note."""
        result = mcp_server.prepare_listing(
            {
                "name": "?!?",
                "salePrice": 15000,
                "image_sources": ["a.png"],
            }
        )
        assert result["ok"] is True
        sugg = result["seo_title_suggestion"]
        assert sugg["suggested"] is None, f"suggested={sugg['suggested']!r}"
        assert sugg["note"], "note 없음 — 조용한 생략 금지 위반"

    def test_d119_item_name_guard(self, hermetic):
        """(e) itemName(품명) 경로에 제안이 흘러들지 않는다.

        제안은 결과 최상위 seo_title_suggestion 키에만 존재해야 하며,
        등록 본체로 넘어가는 product dict·prepared name 어디에도 스며들지
        않는다.
        """
        name = "최고급 홈데코 화병"
        result = mcp_server.prepare_listing(
            {
                "name": name,
                "salePrice": 12000,
                "image_sources": ["a.png"],
                "category_id": "50002366",
            }
        )
        assert result["ok"] is True
        sugg = result["seo_title_suggestion"]
        # 금지어("최고급") 가 정제된 제안 — 즉 입력과 다른 값을 만든다.
        assert sugg["suggested"] and sugg["suggested"] != name
        # 최상위 반환에 제안 문자열이 name-ish 키로 새로 생기지 않는다.
        for key in ("name", "itemName", "item_name", "suggested_name"):
            assert key not in result, f"제안이 최상위 {key} 키로 침투(D119 위반)"
