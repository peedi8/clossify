# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""상품군별 등록 템플릿 저장소 검증 (이슈: 템플릿 저장 — 상품군별 복수, 자동 적용 금지).

본 파일은 다음 계약 지점을 검증한다:

(a) 저장 → list_templates 에 이름·고시타입이 나타난다. **값은 나타나지 않는다.**
(b) 저장된 템플릿 파일(templates.json)에 name/price/image/secret 이 없다.
(c) apply_template 이름 안 줌 → 어떤 템플릿도 적용되지 않는다(암묵 적용 금지).
(d) 사용자가 직접 준 값을 템플릿이 덮어쓰지 않는다(빈 자리만 채운다).
(e) 적용 결과(template_applied)에 출처(어느 템플릿에서 왔는지)가 드러난다.
(f) 존재하지 않는 템플릿 이름 → 조용히 넘기지 않고 명확한 사유.
(g) 파일 부재 → 오류 아님(빈 상태). 손상 → 조용히 덮어쓰지 않고 사유.
(h) 고시타입이 다른 템플릿 2개 이상이 서로 구별된다.
(i) check_config 기존 반환 키가 모두 보존된다(새 키만 추가).
(j) check_config 의 **모든 반환 경로**(설정 없음·손상·naver 비정상·정상)가
    ``templates``/``templates_read_error`` 키를 포함한다 (조용한 누락 금지).

모든 테스트는 ``common.STATE_DIR`` 을 tmp_path 로 격리한다 — 실제 사용자
``.local/templates.json`` 을 건드리지 않는다. 외부 API 호출, 네이버 실호출 없음.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import common, listing_templates, mcp_server, naver_client


# --------------------------------------------------------------------------- #
# 공통 픽스처.
# --------------------------------------------------------------------------- #
@pytest.fixture
def isolated_state_dir(tmp_path, monkeypatch):
    """``common.STATE_DIR`` 을 tmp_path 로 격리.

    ``listing_templates.templates_path`` 가 호출 시점에 ``common.STATE_DIR`` 을
    읽으므로, monkeypatch 로 교체한다. 이미 import 시점에 바인딩 된 상수
    (``PREPARED_DIR`` 등)도 함께 격리한다 — prepare_listing 종단 테스트에서
    쓰기 충돌을 막기 위함.
    """
    fake_state = tmp_path / ".local"
    fake_state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(common, "STATE_DIR", fake_state)
    monkeypatch.setattr(common, "LOCAL_DIR", fake_state)
    prepared = fake_state / "prepared"
    prepared.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(common, "PREPARED_DIR", prepared)
    return fake_state


# notice_config mock — 원산지/AS/제조사 + ETC 공통 5필드.
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

_COMMON_CFG_MOCK = {
    "smartstore_notice_defaults": {
        "origin_area_code": "04",
        "origin_content": "중국",
    },
}


def _apply_common_mocks(monkeypatch):
    """prepare_listing 종단 테스트에서 게이트 mock."""
    monkeypatch.setattr(naver_client, "_notice_config", lambda: _NOTICE_MOCK)
    monkeypatch.setattr(naver_client, "_kc_config", lambda: ({}, ""))
    monkeypatch.setattr(common, "cfg", lambda: _COMMON_CFG_MOCK)


def _make_attach_result(urls):
    """images.attach_images 스텁."""
    return {"urls": list(urls), "rejected": [], "detail": []}


def _compliant_product_input(name="템플릿테스트상품", price=39000):
    """prepare_listing 용 유효 상품 입력."""
    return {
        "name": name,
        "salePrice": price,
        "image_sources": ["http://cdn/img1.png"],
        "categoryId": "50021299",  # WEAR 추론 카테고리
    }


# =========================================================================== #
# (a) 저장 → list_templates 에 이름·고시타입이 나타난다. 값은 나타나지 않는다.
# =========================================================================== #
class TestSaveAndListShape:
    """save_template → list_templates 의 형태 계약."""

    def test_list_shows_name_and_notice_type_not_values(self, isolated_state_dir):
        product = {
            "name": "상품A",
            "salePrice": 10000,
            "return_cost_reason": "단순변심 반품비용 구매자부담",
            "as_tel": "070-1234-5678",
            "origin_code": "04",
            "made_in": "중국",
        }
        result = listing_templates.save_template(
            name="etc-기본값", notice_type="ETC", product=product
        )
        assert result["ok"] is True
        assert result["name"] == "etc-기본값"
        assert result["notice_type"] == "ETC"

        templates = listing_templates.list_templates()
        assert len(templates) == 1
        entry = templates[0]
        # 이름·고시타입·생성일은 있다.
        assert entry["name"] == "etc-기본값"
        assert entry["notice_type"] == "ETC"
        assert entry.get("created_at")
        # **값 자체는 없다** — list_templates 반환에 fields/values/본문 키가 없다.
        assert "fields" not in entry
        assert "values" not in entry
        assert "return_cost_reason" not in entry
        assert "as_tel" not in entry

    def test_saved_keys_reports_sections_not_values(self, isolated_state_dir):
        """save_template 반환의 saved_keys/skipped_keys 는 섹션/키 이름만 담는다.

        값 자체는 절대 담기지 않는다(비밀값 비노출 — 반환은 로그/반환에 흘러도 안전).
        """
        product = {
            "name": "상품A",
            "salePrice": 10000,
            "return_cost_reason": "민감한 반품문구",  # 본문 값 — saved_keys 에 이름만
            "as_tel": "070-1234-5678",
            "client_secret": "super-secret-token",  # skipped — 비밀값
        }
        result = listing_templates.save_template(
            name="sec-test", notice_type="ETC", product=product
        )
        # saved_keys/skipped_keys 는 문자열 이름만 담는다.
        for k in result.get("saved_keys") or []:
            assert isinstance(k, str)
        for k in result.get("skipped_keys") or []:
            assert isinstance(k, str)
        # 값 자체가 반환에 없다.
        flat = json.dumps(result, ensure_ascii=False)
        assert "super-secret-token" not in flat
        assert "민감한 반품문구" not in flat


# =========================================================================== #
# (b) 저장된 템플릿 파일(templates.json)에 name/price/image/secret 이 없다.
# =========================================================================== #
class TestStoredFileExcludesUnsafeFields:
    """save_template 이 화이트리스트 밖 필드를 파일에 쓰지 않는가."""

    def test_stored_json_has_no_name_price_image_secret(self, isolated_state_dir):
        product = {
            "name": "상품명-비밀값아님하지만안담음",
            "title_ko": "제목-마찬가지",
            "salePrice": 99000,
            "sell_price": 99000,
            "stock": 50,
            "stockQuantity": 50,
            "image_sources": ["http://cdn/a.png", "http://cdn/b.png"],
            "images": [{"url": "http://cdn/a.png"}],
            "options": [{"name": "블랙", "stock": 5}],
            "tags": ["신상"],
            "categoryId": "50021299",
            # 비밀값 — 어떤 형태든 담기면 안 된다.
            "client_id": "cid-xyz",
            "client_secret": "csec-SECRET-TOKEN",
            "api_key": "ak-SECRET",
            "access_token": "at-SECRET",
            "secret": "raw-secret",
            "token": "raw-token",
            "password": "raw-password",
            # 안전한 필드 — 담겨야 한다.
            "return_cost_reason": "단순변심 반품비용 구매자부담",
            "as_tel": "070-1234-5678",
            "origin_code": "04",
            "made_in": "중국",
            "manufacturer": "테스트제조사",
        }
        listing_templates.save_template(name="safe-only", notice_type="ETC", product=product)

        # 파일을 직접 읽어 화이트리스트 위반 키가 없는지 확인.
        path = listing_templates.templates_path()
        raw = path.read_text(encoding="utf-8")
        doc = json.loads(raw)

        # 스키마 최소 형태.
        assert doc["version"] == 1
        assert isinstance(doc["templates"], list)
        assert len(doc["templates"]) == 1
        entry = doc["templates"][0]
        assert entry["name"] == "safe-only"
        assert entry["notice_type"] == "ETC"

        # 담긴 안전 섹션.
        fields = entry["fields"]
        assert "productInfoProvidedNotice" in fields
        assert "afterServiceInfo" in fields
        assert "origin" in fields
        assert "manufacturer_brand" in fields

        # 화이트리스트 밖 값이 파일 전체에 없다(비밀값/상품특정값).
        forbidden = [
            "상품명-비밀값아님하지만안담음",
            "제목-마찬가지",
            "99000",  # 가격
            "50",  # 재고 — 위험: 숫자가 다른 값에 우연히 겹칠 수 있다.
            "http://cdn/a.png",
            "http://cdn/b.png",
            "블랙",
            "신상",
            "50021299",
            "cid-xyz",
            "csec-SECRET-TOKEN",
            "ak-SECRET",
            "at-SECRET",
            "raw-secret",
            "raw-token",
            "raw-password",
        ]
        for needle in forbidden:
            if needle == "50":
                # "50" 은 다른 값(예: 50자 제한)에 우연히 겹칠 수 있어 스킵 —
                # 대신 stock/stockQuantity 키가 파일에 없는지 별도로 확인.
                continue
            assert needle not in raw, f"금지된 값이 templates.json 에 있다: {needle!r}"

        # stock/stockQuantity/categoryId 키 자체가 fields 어디에도 없다.
        flat = json.dumps(fields, ensure_ascii=False)
        for forbidden_key in (
            "stock",
            "stockQuantity",
            "categoryId",
            "image_sources",
            "images",
            "options",
            "tags",
            "client_id",
            "client_secret",
            "api_key",
            "access_token",
            "secret",
            "token",
            "password",
        ):
            assert (
                forbidden_key not in flat
            ), f"금지된 키가 templates.json fields 에 있다: {forbidden_key!r}"


# =========================================================================== #
# (c) apply_template 이름 안 줌 → 어떤 템플릿도 적용되지 않는다.
# =========================================================================== #
class TestNoImplicitApplication:
    """빈 이름 → 적용 0건 (암묵 적용 금지)."""

    def test_empty_name_applies_nothing(self, isolated_state_dir):
        # 템플릿 하나를 미리 저장해 둔다.
        listing_templates.save_template(
            name="단하나", notice_type="ETC", product={"return_cost_reason": "문구"}
        )
        product = {"name": "X", "salePrice": 1000}
        result = listing_templates.apply_template(name="", product=product)
        assert result["applied"] is False
        assert result["filled"] == []
        # product 가 변경되지 않았다.
        assert "return_cost_reason" not in product

    def test_whitespace_name_applies_nothing(self, isolated_state_dir):
        listing_templates.save_template(
            name="공백무시", notice_type="ETC", product={"return_cost_reason": "문구"}
        )
        product = {"name": "X", "salePrice": 1000}
        result = listing_templates.apply_template(name="   ", product=product)
        assert result["applied"] is False
        assert "return_cost_reason" not in product

    def test_only_named_template_applies(self, isolated_state_dir):
        """저장된 템플릿이 여러 개여도 이름을 안 주면 아무것도 적용되지 않는다."""
        listing_templates.save_template(
            name="t1", notice_type="ETC", product={"return_cost_reason": "v1"}
        )
        listing_templates.save_template(
            name="t2", notice_type="ETC", product={"return_cost_reason": "v2"}
        )
        product = {"name": "X", "salePrice": 1000}
        result = listing_templates.apply_template(name="", product=product)
        assert result["applied"] is False
        # 가장 최근 것/하나뿐이니까 적용하는 암묵 경로가 없다.
        assert "return_cost_reason" not in product


# =========================================================================== #
# (d) 사용자가 직접 준 값을 템플릿이 덮어쓰지 않는다.
# =========================================================================== #
class TestUserValuePreserved:
    """템플릿 값은 빈 자리만 채운다 — 사용자가 준 값을 우선한다."""

    def test_user_provided_value_not_overwritten(self, isolated_state_dir):
        # 템플릿은 return_cost_reason="템플릿문구" 를 가짐.
        listing_templates.save_template(
            name="overwrite-test",
            notice_type="ETC",
            product={"return_cost_reason": "템플릿문구", "as_tel": "070-0000-0000"},
        )
        # 사용자는 return_cost_reason="내가직접" 을 줬다.
        product = {
            "name": "X",
            "salePrice": 1000,
            "return_cost_reason": "내가직접",
        }
        result = listing_templates.apply_template(name="overwrite-test", product=product)
        # 사용자 값이 보존된다.
        assert product["return_cost_reason"] == "내가직접"
        # 빈 자리(as_tel)는 템플릿에서 채워진다.
        assert product.get("as_tel") == "070-0000-0000"
        # 결과 메타에 사용자 값이라 덮지 않았음이 드러난다.
        skipped_sections = {(s["section"], s["field"]) for s in result["skipped_existing"]}
        # returnCostReason 이 skipped_existing 에 있다.
        assert any(f == "returnCostReason" for _, f in skipped_sections)

    def test_template_fills_only_empty_slots(self, isolated_state_dir):
        listing_templates.save_template(
            name="empty-slot",
            notice_type="ETC",
            product={"return_cost_reason": "템플릿", "no_refund_reason": "템플릿환불"},
        )
        product = {"name": "X", "salePrice": 1000}
        result = listing_templates.apply_template(name="empty-slot", product=product)
        assert result["applied"] is True
        # 빈 자리가 채워진다. 고시 본문 노드(etc) 에 camelCase 필드로 들어간다
        # — 이는 naver_client._merge_notice 가 읽는 구조와 동일하다.
        etc_body = product.get("notice", {}).get("etc", {})
        assert etc_body.get("returnCostReason") == "템플릿"
        assert etc_body.get("noRefundReason") == "템플릿환불"


# =========================================================================== #
# (e) 적용 결과(template_applied)에 출처가 드러난다.
# =========================================================================== #
class TestApplyResultSourceTracking:
    """적용 결과에 어느 템플릿에서 왔는지(source)가 드러난다."""

    def test_apply_result_carries_template_name(self, isolated_state_dir):
        listing_templates.save_template(
            name="출처템플릿",
            notice_type="ETC",
            product={"return_cost_reason": "템플릿문구"},
        )
        product = {"name": "X", "salePrice": 1000}
        result = listing_templates.apply_template(name="출처템플릿", product=product)
        assert result["applied"] is True
        # template_name 으로 출처가 드러난다.
        assert result["template_name"] == "출처템플릿"
        assert result["notice_type"] == "ETC"
        # filled 각 항목은 어느 필드가 채워졌는지 드러낸다.
        assert len(result["filled"]) >= 1
        for item in result["filled"]:
            assert "section" in item
            assert "field" in item

    def test_prepare_listing_return_carries_template_applied(self, isolated_state_dir, monkeypatch):
        """prepare_listing 반환의 template_applied 에 출처가 있다."""
        _apply_common_mocks(monkeypatch)
        monkeypatch.setattr(
            "clossify.images.attach_images",
            lambda srcs: _make_attach_result(["http://cdn/a.png"]),
        )
        # 템플릿 저장.
        listing_templates.save_template(
            name="prepare-출처",
            notice_type="WEAR",
            product={"return_cost_reason": "템플릿문구WEAR"},
        )
        # WEAR 상품 입력에 템플릿 적용.
        product = _compliant_product_input()
        result = mcp_server.prepare_listing(product, apply_template="prepare-출처")
        assert result["ok"] is True, f"prepare 실패: {result.get('error')}"
        ta = result.get("template_applied")
        assert ta is not None
        assert ta["applied"] is True
        assert ta["template_name"] == "prepare-출처"


# =========================================================================== #
# (f) 존재하지 않는 템플릿 이름 → 조용히 넘기지 않고 명확한 사유.
# =========================================================================== #
class TestMissingTemplateNameReportsReason:
    """요청한 템플릿이 없을 때 조용한 실패 금지."""

    def test_missing_name_returns_not_found_reason(self, isolated_state_dir):
        # 저장된 템플릿은 하나도 없다.
        product = {"name": "X", "salePrice": 1000}
        result = listing_templates.apply_template(name="없는템플릿", product=product)
        assert result["applied"] is False
        assert result["not_found"] is not None
        # 사유에 이름이 들어있다.
        assert "없는템플릿" in result["not_found"]

    def test_prepare_listing_missing_template_reports_reason(self, isolated_state_dir, monkeypatch):
        """prepare_listing 에서 없는 템플릿을 적용하려 해도 사유가 드러난다."""
        _apply_common_mocks(monkeypatch)
        monkeypatch.setattr(
            "clossify.images.attach_images",
            lambda srcs: _make_attach_result(["http://cdn/a.png"]),
        )
        result = mcp_server.prepare_listing(
            _compliant_product_input(), apply_template="절대없는이름"
        )
        # 준비 자체는 진행된다(부분 실패 허용).
        assert result["ok"] is True
        ta = result.get("template_applied")
        assert ta is not None
        assert ta["applied"] is False
        assert ta.get("not_found")
        assert "절대없는이름" in ta["not_found"]


# =========================================================================== #
# (g) 파일 부재 → 오류 아님. 손상 → 조용히 덮어쓰지 않고 사유.
# =========================================================================== #
class TestMissingAndCorruptedStore:
    """파일 부재/손상 케이스."""

    def test_missing_file_is_empty_state(self, isolated_state_dir):
        # 파일이 없다.
        assert not listing_templates.templates_path().is_file()
        # list_templates → 빈 리스트(오류 아님).
        assert listing_templates.list_templates() == []
        # apply_template 빈 이름 → 적용 안 함.
        result = listing_templates.apply_template(name="", product={"name": "X"})
        assert result["applied"] is False

    def test_corrupted_file_raises_not_silent_overwrite(self, isolated_state_dir):
        # 손상된 JSON 을 직접 쓴다.
        path = listing_templates.templates_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ 이건 json 이 아니다", encoding="utf-8")
        # list_templates → TemplateStoreError (조용히 빈 리스트로 덮어쓰지 않는다).
        with pytest.raises(listing_templates.TemplateStoreError):
            listing_templates.list_templates()
        # apply_template 도 조용히 넘기지 않는다.
        with pytest.raises(listing_templates.TemplateStoreError):
            listing_templates.apply_template(name="x", product={"name": "X"})

    def test_save_on_corrupted_does_not_silently_overwrite(self, isolated_state_dir):
        """저장 경로도 손상된 저장소를 조용히 덮어쓰지 않는다."""
        path = listing_templates.templates_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not-json", encoding="utf-8")
        with pytest.raises(listing_templates.TemplateStoreError):
            listing_templates.save_template(name="x", notice_type="ETC", product={"name": "X"})
        # 파일이 그대로 손상된 상태로 남는다(조용히 덮어쓰지 않았다).
        assert path.read_text(encoding="utf-8") == "not-json"

    def test_check_config_corrupted_templates_reports_reason(self, isolated_state_dir, monkeypatch):
        """check_config 가 손상된 템플릿 저장소를 만나면 사유를 알린다(조용한 빈 목록 X)."""
        # check_config 가 config.json 을 찾을 수 있게 최소 config 작성.
        cfg_path = isolated_state_dir / "config.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "naver": {
                        "client_id": "cid",
                        "client_secret": "csec",
                        "store_url_slug": "slug",
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_path))
        # 손상된 templates.json.
        (isolated_state_dir / "templates.json").write_text("corrupted", encoding="utf-8")
        result = mcp_server.check_config()
        # 기존 진단 키는 살아 있다(부분 실패 허용).
        assert "ok" in result
        assert "present" in result
        # 템플릿 사유가 드러난다.
        assert result.get("templates") == []
        assert result.get("templates_read_error") is not None
        assert (
            "손상" in result["templates_read_error"] or "템플릿" in result["templates_read_error"]
        )


# =========================================================================== #
# (h) 고시타입이 다른 템플릿 2개 이상이 서로 구별된다.
# =========================================================================== #
class TestMultipleTemplatesDistinguishedByNoticeType:
    """상품군별 복수 템플릿이 notice_type 축으로 구별된다."""

    def test_two_templates_different_notice_types_listed(self, isolated_state_dir):
        listing_templates.save_template(
            name="etc-셋",
            notice_type="ETC",
            product={"return_cost_reason": "ETC문구", "as_tel": "070-1"},
        )
        listing_templates.save_template(
            name="wear-셋",
            notice_type="WEAR",
            product={"return_cost_reason": "WEAR문구", "as_tel": "070-2"},
        )
        templates = listing_templates.list_templates()
        assert len(templates) == 2
        names_to_types = {t["name"]: t["notice_type"] for t in templates}
        assert names_to_types == {"etc-셋": "ETC", "wear-셋": "WEAR"}

    def test_applying_one_does_not_bleed_other(self, isolated_state_dir):
        """한 템플릿 적용이 다른 템플릿의 값으로 섞이지 않는다."""
        listing_templates.save_template(
            name="etc-단독",
            notice_type="ETC",
            product={"return_cost_reason": "ETC-ONLY-VALUE"},
        )
        listing_templates.save_template(
            name="wear-단독",
            notice_type="WEAR",
            product={"return_cost_reason": "WEAR-ONLY-VALUE"},
        )
        product = {"name": "X", "salePrice": 1000}
        result = listing_templates.apply_template(name="wear-단독", product=product)
        assert result["template_name"] == "wear-단독"
        assert result["notice_type"] == "WEAR"
        # wear 템플릿의 값만 들어있다 — etc 값이 섞이지 않는다. 고시 본문 노드
        # (wear) 에 camelCase 필드로 들어간다(naver_client._merge_notice 구조).
        wear_body = product.get("notice", {}).get("wear", {})
        assert wear_body.get("returnCostReason") == "WEAR-ONLY-VALUE"

    def test_replacing_same_name_updates_in_place(self, isolated_state_dir):
        """같은 이름으로 다시 저장하면 갱신된다(목록 길이 증가 없이)."""
        listing_templates.save_template(
            name="갱신대상", notice_type="ETC", product={"as_tel": "070-old"}
        )
        listing_templates.save_template(
            name="갱신대상", notice_type="ETC", product={"as_tel": "070-new"}
        )
        templates = listing_templates.list_templates()
        assert len(templates) == 1  # 같은 이름 → 갱신, 추가 아님.
        # 새 값으로 적용된다.
        product = {"name": "X", "salePrice": 1000}
        listing_templates.apply_template(name="갱신대상", product=product)
        assert product.get("as_tel") == "070-new"


# =========================================================================== #
# (i) check_config 기존 반환 키가 모두 보존된다 (새 키만 추가).
# =========================================================================== #
class TestCheckConfigKeysPreserved:
    """check_config 반환에서 기존 키가 보존되는가."""

    _EXISTING_KEYS = frozenset(
        {
            "ok",
            "config_path",
            "present",
            "missing",
            "placeholders",
            "error",
            "origin_configured",
            "as_tel_configured",
            "policy_gaps",
            "suggested_from_existing",
            "drift_from_existing",
            "existing_read_error",
            "config_form_path",
            "config_form_open",
            "image_generation_configured",
        }
    )

    def test_all_existing_keys_present_when_config_valid(self, isolated_state_dir, monkeypatch):
        # 유효 config 작성.
        cfg_path = isolated_state_dir / "config.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "naver": {
                        "client_id": "cid",
                        "client_secret": "csec",
                        "store_url_slug": "slug",
                    },
                    "smartstore_notice_defaults": {
                        "origin_area_code": "04",
                        "origin_content": "중국",
                        "as_tel": "070-1234-5678",
                    },
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_path))
        result = mcp_server.check_config()
        # 기존 키가 모두 있다.
        missing_keys = self._EXISTING_KEYS - set(result.keys())
        assert not missing_keys, f"기존 check_config 키가 사라졌다: {missing_keys}"
        # 새 키도 있다.
        assert "templates" in result
        assert "templates_read_error" in result
        # 빈 저장소 → 빈 리스트, 오류 None.
        assert result["templates"] == []
        assert result["templates_read_error"] is None

    def test_templates_listed_in_check_config_after_save(self, isolated_state_dir, monkeypatch):
        cfg_path = isolated_state_dir / "config.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "naver": {
                        "client_id": "cid",
                        "client_secret": "csec",
                        "store_url_slug": "slug",
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_path))
        listing_templates.save_template(
            name="cc-template", notice_type="ETC", product={"as_tel": "070-1"}
        )
        result = mcp_server.check_config()
        names = [t["name"] for t in result.get("templates") or []]
        assert "cc-template" in names


# =========================================================================== #
# (j) check_config 의 모든 반환 경로가 templates 키를 포함한다.
#
# 설정이 없거나 손상된 경로에서 조기 반환할 때 templates 키가 사라지면
# 사용자가 자기 템플릿을 조회할 방법이 없고, 호출부가 키 유무를 예측할 수 없다
# (조용한 누락). 템플릿 저장소는 config.json 과 별개 파일이므로 설정 상태와
# 독립적으로 읽힌다.
# =========================================================================== #
class TestCheckConfigTemplatesKeysOnAllReturnPaths:
    """모든 조기 반환 경로에서 templates/templates_read_error 키가 있다."""

    def test_missing_config_still_returns_templates_key(self, isolated_state_dir, monkeypatch):
        """(a) 설정 없음 + 템플릿 1개 저장 → 반환에 templates 키가 있고 그 1개가 보인다."""
        # config.json 은 없다. templates.json 에 템플릿 1개 저장.
        listing_templates.save_template(
            name="no-cfg-template", notice_type="ETC", product={"as_tel": "070-1"}
        )
        # check_config 가 config.json 을 찾을 수 없게(파일 부재 경로).
        missing_cfg = isolated_state_dir / "absent-config.json"
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(missing_cfg))
        result = mcp_server.check_config()
        # 조기 반환 경로(파일 부재)인데도 templates 키가 있다.
        assert "templates" in result
        assert "templates_read_error" in result
        # 저장된 템플릿 1개가 보인다(이름·고시타입·생성일만).
        names = [t["name"] for t in result["templates"]]
        assert "no-cfg-template" in names
        assert result["templates_read_error"] is None
        # 설정 진단은 그대로(설정 없음).
        assert result["ok"] is False
        assert result["error"] is not None

    def test_corrupted_config_still_returns_templates_key(self, isolated_state_dir, monkeypatch):
        """(b) 설정 손상 → templates 키가 있다."""
        # 손상된 config.json.
        cfg_path = isolated_state_dir / "config.json"
        cfg_path.write_text("{ 이건 json 이 아니다", encoding="utf-8")
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_path))
        # 템플릿 1개 저장(별도 파일 — 정상).
        listing_templates.save_template(
            name="corrupt-cfg-tmpl", notice_type="ETC", product={"as_tel": "070-2"}
        )
        result = mcp_server.check_config()
        # 조기 반환 경로(파싱 실패)인데도 templates 키가 있다.
        assert "templates" in result
        assert "templates_read_error" in result
        names = [t["name"] for t in result["templates"]]
        assert "corrupt-cfg-tmpl" in names
        # 설정 진단은 그대로(손상).
        assert result["ok"] is False
        assert result["error"] is not None

    def test_naver_not_dict_still_returns_templates_key(self, isolated_state_dir, monkeypatch):
        """naver 섹션이 객체가 아닌 경로에서도 templates 키가 있다."""
        cfg_path = isolated_state_dir / "config.json"
        cfg_path.write_text(
            json.dumps({"naver": "문자열-객체아님"}),
            encoding="utf-8",
        )
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_path))
        listing_templates.save_template(
            name="bad-naver-tmpl", notice_type="ETC", product={"as_tel": "070-3"}
        )
        result = mcp_server.check_config()
        assert "templates" in result
        assert "templates_read_error" in result
        names = [t["name"] for t in result["templates"]]
        assert "bad-naver-tmpl" in names

    def test_valid_config_templates_key_unchanged(self, isolated_state_dir, monkeypatch):
        """(c) 설정 정상 → 기존 거동 그대로(회귀)."""
        cfg_path = isolated_state_dir / "config.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "naver": {
                        "client_id": "cid",
                        "client_secret": "csec",
                        "store_url_slug": "slug",
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_path))
        result = mcp_server.check_config()
        assert "templates" in result
        assert "templates_read_error" in result
        assert result["templates"] == []
        assert result["templates_read_error"] is None

    def test_corrupted_templates_with_missing_config_reports_both(
        self, isolated_state_dir, monkeypatch
    ):
        """(d) 템플릿 저장소 손상 + 설정 없음 → templates_read_error 에 사유,
        설정 진단 키(ok/error)는 정상 동작."""
        # config 없음.
        missing_cfg = isolated_state_dir / "absent-config.json"
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(missing_cfg))
        # 손상된 templates.json.
        (isolated_state_dir / "templates.json").write_text("corrupted", encoding="utf-8")
        result = mcp_server.check_config()
        # 템플릿 사유가 담긴다.
        assert result["templates"] == []
        assert result["templates_read_error"] is not None
        assert (
            "손상" in result["templates_read_error"] or "템플릿" in result["templates_read_error"]
        )
        # 설정 진단 키는 이 실패와 무관하게 정상 동작(설정 없음).
        assert result["ok"] is False
        assert result["error"] is not None

    def test_no_template_values_leaked_on_any_path(self, isolated_state_dir, monkeypatch):
        """(e) 어느 경우에도 템플릿 값이 반환에 없다(이름·고시타입·생성일만)."""
        # 민감한 값이 섞인 템플릿 저장.
        listing_templates.save_template(
            name="leak-check",
            notice_type="ETC",
            product={
                "return_cost_reason": "LEAK-SENSITIVE-REASON",
                "as_tel": "LEAK-SENSITIVE-TEL",
                "client_secret": "LEAK-SECRET",
            },
        )
        # 설정 없음 경로에서 반환 전체를 덤프해 민감값이 없는지 확인.
        missing_cfg = isolated_state_dir / "absent-config.json"
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(missing_cfg))
        result = mcp_server.check_config()
        flat = json.dumps(result, ensure_ascii=False)
        assert "LEAK-SENSITIVE-REASON" not in flat
        assert "LEAK-SENSITIVE-TEL" not in flat
        assert "LEAK-SECRET" not in flat
        # 이름(메타)은 보인다 — 이것은 의도된 가시성이다.
        assert "leak-check" in flat

    def test_all_existing_keys_present_on_early_return(self, isolated_state_dir, monkeypatch):
        """(f) 기존 반환 키가 전부 그대로다 — 조기 반환 경로에서도 회귀 없음."""
        # 기존 키 집합(TestCheckConfigKeysPreserved 와 동일 기준).
        existing_keys = frozenset(
            {
                "ok",
                "config_path",
                "present",
                "missing",
                "placeholders",
                "error",
                "config_form_path",
                "config_form_open",
            }
        )
        # 설정 없음 경로.
        missing_cfg = isolated_state_dir / "absent-config.json"
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(missing_cfg))
        result = mcp_server.check_config()
        missing_keys = existing_keys - set(result.keys())
        assert not missing_keys, f"설정 없음 경로에서 기존 키가 사라졌다: {missing_keys}"
        # 새 키도 있다.
        assert "templates" in result
        assert "templates_read_error" in result


# =========================================================================== #
# 종단: prepare_listing 의 save_as_template 경로.
# =========================================================================== #
class TestPrepareListingSaveAsTemplate:
    """prepare_listing(product, save_as_template=name) 가 템플릿을 저장하는가."""

    def test_save_as_template_persists(self, isolated_state_dir, monkeypatch):
        _apply_common_mocks(monkeypatch)
        monkeypatch.setattr(
            "clossify.images.attach_images",
            lambda srcs: _make_attach_result(["http://cdn/a.png"]),
        )
        product = _compliant_product_input()
        result = mcp_server.prepare_listing(product, save_as_template="prepare저장")
        assert result["ok"] is True, f"prepare 실패: {result.get('error')}"
        ts = result.get("template_saved")
        assert ts is not None
        assert ts["ok"] is True
        assert ts["name"] == "prepare저장"
        # 파일에 실제로 있다.
        templates = listing_templates.list_templates()
        names = [t["name"] for t in templates]
        assert "prepare저장" in names

    def test_save_as_template_excludes_secrets(self, isolated_state_dir, monkeypatch):
        """prepare_listing 저장 경로도 화이트리스트를 적용한다."""
        _apply_common_mocks(monkeypatch)
        monkeypatch.setattr(
            "clossify.images.attach_images",
            lambda srcs: _make_attach_result(["http://cdn/a.png"]),
        )
        # 비밀값이 섞인 상품 입력.
        product = _compliant_product_input()
        product["client_secret"] = "prepare-SECRET"
        product["api_key"] = "prepare-AK"
        result = mcp_server.prepare_listing(product, save_as_template="prepare-sec")
        assert result["ok"] is True
        # 저장소 파일에 비밀값이 없다.
        raw = listing_templates.templates_path().read_text(encoding="utf-8")
        assert "prepare-SECRET" not in raw
        assert "prepare-AK" not in raw


# =========================================================================== #
# 백업: 저장 전 백업 파일이 생성되는가.
# =========================================================================== #
class TestBackupBeforeWrite:
    """save_template 가 기존 파일을 백업하는가(config_form_server 관례)."""

    def test_backup_file_created_on_overwrite(self, isolated_state_dir):
        listing_templates.save_template(
            name="백업1", notice_type="ETC", product={"as_tel": "070-1"}
        )
        first_path = listing_templates.templates_path()
        # 같은 파일에 두 번째 저장 — 백업이 생겨야 한다.
        listing_templates.save_template(
            name="백업2", notice_type="ETC", product={"as_tel": "070-2"}
        )
        backups = list(first_path.parent.glob("templates.json.bak.*"))
        assert len(backups) >= 1, "저장 전 백업 파일이 없다"


# =========================================================================== #
# 미루기 선언(deferred_notice_fields) 템플릿 저장·적용.
#
# 계약:
#   1. 선언 포함 상품으로 저장 → 템플릿 JSON 에 목록 존재.
#      선언 없이 저장 → 키 부재(빈 리스트 저장 금지).
#   2. 적용: 입력에 선언 없음 → 템플릿 것 채움 + deferred_from_template 보고.
#      입력에 선언 있음 → 입력 그대로(템플릿 무시).
#   3. 템플릿에 미루기 불가 필드(예: 불리언 필드명)를 인위로 넣고 적용 →
#      그 필드만 제외 + deferred_dropped_invalid 보고, 나머지는 적용.
#   4. 실경로 1회: prepare_listing 을 apply_template 로 호출해 반환/저장 payload 에
#      선언이 실려 있음(실호출 출력).
# =========================================================================== #
class TestDeferredNoticeFieldsTemplate:
    """미루기 선언(deferred_notice_fields) 의 템플릿 저장·적용 왕복."""

    def test_save_with_deferred_stores_list(self, isolated_state_dir):
        """선언 포함 상품으로 저장 → 템플릿 JSON 에 목록이 있다."""
        product = {
            "return_cost_reason": "단순변심 반품비용 구매자부담",
            # returnCostReason 은 string 타입 + allowlist 내 → 미루기 가능.
            "deferred_notice_fields": ["returnCostReason"],
        }
        listing_templates.save_template(name="defer-저장", notice_type="ETC", product=product)
        # 파일에서 직접 확인.
        path = listing_templates.templates_path()
        raw = path.read_text(encoding="utf-8")
        doc = json.loads(raw)
        entry = doc["templates"][0]
        fields = entry["fields"]
        assert "deferred_notice_fields" in fields
        assert fields["deferred_notice_fields"] == ["returnCostReason"]

    def test_save_without_deferred_has_no_key(self, isolated_state_dir):
        """선언 없이 저장 → 템플릿 JSON 에 deferred_notice_fields 키가 없다.

        빈 리스트로 저장하지 않는다 — 키 부재와 빈 리스트는 의미가 다르다.
        """
        product = {"return_cost_reason": "문구"}
        listing_templates.save_template(name="no-defer", notice_type="ETC", product=product)
        path = listing_templates.templates_path()
        doc = json.loads(path.read_text(encoding="utf-8"))
        fields = doc["templates"][0]["fields"]
        assert "deferred_notice_fields" not in fields

    def test_save_deferred_filters_invalid_fields(self, isolated_state_dir):
        """저장 시 미루기 불가 필드(boolean/date) 는 정제되어 빠진다.

        저장 단계에서 정제하므로, 템플릿 JSON 에는 미루기 가능 필드만 남는다.
        importDeclaration 은 allowlist 에 있지만 boolean 타입 → 미루기 불가.
        """
        product = {
            "return_cost_reason": "문구",
            "deferred_notice_fields": [
                "returnCostReason",  # string → 가능.
                "importDeclaration",  # boolean → 불가.
            ],
        }
        result = listing_templates.save_template(
            name="defer-정제", notice_type="ETC", product=product
        )
        assert result["ok"] is True
        path = listing_templates.templates_path()
        doc = json.loads(path.read_text(encoding="utf-8"))
        fields = doc["templates"][0]["fields"]
        # returnCostReason 만 남고 importDeclaration 은 빠졌다.
        assert fields.get("deferred_notice_fields") == ["returnCostReason"]

    def test_apply_fills_deferred_from_template(self, isolated_state_dir):
        """입력에 선언 없음 → 템플릿에서 채움 + deferred_from_template 보고."""
        # 템플릿에 선언을 저장.
        listing_templates.save_template(
            name="defer-적용",
            notice_type="ETC",
            product={
                "return_cost_reason": "문구",
                "deferred_notice_fields": ["returnCostReason"],
            },
        )
        # 상품 입력에는 deferred_notice_fields 키가 없다.
        product = {"name": "X", "salePrice": 1000}
        result = listing_templates.apply_template(name="defer-적용", product=product)
        # 템플릿에서 채워졌다.
        assert product.get("deferred_notice_fields") == ["returnCostReason"]
        # 출처 보고.
        assert result["deferred_from_template"] == ["returnCostReason"]
        assert result["deferred_dropped_invalid"] == []

    def test_apply_input_deferred_wins_over_template(self, isolated_state_dir):
        """입력에 선언 있음 → 입력 그대로(템플릿 무시).

        입력 우선 원칙 — 명시가 정본. 합치지 않는다.
        """
        listing_templates.save_template(
            name="defer-입력우선",
            notice_type="ETC",
            product={
                "return_cost_reason": "템플릿문구",
                "deferred_notice_fields": ["returnCostReason"],
            },
        )
        # 상품 입력에는 다른 필드를 미루기로 선언.
        product = {
            "name": "X",
            "salePrice": 1000,
            "deferred_notice_fields": ["noRefundReason"],
        }
        result = listing_templates.apply_template(name="defer-입력우선", product=product)
        # 입력 선언이 그대로 유지된다 (템플릿 것이 합쳐지지 않는다).
        assert product["deferred_notice_fields"] == ["noRefundReason"]
        # 템플릿에서 채운 것이 아니므로 보고도 없다.
        assert result["deferred_from_template"] == []
        assert result["deferred_dropped_invalid"] == []

    def test_apply_input_empty_list_wins_over_template(self, isolated_state_dir):
        """입력에 빈 리스트 선언 → "아무것도 미루지 않겠다" (입력 우선).

        빈 리스트도 "입력이 있다" 로 존중한다 — 키 부재와 다르다.
        """
        listing_templates.save_template(
            name="defer-빈리스트",
            notice_type="ETC",
            product={
                "return_cost_reason": "템플릿문구",
                "deferred_notice_fields": ["returnCostReason"],
            },
        )
        product = {
            "name": "X",
            "salePrice": 1000,
            "deferred_notice_fields": [],
        }
        result = listing_templates.apply_template(name="defer-빈리스트", product=product)
        # 빈 리스트가 유지된다 — 템플릿 것이 채워지지 않는다.
        assert product["deferred_notice_fields"] == []
        assert result["deferred_from_template"] == []

    def test_apply_drops_invalid_from_old_template(self, isolated_state_dir):
        """낡은 템플릿의 미루기 불가 필드(boolean) → 제외 + deferred_dropped_invalid 보고.

        템플릿 JSON 을 직접 편집해 불가 필드를 인위적으로 넣는 시나리오.
        전체 적용을 죽이지 않고 해당 필드만 제외한다.
        """
        # 정상 템플릿 저장.
        listing_templates.save_template(
            name="defer-낡은",
            notice_type="ETC",
            product={
                "return_cost_reason": "문구",
                "deferred_notice_fields": ["returnCostReason"],
            },
        )
        # 템플릿 JSON 을 직접 편집 — 미루기 불가 필드(importDeclaration, boolean) 를
        # 인위적으로 추가. 데이터 파일 변화·직접 편집 시뮬레이션.
        path = listing_templates.templates_path()
        doc = json.loads(path.read_text(encoding="utf-8"))
        entry = doc["templates"][0]
        entry["fields"]["deferred_notice_fields"] = [
            "returnCostReason",  # string → 가능.
            "importDeclaration",  # boolean → 불가 (낡은 템플릿).
        ]
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

        # 상품 입력에는 선언이 없다 → 템플릿에서 채운다.
        product = {"name": "X", "salePrice": 1000}
        result = listing_templates.apply_template(name="defer-낡은", product=product)
        # 가능 필드만 채워진다.
        assert product.get("deferred_notice_fields") == ["returnCostReason"]
        # 불가 필드는 제외 사실이 보고된다.
        assert result["deferred_from_template"] == ["returnCostReason"]
        assert result["deferred_dropped_invalid"] == ["importDeclaration"]

    def test_apply_template_without_deferred_key_no_fill(self, isolated_state_dir):
        """템플릿에 deferred_notice_fields 키가 없으면 채우지 않는다."""
        listing_templates.save_template(
            name="defer-키없음",
            notice_type="ETC",
            product={"return_cost_reason": "문구"},
        )
        product = {"name": "X", "salePrice": 1000}
        result = listing_templates.apply_template(name="defer-키없음", product=product)
        # 템플릿에 키가 없으므로 product 에 채워지지 않는다.
        assert "deferred_notice_fields" not in product
        assert result["deferred_from_template"] == []
        assert result["deferred_dropped_invalid"] == []

    def test_prepare_listing_applies_deferred_from_template(self, isolated_state_dir, monkeypatch):
        """실경로 1회: prepare_listing 이 apply_template 를 호출해
        반환/저장 payload 에 미루기 선언이 실려 있음(실호출 출력).

        상품 입력에 deferred_notice_fields 키가 없고 템플릿에 있으면,
        prepare_listing 반환의 template_applied 에서 채운 사실이 드러난다.
        """
        _apply_common_mocks(monkeypatch)
        monkeypatch.setattr(
            "clossify.images.attach_images",
            lambda srcs: _make_attach_result(["http://cdn/a.png"]),
        )
        # 템플릿에 미루기 선언을 저장.
        listing_templates.save_template(
            name="prepare-defer",
            notice_type="WEAR",
            product={
                "return_cost_reason": "템플릿문구WEAR",
                "deferred_notice_fields": ["returnCostReason"],
            },
        )
        # 상품 입력에는 deferred_notice_fields 키가 없다.
        product = _compliant_product_input()
        result = mcp_server.prepare_listing(product, apply_template="prepare-defer")
        assert result["ok"] is True, f"prepare 실패: {result.get('error')}"
        ta = result.get("template_applied")
        assert ta is not None
        # 템플릿에서 미루기 선언이 채워졌다.
        assert ta["deferred_from_template"] == ["returnCostReason"]
        assert ta["deferred_dropped_invalid"] == []
