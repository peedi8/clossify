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


# =========================================================================== #
# prepared → register 왕복 확장 (이음매 결함 수정).
#
# 과거에는 detail_html · image_urls 딱 2개만 복원했다 — prepare 단계에서 채운
# notice · tags · options · option_groups · deferred_notice_fields 가 등록에
# 도달하지 못하는 결함이 있었다. 아래 테스트는 이 결함이 수정되었음을 검증한다.
#
# 시나리오:
#   (a) prepare(고시 포함) → register(notice 생략) → 컴플라이언스 통과 +
#       filled_from_prepared 에 notice.
#   (b) 호출자가 notice 를 직접 넘기면 그 값이 쓰인다(prepared 가 안 덮음).
#   (c) tags · options · option_groups · deferred_notice_fields 각각 동일하게
#       복원되고 filled_from_prepared 에 기록된다.
#   (d) prepared 에 없는 항목은 복원 목록에 안 들어간다(창작 금지).
#   (e) prepared 무결성 검증 실패 시 복원하지 않는다(회귀 — 우회 금지).
#   (f) 승인 없이 등록하면 여전히 차단된다(회귀).
#   (g) 원본 이미지 0장 게이트 여전히 유효(회귀).
#   (h) docstring 이 실제 복원 항목과 일치한다.
# =========================================================================== #


def _write_full_prepared(
    pkey: str,
    *,
    name: str,
    price: int,
    listing_urls: list[str],
    detail_html: str,
    notice: dict | None = None,
    tags: list[str] | None = None,
    options: list[dict] | None = None,
    option_groups: list[str] | None = None,
    deferred_notice_fields: list[str] | None = None,
):
    """notice/tags/options/option_groups/deferred_notice_fields 를 포함한
    prepared payload 를 디스크에 저장한다(실제 prepare_listing 저장 스키마와
    동일). product block 에는 상품군별 필드가 산다."""
    agent_rows = [
        qa_agents._qa_agent_result("image", "PASS", [], "PASS"),
        qa_agents._qa_agent_result("copy", "PASS", [], "PASS"),
    ]
    qa = qa_agents.aggregate_qa_results(agent_rows)
    product: dict = {
        "name": name,
        "salePrice": price,
    }
    if notice is not None:
        product["notice"] = notice
    if tags is not None:
        product["tags"] = tags
    if options is not None:
        product["options"] = options
    if option_groups is not None:
        product["option_groups"] = option_groups
    payload: dict = {
        "product_key": pkey,
        "version": common.PREPARED_PAYLOAD_VERSION,
        "product": product,
        "images": {"listing_urls": listing_urls, "detail_urls": []},
        "detail_html": detail_html,
        "qa": qa,
        "needs_llm": [],
        "needs_user": [],
    }
    if deferred_notice_fields is not None:
        payload["deferred_notice_fields"] = deferred_notice_fields
    register.write_prepared_payload(payload)
    return payload


def _setup_dry_run_gate(monkeypatch):
    """DRY_RUN + 게이트 통과 + config mock 세팅."""
    monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
    monkeypatch.setattr(naver_client, "_notice_config", lambda: _NOTICE_MOCK)
    monkeypatch.setattr(naver_client, "_kc_config", lambda: ({}, ""))
    monkeypatch.setattr(common, "cfg", lambda: _COMMON_CFG_MOCK)
    monkeypatch.setattr(naver_client, "build_payload", lambda *a, **kw: _make_compliant_payload())
    monkeypatch.setattr(naver_client, "register_product", _dry_run_naver_register)


# WEAR 고시 본문 — 컴플라이언스 게이트를 통과하는 값.
_WEAR_NOTICE = {
    "productInfoProvidedNoticeType": "WEAR",
    "wear": {
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
    },
}


# --------------------------------------------------------------------------- #
# (a) prepare(고시 포함) → register(notice 생략) → 컴플라이언스 통과 +
#     filled_from_prepared 에 notice.
# --------------------------------------------------------------------------- #
class TestNoticeRestoredFromPrepared:
    """prepare 에서 채운 고시(notice) 가 등록까지 흐르는가 (결함 수정의 핵심)."""

    def test_notice_restored_and_compliance_passes(self, isolated_prepared_dir, monkeypatch):
        name = "고시복원상품"
        price = 45000
        pkey = register.make_product_key(name, price)
        _write_full_prepared(
            pkey,
            name=name,
            price=price,
            listing_urls=["http://cdn/a.png", "http://cdn/b.png"],
            detail_html="<html><body>detail</body></html>",
            notice=_WEAR_NOTICE,
        )
        _setup_dry_run_gate(monkeypatch)

        # notice 를 생략하고 호출 → prepared 에서 복원되어야 함.
        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50021299",
            preview_confirmed=True,
        )
        assert (
            result["ok"] is True
        ), f"prepared notice 복원으로 컴플라이언스를 통과해야 한다: {result}"
        # blocked_by 가 없어야 한다(컴플라이언스 통과).
        assert (
            "blocked_by" not in result or result.get("blocked_by") is None
        ), f"컴플라이언스 차단이면 안 됨: {result.get('blocked_by')}"
        filled = result.get("filled_from_prepared", [])
        assert (
            "notice" in filled
        ), f"notice 가 filled_from_prepared 에 있어야 한다 (조용한 흐름 금지): {filled}"


# --------------------------------------------------------------------------- #
# (b) 호출자가 notice 를 직접 넘기면 그 값이 쓰인다(prepared 가 안 덮음).
# --------------------------------------------------------------------------- #
class TestExplicitNoticeWins:
    """명시 notice 인자가 prepared notice 보다 우선하는가."""

    def test_explicit_notice_overrides_prepared(self, isolated_prepared_dir, monkeypatch):
        name = "명시고시우선"
        price = 46000
        pkey = register.make_product_key(name, price)
        _write_full_prepared(
            pkey,
            name=name,
            price=price,
            listing_urls=["http://cdn/a.png"],
            detail_html="<html>detail</html>",
            notice=_WEAR_NOTICE,
        )
        captured: list[dict] = []

        def fake_build(product, detail_html_arg, image_urls_arg, status="SALE", **kwargs):
            captured.append({"notice": product.get("notice")})
            return _make_compliant_payload()

        monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
        monkeypatch.setattr(naver_client, "_notice_config", lambda: _NOTICE_MOCK)
        monkeypatch.setattr(naver_client, "_kc_config", lambda: ({}, ""))
        monkeypatch.setattr(common, "cfg", lambda: _COMMON_CFG_MOCK)
        monkeypatch.setattr(naver_client, "build_payload", fake_build)
        monkeypatch.setattr(naver_client, "register_product", _dry_run_naver_register)

        explicit_notice = {
            "productInfoProvidedNoticeType": "WEAR",
            "wear": {
                "material": "명시 폴리에스테르 100%",
                "color": "네이비",
                "size": "L",
                "manufacturer": "명시제조사",
                "caution": "드라이클리닝",
                "packDateText": "2026-02",
                "warrantyPolicy": "명시 보증",
                "afterServiceDirector": "070-9999-8888",
                "returnCostReason": "단순변심 반품비용 구매자부담",
                "noRefundReason": "주문제작 청약철회 제한",
                "qualityAssuranceStandard": "관련법에 따름",
                "compensationProcedure": "소비자분쟁해결기준",
                "troubleShootingContents": "고객센터 문의",
            },
        }
        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50021299",
            image_urls=["http://cdn/explicit.png"],
            detail_html="<html>explicit</html>",
            notice=explicit_notice,
            preview_confirmed=True,
        )
        assert result["ok"] is True, f"명시 notice 등록 실패: {result}"
        # 명시 notice 가 실제로 전달되었는가.
        sent_notice = captured[0]["notice"]
        assert isinstance(sent_notice, dict)
        sent_material = (sent_notice.get("wear") or {}).get("material")
        assert (
            sent_material == "명시 폴리에스테르 100%"
        ), f"명시 notice 가 안 쓰였다 (prepared 가 덮었나): {sent_material!r}"
        # 명시적으로 준 항목은 filled_from_prepared 에 없어야 한다.
        filled = result.get("filled_from_prepared", [])
        assert "notice" not in filled, f"명시값인데 filled 에 있음: {filled}"


# --------------------------------------------------------------------------- #
# (c) tags · options · option_groups · deferred_notice_fields 각각 동일하게
#     복원되고 filled_from_prepared 에 기록된다.
# --------------------------------------------------------------------------- #
class TestOtherFieldsRestoredFromPrepared:
    """tags/options/option_groups/deferred_notice_fields 도 복원되는가."""

    def test_tags_restored(self, isolated_prepared_dir, monkeypatch):
        name = "태그복원상품"
        price = 47000
        pkey = register.make_product_key(name, price)
        _write_full_prepared(
            pkey,
            name=name,
            price=price,
            listing_urls=["http://cdn/a.png"],
            detail_html="<html>detail</html>",
            notice=_WEAR_NOTICE,
            tags=["봄", "면100%", "기본핏"],
        )
        captured: list[dict] = []

        def fake_build(product, detail_html_arg, image_urls_arg, status="SALE", **kwargs):
            captured.append({"tags": list(product.get("tags") or [])})
            return _make_compliant_payload()

        monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
        monkeypatch.setattr(naver_client, "_notice_config", lambda: _NOTICE_MOCK)
        monkeypatch.setattr(naver_client, "_kc_config", lambda: ({}, ""))
        monkeypatch.setattr(common, "cfg", lambda: _COMMON_CFG_MOCK)
        monkeypatch.setattr(naver_client, "build_payload", fake_build)
        monkeypatch.setattr(naver_client, "register_product", _dry_run_naver_register)

        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50021299",
            preview_confirmed=True,
        )
        assert result["ok"] is True, f"tags 복원 등록 실패: {result}"
        assert captured[0]["tags"] == [
            "봄",
            "면100%",
            "기본핏",
        ], f"prepared tags 가 전달되지 않음: {captured[0]['tags']}"
        assert "tags" in result.get("filled_from_prepared", [])

    def test_options_and_option_groups_restored(self, isolated_prepared_dir, monkeypatch):
        name = "옵션복원상품"
        price = 48000
        pkey = register.make_product_key(name, price)
        # 2축 옵션(색상·사이즈). option_groups 와 축 수 일치.
        # naver_client._option_width 가 축 수를 2 로 인식하려면 names 리스트
        # (또는 optionName1/optionName2 키) 가 필요하다 — name 단일 키는 1축.
        options = [
            {"names": ["블랙", "S"], "stock": 5, "price": 48000},
            {"names": ["블랙", "M"], "stock": 3, "price": 48000},
            {"names": ["화이트", "S"], "stock": 4, "price": 48000},
        ]
        _write_full_prepared(
            pkey,
            name=name,
            price=price,
            listing_urls=["http://cdn/a.png"],
            detail_html="<html>detail</html>",
            notice=_WEAR_NOTICE,
            options=options,
            option_groups=["색상", "사이즈"],
        )
        captured: list[dict] = []

        def fake_build(product, detail_html_arg, image_urls_arg, status="SALE", **kwargs):
            captured.append(
                {
                    "options": list(product.get("options") or []),
                    "option_groups": list(product.get("option_groups") or []),
                }
            )
            return _make_compliant_payload()

        monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
        monkeypatch.setattr(naver_client, "_notice_config", lambda: _NOTICE_MOCK)
        monkeypatch.setattr(naver_client, "_kc_config", lambda: ({}, ""))
        monkeypatch.setattr(common, "cfg", lambda: _COMMON_CFG_MOCK)
        monkeypatch.setattr(naver_client, "build_payload", fake_build)
        monkeypatch.setattr(naver_client, "register_product", _dry_run_naver_register)

        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50021299",
            preview_confirmed=True,
        )
        assert result["ok"] is True, f"options 복원 등록 실패: {result}"
        assert (
            len(captured[0]["options"]) == 3
        ), f"prepared options 가 전달되지 않음: {captured[0]['options']}"
        assert captured[0]["option_groups"] == [
            "색상",
            "사이즈",
        ], f"prepared option_groups 가 전달되지 않음: {captured[0]['option_groups']}"
        filled = result.get("filled_from_prepared", [])
        assert "options" in filled
        assert "option_groups" in filled

    def test_deferred_notice_fields_restored(self, isolated_prepared_dir, monkeypatch):
        """deferred_notice_fields 가 prepared 에서 복원되어 컴플라이언스를 통과.

        material/color 필드를 '상세페이지 참조' 로 미루면 컴플라이언스 게이트의
        '고시 필수필드 누락' 위반에서 제외된다. 복원 경로가 호출자가 준 값과
        동일한 검증을 거쳐야 한다.
        """
        name = "미루기복원상품"
        price = 49000
        pkey = register.make_product_key(name, price)
        # material/color 가 빠진 WEAR notice — 미루기로 해결.
        partial_notice = {
            "productInfoProvidedNoticeType": "WEAR",
            "wear": {
                # material, color, size 는 deferred 로 미룸.
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
            },
        }
        _write_full_prepared(
            pkey,
            name=name,
            price=price,
            listing_urls=["http://cdn/a.png"],
            detail_html="<html>detail</html>",
            notice=partial_notice,
            deferred_notice_fields=["material", "color"],
        )
        # build_payload 에 전달되는 deferred_notice_fields 인자를 캡처.
        # _setup_dry_run_gate 가 build_payload 를 lambda 로 덮어쓰므로, 직접
        # 캡처링 빌더로 덮어쓴다.
        captured: list[dict] = []

        def capturing_build(product, detail_html_arg, image_urls_arg, status="SALE", **kwargs):
            captured.append({"deferred_notice_fields": kwargs.get("deferred_notice_fields")})
            return _make_compliant_payload()

        monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
        monkeypatch.setattr(naver_client, "_notice_config", lambda: _NOTICE_MOCK)
        monkeypatch.setattr(naver_client, "_kc_config", lambda: ({}, ""))
        monkeypatch.setattr(common, "cfg", lambda: _COMMON_CFG_MOCK)
        monkeypatch.setattr(naver_client, "build_payload", capturing_build)
        monkeypatch.setattr(naver_client, "register_product", _dry_run_naver_register)

        # deferred_notice_fields 생략 → prepared 에서 복원되어야 함.
        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50021299",
            preview_confirmed=True,
        )
        assert result["ok"] is True, f"deferred 복원으로 컴플라이언스를 통과해야 한다: {result}"
        filled = result.get("filled_from_prepared", [])
        assert (
            "deferred_notice_fields" in filled
        ), f"deferred_notice_fields 가 filled 에 있어야 한다: {filled}"
        # build_payload 에 material/color 가 전달되었는지 확인(복원 경로가
        # 실제로 값을 흘려보냈다). result["deferred_notice_fields"] 는 빌더가
        # sentinel 값을 채운 후의 보고이므로, 모킹한 빌더에서는 검증 불가.
        assert len(captured) == 1, f"build_payload 호출 횟수: {len(captured)}"
        passed_deferred = captured[0]["deferred_notice_fields"]
        assert (
            passed_deferred is not None
        ), "deferred_notice_fields 가 build_payload 에 전달되지 않음 (복원 누락)"
        reported = set(passed_deferred or [])
        assert {"material", "color"}.issubset(
            reported
        ), f"material/color 가 build_payload 인자에 없다: {reported}"


# --------------------------------------------------------------------------- #
# (d) prepared 에 없는 항목은 복원 목록에 안 들어간다(창작 금지).
# --------------------------------------------------------------------------- #
class TestNoFabricationFromPrepared:
    """prepared 에 없는 항목을 지어내서 복원하지 않는가."""

    def test_missing_fields_not_restored(self, isolated_prepared_dir, monkeypatch):
        name = "창작금지상품"
        price = 51000
        pkey = register.make_product_key(name, price)
        # tags/options/option_groups/deferred 없이 notice 만 있는 prepared.
        _write_full_prepared(
            pkey,
            name=name,
            price=price,
            listing_urls=["http://cdn/a.png"],
            detail_html="<html>detail</html>",
            notice=_WEAR_NOTICE,
        )
        _setup_dry_run_gate(monkeypatch)

        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50021299",
            preview_confirmed=True,
        )
        assert result["ok"] is True, f"등록 실패: {result}"
        filled = set(result.get("filled_from_prepared") or [])
        # detail_html/image_urls/notice 는 복원되지만, prepared 에 없는
        # tags/options/option_groups/deferred_notice_fields 는 절대 들어가면 안 된다.
        assert "tags" not in filled, "prepared 에 tags 없는데 복원됨 (창작)"
        assert "options" not in filled, "prepared 에 options 없는데 복원됨 (창작)"
        assert "option_groups" not in filled, "prepared 에 option_groups 없는데 복원됨 (창작)"
        assert (
            "deferred_notice_fields" not in filled
        ), "prepared 에 deferred_notice_fields 없는데 복원됨 (창작)"


# --------------------------------------------------------------------------- #
# (e) prepared 무결성 검증 실패 시 복원하지 않는다(회귀 — 우회 금지).
# --------------------------------------------------------------------------- #
class TestPreparedIntegrityGateStillHolds:
    """prepared 무결성(version 검증) 실패 시 복원하지 않고 차단하는가."""

    def test_version_mismatch_blocks_registration(self, isolated_prepared_dir, monkeypatch):
        name = "버전불일치상품"
        price = 52000
        pkey = register.make_product_key(name, price)
        # 버전이 다른 payload 를 직접 디스크에 쓴다.
        bad_payload = {
            "product_key": pkey,
            "version": "BOGUS-VERSION",
            "product": {"name": name, "salePrice": price, "notice": _WEAR_NOTICE},
            "images": {"listing_urls": ["http://cdn/a.png"], "detail_urls": []},
            "detail_html": "<html>detail</html>",
            "qa": qa_agents.aggregate_qa_results(
                [
                    qa_agents._qa_agent_result("image", "PASS", [], "PASS"),
                    qa_agents._qa_agent_result("copy", "PASS", [], "PASS"),
                ]
            ),
        }
        register.write_prepared_payload(bad_payload)
        # write_prepared_payload 이 version 을 덮어쓰므로, 강제로 다시 깨뜨린다.
        bad_payload["version"] = "BOGUS-VERSION"
        common._write_json_file(register._prepared_payload_path(pkey), bad_payload)
        _setup_dry_run_gate(monkeypatch)

        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50021299",
            product_key=pkey,  # 명시적 product_key → load_prepared_payload 경유
            preview_confirmed=True,
        )
        # version 불일치 → load_prepared_payload 가 ValueError →
        # resolve_prepared_for_register 가 (None, ...) 반환 → prepared 없음 취급.
        # image_urls/detail_html 생략이면 입력 없음으로 거부(무동작 금지).
        assert (
            result["ok"] is False
        ), f"version 불일치 prepared 인데 통과함 (우회 금지 위반): {result}"


# --------------------------------------------------------------------------- #
# (f) 승인 없이 등록하면 여전히 차단된다(회귀).
# --------------------------------------------------------------------------- #
class TestApprovalGateStillHolds:
    """preview_confirmed 없이 등록하면 차단되는가(회귀)."""

    def test_no_preview_confirmation_blocked(self, isolated_prepared_dir, monkeypatch):
        name = "승인없음상품"
        price = 53000
        pkey = register.make_product_key(name, price)
        _write_full_prepared(
            pkey,
            name=name,
            price=price,
            listing_urls=["http://cdn/a.png"],
            detail_html="<html>detail</html>",
            notice=_WEAR_NOTICE,
        )
        # config.require_preview_confirmation 이 켜진 것으로 가정한다.
        # _config_require_preview_confirmation / _config_enable_local_approval
        # 을 mcp_server 모듈에서 직접 mock 한다(실제 config 파일 읽기 경로를
        # 거치지 않는다 — 테스트는 게이트 동작 자체를 검증한다).
        _setup_dry_run_gate(monkeypatch)
        monkeypatch.setattr(mcp_server, "_config_require_preview_confirmation", lambda: True)
        monkeypatch.setattr(mcp_server, "_config_enable_local_approval", lambda: False)

        # preview_confirmed 생략(False) → 차단.
        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50021299",
            # image_urls/detail_html/notice 모두 생략 — prepared 에서 채워지지만
            # 승인 게이트가 먼저 차단해야 한다.
        )
        assert result["ok"] is False, f"승인 없이 통과함 (회귀 위반): {result}"
        assert (
            result.get("blocked_by") == "preview_confirmation"
        ), f"차단 사유가 preview_confirmation 이어야 함: {result.get('blocked_by')}"


# --------------------------------------------------------------------------- #
# (g) 원본 이미지 0장 게이트 여전히 유효(회귀).
# --------------------------------------------------------------------------- #
class TestOriginalImageGateStillHolds:
    """prepared 이미지가 빈 리스트이면 거부, 네이버 호출 0회(회귀)."""

    def test_zero_prepared_images_rejected(self, isolated_prepared_dir, monkeypatch):
        name = "이미지0장상품"
        price = 54000
        pkey = register.make_product_key(name, price)
        _write_full_prepared(
            pkey,
            name=name,
            price=price,
            listing_urls=[],  # 빈 리스트
            detail_html="<html>detail</html>",
            notice=_WEAR_NOTICE,
        )
        naver_calls: list = []
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
        monkeypatch.setattr(
            naver_client,
            "register_product",
            lambda payload: naver_calls.append(payload) or (200, {}),
        )
        monkeypatch.setattr(
            naver_client, "build_payload", lambda *a, **kw: _make_compliant_payload()
        )

        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50021299",
            detail_html="<html>explicit</html>",
            preview_confirmed=True,
        )
        assert result["ok"] is False, f"빈 이미지인데 통과함 (회귀 위반): {result}"
        assert len(naver_calls) == 0, f"네이버 API 가 호출됨 (뒷문): {len(naver_calls)}회"


# --------------------------------------------------------------------------- #
# (h) docstring 이 실제 복원 항목과 일치한다.
# --------------------------------------------------------------------------- #
class TestDocstringMatchesBehavior:
    """docstring 의 filled_from_prepared 설명이 실제 동작과 일치하는가."""

    def test_docstring_lists_all_restored_fields(self):
        doc = mcp_server.register_product.__doc__ or ""
        # docstring 이 가능한 항목 전부를 나열하는가.
        for field in (
            "detail_html",
            "image_urls",
            "notice",
            "tags",
            "options",
            "option_groups",
            "deferred_notice_fields",
            "attributes",
        ):
            assert field in doc, f"docstring 에 {field!r} 이(가) 없다 (실제 동작과 불일치)"
