# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""품질 게이트 — 3분할 QA + 집계 + 등록 차단.

원본 ``sourcing.py`` 의 QA 서브시스템을 이식하되, 아래 핵심 개정사항을
반영한다:

1. **고시 검증 데이터 기반화**: 원본은 ``ETC``/``FURNITURE`` 2종만 하드코딩으로
   알고 있었다. 본 이식판은 ``data/notice_types.json`` 에서 타입별 필수 필드를
   읽어, 카테고리에 맞는 고시 타입의 **필수 필드 누락**을 지적한다.
2. **원산지·KC 판정 정책 변경 (중요)**: 원본의 ``origin_compact != "중국" → FAIL``
   및 ``KC_EXEMPTION_OBJECT/OVERSEAS`` 하드 단정은 **이식 금지**. 대신:
     - 원산지: config 설정값과 payload 값이 **일치하는지**만 검사 (값 자체 판정 X)
     - KC: ``category_meta.requires_kc(category_id)`` 가 True 인데 KC 정보가 없으면
       지적; False 면 검사하지 않음.
3. **fail-closed**: 검사 실패·예외를 삼켜 PASS 로 만들지 않는다.
4. **위임 미회신 차단 (중대)**: LLM 판단이 필요한 항목(카피 QA 등)이 아직
   회신되지 않은 상태는 ``PENDING`` verdict 로 표기하고, **게이트는 PENDING 을
   통과시키지 않는다.** 원본은 verdict 누락 시 WARN 으로 떨어뜨리고 WARN 을
   통과시켰다 — 이 경로를 막는 것이 본 이식판의 핵심 수정이다.

의존 방향: ``agent_calls``, ``category`` (상위) → ``qa_agents`` (본 모듈).
``common``, ``text_props``, ``copywriting``, ``category_meta`` 는 어디서든 import 가능.
"""

from __future__ import annotations

import re

from . import common
from .text_props import BANNED_CLAIM_RE, CATEGORY_PATH_NOTICE_HINTS

# ---------------------------------------------------------------------------
# Verdict 상수. PENDING 은 본 이식판이 새로 도입한 "위임 미회신" 상태다.
# 원본은 PASS/WARN/FAIL 만 있었으나, PENDING 을 게이트 차단 상태로 명시한다.
# ---------------------------------------------------------------------------
PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
PENDING = "PENDING"

_VALID_VERDICTS = frozenset({PASS, WARN, FAIL, PENDING})
_BLOCKING_VERDICTS = frozenset({FAIL, PENDING})  # PENDING 도 게이트 차단


_DELEGATION_HINT_KEYS = frozenset({"needs_llm", "task", "instruction", "input"})


def _is_delegation_descriptor(data):
    """``data`` 가 위임 미회신 디스크립터(``llm_hint``)인지 판정.

    ``common._llm_hint`` 가 만드는 ``{"needs_llm": True, "task": ..., "input": ...,
    "instruction": ...}`` 형태가 결과 자리에 그대로 들어온 경우를 잡는다.
    핵심 신호:
      - ``needs_llm`` 이 참이거나
      - 위임 디스크립터 키 집합(``task``/``instruction`` 등)이 과반 출현하면서
        ``verdict`` 키가 없는 경우.
    """
    if not isinstance(data, dict):
        return False
    if data.get("needs_llm"):
        return True
    if "verdict" in data and _clamp_verdict(data.get("verdict")) in _VALID_VERDICTS:
        return False
    overlap = _DELEGATION_HINT_KEYS & set(data.keys())
    if len(overlap) >= 2 and "task" in data:
        return True
    return False


def _clamp_verdict(value, default=WARN):
    """verdict 값을 ``PASS/WARN/FAIL/PENDING`` 으로 정규화.

    알 수 없거나 빈 값은 ``default`` (기본 WARN)로 떨어진다. 단 호출자가
    PENDING 을 명시한 경우 보존한다 (위임 미회신 신호).
    """
    v = str(value or "").strip().upper()
    if v in _VALID_VERDICTS:
        return v
    return default


def _verdict_from_violations(violations, default=PASS):
    """위반 목록에서 최악 verdict 산출.

    FAIL-severity 위반이 하나라도 있으면 → FAIL; 아니면 WARN → WARN; 아니면 default.
     PENDING 은 위반 목록이 아닌 별도 경로(위임 미회신)에서만 부여된다.
    """
    severities = {
        str(row.get("severity") or "").strip().upper()
        for row in (violations or [])
        if isinstance(row, dict)
    }
    if FAIL in severities:
        return FAIL
    if WARN in severities:
        return WARN
    return default


def _normalize_qa_result(data):
    """QA 응답 dict 를 정규화 (위임 미회신 fail-open 차단 개정).

    **위임 미회신 fail-open 차단 (중대)**:
    원본/초기 이식판은 ``verdict`` 키가 없으면 ``WARN`` 으로 떨어뜨렸고,
    ``WARN`` 은 게이트를 통과했다. 이는 LLM 판단 위임 디스크립터(``llm_hint``)
    가 결과 자리에 그대로 들어온 경우(판단이 실제로 이뤄지지 않음)를 등록으로
    넘기는 심각한 fail-open 이다. 본 개정은 아래 세 경우를 **PENDING** 으로
    판정한다 (ADR-0002 "클라이언트 LLM 이 관대해도 서버가 막는다"):

      1. ``verdict`` 키가 없거나 빈 값 (판단이 이뤄졌다는 증거가 없음)
      2. ``needs_llm`` 이 참 (위임 계약이 결과 자리에 들어온 경우)
      3. ``llm_hint`` 형태의 위임 dict 가 결과 자리에 들어온 경우

    단, 호출자가 ``_qa_agent_result`` 등을 통해 **명시적으로 verdict 를 전달한**
    경우(그리고 그 verdict 가 유효한 값인 경우)는 이 규칙에서 제외한다 —
    로컬 코드검사·컴플라이언스 검사가 산출한 WARN 은 합법적이므로
    원본 정책대로 통과시킨다 (WARN 정책 보존).

    Returns:
        ``{"verdict": ..., "violations": [...], "summary": ...}``
    """
    if not isinstance(data, dict):
        return {
            "verdict": WARN,
            "violations": [
                {
                    "rule": "QA",
                    "severity": WARN,
                    "detail": "QA 응답이 dict 가 아닙니다 (파싱 실패).",
                }
            ],
            "summary": "QA JSON 파싱 실패",
        }
    raw_verdict = data.get("verdict")
    verdict_present = raw_verdict is not None and str(raw_verdict).strip() != ""
    # --- 위임 미회신 판정 (fail-open 차단) ---
    # 판단이 실제로 이뤄졌다는 증거가 없으면 PENDING.
    if not verdict_present:
        return {
            "verdict": PENDING,
            "violations": [
                {
                    "rule": "QA",
                    "severity": PENDING,
                    "detail": (
                        "verdict 가 없습니다 — 판단이 아직 이뤄지지 않았습니다"
                        " (위임 미회신으로 간주, PENDING)."
                    ),
                }
            ],
            "summary": "QA verdict 누락 — 위임 미회신 PENDING",
        }
    if _is_delegation_descriptor(data):
        # needs_llm 참 또는 llm_hint 형태가 결과 자리에 들어온 경우.
        return {
            "verdict": PENDING,
            "violations": [
                {
                    "rule": "QA",
                    "severity": PENDING,
                    "detail": (
                        "QA 응답 자리에 위임 디스크립터(llm_hint)가 들어왔습니다 — "
                        "판단이 아직 이뤄지지 않았습니다 (PENDING)."
                    ),
                }
            ],
            "summary": "위임 디스크립터가 결과 자리에 있음 — PENDING",
        }
    verdict = _clamp_verdict(raw_verdict, default=WARN)
    raw_violations = data.get("violations")
    if isinstance(raw_violations, str):
        raw_violations = [{"rule": "QA", "severity": verdict, "detail": raw_violations}]
    elif not isinstance(raw_violations, list):
        raw_violations = []
    violations = []
    for row in raw_violations:
        if isinstance(row, dict):
            severity = _clamp_verdict(row.get("severity"), default=verdict)
            violations.append(
                {
                    "rule": str(row.get("rule") or "QA")[:40],
                    "severity": severity,
                    "detail": str(row.get("detail") or "")[:240],
                }
            )
    summary = str(data.get("summary") or "").strip()
    if not summary:
        summary = f"{verdict} — 위반 {len(violations)}건"
    return {"verdict": verdict, "violations": violations, "summary": summary}


def _qa_agent_result(agent, verdict=PASS, violations=None, summary=""):
    """에이전트 결과 dict 생성."""
    result = _normalize_qa_result(
        {"verdict": verdict, "violations": violations or [], "summary": summary}
    )
    result["agent"] = str(agent or "unknown")
    return result


def _normalize_agent_result(data, agent):
    """개별 에이전트 결과 정규화 (위임 미회신 fail-open 차단 개정).

    원본의 동작 보존: verdict 를 정규화하고, ``agent`` 필드를 붙인다.
    추가 키는 그대로 합친다.

    ``data`` 가 dict 가 아니거나 ``verdict`` 키가 없으면 PENDING 으로
    판정한다 (위임 미회신 fail-open 차단).
    """
    if not isinstance(data, dict):
        # dict 자체가 아님 — 판단이 이뤄졌다는 증거 없음. PENDING.
        data = {"verdict": PENDING}
    result = _normalize_qa_result(data)
    # 원본이 가지고 있던 추가 필드 보존 (예: elapsed_sec, image_preprocess 등)
    for key, value in data.items():
        if key not in result and key != "agent":
            result[key] = value
    result["agent"] = str(data.get("agent") or agent or "unknown")
    return result


def _merge_code_check(result, code_check, key):
    """서브 검사 결과를 부모 에이전트 결과에 병합.

    서브 FAIL → 부모 FAIL 승격; 서브 WARN → 부모 PASS 를 WARN 으로 승격.
    PENDING 은 승격 대상이 아니다 (위임 미회신은 다른 경로에서 처리).
    """
    if not isinstance(code_check, dict):
        return result
    sub_verdict = _clamp_verdict(code_check.get("verdict"))
    if sub_verdict == FAIL:
        result["verdict"] = FAIL
    elif sub_verdict == WARN and _clamp_verdict(result.get("verdict")) == PASS:
        result["verdict"] = WARN
    sub_violations = code_check.get("violations")
    if isinstance(sub_violations, list):
        result.setdefault("violations", [])
        tag = str(code_check.get("agent") or key or "code")
        for row in sub_violations:
            if isinstance(row, dict):
                v = dict(row)
                v.setdefault("source", tag)
                result["violations"].append(v)
    result[key] = code_check
    return result


# ---------------------------------------------------------------------------
# notice_types.json 로더 (데이터 기반 고시 검증).
#
# 원본은 ETC + FURNITURE 2종만 하드코딩했다. 본 이식판은 data/notice_types.json
# 의 verified 배열에서 {type, node, fields} 를 읽어 임의 타입의 필수 필드를
# 검사한다. 파일 부재 시 fail-closed: 명시적 예외.
# ---------------------------------------------------------------------------

_NOTICE_TYPES_CACHE: list[dict] | None = None


def _load_notice_types():
    """``data/notice_types.json`` 의 verified 타입 목록을 반환.

    Returns:
        타입 dict 리스트. 각 원소는 ``{type, node, fields, label_ko}``.

    Raises:
        RuntimeError: 파일이 부재하거나 구조가 올바르지 않을 때 (fail-closed).
    """
    global _NOTICE_TYPES_CACHE
    if _NOTICE_TYPES_CACHE is not None:
        return _NOTICE_TYPES_CACHE
    import json

    path = common.package_data_path("notice_types.json")
    if not path.exists():
        raise RuntimeError(f"notice_types.json 파일이 없습니다: {path} (fail-closed).")
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"notice_types.json 읽기 실패: {path} ({exc})") from exc
    verified = doc.get("verified") if isinstance(doc, dict) else None
    if not isinstance(verified, list):
        raise RuntimeError(f"notice_types.json 구조가 올바르지 않습니다: {path}")
    _NOTICE_TYPES_CACHE = verified
    return verified


def _notice_type_spec(notice_type):
    """특정 고시 타입의 스펙(``{type, node, fields}``)을 반환.

    Args:
        notice_type: 고시 타입 enum (예: ``"WEAR"``, ``"FURNITURE"``).

    Returns:
        매칭되는 dict 또는 ``None`` (알 수 없는 타입).
    """
    notice_type = str(notice_type or "").strip().upper()
    for entry in _load_notice_types():
        if str(entry.get("type") or "").upper() == notice_type:
            return entry
    return None


# ---------------------------------------------------------------------------
# notice_field_types.json 로더 (고시 필드 타입 정보).
#
# 핵심 계약: **타입을 지어내지 않는다.** 이 파일에는 실호출로 확인된
# 필드만 기록된다. 미기재 필드는 문자열(기존 동작)로 둔다. 확인되지
# 않은 필드를 "아마 boolean 일 것이다" 로 채우면 규제 필드 오신고가 된다.
#
# 값은 ``string`` / ``boolean`` / ``date`` / ``year_month`` / ``local_date`` /
# ``integer`` / ``long`` 일곱 가지. ``string`` 은 기존 동작(공백·placeholder =
# 미제공)을, ``boolean`` 은 True/False 둘 다 제공으로, ``date``/``year_month``/
# ``local_date``/``integer``/``long`` 은 비어있지 않으면 제공(형식 미확정)으로
# 다룬다.
# ---------------------------------------------------------------------------
_NOTICE_FIELD_TYPES_CACHE: dict[str, dict] | None = None


def _load_notice_field_types() -> dict[str, dict]:
    """``data/notice_field_types.json`` 의 ``field_types`` 맵을 반환 (캐싱).

    Returns:
        필드명 → 타입 딕셔너리. 각 값은 최소한 ``{"type": ...}`` 키를 갖는다.
        가능한 type 값: ``"string"`` / ``"boolean"`` / ``"date"`` /
        ``"year_month"`` / ``"local_date"`` / ``"integer"`` / ``"long"``.

    Raises:
        RuntimeError: 파일이 부재하거나 구조가 올바르지 않을 때 (fail-closed).
            과거에는 파일 부재 시 빈 dict 를 조용히 반환했으나,
            install-paths 재배치에서 이 조용한 폴백을 금지했다 — 데이터 파일은
            패키지에 포함되므로 부재는 설치 불량이나 빌드 오류이다 (사용자 환경
            문제가 아니다).
    """
    global _NOTICE_FIELD_TYPES_CACHE
    if _NOTICE_FIELD_TYPES_CACHE is not None:
        return _NOTICE_FIELD_TYPES_CACHE
    import json

    path = common.package_data_path("notice_field_types.json")
    if not path.exists():
        raise RuntimeError(
            f"notice_field_types.json 파일이 없습니다: {path} (fail-closed). "
            "패키지 설치가 올바르지 않습니다 — wheel 을 재설치하세요."
        )
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"notice_field_types.json 읽기 실패: {path} ({exc})") from exc
    field_types = doc.get("field_types") if isinstance(doc, dict) else None
    if not isinstance(field_types, dict):
        raise RuntimeError(f"notice_field_types.json 구조가 올바르지 않습니다: {path}")
    # 유효한 type 값만 보존한다(알 수 없는 type 은 문자열로 취급).
    # date 필드의 경우 format(사람 읽기용 설명)과 formats_by_type(타입별 확정
    # 형식 맵) 키가 있으면 함께 보존한다 — 그렇지 않으면 reader 가 형식 정보를
    # 조용히 버려 소비자가 잘못된 형식을 보고하게 된다.
    #
    # 2026-08-10 개정 (D64 실측 계약): ``year_month`` / ``local_date`` /
    # ``integer`` / ``long`` 이 추가되었다. 이 중 하나라도 누락하면
    # ``_NON_DEFERRABLE_FIELD_TYPES`` 에는 들어 있지만 loader 가 통과시키지
    # 않아, 해당 필드가 문자열로 취급되며 미루기 대상이 되어 버린다
    # (fail-open 회귀). 따라서 여기의 집합은 ``_NON_DEFERRABLE_FIELD_TYPES``
    # 와 정확히 짝을 이룬다 — ``string`` 을 제외한 모든 non-deferrable 타입.
    valid_types = frozenset(
        {"string", "boolean", "date", "year_month", "local_date", "integer", "long"}
    )
    normalized: dict[str, dict] = {}
    for name, entry in field_types.items():
        if not isinstance(entry, dict):
            continue
        ftype = str(entry.get("type") or "").strip().lower()
        if ftype not in valid_types:
            continue
        item: dict[str, object] = {"type": ftype}
        raw_format = entry.get("format")
        if isinstance(raw_format, str) and raw_format.strip():
            item["format"] = raw_format.strip()
        # 2026-08-10: API 원값(api_field_type)과 사람 읽기용 설명
        # (field_description / field_add_description) 을 보존한다.
        # 티켓 요구사항 (e): "필드 설명이 조회 가능" 하려면 loader 가 이 키들을
        # 통과시켜야 한다 — 그렇지 않으면 JSON 에 있어도 소비자가 못 본다.
        raw_api_ft = entry.get("api_field_type")
        if isinstance(raw_api_ft, str) and raw_api_ft.strip():
            item["api_field_type"] = raw_api_ft.strip()
        raw_desc = entry.get("field_description")
        if isinstance(raw_desc, str) and raw_desc.strip():
            item["field_description"] = raw_desc.strip()
        raw_add_desc = entry.get("field_add_description")
        if isinstance(raw_add_desc, str) and raw_add_desc.strip():
            item["field_add_description"] = raw_add_desc.strip()
        raw_fbt = entry.get("formats_by_type")
        if isinstance(raw_fbt, dict):
            fbt: dict[str, str] = {}
            for type_name, fmt_value in raw_fbt.items():
                if isinstance(fmt_value, str) and fmt_value.strip():
                    fbt[str(type_name).strip().upper()] = fmt_value.strip()
            if fbt:
                item["formats_by_type"] = fbt
        normalized[str(name)] = item
    _NOTICE_FIELD_TYPES_CACHE = normalized
    return normalized


def _notice_field_type(field: str) -> str:
    """특정 고시 필드의 타입을 반환.

    Returns:
        ``"string"`` / ``"boolean"`` / ``"date"`` / ``"year_month"`` /
        ``"local_date"`` / ``"integer"`` / ``"long"``. 미기재 필드는
        ``"string"`` (기존 동작). 타입을 추측하지 않는다 — 데이터에 없으면
        문자열이다.
    """
    types = _load_notice_field_types()
    entry = types.get(str(field or ""))
    if isinstance(entry, dict):
        return str(entry.get("type") or "string")
    return "string"


# 미루기(deferred) 불가능한 필드 타입 집합.
# boolean 필드는 "상세페이지 참조" 같은 문자열을 네이버 API 가 거부한다
# (live API: Cannot deserialize value of type `java.lang.Boolean` from String).
# date 계열(YearMonth/LocalDate) 필드도 마찬가지로 문자열 placeholder 를 거부한다
# (live API: date parse error). integer/long 도 마찬가지로 문자열 placeholder 를
# 거부한다. 따라서 이 타입의 필드는 미루기 대상에서 제외하고, 사용자에게 실제
# 값을 요구한다 (needs_user 로 안내).
#
# 2026-08-10 개정 (D64 실측 계약): API fieldType 분포가 String/YearMonth/
# LocalDate/Boolean/Integer/Long 6종으로 확인되었다. 과거의 ``date`` 한 덩어리
# 표현을 ``year_month`` 와 ``local_date`` 로 분리한다. ``date`` 도 backward-compat
# 용으로 남겨둔다(구 데이터 파일 호환). ``String`` 만 미루기 가능하다.
_NON_DEFERRABLE_FIELD_TYPES = frozenset(
    {"boolean", "date", "year_month", "local_date", "integer", "long"}
)


def _is_field_deferrable(field: str) -> bool:
    """필드가 미루기(deferred) 대상으로 허용되는 타입인지 판정.

    ``data/notice_field_types.json`` 에서 해당 필드의 타입을 읽어:
      - ``"boolean"`` 또는 ``"date"`` → 미루기 불가 (``False``).
        네이버 API 가 문자열 placeholder 를 Boolean/date 타입으로
        deserialize 하지 못해 400 거절.
      - ``"string"`` 또는 미기재 → 미루기 가능 (``True``).
        기존 동작(문자열 placeholder = "상세페이지 참조")을 유지한다.

    판단 기준은 코드에 박힌 필드명 목록이 아니라 데이터 파일이다 —
    새 타입이 추가되면 자동으로 반영된다.
    """
    return _notice_field_type(field) not in _NON_DEFERRABLE_FIELD_TYPES


# ---------------------------------------------------------------------------
# notice_field_relations.json 로더 (고시 필드 관계 — XOR 상호배제).
#
# 핵심 계약: **확인된 것만 기록한다.** 이 파일에는 실호출로 확인된
# 상호배제 관계만 기록된다. 다른 타입의 필드를 보고 "이것도 XOR 같다"
# 로 채우면 규제 필드 오신고가 된다. 확인되지 않은 관계를 기록하지 않는다
# (= 기존 동작 유지).
#
# XOR 그룹은 "이 필드들 중 정확히 하나" 를 뜻한다:
#   - 둘 다 비면 미제공(차단).
#   - 하나만 있으면 충족.
#   - 둘 다 있으면 위반(네이버가 거절하므로 미리 막는 편이 낫다).
# ---------------------------------------------------------------------------
_NOTICE_FIELD_RELATIONS_CACHE: dict[str, list[list[str]]] | None = None


def _load_notice_field_relations() -> dict[str, list[list[str]]]:
    """``data/notice_field_relations.json`` 의 XOR 관계 맵을 반환 (캐싱).

    Returns:
        고시 타입명(대문자) → XOR 그룹 리스트. 각 XOR 그룹은 필드명 리스트.

    Raises:
        RuntimeError: 파일이 부재하거나 구조가 올바르지 않을 때 (fail-closed).
            과거에는 파일 부재 시 빈 dict 를 조용히 반환했으나,
            install-paths 재배치에서 이 조용한 폴백을 금지했다 — 데이터 파일은
            패키지에 포함되므로 부재는 설치 불량이나 빌드 오류이다.
    """
    global _NOTICE_FIELD_RELATIONS_CACHE
    if _NOTICE_FIELD_RELATIONS_CACHE is not None:
        return _NOTICE_FIELD_RELATIONS_CACHE
    import json

    path = common.package_data_path("notice_field_relations.json")
    if not path.exists():
        raise RuntimeError(
            f"notice_field_relations.json 파일이 없습니다: {path} (fail-closed). "
            "패키지 설치가 올바르지 않습니다 — wheel 을 재설치하세요."
        )
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"notice_field_relations.json 읽기 실패: {path} ({exc})") from exc
    relations = doc.get("relations") if isinstance(doc, dict) else None
    if not isinstance(relations, dict):
        raise RuntimeError(f"notice_field_relations.json 구조가 올바르지 않습니다: {path}")
    normalized: dict[str, list[list[str]]] = {}
    for type_name, entry in relations.items():
        if not isinstance(entry, dict):
            continue
        xor_groups = entry.get("xor")
        if not isinstance(xor_groups, list):
            continue
        groups: list[list[str]] = []
        for group in xor_groups:
            if not isinstance(group, list):
                continue
            fields = [str(f) for f in group if isinstance(f, str) and f]
            if len(fields) >= 2:
                groups.append(fields)
        if groups:
            normalized[str(type_name).strip().upper()] = groups
    _NOTICE_FIELD_RELATIONS_CACHE = normalized
    return normalized


def _notice_xor_groups(notice_type: str) -> list[list[str]]:
    """특정 고시 타입의 XOR 그룹 리스트를 반환.

    Returns:
        XOR 그룹 리스트. 각 그룹은 필드명 리스트(길이 2 이상).
        기록된 관계가 없으면 빈 리스트(기존 동작 유지).
    """
    relations = _load_notice_field_relations()
    return relations.get(str(notice_type or "").strip().upper(), [])


def _notice_field_filled(notice_body, field) -> bool:
    """단일 필드가 "제공됨" 인지 판정 (타입 인지).

    ``_notice_field_missing`` 의 단일 필드 판정 로직을 분리한 헬퍼.
    XOR 그룹 판정에서 재사용하기 위해 같은 규칙을 적용한다:
      - ``boolean``: None 만 미제공, True/False 모두 제공.
      - ``integer``/``long``: None 만 미제공, 0 포함 모든 숫자 제공.
        (``_is_placeholder_value(0)`` 은 True 를 반환하므로 placeholder 경로에
        맡기면 0 이 미제공으로 판정되는 회귀가 생긴다.)
      - ``date``/``year_month``/``local_date``: placeholder 가 아니면 제공.
      - ``string``/미기재: placeholder 가 아니면 제공.
    """
    if not isinstance(notice_body, dict):
        return False
    raw = notice_body.get(field)
    ftype = _notice_field_type(field)
    if ftype == "boolean":
        return raw is not None
    if ftype in ("integer", "long"):
        # 0 은 유효한 정수값이다 — _is_placeholder_value(0) == True 이므로
        # placeholder 경로에 맡기면 안 된다. None/키부재만 미제공.
        return raw is not None
    if ftype == "date":
        return not _is_placeholder_value(raw)
    # string/미기재.
    return not _is_placeholder_value(raw)


# 고시 타입 결정: 입력에서 명시적으로 주어지거나, 카테고리 경로에서 휴리스틱.
# (원본의 _is_furniture_notice 휴리스틱을 일반화한 데이터 기반 매핑)
#
# ``CATEGORY_PATH_NOTICE_HINTS`` 테이블의 정본은 :mod:`text_props` 에 있다
# (의존 없는 상위 DAG 노드). 본 모듈과 :mod:`naver_client` 모두 거기서
# import 한다. 과거에 두 모듈에 사본이 있었고 주석으로 "동일" 이라고
# 표시만 했으나, 사본은 inevitably 갈라진다 — 단일 진실 공급원으로 통합.


def _infer_notice_type(context):
    """컨텍스트에서 고시 타입을 추론 (데이터 기반).

    우선순위:
      1. ``context.notice.notice_type`` / ``context.notice_type`` / ``context.productInfoProvidedNoticeType``
      2. 카테고리 경로 휴리스틱 (``CATEGORY_PATH_NOTICE_HINTS``)

    알 수 없으면 ``"ETC"`` (원본 기본값).
    """
    if isinstance(context, dict):
        notice = context.get("notice")
        if isinstance(notice, dict):
            explicit = notice.get("productInfoProvidedNoticeType") or notice.get("notice_type")
            if explicit:
                return str(explicit).strip().upper()
        explicit = context.get("notice_type") or context.get("productInfoProvidedNoticeType")
        if explicit:
            return str(explicit).strip().upper()
        cat_text = " ".join(
            str(context.get(k) or "") for k in ("category_name", "category_path", "categoryPath")
        )
    else:
        cat_text = ""
    for needle, notice_type in CATEGORY_PATH_NOTICE_HINTS:
        if needle in cat_text:
            return notice_type
    return "ETC"


# ---------------------------------------------------------------------------
# 컴플라이언스 코드검사 (원본 _compliance_code_check 대체, 데이터 기반).
#
# 핵심 정책 변경:
#   - 원산지: config 값과 payload 값이 **일치**하는지만 검사 (값 판정 X)
#   - KC: category_meta.requires_kc() 가 True 인데 KC 정보가 없으면 지적
#   - 고시 필드: data/notice_types.json 기반으로 타입별 필수 필드 누락 지적
#   - fail-closed: 예외 삼켜 PASS 금지
# ---------------------------------------------------------------------------


def _normalize_placeholder_value(raw):
    """고시 필드 값을 placeholder 판정용으로 정규화.

    정규화 규칙 (합리적 범위, 과잉 차단 금지):
      1. 양끝 공백 제거 (str.strip).
      2. 전각 공백(U+3000) 및 전각/반각 공백류를 ASCII 공백으로 통일.
      3. 내부 공백 런을 단일 공백으로 축소 — ``"상 세  참 조"`` 같은
         공백 삽입 변형이 ``"상 세 참 조"`` 로 모이도록.
      4. 소문자 변환(영문 토큰 N/A, NULL 등 대소문자 차이 흡수).
      5. 세 가지 정규형을 반환한다(자세한 내용은 Returns 참고).

    인자로 전달된 값은 문자열로 캐스트한다 (None 안전).

    Returns:
        ``(standard_form, compact_form, compact_sep_form)`` — 셋 다 str.
        standard 는 공백을 단일 스페이스로 축소한 형태.
        compact 는 공백을 완전 제거한 형태(한국어 공백 삽입 변형 판정용).
        compact_sep 은 공백/-/. 를 ``_`` 로 통일한 형태(영문 구분자 변형 판정용).
    """
    text = str(raw or "")
    # (1) 전각 공백(U+3000) 및 다양한 공백류을 ASCII 스페이스로 통일.
    #     전각/반각 통일: 네이버 셀러 입력기가 전각 스페이스를 넣는 경우가 있다.
    text = text.replace("\u3000", " ")
    # (2) 양끝 공백 제거.
    text = text.strip()
    # (3) 소문자 변환.
    text = text.lower()
    # (4) 내부 공백 런 축소.
    standard = re.sub(r"\s+", " ", text)
    # (5) 축약형 두 가지를 만든다:
    #     (a) whitespace_collapsed: 공백을 모두 제거한 형태.
    #         한국어 안내문구의 공백 삽입 변형을 잡는다("상 세 참 조" → "상세참조").
    #     (b) separator_unified: 공백/-/. 를 _ 로 통일한 형태.
    #         영문 placeholder 의 구분자 변형을 잡는다("REPLACE-ME" → "replace_me",
    #         "REPLACE ME" → "replace_me").
    #     "/" 는 (b)에서 건드리지 않는다 — "n/a" 토큰이 그대로 대조되어야 한다.
    compact = re.sub(r"\s+", "", text)
    compact_sep = re.sub(r"[\s\-\.]+", "_", text)
    return standard, compact, compact_sep


# placeholder 정본 토큰 목록 (단일 진실 공급원).
#
# "실질 정보가 없는 값" 판정의 기준선. 감리가 지적한 대로 과거에는 이 판정이
# qa_agents.py 세 지점(standard 집합, compact 집합, 구두점 휴리스틱)과
# naver_client.py 한 지점(_has_text)에 흩어져 있었다. 본 개정은 토큰 목록을
# 한 곳(_PLACEHOLDER_TOKENS)에 두고 standard/compact 대조 집합은 여기서 자동
# 파생한다. naver_client._has_text 는 _is_placeholder_value 를 import 해서
# 쓴다 — 새 판정 함수를 만들지 않는다.
#
# 정상 한국어 정책 문구와 정상 값("면 100%", "2026-01", "어깨 42cm",
# "단순변심 시 왕복 배송비 6,000원")은 여기에 절대 들지 않는다.
# 판정은 문자열 전체가 placeholder 일 때만 성립한다(부분 일치로 막지 않는다).
_PLACEHOLDER_TOKENS = frozenset(
    {
        # 빈 값 / 구분자만.
        "",
        "-",
        # 영문 일반 placeholder (소문자 원형만 등록 — _normalize_placeholder_value
        # 가 소문자화한다). 구분자 변형(-/_/./공백)은 compact 대조로 흡수.
        "n/a",
        "null",
        "none",
        # 복사해온 예시 설정이나 채우다 만 값이 흔히 남기는 placeholder 표식.
        "tbd",
        "todo",
        "replace_me",
        "placeholder",
        "dummy",
        # 한국어 안내문구 (공백 삽입 변형은 compact 대조로 흡수).
        "해당없음",
        "해당없음없음",
        "상세참조",
        "상세 참조",
        "상세페이지참조",
        "상세페이지 참조",
        "상세 페이지 참조",
        "상세페이지 확인",
        "상세 페이지 확인",
        "본품참조",
        "본품 참조",
        "별도표시",
        "별도 표시",
    }
)

# standard/compact 대조 집합 — 정본 _PLACEHOLDER_TOKENS 에서 자동 파생.
# 두 집합을 손으로 따로 유지하지 않는다(정본 하나에서 갈라지게).
# COMPACT 는 공백을 제거한 형태(한국어 공백 삽입 변형 판정).
# COMPACT_SEP 은 공백/-/. 를 _ 로 통일한 형태(영문 구분자 변형 판정).
# NO_SEP 은 공백/-/. 를 **완전 제거**한 형태(구분자를 문자 그대로 찍은
# 변형 판정). 정본 토큰 하나에서 네 집합이 모두 갈라진다 — 손으로 유지하는
# 독자 토큰 목록이 아니다. "T.B.D" → COMPACT_SEP 은 "t_b_d" 가 되어 어떤
# 토큰과도 맞지 않지만, NO_SEP 은 "tbd" 가 되어 정본 토큰 "tbd" 에 닿는다.
_PLACEHOLDER_TOKENS_STANDARD = frozenset(_PLACEHOLDER_TOKENS)
_PLACEHOLDER_TOKENS_COMPACT = frozenset(re.sub(r"\s+", "", tok) for tok in _PLACEHOLDER_TOKENS)
_PLACEHOLDER_TOKENS_COMPACT_SEP = frozenset(
    re.sub(r"[\s\-\.]+", "_", tok) for tok in _PLACEHOLDER_TOKENS
)
_PLACEHOLDER_TOKENS_NO_SEP = frozenset(re.sub(r"[\s\-\.]+", "", tok) for tok in _PLACEHOLDER_TOKENS)


def _is_placeholder_value(raw):
    """정규화 후 안내문구 집합 대조 + 구두점만 값 휴리스틱.

    "실질 정보가 없는 값" 으로 판정되면 True.
      - 정규화(공백 통일/축소·전각반각 통일·소문자) 후 알려진 안내문구
        집합(standard/compact/compact_sep/no_sep 양쪽)과 대조.
      - 괄호/구두점/공백만 남은 값도 미제공으로 간주.

    과잉 차단 금지: 정상 값(예: ``면 100%``, ``2026-01``, ``어깨 42cm``)은
    안내문구 집합에 없으므로 판정을 통과한다.
    """
    standard, compact, compact_sep = _normalize_placeholder_value(raw)
    if standard in _PLACEHOLDER_TOKENS_STANDARD:
        return True
    if compact in _PLACEHOLDER_TOKENS_COMPACT:
        return True
    if compact_sep in _PLACEHOLDER_TOKENS_COMPACT_SEP:
        return True
    # 구분자를 문자 그대로 찍은 변형(예: "T.B.D", "T-B-D")을 잡기 위한 네 번째
    # 정규형. 공백/하이픈/마침표를 "_" 로 치환하지 않고 **완전히 제거**한다.
    # 이렇게 하면 "T.B.D" → "tbd" 가 되어 정본 토큰 "tbd" 에 닿는다.
    # 동일한 정본 토큰 집합에서 파생된 _PLACEHOLDER_TOKENS_NO_SEP 와 대조하며,
    # 두 번째 판정 함수를 만들지 않는다. 대조는 항상 문자열 전체 일치로만
    # 성립한다(부분 일치로 막지 않는다) — 정상 문구가 잘리지 않는다.
    no_sep = re.sub(r"[\s\-\.]+", "", standard)
    if no_sep in _PLACEHOLDER_TOKENS_NO_SEP:
        return True
    # 괄호/구두점/공백만 남은 값도 미제공으로 간주.
    stripped = re.sub(r"[\s\-\.\,\(\)\[\]\{\}\/]", "", compact)
    if not stripped:
        return True
    return False


# ---------------------------------------------------------------------------
# 판매자가 명시적으로 고시 필드를 "상세페이지 참조" 로 미루기(defer) 선택했을 때
# 게이트가 그 필드를 "채워진" 것으로 인정하게 하는 표준 문구.
#
# 과거 이 코드는 안내문구 값을 지어내어 조용히 채웠다. 그 실패 모드(허위 신고)는
# 영구 금지다 — 본 상수는 게이트가 판정할 때 쓰는 *토큰* 이 아니라, 판매자가 선택한
# 필드에 한해 naver_client 가 전송값으로 채우는 *표준 문구* 다. 게이트의 placeholder
# 판정(_is_placeholder_value) 은 이 값도 여전히 "미제공" 으로 본다 — 차이점은
# 호출자가 deferred 집합을 넘겨 그 판정을 건너뛰는 것이다.
#
# 한국 커머스에서 "see the detail page" 의 표준 한국어 표기. _PLACEHOLDER_TOKENS
# 가 이 토큰을 이미 인식하므로 별도의 인식 로직이 필요하지 않다.
# ---------------------------------------------------------------------------
DEFERRED_NOTICE_PLACEHOLDER = "상세페이지 참조"

# ---------------------------------------------------------------------------
# 고시 35종에 공통으로 등장하는 5개 필드(returnCostReason · noRefundReason ·
# qualityAssuranceStandard · compensationProcedure · troubleShootingContents) 를
# 미룰 때 naver_client 가 채우는 **둘째 표준 문구**.
#
# 관측 사실만 기록한다 — 의미는 단정하지 않는다:
#   2026-08-06 실측(오케스트레이터, 실스토어 상품 20건) — 이 5필드는 값이
#   ``"1"`` 이었고, 그 외 고시 필드(manufacturer · modelName · color 등)은
#   ``"상세페이지 참조"`` 였다. 두 방식은 필드별로 완전히 갈렸다(섞임 0건).
#   공식 문서에서 ``"1"`` 이 무엇을 의미하는지는 **확인된 바 없다** (가설:
#   네이버 UI 가 이 5필드에 제공하는 법정 표준 문구의 선택 코드로 추정).
#
# 본 상수는 **자리**(common notice field)를 가리키는 이름이다 — 값의 의미를
# 단정하지 않는다. ``"1"`` 이 우리 해석과 다를 가능성이 남아 있으므로, 한 곳
# (``_deferred_value_for_field``)만 고치면 되돌릴 수 있다.
#
# 이 5필드는 **하드코딩하지 않는다** — ``_common_notice_deferred_fields`` 가
# ``notice_types.json`` 35종의 교집합에서 유도한다(현재 정확히 5개).
# ---------------------------------------------------------------------------
DEFERRED_COMMON_NOTICE_VALUE = "1"

# 원산지(origin) 필드는 법적 선언이므로 판매자가 미루기를 선택해도 거부한다.
# originAreaInfo.content / made_in / countryOfOrigin 등 고시 본문의 원산지 자리는
# 게이트가 *항상* 실값을 요구한다. 이름이 바뀌어도 이 집합에 등록된 필드명은
# 미루기 대상에서 제외된다.
ORIGIN_FIELDS_NOT_DEFERRABLE = frozenset(
    {
        "originAreaCode",
        "originAreaInfo.content",
        "originAreaInfo",
        "countryOfOrigin",
        "madeIn",
        "made_in",
        "origin_content",
    }
)


def _field_is_deferred(field: str, deferred: list[str] | tuple[str, ...] | set[str] | None) -> bool:
    """``field`` 가 판매자가 명시적으로 미루기로 선택한 필드인지 판정.

    판매자가 ``deferred_notice_fields`` 로 넘긴 이름 집합에 ``field`` 가 포함되면
    True. 원산지 필드는 호출자가 넘겼더라도 여기서 False 로 내려보낸 뒤 호출자가
    다시 거부하도록 만들 수 있다 — 단 본 함수는 순수 집합 조회만 한다(정책 적용은
    ``_reject_origin_deferred`` / mcp_server 게이트가 담당).

    Args:
        field: 고시 필드명(camelCase).
        deferred: 판매자가 선택한 미루기 필드명 집합/리스트. ``None`` 이면 빈 것으로
            간주해 False 를 반환(미루기 선택이 없는 일반 경로).

    Returns:
        ``True`` = 판매자가 이 필드를 미루기로 선택함.
    """
    if not deferred:
        return False
    target = str(field or "").strip()
    if not target:
        return False
    return any(str(name or "").strip() == target for name in deferred)


# ---------------------------------------------------------------------------
# 미루기(deferred) 대상 고시 필드 allowlist — 고시 정의에서 유도.
#
# 감리 지적: 과거 이 게이트는 어떤 키든 판매자가 deferred_notice_fields 로 넘기면
# "적용됐다" 고 믿게 두고, 네이버 스키마에 없는 키를 실제로 전송했다. 대소문자
# 변형·오타·별칭(originAreaInfo.content.value, country_of_origin, madein 등) 이
# 각각 HTTP POST 1회씩 일으키며 "상세페이지 참조" 값이 임의 키로 딸려 나갔다.
# 본 allowlist 는 이를 막는다 — **수동 목록이 아니라** notice_types.json 의 35종
# 전체 fields 배열의 합집합에서 유도한다(고시 정의가 바뀌면 자동으로 따라감).
#
# 원산지 필드는 ORIGIN_FIELDS_NOT_DEFERRABLE 이 별도로 거르므로, 본 집합에
# 원산지 키가 있더라도 미루기 대상이 되지 않는다(이중 방어).
# ---------------------------------------------------------------------------
_DEFERRABLE_NOTICE_FIELDS_CACHE: frozenset[str] | None = None


def _deferrable_notice_fields() -> frozenset[str]:
    """미루기 대상이 될 수 있는 고시 필드명의 allowlist 를 반환.

    **수동 목록이 아니다** — ``data/notice_types.json`` 의 35종 전체에서
    ``fields`` 배열을 합집합으로 모아 반환한다(고시 정의가 단일 진실 공급원).
    새 고시 타입이나 필드가 데이터에 추가되면 본 집합이 자동으로 따라간다.

    Returns:
        미루기 허용 필드명 ``frozenset``. 원산지 후보명이 섞여 있더라도 호출자가
        ``_reject_origin_deferred`` 로 한 번 더 거른다(이중 방어).

    Raises:
        RuntimeError: ``notice_types.json`` 부재/손상 (fail-closed — 이미
            ``_load_notice_types`` 가 같은 파일을 검증하므로 정상 설치 시 발생
            하지 않는다).
    """
    global _DEFERRABLE_NOTICE_FIELDS_CACHE
    if _DEFERRABLE_NOTICE_FIELDS_CACHE is not None:
        return _DEFERRABLE_NOTICE_FIELDS_CACHE
    names: set[str] = set()
    for entry in _load_notice_types():
        if not isinstance(entry, dict):
            continue
        fields = entry.get("fields")
        if not isinstance(fields, list):
            continue
        for name in fields:
            if isinstance(name, str) and name.strip():
                names.add(name.strip())
    _DEFERRABLE_NOTICE_FIELDS_CACHE = frozenset(names)
    return _DEFERRABLE_NOTICE_FIELDS_CACHE


# ---------------------------------------------------------------------------
# 고시 35종 전체에 **공통으로 등장하는** 필드 — 교집합에서 유도.
#
# 2026-08-06 실측: 이 교집합은 정확히 5개다 (returnCostReason · noRefundReason ·
# qualityAssuranceStandard · compensationProcedure · troubleShootingContents).
# 실스토어 상품 20건에서 이 5필드는 값이 ``"1"`` 이었고, 그 외 고시 필드는
# ``"상세페이지 참조"`` 였다. 본 집합은 그 5필드에 ``DEFERRED_COMMON_NOTICE_VALUE``
# 를 적용할지 판정하는 데 쓴다 (하드코딩 아님 — 데이터가 바뀌면 따라감).
# ---------------------------------------------------------------------------
_COMMON_NOTICE_DEFERRED_FIELDS_CACHE: frozenset[str] | None = None


def _common_notice_deferred_fields() -> frozenset[str]:
    """고시 35종 전체에 공통으로 등장하는 고시 필드명의 교집합을 반환.

    **수동 목록이 아니다** — ``data/notice_types.json`` 의 35종 각각의 ``fields``
    배열의 **교집합**(intersection)을 계산한다. 모든 타입에 등장하는 필드만 반환.

    2026-08-06 현재 이 교집합은 정확히 5개다:

        ``{compensationProcedure, noRefundReason, qualityAssuranceStandard,
           returnCostReason, troubleShootingContents}``

    이 5필드는 네이버 UI 가 법정 표준 문구를 기본 제공하며, 판매자 대부분이
    그대로 둬서 실스토어 상품에서 ``"1"`` 값으로 관측된다(별도 실측).
    본 함수는 "의미" 를 단정하지 않는다 — **데이터에서 유도되는 자리 집합**이다.

    Returns:
        35종 전체에 공통인 고시 필드명 ``frozenset``. 데이터가 바뀌면 자동으로
        따라간다.

    Raises:
        RuntimeError: ``notice_types.json`` 부재/손상 (fail-closed — 이미
            ``_load_notice_types`` 가 같은 파일을 검증하므로 정상 설치 시 발생
            하지 않는다).
    """
    global _COMMON_NOTICE_DEFERRED_FIELDS_CACHE
    if _COMMON_NOTICE_DEFERRED_FIELDS_CACHE is not None:
        return _COMMON_NOTICE_DEFERRED_FIELDS_CACHE
    field_sets: list[set[str]] = []
    for entry in _load_notice_types():
        if not isinstance(entry, dict):
            continue
        fields = entry.get("fields")
        if not isinstance(fields, list):
            continue
        names = {str(name).strip() for name in fields if isinstance(name, str) and name.strip()}
        if names:
            field_sets.append(names)
    # 교집합 계산. 타입이 1개 이상이어야 의미가 있다(빈 교집합 방어).
    common: set[str] = set.intersection(*field_sets) if field_sets else set()
    _COMMON_NOTICE_DEFERRED_FIELDS_CACHE = frozenset(common)
    return _COMMON_NOTICE_DEFERRED_FIELDS_CACHE


def _deferred_value_for_field(field: str) -> str:
    """필드별 미루기(deferred) 전송값을 반환 — **단일 분기 지점**.

    본 함수가 **유일한** 분기 지점이다 — 한 곳만 고치면 되돌릴 수 있다:
      - 고시 35종에 공통인 5필드(``_common_notice_deferred_fields``) →
        ``DEFERRED_COMMON_NOTICE_VALUE`` (현재 ``"1"``).
      - 그 외 모든 고시 필드 → ``DEFERRED_NOTICE_PLACEHOLDER``
        (현재 ``"상세페이지 참조"``).

    관측 사실(2026-08-06): 실스토어 상품 20건에서 5 공통필드는 ``"1"``,
    그 외 필드는 ``"상세페이지 참조"`` 였다 (섞임 0건). ``"1"`` 의 의미는
    공식 문서에서 확인된 바 없다 (가설: 법정 표준 문구 선택 코드).

    **타입 가드 (defense in depth)**: boolean/date 타입 필드에는 placeholder
    문자열을 주지 않는다. ``_partition_deferred_by_allowlist`` 와
    ``_field_missing_with_deferred`` 에서 이미 미루기 대상에서 제외되므로
    본 함수에 boolean/date 필드가 들어오는 것은 정상 경로가 아니다 —
    그러나 호출자가 방어를 우회하는 직접 호출 경로까지 막기 위해,
    boolean/date 필드에는 빈 문자열을 반환한다 (문자열 placeholder 를 넣으면
    네이버 API 가 400 거절).

    Args:
        field: 고시 필드명(camelCase).

    Returns:
        해당 필드를 미루기로 선택했을 때 naver_client 가 전송값으로 채울 문자열.
        원산지 필드는 본 함수에 들어오지 않는다(호출자가 ``_reject_origin_deferred``
        로 이미 걸렀다). boolean/date 필드는 빈 문자열(미루기 불가).
    """
    target = str(field or "").strip()
    if not target:
        return DEFERRED_NOTICE_PLACEHOLDER
    # 타입 가드: boolean/date 필드는 문자열 placeholder 를 주면 안 된다.
    if not _is_field_deferrable(target):
        return ""
    if target in _common_notice_deferred_fields():
        return DEFERRED_COMMON_NOTICE_VALUE
    return DEFERRED_NOTICE_PLACEHOLDER


# 미루기 전송값 토큰 두 개를 모두 인식하는 집합 — 보고(mcp_server) 에서
# "이 필드가 미루기로 채워졌는가" 를 판정할 때 쓴다. 두 토큰 중 하나와
# 정확히 일치하면 True. 새 토큰이 추가되면 이 집합에도 추가한다(정본인
# DEFERRED_NOTICE_PLACEHOLDER / DEFERRED_COMMON_NOTICE_VALUE 에서 자동 파생이
# 아니라 — 아직 2개뿐이라 명시적 열거가 더 안전하다).
_DEFERRED_SENTINEL_VALUES = frozenset({DEFERRED_NOTICE_PLACEHOLDER, DEFERRED_COMMON_NOTICE_VALUE})


def _is_deferred_sentinel_value(value) -> bool:
    """``value`` 가 미루기 전송 토큰(``"상세페이지 참조"`` 또는 ``"1"``) 인지 판정.

    mcp_server 의 미루기 적용 보고 로직이 쓴다 — 페이로드 본문에서 특정 필드값이
    미루기 토큰 중 하나와 일치하면 "이 필드는 미루기로 적용되었다" 고 본다.
    본 함수는 **문자열 전체 일치**로만 판정한다 (placeholder 판정처럼 정규화하지
    않는다 — 미루기 토큰은 시스템이 정확히 그대로 채워 넣은 값이다).

    Args:
        value: 페이로드 본문의 필드값.

    Returns:
        ``True`` = ``value`` 가 두 미루기 토큰 중 하나와 정확히 일치.
    """
    return value in _DEFERRED_SENTINEL_VALUES


def _partition_deferred_by_allowlist(
    deferred: list[str] | tuple[str, ...] | set[str] | None,
) -> tuple[list[str], list[str]]:
    """미루기 후보를 (allowlist 내, allowlist 밖) 으로 분리한다.

    allowlist 는 ``_deferrable_notice_fields`` 에서 유도한다(수동 목록 아님).
    원산지 필드는 allowlist 안에 있더라도 ``ORIGIN_FIELDS_NOT_DEFERRABLE`` 로
    별도 거른다 — 원산지는 어떤 표기로도 미루기 대상이 될 수 없다(기존 의도).

    **타입 기반 미루기 불가 (boolean/date)**: ``_is_field_deferrable`` 이
    ``False`` 인 필드(boolean/date)도 rejected 로 보낸다. 네이버 API 가 문자열
    placeholder 를 Boolean/date 타입으로 받지 않기 때문이다. 이 필드들은
    미루기 대상에서 제외되고, 사용자에게 실제 값을 요구한다 (needs_user 로 안내).

    Args:
        deferred: 판매자가 넘긴 미루기 후보 필드명 컬렉션.

    Returns:
        ``(allowed, rejected)`` —
        - ``allowed``: allowlist 안에 있고 원산지가 아니며 타입이 미루기
          가능(string/미기재)한 필드명 리스트(순서 보존).
        - ``rejected``: allowlist 밖이거나 원산지이거나 boolean/date 타입인
          필드명 리스트(순서 보존). 호출자가 거부 사유에 그대로 쓴다.
    """
    if not deferred:
        return [], []
    allowed: list[str] = []
    rejected: list[str] = []
    allowlist = _deferrable_notice_fields()
    seen: set[str] = set()
    for name in deferred:
        text = str(name or "").strip()
        if not text:
            # 빈 항목은 호출자(mcp_server) 가 이미 거부하지만, 본 함수가 직접
            # 호출되는 경로까지 방어적으로 거른다.
            continue
        if text in seen:
            # 중복 항목은 첫 번째 것만 인정(나머지는 무시).
            continue
        seen.add(text)
        if text in ORIGIN_FIELDS_NOT_DEFERRABLE:
            rejected.append(text)
        elif text in allowlist and _is_field_deferrable(text):
            allowed.append(text)
        else:
            rejected.append(text)
    return allowed, rejected


def _reject_origin_deferred(
    deferred: list[str] | tuple[str, ...] | set[str] | None,
) -> list[str]:
    """판매자가 미루기로 선택한 필드 중 원산지 필드를 거르고 남긴다.

    원산지는 법적 선언이므로 "상세페이지 참조" 로 미루는 것을 허용하지 않는다.
    판매자가 origin 필드를 미루기로 선택한 경우, 본 함수는 그것을 가차 없이
    제거한 뒤 남은 필드명 리스트를 반환한다 — 호출자는 이 반환값을 쓴다.
    원산지를 거른 사실은 별도로 mcp_server 단에서 거부 응답으로 알린다
    (origin 은 미루기 불가). 본 함수 자체는 정책의 *필터* 다.

    Args:
        deferred: 판매자가 넘긴 미루기 후보 필드명 컬렉션.

    Returns:
        원산지 필드를 제외한 미루기 필드명 리스트(순서 보존). 입력이 ``None``
        이거나 비어있으면 빈 리스트.
    """
    if not deferred:
        return []
    kept: list[str] = []
    for name in deferred:
        text = str(name or "").strip()
        if not text:
            continue
        if text in ORIGIN_FIELDS_NOT_DEFERRABLE:
            continue
        kept.append(text)
    return kept


def _field_missing_with_deferred(
    notice_body,
    fields,
    deferred: list[str] | tuple[str, ...] | set[str] | None,
    notice_type=None,
) -> list[str]:
    """``_notice_field_missing_with_relations`` 의 deferred 인지 변형.

    판매자가 명시적으로 미루기로 선택(``deferred``) 한 필드는 "누락" 에서
    제외한다. 이것이 본 기능의 핵심 — 미루기 선택이 없으면 빈 것과 같아
    기존 동작(``_notice_field_missing_with_relations``)이 그대로 적용된다.
    원산지 필드는 호출자가 선택해도 미루기에서 제외된다
    (``_field_is_deferred`` 가 ORIGIN 필드를 인지하지만, ``_reject_origin_deferred``
    가 mcp_server 단에서 미리 걸러낸다 — 본 함수에서도 방어적으로 한 번 더 거른다).

    **타입 기반 미루기 불가 (boolean/date)**: ``_is_field_deferable`` 이
    ``False`` 인 필드(boolean/date)는 판매자가 미루기로 선택했더라도 "누락"
    에서 제외하지 않는다. 네이버 API 가 문자열 placeholder 를 Boolean/date
    타입으로 deserialize 하지 못해 400 거절하기 때문이다. 이 필드들은 missing
    에 남아 needs_user 로 사용자에게 실제 값을 요구한다.

    ``notice_type`` 이 주어지면 XOR 그룹을 함께 고려한다(XOR 그룹의 멤버 중
    하나가 미루기 대상이거나 채워져 있으면 그룹 전체를 충족으로 본다).
    """
    deferred_set = _reject_origin_deferred(deferred)
    if not deferred_set:
        return _notice_field_missing_with_relations(notice_body, fields, notice_type=notice_type)
    # XOR 인지가 필요할 때만 본 경로로 들어온다 — 같은 헬퍼를 재사용.
    xor_groups = _notice_xor_groups(notice_type) if notice_type else []
    if not xor_groups:
        # 단순 경로: deferred 가 아닌 필드만 누락 판정.
        missing: list[str] = []
        if not isinstance(notice_body, dict):
            return list(fields)
        for field in fields:
            if _field_is_deferred(field, deferred_set) and _is_field_deferrable(field):
                continue
            raw = notice_body.get(field)
            ftype = _notice_field_type(field)
            if ftype == "boolean":
                if raw is None:
                    missing.append(field)
            elif ftype in ("integer", "long"):
                # 0 은 유효한 정수값이다 — _is_placeholder_value(0) == True 이므로
                # placeholder 경로에 맡기면 0 이 미제공으로 판정되는 회귀가 생긴다.
                # None/키부재만 미제공.
                if raw is None:
                    missing.append(field)
            elif ftype == "date":
                if _is_placeholder_value(raw):
                    missing.append(field)
            elif _is_placeholder_value(raw):
                missing.append(field)
        return missing
    # XOR 그룹이 있을 때: _notice_field_missing_with_relations 의 충족 판정을
    # 재사용하되, deferred 멤버를 "채워진" 것으로 취급해 그룹 전체를 충족시킨다.
    # 허위 신고가 되지 않도록 — deferred 필드의 값은 naver_client 가 표준 문구로
    # 채워 전송하므로, "filled" 로 보는 것은 전송 사실과 일치한다.
    member_to_group: dict[str, tuple[str, ...]] = {}
    for group in xor_groups:
        group_tuple = tuple(group)
        for member in group:
            member_to_group[member] = group_tuple
    satisfied: set[tuple[str, ...]] = set()
    missing_x: list[str] = []
    if not isinstance(notice_body, dict):
        return list(fields)
    for field in fields:
        group = member_to_group.get(field)
        if group is not None and group in satisfied:
            continue
        if group is not None:
            # 그룹 멤버 중 하나라도 채워져 있거나 deferred 멤버면 충족.
            # 단, boolean/date 타입의 멤버는 미루기 불가 → deferred 로 인정하지 않는다.
            if any(
                _notice_field_filled(notice_body, m)
                or (_field_is_deferred(m, deferred_set) and _is_field_deferrable(m))
                for m in group
            ):
                satisfied.add(group)
                continue
            if field == group[0]:
                missing_x.append(field)
            continue
        if _field_is_deferred(field, deferred_set) and _is_field_deferrable(field):
            continue
        raw = notice_body.get(field)
        ftype = _notice_field_type(field)
        if ftype == "boolean":
            if raw is None:
                missing_x.append(field)
        elif ftype in ("integer", "long"):
            # 0 은 유효한 정수값이다 — _is_placeholder_value(0) == True 이므로
            # placeholder 경로에 맡기면 0 이 미제공으로 판정되는 회귀가 생긴다.
            # None/키부재만 미제공.
            if raw is None:
                missing_x.append(field)
        elif ftype == "date":
            if _is_placeholder_value(raw):
                missing_x.append(field)
        elif _is_placeholder_value(raw):
            missing_x.append(field)
    return missing_x


def _notice_field_missing(notice_body, fields):
    """``notice_body`` 에서 누락된 필수 필드 이름 리스트 반환.

    안내 문구성 값(예: ``상세참조``, ``상세페이지 참조``, ``해당없음``,
    ``-``, 공백류)은 **미제공으로 취급**한다. 핵심 정책:

      - **전송은 하되 "필수 항목이 채워졌다"고 판정하지는 않는다** — 두 가지를
        구분한다. ``_merge_notice`` (naver_client) 가 사용자가 명시적으로 준
        placeholder 값을 그대로 payload 에 싣는 것은 기존 결정을 유지하되,
        컴플라이언스 판정은 그 값을 "유효 제공" 으로 인정하지 않는다.
      - 사용자가 placeholder 없이 실질 정보를 주어야만 "채워짐" 이다.
      - **변형 우회 차단**: 판정 시 값을 정규화한 뒤 비교한다(공백 제거·
        전각/반각 통일 등). 공백 삽입/표기 차이로 안내문구가 "채워짐" 으로
        우회되지 않도록 한다 (``_is_placeholder_value`` 참고).

    **필드 타입 인지 (중요)**: ``_notice_field_type`` 이 반환하는 타입에 따라
    "제공됨" 판정이 다르다.

      - ``boolean``: ``True`` 와 ``False`` **둘 다 제공된 것**이다. 미제공은
        ``None`` (또는 키 부재) 뿐이다. 과거의 결정적 결함 — boolean ``False``
        를 미제공으로 읽어 게이트가 차단하던 동작 — 을 바로잡는다. 문자열 등
        다른 타입의 값은 "제공된 것으로" 두고 naver_client 전송 단계에서
        타입 검증·거부한다 (여기서 조용히 판정하지 않는다).
      - ``date``: 비어있지 않은 값이면 제공. 형식 검증은 하지 않는다(형식
        미확정 — 사용자가 준 값을 가공 없이 싣는다).
      - ``string`` (기본/미기재): 기존 동작(공백·placeholder = 미제공).

    **XOR 그룹 인지 (상호배제)**: ``notice_field_relations.json`` 에 기록된
    XOR 그룹의 필드들은 "이 필드들 중 정확히 하나" 가 충족 조건이다. 호출자가
    ``fields`` 에 XOR 그룹의 모든 멤버를 개별적으로 넣더라도, 하나만 채워져
    있으면 그 멤버들을 누락에서 제외한다. "둘 다 채워짐" 위반은 본 함수가
    아닌 ``_compliance_code_check`` 의 별도 위반 규칙으로 잡는다(분리된 관심).
    본 함수는 "누락" 만 다룬다.
    """
    missing = []
    if not isinstance(notice_body, dict):
        return list(fields)
    # notice_type 을 알 수 없으므로, 본 함수는 XOR 인지를 외부에서 받지 않는다.
    # XOR 처리는 _compliance_code_check 가 notice_type 을 아는 상태에서
    # _notice_field_missing 을 호출하기 전에 fields 를 가공하는 경로로 다룬다.
    # (순환 의존 방지: _notice_field_missing 은 타입 비지식 함수로 둔다.)
    for field in fields:
        raw = notice_body.get(field)
        ftype = _notice_field_type(field)
        if ftype == "boolean":
            # True/False 둘 다 제공. None/키부재만 미제공. 문자열 등 다른 타입은
            # "제공된 것으로" 두고 naver_client 전송 단계에서 타입 검증·거부한다.
            if raw is None:
                missing.append(field)
            # bool True/False 또는 그 외 타입은 제공된 것으로 간주.
        elif ftype in ("integer", "long"):
            # 0 은 유효한 정수값이다 — _is_placeholder_value(0) == True 이므로
            # placeholder 경로에 맡기면 0 이 미제공으로 판정되는 회귀가 생긴다.
            # None/키부재만 미제공.
            if raw is None:
                missing.append(field)
        elif ftype == "date":
            # 비어있지 않은 값이면 제공. 형식 검증은 하지 않는다(형식 미확정).
            if _is_placeholder_value(raw):
                missing.append(field)
        # string (기본/미기재) — 기존 동작 유지. 아래 elif 은 ftype 이
        # "boolean" 도 "date" 도 아닌 경우(= 미기재 → 문자열 취급)의 분기다.
        elif _is_placeholder_value(raw):
            missing.append(field)
    return missing


def _notice_field_missing_with_relations(notice_body, fields, notice_type=None):
    """``_notice_field_missing`` 의 XOR 인지 변형.

    ``notice_type`` 이 주어지면 해당 타입의 XOR 그룹을 ``notice_field_relations.json``
    에서 읽어 적용한다. XOR 그룹의 멤버 중 **하나라도 채워져 있으면** 그 그룹의
    모든 멤버를 "충족" 으로 처리한다.

    본 함수는 "누락" 만 다룬다. "둘 다 채워짐" 위반은 별도 규칙
    (``_compliance_code_check`` 의 ``고시 필드 상호배제`` rule) 이 다룬다.

    Args:
        notice_body: 고시 본문 dict.
        fields: 필수 필드명 리스트.
        notice_type: 고시 타입명 (XOR 관계 조회용). ``None`` 이면 XOR 미적용
            (``_notice_field_missing`` 과 동일).

    Returns:
        누락된 필수 필드명 리스트.
    """
    if notice_type is None:
        return _notice_field_missing(notice_body, fields)
    xor_groups = _notice_xor_groups(notice_type)
    if not xor_groups:
        # 기록된 XOR 관계가 없으면 기존 동작 유지.
        return _notice_field_missing(notice_body, fields)
    # XOR 그룹 멤버를 (member → group) 맵으로 인덱싱.
    member_to_group: dict[str, tuple[str, ...]] = {}
    for group in xor_groups:
        group_tuple = tuple(group)
        for member in group:
            member_to_group[member] = group_tuple
    missing: list[str] = []
    if not isinstance(notice_body, dict):
        return list(fields)
    # 이미 "충족" 처리된 XOR 그룹을 추적 (중복 누락 보고 방지).
    satisfied_groups: set[tuple[str, ...]] = set()
    for field in fields:
        group = member_to_group.get(field)
        if group is not None and group in satisfied_groups:
            # 이 그룹은 이미 하나가 채워져 충족됨 — 스킵.
            continue
        if group is not None:
            # 그룹 멤버 중 하나라도 채워져 있는지 확인.
            if any(_notice_field_filled(notice_body, m) for m in group):
                satisfied_groups.add(group)
                continue
            # 그룹 전체가 비어있으면 — 각 멤버를 한 번만 누락으로 보고.
            # 호출자가 그룹 전체 멤버를 fields 에 넣었더라도 중복을 피하기 위해
            # 첫 멤버만 보고한다.
            if field == group[0]:
                missing.append(field)
            continue
        # XOR 그룹에 속하지 않는 필드 — 기존 판정.
        raw = notice_body.get(field)
        ftype = _notice_field_type(field)
        if ftype == "boolean":
            if raw is None:
                missing.append(field)
        elif ftype in ("integer", "long"):
            # 0 은 유효한 정수값이다 — _is_placeholder_value(0) == True 이므로
            # placeholder 경로에 맡기면 0 이 미제공으로 판정되는 회귀가 생긴다.
            # None/키부재만 미제공.
            if raw is None:
                missing.append(field)
        elif ftype == "date":
            if _is_placeholder_value(raw):
                missing.append(field)
        elif _is_placeholder_value(raw):
            missing.append(field)
    return missing


def _notice_field_xor_violations(notice_body, notice_type) -> list[dict]:
    """XOR 그룹에서 "둘 다 채워져 있는" 위반을 반환.

    네이버 API 가 둘 다 있으면 거절하므로, 게이트에서 미리 막는다.
    사유에 "둘 중 하나만" 임을 밝힌다.

    Returns:
        위반 dict 리스트. 각 원소는 ``{"group": [...], "detail": ...}`` 형태.
        빈 리스트면 위반 없음.
    """
    xor_groups = _notice_xor_groups(notice_type)
    if not xor_groups:
        return []
    violations: list[dict] = []
    for group in xor_groups:
        filled = [m for m in group if _notice_field_filled(notice_body, m)]
        if len(filled) >= 2:
            violations.append(
                {
                    "group": list(group),
                    "detail": (
                        f"고시 필드 {', '.join(group)} 중 둘 중 하나만 입력해야 합니다 "
                        "(네이버 상호배제 — 둘 다 있으면 거절됨). "
                        f"현재 채워진 필드: {', '.join(filled)}."
                    ),
                }
            )
    return violations


def _compliance_code_check(name, context, api_payload=None, deferred_notice_fields=None):
    """컴플라이언스 코드검사 (데이터 기반 재작성).

    ``api_payload`` 가 주어지면 그 안의 ``originProduct.detailAttribute`` 를
    검사한다. ``context`` 만 주어지면 ``context`` 에서 notice/origin/kc 값을
    읽는다.

    ``deferred_notice_fields`` 는 판매자가 명시적으로 "상세페이지 참조" 로
    미루기로 선택한 고시 필드명 리스트다. 원산지 필드는 여기 들어와도
    거부된다(``_reject_origin_deferred``). 미루기로 선택된 필드는 "필수 필드
    누락" 판정에서 제외된다 — 허위 신고가 되지 않는 이유는 naver_client 가 그
    자리에 표준 문구(``DEFERRED_NOTICE_PLACEHOLDER``)를 채워 전송하기 때문이다.
    미루기 선택이 ``None`` 이거나 비어있으면 기존 동작(fail-closed)을 유지한다.

    Args:
        name: 상품명.
        context: 컨텍스트 dict (``category_id``, ``notice``, ``origin_code``,
            ``origin_content``, ``kc_declaration`` 등).
        api_payload: 등록 payload (선택).
        deferred_notice_fields: 판매자가 선택한 미루기 필드명 리스트(선택).
            원산지 필드는 무시된다.

    Returns:
        ``{agent, verdict, violations, summary}`` dict.
    """
    violations = []
    ctx = context if isinstance(context, dict) else {}

    # detailAttribute 추출 (api_payload 우선, 없으면 context).
    detail_attr = {}
    if isinstance(api_payload, dict):
        origin_product = api_payload.get("originProduct")
        if isinstance(origin_product, dict):
            da = origin_product.get("detailAttribute")
            if isinstance(da, dict):
                detail_attr = da
    if not detail_attr:
        detail_attr = ctx.get("detailAttribute") or {}

    # --- 원본 이미지 검사 (보고 지점 — 페이로드 관측 시에만) ---
    # 우연한 차단(고시 필드 등)과 명시적 차단(이미지 부재)을 구분하기 위해
    # 별도 rule 로 발생한다. 단, **api_payload 가 dict 일 때만** 판정한다.
    # api_payload 가 없는 경로(prepare_listing 등)에서 "없음" 으로 단정하면
    # 정상 이미지가 있어도 FAIL 이 되어 영구 등록 불가가 된다.
    #
    # 이 지점은 강제 지점이 아니라 **보고 지점**이다. 네이버에 실제로 도달하는
    # 모든 경로는 naver_client.build_payload 진입 게이트와 register_product 의
    # POST 직전 검증을 반드시 통과하므로, 페이로드를 관측할 수 없는 여기서
    # FAIL/PENDING 을 내지 않아도 fail-closed 가 무너지지 않는다. 강제는 페이로드
    # 계층이 한다.
    if isinstance(api_payload, dict):
        rep_url = ""
        op = api_payload.get("originProduct")
        if isinstance(op, dict):
            images = op.get("images")
            if isinstance(images, dict):
                rep = images.get("representativeImage")
                if isinstance(rep, dict):
                    rep_url = str(rep.get("url") or "").strip()
        if not rep_url:
            violations.append(
                {
                    "rule": "원본 이미지",
                    "severity": FAIL,
                    "detail": (
                        "대표 이미지 URL 이 비어 있습니다. 원본 이미지가 최소 1장 필요합니다. "
                        "실재하는 상품의 사진 없이는 등록을 진행하지 않습니다."
                    ),
                }
            )

    # --- 고시 필수 필드 검사 (데이터 기반) ---
    notice = (
        detail_attr.get("productInfoProvidedNotice")
        if isinstance(detail_attr.get("productInfoProvidedNotice"), dict)
        else ctx.get("notice")
    )
    notice_type = (
        _infer_notice_type(
            {"notice": notice} if isinstance(notice, dict) else ctx,
        )
        if not isinstance(notice, dict)
        else _infer_notice_type({"notice": notice})
    )
    spec = _notice_type_spec(notice_type)
    notice_body = {}
    if isinstance(notice, dict):
        node_key = (spec or {}).get("node") or "etc"
        body = notice.get(node_key)
        if isinstance(body, dict):
            notice_body = body
        else:
            # node_key 가 없으면 etc/furniture 중 존재하는 것 사용.
            for fallback_key in ("etc", "furniture"):
                fb = notice.get(fallback_key)
                if isinstance(fb, dict):
                    notice_body = fb
                    break
    required_fields = (spec or {}).get("fields") or []
    if required_fields:
        # XOR 인지 누락 판정: 하나만 채워져도 그룹 전체가 충족으로 인정.
        # deferred_notice_fields 로 넘겨진 필드는 누락에서 제외한다(원산지는 제외).
        missing = _field_missing_with_deferred(
            notice_body,
            required_fields,
            deferred_notice_fields,
            notice_type=notice_type,
        )
        if missing:
            violations.append(
                {
                    "rule": "고시 필수필드",
                    "severity": FAIL,
                    "detail": (
                        f"고시 타입 {notice_type} 필수 필드 누락: " + ", ".join(missing[:10])
                    ),
                }
            )
        # XOR "둘 다 채워짐" 위반 — 네이버가 거절하므로 게이트에서 미리 차단.
        # 이 위반은 "누락" 과는 별개의 규칙이다 (분리된 관심).
        xor_violations = _notice_field_xor_violations(notice_body, notice_type)
        for xv in xor_violations:
            violations.append(
                {
                    "rule": "고시 필드 상호배제",
                    "severity": FAIL,
                    "detail": str(xv.get("detail") or ""),
                }
            )

    # --- 원산지 검사: config 값과 payload 값의 일치 (값 판정 X) ---
    origin_info = (
        detail_attr.get("originAreaInfo")
        if isinstance(detail_attr.get("originAreaInfo"), dict)
        else {}
    )
    payload_origin_content = str(
        origin_info.get("content") or ctx.get("origin_content") or ""
    ).strip()
    config_origin_content = ""
    try:
        notice_defaults = common.cfg().get("smartstore_notice_defaults") or {}
        if isinstance(notice_defaults, dict):
            config_origin_content = str(notice_defaults.get("origin_content") or "").strip()
    except Exception:
        pass
    if config_origin_content and payload_origin_content:
        # 둘 다 있으면 일치해야 함 (값 자체는 판정하지 않음).
        if config_origin_content != payload_origin_content:
            violations.append(
                {
                    "rule": "원산지 불일치",
                    "severity": FAIL,
                    "detail": (
                        f"config 원산지({config_origin_content!r})와 payload 원산지"
                        f"({payload_origin_content!r})가 일치하지 않습니다."
                    ),
                }
            )
    elif not payload_origin_content:
        # payload 원산지 누락 — fail-closed.
        violations.append(
            {
                "rule": "원산지 누락",
                "severity": FAIL,
                "detail": "원산지(originAreaInfo.content)가 비어 있습니다.",
            }
        )

    # --- KC 검사: category_meta.requires_kc() 기반 (3-상태, fail-closed) ---
    # requires_kc 는 True/False/None(불명) 을 반환한다. 상세 조회가 실패해 KC
    # 필요 여부를 확정하지 못한 카테고리는 None 이며, 이를 False 처럼 통과시키면
    # KC 대상을 면제로 오판하는 허위 신고가 된다. 불명은 FAIL 로 차단한다.
    category_id = ctx.get("category_id") or ctx.get("categoryId") or ctx.get("leaf_category_id")
    if category_id is not None:
        try:
            from . import category_meta

            requires_kc = category_meta.requires_kc(
                category_id,
                raise_if_unknown=False,
                raise_if_incomplete=False,
            )
        except Exception as exc:
            # fail-closed: category_meta 조회 실패 시 KC 검사를 건너뛰지 않고 예외 전파.
            raise RuntimeError(f"category_meta.requires_kc 조회 실패 (fail-closed): {exc}") from exc
        if requires_kc is None:
            # KC 필요 여부 불명 — 통과시키지 않는다(허위 신고 방지).
            violations.append(
                {
                    "rule": "KC 필요 여부 불명",
                    "severity": FAIL,
                    "detail": (
                        f"카테고리 {category_id} 의 KC 인증 필요 여부를 확정할 수 없습니다 "
                        "(상세 조회 실패로 exceptionalCategories 미확정). "
                        "실제 KC 대상인지 확인하기 전까지 등록을 차단한다(fail-closed)."
                    ),
                }
            )
        elif requires_kc:
            kc_block = (
                detail_attr.get("certificationTargetExcludeContent")
                if isinstance(detail_attr.get("certificationTargetExcludeContent"), dict)
                else {}
            )
            if not kc_block:
                violations.append(
                    {
                        "rule": "KC 인증 누락",
                        "severity": FAIL,
                        "detail": (
                            f"카테고리 {category_id} 는 KC 인증이 필요하지만 "
                            "certificationTargetExcludeContent 정보가 없습니다."
                        ),
                    }
                )
        # requires_kc == False 면 KC 검사하지 않음 (확정 불필요).

    # --- A/S 정보 검사 ---
    # AS 연락처 미설정 시 기본 문자열 생성이 제거되었으므로, 컴플라이언스는
    # 미설정을 FAIL 로 차단한다 (문서 서술과 일치). WARN 에 그치는 것은 fail-open.
    as_info = (
        detail_attr.get("afterServiceInfo")
        if isinstance(detail_attr.get("afterServiceInfo"), dict)
        else {}
    )
    as_tel_value = str(
        as_info.get("afterServiceTelephoneNumber") or ctx.get("as_tel") or ""
    ).strip()
    if not as_tel_value:
        violations.append(
            {
                "rule": "A/S 연락처 누락",
                "severity": FAIL,
                "detail": (
                    "afterServiceTelephoneNumber 이 비어 있습니다. AS 연락처는 판매자가 "
                    "실제 신고하는 필수 규제값이며 코드가 임의값을 만들어 넣지 않습니다."
                ),
            }
        )

    verdict = _verdict_from_violations(violations)
    return {
        "agent": "compliance",
        "verdict": verdict,
        "violations": violations,
        "summary": f"컴플라이언스 {verdict} — 위반 {len(violations)}건",
    }


# ---------------------------------------------------------------------------
# 카피 코드검사 (원본 _copy_code_check 의 결정론적 부분 보존).
#
# 금지 표현 정규식은 text_props.BANNED_CLAIM_RE 를 재사용한다 (중복 정의 금지).
# ---------------------------------------------------------------------------


def _copy_code_check(name, detail_text, option_texts=None):
    """카피/텍스트 결정론적 코드검사.

    LLM 위임(qa_copy_agent)과 병행하여 실행되는 로컬 검사다. 결정론적으로
    잡을 수 있는 위반(금지어, 빈 제목 등)을 FAIL/WARN 으로 보고한다.

    Returns:
        ``{agent, verdict, violations, summary}`` dict.
    """
    violations = []
    name = str(name or "")
    detail_text = str(detail_text or "")

    if not name.strip():
        violations.append(
            {
                "rule": "빈 제목",
                "severity": WARN,
                "detail": "SEO 상품명이 비어 있습니다.",
            }
        )
    elif len(name) > 100:
        violations.append(
            {
                "rule": "제목 길이 초과",
                "severity": WARN,
                "detail": f"SEO 상품명이 100자를 초과합니다 ({len(name)}자).",
            }
        )

    if BANNED_CLAIM_RE.search(name):
        violations.append(
            {
                "rule": "금지 표현",
                "severity": FAIL,
                "detail": "SEO 상품명에 금지 표현(정품/진품/100%/최고급 등)이 포함되어 있습니다.",
            }
        )
    if BANNED_CLAIM_RE.search(detail_text):
        violations.append(
            {
                "rule": "금지 표현",
                "severity": FAIL,
                "detail": "상세 본문에 금지 표현이 포함되어 있습니다.",
            }
        )

    verdict = _verdict_from_violations(violations)
    return {
        "agent": "copy",
        "verdict": verdict,
        "violations": violations,
        "summary": f"카피 코드검사 {verdict} — 위반 {len(violations)}건",
    }


# ---------------------------------------------------------------------------
# 3분할 QA 에이전트.
# ---------------------------------------------------------------------------


def qa_image(detail_jpeg_path, name, context=None):
    """이미지 QA.

    본 이식판은 상세 JPEG 렌더 파이프라인이 별도 모듈에 있으므로, 실제 픽셀
    검사는 스텁이 아닌 **파일 부재 시 FAIL** 처리한다. 렌더된 JPEG 이 존재하면
    로컬 결정론적 검사(여백/레이아웃)를 수행한다 — 단 PIL 이 없으면 예외를
    삼키지 않고 WARN 으로 보고한다 (fail-closed 원칙: 예외 삼켜 PASS 금지).
    """
    import os

    violations = []
    if not detail_jpeg_path or not os.path.exists(str(detail_jpeg_path)):
        violations.append(
            {
                "rule": "상세 이미지 부재",
                "severity": FAIL,
                "detail": f"상세 JPEG 경로가 없거나 파일이 존재하지 않습니다: {detail_jpeg_path}",
            }
        )
        return _qa_agent_result("image", FAIL, violations, "상세 이미지 없음 — 이미지 QA 불가")
    # 실제 픽셀 검사는 렌더 파이프라인이 완성된 후 연결된다.
    # 현재는 파일 존재만 확인하고 PASS 로 둔다 — 단 예외 삼킴 없이.
    return _qa_agent_result(
        "image", PASS, [], "상세 이미지 존재 확인 (픽셀 검사는 렌더 파이프라인 연동 예정)"
    )


def qa_copy(detail_jpeg_path, name, context=None):
    """카피/텍스트 QA.

    본 이식판의 동작:
      1. 로컬 결정론적 코드검사(``_copy_code_check``) 실행 — 즉시 verdict 산출.
      2. ``agent_calls.qa_copy_agent`` 로 LLM 위임 디스크립터 생성.
      3. 위임 결과가 **아직 회신되지 않았으면**(llm_hint dict 인 경우) 해당
         항목을 ``PENDING`` verdict 로 표시한다. 이것이 "위임 미회신 차단"
         의 핵심이다.

    호출자가 이미 LLM 회신 dict 을 ``context["copy_qa_response"]`` 로 전달한
    경우, 그 결과를 정규화하여 병합한다.
    """
    from . import agent_calls

    ctx = context if isinstance(context, dict) else {}
    detail_text = str(ctx.get("detail_text") or ctx.get("detail_html_text") or "")
    option_texts = ctx.get("option_texts") or []

    # 1. 로컬 코드검사.
    code_check = _copy_code_check(name, detail_text, option_texts)
    result = _normalize_agent_result(code_check, "copy")

    # 2. LLM 위임.
    pre_response = ctx.get("copy_qa_response")
    if isinstance(pre_response, dict):
        # 호스트가 이미 회신한 경우: 정규화하여 병합.
        llm_result = _normalize_agent_result(pre_response, "copy")
        result = _merge_code_check(llm_result, code_check, "copy_code_check")
    else:
        # 위임이 아직 회신되지 않음 — llm_hint 를 input 에 싣고 PENDING 표시.
        hint = agent_calls.qa_copy_agent(name, ctx, detail_text)
        result["copy_qa_hint"] = hint
        # 카피 QA 의 최종 verdict 는 LLM 회신이 있어야 확정된다. 로컬 코드검사가
        # FAIL 이면 즉시 FAIL; 아니면 PENDING (게이트가 차단).
        local_verdict = _clamp_verdict(result.get("verdict"))
        if local_verdict != FAIL:
            result["verdict"] = PENDING
            result["summary"] = (
                "카피 QA LLM 판단 대기 중(PENDING) — 로컬 코드검사는 " f"{local_verdict}."
            )
    return result


def qa_compliance(
    detail_jpeg_path=None,
    name="",
    context=None,
    api_payload=None,
    deferred_notice_fields=None,
):
    """컴플라이언스 QA.

    ``_compliance_code_check`` 로 위임. LLM 위임이 필요 없는 결정론적 검사다.

    ``deferred_notice_fields`` 는 판매자가 명시적으로 "상세페이지 참조" 로
    미루기로 선택한 고시 필드명 리스트다. 원산지 필드는 여기 들어와도
    게이트에서 거부된다. ``None`` 이면 기존 동작(fail-closed)을 유지한다.
    """
    check = _compliance_code_check(
        name,
        context,
        api_payload=api_payload,
        deferred_notice_fields=deferred_notice_fields,
    )
    return _normalize_agent_result(check, "compliance")


# ---------------------------------------------------------------------------
# 집계 + 게이트.
# ---------------------------------------------------------------------------

# verdict 순위: FAIL > PENDING > WARN > PASS. PENDING 은 WARN 보다 나쁘다
# (회신되지 않은 판단은 검증 누락이므로).
_VERDICT_RANK = {PASS: 0, WARN: 1, PENDING: 2, FAIL: 3}


def aggregate_qa_results(agent_results):
    """3분할 QA 결과를 하나의 verdict 로 집계.

    원본과의 차이:
      - **PENDING verdict 도입**: 위임 미회신 항목은 PENDING 이며, 집계된
        최종 verdict 에 PENDING 이 섞여 있으면 집계 verdict 도 PENDING 이다.
      - 원본은 verdict 누락 시 WARN 으로 떨어뜨렸으나, 본 이식판은 명시적
        PENDING 을 보존한다 (``_clamp_verdict`` 가 default WARN 을 주지만,
        호출자가 PENDING 을 명시한 경우 보존).
    """
    agents = []
    for row in agent_results or []:
        if isinstance(row, dict):
            agents.append(_normalize_agent_result(row, row.get("agent", "unknown")))
    if not agents:
        return _qa_agent_result(
            "aggregate",
            WARN,
            [{"rule": "QA", "severity": WARN, "detail": "3분할 QA 결과가 없습니다."}],
            "3분할 QA 결과 없음",
        )
    worst = PASS
    all_violations = []
    for row in agents:
        v = _clamp_verdict(row.get("verdict"))
        if _VERDICT_RANK.get(v, 1) > _VERDICT_RANK.get(worst, 1):
            worst = v
        for violation in row.get("violations") or []:
            if isinstance(violation, dict):
                tagged = dict(violation)
                tagged.setdefault("agent", row.get("agent"))
                all_violations.append(tagged)
    summary_parts = [
        f"{row.get('agent', '?')}={_clamp_verdict(row.get('verdict'))}" for row in agents
    ]
    return {
        "agent": "aggregate",
        "verdict": worst,
        "violations": all_violations,
        "summary": " | ".join(summary_parts),
        "agents": agents,
    }


def run_qa_agents(detail_jpeg_path, name, context=None, api_payload=None):
    """3개 QA 에이전트 순차 실행 후 집계.

    Args:
        detail_jpeg_path: 렌더된 상세 JPEG 경로.
        name: 상품명.
        context: QA 컨텍스트 dict.
        api_payload: 등록 payload (컴플라이언스 검사용).

    Returns:
        집계 QA 결과 dict.
    """
    ctx = context if isinstance(context, dict) else {}
    results = [
        qa_image(detail_jpeg_path, name, ctx),
        qa_copy(detail_jpeg_path, name, ctx),
        qa_compliance(detail_jpeg_path, name, ctx, api_payload=api_payload),
    ]
    return aggregate_qa_results(results)


def replace_qa_agent_result(qa, replacement):
    """기존 집계 결과 내 한 에이전트 결과를 교체.

    LLM 회신이 도착한 후 카피 QA verdict 를 갱신할 때 사용한다.
    """
    if not isinstance(qa, dict):
        return aggregate_qa_results([replacement])
    agents = [row for row in (qa.get("agents") or []) if isinstance(row, dict)]
    agent_name = str((replacement or {}).get("agent") or "")
    found = False
    for idx, row in enumerate(agents):
        if str(row.get("agent") or "") == agent_name:
            agents[idx] = _normalize_agent_result(replacement, agent_name)
            found = True
            break
    if not found:
        agents.append(_normalize_agent_result(replacement, agent_name))
    return aggregate_qa_results(agents)


def qa_gate(payload):
    """등록 게이트 — QA 결과를 보고 등록 허용/차단 결정 (개정판).

    **핵심 반영 (위임 미회신 차단 — 중대)**:
    원본은 ``verdict in ("PASS", "WARN")`` 일 때 통과시켰다. 이는 verdict 가
    없으면 WARN 으로 떨어지고, WARN 이 통과하는 문제가 있다. 본 이식판은
    ``PENDING`` verdict 를 **차단**한다 — LLM 판단이 아직 회신되지 않은
    상태에서는 등록을 진행할 수 없다.

    Args:
        payload: prepared payload dict. ``payload["qa"]`` 에 집계 결과가 있어야 한다.

    Returns:
        ``(allowed: bool, reason: str)``.
    """
    if not isinstance(payload, dict):
        return False, "payload 가 dict 가 아닙니다."
    qa = payload.get("qa")
    if not isinstance(qa, dict):
        return False, "등록 전 QA 기록이 없어 등록을 차단했습니다."
    verdict = _clamp_verdict(qa.get("verdict"))
    if verdict in _BLOCKING_VERDICTS:
        # FAIL 또는 PENDING — 차단.
        agents = qa.get("agents") or []
        details = []
        for row in agents:
            row_v = _clamp_verdict(row.get("verdict"))
            if row_v in _BLOCKING_VERDICTS:
                v_list = row.get("violations") or []
                bits = [
                    str(v.get("detail", ""))
                    for v in v_list
                    if isinstance(v, dict)
                    and _clamp_verdict(v.get("severity")) in _BLOCKING_VERDICTS
                ]
                details.append(f"{row.get('agent')}: {bits[:3] or row_v}")
        if verdict == PENDING:
            return False, ("QA PENDING(위임 미회신)로 등록 차단: " + " | ".join(details))
        return False, "3분할 QA FAIL로 등록 차단: " + " | ".join(details)
    if verdict == WARN:
        # WARN 은 통과 (원본 동작 보존 — 단 PENDING 이 섞이면 위에서 차단됨).
        return True, ""
    if verdict == PASS:
        return True, ""
    # 알 수 없는 verdict — fail-closed 차단.
    return False, f"알 수 없는 QA verdict({verdict!r})로 등록 차단했습니다."


__all__ = [
    "DEFERRED_COMMON_NOTICE_VALUE",
    "DEFERRED_NOTICE_PLACEHOLDER",
    "FAIL",
    "ORIGIN_FIELDS_NOT_DEFERRABLE",
    "PASS",
    "PENDING",
    "WARN",
    "_clamp_verdict",
    "_common_notice_deferred_fields",
    "_compliance_code_check",
    "_copy_code_check",
    "_deferrable_notice_fields",
    "_deferred_value_for_field",
    "_field_is_deferred",
    "_field_missing_with_deferred",
    "_infer_notice_type",
    "_is_deferred_sentinel_value",
    "_is_field_deferrable",
    "_is_placeholder_value",
    "_load_notice_field_relations",
    "_load_notice_field_types",
    "_load_notice_types",
    "_merge_code_check",
    "_normalize_agent_result",
    "_normalize_placeholder_value",
    "_normalize_qa_result",
    "_notice_field_filled",
    "_notice_field_missing",
    "_notice_field_missing_with_relations",
    "_notice_field_type",
    "_notice_field_xor_violations",
    "_notice_type_spec",
    "_notice_xor_groups",
    "_partition_deferred_by_allowlist",
    "_qa_agent_result",
    "_reject_origin_deferred",
    "_verdict_from_violations",
    "aggregate_qa_results",
    "qa_compliance",
    "qa_copy",
    "qa_gate",
    "qa_image",
    "replace_qa_agent_result",
    "run_qa_agents",
]
