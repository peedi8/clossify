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
  - ``공식`` 단독 → ``가공식품``·``비공식`` 오탐 → ``(?!품)`` 룩어헤드는
    한계가 있어(비공식 굿즈 를 못 막음) WO 3라운드에서 한글 경계
    ``(?<![가-힣])공식(?![가-힣])`` 로 전면 교체.
  - ``베스트`` (한글) → 조끼(vest) 카테고리 충돌 → 한글 ``베스트`` 는 제외,
    영문 ``BEST`` 만 추가.
  - ``정식`` → ``한정식``·``가정식``·``정식 도시락`` 등 정상 식품 상품명과
    구별 불가 → 정규식에서 제외(과탐 철회).
  - 숫자+위 → ``3위생`` 오탐 → 경계 lookaround 로 좁힘.
  - ``\\s*`` 갈래가 낱말 사이를 건너뛰어 ``가공 식료품`` 의 ``공 식``,
    ``최저 가지색`` 의 ``최저 가`` 를 잡는 과탐 → WO 3라운드에서 한 낱말은
    ``\\s*`` 제거 + 경계. 복합형(``초 특가``·``국내 유일``·``세계 최초``) 은
    ``\\s*`` 유지 + 경계.
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
        """가공식품(테스트 픽스처 실제 productName) → 안 걸림.

        경계 ``(?<![가-힣])공식(?![가-힣])`` 로 바꾼 뒤 ``가공식품`` 의
        ``공식`` 앞 ``가``·뒤 ``품`` 이 한글이어서 자연스럽게 비매치한다.
        예전 ``(?!품)`` lookahead 는 경계로 충분해져 제거되었다.
        """
        assert (
            BANNED_CLAIM_RE.search("테스트 가공식품") is None
        ), "가공식품 이 공식 패턴에 걸리면 안 됨 (한글 경계 확인)"

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

    def test_gongsik_with_suffix_not_caught(self):
        """``공식적으로`` (공식 + 적 접미사) → 안 걸림.

        한국어는 조사·접미사가 띄어쓰기 없이 붙는다 — ``공식적으로`` 는
        ``공식`` 의 부사형이지 단독 마케팅 주장이 아니다. 경계
        ``(?<![가-힣])공식(?![가-힣])`` 는 뒤의 ``적`` (한글) 을 보고
        비매치한다. 이는 N86/T3 와 같은 원칙이다. WO 3라운드 금지 목록의
        ``공식 판매처`` (공백 분리) 만 잡는다.
        """
        assert BANNED_CLAIM_RE.search("공식적으로 인증받은") is None

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
        ``\\d\\s*위(?![가-힣])`` 로 좁혼 결과 확인 — ``위`` 뒤 ``생`` 이
        한글이면 비매치.
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


# --------------------------------------------------------------------------- #
# 6. WO 3라운드 감리 — 통제군 8건 (전부 NOT caught) · 금지 9건 (전부 caught).
#
# 두 증상의 같은 뿌리: ``BANNED_CLAIM_RE`` 에 한글 경계가 없었다.
#   - ``비공식 굿즈`` 안의 ``공식`` 이 잡혀 등록이 하드 차단 + 제목 훼손.
#   - ``\s*`` 가 낱말 사이를 건너뛰어 ``가공 식료품`` 의 ``공 식`` 을 잡음.
# 한글 경계 ``(?<![가-힣])...(?![가-힣])`` + 한 낱말의 ``\s*`` 제거로 고쳤다.
# --------------------------------------------------------------------------- #


class TestWoRound3ControlGroupNotCaught:
    """WO 3라운드 통제군 8건 — 한글 경계 수리 후 전부 안 걸려야 한다.

    실측 (수리 전):
      ``비공식 굿즈``        → ``공식`` CAUGHT (FAIL)
      ``가공 식료품``        → ``공 식`` CAUGHT (FAIL, \\s* 과탐)
      ``정품인증서 파일``    → ``정품`` CAUGHT (FAIL)
      ``수산 가공 식자재``   → ``공 식`` CAUGHT (FAIL)
    수리 후: 전부 NOT caught 여야 한다. 원문 보존도 함께 검증한다.
    """

    def test_bigongsik_goods_not_caught(self):
        """``비공식 굿즈`` → 안 걸림 (한글 경계로 ``공식`` 앞 ``비`` 차단)."""
        assert BANNED_CLAIM_RE.search("비공식 굿즈") is None

    def test_bigongsik_fanart_poster_not_caught(self):
        """``비공식 팬아트 포스터`` → 안 걸림."""
        assert BANNED_CLAIM_RE.search("비공식 팬아트 포스터") is None

    def test_gagong_sikryopum_not_caught(self):
        """``가공 식료품`` → 안 걸림 (\\s* 제거로 ``공 식`` 과탐 차단)."""
        assert BANNED_CLAIM_RE.search("가공 식료품") is None

    def test_susan_gagong_sikjajae_not_caught(self):
        """``수산 가공 식자재`` → 안 걸림."""
        assert BANNED_CLAIM_RE.search("수산 가공 식자재") is None

    def test_gagongsikpum_seonmul_not_caught(self):
        """``가공식품 선물세트`` → 안 걸림 (``공식`` 뒤 ``품`` 한글)."""
        assert BANNED_CLAIM_RE.search("가공식품 선물세트") is None

    def test_sgiiseonhyu_bochungje_not_caught(self):
        """``식이섬유 보충제`` → 안 걸림."""
        assert BANNED_CLAIM_RE.search("식이섬유 보충제") is None

    def test_gongsanpum_bogwanham_not_caught(self):
        """``공산품 보관함`` → 안 걸림 (``공산`` 은 ``공식`` 이 아님)."""
        assert BANNED_CLAIM_RE.search("공산품 보관함") is None

    def test_jeongpuminjeungseo_file_not_caught(self):
        """``정품인증서 파일`` → 안 걸림 (한글 경계로 ``정품`` 뒤 ``인`` 차단).

        WO §1 이 건을 명시적으로 확인하라고 지시했다 — ``정\\s*품`` 이
        ``정품인증서`` 를 먹는지.
        """
        assert BANNED_CLAIM_RE.search("정품인증서 파일") is None


class TestWoRound3BannedGroupCaught:
    """WO 3라운드 금지군 9건 — 한글 경계 수리 후에도 전부 잡혀야 한다."""

    def test_gongsik_pamaecheo_caught(self):
        """``공식 판매처`` → 걸림 (공백 분리 — 마케팅 수식어)."""
        assert BANNED_CLAIM_RE.search("공식 판매처") is not None

    def test_100percent_jeongpum_caught(self):
        """``100% 정품`` → 걸림 (``100%`` + ``정품``)."""
        assert BANNED_CLAIM_RE.search("100% 정품") is not None

    def test_choegogeup_ondan_caught(self):
        """``최고급 원단`` → 걸림."""
        assert BANNED_CLAIM_RE.search("최고급 원단") is not None

    def test_chojeoga_bojang_caught(self):
        """``최저가 보장`` → 걸림."""
        assert BANNED_CLAIM_RE.search("최저가 보장") is not None

    def test_gungnae_yuil_caught(self):
        """``국내 유일`` → 걸림."""
        assert BANNED_CLAIM_RE.search("국내 유일") is not None

    def test_segye_choiso_caught(self):
        """``세계 최초`` → 걸림."""
        assert BANNED_CLAIM_RE.search("세계 최초") is not None

    def test_mujeogeon_manjeok_caught(self):
        """``무조건 만족`` → 걸림."""
        assert BANNED_CLAIM_RE.search("무조건 만족") is not None

    def test_eopgye_1wi_caught(self):
        """``업계 1위`` → 걸림."""
        assert BANNED_CLAIM_RE.search("업계 1위") is not None

    def test_BEST_sangpum_caught(self):
        """``BEST상품`` → 걸림."""
        assert BANNED_CLAIM_RE.search("BEST상품") is not None


class TestWoRound3SanitizationPreserves:
    """WO 3라운드 통제군 정제 결과 — 원문 보존.

    ``_sanitize_seo_title`` 은 ``BANNED_CLAIM_RE`` 를 먼저 sub 한다. 경계
    수리 후 통제군은 원문 그대로 나와야 한다.
    """

    def test_bigongsik_goods_preserved(self):
        from clossify.text_props import _sanitize_seo_title

        assert _sanitize_seo_title("비공식 굿즈") == "비공식 굿즈"

    def test_gagong_sikryopum_preserved(self):
        from clossify.text_props import _sanitize_seo_title

        assert _sanitize_seo_title("가공 식료품") == "가공 식료품"

    def test_jeongpuminjeungseo_preserved(self):
        from clossify.text_props import _sanitize_seo_title

        assert _sanitize_seo_title("정품인증서 파일") == "정품인증서 파일"


# --------------------------------------------------------------------------- #
# 7. WO 3라운드 — ``\\s*`` 갈래 통제군 판정표.
#
# WO §1: "눈으로 훑지 마라(직전 라운드에서 그 방식이 틀렸다)" — 각 \\s* 갈래를
# 통제군으로 돌려 판정한다. 한 낱말은 ``\\s*`` 제거, 복합형은 ``\\s*`` 유지 +
# 경계.
# --------------------------------------------------------------------------- #


class TestWoRound3SstarBranchAudit:
    """``\\s*`` 갈래별 통제군 판정표.

    각 갈래 / 통제군 / 조치:
      ``100\\s*%``       | ``100매`` (수량) 안 걸림          | 유지 (``%`` 앵커)
      ``AUTH\\s*ENTIC``  | 영문 전용, 한글 위험 없음         | 유지
      ``정\\s*품``       | ``정품인증서`` 과탐               | ``\\s*`` 제거 + 경계
      ``진\\s*품``       | ``진품감정사`` 과탐               | ``\\s*`` 제거 + 경계
      ``공\\s*식(?!품)`` | ``가공 식료품`` · ``비공식`` 과탐 | ``\\s*`` 제거 + 경계 + ``(?!품)`` 제거
      ``\\d\\s*위``      | ``3위생`` 정상어 보호             | 유지 (이미 경계)
      ``최저\\s*가``     | ``최저 가지색`` 의 ``가`` 과탐    | ``\\s*`` 제거 + 경계
      ``초\\s*특가``     | ``초 특별 솔루션`` 안 걸림        | ``\\s*`` 유지 + 경계
      ``국내\\s*유일``   | ``국내 유기농 일반`` 안 걸림      | ``\\s*`` 유지 + 경계
      ``세계\\s*최초``   | ``세계 최저 초콜릿`` 안 걸림      | ``\\s*`` 유지 + 경계
    """

    def test_100sstar_percent_no_quantity_overtap(self):
        """``100\\s*%`` — ``위생장갑 100매`` 안 걸림 (``%`` 없음)."""
        assert BANNED_CLAIM_RE.search("위생장갑 100매") is None

    def test_jeongsstar_pum_juminjeungseo_not_caught(self):
        """``정\\s*품`` (구) → ``정품인증서 파일`` 과탐. 수리 후 안 걸림."""
        assert BANNED_CLAIM_RE.search("정품인증서 파일") is None

    def test_jinsstar_pum_jinjeung_not_caught(self):
        """``진\\s*품`` (구) → ``진품감정사`` 과탐 위험. 수리 후 안 걸림."""
        assert BANNED_CLAIM_RE.search("진품감정사") is None

    def test_gongsstar_sik_bigongsik_not_caught(self):
        """``공\\s*식`` (구) → ``비공식 굿즈`` 과탐. 수리 후 안 걸림."""
        assert BANNED_CLAIM_RE.search("비공식 굿즈") is None

    def test_gongsstar_sik_gagong_sikryopum_not_caught(self):
        """``공\\s*식`` (구) → ``가공 식료품`` 의 ``공 식`` 과탐. 수리 후 안 걸림."""
        assert BANNED_CLAIM_RE.search("가공 식료품") is None

    def test_dsstar_wi_3wisang_not_caught(self):
        """``\\d\\s*위`` — ``3위생 마스크`` 안 걸림 (``위`` 뒤 ``생`` 한글)."""
        assert BANNED_CLAIM_RE.search("3위생 마스크") is None

    def test_chojeosstar_ga_gajisaek_not_caught(self):
        """``최저\\s*가`` (구) → ``최저 가지색`` 의 ``가`` 과탐. 수리 후 안 걸림.

        ``최저가(?![가-힣])`` 로 바꾸어 ``최저 가지색`` 의 분리된 ``가`` 를
        안 잡는다.
        """
        assert BANNED_CLAIM_RE.search("최저 가지색 액자") is None

    def test_chosstar_teukga_cho_teukbyeol_not_caught(self):
        """``초\\s*특가`` (유지) — ``초 특별 솔루션`` 안 걸림 (``특`` 뒤 ``가`` 아님)."""
        assert BANNED_CLAIM_RE.search("초 특별 솔루션") is None

    def test_gungnaesstar_yuil_yuginong_not_caught(self):
        """``국내\\s*유일`` (유지) — ``국내 유기농 일반`` 안 걸림 (``유일`` 아님)."""
        assert BANNED_CLAIM_RE.search("국내 유기농 일반") is None

    def test_segyesstar_choiso_chojeo_not_caught(self):
        """``세계\\s*최초`` (유지) — ``세계 최저 초콜릿`` 안 걸림 (``최초`` 아님)."""
        assert BANNED_CLAIM_RE.search("세계 최저 초콜릿") is None
