# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""상품명↔팩트 모순 게이트 (결정론 규칙, LLM 0·외부 호출 0).

생성 트랙이 번들 팩트를 번역하는 과정에서 오역(예: 손잡이 있는 ↔ 손잡이
없는 디자인) 이 생기면, 그 상태로 등록하면 네이버 검색 SEO 가이드의
"상품정보와 다른 기재" 랭크다운 사유가 된다. 본 모듈은 등록 *앞* 에서
상품명과 팩트를 대조해 모순을 드러낸다. 판정은 데이터 상수(극성 쌍·소재군)
기반의 결정론만 쓴다 — 그 외는 판정하지 않는다(과잉 오탐 금지).

출력 계약::

    {"status": "ok"|"conflict"|"skipped",
     "conflicts": [{"topic","name_says","fact_says","rule"}, ...],
     "reason": "facts 없음"}   # skipped 일 때만
"""

from __future__ import annotations

import re

__all__ = ["check_name_facts"]


# --------------------------------------------------------------------------- #
# 규칙 표 (데이터 상수 — 확장 가능 구조).
# --------------------------------------------------------------------------- #

# 극성 반의어 쌍. 각 쌍은 pos(긍정 극성 토큰) 와 neg(부정 극성 토큰) 을 갖는다.
# 판정: 같은 쌍의 pos 가 이름에, neg 가 팩트 값에 있으면(또는 그 반대면) 모순.
_POLARITY_PAIRS: list[dict[str, list[str]]] = [
    {"pos": ["있음", "있는"], "neg": ["없음", "없는"]},
    {"pos": ["포함"], "neg": ["미포함", "불포함"]},
    {"pos": ["가능"], "neg": ["불가능", "불가"]},
    {"pos": ["부착"], "neg": ["미부착", "부착 안 됨", "부착안됨"]},
    {"pos": ["세트"], "neg": ["단품"]},
    # 한 글자 토큰(유/무) 은 단어 경계 일치로만 판정한다(부분일치 오탐 방지).
    {"pos": ["유"], "neg": ["무"], "boundary": True},
]

# 소재 동의어군. 같은 군(도자기≒세라믹) 은 통과, 다른 군이면 모순.
# 군 배열은 확장 가능하다. 오탐을 줄이기 위해 2글자 이상 확실한 표현만 담는다.
MATERIAL_GROUPS: list[list[str]] = [
    ["세라믹", "도자기", " ceramic", "ceramic"],  # 동의어 시드 포함
    ["유리", "글라스", "glass"],
    ["스텐", "스테인리스", "스테인레스", "stainless"],
    ["플라스틱", "수지", "plastic"],
    ["실리콘", "silicone"],
    ["우드", "원목", "나무", "목재", "wood"],
    ["법랑", "에나멜", "enamel"],
    ["알루미늄", "aluminum"],
    ["금속", "스틸", "metal"],
]

# 소재 팩트로 인정하는 팩트명(부분일치).
_MATERIAL_FACT_NAMES = ("소재", "재질", "material")

# 주제어 후보에서 제외할 일반 명사/조사성 토큰(극성 토큰도 제외).
_TOPIC_STOPWORDS = {
    "디자인",
    "여부",
    "포함",
    "미포함",
    "재질",
    "소재",
    "종류",
    "구성",
    "상품",
    "제품",
}

_TOKEN_SPLIT_RE = re.compile(r"[^가-힣A-Za-z0-9]+")


def _tokens(text: str) -> list[str]:
    """문자열을 토큰으로 쪼갠다(구두점·공백 기준)."""
    return [t for t in _TOKEN_SPLIT_RE.split(str(text or "")) if t]


_ALL_POLARITY_WORDS: frozenset[str] = frozenset(
    w for pair in _POLARITY_PAIRS for w in pair["pos"] + pair["neg"]
)


def _contains(text: str, needle: str, *, boundary: bool = False) -> bool:
    """text 안에 needle 이 있는가.

    boundary=True(한 글자 토큰) 일 때는 독립 토큰 일치만 인정한다 —
    "무광"/"유리" 같은 합성어를 유/무 극성으로 오탐하는 것을 막는다.
    """
    if not needle:
        return False
    if boundary:
        return any(t == needle for t in _tokens(text))
    return needle in text


def _polarity_hits(
    text: str,
    own: list[str],
    opposite: list[str],
    *,
    boundary: bool = False,
) -> list[str]:
    """text 에서 해당 극성(own) 토큰들을 찾는다.

    반대 극성(opposite) 단어 안에 포개진 출현은 세지 않는다 — "미포함" 을
    "포함" 으로, "불가능" 을 "가능" 으로 오탐하는 것을 막는다.
    boundary(한 글자 토큰) 는 독립 토큰 일치만 인정한다.
    """
    _text = str(text or "")
    if boundary:
        toks = set(_tokens(_text))
        return [w for w in own if w in toks]
    opp_spans: list[tuple[int, int]] = []
    for w in opposite:
        start = 0
        while True:
            idx = _text.find(w, start)
            if idx < 0:
                break
            opp_spans.append((idx, idx + len(w)))
            start = idx + 1
    hits: list[str] = []
    for w in own:
        start = 0
        while True:
            idx = _text.find(w, start)
            if idx < 0:
                break
            span = (idx, idx + len(w))
            # 반대 극성 단어 *안에 포개진* 출현만 제외한다("포함"←"미포함").
            # 반대로 내가 더 긴 단어("미포함") 는 그대로 유효하다.
            overlaps = any(
                o0 <= span[0] and span[1] <= o1 and (o1 - o0) > (span[1] - span[0])
                for o0, o1 in opp_spans
            )
            if not overlaps:
                hits.append(w)
                break
            start = idx + 1
    return hits


def _material_group_of(text: str) -> tuple[int, str] | None:
    """text 에 언급된 소재군(인덱스, 소재어) 을 반환. 미발견 시 None."""
    for idx, group in enumerate(MATERIAL_GROUPS):
        for word in group:
            if word.strip() and word.strip() in text:
                return idx, word.strip()
    return None


def _normalize_facts(facts) -> list[dict[str, str]]:
    """facts 입력을 [{"name","value"}] 로 정규화.

    생성 트랙 번들의 name_ko/value_ko 형태도 같이 받는다(원산지는 이 모듈
    몫이 아니므로 그대로 보존만 한다).
    """
    out: list[dict[str, str]] = []
    if not isinstance(facts, list):
        return out
    for item in facts:
        if not isinstance(item, dict):
            continue
        name = str(
            item.get("name") if item.get("name") is not None else item.get("name_ko") or ""
        ).strip()
        value = str(
            item.get("value") if item.get("value") is not None else item.get("value_ko") or ""
        ).strip()
        if not name and not value:
            continue
        out.append({"name": name, "value": value})
    return out


def _find_topic(name: str, fact: dict[str, str]) -> str:
    """이름·팩트 양쪽에 공통으로 등장하는 주제어(명사) 를 찾는다.

    팩트명+팩트값의 토큰 중에서 (극성어·불용어 제외) 이름에 부분일치하는
    가장 긴 토큰을 주제어로 삼는다. 없으면 빈 문자열 — 판정하지 않는다.
    """
    candidates: list[str] = []
    for source in (fact["name"], fact["value"]):
        for tok in _tokens(source):
            if len(tok) < 2 or tok in _TOPIC_STOPWORDS or tok in _ALL_POLARITY_WORDS:
                continue
            candidates.append(tok)
    # 긴 토큰 우선.
    for tok in sorted(set(candidates), key=len, reverse=True):
        if tok in name:
            return tok
    return ""


def check_name_facts(name, facts) -> dict:
    """상품명과 팩트를 대조해 모순 여부를 판정한다 (결정론).

    Args:
        name: 상품명 문자열.
        facts: ``[{"name": str, "value": str}, ...]`` (또는 name_ko/value_ko).

    Returns:
        ``{"status": "ok"|"conflict"|"skipped", "conflicts": [...], "reason": ...}``.
        facts 가 없으면 skipped + 사유(조용한 통과 금지).
    """
    _name = str(name or "").strip()
    normalized = _normalize_facts(facts)
    if not normalized:
        return {"status": "skipped", "conflicts": [], "reason": "facts 없음"}

    conflicts: list[dict[str, str]] = []
    for fact in normalized:
        fname, fvalue = fact["name"], fact["value"]
        # --- 극성 반의어: 같은 주제어의 반대 극성이면 모순. ---
        topic = _find_topic(_name, fact)
        if topic:
            for pair in _POLARITY_PAIRS:
                boundary = bool(pair.get("boundary"))
                name_pos = _polarity_hits(_name, pair["pos"], pair["neg"], boundary=boundary)
                name_neg = _polarity_hits(_name, pair["neg"], pair["pos"], boundary=boundary)
                fact_pos = _polarity_hits(fvalue, pair["pos"], pair["neg"], boundary=boundary)
                fact_neg = _polarity_hits(fvalue, pair["neg"], pair["pos"], boundary=boundary)
                name_says = ""
                if name_pos and fact_neg:
                    name_says = name_pos[0]
                elif name_neg and fact_pos:
                    name_says = name_neg[0]
                if name_says:
                    conflicts.append(
                        {
                            "topic": topic,
                            "name_says": name_says,
                            "fact_says": fvalue or fname,
                            "rule": "polarity",
                        }
                    )
                    break
        # --- 소재 모순: 이름 소재군 != 팩트 소재군 이면 모순. ---
        if any(k in fname for k in _MATERIAL_FACT_NAMES):
            name_group = _material_group_of(_name)
            fact_group = _material_group_of(fvalue)
            if name_group is not None and fact_group is not None:
                if name_group[0] != fact_group[0]:
                    conflicts.append(
                        {
                            "topic": "소재",
                            "name_says": name_group[1],
                            "fact_says": fvalue or fname,
                            "rule": "material",
                        }
                    )

    status = "conflict" if conflicts else "ok"
    result: dict = {"status": status, "conflicts": conflicts}
    if status == "conflict":
        result["reason"] = "상품명과 팩트가 서로 다른 사실을 말합니다(오역·사실 오류 가능)."
    return result
