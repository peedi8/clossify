"""T-114 — 이식 취소된 계층의 설정 리더 잔재 제거 검증.

작업지시(T-114)가 요구하는 검증:
  1. common.py 에 삭제 대상 심볼이 잔존하지 않는다.
  2. 삭제 대상 심볼을 참조하는 호출부가 코드 범위에 잔존하지 않는다.
  3. 삭제된 계층의 설정 키(upstream.base_url, llm.vendor_*)가
     config.example.json 에 존재하지 않는다.
  4. image_providers 섹션은 "현재 미사용"임이 _comment 로 명시되어 있다
     (삭제하지 않음 — 로드맵상 예정된 자리).
  5. 삭제 후에도 live 접근자(DEFAULT_AS_TEL 등)가 정상 동작한다.
  6. 무동작/identity 금지: 본 테스트는 실제로 단언한다 (무조건 통과 아님).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import common

_CONFIG_EXAMPLE_PATH = _PROJECT_ROOT / "config.example.json"

# T-114 에서 삭제 대상으로 지정된 심볼 목록.
_REMOVED_SYMBOLS = (
    "OB",
    "DEFAULT_VENDOR_A_CMD",
    "DEFAULT_VENDOR_A_LLM_TIMEOUT",
    "DEFAULT_VENDOR_B_CMD",
    "DEFAULT_VENDOR_B_MODEL",
    "DEFAULT_VENDOR_B_MODELS",
    "DEFAULT_TRANSLATION_VENDOR_B_MODELS",
    "VENDOR_B_MODEL_COOLDOWN_UNTIL",
    "DEFAULT_VENDOR_B_MODEL_COOLDOWN_SECONDS",
    "TRANSLATION_LLM_OPS",
    "_VENDOR_B_COOLDOWN_REGISTRY",
    "_TRANSLATION_LLM_OPS_DEFAULT",
)

# T-114 에서 삭제 대상으로 지정된 설정 키 접두사/이름.
_DEAD_CONFIG_KEYS = (
    "base_url",          # upstream.base_url
    "vendor_a_cmd",
    "vendor_b_cmd",
    "vendor_b_model",
    "vendor_b_models",
    "vendor_a_timeout",
    "vendor_b_cooldown_seconds",
    "translation_vendor_b_models",
    "translation_ops",
)


# --------------------------------------------------------------------------- #
# 1. 심볼 잔존 0건.
# --------------------------------------------------------------------------- #
class TestRemovedSymbolsGone:
    """common 모듈에 삭제 대상 심볼이 더 이상 존재하지 않는다."""

    @pytest.mark.parametrize("name", _REMOVED_SYMBOLS)
    def test_symbol_absent(self, name):
        assert not hasattr(common, name), (
            f"common.{name} 이 잔존함 — T-114 삭제 대상"
        )

    def test_no_dead_section_reads_in_source(self):
        """common.py 소스에 upstream/llm 섹션 읽기가 남아있지 않다."""
        src = (_SRC / "clossify" / "common.py").read_text(encoding="utf-8")
        # 섹션 이름 자체가 더 이상 등장하지 않아야 한다.
        assert '_cfg_section("upstream")' not in src, (
            'common.py 가 _cfg_section("upstream") 을 아직 읽음'
        )
        assert '_cfg_section("llm")' not in src, (
            'common.py 가 _cfg_section("llm") 을 아직 읽음'
        )

    def test_no_vendor_literal_in_source(self):
        """common.py 소스에 vendor_a/vendor_b 설정 키 리터럴이 없다."""
        src = (_SRC / "clossify" / "common.py").read_text(encoding="utf-8")
        for key in _DEAD_CONFIG_KEYS:
            # 설정 키 리터럴(따옴표로 묶인 형태)만 금지.
            assert f'"{key}"' not in src and f"'{key}'" not in src, (
                f"common.py 소스에 죽은 설정 키 리터럴이 잔존: {key}"
            )


# --------------------------------------------------------------------------- #
# 2. 호출부 잔존 0건 (패키지 범위).
# --------------------------------------------------------------------------- #
class TestNoCallSitesRemain:
    """패키지 소스 범위에서 삭제 심볼 참조가 잔존하지 않는다."""

    def test_no_call_site_in_src(self):
        pkg_dir = _SRC / "clossify"
        hits = []
        for py in pkg_dir.rglob("*.py"):
            text = py.read_text(encoding="utf-8")
            for sym in _REMOVED_SYMBOLS:
                # common.<sym> 형태의 참조만 금지 (자기 정의 제외).
                if f"common.{sym}" in text:
                    hits.append(f"{py.name}: common.{sym}")
        assert not hits, (
            f"삭제 심볼 참조 잔존: {hits}"
        )

    def test_no_import_of_removed_symbols(self):
        pkg_dir = _SRC / "clossify"
        hits = []
        for py in pkg_dir.rglob("*.py"):
            text = py.read_text(encoding="utf-8")
            for sym in _REMOVED_SYMBOLS:
                if f"import {sym}" in text or f", {sym}" in text:
                    hits.append(f"{py.name}: {sym}")
        assert not hits, (
            f"삭제 심볼 import 잔존: {hits}"
        )


# --------------------------------------------------------------------------- #
# 3. config.example.json 에 죽은 키 없음.
# --------------------------------------------------------------------------- #
class TestConfigExampleClean:
    """config.example.json 에 죽은 섹션/키가 없다."""

    def test_no_upstream_section(self):
        cfg = json.loads(_CONFIG_EXAMPLE_PATH.read_text(encoding="utf-8-sig"))
        assert "upstream" not in cfg, (
            "config.example.json 에 upstream 섹션이 있음"
        )

    def test_no_llm_section(self):
        cfg = json.loads(_CONFIG_EXAMPLE_PATH.read_text(encoding="utf-8-sig"))
        assert "llm" not in cfg, (
            "config.example.json 에 llm 섹션이 있음"
        )


# --------------------------------------------------------------------------- #
# 4. image_providers 미사용 명시.
# --------------------------------------------------------------------------- #
class TestImageProvidersMarkedUnused:
    """image_providers 섹션은 유지하되 미사용임이 명시되어 있다."""

    def test_image_providers_present(self):
        cfg = json.loads(_CONFIG_EXAMPLE_PATH.read_text(encoding="utf-8-sig"))
        assert "image_providers" in cfg, (
            "image_providers 섹션이 삭제됨 — 로드맵 자리이므로 유지해야 함"
        )

    def test_image_providers_comment_marks_unused(self):
        cfg = json.loads(_CONFIG_EXAMPLE_PATH.read_text(encoding="utf-8-sig"))
        comment = str(cfg["image_providers"].get("_comment", ""))
        assert "미사용" in comment, (
            "image_providers._comment 가 미사용 표시를 포함하지 않음"
        )


# --------------------------------------------------------------------------- #
# 5. live 접근자 무회귀.
# --------------------------------------------------------------------------- #
class TestLiveAccessorsIntact:
    """삭제 후에도 살아남은 live 접근자가 정상 동작한다."""

    def test_default_as_tel_still_callable(self):
        """DEFAULT_AS_TEL 은 존재하며, 미설정 시 ValueError 를 낸다."""
        assert hasattr(common, "DEFAULT_AS_TEL"), (
            "DEFAULT_AS_TEL 이 삭제됨 — live 접근자"
        )
        # cfg 가 비 구성일 때 fail-closed 동작 확인.
        from unittest import mock
        with mock.patch.object(common, "cfg", return_value={}), pytest.raises(ValueError):
            common.DEFAULT_AS_TEL()

    def test_cfg_section_still_works(self):
        """_cfg_section 헬퍼는 live 접근자가 쓰므로 유지되어야 한다."""
        assert hasattr(common, "_cfg_section"), (
            "_cfg_section 이 삭제됨 — DEFAULT_AS_TEL 이 사용 중"
        )
        # 빈 cfg 에서는 {} 반환.
        from unittest import mock
        with mock.patch.object(common, "cfg", return_value={}):
            assert common._cfg_section("brand") == {}


# --------------------------------------------------------------------------- #
# 6. 무동작 금지 — 본 테스트 클래스가 실제 단언을 수행한다.
# --------------------------------------------------------------------------- #
class TestNoNoOp:
    """본 검증이 무동작이 아님을 보인다."""

    def test_removed_vs_kept_differ(self):
        """삭제 대상은 부재, live 대상은 존재 → 검증이 유효하다."""
        removed_present = any(hasattr(common, s) for s in _REMOVED_SYMBOLS)
        live_present = hasattr(common, "DEFAULT_AS_TEL")
        assert removed_present is False, (
            "삭제 대상 심볼 중 하나 이상이 잔존 — 검증 실패"
        )
        assert live_present is True, (
            "live 심볼(DEFAULT_AS_TEL)이 부재 — 과삭제"
        )
