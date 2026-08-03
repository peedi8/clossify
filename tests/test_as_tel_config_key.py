"""as_tel 설정 키 불일치 수리 검증 테스트.

검증 시나리오:
  1. config.example.json 의 smartstore_notice_defaults 에 as_tel 항목이 존재.
  2. config.example.json 을 그대로 복사해 값만 채운 설정으로 build_payload 호출 시
     AS 전화번호가 payload 의 afterServiceInfo.afterServiceTelephoneNumber 에 정상
     반영된다 (키 자리 불일치 결함의 직접 반례).
  3. AS 전화번호 미설정 상태에서 check_config 가 부족 항목으로 보고하고,
     값 자체는 응답에 없다.
  4. AS 전화번호가 설정된 상태에서 check_config 가 채워짐으로 보고하고,
     역시 값 자체는 응답에 없다.
  5. config.example.json 의 키 ↔ 코드가 읽는 키 대조 (잔여 불일치 명시).
  6. brand.as_tel 은 에이전트 문서 치환용 별도 용도로 유지됨을 명시.

모든 테스트는 실제 네이버 API 를 호출하지 않는다 — monkeypatch 로 네트워크 차단.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import mcp_server, naver_client

_CONFIG_EXAMPLE_PATH = _PROJECT_ROOT / "config.example.json"


def _load_example_config() -> dict:
    """config.example.json 을 파싱해 반환."""
    with open(_CONFIG_EXAMPLE_PATH, encoding="utf-8-sig") as f:
        return json.load(f)


def _write_config(tmp_path: Path, cfg: dict) -> Path:
    """테스트용 config.json 을 tmp_path 하위에 쓰고 경로 반환."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# 1. 정본 위치에 as_tel 항목이 존재한다.
# --------------------------------------------------------------------------- #
class TestCanonicalKeyPresent:
    """smartstore_notice_defaults.as_tel 항목이 예시에 존재하는가."""

    def test_canonical_as_tel_key_exists(self):
        cfg = _load_example_config()
        notice = cfg.get("smartstore_notice_defaults")
        assert isinstance(notice, dict), "smartstore_notice_defaults 섹션이 객체가 아님"
        assert (
            "as_tel" in notice
        ), "smartstore_notice_defaults.as_tel (정본) 항목이 없음 — as_tel 키 불일치 결함 미수리"

    def test_brand_as_tel_still_present(self):
        """brand.as_tel 은 에이전트 문서 치환용으로 유지됨."""
        cfg = _load_example_config()
        brand = cfg.get("brand")
        assert isinstance(brand, dict), "brand 섹션이 객체가 아님"
        assert (
            "as_tel" in brand
        ), "brand.as_tel 이 제거됨 — 에이전트 문서 {AS_TEL} 치환용이므로 유지 필요"

    def test_canonical_as_tel_has_placeholder(self):
        """정본 as_tel 값은 플레이스홀더여야 한다 (실번호 노출 금지)."""
        cfg = _load_example_config()
        val = cfg["smartstore_notice_defaults"]["as_tel"]
        assert isinstance(val, str) and val, "as_tel 값이 비문자열/빈 문자열"
        # 실전화번호가 아닌 플레이스홀더여야 한다.
        assert "REPLACE_WITH_" in val or val.startswith(
            "{"
        ), f"as_tel 이 플레이스홀더가 아님: {val!r}"

    def test_as_tel_usage_documented_in_comment(self):
        """두 항목의 용도 차이가 _comment 또는 별도 키로 명시되어 있는가."""
        cfg = _load_example_config()
        notice = cfg["smartstore_notice_defaults"]
        # as_tel_comment 또는 _comment 중 하나는 용도 차이를 설명해야 한다.
        comment_blob = " ".join(
            str(v)
            for v in (
                notice.get("as_tel_comment"),
                notice.get("_comment"),
                cfg.get("brand", {}).get("_comment"),
            )
            if isinstance(v, str)
        )
        # "정본" 또는 "참조" 키워드가 두 키의 관계를 설명하고 있어야 한다.
        assert (
            "정본" in comment_blob or "참조" in comment_blob
        ), "as_tel 두 용도(정본/치환용) 구분 명시가 없음"


# --------------------------------------------------------------------------- #
# 2. 직접 반례: 예시 복사 + 값 채운 설정으로 build_payload → as_tel 반영.
# --------------------------------------------------------------------------- #
class TestBuildPayloadCarriesAsTel:
    """config.example.json 복사본의 정본 as_tel 자리에 실번호를 채우면
    payload 의 afterServiceInfo.afterServiceTelephoneNumber 에 반영되는가."""

    def test_canonical_as_tel_lands_in_payload(self):
        """정본 as_tel 자리에 채운 값이 payload 에 정상 반영된다 (결함 반례).

        결함: 예시에는 smartstore_notice_defaults.as_tel 이 없어 사용자가
        brand.as_tel 에만 값을 넣고, 코드가 찾는 자리에는 값이 없어 누락되었다.
        본 테스트는 정본 자리에 값을 넣으면 payload 에 실리는지 확인한다.
        """
        # config.example.json 을 그대로 복사해 값만 채운다.
        cfg = _load_example_config()
        notice_section = dict(cfg["smartstore_notice_defaults"])
        # 실제 신고하는 값으로 채운다 (테스트용 가짜 번호).
        notice_section["as_tel"] = "070-0000-0000"
        notice_section["origin_area_code"] = "01"
        notice_section["origin_content"] = "한국"
        notice_section["manufacturer"] = "테스트제조사"

        product = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
        }
        with mock.patch.object(naver_client, "_notice_config", return_value=notice_section):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                payload = naver_client.build_payload(
                    product,
                    "<html></html>",
                    ["http://cdn/x.png"],
                    status="SALE",
                )
        after_info = (
            payload.get("originProduct", {}).get("detailAttribute", {}).get("afterServiceInfo", {})
        )
        assert after_info.get("afterServiceTelephoneNumber") == "070-0000-0000", (
            f"정본 as_tel 이 payload 에 반영되지 않음: "
            f"{after_info.get('afterServiceTelephoneNumber')!r}"
        )

    def test_canonical_as_tel_in_notice_after_service_director(self):
        """고시 본문의 afterServiceDirector 에도 정본 as_tel 이 반영되는가."""
        cfg = _load_example_config()
        notice_section = dict(cfg["smartstore_notice_defaults"])
        notice_section["as_tel"] = "070-0000-0000"
        notice_section["origin_area_code"] = "01"
        notice_section["origin_content"] = "한국"
        notice_section["manufacturer"] = "테스트제조사"

        product = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
        }
        with mock.patch.object(naver_client, "_notice_config", return_value=notice_section):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                payload = naver_client.build_payload(
                    product,
                    "<html></html>",
                    ["http://cdn/x.png"],
                    status="SALE",
                )
        notice = (
            payload.get("originProduct", {})
            .get("detailAttribute", {})
            .get("productInfoProvidedNotice", {})
        )
        etc = notice.get("etc", {})
        # ETC afterServiceDirector 형식: "{manufacturer} {as_tel}"
        assert "070-0000-0000" in str(etc.get("afterServiceDirector", "")), (
            f"afterServiceDirector 에 as_tel 이 없음: " f"{etc.get('afterServiceDirector')!r}"
        )


# --------------------------------------------------------------------------- #
# 3. check_config 보강: 미설정 시 부족 항목, 값 미노출.
# --------------------------------------------------------------------------- #
class TestCheckConfigReportsAsTel:
    """check_config 가 as_tel 정본 위치 설정 여부를 보고하는가 (값 미노출)."""

    def test_check_config_reports_as_tel_unset(self, tmp_path):
        """as_tel 미설정 → as_tel_configured=False, as_tel_hint 포함, 값 미노출."""
        cfg = {
            "naver": {
                "client_id": "real-id",
                "client_secret": "real-secret",
                "type": "SELF",
                "store_url_slug": "real-slug",
            },
            "smartstore_notice_defaults": {
                "origin_area_code": "01",
                "origin_content": "한국",
                # as_tel 없음.
            },
        }
        path = _write_config(tmp_path, cfg)
        with mock.patch.object(naver_client, "config_path", return_value=str(path)):
            result = mcp_server.check_config()
        assert "as_tel_configured" in result, "check_config 응답에 as_tel_configured 필드가 없음"
        assert result["as_tel_configured"] is False, "as_tel 미설정인데 configured=True 로 보고함"
        assert "as_tel_hint" in result, "as_tel 미설정 안내(as_tel_hint)가 없음"
        # 값 자체는 응답에 없어야 한다.
        result_json = json.dumps(result, ensure_ascii=False)
        assert "070-" not in result_json, "check_config 응답에 전화번호 값이 노출됨"

    def test_check_config_reports_as_tel_set(self, tmp_path):
        """as_tel 설정 → as_tel_configured=True, 값 미노출."""
        cfg = {
            "naver": {
                "client_id": "real-id",
                "client_secret": "real-secret",
                "type": "SELF",
                "store_url_slug": "real-slug",
            },
            "smartstore_notice_defaults": {
                "origin_area_code": "01",
                "origin_content": "한국",
                "as_tel": "070-1234-5678",
            },
        }
        path = _write_config(tmp_path, cfg)
        with mock.patch.object(naver_client, "config_path", return_value=str(path)):
            result = mcp_server.check_config()
        assert result.get("as_tel_configured") is True, "as_tel 설정인데 configured=False 로 보고함"
        # 값 자체는 응답에 없어야 한다.
        assert "070-1234-5678" not in json.dumps(
            result, ensure_ascii=False
        ), "check_config 응답에 as_tel 실값이 노출됨"

    def test_check_config_reports_placeholder_as_unset(self, tmp_path):
        """as_tel 이 플레이스홀더(예: REPLACE_WITH_REAL_AS_TEL)면 미설정으로 본다."""
        cfg = {
            "naver": {
                "client_id": "real-id",
                "client_secret": "real-secret",
                "type": "SELF",
                "store_url_slug": "real-slug",
            },
            "smartstore_notice_defaults": {
                "origin_area_code": "01",
                "origin_content": "한국",
                "as_tel": "REPLACE_WITH_REAL_AS_TEL",
            },
        }
        path = _write_config(tmp_path, cfg)
        with mock.patch.object(naver_client, "config_path", return_value=str(path)):
            result = mcp_server.check_config()
        assert (
            result["as_tel_configured"] is False
        ), "플레이스홀더 as_tel 을 configured=True 로 보고함"
        assert "등록을 거부" in result.get(
            "as_tel_hint", ""
        ), "as_tel_hint 가 등록 거부 안내를 포함하지 않음"


# --------------------------------------------------------------------------- #
# 4. 무동작·identity 금지 검증.
# --------------------------------------------------------------------------- #
class TestNoNoOp:
    """check_config 의 as_tel 점검이 실제로 효과를 발휘하는가."""

    def test_as_tel_field_absent_vs_present_differ(self, tmp_path):
        """as_tel 자리가 빈 경우와 채운 경우의 보고가 다르다 (무동작 아님)."""
        base = {
            "naver": {
                "client_id": "id",
                "client_secret": "secret",
                "type": "SELF",
                "store_url_slug": "slug",
            },
            "smartstore_notice_defaults": {
                "origin_area_code": "01",
                "origin_content": "한국",
            },
        }
        import copy as _copy

        empty_cfg = _copy.deepcopy(base)
        path_empty = _write_config(tmp_path, empty_cfg)
        filled_dir = tmp_path / "filled"
        filled_dir.mkdir()
        filled_cfg = _copy.deepcopy(base)
        filled_cfg["smartstore_notice_defaults"]["as_tel"] = "070-0000-0000"
        path_filled = _write_config(filled_dir, filled_cfg)

        with mock.patch.object(naver_client, "config_path", return_value=str(path_empty)):
            r_empty = mcp_server.check_config()
        with mock.patch.object(naver_client, "config_path", return_value=str(path_filled)):
            r_filled = mcp_server.check_config()
        assert (
            r_empty["as_tel_configured"] != r_filled["as_tel_configured"]
        ), "as_tel 점검이 무동작이다 (설정 여부와 무관하게 동일 결과)"


# --------------------------------------------------------------------------- #
# 5. 키 대조 결과 — 잔여 불일치 명시.
# --------------------------------------------------------------------------- #
class TestKeyCrossReference:
    """config.example.json 의 키와 코드가 읽는 키의 대조 결과.

    as_tel 불일치는 수리됨. 예시에 있지만 코드가 읽지 않는 섹션 중
    image_providers 는 어댑터 연동 전 미사용으로 예시 _comment 에 명시되어 있으므로
    그 사실을 검증한다 (문서화 목적의 실제 단언).
    """

    def test_as_tel_mismatch_resolved(self):
        """as_tel 불일치는 해결됨: 코드가 읽는 자리 = 정본 예시 자리."""
        cfg = _load_example_config()
        # 코드가 읽는 자리: naver_client._notice_config() -> smartstore_notice_defaults
        # 그 안에서 _notice_defaults 가 cfg_notice.get("as_tel") 로 읽는다.
        assert (
            "as_tel" in cfg["smartstore_notice_defaults"]
        ), "코드가 읽는 자리(smartstore_notice_defaults.as_tel)에 예시 항목이 없음"

    def test_image_providers_section_marked_unused(self):
        """image_providers 섹션은 어댑터 연동 전 미사용이며 예시에 명시되어 있다.

        코드가 이 섹션을 읽지 않는 것이 의도적임을 예시 _comment 로 확인한다.
        (as_tel 본래 범위 밖이지만, 예시-코드 불일치 중 검증 가능한 항목.)
        """
        cfg = _load_example_config()
        ip = cfg.get("image_providers")
        assert isinstance(ip, dict), "image_providers 섹션이 객체가 아님"
        comment = str(ip.get("_comment") or "")
        assert (
            "미사용" in comment
        ), "image_providers._comment 가 이 섹션이 현재 미사용임을 명시하지 않음"
