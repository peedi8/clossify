# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""FIX-config-gap-coverage — 설정 점검 커버리지 구멍 전수 시험.

리허설 관통에서 발견된 결함: 등록 경로가 config 값 부재로 거부하는 키 중
``smartstore_notice_defaults.delivery_company`` 가 ``check_config`` 의
``policy_gaps`` 에 없었다. 사용자가 설정 점검이 시키는 것을 전부 해도
마지막 순간에 점검이 한 번도 언급하지 않은 값 때문에 등록이 막힌다.

본 시험:
  (a) delivery_company 비어있음 → policy_gaps 에 등장
  (b) 채워진 설정 → 등장하지 않음
  (c) ★ 커버리지 회귀 가드 — 등록 경로가 하드 요구하는 config 키 집합과
      _diagnose_policy_gaps 가 검사하는 집합을 대조. 새 하드 요구 키가
      생겼는데 gap 목록에 없으면 실패한다. 요구 키 목록은 소스
      (naver_client.py / register.py)에서 유도하고, 유도된 각 키가
      실제 거부 지점인지 런타임으로도 재확인한다.
  (d) 기존 policy_gaps 항목 회귀 (기존 12+1 키 그대로 보고)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest import mock

import pytest

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import mcp_server, naver_client, register

# =========================================================================== #
# 공통 픽스처 — 임시 설정만 쓴다 (.local/config.json 을 읽지 않는다).
# =========================================================================== #

# 등록 경로(build_payload / register_product)가 config 부재로 하드 거부하는
# 정책 키 전부. (c) 가드가 이 목록과 소스·실거부지점의 어긋남을 잡는다.
#
# - as_tel: 세 별칭(as_tel/seller_tel/customerServicePhoneNumber) 중 어느
#   하나라도 있으면 통과 — naver_client._resolve_as_tel 이 판정.
# - origin_area_code / origin_content / delivery_company: 단일 키.
HARD_REQUIRED_POLICY_KEYS: tuple[str, ...] = (
    "smartstore_notice_defaults.as_tel",
    "smartstore_notice_defaults.origin_area_code",
    "smartstore_notice_defaults.origin_content",
    "smartstore_notice_defaults.delivery_company",
)

# 등록 경로 소스의 거부 메시지/후보에 등장하는 "smartstore_notice_defaults.<키>"
# 토큰 → policy_gaps 키 경로 매핑. 소스에서 새 토큰이 발견되면 이 매핑에
# 분류(하드 요구 / 비요구)를 추가해야 한다 — 미분류 토큰은 시험이 실패시킨다.
_SOURCE_TOKEN_TO_POLICY_KEY: dict[str, str] = {
    "as_tel": "smartstore_notice_defaults.as_tel",
    "seller_tel": "smartstore_notice_defaults.as_tel",
    "customerServicePhoneNumber": "smartstore_notice_defaults.as_tel",
    "origin_area_code": "smartstore_notice_defaults.origin_area_code",
    "origin_content": "smartstore_notice_defaults.origin_content",
    "delivery_company": "smartstore_notice_defaults.delivery_company",
    # 비(fail-closed 아님): 배송비는 상거래 조건 — 미설정이 등록을 막지 않는다.
    # 다만 정책 인벤토리(_POLICY_CONFIG_KEYS) 에는 등록되어 진단된다.
    "delivery_fee": "smartstore_notice_defaults.delivery_fee",
    "deliveryFee": "smartstore_notice_defaults.delivery_fee",
}

# 등록에 필요한 최소 상품 입력 (config 유래 키는 일부러 넣지 않는다).
_MIN_PRODUCT = {
    "name": "테스트상품",
    "categoryId": "50000000",
    "salePrice": 30000,
}


def _full_notice_cfg() -> dict:
    """하드 요구 키 전부를 채운 임시 설정 (notice 섹션)."""
    return {
        "origin_area_code": "04",
        "origin_content": "중국",
        "as_tel": "070-0000-0000",
        "delivery_company": "HKSTRANS",
    }


def _build_with_cfg(cfg_notice: dict) -> dict:
    """notice_config 을 고정하고 build_payload 실행. 네트워크 없음."""
    with (
        mock.patch.object(naver_client, "_notice_config", return_value=cfg_notice),
        mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
    ):
        return naver_client.build_payload(_MIN_PRODUCT, "<html></html>", ["http://x.png"])


# =========================================================================== #
# (a)+(b) delivery_company gap 진단.
# =========================================================================== #


class TestDeliveryCompanyGapDiagnosis:
    """delivery_company 부재/존재에 따른 policy_gaps 진단."""

    def test_missing_delivery_company_reported_in_gaps(self):
        """(a) delivery_company 비어있음 → policy_gaps 에 등장."""
        cfg = {"smartstore_notice_defaults": _full_notice_cfg()}
        del cfg["smartstore_notice_defaults"]["delivery_company"]
        gaps = mcp_server._diagnose_policy_gaps(cfg)
        assert (
            "smartstore_notice_defaults.delivery_company" in gaps
        ), f"delivery_company 가 비어있는데 policy_gaps 에 없음: {gaps!r}"

    def test_filled_delivery_company_not_in_gaps(self):
        """(b) 채워진 설정 → policy_gaps 에 등장하지 않음."""
        cfg = {"smartstore_notice_defaults": _full_notice_cfg()}
        gaps = mcp_server._diagnose_policy_gaps(cfg)
        assert (
            "smartstore_notice_defaults.delivery_company" not in gaps
        ), f"delivery_company 가 채워졌는데 policy_gaps 에 등장: {gaps!r}"

    def test_placeholder_delivery_company_reported_in_gaps(self):
        """자리표시자(REPLACE_WITH_...) 값은 미설정으로 진단한다."""
        cfg = {"smartstore_notice_defaults": _full_notice_cfg()}
        cfg["smartstore_notice_defaults"]["delivery_company"] = "REPLACE_WITH_DELIVERY_COMPANY_CODE"
        gaps = mcp_server._diagnose_policy_gaps(cfg)
        assert (
            "smartstore_notice_defaults.delivery_company" in gaps
        ), f"자리표시자인데 policy_gaps 에 없음: {gaps!r}"

    def test_camelcase_delivery_company_alias_not_in_gaps(self):
        """camelCase deliveryCompany 로 설정해도 "설정됨" 으로 본다 (별칭)."""
        cfg = {"smartstore_notice_defaults": _full_notice_cfg()}
        del cfg["smartstore_notice_defaults"]["delivery_company"]
        cfg["smartstore_notice_defaults"]["deliveryCompany"] = "HKSTRANS"
        gaps = mcp_server._diagnose_policy_gaps(cfg)
        assert (
            "smartstore_notice_defaults.delivery_company" not in gaps
        ), f"camelCase 별칭이 있는데 policy_gaps 에 등장: {gaps!r}"


# =========================================================================== #
# (c) ★ 커버리지 회귀 가드.
#
# 두 갈래로 어긋남을 잡는다:
#   1. 소스 유도 — naver_client.py / register.py 소스에서
#      "smartstore_notice_defaults.<키>" 토큰을 전수 추출. 미분류 토큰(=새 하드
#      요구 가능성)이 생기면 실패. 분류된 토큰은 전부 policy_gaps 커버여야 함.
#   2. 런타임 재확인 — HARD_REQUIRED_POLICY_KEYS 각 키가 실제로 등록을 거부하는지
#      build_payload / register_product(COMMERCE_DRY_RUN=1) 로 재라.
#      상수와 실제 거부 지점이 어긋나면 실패.
# =========================================================================== #


class TestCoverageGuardSourceDerived:
    """소스에서 유도한 요구 키가 전부 policy_gaps 커버인지 대조."""

    def test_every_source_required_key_is_covered_by_policy_gaps(self):
        """소스 토큰 → policy 키 매핑 전부가 _POLICY_CONFIG_KEYS 에 있어야 함."""
        covered = {".".join(path) for path in mcp_server._POLICY_CONFIG_KEYS}
        for token, policy_key in _SOURCE_TOKEN_TO_POLICY_KEY.items():
            assert policy_key in covered, (
                f"등록 경로 소스가 {token!r}({policy_key}) 을 요구하는데 "
                f"policy_gaps 가 검사하지 않음 — 커버리지 구멍: {sorted(covered)!r}"
            )

    def test_no_unclassified_source_tokens(self):
        """소스에 새 "smartstore_notice_defaults.<키>" 토큰이 생기면 실패.

        미분류 토큰은 새 하드 요구 키가 생겼다는 신호다 — 하드 요구인지
        판정해서 _SOURCE_TOKEN_TO_POLICY_KEY 에 분류하거나(→ 커버 추가),
        비요구임을 확인해 목록에서 제외해야 한다.
        """
        pattern = re.compile(r"smartstore_notice_defaults\.([A-Za-z_][A-Za-z0-9_]*)")
        found: set[str] = set()
        for module in (naver_client, register):
            source = Path(module.__file__).read_text(encoding="utf-8")
            found.update(pattern.findall(source))
        unclassified = found - set(_SOURCE_TOKEN_TO_POLICY_KEY)
        assert not unclassified, (
            f"등록 경로 소스에 미분류 토큰 발견: {sorted(unclassified)!r} — "
            "하드 요구 키라면 _POLICY_CONFIG_KEYS 에 추가하고 "
            "_SOURCE_TOKEN_TO_POLICY_KEY 에 분류할 것"
        )

    def test_hard_required_keys_subset_of_policy_inventory(self):
        """명시 상수(HARD_REQUIRED_POLICY_KEYS) 전부가 인벤토리에 등록됨."""
        covered = {".".join(path) for path in mcp_server._POLICY_CONFIG_KEYS}
        missing = set(HARD_REQUIRED_POLICY_KEYS) - covered
        assert not missing, f"하드 요구 키가 policy_gaps 커버가 아님: {sorted(missing)!r}"


class TestCoverageGuardRuntimeRejection:
    """HARD_REQUIRED_POLICY_KEYS 각 키가 실제 거부 지점인지 런타임 재확인.

    상수에 있는 키가 실제로는 등록을 거부하지 않으면(요구가 풀렸으면)
    시험이 실패한다 — 상수와 실제 거부 지점의 어긋남 방지.
    """

    def test_missing_as_tel_rejects_registration(self):
        cfg = _full_notice_cfg()
        for key in ("as_tel", "seller_tel", "customerServicePhoneNumber"):
            cfg.pop(key, None)
        with pytest.raises(ValueError, match="AS 연락처"):
            _build_with_cfg(cfg)

    def test_missing_origin_area_code_rejects_registration(self):
        cfg = _full_notice_cfg()
        cfg.pop("origin_area_code", None)
        with pytest.raises(ValueError, match="origin_area_code"):
            _build_with_cfg(cfg)

    def test_missing_origin_content_rejects_registration(self):
        cfg = _full_notice_cfg()
        cfg.pop("origin_content", None)
        with pytest.raises(ValueError, match="origin_content"):
            _build_with_cfg(cfg)

    def test_missing_delivery_company_rejects_registration(self, monkeypatch):
        """delivery_company 부재 → 등록 경계(register_product)에서 거부.

        COMMERCE_DRY_RUN=1 — 실제 API 호출 없이 게이트만 재며, 워크오더의
        실측 에러 메시지(delivery_company) 와 일치함을 확인한다.
        """
        monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
        cfg = _full_notice_cfg()
        cfg.pop("delivery_company", None)
        payload = _build_with_cfg(cfg)  # 구성 단계는 통과 (빈 문자열 허용)
        with pytest.raises(ValueError, match="delivery_company"):
            naver_client.register_product(payload)

    def test_filled_delivery_company_passes_register_gate(self, monkeypatch):
        """delivery_company 채움 → 등록 경계 게이트 통과 (dry-run 반환)."""
        monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
        payload = _build_with_cfg(_full_notice_cfg())
        result = naver_client.register_product(payload)
        assert result.get("dry_run") is True, f"게이트가 잘못 거부함: {result!r}"


# =========================================================================== #
# (d) 기존 policy_gaps 항목 회귀 — 추가만 하고 기존 항목은 그대로.
# =========================================================================== #


class TestExistingPolicyGapsRegression:
    """기존 12+1(delivery_fee) 키가 빈 설정에서 그대로 보고되는지 확인."""

    def test_empty_config_reports_all_existing_keys(self):
        """빈 설정 → 기존 전체 키 + delivery_company 까지 보고."""
        gaps = mcp_server._diagnose_policy_gaps({"smartstore_notice_defaults": {}})
        expected = [
            "smartstore_notice_defaults.origin_area_code",
            "smartstore_notice_defaults.origin_content",
            "smartstore_notice_defaults.as_tel",
            "smartstore_notice_defaults.as_guide",
            "smartstore_notice_defaults.manufacturer",
            "smartstore_notice_defaults.importer",
            "smartstore_notice_defaults.returnCostReason",
            "smartstore_notice_defaults.noRefundReason",
            "smartstore_notice_defaults.qualityAssuranceStandard",
            "smartstore_notice_defaults.compensationProcedure",
            "smartstore_notice_defaults.troubleShootingContents",
            "smartstore_notice_defaults.delivery_fee",
            # 신규 (본 수정).
            "smartstore_notice_defaults.delivery_company",
        ]
        for key in expected:
            assert key in gaps, f"기존/신규 정책 키가 policy_gaps 에 없음: {key!r} (gaps={gaps!r})"

    def test_check_config_returns_policy_gaps_unchanged_shape(self, tmp_path, monkeypatch):
        """check_config 반환의 policy_gaps 형식(경로 문자열 목록) 회귀."""
        import json

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps(
                {
                    "naver": {
                        "client_id": "id",
                        "client_secret": "secret",
                        "store_url_slug": "slug",
                    },
                    "smartstore_notice_defaults": {
                        "origin_area_code": "04",
                        "origin_content": "중국",
                        "as_tel": "070-0000-0000",
                        # delivery_company 없음 → (a) 진입 경로에서도 등장.
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_file))
        result = mcp_server.check_config()
        gaps = result.get("policy_gaps") or []
        assert isinstance(gaps, list), f"policy_gaps 가 목록이 아님: {type(gaps)}"
        assert all(isinstance(g, str) for g in gaps), f"항목이 문자열이 아님: {gaps!r}"
        assert (
            "smartstore_notice_defaults.delivery_company" in gaps
        ), f"check_config 경로에서 delivery_company gap 미보고: {gaps!r}"
        assert (
            "smartstore_notice_defaults.origin_area_code" not in gaps
        ), "채워진 키가 gap 으로 보고됨 (회귀)"
