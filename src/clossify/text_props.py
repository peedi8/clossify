# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""Text and property extraction helpers.

Ported from the original sourcing pipeline. Depends on :mod:`common`.

All Chinese (Hanja) detection and stripping code has been
removed entirely. This product only ingests Korean user-supplied text,
so there is no input path that could carry Chinese ideographs. The
Korean marketing-claim filters are preserved and use literal Korean
characters.

This module is now the canonical home of the text-filter
regexes (``BANNED_CLAIM_RE``, ``EDITORIAL_NOISE_RE``, ...). Downstream
modules (``copywriting``) import them from here. This module no longer
imports any other ``clossify`` submodule — the previous lazy import of
``BANNED_CLAIM_RE`` from :mod:`copywriting` (which created a cycle) is
gone. The translation / external-market prop helpers have been removed
(this product only ingests Korean user-supplied text).
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

# This module must not import any other ``clossify`` submodule
# (top-level or lazy). It is the upstream node of the DAG; ``copywriting``
# and ``seo`` import from here, never the reverse. ``_safe_float`` is
# available from :mod:`common` directly — do not re-export it here.

# ---------------------------------------------------------------------------
# Image / detail rendering limits. Pure literals.
# ---------------------------------------------------------------------------

MAIN_IMAGE_LIMIT = None
LISTING_IMAGE_LIMIT = None
DESC_IMAGE_SCAN_LIMIT: int = 32
OPTION_GRID_LIMIT = None
DETAIL_RENDER_WIDTH: int = 1000
DETAIL_CONTENT_TARGET: int = 860
DETAIL_ASPECT_TALL: float = 1.3
DETAIL_IMAGES_MIN: int = 5
DETAIL_IMAGES_MAX: int = 10
DETAIL_TILE_MIN_CONTENT: int = 760
DETAIL_TILE_CONTENT_MAX: int = DETAIL_CONTENT_TARGET
DETAIL_TILE_MAX_UPSCALE: float = 1.8
DETAIL_TILE_SKIP_MIN: int = 0
DETAIL_RENDER_CAPTURE_SCALE: int = 2
DETAIL_RENDER_SEGMENT_MAX_DEVICE_PX: int = 12000
DETAIL_RENDER_FINAL_JPEG_QUALITY: int = 95
DETAIL_HERO_IMAGE_COUNT: int = 2
DETAIL_MERGE_COLUMNS: int = 2
DETAIL_MERGE_ROWS: int = 2
DETAIL_MERGE_CELL: int = DETAIL_RENDER_WIDTH // DETAIL_MERGE_COLUMNS
RETOUCH_SHEET_MAX_PX: int = 2048
RETOUCH_GRID_MAX_DEFAULT: int = 5
RETOUCH_GRID_MAX_LIMIT: int = 5
RETOUCH_GRID_MIN_CONTENT: int = 400
RETOUCH_GRID_PADDING: int = 12

# ---------------------------------------------------------------------------
# Regexes. Korean patterns are expressed as literal characters (no
# \u escapes).
# ---------------------------------------------------------------------------

OPTION_LABEL_TEXT_RE = re.compile(
    r"(?:\bSTY(?:LE|IE|1E)\b|\bTYPE\b|\bMODEL\b|\bCOLOR\b|"
    r"(?<![A-Za-z0-9])[A-Z]\s*\d{1,3}(?![A-Za-z0-9]))",
    re.IGNORECASE,
)

SELLER_SIZE_TEXT_RE = re.compile(
    r"(?:\d+(?:\.\d+)?\s*(?:cm|mm|m|in|inch)" r"\s*(?:[*xX\u00d7]\s*)?){2,3}",
    re.IGNORECASE,
)

DETAIL_GARBAGE_TEXT_RE = re.compile(
    r"watermark|logo|coupon|free\s*shipping|sale",
    re.IGNORECASE,
)

DETAIL_INFOGRAPHIC_TEXT_RE = re.compile(
    r"our\s*product\s*advantages|product\s*advantages|"
    r"A5\s*melamine|melamine\s*material|"
    r"utensils?",
    re.IGNORECASE,
)

STRONG_GARBAGE_TEXT_RE = re.compile(r"(?!x)x", re.IGNORECASE)

SELLER_NOTICE_HEADING_RE = re.compile(r"(?!x)x", re.IGNORECASE)

OPTION_CARD_TONES = {"brown", "orange", "pink", "green", "neutral"}
OPTION_CODE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Za-z]{1,12}\d[A-Za-z0-9_-]*|\d+[A-Za-z][A-Za-z0-9_-]*)(?![A-Za-z0-9])"
)

PROPERTY_FIELD_SPLIT_RE = re.compile(r"[:\uff1a]")

# ---------------------------------------------------------------------------
# Text-filter regexes — canonical home.
#
# These were previously defined in ``copywriting`` and imported
# lazily from here, creating a hidden circular dependency. The canonical
# definitions now live in this module (the upstream DAG node).
# Korean patterns are literal characters.
# ---------------------------------------------------------------------------

# 한국어 조사·어미 lookahead — ``BANNED_CLAIM_RE`` 의 뒤쪽 경계로 쓴다.
#
# WO 4라운드 감리 실측: 3라운드에서 단순 ``(?![가-힣])`` 를 붙인 결과,
# 한국어의 조사·어미(은·는·이·가·을·를·의·에 …) 가 **전부 한글**이라
# ``정품입니다``·``정품을 보장``·``최고의 품질`` 같은 정상 금지 주장이
# 통째로 빠져나갔다 (게이트가 있으나 마나). 이 컴플라이언스 게이트는
# 미탐이 오탐보다 위험하므로(WO §1) 경계를 뒤집는다:
#
#   구: ``(?![가-힣])``             — 한글이 뒤에 오면 무조건 놓침
#   신: ``(?:(?![가-힣])|(?=조사·어미))`` — 한글이 아니거나, 조사·어미로
#                                       시작하면 잡는다
#
# 한국어 조사·어미는 닫힌 집합이므로 정상 복합명사(``정품인증서``·
# ``가공식품``) 와 마케팅 주장 + 조사(``정품입니다``) 를 이것으로 가른다.
# 정규식 alternation 은 왼쪽이 우선이므로 긴 것부터 짧은 것 순으로 나열한다.
#
# WO §2 가 명시한 최소 목록에 더해 자주 쓰이는 보조사·연결어미를 추가했다:
#   - ``하고``·``(이)나``·``(이)며``·``(이)면서``·``대로``·``뿐``·``임``·
#     ``마저``·``조차``·``든`` — ``정품하고 비교``·``최저가임``·
#     ``최고급뿐`` 같은 구어체·명사형 종결에도 잡기 위해. 각 항목은
#     한국어 교재의 보조사/연결어미 표준 목록에 근거.
# ``요`` (해요체) 와 ``죠`` (죠체) 도 회화체 종결어미로 빈번하므로 추가 —
# ``정품이죠``·``최저가요`` 잡기.
_KOREAN_PARTICLE_OR_ENDING = (
    r"(?:"
    # 다음절 종결어미 (긴 것 우선)
    r"입니다|이에요|예요|이라고|이라|으로|에서|부터|까지|보다|처럼|"
    r"하게|해서|하다|한|한다|했|이었|였|스럽|니다|대로|면서|으며|"
    # 보조사·연결어미 (한 음절 이상)
    r"하고|이나|나나|든지|이며|뿐|임|마저|조차|대로|"
    # 단음절 조사 (가장 짧음)
    r"은|는|이|가|을|를|의|에|로|와|과|도|만|나|며|요|죠|든|죠"
    r")"
)


# 각 한글 금지 조각의 뒤쪽 경계:
# 비한글(문자열 끝·공백·숫자·영문·구두점) 이거나, ``_KOREAN_PARTICLE_OR_ENDING``
# 으로 시작해야 매치한다. ``(?=...)`` lookahead 로 조사·어미 시작점을 보고,
# ``(?![가-힣])`` 로 비한글 경계를 잡는다. 둘을 ``|`` 로 묶어 "둘 중 하나".
def _kr_tail():
    """뒤쪽 경계: 비한글 또는 한국어 조사·어미 시작.

    ``(?![가-힣])`` — 비한글(공백·문장부호·숫자·영문·문자열 끝) 이면 통과.
    ``(?=_KOREAN_PARTICLE_OR_ENDING)`` — 뒤가 조사·어미로 시작하면 통과.
    """
    return r"(?:(?![가-힣])|(?=" + _KOREAN_PARTICLE_OR_ENDING + r"))"


BANNED_CLAIM_RE = re.compile(
    # ── 영문·숫자 조각: 영문/숫자 경계로 판별 ──
    # ``100%``: % 가 있어야 매치 — "100매" 같은 정상 수량엔 안 걸림.
    r"100\s*%|"
    # ``AUTHENTIC``: 영문 접두/접미 경계.
    r"AUTH\s*ENTIC|"
    # 영문 BEST: Python \b 는 한글을 단어문자로 취급하여 "BEST상품" 에서
    # 경계가 안 생긴다. 영문자/숫자 경계로만 판별한다.
    r"(?<![A-Za-z0-9])BEST(?![A-Za-z0-9])|"
    # ── 한글 조각: 한국어판 단어 경계 (N86/T3 + WO 4라운드 수리) ──
    #
    # WO 3라운드에서 각 한글 금지 조각을 ``(?<![가-힣]) … (?![가-힣])`` 로
    # 감쌌다. 앞쪽 lookbehind 는 그대로 둔다 — ``비공식`` 의 ``비`` 등
    # 부정 접두사 한글 차단은 유효하다.
    #
    # **뒤쪽 경계만 WO 4라운드에서 뒤집는다** (3라운드 수리를 깨뜨리지 말 것):
    # ``(?![가-힣])`` 는 한국어 조사·어미(은·는·이·가·을·를·의·에 …) 가
    # 전부 한글이라 ``정품입니다``·``정품을 보장``·``최고의 품질`` 같은
    # 정상 금지 주장을 통째로 놓쳤다(미탐 대량). ``_kr_tail()`` 로 교체:
    # 비한글이거나 조사·어미로 시작할 때만 매치한다. ``정품인증서``
    # (``인`` 은 조사가 아님) · ``가공식품`` (``식`` 뒤 ``품``) · ``한정식``
    # 같은 복합명사는 여전히 보호된다.
    #
    # **경계 없이 ``공\s*식`` 을 쓰면 두 사고가 겹친다** (WO 3라운드 감리
    # 실측):
    #   1. ``비공식`` 안의 ``공식`` 이 그대로 잡힌다 — 금지 주장의 부정형이
    #      금지 주장으로 판정되어 ``비공식 굿즈`` 가 하드 차단된다.
    #   2. ``\s*`` 가 낱말 경계를 넘는다 — ``가공 식료품`` 의 ``공``+공백+
    #      ``식`` 이 ``공\s*식`` 에 걸린다.
    # 앞쪽 경계 + ``\s*`` 제거로 둘 다 고친다. ``정품``·``진품``·``최저가`` 도
    # 동일한 패턴: ``정품인증서``·``진품감정사``·``최저 가지색`` 과탐을
    # 막는다.
    r"(?<![가-힣])정품" + _kr_tail() + r"|"
    r"(?<![가-힣])진품" + _kr_tail() + r"|"
    r"(?<![가-힣])최고(?:급)?" + _kr_tail() + r"|"
    r"(?<![가-힣])최상급" + _kr_tail() + r"|"
    r"(?<![가-힣])완벽(?:한|하게)?" + _kr_tail() + r"|"
    r"(?<![가-힣])프리미엄" + _kr_tail() + r"|"
    # ``공식`` — 단어 경계만으로 ``가공식품`` (앞 ``가``·뒤 ``품`` 이 한글)
    # 을 충분히 보호하므로, 예전 ``(?!품)`` lookahead 는 불필요해졌다.
    # ``비공식`` 도 앞의 ``비`` 가 한글이어서 lookbehind 가 차단한다.
    r"(?<![가-힣])공식" + _kr_tail() + r"|"
    # 순위 주장: ``\d\s*위`` + 뒤쪽 경계 — 앞쪽은 lookbehind 없이
    # (``업계1위``·``국내1위`` 같은 무공백 형태를 잡기 위해), 뒤쪽은 한글이
    # 바로 붙으면 통과(``1위생용품`` 정상어 보호). N86/T3 검증 패턴.
    # "1위상품"·"100위권" 은 ``위`` 뒤 한글이라 못 잡지만 — ``1위생용품``
    # 정상어와 구별 불가이므로 COMPLIANCE_RULES.md §1 비고에 명시했다.
    # ``업계 1위의`` (``의`` 조사)·``업계 1위입니다`` (``입니다`` 어미) 는
    # WO 4라운드에서 ``_kr_tail()`` 로 잡는다.
    r"\d\s*위" + _kr_tail() + r"|"
    # 가격 배타 주장. ``최저가`` 는 한 낱말이므로 ``\s*`` 제거 + 경계.
    # (구 ``최저\s*가`` 는 ``최저 가지색`` 의 ``가`` 를 잡아 과탐.)
    r"(?<![가-힣])최저가" + _kr_tail() + r"|"
    # ``초특가``·``초 특가`` 둘 다 잡아야 하므로 ``\s*`` 유지 + 경계.
    # ``초 특별 솔루션`` 은 ``특`` 뒤 ``가`` 가 아니므로 안 걸림.
    r"(?<![가-힣])초\s*특가" + _kr_tail() + r"|"
    r"(?<![가-힣])역대급" + _kr_tail() + r"|"
    # 배타 주장. ``국내 유일``·``국내유일``·``세계 최초``·``세계최초`` 를
    # 잡기 위해 ``\s*`` 유지 + 경계.
    r"(?<![가-힣])국내\s*유일" + _kr_tail() + r"|"
    r"(?<![가-힣])세계\s*최초" + _kr_tail() + r"|"
    r"(?<![가-힣])무조건" + _kr_tail(),
    re.IGNORECASE,
)

EDITORIAL_NOISE_RE = re.compile(
    r"배송|출고|발송|택배|"
    r"판매처|판매자|스토어|"
    r"구매대행|주문\s*확인|"
    r"반품|교환|고객센터|"
    r"무료배송|특가|도매|"
    r"공장직영|쿠폰",
    re.IGNORECASE,
)

EMPTY_MARKETING_COPY_RE = re.compile(
    r"일상에\s*별별|당신만을\s*위한|"
    r"나만을\s*위한|별별한\s*하루|"
    r"삶의\s*격|생활의\s*격|"
    r"공간을\s*완성|물드를\s*완성|"
    r"감성을\s*더하|각을\s*더하|"
    r"완벽한\s*선택|소중한\s*사람을\s*위한",
    re.IGNORECASE,
)

SENSORY_COPY_NOISE_RE = EMPTY_MARKETING_COPY_RE

# ---------------------------------------------------------------------------
# Category-path -> notice-type heuristic table (canonical, single source).
#
# Both ``qa_agents._infer_notice_type`` (prepare step) and
# ``naver_client._resolve_notice_type`` (register step) must infer the same
# notice type from the same category path. Previously this table existed as
# two literal copies with a comment admitting the duplication; the copies
# inevitably diverged. It now lives once here, and both modules import this
# symbol. This module is the upstream DAG node (no clossify imports), so it
# is the safe shared home for both consumers.
#
# The single source of truth for notice *types/fields* remains
# ``data/notice_types.json``; this tuple is only the path-keyword heuristic
# that picks a candidate type before the data file is consulted.
# ---------------------------------------------------------------------------
CATEGORY_PATH_NOTICE_HINTS = (
    ("가구", "FURNITURE"),
    ("의류", "WEAR"),
    ("신발", "SHOES"),
    ("구두", "SHOES"),
    ("가방", "BAG"),
    ("침구", "SLEEPING_GEAR"),
    ("커튼", "SLEEPING_GEAR"),
    ("가전", "HOME_APPLIANCES"),
    ("영상가전", "IMAGE_APPLIANCES"),
    ("계절가전", "SEASON_APPLIANCES"),
    ("사무용기기", "OFFICE_APPLIANCES"),
    ("휴대폰", "CELLPHONE"),
    ("광학기기", "OPTICS_APPLIANCES"),
    ("귀금속", "JEWELLERY"),
    ("보석", "JEWELLERY"),
    ("시계", "JEWELLERY"),
    ("서적", "BOOKS"),
    ("어린이", "KIDS"),
    ("생활화학", "BIOCHEMISTRY"),
    ("살생물", "BIOCIDAL"),
    ("패션잡화", "FASHION_ITEMS"),
    ("주방", "KITCHEN_UTENSILS"),
    ("식기", "KITCHEN_UTENSILS"),
    ("화장품", "COSMETIC"),
    ("식품", "FOOD"),
    ("스포츠", "SPORTS_EQUIPMENT"),
    ("악기", "MUSICAL_INSTRUMENT"),
    ("자동차", "CAR_ARTICLES"),
    ("의료기기", "MEDICAL_APPLIANCES"),
    ("네비게이션", "NAVIGATION"),
)

# SEO-title specific banned patterns. These are a
# superset of the marketing-claim regex aimed at title copy.
#
# **구조 (N86/T3 수리)**: 예전 항목별 lookahead 좁힘은 두더지잡기였다 —
# 짧은 한글 조각(``고급``·``인기``·``추천`` …) 이 정상 상품명의 부분
# 문자열로 흔히 등장하여 ``고급스러운``→``스러운``, ``인기가요``→``가요``
# 처럼 판매자 상품명을 훼손했다. 근본 원인은 삭제형(substring removal) 이
# 한국어 조사·접미사 붙음을 고려하지 않은 것이다.
#
# **새 규칙**: 삭제는 "낱말 단위로 떨어질 때만" 한다. 각 조각을
# ``(?<![가-힣]) … (?![가-힣])`` 경계로 감싸, 앞뒤가 한글이 아닐 때
# (문자열 시작/끝·공백·구두점·숫자·영문) 만 매치한다. 한국어판 단어 경계.
#
# **복합형 claim** (``최고급``·``한정판``·``무료배송`` …) 도 같은 경계를
# 쓴다 — 실측 결과 ``최고급분말``·``한정판매처``·``선착순서`` 같은 정상
# 명사의 부분 문자열로 등장하므로, 경계 없이는 동일한 훼손이 발생한다.
# 경계가 있어도 단독 사용(``최고급 원단``) 은 잡힌다.
#
# **판단 원칙**: 이 함수는 컴플라이언스 게이트가 **아니다**. 법적 차단은
# ``BANNED_CLAIM_RE`` (거부형) 이 맡는다. 여기서 하나 놓치는 것보다
# 판매자 상품명을 훼손하는 것이 더 나쁘다. 애매하면 보존.
SEO_TITLE_BANNED_RE = re.compile(
    # 복합형 claim — 경계 안에서만 (한글이 앞뒤에 없어야 매치).
    r"(?<![가-힣])최고급(?![가-힣])|"
    r"(?<![가-힣])한정판(?![가-힣])|"
    r"(?<![가-힣])한정수량(?![가-힣])|"
    r"(?<![가-힣])무료\s*배송(?![가-힣])|"
    r"(?<![가-힣])주문\s*폭주(?![가-힣])|"
    r"(?<![가-힣])즉시\s*할인(?![가-힣])|"
    r"(?<![가-힣])MD\s*추천(?![가-힣])|"
    r"(?<![가-힣])선착순(?![가-힣])|"
    # 단어-경계 조각들 — 앞뒤 한글이 아니어야 매치.
    r"(?<![가-힣])정\s*품(?![가-힣])|"
    r"(?<![가-힣])최\s*고(?![가-힣])|"
    r"(?<![가-힣])1\s*위(?![가-힣])|"
    r"(?<![가-힣])100\s*%(?![가-힣])|"
    r"(?<![가-힣])공\s*식(?![가-힣])|"
    r"(?<![가-힣])명\s*품(?![가-힣])|"
    r"(?<![가-힣])고\s*급(?![가-힣])|"
    r"(?<![가-힣])재입고(?![가-힣])|"
    r"(?<![가-힣])한정(?![가-힣])|"
    r"(?<![가-힣])첫구매(?![가-힣])|"
    r"(?<![가-힣])공짜(?![가-힣])|"
    r"(?<![가-힣])품절(?![가-힣])|"
    r"(?<![가-힣])임박(?![가-힣])|"
    r"(?<![가-힣])인기(?![가-힣])|"
    r"(?<![가-힣])가성비(?![가-힣])|"
    r"(?<![가-힣])저렴(?![가-힣])|"
    r"(?<![가-힣])추천(?![가-힣])|"
    r"(?<![가-힣])신상품(?![가-힣])|"
    r"(?<![가-힣])이벤트(?![가-힣])",
    re.IGNORECASE,
)

SEO_STOPWORDS = {
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "의",
    "와",
    "과",
    "도",
    "로",
    "으로",
    "에",
    "에서",
    "및",
    "또는",
    "그리고",
    "상품",
    "제품",
}


# ---------------------------------------------------------------------------
# Description HTML -> text helpers.
# ---------------------------------------------------------------------------


class _DescTextExtractor(HTMLParser):
    """Extract visible text from upstream description HTML.

    Drops ``script``/``style``/``noscript``/``svg`` subtrees and emits a
    newline at each block-level boundary so callers can re-flow lines.
    """

    BLOCK_TAGS = frozenset(
        {
            "p",
            "div",
            "br",
            "li",
            "tr",
            "td",
            "th",
            "section",
            "article",
            "header",
            "footer",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "ul",
            "ol",
            "table",
            "tbody",
            "thead",
            "figcaption",
            "figure",
            "blockquote",
        }
    )
    SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "template"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
            return
        if not self._skip_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if not self._skip_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth and data:
            self.parts.append(data)


def _normalize_desc_text(text, limit=6000):
    """Collapse whitespace and trim a raw description string.

    Drops zero-width/nbsp characters and bare URL lines.
    """
    text = html.unescape(str(text or ""))
    text = re.sub(r"[\u00a0\u200b\ufeff]+", " ", text)
    lines = []
    for line in re.split(r"[\r\n]+", text):
        line = re.sub(r"\s+", " ", line).strip()
        if not line or re.fullmatch(r"https?://\S+", line, flags=re.IGNORECASE):
            continue
        lines.append(line)
    return "\n".join(lines)[:limit].strip()


def desc_html_to_text(desc_html):
    """Return visible text from upstream desc HTML.

    Image-only desc returns empty string.
    """
    raw = str(desc_html or "").strip()
    if not raw:
        return ""
    raw = html.unescape(raw)
    parser = _DescTextExtractor()
    try:
        parser.feed(raw)
        parser.close()
        text = "".join(parser.parts)
    except Exception:
        text = re.sub(r"<(script|style|noscript|svg)\b[\s\S]*?</\1>", " ", raw, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
    return _normalize_desc_text(text)


def _hesc(value, default=""):
    """HTML-escape ``value`` for safe inline interpolation."""
    text = str(value if value not in (None, "") else default)
    return html.escape(text, quote=True)


# ---------------------------------------------------------------------------
# Property flatten / summarise.
# ---------------------------------------------------------------------------


def _first_text(*values, default=""):
    """Return the first non-empty stringified value, else ``default``."""
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return default


def _compact_spaces(text):
    """Collapse runs of whitespace into single spaces and trim."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _strip_banned_claims(text):
    """Strip banned Korean marketing claims and collapse whitespace.

    ``BANNED_CLAIM_RE`` lives in this module, so this helper performs
    the real removal rather than being an identity.
    """
    text = BANNED_CLAIM_RE.sub(" ", str(text or ""))
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def _detail_safe_text(text, default=""):
    """Sanitise free-form text for detail rendering.

    Strips banned Korean marketing claims (e.g. ``"100%"``, ``"정품"`` /
    ``"진품"`` = "genuine/authentic", ``"최고급"`` = "top-grade",
    ``"프리미엄"`` = "premium") then collapses whitespace.
    """
    text = _strip_banned_claims(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or default


def _sanitize_seo_title(text, *, max_len=100):
    """Sanitise a candidate SEO title.

    Restored to the original pipeline:
      1. strip banned marketing claims (``BANNED_CLAIM_RE``)
      2. strip SEO-title-specific banned patterns (``SEO_TITLE_BANNED_RE``)
      3. drop non-Korean/non-ASCII-alnum/non-space characters
      4. drop SEO stopwords and duplicate words (case-insensitive)
      5. truncate to ``max_len`` on a word boundary
    """
    text = _strip_banned_claims(text)
    text = SEO_TITLE_BANNED_RE.sub(" ", text)
    text = re.sub(r"[^0-9A-Za-z가-힣\s]", " ", text)
    words, seen = [], set()
    for word in _compact_spaces(text).split():
        key = word.lower()
        if key in SEO_STOPWORDS or key in seen:
            continue
        seen.add(key)
        words.append(word)
    title = " ".join(words)
    if len(title) > max_len:
        cut = title[:max_len].rstrip()
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        title = cut or title[:max_len]
    return title.strip()


def _flatten_prop_terms(value, *, limit=30, clean=True):
    """Flatten nested prop structures into a flat list of short phrases.

    Walks dicts/lists/tuples; for each leaf emits ``"<label> <value>"``
    when a label/value pair is detectable, otherwise the raw string.
    """
    terms: list[str] = []

    def add(text):
        text = _compact_spaces(text)
        fields = [x.strip() for x in PROPERTY_FIELD_SPLIT_RE.split(text) if x.strip()]
        if len(fields) >= 4:
            text = f"{fields[-2]} {fields[-1]}"
        elif len(fields) == 2:
            text = f"{fields[0]} {fields[1]}"
        if clean:
            text = _detail_safe_text(text)
        if text and text not in terms:
            terms.append(text)

    def walk(v):
        if len(terms) >= limit:
            return
        if v in (None, ""):
            return
        if isinstance(v, dict):
            label = _first_text(
                v.get("name"),
                v.get("label"),
                v.get("key"),
                v.get("title"),
                v.get("prop_name"),
                v.get("attr_name"),
                default="",
            )
            val = _first_text(
                v.get("value"),
                v.get("values"),
                v.get("text"),
                v.get("desc"),
                v.get("prop_value"),
                v.get("attr_value"),
                default="",
            )
            if label and val:
                add(f"{label} {val}")
                return
            if val:
                add(val)
                return
            for nested in v.values():
                walk(nested)
            return
        if isinstance(v, list | tuple | set):
            for item in v:
                walk(item)
            return
        for part in re.split(r"[;\n\r,|/]+", str(v)):
            add(part)

    walk(value)
    return terms[:limit]


def _props_summary(props, *, max_terms=10):
    """Return a single space-joined summary of the flattened prop terms."""
    return " ".join(_flatten_prop_terms(props, limit=max_terms))


def _fallback_seo_title(title_ko, props, category_path):
    """Build a deterministic fallback SEO title.

    Concatenates the Korean title, the leaf category, and a prop summary,
    then sanitises to ``max_len=100``.
    """
    leaf = str(category_path or "").split(">")[-1].strip()
    pieces = [title_ko, leaf, _props_summary(props, max_terms=12)]
    return _sanitize_seo_title(" ".join(p for p in pieces if p), max_len=100) or "item-detail"


__all__ = [
    "BANNED_CLAIM_RE",
    "CATEGORY_PATH_NOTICE_HINTS",
    "DESC_IMAGE_SCAN_LIMIT",
    "DETAIL_ASPECT_TALL",
    "DETAIL_CONTENT_TARGET",
    "DETAIL_GARBAGE_TEXT_RE",
    "DETAIL_HERO_IMAGE_COUNT",
    "DETAIL_IMAGES_MAX",
    "DETAIL_IMAGES_MIN",
    "DETAIL_INFOGRAPHIC_TEXT_RE",
    "DETAIL_MERGE_CELL",
    "DETAIL_MERGE_COLUMNS",
    "DETAIL_MERGE_ROWS",
    "DETAIL_RENDER_CAPTURE_SCALE",
    "DETAIL_RENDER_FINAL_JPEG_QUALITY",
    "DETAIL_RENDER_SEGMENT_MAX_DEVICE_PX",
    "DETAIL_RENDER_WIDTH",
    "DETAIL_TILE_CONTENT_MAX",
    "DETAIL_TILE_MAX_UPSCALE",
    "DETAIL_TILE_MIN_CONTENT",
    "DETAIL_TILE_SKIP_MIN",
    "EDITORIAL_NOISE_RE",
    "EMPTY_MARKETING_COPY_RE",
    "LISTING_IMAGE_LIMIT",
    "MAIN_IMAGE_LIMIT",
    "OPTION_CARD_TONES",
    "OPTION_CODE_RE",
    "OPTION_GRID_LIMIT",
    "OPTION_LABEL_TEXT_RE",
    "PROPERTY_FIELD_SPLIT_RE",
    "RETOUCH_GRID_MAX_DEFAULT",
    "RETOUCH_GRID_MAX_LIMIT",
    "RETOUCH_GRID_MIN_CONTENT",
    "RETOUCH_GRID_PADDING",
    "RETOUCH_SHEET_MAX_PX",
    "SELLER_NOTICE_HEADING_RE",
    "SELLER_SIZE_TEXT_RE",
    "SENSORY_COPY_NOISE_RE",
    "SEO_STOPWORDS",
    "SEO_TITLE_BANNED_RE",
    "STRONG_GARBAGE_TEXT_RE",
    "_DescTextExtractor",
    "_compact_spaces",
    "_detail_safe_text",
    "_fallback_seo_title",
    "_first_text",
    "_flatten_prop_terms",
    "_hesc",
    "_normalize_desc_text",
    "_props_summary",
    "_sanitize_seo_title",
    "_strip_banned_claims",
    "desc_html_to_text",
]
