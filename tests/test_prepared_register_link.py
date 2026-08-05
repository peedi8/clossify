"""prepared payload 와 register_product 도구의 연결 검증.

``prepare_listing`` 이 만든 상세 HTML 과 정규화된 이미지를 ``register_product``
가 재사용할 수 있는지 확인한다. 핵심 검증 항목:

- (a) ``prepare_listing`` 반환에 ``images`` 가 있고 정규화된 URL 리스트다.
- (b) prepared 가 있을 때 ``detail_html`` 생략 → prepared 의 상세 HTML 사용.
- (c) ``image_urls`` 생략 → prepared 의 이미지 사용, 대표 이미지가 첫 URL.
- (d) 명시값 우선 — prepared 가 있어도 인자로 준 값이 이긴다.
- (e) 뒷문 아님 — prepared 의 이미지가 비어 있으면 거부, 네이버 호출 0회.
- (f) 둘 다 생략 + prepared 없음 → 거부, 네이버 호출 0회.
- (g) ``filled_from_prepared`` 가 실제로 채운 항목만 보고한다.

모든 테스트는 tmp_path 기반 격리와 monkeypatch 로 네이버 API 호출을 차단한다.
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

from clossify import (
    common,
    mcp_server,
    naver_client,
    qa_agents,
    register,
)


# --------------------------------------------------------------------------- #
# 공통 픽스처.
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


_NOTICE_MOCK = {
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

# common.cfg() mock 용 — origin 만 포함한 최소 config.
# _compliance_code_check 가 common.cfg().get("smartstore_notice_defaults") 를
# 직접 읽기 때문에 _notice_config 와 값이 일치해야 한다.
_COMMON_CFG_MOCK = {
    "smartstore_notice_defaults": {
        "origin_area_code": "04",
        "origin_content": "중국",
    },
}


def _make_compliant_payload(extra: dict | None = None) -> dict:
    """컴플라이언스 게이트를 통과하는 WEAR 페이로드를 반환.

    DRY_RUN 모드에서도 게이트가 실행되므로, mock build_payload 가
    게이트를 통과하는 페이로드를 반환해야 한다. WEAR 타입의 필수
    필드를 모두 포함한다.
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
    payload = {
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
    if extra:
        payload["originProduct"].update(extra)
    return payload


def _dry_run_naver_register(payload):
    """DRY_RUN 모드의 naver_client.register_product 대체.

    dict 를 반환하면 mcp_server.register_product 가 DRY_RUN 경로로 처리한다.
    """
    return {"ok": True, "originProductNo": "test-no"}


def _write_prepared_payload(
    pkey: str,
    *,
    listing_urls: list[str],
    detail_html: str,
    name: str = "",
    price: int | None = None,
    qa_agents_list: list[dict] | None = None,
    needs_llm: list | None = None,
    needs_user: list | None = None,
):
    """테스트용 prepared payload 를 디스크에 저장한다.

    ``name``/``price`` 를 ``product`` dict 에 넣어 후보 스캔
    (``find_prepared_candidates``) 이 이름+가격 으로 찾을 수 있게 한다.
    실제 ``prepare_listing`` 도 payload 에 ``product.name``/``product.salePrice``
    를 저장하므로 이 형태가 실제 동작과 일치한다.
    """
    if qa_agents_list is None:
        # 모두 PASS 인 기본 QA — 게이트 통과용.
        agent_rows = [
            qa_agents._qa_agent_result("image", "PASS", [], "PASS"),
            qa_agents._qa_agent_result("copy", "PASS", [], "PASS"),
        ]
    else:
        agent_rows = qa_agents_list
    qa = qa_agents.aggregate_qa_results(agent_rows)
    product = {
        "name": name,
        "salePrice": price if price is not None else 0,
    }
    payload = {
        "product_key": pkey,
        "version": common.PREPARED_PAYLOAD_VERSION,
        "product": product,
        "images": {
            "listing_urls": listing_urls,
            "detail_urls": [],
        },
        "detail_html": detail_html,
        "qa": qa,
        "needs_llm": needs_llm or [],
        "needs_user": needs_user or [],
    }
    register.write_prepared_payload(payload)
    return payload


# --------------------------------------------------------------------------- #
# (a) prepare_listing 반환에 images 가 있고 정규화된 URL 리스트다.
# --------------------------------------------------------------------------- #
class TestPrepareListingReturnsImages:
    """prepare_listing MCP 도구 반환에 images 키가 있는가."""

    def test_images_key_present_and_is_list(self, isolated_prepared_dir, monkeypatch):
        d = {
            "name": "이미지반환테스트",
            "salePrice": 15000,
            "image_sources": ["a.png", "b.png"],
            "category_id": "50002366",
        }
        # images.attach_images 만 mock 하고 실제 prepare_listing library 호출.
        monkeypatch.setattr(
            "clossify.images.attach_images",
            _fake_attach_ok,
        )
        result = mcp_server.prepare_listing(d)
        assert result["ok"] is True, f"prepare_listing 실패: {result}"
        assert "images" in result, "반환에 images 키가 없음"
        images = result["images"]
        assert isinstance(images, list), f"images 가 리스트가 아님: {type(images)}"
        assert len(images) == 2, f"이미지가 2장이어야 함: {len(images)}"
        # 모든 항목이 비어있지 않은 문자열이어야 한다.
        for url in images:
            assert isinstance(url, str) and url.strip(), f"무효 URL: {url!r}"

    def test_images_empty_when_no_sources(self, isolated_prepared_dir, monkeypatch):
        """이미지 소스가 0장이면 images 도 빈 리스트다."""
        # image_sources 없이 prepare 하면 ValueError — 이 테스트는
        # prepare_listing 이 images 키를 항상 반환하는지가 목적이므로,
        # 정상 케이스만 검증한다. (0장 거부는 test_prepare_delegate_and_gate 에서 커버)
        d = {
            "name": "정상상품",
            "salePrice": 10000,
            "image_sources": ["x.png"],
            "category_id": "50002366",
        }
        monkeypatch.setattr(
            "clossify.images.attach_images",
            _fake_attach_ok,
        )
        result = mcp_server.prepare_listing(d)
        assert result["ok"] is True
        assert isinstance(result.get("images"), list)
        assert len(result["images"]) >= 1


# --------------------------------------------------------------------------- #
# (b) prepared detail_html 생략 시 채워진다.
# --------------------------------------------------------------------------- #
class TestFillDetailHtmlFromPrepared:
    """detail_html 생략 → prepared 의 상세 HTML 이 페이로드에 실제로 들어가는가."""

    def test_detail_html_filled_from_prepared(self, isolated_prepared_dir, monkeypatch):
        name = "상세채우기상품"
        price = 20000
        pkey = register.make_product_key(name, price)
        prepared_html = "<html><body>prepared-detail</body></html>"
        _write_prepared_payload(
            pkey,
            listing_urls=["http://cdn/img1.png", "http://cdn/img2.png"],
            detail_html=prepared_html,
            name=name,
            price=price,
        )

        # build_payload 를 가로채서 detail_html 인자를 캡처.
        captured: list[dict] = []

        def fake_build(product, detail_html_arg, image_urls_arg, status="SALE", **kwargs):
            captured.append(
                {
                    "detail_html": detail_html_arg,
                    "image_urls": list(image_urls_arg),
                }
            )
            return _make_compliant_payload()

        monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
        monkeypatch.setattr(naver_client, "_notice_config", lambda: _NOTICE_MOCK)
        monkeypatch.setattr(naver_client, "_kc_config", lambda: ({}, ""))
        monkeypatch.setattr(common, "cfg", lambda: _COMMON_CFG_MOCK)
        monkeypatch.setattr(naver_client, "build_payload", fake_build)
        monkeypatch.setattr(naver_client, "register_product", _dry_run_naver_register)

        # detail_html 생략하고 호출.
        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50021299",
            image_urls=["http://cdn/explicit.png"],
            preview_confirmed=True,
        )
        assert result["ok"] is True, f"등록 실패: {result}"
        assert len(captured) == 1, f"build_payload 호출 횟수: {len(captured)}"
        # prepared 의 detail_html 이 실제로 전달되었는가.
        assert (
            captured[0]["detail_html"] == prepared_html
        ), f"prepared 의 detail_html 이 전달되지 않음: {captured[0]['detail_html']!r}"
        # filled_from_prepared 보고.
        filled = result.get("filled_from_prepared", [])
        assert "detail_html" in filled, f"detail_html 이 filled_from_prepared 에 없음: {filled}"


# --------------------------------------------------------------------------- #
# (c) image_urls 생략 시 prepared 이미지 사용, 대표 이미지가 첫 URL.
# --------------------------------------------------------------------------- #
class TestFillImageUrlsFromPrepared:
    """image_urls 생략 → prepared 의 이미지가 쓰이고 대표 이미지가 첫 번째인가."""

    def test_image_urls_filled_from_prepared(self, isolated_prepared_dir, monkeypatch):
        name = "이미지채우기상품"
        price = 25000
        pkey = register.make_product_key(name, price)
        prepared_urls = [
            "http://cdn/rep.png",
            "http://cdn/second.png",
            "http://cdn/third.png",
        ]
        _write_prepared_payload(
            pkey,
            listing_urls=prepared_urls,
            detail_html="<html><body>detail</body></html>",
            name=name,
            price=price,
        )

        captured: list[dict] = []

        def fake_build(product, detail_html_arg, image_urls_arg, status="SALE", **kwargs):
            captured.append(
                {
                    "detail_html": detail_html_arg,
                    "image_urls": list(image_urls_arg),
                }
            )
            return _make_compliant_payload()

        monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
        monkeypatch.setattr(naver_client, "_notice_config", lambda: _NOTICE_MOCK)
        monkeypatch.setattr(naver_client, "_kc_config", lambda: ({}, ""))
        monkeypatch.setattr(common, "cfg", lambda: _COMMON_CFG_MOCK)
        monkeypatch.setattr(naver_client, "build_payload", fake_build)
        monkeypatch.setattr(naver_client, "register_product", _dry_run_naver_register)

        # image_urls 생략하고 호출.
        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50021299",
            detail_html="<html><body>explicit</body></html>",
            preview_confirmed=True,
        )
        assert result["ok"] is True, f"등록 실패: {result}"
        assert len(captured) == 1
        passed_urls = captured[0]["image_urls"]
        assert len(passed_urls) == 3, f"이미지 3장이어야 함: {len(passed_urls)}"
        # 대표 이미지(첫 번째 URL)가 prepared 의 첫 번째와 동일한가.
        assert (
            passed_urls[0] == "http://cdn/rep.png"
        ), f"대표 이미지가 prepared 첫 URL 이 아님: {passed_urls[0]}"
        # 전체 순서 보존.
        assert passed_urls == prepared_urls, f"이미지 순서 불일치: {passed_urls}"
        # filled_from_prepared 보고.
        filled = result.get("filled_from_prepared", [])
        assert "image_urls" in filled, f"image_urls 이 filled_from_prepared 에 없음: {filled}"


# --------------------------------------------------------------------------- #
# (d) 명시값 우선.
# --------------------------------------------------------------------------- #
class TestExplicitValueWins:
    """prepared 가 있어도 인자로 준 값이 우선하는가."""

    def test_explicit_detail_html_wins(self, isolated_prepared_dir, monkeypatch):
        name = "명시값우선상품"
        price = 30000
        pkey = register.make_product_key(name, price)
        _write_prepared_payload(
            pkey,
            listing_urls=["http://cdn/prepared.png"],
            detail_html="<html>prepared</html>",
            name=name,
            price=price,
        )

        captured: list[dict] = []

        def fake_build(product, detail_html_arg, image_urls_arg, status="SALE", **kwargs):
            captured.append({"detail_html": detail_html_arg})
            return _make_compliant_payload()

        monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
        monkeypatch.setattr(naver_client, "_notice_config", lambda: _NOTICE_MOCK)
        monkeypatch.setattr(naver_client, "_kc_config", lambda: ({}, ""))
        monkeypatch.setattr(common, "cfg", lambda: _COMMON_CFG_MOCK)
        monkeypatch.setattr(naver_client, "build_payload", fake_build)
        monkeypatch.setattr(naver_client, "register_product", _dry_run_naver_register)

        explicit_html = "<html><body>EXPLICIT-WINS</body></html>"
        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50021299",
            image_urls=["http://cdn/explicit.png"],
            detail_html=explicit_html,
            preview_confirmed=True,
        )
        assert result["ok"] is True
        assert (
            captured[0]["detail_html"] == explicit_html
        ), f"명시값이 prepared 에 덮어씌워짐: {captured[0]['detail_html']!r}"
        # 명시적으로 준 항목은 filled_from_prepared 에 없어야 한다.
        filled = result.get("filled_from_prepared", [])
        assert "detail_html" not in filled, f"명시값인데 filled_from_prepared 에 있음: {filled}"

    def test_explicit_image_urls_wins(self, isolated_prepared_dir, monkeypatch):
        name = "명시이미지우선"
        price = 31000
        pkey = register.make_product_key(name, price)
        _write_prepared_payload(
            pkey,
            listing_urls=["http://cdn/prepared1.png", "http://cdn/prepared2.png"],
            detail_html="<html>prepared</html>",
            name=name,
            price=price,
        )

        captured: list[dict] = []

        def fake_build(product, detail_html_arg, image_urls_arg, status="SALE", **kwargs):
            captured.append({"image_urls": list(image_urls_arg)})
            return _make_compliant_payload()

        monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
        monkeypatch.setattr(naver_client, "_notice_config", lambda: _NOTICE_MOCK)
        monkeypatch.setattr(naver_client, "_kc_config", lambda: ({}, ""))
        monkeypatch.setattr(common, "cfg", lambda: _COMMON_CFG_MOCK)
        monkeypatch.setattr(naver_client, "build_payload", fake_build)
        monkeypatch.setattr(naver_client, "register_product", _dry_run_naver_register)

        explicit_urls = ["http://cdn/explicit-only.png"]
        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50021299",
            image_urls=explicit_urls,
            detail_html="<html>explicit</html>",
            preview_confirmed=True,
        )
        assert result["ok"] is True
        assert (
            captured[0]["image_urls"] == explicit_urls
        ), f"명시 이미지가 prepared 에 덮어씌워짐: {captured[0]['image_urls']}"
        filled = result.get("filled_from_prepared", [])
        assert "image_urls" not in filled


# --------------------------------------------------------------------------- #
# (e) 뒷문 아님 — prepared 이미지 비어 있으면 거부.
# --------------------------------------------------------------------------- #
class TestNoBackdoorEmptyPreparedImages:
    """prepared 의 이미지가 비어 있으면 거부, 네이버 호출 0회."""

    def test_empty_prepared_images_rejected(self, isolated_prepared_dir, monkeypatch):
        name = "빈이미지prepared"
        price = 12000
        pkey = register.make_product_key(name, price)
        # listing_urls 가 빈 리스트인 prepared payload.
        _write_prepared_payload(
            pkey,
            listing_urls=[],
            detail_html="<html>detail</html>",
            name=name,
            price=price,
        )

        naver_calls: list = []
        monkeypatch.setattr(
            naver_client,
            "register_product",
            lambda payload: naver_calls.append(payload) or (200, {}),
        )
        monkeypatch.setattr(
            naver_client, "build_payload", lambda *a, **kw: _make_compliant_payload()
        )
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)

        # image_urls 를 생략 → prepared 에서 채우려 시도 → 비어있음 → 거부.
        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50021299",
            detail_html="<html>explicit</html>",
            preview_confirmed=True,
        )
        assert result["ok"] is False, f"빈 이미지인데 통과함: {result}"
        # 네이버 API 가 호출되지 않아야 한다.
        assert len(naver_calls) == 0, f"네이버 API 가 호출됨 (뒷문): {len(naver_calls)}회"


# --------------------------------------------------------------------------- #
# (f) 둘 다 생략 + prepared 없음 → 거부.
# --------------------------------------------------------------------------- #
class TestRejectWhenNoInputsAndNoPrepared:
    """둘 다 생략하고 prepared 도 없으면 거부, 네이버 호출 0회."""

    def test_no_inputs_no_prepared_rejected(self, isolated_prepared_dir, monkeypatch):
        name = "입력없음상품"
        price = 9000
        # prepared payload 를 전혀 작성하지 않는다.

        naver_calls: list = []
        monkeypatch.setattr(
            naver_client,
            "register_product",
            lambda payload: naver_calls.append(payload) or (200, {}),
        )
        monkeypatch.setattr(
            naver_client, "build_payload", lambda *a, **kw: _make_compliant_payload()
        )
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)

        # image_urls 와 detail_html 모두 생략.
        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50021299",
            preview_confirmed=True,
        )
        assert result["ok"] is False, f"입력도 prepared 도 없는데 통과함: {result}"
        assert (
            len(naver_calls) == 0
        ), f"네이버 API 가 호출됨 (무동작 금지 위반): {len(naver_calls)}회"
        # 사유가 명시되어야 한다.
        assert result.get("error"), "거부 사유가 없음"


# --------------------------------------------------------------------------- #
# (g) filled_from_prepared 정확성.
# --------------------------------------------------------------------------- #
class TestFilledFromPreparedReporting:
    """filled_from_prepared 가 실제로 채운 항목만 정확히 보고하는가."""

    def test_both_filled_reports_both(self, isolated_prepared_dir, monkeypatch):
        name = "둘다채우기"
        price = 33000
        pkey = register.make_product_key(name, price)
        _write_prepared_payload(
            pkey,
            listing_urls=["http://cdn/a.png"],
            detail_html="<html>both</html>",
            name=name,
            price=price,
        )

        monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
        monkeypatch.setattr(naver_client, "_notice_config", lambda: _NOTICE_MOCK)
        monkeypatch.setattr(naver_client, "_kc_config", lambda: ({}, ""))
        monkeypatch.setattr(common, "cfg", lambda: _COMMON_CFG_MOCK)
        monkeypatch.setattr(
            naver_client,
            "build_payload",
            lambda *a, **kw: _make_compliant_payload(),
        )
        monkeypatch.setattr(naver_client, "register_product", _dry_run_naver_register)

        # image_urls 와 detail_html 모두 생략 → 둘 다 prepared 에서 채워짐.
        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50021299",
            preview_confirmed=True,
        )
        assert result["ok"] is True
        filled = result.get("filled_from_prepared", [])
        assert set(filled) == {"detail_html", "image_urls"}, f"둘 다 채웠는데 보고가 다름: {filled}"

    def test_neither_filled_reports_empty(self, isolated_prepared_dir, monkeypatch):
        """둘 다 명시적으로 줬으면 filled_from_prepared 는 빈 리스트."""
        name = "둘다명시"
        price = 34000
        pkey = register.make_product_key(name, price)
        _write_prepared_payload(
            pkey,
            listing_urls=["http://cdn/p.png"],
            detail_html="<html>p</html>",
            name=name,
            price=price,
        )

        monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
        monkeypatch.setattr(naver_client, "_notice_config", lambda: _NOTICE_MOCK)
        monkeypatch.setattr(naver_client, "_kc_config", lambda: ({}, ""))
        monkeypatch.setattr(common, "cfg", lambda: _COMMON_CFG_MOCK)
        monkeypatch.setattr(
            naver_client,
            "build_payload",
            lambda *a, **kw: _make_compliant_payload(),
        )
        monkeypatch.setattr(naver_client, "register_product", _dry_run_naver_register)

        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50021299",
            image_urls=["http://cdn/explicit.png"],
            detail_html="<html>explicit</html>",
            preview_confirmed=True,
        )
        assert result["ok"] is True
        filled = result.get("filled_from_prepared", [])
        assert filled == [], f"명시값인데 filled 에 있음: {filled}"

    def test_only_detail_filled_reports_only_detail(self, isolated_prepared_dir, monkeypatch):
        """detail_html 만 생략 → filled_from_prepared 는 ['detail_html'] 만."""
        name = "detail만생략"
        price = 35000
        pkey = register.make_product_key(name, price)
        _write_prepared_payload(
            pkey,
            listing_urls=["http://cdn/p.png"],
            detail_html="<html>prepared-detail</html>",
            name=name,
            price=price,
        )

        monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
        monkeypatch.setattr(naver_client, "_notice_config", lambda: _NOTICE_MOCK)
        monkeypatch.setattr(naver_client, "_kc_config", lambda: ({}, ""))
        monkeypatch.setattr(common, "cfg", lambda: _COMMON_CFG_MOCK)
        monkeypatch.setattr(
            naver_client,
            "build_payload",
            lambda *a, **kw: _make_compliant_payload(),
        )
        monkeypatch.setattr(naver_client, "register_product", _dry_run_naver_register)

        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50021299",
            image_urls=["http://cdn/explicit.png"],
            # detail_html 생략
            preview_confirmed=True,
        )
        assert result["ok"] is True
        filled = result.get("filled_from_prepared", [])
        assert filled == ["detail_html"], f"detail 만 채워야 함: {filled}"
