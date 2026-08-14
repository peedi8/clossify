# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""원산지 코드 목록 + 고시 필드 타입 목록 수집 스크립트.

본 스크립트는 **실행 시에만** 네이버 커머스 API 를 호출한다.
테스트나 런타임에서는 한 번도 네이버를 부르지 않는다 (N60 네트워크 차단 유지).

수집 대상 (세 목록 중 두 목록 — 택배사는 판매자별 설정이라 굳히지 않는다):
  1. ``GET /external/v1/product-origin-areas``
     → ``data/product_origin_areas.json``
     (최상위 6 + 시·도 + 시·군·구 계층)
  2. ``GET /external/v1/products-for-provided-notice``
     → ``data/notice_field_types.json`` 갱신 (API 정답표로 교체)
     (fieldType 분포: String·YearMonth·LocalDate·Boolean·Integer·Long)

사용법:
    python scripts/fetch_origin_and_notice_types.py

환경변수:
    CLOSSIFY_CONFIG  — 설정 파일 경로 (기본: .local/config.json)

설계 요점 (fetch_category_meta.py 관례 준수):
  - 인증은 ``clossify.naver_client`` 의 ``get_token``/``_h`` 를 재사용.
    자체 인증 구현 금지.
  - 읽기 전용(GET) 호출만 사용. 쓰기 호출 금지.
  - 429/5xx 지수백오프 최대 3회 재시도.
  - 수집 데이터에는 계정 식별자·스토어명·토큰이 섞이지 않는다
    (두 목록 모두 스토어 무관 공개 메타).
  - 테스트나 런트``임에서 import 되지 않는다 — 순수 수집 전용 스크립트.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

# 스크립트 위치 기준으로 src/ 를 import 경로에 추가.
_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_SRC_PATH = os.path.join(_REPO_ROOT, "src")
if _SRC_PATH not in sys.path:
    sys.path.insert(0, _SRC_PATH)

from clossify import naver_client as nc

DATA_DIR = os.path.join(_REPO_ROOT, "src", "clossify", "data")
ORIGIN_PATH = os.path.join(DATA_DIR, "product_origin_areas.json")
NOTICE_FIELD_TYPES_PATH = os.path.join(DATA_DIR, "notice_field_types.json")
NOTICE_TYPES_PATH = os.path.join(DATA_DIR, "notice_types.json")
FIXTURE_NOTICE_PATH = os.path.join(
    _REPO_ROOT, "tests", "fixtures", "products_for_provided_notice.json"
)
NOTICE_API_PATH = "GET /external/v1/products-for-provided-notice"
COMMON_NOTICE_FIELDS = [
    "returnCostReason",
    "noRefundReason",
    "qualityAssuranceStandard",
    "compensationProcedure",
    "troubleShootingContents",
]
PREVIOUS_NOTICE_ENTRY_KEYS = (
    "field_notes",
    "field_source_note",
    "required_fields_note",
)
UNVERIFIED_NOTICE_TYPES = {
    "AIRLINE_TICKET": {"node": "airlineTicket", "candidate_ko": "항공권 상품 요약 정보"},
    "LODGMENT_RESERVATION": {
        "node": "lodgmentReservation",
        "candidate_ko": "숙박예약 상품 요약 정보",
    },
    "RENT_CAR": {"node": "rentCar", "candidate_ko": "렌터카 상품 요약 정보"},
    "TRAVEL_PACKAGE": {"node": "travelPackage", "candidate_ko": "여행상품 상품 요약 정보"},
}
UNVERIFIED_NOTICE_NOTE = (
    "2026-08-12 정본 API 목록에 없음 — 여행/항공 계열이라 별도 채널일 가능성(미확인)"
)

# 429/5xx 재시도: 최대 3회, 지수백오프.
MAX_RETRIES = 3
RETRY_STATUS = {429, 500, 502, 503, 504}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(int(value))
    except (TypeError, ValueError):
        return None


def _get_with_retry(url: str, tk: str) -> dict:
    """GET with retry. 429/5xx → 지수백오프 최대 MAX_RETRIES 회."""
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = requests.get(url, headers=nc._h(tk, json_ct=False), timeout=30)
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                time.sleep(2**attempt)
                continue
            raise
        if r.status_code in RETRY_STATUS and attempt < MAX_RETRIES:
            retry_after = r.headers.get("Retry-After")
            wait = _retry_after_seconds(retry_after)
            if wait is None:
                wait = float(2**attempt)
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"GET exhausted retries: {url}")


# ---------------------------------------------------------------------------
# 1. 원산지 코드 목록.
# ---------------------------------------------------------------------------


def fetch_origin_areas(tk: str) -> dict:
    """``GET /external/v1/product-origin-areas`` 수집.

    실측 계약:
      - 응답: ``{"originAreaCodeNames": [{"code": "00", "name": "국산"}, ...]}``
      - 535개. 계층은 코드 길이: 2자리(최상위 6)/4자리(시·도 27)/7자리(시·군·구 502).

    반환은 데이터 파일에 쓸 dict.
    """
    url = nc.BASE + "/external/v1/product-origin-areas"
    data = _get_with_retry(url, tk)
    raw = data.get("originAreaCodeNames") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise RuntimeError(
            "product-origin-areas 응답 구조가 올바르지 않음: originAreaCodeNames 배열 없음"
        )

    items: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("code") or "").strip()
        name = str(entry.get("name") or "").strip()
        if not code or not name:
            continue
        items.append({"code": code, "name": name})

    # 계층 분류: 코드 길이 = 계층.
    #   2자리 = 최상위(TOP, 6개)
    #   4자리 = 시·도 (STATE, 27개)
    #   7자리 = 시·군·구 (CITY, 502개)
    top = [it for it in items if len(it["code"]) == 2]
    state = [it for it in items if len(it["code"]) == 4]
    city = [it for it in items if len(it["code"]) == 7]
    other = [it for it in items if len(it["code"]) not in {2, 4, 7}]

    # 코드 기준 정렬 — 데이터가 결정론적이게.
    for bucket in (top, state, city, other):
        bucket.sort(key=lambda x: x["code"])

    doc = {
        "generated_at": _utc_now_iso(),
        "method": "live API",
        "source": "GET /external/v1/product-origin-areas",
        "source_url": "https://api.commerce.naver.com/external/v1/product-origin-areas",
        "count": len(items),
        "hierarchy_note": (
            "계층 = 코드 길이. 2자리 6개(최상위) · 4자리 27개(시·도) · "
            "7자리 502개(시·군·구). 최상위 6: 00 국산 · 01 원양산 · 02 수입산 · "
            "03 상세설명에 표시 · 04 직접입력 · 05 표기의무 아님. "
            "03/04/05 는 '지역 코드'가 아니라 미루기/특수 선택지다 — "
            "지역 코드와 시각적으로 구분하여 판매자가 모르고 고르지 않게 해야 한다."
        ),
        "top_level": top,
        "state_level": state,
        "city_level": city,
    }
    if other:
        # 예상과 다른 코드 길이가 있으면 버리지 않고 보고.
        doc["unexpected_length_items"] = other
    return doc


# ---------------------------------------------------------------------------
# 2. 고시 필드 타입 목록 (API 정답표).
# ---------------------------------------------------------------------------


def _api_field_type_to_ours(field_type: str) -> str:
    """API fieldType → 우리 내부 type 키로 정규화.

    실측 계약 ( fieldType 분포 ):
      String 374 / YearMonth 18 / LocalDate 12 / Boolean 5 / Integer 1 / Long 1

    우리 내부 표현:
      - String     → string   (미루기 허용)
      - Boolean    → boolean  (미루기 불가 — 사용자 요구)
      - YearMonth  → year_month (미루기 불가)
      - LocalDate  → local_date  (미루기 불가)
      - Integer    → integer  (미루기 불가)
      - Long       → long     (미루기 불가)

    과거의 ``date`` 한 덩어리 표현을 YearMonth 와 LocalDate 로 분리한다
    (API 가 구분하므로 우리도 구분한다 — date 를 뭉개면 형식 검증이 어긋난다).
    """
    mapping = {
        "String": "string",
        "Boolean": "boolean",
        "YearMonth": "year_month",
        "LocalDate": "local_date",
        "Integer": "integer",
        "Long": "long",
    }
    return mapping.get(str(field_type or "").strip(), "string")


def fetch_notice_response(tk: str) -> list[dict]:
    """정본 고시 API의 원 응답을 가져온다.

    이 경로는 수동 수집용이다. CI와 오프라인 재생성은 반드시
    ``--notice-fixture``를 사용하며 이 함수를 호출하지 않는다.
    """
    url = nc.BASE + "/external/v1/products-for-provided-notice"
    data = _get_with_retry(url, tk)
    if not isinstance(data, list):
        raise RuntimeError(
            f"products-for-provided-notice 응답이 JSON 배열이 아님: {type(data).__name__}"
        )
    return data


def build_notice_field_types_document(data: list[dict]) -> dict:
    """정본 응답에서 기존 ``notice_field_types.json`` 형식을 만든다.

    실측 계약:
      - 응답: ``[{"productInfoProvidedNoticeType": "WEAR",
        "productInfoProvidedNoticeTypeName": "의류",
        "productInfoProvidedNoticeContents": [
          {"fieldType": "String", "fieldName": "material",
           "fieldDescription": "제품 소재", "fieldAddDescription": "..."}]}, ...]``
      - 36 타입 · 118 필드.
      - fieldType 분포: String 374 · YearMonth 18 · LocalDate 12 · Boolean 5 · Integer 1 · Long 1.

    반환은 데이터 파일에 쓸 dict. 구조는 기존 field_types 맵(필드명 → 타입 정보)을
    유지하되, API 의 fieldDescription/fieldAddDescription 을 함께 싣는다.
    """
    if not isinstance(data, list):
        raise RuntimeError(
            f"products-for-provided-notice 응답이 JSON 배열이 아님: {type(data).__name__}"
        )

    # fieldType 분포 집계(보고용).
    type_distribution: dict[str, int] = {}
    # 필드명 → API 관찰 타입 정보 (동일 필드명이 여러 타입에 나타나면 모두 기록).
    field_observations: dict[str, list[dict]] = {}
    # 타입 목록 (API 정답표).
    api_types: list[dict] = []
    field_count_total = 0

    for entry in data:
        if not isinstance(entry, dict):
            continue
        notice_type = str(entry.get("productInfoProvidedNoticeType") or "").strip()
        notice_type_name = str(entry.get("productInfoProvidedNoticeTypeName") or "").strip()
        contents = entry.get("productInfoProvidedNoticeContents")
        if not isinstance(contents, list):
            contents = []
        api_types.append(
            {
                "type": notice_type,
                "name_ko": notice_type_name,
                "field_count": len(contents),
            }
        )
        for field_entry in contents:
            if not isinstance(field_entry, dict):
                continue
            field_type = str(field_entry.get("fieldType") or "").strip()
            field_name = str(field_entry.get("fieldName") or "").strip()
            field_description = str(field_entry.get("fieldDescription") or "").strip()
            field_add_description = str(field_entry.get("fieldAddDescription") or "").strip()
            if not field_name or not field_type:
                continue
            field_count_total += 1
            type_distribution[field_type] = type_distribution.get(field_type, 0) + 1
            field_observations.setdefault(field_name, []).append(
                {
                    "notice_type": notice_type,
                    "field_type": field_type,
                    "field_description": field_description,
                    "field_add_description": field_add_description,
                }
            )

    # 필드명별 대표 타입 결정.
    # 동일 필드명이 여러 fieldType 으로 관찰될 수 있는데, 본 데이터는 "필드명 → 타입"
    # 단일 진실 공급원으로 쓰므로 한 값으로 정해야 한다. 계약:
    #   - 관찰된 fieldType 집합이 1개면 그것.
    #   - 여러 개면 "혼합" 으로 표시하고 observations 에 모든 관찰을 남긴다
    #     (사용자가 어느 타입에서 어떤 형태로 쓰이는지 직접 확인).
    field_types: dict[str, dict] = {}
    mixed_fields: list[str] = []
    for field_name, observations in field_observations.items():
        observed_types = {obs["field_type"] for obs in observations}
        if len(observed_types) == 1:
            api_type = next(iter(observed_types))
            primary_obs = observations[0]
            field_types[field_name] = {
                "type": _api_field_type_to_ours(api_type),
                "api_field_type": api_type,
                "field_description": primary_obs["field_description"],
                "field_add_description": primary_obs["field_add_description"],
                "observed_in_types": sorted({obs["notice_type"] for obs in observations}),
            }
        else:
            # 혼합 타입 — 가장 많이 관찰된 타입을 대표로 두되, observations 전체를 남긴다.
            # 단일 진실 공급원이 필요한 코드 경로(deferrable 판정)는 "미루기 불가" 쪽으로
            # 안전하게 기울인다 — String 이 아닌 타입이 하나라도 섞여 있으면 사용자 요구로 둔다.
            non_string = observed_types - {"String"}
            if not non_string:
                # 모두 String.
                api_type = "String"
            else:
                # 가장 많이 관찰된 비-String 타입. (동점이면 알파벳순.)
                # 실측상 동일 필드명이 String 과 다른 타입으로 섞이는 경우는 아직
                # 관찰되지 않았지만, 방어적으로 다룬다.
                api_type = sorted(non_string)[0]
            field_types[field_name] = {
                "type": _api_field_type_to_ours(api_type),
                "api_field_type": api_type,
                "field_description": observations[0]["field_description"],
                "field_add_description": observations[0]["field_add_description"],
                "observed_in_types": sorted({obs["notice_type"] for obs in observations}),
                "mixed_types": sorted(observed_types),
            }
            mixed_fields.append(field_name)

    doc = {
        "generated_at": _utc_now_iso(),
        "method": "live API",
        "source": NOTICE_API_PATH,
        "source_url": "https://api.commerce.naver.com/external/v1/products-for-provided-notice",
        "api_notice_type_count": len(api_types),
        "api_field_count_total": field_count_total,
        "api_field_type_distribution": dict(
            sorted(type_distribution.items(), key=lambda kv: (-kv[1], kv[0]))
        ),
        "mixed_type_fields": sorted(mixed_fields),
        "postponable_rule": (
            "String 만 미루기 가능. YearMonth·LocalDate·Boolean·Integer·Long 은 "
            "사용자 요구. 미루기 가능 = (type == 'string')."
        ),
        "field_types": field_types,
        "api_types": sorted(api_types, key=lambda x: x["type"]),
    }
    return doc


def fetch_notice_field_types(tk: str) -> dict:
    """하위 호환용: 실응답을 기존 필드 타입 정본 형식으로 정규화한다."""
    return build_notice_field_types_document(fetch_notice_response(tk))


def _node_from_notice_type(notice_type: str) -> str:
    """알려지지 않은 타입에도 결정론적인 camelCase node 후보를 만든다."""
    parts = [part.lower() for part in notice_type.split("_") if part]
    if not parts:
        raise ValueError("고시 타입이 비어 있어 node 를 만들 수 없습니다.")
    return parts[0] + "".join(part.title() for part in parts[1:])


def build_notice_types_document(response: list[dict], previous: dict) -> dict:
    """저장된 정본 응답을 런타임 ``notice_types.json`` 스키마로 정규화한다.

    응답이 제공하지 않는 공통 5필드는 기존 표현을 유지한다. 응답이 제공한
    필드의 타입·최대 길이·설명·추가 설명은 ``field_meta``에 손실 없이 보존한다.
    """
    if not isinstance(response, list):
        raise ValueError("고시 정본 응답은 JSON 배열이어야 합니다.")
    if not isinstance(previous, dict):
        raise ValueError("기존 notice_types.json 은 JSON 객체여야 합니다.")

    previous_verified = {
        str(entry.get("type") or ""): entry
        for entry in previous.get("verified") or []
        if isinstance(entry, dict) and entry.get("type")
    }
    previous_unverified = {
        str(entry.get("type") or ""): entry
        for entry in previous.get("unverified") or []
        if isinstance(entry, dict) and entry.get("type")
    }

    verified: list[dict] = []
    seen_types: set[str] = set()
    for raw_type in response:
        if not isinstance(raw_type, dict):
            raise ValueError("고시 정본 응답의 타입 항목은 객체여야 합니다.")
        notice_type = str(raw_type.get("productInfoProvidedNoticeType") or "").strip()
        label_ko = str(raw_type.get("productInfoProvidedNoticeTypeName") or "").strip()
        contents = raw_type.get("productInfoProvidedNoticeContents")
        if not notice_type or not label_ko or not isinstance(contents, list):
            raise ValueError(f"고시 정본 타입 항목이 불완전합니다: {raw_type!r}")
        if notice_type in seen_types:
            raise ValueError(f"고시 정본 응답에 중복 타입이 있습니다: {notice_type}")
        seen_types.add(notice_type)

        previous_entry = previous_verified.get(notice_type) or previous_unverified.get(
            notice_type, {}
        )
        fields = list(COMMON_NOTICE_FIELDS)
        field_meta: dict[str, dict] = {}
        for raw_field in contents:
            if not isinstance(raw_field, dict):
                raise ValueError(f"{notice_type} 의 필드 항목은 객체여야 합니다.")
            field_name = str(raw_field.get("fieldName") or "").strip()
            if not field_name:
                raise ValueError(f"{notice_type} 의 fieldName 이 비어 있습니다.")
            if field_name in field_meta:
                raise ValueError(f"{notice_type} 에 중복 fieldName 이 있습니다: {field_name}")
            fields.append(field_name)
            field_meta[field_name] = {
                "fieldType": raw_field.get("fieldType"),
                "fieldMaxLength": raw_field.get("fieldMaxLength"),
                "fieldDescription": raw_field.get("fieldDescription"),
                "fieldAddDescription": raw_field.get("fieldAddDescription"),
            }

        entry = {
            "type": notice_type,
            "source": NOTICE_API_PATH,
            "node": previous_entry.get("node") or _node_from_notice_type(notice_type),
            "label_ko": label_ko,
            "fields": fields,
            "field_source": NOTICE_API_PATH,
            "field_meta": field_meta,
        }
        if previous_entry.get("note"):
            entry["note"] = previous_entry["note"]
        # 정본 API 값이 있으면 그것을 사용하고, 없을 때만 이전 보조 메모를 잇는다.
        for key in PREVIOUS_NOTICE_ENTRY_KEYS:
            if key in raw_type:
                entry[key] = raw_type[key]
            elif key in previous_entry:
                entry[key] = previous_entry[key]
        verified.append(entry)

    unverified: list[dict] = []
    for notice_type in sorted(UNVERIFIED_NOTICE_TYPES):
        if notice_type in seen_types:
            raise ValueError(f"미검증 타입이 정본 응답에 포함되었습니다: {notice_type}")
        prior = previous_unverified.get(notice_type, {})
        info = UNVERIFIED_NOTICE_TYPES[notice_type]
        unverified.append(
            {
                "type": notice_type,
                "node": prior.get("node") or info["node"],
                "candidate_ko": prior.get("candidate_ko") or info["candidate_ko"],
                "note": UNVERIFIED_NOTICE_NOTE,
            }
        )

    doc = {
        "generated_at": "2026-08-12T00:00:00Z",
        "method": "stored API fixture (offline reproducible)",
        "source": NOTICE_API_PATH,
        "sources": [
            {
                "source": NOTICE_API_PATH,
                "what": "저장된 실응답 픽스처에서 오프라인 재생성한 고시 상품군/필드 메타",
            }
        ],
        "coverage_note": (
            "정본 API 픽스처 36종은 verified 로 보관한다. "
            "AIRLINE_TICKET·LODGMENT_RESERVATION·RENT_CAR·TRAVEL_PACKAGE 는 "
            "2026-08-12 정본 API 목록에 없음 — 여행/항공 계열이라 별도 채널일 가능성(미확인)."
        ),
        "expected_total": len(verified) + len(unverified),
        "verified": sorted(verified, key=lambda entry: entry["type"]),
        "unverified": unverified,
    }
    validate_notice_types_document(doc, response)
    return doc


def validate_notice_types_document(document: dict, response: list[dict]) -> None:
    """정본 응답의 필드·메타가 최종 문서에서 누락되지 않았는지 검증한다."""
    if not isinstance(document, dict):
        raise ValueError("검증할 notice_types 문서는 JSON 객체여야 합니다.")
    verified = document.get("verified")
    unverified = document.get("unverified")
    if not isinstance(verified, list) or not isinstance(unverified, list):
        raise ValueError("notice_types 문서에 verified/unverified 배열이 필요합니다.")
    by_type = {str(entry.get("type") or ""): entry for entry in verified if isinstance(entry, dict)}
    response_by_type = {
        str(entry.get("productInfoProvidedNoticeType") or ""): entry
        for entry in response
        if isinstance(entry, dict)
    }
    if set(by_type) != set(response_by_type):
        raise ValueError("verified 타입 집합이 정본 응답 타입 집합과 다릅니다.")
    for notice_type, raw_type in response_by_type.items():
        entry = by_type[notice_type]
        contents = raw_type.get("productInfoProvidedNoticeContents")
        if not isinstance(contents, list):
            raise ValueError(f"{notice_type} 의 정본 필드 배열이 없습니다.")
        expected_fields = COMMON_NOTICE_FIELDS + [
            str(field.get("fieldName") or "").strip()
            for field in contents
            if isinstance(field, dict)
        ]
        if entry.get("fields") != expected_fields:
            raise ValueError(f"{notice_type} 의 fields 가 정본 응답과 다릅니다.")
        if entry.get("source") != NOTICE_API_PATH or entry.get("field_source") != NOTICE_API_PATH:
            raise ValueError(f"{notice_type} 의 출처가 API 경로가 아닙니다.")
        field_meta = entry.get("field_meta")
        if not isinstance(field_meta, dict):
            raise ValueError(f"{notice_type} 의 field_meta 가 없습니다.")
        expected_meta = {
            str(field.get("fieldName") or "").strip(): {
                "fieldType": field.get("fieldType"),
                "fieldMaxLength": field.get("fieldMaxLength"),
                "fieldDescription": field.get("fieldDescription"),
                "fieldAddDescription": field.get("fieldAddDescription"),
            }
            for field in contents
            if isinstance(field, dict)
        }
        if field_meta != expected_meta:
            raise ValueError(f"{notice_type} 의 field_meta 가 정본 응답과 다릅니다.")

    unverified_by_type = {
        str(entry.get("type") or ""): entry for entry in unverified if isinstance(entry, dict)
    }
    if set(unverified_by_type) != set(UNVERIFIED_NOTICE_TYPES):
        raise ValueError("미검증 타입 집합이 보존되지 않았습니다.")
    for notice_type, entry in unverified_by_type.items():
        if entry.get("note") != UNVERIFIED_NOTICE_NOTE:
            raise ValueError(f"{notice_type} 의 미검증 근거 메모가 다릅니다.")
    if document.get("expected_total") != len(verified) + len(unverified):
        raise ValueError("expected_total 이 verified/unverified 합계와 다릅니다.")


def _load_json(path: str) -> object:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: str, document: dict) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(document, f, ensure_ascii=False, indent=2)
        f.write("\n")


# --------------------------------------------------------------------------- #
# 메인.
# --------------------------------------------------------------------------- #


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--notice-fixture",
        help="저장된 products-for-provided-notice 실응답 JSON. 지정 시 네트워크를 사용하지 않음.",
    )
    parser.add_argument(
        "--notice-output",
        default=NOTICE_TYPES_PATH,
        help="오프라인 고시 스펙 출력 경로 (기본: data/notice_types.json).",
    )
    parser.add_argument(
        "--notice-template",
        default=NOTICE_TYPES_PATH,
        help="정본에 없는 보조 메모와 기존 node/candidate 라벨을 보존할 notice_types.json 경로.",
    )
    parser.add_argument(
        "--validate-notice",
        help="기존 출력 파일을 픽스처와 대조만 하고 변경하지 않음 (--notice-fixture 필요).",
    )
    args = parser.parse_args(argv)
    if args.validate_notice and not args.notice_fixture:
        parser.error("--validate-notice 는 --notice-fixture 와 함께 사용해야 합니다.")
    return args


def _run_offline_notice(args: argparse.Namespace) -> None:
    response = _load_json(args.notice_fixture)
    if not isinstance(response, list):
        raise ValueError("--notice-fixture 파일은 JSON 배열이어야 합니다.")
    if args.validate_notice:
        candidate = _load_json(args.validate_notice)
        if not isinstance(candidate, dict):
            raise ValueError("--validate-notice 파일은 JSON 객체여야 합니다.")
        validate_notice_types_document(candidate, response)
        print(f"[notice fixture] 검증 성공: {args.validate_notice}", flush=True)
        return

    previous = _load_json(args.notice_template)
    if not isinstance(previous, dict):
        raise ValueError("--notice-template 파일은 JSON 객체여야 합니다.")
    document = build_notice_types_document(response, previous)
    _write_json(args.notice_output, document)
    print(
        f"[notice fixture] {len(document['verified'])} verified / "
        f"{len(document['unverified'])} unverified → {args.notice_output}",
        flush=True,
    )


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if args.notice_fixture:
        _run_offline_notice(args)
        return

    os.makedirs(DATA_DIR, exist_ok=True)
    tk = nc.get_token()

    print("[fetch_origin_and_notice_types] 원산지 코드 목록 조회 중...", flush=True)
    origin_doc = fetch_origin_areas(tk)
    with open(ORIGIN_PATH, "w", encoding="utf-8") as f:
        json.dump(origin_doc, f, ensure_ascii=False, indent=2)
    print(
        f"[fetch_origin_and_notice_types] 원산지 코드: {origin_doc['count']}개 "
        f"(최상위 {len(origin_doc['top_level'])} / 시·도 {len(origin_doc['state_level'])} / "
        f"시·군·구 {len(origin_doc['city_level'])}) → {ORIGIN_PATH}",
        flush=True,
    )

    print("[fetch_origin_and_notice_types] 고시 필드 타입 조회 중...", flush=True)
    notice_response = fetch_notice_response(tk)
    notice_doc = build_notice_field_types_document(notice_response)
    with open(NOTICE_FIELD_TYPES_PATH, "w", encoding="utf-8") as f:
        json.dump(notice_doc, f, ensure_ascii=False, indent=2)
    previous_notice_types = _load_json(NOTICE_TYPES_PATH)
    if not isinstance(previous_notice_types, dict):
        raise RuntimeError(f"notice_types.json 구조가 올바르지 않습니다: {NOTICE_TYPES_PATH}")
    regenerated_notice_types = build_notice_types_document(notice_response, previous_notice_types)
    _write_json(NOTICE_TYPES_PATH, regenerated_notice_types)
    print(
        f"[fetch_origin_and_notice_types] 고시 필드 타입: "
        f"{notice_doc['api_notice_type_count']} 타입 / "
        f"{len(notice_doc['field_types'])} 고유 필드명 → {NOTICE_FIELD_TYPES_PATH}; "
        f"고시 스펙 {len(regenerated_notice_types['verified'])} verified → {NOTICE_TYPES_PATH}",
        flush=True,
    )


if __name__ == "__main__":
    main()
