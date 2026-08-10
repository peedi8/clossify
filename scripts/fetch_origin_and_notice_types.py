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
NOTICE_TYPES_PATH = os.path.join(DATA_DIR, "notice_field_types.json")

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


def fetch_notice_field_types(tk: str) -> dict:
    """``GET /external/v1/products-for-provided-notice`` 수집.

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
    url = nc.BASE + "/external/v1/products-for-provided-notice"
    data = _get_with_retry(url, tk)
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
        "source": "GET /external/v1/products-for-provided-notice",
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


# --------------------------------------------------------------------------- #
# 메인.
# --------------------------------------------------------------------------- #


def main() -> None:
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
    notice_doc = fetch_notice_field_types(tk)
    with open(NOTICE_TYPES_PATH, "w", encoding="utf-8") as f:
        json.dump(notice_doc, f, ensure_ascii=False, indent=2)
    print(
        f"[fetch_origin_and_notice_types] 고시 필드 타입: "
        f"{notice_doc['api_notice_type_count']} 타입 / "
        f"{len(notice_doc['field_types'])} 고유 필드명 → {NOTICE_TYPES_PATH}",
        flush=True,
    )


if __name__ == "__main__":
    main()
