# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""금지표현 정규식 검증.

``BANNED_CLAIM_RE`` 가 규칙문서(COMPLIANCE_RULES.md §1) 와 맞추기 위해
넓혀졌다. 본 테스트는:

  1. 새로 잡는 표현 각각 1건씩 → 걸린다.
  2. 정상 문장 반례(기존 픽스처 실제 상품명 + 통제군 상품명) → 안 걸린다.
  3. 기존 금지표현 시험이 전부 그대로 통과(회귀 없음).
  4. 정규식은 text_props.py 한 곳에만 정의(중복 정의 금지).

과탐 위험 실측(스캔 결과):
  - ``공식`` 단독 → ``가공식품`` 오탐 → ``(?!품)`` 로 좁힘.
  - ``베스트`` (한글) → 조끼(vest) 카테고리 충돌 → 한글 ``베스트`` 는 제외,
    영문 ``BEST`` 만 추가.
  - ``정식`` → ``한정식``·``가정식``·``정식 도시락`` 등 정상 식품 상품명과
    구별 불가 → 정규식에서 제외(과탐 철회).
  - 숫자+위 → ``3위생`` 오탐 → 경계 lookaround 로 좁힘.
"""

from __future__ import annotations

import sys
from pathlib import Path

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify.text_props import BANNED_CLAIM_RE

# --------------------------------------------------------------------------- #
# 1. 새로 잡는 표현 각각 → 걸린다.
# --------------------------------------------------------------------------- #


class TestNewBannedTermsCaught:
    """규칙문서 확장으로 새로 추가된 표현이 각각 걸리는가."""

    def test_gongsik_caught(self):
        """공식 (단 품 앞이 아닐 때) → 걸림."""
        assert BANNED_CLAIM_RE.search("공식 판매처") is not None

    def test_1wi_caught(self):
        """1위 → 걸림."""
        assert BANNED_CLAIM_RE.search("판매 1위 상품") is not None

    def test_eopgye_1wi_caught(self):
        """업계 1위 → 걸림."""
        assert BANNED_CLAIM_RE.search("업계 1위 브랜드") is not None

    def test_BEST_caught(self):
        """BEST → 걸림 (영문 마케팅)."""
        assert BANNED_CLAIM_RE.search("BEST 상품") is not None
        assert BANNED_CLAIM_RE.search("best item") is not None

    def test_chojeoga_caught(self):
        """최저가 → 걸림."""
        assert BANNED_CLAIM_RE.search("최저가 보장") is not None

    def test_choteukga_caught(self):
        """초특가 → 걸림."""
        assert BANNED_CLAIM_RE.search("초특가 이벤트") is not None

    def test_yeokdaegeup_caught(self):
        """역대급 → 걸림."""
        assert BANNED_CLAIM_RE.search("역대급 혜택") is not None

    def test_gungnae_yuil_caught(self):
        """국내 유일 (띄어쓰기 있음) → 걸림."""
        assert BANNED_CLAIM_RE.search("국내 유일 아이템") is not None

    def test_gungnae_yuil_nospace_caught(self):
        """국내유일 (띄어쓰기 없음) → 걸림."""
        assert BANNED_CLAIM_RE.search("국내유일 상품") is not None

    def test_segye_choiso_caught(self):
        """세계 최초 → 걸림."""
        assert BANNED_CLAIM_RE.search("세계 최초 기술") is not None

    def test_segyechoiso_nospace_caught(self):
        """세계최초 → 걸림."""
        assert BANNED_CLAIM_RE.search("세계최초 개발") is not None

    def test_mujeogeon_caught(self):
        """무조건 → 걸림."""
        assert BANNED_CLAIM_RE.search("무조건 추천") is not None


# --------------------------------------------------------------------------- #
# 2. 정상 문장 반례 → 안 걸린다.
# --------------------------------------------------------------------------- #


class TestNormalSentencesNotCaught:
    """정상 한국어 문장/상품명이 과탐되지 않는가.

    기존 픽스처 실제 상품명/본문에서 뽑은 반례 (오탐 스캔 결과 반영):
      - ``테스트 가공식품`` (test_notice_type_coverage.py productName)
      - ``테스트 일반식품`` (test_deferred_fields.py productName)
      - ``면 100%`` — 주의: 이것은 기존 금지표현(100%) 이라 걸려야 정상.
        따라서 반례에서 제외. 대신 ``면 50%`` 를 쓴다.
    """

    def test_gongsik_lookahead_blocks_gongshik_pum(self):
        """가공식품(테스트 픽스처 실제 productName) → 안 걸림 (공식(?!품))."""
        assert (
            BANNED_CLAIM_RE.search("테스트 가공식품") is None
        ), "가공식품 이 공식 패턴에 걸리면 안 됨 (공식(?!품) 룩어헤드 확인)"

    def test_chuksan_gagongsikpum_not_caught(self):
        """축산가공식품 (category_meta 실제 카테고리명) → 안 걸림."""
        assert BANNED_CLAIM_RE.search("축산가공식품") is None
        assert BANNED_CLAIM_RE.search("수산가공식품") is None

    def test_yugin_pum_fixture_not_caught(self):
        """유기가공식품 (certification_types 실제 인증명) → 안 걸림."""
        assert BANNED_CLAIM_RE.search("유기가공식품인증") is None

    def test_ilban_sikpum_fixture_not_caught(self):
        """테스트 일반식품 (test_deferred_fields.py productName) → 안 걸림."""
        assert BANNED_CLAIM_RE.search("테스트 일반식품") is None

    def test_normal_korean_naming_not_caught(self):
        """정상 한국어 상품명 → 안 걸림 (과탐 방어)."""
        assert BANNED_CLAIM_RE.search("우드 슬랩 원목 테이블") is None
        assert BANNED_CLAIM_RE.search("도자기 화병 인테리어 소품") is None
        assert BANNED_CLAIM_RE.search("면 50% 혼용 티셔츠") is None

    def test_gongsik_normal_usage_not_in_sikpum(self):
        """공식 + 품 이 아닌 정상 조합 — 실제로는 과장 의미로 걸려야 함.

        이 테스트는 ``공식적으로`` 같은 정상 단어가 잡히는 것을 확인한다 —
        ``공식`` 단어 자체가 마케팅 과장 맥락에서 자주 쓰이므로 규칙문서가
        금지한다. 단 ``가공식품`` 예외는 유지한다.
        """
        # 공식적으로 는 걸린다 — 의도적 (공식 이 단어 자체가 단정표현).
        assert BANNED_CLAIM_RE.search("공식적으로 인증받은") is not None

    def test_jeongsik_not_caught(self):
        """정식 → 안 걸림 (정규식에서 제외 — 정상 식품 상품명과 구별 불가)."""
        assert BANNED_CLAIM_RE.search("정식 수입품") is None

    def test_control_group_food_names_not_caught(self):
        """통제군 정상 식품 상품명 3건 → 안 걸림 (정식 과탐 철회 확인).

        실제 판매되는 정상 상품명:
          - 가정식 반찬 모둠 500g
          - 한정식 반상기 4인
          - 수제 정식 도시락
        """
        assert BANNED_CLAIM_RE.search("가정식 반찬 모둠 500g") is None
        assert BANNED_CLAIM_RE.search("한정식 반상기 4인") is None
        assert BANNED_CLAIM_RE.search("수제 정식 도시락") is None

    def test_control_group_wisanit_not_caught(self):
        """3위생 마스크 대형 → 안 걸림 (\\d\\s*위 좁힘 확인).

        ``3위생`` 이 ``\\d\\s*위`` 패턴에 걸리지 않아야 한다.
        ``(?<![가-힣0-9])\\d\\s*위(?![가-힣])`` 로 좁혼 결과 확인.
        """
        assert BANNED_CLAIM_RE.search("3위생 마스크 대형") is None
        assert BANNED_CLAIM_RE.search("위생장갑 100매") is None
        assert BANNED_CLAIM_RE.search("1위생용품") is None


# --------------------------------------------------------------------------- #
# 3. 기존 금지표현 회귀 — 전부 그대로 통과.
# --------------------------------------------------------------------------- #


class TestExistingBannedTermsRegression:
    """기존에 잡히던 표현이 여전히 잡히는가 (회귀 방지)."""

    def test_100percent_caught(self):
        assert BANNED_CLAIM_RE.search("면 100%") is not None

    def test_AUTHENTIC_caught(self):
        assert BANNED_CLAIM_RE.search("AUTHENTIC product") is not None

    def test_jeongpum_caught(self):
        assert BANNED_CLAIM_RE.search("정품 보장") is not None

    def test_jinpum_caught(self):
        assert BANNED_CLAIM_RE.search("진품 인증") is not None

    def test_choegogeup_caught(self):
        assert BANNED_CLAIM_RE.search("최고급 소재") is not None

    def test_choesanggeup_caught(self):
        assert BANNED_CLAIM_RE.search("최상급 품질") is not None

    def test_wanbyeok_caught(self):
        assert BANNED_CLAIM_RE.search("완벽한 마감") is not None

    def test_premium_caught(self):
        assert BANNED_CLAIM_RE.search("프리미엄 라인") is not None


# --------------------------------------------------------------------------- #
# 4. 정규식 단일 정의 — text_props.py 한 곳.
# --------------------------------------------------------------------------- #


class TestSingleRegexDefinition:
    """BANNED_CLAIM_RE 가 text_props.py 한 곳에만 정의되어 있는가."""

    def test_regex_imported_not_redefined_in_qa_agents(self):
        """qa_agents.py 는 BANNED_CLAIM_RE 를 import 만 하고 재정의 안 함."""
        # qa_agents 의 BANNED_CLAIM_RE 는 text_props 의 것과 동일 객체.
        from clossify import qa_agents, text_props

        assert qa_agents.BANNED_CLAIM_RE is text_props.BANNED_CLAIM_RE


# --------------------------------------------------------------------------- #
# 5. 감리 실측표 고정 — 순위 양성 5건 · 음성 6건 · BEST 양성 3건·음성 3건.
# (PR #27 감리: 무공백 순위 주장과 BEST상품 이 빠져나가던 결함)
# --------------------------------------------------------------------------- #


class TestRankPositiveAuditedTable:
    """순위 정규식 양성 5건 — 감리 실측표 그대로 고정.

    ``\\d\\s*위(?![가-힣])`` 로 전환한 후 무공백 순위 주장을 전부 잡아야 한다.
    """

    def test_eopgye_1wi_nospace_caught(self):
        """업계1위 브랜드 → 걸림 (구 패턴은 놓쳤던 것)."""
        assert BANNED_CLAIM_RE.search("업계1위 브랜드") is not None

    def test_gungnae_1wi_nospace_caught(self):
        """국내1위 제품 → 걸림."""
        assert BANNED_CLAIM_RE.search("국내1위 제품") is not None

    def test_pammae_1wi_nospace_caught(self):
        """판매1위 상품 → 걸림."""
        assert BANNED_CLAIM_RE.search("판매1위 상품") is not None

    def test_eopgye_1wi_space_caught(self):
        """업계 1위 → 걸림 (구 패턴도 잡았던 것 — 회귀 확인)."""
        assert BANNED_CLAIM_RE.search("업계 1위") is not None

    def test_naver_3wi_caught(self):
        """네이버 3위 → 걸림."""
        assert BANNED_CLAIM_RE.search("네이버 3위") is not None


class TestRankNegativeAuditedTable:
    """순위 정규식 음성 6건 — 감리 실측표 그대로 고정.

    정상어(위생·위생용품·상위/하위) 와 혼동되는 표현은 잡지 않아야 한다.
    """

    def test_3wisang_not_caught(self):
        """3위생 마스크 → 안 걸림 (위생 정상어 보호)."""
        assert BANNED_CLAIM_RE.search("3위생 마스크") is None

    def test_1wisangyongpum_not_caught(self):
        """1위생용품 → 안 걸림 (위생용품 정상어 보호)."""
        assert BANNED_CLAIM_RE.search("1위생용품") is None

    def test_wisangjanggap_not_caught(self):
        """위생장갑 100매 → 안 걸림."""
        assert BANNED_CLAIM_RE.search("위생장갑 100매") is None

    def test_sangwi_10percent_not_caught(self):
        """상위 10% 등급 → 안 걸림 (상위 정상어)."""
        assert BANNED_CLAIM_RE.search("상위 10% 등급") is None

    def test_hawi_hohwan_not_caught(self):
        """하위호환 케이블 → 안 걸림 (하위호환 정상어)."""
        assert BANNED_CLAIM_RE.search("하위호환 케이블") is None

    def test_35wiskijan_not_caught(self):
        """35위스키잔 → 안 걸림 (위스키 정상어)."""
        assert BANNED_CLAIM_RE.search("35위스키잔") is None


class TestBestAuditedTable:
    """BEST 정규식 감리 실측표 — 양성 3건 · 음성 3건.

    ``\\bBEST\\b`` 는 Python 이 한글을 단어문자로 취급하여 ``BEST상품`` 에서
    경계가 안 생겼다. ``(?<![A-Za-z0-9])BEST(?![A-Za-z0-9])`` 로 교체.
    """

    def test_BEST_sangpum_caught(self):
        """BEST상품 (무공백) → 걸림 (구 \\b 패턴은 놓쳤던 것)."""
        assert BANNED_CLAIM_RE.search("BEST상품") is not None

    def test_BEST_space_caught(self):
        """BEST 상품 → 걸림."""
        assert BANNED_CLAIM_RE.search("BEST 상품") is not None

    def test_best_lowercase_caught(self):
        """best 아이템 → 걸림 (IGNORECASE)."""
        assert BANNED_CLAIM_RE.search("best 아이템") is not None

    def test_BESTSELLER_not_caught(self):
        """BESTSELLER 목록 → 안 걸림 (BEST 접두사가 아닌 단어)."""
        assert BANNED_CLAIM_RE.search("BESTSELLER 목록") is None

    def test_SKU_BEST01_not_caught(self):
        """SKU-BEST01 코드 → 안 걸림 (품목코드 안의 BEST)."""
        assert BANNED_CLAIM_RE.search("SKU-BEST01 코드") is None

    def test_korean_beseuteu_not_caught(self):
        """베스트 조끼 → 안 걸림 (한글 베스트, 조끼 카테고리 충돌)."""
        assert BANNED_CLAIM_RE.search("베스트 조끼") is None
