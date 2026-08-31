"""원산지·KC 신고값 하드코딩 제거 검증 테스트.

이 테스트는 해외 소싱 도구 원본의 규제 신고 하드코딩이 제거되었는지 확인한다:
1. 원산지 코드/국가명 해외 기본값(``"04"``, ``"중국"``) 및 폴백 제거 → fail-closed.
2. ``build_payload`` 의 KC 하드코딩(``KC_EXEMPTION_OBJECT`` / ``OVERSEAS``) 제거.
3. 외부 마켓 ID(``num_iid``/``item_id``) 기반 모델명 접두사(``TB-...``) 제거.
4. config 에 값이 있을 때 payload 에 그대로 반영되는지.

외부 API 호출/네트워크/실제 config 파일 의존성 없이 ``_notice_config`` /
``_kc_config`` 를 mock 하여 검증한다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import naver_client


# ============================================================================ #
# 시나리오 1 — 원산지 설정 없이 build_payload → ValueError (fail-closed)
# ============================================================================ #
class TestOriginFailClosed:
    """config 에 원산지 설정이 없을 때 build_payload 가 등록을 거부하는가."""

    def _base_product(self):
        return {
            "name": "테스트상품",
            "categoryId": "50002366",
            "salePrice": 10000,
            "as_tel": "070-1234-5678",
        }

    def test_missing_origin_area_code_raises(self):
        """counterexample: config 에 원산지 설정 없음 → ValueError (fail-closed).

        ``_notice_defaults`` 가 made_in 검사를 먼저 수행하므로, origin_area_code 가
        누락된 경우 결국 origin 관련 ValueError 중 하나가 발생한다. 핵심은
        조용한 기본값 없이 등록이 거부된다는 것이다.
        """
        with mock.patch.object(naver_client, "_notice_config", return_value={}):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "warn")):
                with pytest.raises(ValueError):
                    naver_client.build_payload(
                        self._base_product(), "<html></html>", ["http://x.png"]
                    )

    def test_missing_origin_area_code_only_raises(self):
        """origin_content 는 있지만 origin_area_code 가 없을 때 ValueError.

        이 케이스는 ``_resolve_origin_area_code`` 가 직접 발생시키는 에러다.
        """
        cfg = {"origin_content": "한국"}
        product = self._base_product()
        product["made_in"] = "한국"
        with mock.patch.object(naver_client, "_notice_config", return_value=cfg):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "warn")):
                with pytest.raises(ValueError, match="origin_area_code"):
                    naver_client.build_payload(product, "<html></html>", ["http://x.png"])

    def test_missing_origin_content_raises(self):
        """counterexample: origin_area_code 만 있고 origin_content 없음 → ValueError."""
        cfg = {"origin_area_code": "05", "origin_content": ""}
        with mock.patch.object(naver_client, "_notice_config", return_value=cfg):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "warn")):
                with pytest.raises(ValueError, match="origin_content"):
                    naver_client.build_payload(
                        self._base_product(), "<html></html>", ["http://x.png"]
                    )

    def test_resolve_origin_area_code_empty_raises(self):
        with pytest.raises(ValueError):
            naver_client._resolve_origin_area_code({}, {})

    def test_resolve_origin_area_code_invalid_raises(self):
        """화이트리스트 밖 코드는 ``"04"`` 폴백 없이 ValueError."""
        with pytest.raises(ValueError):
            naver_client._resolve_origin_area_code({"origin_code": "ZZ"}, {})

    def test_resolve_origin_area_code_valid_returns_code(self):
        assert naver_client._resolve_origin_area_code({"origin_code": "05"}, {}) == "05"

    def test_no_silent_04_fallback(self):
        """빈 입력에 ``"04"`` 를 조용히 반환하지 않는다."""
        with pytest.raises(ValueError):
            naver_client._resolve_origin_area_code({}, {})


# ============================================================================ #
# 시나리오 2 — KC 설정 없이 호출 → KC 필드 없음 + 경고 메타
# ============================================================================ #
class TestKCConfigGated:
    """KC 신고값이 config 에 없을 때 payload 에 KC 필드가 생략되고 경고가 포함되는가."""

    def _base_product(self):
        return {
            "name": "테스트상품",
            "categoryId": "50002366",
            "salePrice": 10000,
            "origin_code": "05",
            "made_in": "한국",
            "as_tel": "02-0000-0000",
        }

    def test_kc_absent_no_kc_fields_in_payload(self):
        with mock.patch.object(naver_client, "_notice_config", return_value={}):
            with mock.patch.object(
                naver_client,
                "_kc_config",
                return_value=({}, "kc_warning_text"),
            ):
                payload = naver_client.build_payload(
                    self._base_product(), "<html></html>", ["http://x.png"]
                )
        detail = payload["originProduct"]["detailAttribute"]
        assert "certificationTargetExcludeContent" not in detail

    def test_kc_absent_warning_in_meta(self):
        with mock.patch.object(naver_client, "_notice_config", return_value={}):
            with mock.patch.object(
                naver_client,
                "_kc_config",
                return_value=({}, "kc_warning_text"),
            ):
                payload = naver_client.build_payload(
                    self._base_product(), "<html></html>", ["http://x.png"]
                )
        assert payload.get("_kcWarning") == "kc_warning_text"

    def test_kc_present_block_in_payload(self):
        kc_block = {
            "kcCertifiedProductExclusionYn": "KC_EXEMPTION_OBJECT",
            "kcExemptionType": "ELECTRONIC",
        }
        with mock.patch.object(naver_client, "_notice_config", return_value={}):
            with mock.patch.object(
                naver_client,
                "_kc_config",
                return_value=(kc_block, ""),
            ):
                payload = naver_client.build_payload(
                    self._base_product(), "<html></html>", ["http://x.png"]
                )
        detail = payload["originProduct"]["detailAttribute"]
        assert detail["certificationTargetExcludeContent"] == kc_block
        # 값이 있을 때는 경고 없음.
        assert "_kcWarning" not in payload

    def test_no_hardcoded_kc_exemption_object(self):
        """``KC_EXEMPTION_OBJECT`` 가 소스에 하드코딩되지 않았는지(문자열 검색)."""
        source = (Path(naver_client.__file__)).read_text(encoding="utf-8")
        # _kc_config 의 docstring(설명) 과 테스트 파일 자체는 제외해야 하지만,
        # 여기서는 naver_client.py 내 "KC_EXEMPTION_OBJECT" 가 리터럴 코드로
        # 등장하지 않는지 확인한다. docstring 안 설명은 허용.
        # 보수적으로: 문자열 리터럴 할당 형태가 없어야 한다.
        assert 'kcCertifiedProductExclusionYn": "KC_EXEMPTION_OBJECT"' not in source

    def test_no_hardcoded_overseas(self):
        """``OVERSEAS`` 가 kcExemptionType 리터럴로 박혀있지 않은지."""
        source = (Path(naver_client.__file__)).read_text(encoding="utf-8")
        assert 'kcExemptionType": "OVERSEAS"' not in source


# ============================================================================ #
# 시나리오 3 — config 에 원산지/KC 설정 있을 때 → payload 에 그대로 반영
# ============================================================================ #
class TestConfigValuesPropagated:
    def test_origin_values_reflected_in_payload(self):
        product = {
            "name": "테스트",
            "categoryId": "50002366",
            "salePrice": 5000,
            "as_tel": "02-0000-0000",
        }
        # "02" = 수입산 (네이버 커머스 API 원산지 화이트리스트의 유효 코드).
        cfg_notice = {"origin_area_code": "02", "origin_content": "일본"}
        with mock.patch.object(naver_client, "_notice_config", return_value=cfg_notice):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                payload = naver_client.build_payload(product, "<html></html>", ["http://x.png"])
        info = payload["originProduct"]["detailAttribute"]["originAreaInfo"]
        assert info["originAreaCode"] == "02"
        assert info["content"] == "일본"

    def test_product_origin_code_overrides_config(self):
        product = {
            "name": "테스트",
            "categoryId": "50002366",
            "salePrice": 5000,
            "origin_code": "03",
            "made_in": "미국",
            "as_tel": "02-0000-0000",
        }
        cfg_notice = {"origin_area_code": "02", "origin_content": "일본"}
        with mock.patch.object(naver_client, "_notice_config", return_value=cfg_notice):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                payload = naver_client.build_payload(product, "<html></html>", ["http://x.png"])
        info = payload["originProduct"]["detailAttribute"]["originAreaInfo"]
        assert info["originAreaCode"] == "03"
        assert info["content"] == "미국"

    def test_kc_values_reflected_verbatim(self):
        kc_block = {
            "kcCertifiedProductExclusionYn": "NOT_EXEMPTION_OBJECT",
            "kcExemptionType": "CHILDREN",
        }
        product = {
            "name": "테스트",
            "categoryId": "50002366",
            "salePrice": 5000,
            "origin_code": "05",
            "made_in": "한국",
            "as_tel": "02-0000-0000",
        }
        with mock.patch.object(naver_client, "_notice_config", return_value={}):
            with mock.patch.object(naver_client, "_kc_config", return_value=(kc_block, "")):
                payload = naver_client.build_payload(product, "<html></html>", ["http://x.png"])
        detail = payload["originProduct"]["detailAttribute"]
        assert detail["certificationTargetExcludeContent"] == kc_block


# ============================================================================ #
# 시나리오 4 — 모델명 외부 마켓 ID 접두사(TB-...) 제거
# ============================================================================ #
class TestModelNameNoExternalPrefix:
    """``num_iid``/``item_id`` 에서 ``TB-`` 접두사를 만들지 않는지."""

    def test_model_name_default_returns_empty(self):
        assert naver_client._model_name_default({"num_iid": "123", "item_id": "456"}) == ""

    def test_no_tb_prefix_in_payload(self):
        # categoryId 는 ETC 로 폴백하는 미확정 ID 를 쓴다 — 본 검증의 대상은
        # 모델명 접두사이지 고시 타입이 아니다. (build_payload 가 이제 categoryId
        # 만으로 경로를 자체 조회하므로, 50002366 은 HOME_APPLIANCES 로 바뀌어
        # etc 노드 키를 쓸 수 없다.)
        product = {
            "name": "테스트",
            "categoryId": "99999999",
            "salePrice": 5000,
            "origin_code": "05",
            "made_in": "한국",
            "num_iid": "999",
            "as_tel": "02-0000-0000",
        }
        with mock.patch.object(naver_client, "_notice_config", return_value={}):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                payload = naver_client.build_payload(product, "<html></html>", ["http://x.png"])
        etc = payload["originProduct"]["detailAttribute"]["productInfoProvidedNotice"]["etc"]
        # 모델명이 입력되지 않았으므로 modelName 필드 자체가 없어야 한다.
        assert "modelName" not in etc

    def test_model_name_from_config_propagated(self):
        # 동일한 이유로 ETC 폴백 카테고리 사용 (위 주석 참조).
        product = {
            "name": "테스트",
            "categoryId": "99999999",
            "salePrice": 5000,
            "origin_code": "05",
            "made_in": "한국",
            "as_tel": "02-0000-0000",
        }
        cfg = {"origin_area_code": "05", "origin_content": "한국", "model_name": "MODEL_X1"}
        with mock.patch.object(naver_client, "_notice_config", return_value=cfg):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                payload = naver_client.build_payload(product, "<html></html>", ["http://x.png"])
        etc = payload["originProduct"]["detailAttribute"]["productInfoProvidedNotice"]["etc"]
        assert etc["modelName"] == "MODEL_X1"

    def test_no_tb_prefix_string_in_source(self):
        source = (Path(naver_client.__file__)).read_text(encoding="utf-8")
        assert "TB-{num_iid}" not in source
        assert 'f"TB-' not in source


# ============================================================================ #
# 시나리오 5 — 소스에 해외국 코드/국가명 기본값 잔존 0건
# ============================================================================ #
class TestNoOverseasDefaultsRemain:
    """naver_client.py 에 해외 기본값 잔존이 없는지 (문자열 기반 검증)."""

    def test_no_china_default_in_first_value_for_made_in(self):
        """``default="중국"`` 형태의 made_in 기본값이 없어야 한다."""
        source = (Path(naver_client.__file__)).read_text(encoding="utf-8")
        # ``"중국"`` 이 default= 인자로 등장하면 안 됨.
        assert 'default="중국"' not in source

    def test_no_overseas_purchasing_default(self):
        """``"해외구매대행"`` 이 기본값으로 등장하면 안 됨."""
        source = (Path(naver_client.__file__)).read_text(encoding="utf-8")
        assert 'default="해외구매대행"' not in source

    def test_no_04_default_in_resolve(self):
        """``_first_value(..., default="04")`` 잔존 없음."""
        source = (Path(naver_client.__file__)).read_text(encoding="utf-8")
        assert 'default="04"' not in source

    def test_no_kc_exempt_default(self):
        """``"해당없음 / KC면제"`` 기본값 잔존 없음."""
        source = (Path(naver_client.__file__)).read_text(encoding="utf-8")
        assert 'default="해당없음 / KC면제"' not in source

    def test_config_example_has_origin_keys(self):
        cfg_path = _PROJECT_ROOT / "config.example.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        notice = cfg.get("smartstore_notice_defaults", {})
        assert "origin_area_code" in notice
        assert "origin_content" in notice
        kc = cfg.get("kc_declaration", {})
        assert "kcCertifiedProductExclusionYn" in kc
        assert "kcExemptionType" in kc

    def test_config_example_origin_keys_are_placeholders(self):
        cfg_path = _PROJECT_ROOT / "config.example.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        notice = cfg["smartstore_notice_defaults"]
        assert notice["origin_area_code"].startswith("REPLACE_WITH_")
        kc = cfg["kc_declaration"]
        assert kc["kcCertifiedProductExclusionYn"].startswith("REPLACE_WITH_")
