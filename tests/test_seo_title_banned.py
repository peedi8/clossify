# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""SEO-title 금지표현 정규식(``SEO_TITLE_BANNED_RE``) 토큰 경계 수리 검증.

금지표현 패턴 수리 — 항목별 예외(lookahead 좁힘)는 두더지잡기였다. ``고급스러운``
→ ``스러운``, ``인기가요`` → ``가요`` 처럼 판매자 상품명이 훼손되었다.
근본 원인: 삭제형(substring removal) 정규식이 한국어 조사·접미사 붙음을
고려하지 않았다.

**새 구조**: 각 금지 조각을 ``(?<![가-힣]) … (?![가-힣])`` 경계로 감싸,
앞뒤가 한글이 아닐 때(문자열 시작/끝·공백·구두점·숫자·영문) 만 매치한다.
복합형 claim(``최고급``·``한정판``·``무료배송`` …) 도 같은 경계를 쓴다 —
실측 결과 ``최고급분말``·``한정판매처``·``선착순서`` 같은 정상 명사의
부분 문자열로 등장하여 경계 없이는 동일한 훼손이 발생한다.

본 테스트는:

  1. **정상 상품명 보존 20건**: WO 실측 11줄 + 직전 수리 5줄 + 추가 4줄.
  2. **진짜 금지표현 지속 10건**: ``정품 보장``·``최고급 원단`` … — 계속 걸림.
  3. **경계 단위 시험**: 각 조각이 단독이면 걸리고 한글에 붙으면 안 걸린다.
  4. **복합형 부분문자열 보호**: ``최고급분말``·``한정판매처`` … 보존.
"""

from __future__ import annotations

import sys
from pathlib import Path

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify.text_props import (
    BANNED_CLAIM_RE,
    SEO_TITLE_BANNED_RE,
    _sanitize_seo_title,
)

# --------------------------------------------------------------------------- #
# 1. 정상 상품명 보존 20건 — 토큰 경계 수리 후 전부 원문 그대로.
# --------------------------------------------------------------------------- #


class TestNormalProductNamePreserved:
    """``SEO_TITLE_BANNED_RE`` 토큰 경계 수리 후 정상 상품명이 보존되는가.

    회귀(수리 전): 짧은 한글 조각(``고급``·``인기``·``추천`` …) 이 정상
    상품명의 부분 문자열로 흔히 등장하여 ``고급스러운``→``스러운``,
    ``인기가요``→``가요`` 처럼 상품 종류가 제목에서 사라졌다.
    """

    # --- WO 실측 11줄 (직전 수리로는 부족했던 사례) ---

    def test_gogeubseureoun_preserved(self):
        """``고급스러운 원목 선반`` → 원문 보존 (``고급`` 경계 확인)."""
        assert _sanitize_seo_title("고급스러운 원목 선반") == "고급스러운 원목 선반"

    def test_ingigayo_preserved(self):
        """``인기가요 굿즈 앨범`` → 원문 보존 (``인기`` 경계 확인)."""
        assert _sanitize_seo_title("인기가요 굿즈 앨범") == "인기가요 굿즈 앨범"

    def test_cheucheonseo_preserved(self):
        """``추천서 보관 파일`` → 원문 보존 (``추천`` 경계 확인)."""
        assert _sanitize_seo_title("추천서 보관 파일") == "추천서 보관 파일"

    def test_sinsangpumgwon_preserved(self):
        """``신상품권 케이스`` → 원문 보존 (``신상품`` 경계 확인)."""
        assert _sanitize_seo_title("신상품권 케이스") == "신상품권 케이스"

    def test_ibenteuhol_preserved(self):
        """``이벤트홀 조명`` → 원문 보존 (``이벤트`` 경계 확인)."""
        assert _sanitize_seo_title("이벤트홀 조명") == "이벤트홀 조명"

    def test_myungpumgwan_preserved(self):
        """``명품관 쇼핑백`` → 원문 보존 (``명품`` 경계 확인)."""
        assert _sanitize_seo_title("명품관 쇼핑백") == "명품관 쇼핑백"

    def test_gogeubjin_preserved(self):
        """``고급진 가죽 지갑`` → 원문 보존 (``고급`` 경계 확인)."""
        assert _sanitize_seo_title("고급진 가죽 지갑") == "고급진 가죽 지갑"

    def test_pumjeoltem_preserved(self):
        """``품절템 리스트 노트`` → 원문 보존 (``품절`` 경계 확인)."""
        assert _sanitize_seo_title("품절템 리스트 노트") == "품절템 리스트 노트"

    def test_jaeipgoalim_preserved(self):
        """``재입고알림 스티커`` → 원문 보존 (``재입고`` 경계 확인)."""
        assert _sanitize_seo_title("재입고알림 스티커") == "재입고알림 스티커"

    def test_jeoryeomi_preserved(self):
        """``저렴이 화장품 파우치`` → 원문 보존 (``저렴`` 경계 확인)."""
        assert _sanitize_seo_title("저렴이 화장품 파우치") == "저렴이 화장품 파우치"

    def test_imbakah_preserved(self):
        """``임박아 캐릭터 인형`` → 원문 보존 (``임박`` 경계 확인)."""
        assert _sanitize_seo_title("임박아 캐릭터 인형") == "임박아 캐릭터 인형"

    # --- 직전 수리가 고친 5줄 (여전히 보존) ---

    def test_gagongsikpum_seonmulseu_preserved(self):
        """``가공식품 선물세트`` → 원문 보존 (``공식`` 경계 확인)."""
        assert _sanitize_seo_title("가공식품 선물세트") == "가공식품 선물세트"

    def test_gajeongsik_banchan_modum_preserved(self):
        """``가정식 반찬 모둠 500g`` → 원문 보존."""
        assert _sanitize_seo_title("가정식 반찬 모둠 500g") == "가정식 반찬 모둠 500g"

    def test_hanjeongsik_bansanggi_preserved(self):
        """``한정식 반상기 4인`` → 원문 보존 (``한정`` 경계 확인)."""
        assert _sanitize_seo_title("한정식 반상기 4인") == "한정식 반상기 4인"

    def test_suje_jeongsik_dosirak_preserved(self):
        """``수제 정식 도시락`` → 원문 보존."""
        assert _sanitize_seo_title("수제 정식 도시락") == "수제 정식 도시락"

    def test_yeoseong_niteu_beseuteu_preserved(self):
        """``여성 니트 베스트`` → 원문 보존."""
        assert _sanitize_seo_title("여성 니트 베스트") == "여성 니트 베스트"

    # --- 추가 4줄 ---

    def test_wonmok_doseo_preserved(self):
        """``원목 도서 코너 책장`` → 원문 보존."""
        assert _sanitize_seo_title("원목 도서 코너 책장") == "원목 도서 코너 책장"

    def test_suje_beuraendeu_preserved(self):
        """``수제 브랜드 손수건`` → 원문 보존."""
        assert _sanitize_seo_title("수제 브랜드 손수건") == "수제 브랜드 손수건"

    def test_chinhwanbyeong_jeondanji_preserved(self):
        """``친환경 전단지 홀더`` → 원문 보존."""
        assert _sanitize_seo_title("친환경 전단지 홀더") == "친환경 전단지 홀더"

    def test_ibenteuyong_saekjongi_preserved(self):
        """``이벤트용 색종이 세트`` → 원문 보존 (``이벤트`` 뒤 ``용`` 확인)."""
        assert _sanitize_seo_title("이벤트용 색종이 세트") == "이벤트용 색종이 세트"

    # --- 복합형 부분문자열 보호 (추가) ---
    # ``최고급분말`` 은 ``BANNED_CLAIM_RE`` 의 ``최고(?:급)?`` (Out of Scope) 가
    # 잡아버리므로 ``_sanitize_seo_title`` 통과 불가 — ``SEO_TITLE_BANNED_RE``
    # 단독 경계 동작은 TestTokenBoundaryUnits::test_compound_chagogeub_boundary
    # 로 검증한다. 여기선 ``BANNED_CLAIM_RE`` 이 잡지 않는 복합형만 다룬다.

    def test_hanjeongpamaecheo_preserved(self):
        """``한정판매처 안내문`` → 원문 보존 (``한정판`` 경계 확인)."""
        assert _sanitize_seo_title("한정판매처 안내문") == "한정판매처 안내문"

    def test_seonchaksunseo_preserved(self):
        """``선착순서표`` → 원문 보존 (``선착순`` 경계 확인)."""
        assert _sanitize_seo_title("선착순서표") == "선착순서표"

    def test_hanjeongsuryangpyo_preserved(self):
        """``한정수량표`` → 원문 보존 (``한정수량`` 경계 확인)."""
        assert _sanitize_seo_title("한정수량표") == "한정수량표"


# --------------------------------------------------------------------------- #
# 2. 진짜 금지표현 지속 10건 — 토큰 경계 수리 후에도 계속 잡아야 한다.
# --------------------------------------------------------------------------- #


class TestTrueBannedClaimsStillFiltered:
    """토큰 경계 수리 후에도 진짜 금지표현은 여전히 잘려야 한다."""

    def test_jeongpum_bojang_filtered(self):
        """``정품 보장 시계`` → ``정품`` 이 잘려야 한다."""
        out = _sanitize_seo_title("정품 보장 시계")
        assert "정품" not in out, f"'정품' 이 잔존: {out!r}"

    def test_choegogeup_ondan_filtered(self):
        """``최고급 원단 코트`` → ``최고급`` 이 잘려야 한다."""
        out = _sanitize_seo_title("최고급 원단 코트")
        assert "최고급" not in out, f"'최고급' 이 잔존: {out!r}"

    def test_1wi_sangpum_filtered(self):
        """``1위 상품 베개`` → ``1위`` 가 잘려야 한다."""
        out = _sanitize_seo_title("1위 상품 베개")
        assert "1위" not in out, f"'1위' 가 잔존: {out!r}"

    def test_hanjeongpan_edijeun_filtered(self):
        """``한정판 에디션 인형`` → ``한정판`` 이 잘려야 한다."""
        out = _sanitize_seo_title("한정판 에디션 인형")
        assert "한정판" not in out, f"'한정판' 이 잔존: {out!r}"

    def test_muryo_baesong_filtered(self):
        """``무료배송 이벤트 안내`` → ``무료배송`` 이 잘려야 한다."""
        out = _sanitize_seo_title("무료배송 이벤트 안내")
        assert "무료배송" not in out, f"'무료배송' 이 잔존: {out!r}"

    def test_jumun_pokju_filtered(self):
        """``주문폭주 상품 텀블러`` → ``주문폭주`` 가 잘려야 한다."""
        out = _sanitize_seo_title("주문폭주 상품 텀블러")
        assert "주문폭주" not in out, f"'주문폭주' 가 잔존: {out!r}"

    def test_jeuksi_halin_filtered(self):
        """``즉시할인 쿠폰 붙이기`` → ``즉시할인`` 이 잘려야 한다."""
        out = _sanitize_seo_title("즉시할인 쿠폰 붙이기")
        assert "즉시할인" not in out, f"'즉시할인' 이 잔존: {out!r}"

    def test_md_cheucheon_filtered(self):
        """``MD추천 상품 전시대`` → ``MD추천`` 이 잘려야 한다."""
        out = _sanitize_seo_title("MD추천 상품 전시대")
        assert "추천" not in out, f"'추천' 이 잔존: {out!r}"

    def test_seonchaksun_mageun_filtered(self):
        """``선착순 마감 안내문`` → ``선착순`` 이 잘려야 한다."""
        out = _sanitize_seo_title("선착순 마감 안내문")
        assert "선착순" not in out, f"'선착순' 이 잔존: {out!r}"

    def test_gongsik_pamaecheo_filtered(self):
        """``공식 판매처 스티커`` → ``공식`` 이 잘려야 한다 (마케팅 과장).

        ``BANNED_CLAIM_RE`` 의 기존 판정과 동일 — ``공식 판매처`` 의 ``공식``
        은 마케팅 수식어. ``가공식품`` 의 ``공식`` (한글 ``품`` 이 뒤따름) 은
        토큰 경계로 보호된다.
        """
        assert BANNED_CLAIM_RE.search("공식 판매처") is not None
        out = _sanitize_seo_title("공식 판매처 스티커")
        assert "공식" not in out, f"'공식' 이 잔존: {out!r}"


# --------------------------------------------------------------------------- #
# 3. 경계 단위 시험 — 단독=잘리고, 한글에 붙으면 보존.
# --------------------------------------------------------------------------- #


class TestTokenBoundaryUnits:
    """각 금지 조각이 토큰 경계에서 정확히 동작하는가.

    규칙: 단독(문자열 시작/끝·공백 인접) 이면 매치, 한글이 앞뒤에 붙으면
    비매치. 한국어판 단어 경계.
    """

    def test_gogeub_boundary(self):
        """``고급`` 단독=매치 / ``고급스러운``=비매치."""
        assert SEO_TITLE_BANNED_RE.search("고급") is not None
        assert SEO_TITLE_BANNED_RE.search("고급스러운") is None

    def test_ingi_boundary(self):
        """``인기`` 단독=매치 / ``인기가요``=비매치."""
        assert SEO_TITLE_BANNED_RE.search("인기") is not None
        assert SEO_TITLE_BANNED_RE.search("인기가요") is None

    def test_cheucheon_boundary(self):
        """``추천`` 단독=매치 / ``추천서``=비매치."""
        assert SEO_TITLE_BANNED_RE.search("추천") is not None
        assert SEO_TITLE_BANNED_RE.search("추천서") is None

    def test_myungpum_boundary(self):
        """``명품`` 단독=매치 / ``명품관``=비매치."""
        assert SEO_TITLE_BANNED_RE.search("명품") is not None
        assert SEO_TITLE_BANNED_RE.search("명품관") is None

    def test_pumjeol_boundary(self):
        """``품절`` 단독=매치 / ``품절템``=비매치."""
        assert SEO_TITLE_BANNED_RE.search("품절") is not None
        assert SEO_TITLE_BANNED_RE.search("품절템") is None

    def test_jaeipgo_boundary(self):
        """``재입고`` 단독=매치 / ``재입고알림``=비매치."""
        assert SEO_TITLE_BANNED_RE.search("재입고") is not None
        assert SEO_TITLE_BANNED_RE.search("재입고알림") is None

    def test_jeoryeom_boundary(self):
        """``저렴`` 단독=매치 / ``저렴이``=비매치."""
        assert SEO_TITLE_BANNED_RE.search("저렴") is not None
        assert SEO_TITLE_BANNED_RE.search("저렴이") is None

    def test_imbak_boundary(self):
        """``임박`` 단독=매치 / ``임박아``=비매치."""
        assert SEO_TITLE_BANNED_RE.search("임박") is not None
        assert SEO_TITLE_BANNED_RE.search("임박아") is None

    def test_sinsangpum_boundary(self):
        """``신상품`` 단독=매치 / ``신상품권``=비매치."""
        assert SEO_TITLE_BANNED_RE.search("신상품") is not None
        assert SEO_TITLE_BANNED_RE.search("신상품권") is None

    def test_ibenteu_boundary(self):
        """``이벤트`` 단독=매치 / ``이벤트홀``=비매치."""
        assert SEO_TITLE_BANNED_RE.search("이벤트") is not None
        assert SEO_TITLE_BANNED_RE.search("이벤트홀") is None

    def test_hanjeong_boundary(self):
        """``한정`` 단독=매치 / ``한정식``=비매치."""
        assert SEO_TITLE_BANNED_RE.search("한정") is not None
        assert SEO_TITLE_BANNED_RE.search("한정식") is None

    def test_gongsik_boundary(self):
        """``공식`` 단독=매치 / ``가공식품``=비매치."""
        assert SEO_TITLE_BANNED_RE.search("공식") is not None
        assert SEO_TITLE_BANNED_RE.search("가공식품") is None

    def test_compound_chagogeub_boundary(self):
        """``최고급`` 단독=매치 / ``최고급분말``=비매치 (복합형 경계)."""
        assert SEO_TITLE_BANNED_RE.search("최고급") is not None
        assert SEO_TITLE_BANNED_RE.search("최고급분말") is None

    def test_compound_hanjeongpan_boundary(self):
        """``한정판`` 단독=매치 / ``한정판매처``=비매치 (복합형 경계)."""
        assert SEO_TITLE_BANNED_RE.search("한정판") is not None
        assert SEO_TITLE_BANNED_RE.search("한정판매처") is None

    def test_compound_seonchaksun_boundary(self):
        """``선착순`` 단독=매치 / ``선착순서``=비매치 (복합형 경계)."""
        assert SEO_TITLE_BANNED_RE.search("선착순") is not None
        assert SEO_TITLE_BANNED_RE.search("선착순서") is None
