# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""등록 오케스트레이션.

원본 ``sourcing.py`` 의 등록 파이프라인을 이식하되, 핵심 개정사항을
반영한다:

1. **URL 입력 거부**: 요청 dict 에 ``url``/``source_url``/``item_url``/
   ``detail_url`` 중 하나라도 키가 존재하면 ``ValueError``. 값이 비어 있어도
   키 존재만으로 거부. payload 에 ``source_url`` 필드를 만들지 않는다.
2. **product_key**: 외부 마켓 ID(``num_iid`` 등)를 쓰지 않는다. 호출자가 주지
   않으면 ``sha1(상품명 + 가격)[:12]`` 로 생성. 빈 문자열/공백 키는 거부.
3. **가격 KRW 직입력**: 원가/환율/해외배송 기반 계산은 이식하지 않는다.
4. **이미지 파이프라인**: 리터치·시트 병합·상세 렌더 의존 지점은 명시 스텁
   (``NotImplementedError``).
5. **중복 정의 처리**: 원본에 같은 이름 함수가 두 번 정의된 것이 있으면
   뒤 정의만 가져간다 — 본 모듈에서는 ``inject_prepared_qa`` 의 단일 정의로
   통합하고 이 사실을 보고한다.
6. **prepared 포맷 버전**: ``common.PREPARED_PAYLOAD_VERSION`` 을 읽어 쓰되
   수정하지 않는다 (common 은 쓰기 범위 밖).

의존 방향: ``qa_agents``, ``category`` (상위) → ``register`` (본 모듈).
``common``, ``naver_client``, ``category_meta`` 는 어디서든 import 가능.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from . import common, qa_agents

# ---------------------------------------------------------------------------
# URL 입력 거부 (핵심 정책).
#
# 외부 마켓 수집은 본 제품에서 지원하지 않는다. ``url``/``source_url``/
# ``item_url``/``detail_url`` 키 중 하나라도 존재하면 거부한다 — 값이 비어
# 있어도 키 존재만으로 거부 (조용한 허용 금지).
# ---------------------------------------------------------------------------

_URL_INPUT_KEYS = frozenset({"url", "source_url", "item_url", "detail_url"})


def _reject_url_inputs(d):
    """URL 기반 수집 키가 존재하면 ``ValueError`` 를 발생시킨다.

    정책: "값이 비어 있어도 키 존재만으로 거부한다."
    """
    if isinstance(d, dict):
        present = sorted(k for k in _URL_INPUT_KEYS if k in d)
        if present:
            raise ValueError(
                "URL 기반 수집은 지원하지 않습니다. 상품 정보를 직접 전달하세요. "
                f"(감지된 키: {', '.join(present)})"
            )


# ---------------------------------------------------------------------------
# product_key 생성 (핵심 정책).
#
# 외부 마켓 ID 를 쓰지 않는다. 이름·가격만으로는 부족하다 — 색상만 다른 SKU
# 처럼 이름·가격이 같은 서로 다른 상품이 같은 키를 받으면 두 번째 준비가 첫
# 번째를 조용히 덮는다. 따라서 상품을 구별하는 입력(카테고리·이미지 소스 구성)을
# 키 유도에 포함한다. 빈 문자열/공백 키는 거부 (디렉터리 충돌·무음 덮어쓰기 방지).
#
# 하위호환: ``make_product_key(name, price)`` 2-인자 호출은 구별 입력이 없을 때의
# 기본 키(``sha1(name+price)[:12]``)를 그대로 반환한다. 구별 인자를 주면 그것들이
# 해시에 추가로 반영되어 같은 이름·가격이라도 서로 다른 키가 나온다.
# ---------------------------------------------------------------------------


def _sanitize_product_key(key):
    """product_key 를 파일시스템 안전 문자열로 정규화.

    ``[0-9A-Za-z_-]`` 외 문자는 ``_`` 로 치환, 80자로 절삭.
    """
    return re.sub(r"[^0-9A-Za-z_-]", "_", str(key or ""))[:80]


def _fingerprint_sources(category_id, image_sources):
    """카테고리·이미지 소스 구성을 안정적인 문자열로 직렬화한다.

    같은 이름·가격이라도 카테고리가 다르거나 이미지 소스 구성이 다르면 다른
    결과가 나와야 한다. 정렬하지 않고 입력 순서를 보존한다 — 이미지 순서 자체가
    대표 이미지 선택에 영향을 주므로 순서가 바뀌면 다른 상품으로 보는 것이
    안전하다(결정론 유지: 같은 입력은 같은 결과).
    """
    cat_part = str(category_id or "").strip()
    if isinstance(image_sources, list):
        src_parts = [str(s or "") for s in image_sources]
    else:
        src_parts = []
    return f"cat={cat_part}|srcs={','.join(src_parts)}"


def make_product_key(name, price, *, category_id=None, image_sources=None):
    """상품명 + 가격(+ 구별 입력) 으로 product_key 생성 (``sha1[:12]``).

    규칙:
      - 기본: ``sha1(상품명 + 가격)[:12]`` (하위호환 — 2-인자 호출).
      - 구별 입력 주어지면: ``sha1(상품명 + 가격 + 카테고리 + 이미지소스구성)[:12]``.
        이름·가격이 같아도 카테고리나 이미지 소스 구성이 다르면 다른 키가 나온다.
      - 같은 입력은 항상 같은 키를 낸다(결정론 — 재실행이 새 항목을 만들면 안 된다).

    Args:
        name: 상품명 (한국어).
        price: KRW 가격 (int/str).
        category_id: 카테고리 ID. 상품을 구별하는 입력.
        image_sources: 이미지 소스 리스트. 상품을 구별하는 입력.

    Returns:
        12자 hex product_key.

    Raises:
        ValueError: 상품명이 빈 문자열/공백인 경우.
    """
    name_str = str(name or "").strip()
    if not name_str:
        raise ValueError("product_key 생성에 필요한 상품명이 비어 있습니다 (빈 키 방지).")
    price_str = str(price if price is not None else "").strip()
    raw = f"{name_str}+{price_str}"
    # 구별 입력이 하나라도 주어지면 해시에 반영한다.
    if category_id is not None or image_sources is not None:
        raw += "+" + _fingerprint_sources(category_id, image_sources)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def resolve_product_key(d):
    """요청 dict 에서 product_key 를 결정.

    우선순위:
      1. ``d.product_key`` (호출자 명시)
      2. ``sha1(name + price)[:12]`` 자동 생성

    빈 문자열/공백 키는 ``ValueError`` 로 거부한다.
    """
    if isinstance(d, dict) and str(d.get("product_key") or "").strip():
        return _sanitize_product_key(d["product_key"])
    name = str(d.get("name") or d.get("title_ko") or "").strip() if isinstance(d, dict) else ""
    price = d.get("salePrice") if isinstance(d, dict) else None
    if price is None and isinstance(d, dict):
        price = d.get("sell_price") or d.get("price")
    if not name:
        raise ValueError("product_key 를 생성할 수 없습니다 — 상품명이 비어 있습니다.")
    return make_product_key(name, price)


# ---------------------------------------------------------------------------
# prepared payload 저장/로드.
#
# 원본은 ``PREPARED_DIR / <key> / payload.json`` 구조를 사용했다. 본 이식판도
# 동일 구조를 유지하되, ``common.PREPARED_PAYLOAD_VERSION`` 을 읽어 버전 스탬프로
# 쓴다 (common 은 쓰기 범위 밖 — 수정하지 않음).
# ---------------------------------------------------------------------------


def _prepared_dir():
    """prepared payload 저장 디렉터리(``common.PREPARED_DIR``) 반환."""
    return Path(common.PREPARED_DIR)


def _prepared_item_dir(product_key):
    """product_key 에 해당하는 prepared 아이템 디렉터리.

    경로 순회 검사: 결과가 ``PREPARED_DIR`` 하위인지 확인.
    """
    base = _prepared_dir()
    key = _sanitize_product_key(product_key)
    if not key:
        raise ValueError("product_key 가 비어 있습니다 (prepared 경로 불가).")
    item_dir = (base / key).resolve()
    base_resolved = base.resolve()
    try:
        item_dir.relative_to(base_resolved)
    except ValueError as exc:
        raise ValueError(f"product_key 가 prepared 디렉터리를 벗어납니다: {key}") from exc
    return item_dir


def _prepared_payload_path(product_key):
    """prepared payload JSON 파일 경로."""
    return _prepared_item_dir(product_key) / "payload.json"


def write_prepared_payload(payload):
    """prepared payload 를 디스크에 쓴다.

    payload 는 ``product_key`` 키를 포함해야 한다. 버전 스탬프로
    ``common.PREPARED_PAYLOAD_VERSION`` 을 읽어 ``version`` 필드에 설정한다.
    """
    if not isinstance(payload, dict):
        raise ValueError("payload 는 dict 여야 합니다.")
    key = str(payload.get("product_key") or "").strip()
    if not key:
        raise ValueError("payload 에 product_key 가 필요합니다.")
    path = _prepared_payload_path(key)
    payload = dict(payload)
    payload["version"] = common.PREPARED_PAYLOAD_VERSION
    payload["updated_at"] = _utc_now_iso()
    common._write_json_file(path, payload)
    return path


def read_prepared_payload(path):
    """prepared payload JSON 을 읽는다."""
    return common._read_json_file(path, None)


def load_prepared_payload(*, product_key=None):
    """product_key 로 prepared payload 를 로드.

    본 이식판은 product_key 만으로 로드한다 (외부 마켓 ID 불가,
    원본의 ``prepare_token`` 기반 접근 제어는 단순화했다).

    버전 검사: payload 의 ``version`` 이 ``common.PREPARED_PAYLOAD_VERSION``
    과 불일치하면 ``ValueError`` 로 명시 거부한다. 스키마 변경 후 조용한
    승격(fallback)을 허용하지 않는다. ``common.py`` 는 쓰기 범위 밖이므로
    상수는 읽어 비교만 한다.

    Raises:
        FileNotFoundError: payload 가 없을 때.
        ValueError: product_key 가 비었거나 version 이 불일치할 때.
    """
    key = str(product_key or "").strip()
    if not key:
        raise ValueError("product_key 가 필요합니다.")
    path = _prepared_payload_path(key)
    data = read_prepared_payload(path)
    if data is None:
        raise FileNotFoundError(f"prepared payload 를 찾을 수 없습니다: {path}")
    # version 검사 — 불일치 시 명시 예외(조용한 승격 금지).
    payload_version = data.get("version") if isinstance(data, dict) else None
    expected = common.PREPARED_PAYLOAD_VERSION
    if payload_version != expected:
        raise ValueError(
            f"prepared payload version 불일치: 기대={expected}, "
            f"실제={payload_version!r} (product_key={key}). "
            "스키마가 변경되었으므로 조용한 승격 없이 거부한다."
        )
    return data


def iter_prepared_payload_paths():
    """모든 prepared payload 경로를 mtime 역순으로 yield."""
    base = _prepared_dir()
    if not base.exists():
        return []
    paths = sorted(base.glob("*/payload.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return paths


def _utc_now_iso():
    """현재 UTC 시각을 ISO 8601 문자열로."""
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 등록 기록(record) 저장/조회.
#
# 네이버 상태 변경(수정/삭제/상태보정) 은 **채널상품번호**(``channelProductNo``)
# 를 요구한다. 그 번호는 **등록 응답에만** 들어 있고 ``get_product``(origin-products
# 조회) 응답에는 없다. 따라서 등록 직후에 그 번호를 디스크에 남겨두지 않으면 이후
# 그 상품을 다시 손댈 방법이 사라진다.
#
# 저장 위치는 prepared payload 가 사는 디렉터리 규약을 따른다(새 규약 만들지 않음).
# 같은 product_key 하위에 ``registration_record.json`` 파일로 기록한다. 이후 수정
# 기능이 올라탈 자리다.
# ---------------------------------------------------------------------------


def _registration_record_path(product_key):
    """등록 기록 JSON 파일 경로(prepared 디렉터리 규약 하위).

    같은 product_key 의 prepared payload 옆에 둔다 — 새 디렉터리 규약을 만들지
    않는다. 경로 순회 검사는 ``_prepared_item_dir`` 이 이미 수행한다.
    """
    return _prepared_item_dir(product_key) / "registration_record.json"


def _extract_channel_product_no(body):
    """등록 응답 본문에서 ``channelProductNo`` 추출.

    네이버 커머스 API 등록 응답의 구조 변형을 고려해 여러 자리를 찾는다:
      - ``body.smartstoreChannelProductNo`` (실등록 관측 응답 — 최상위 키).
      - ``body.channelProductNo``
      - ``body.channelProduct.channelProductNo``
      - ``body.originProduct.channelProductNo``
      - ``body.smartstoreChannelProduct.channelProductNo``

    실등록 응답은 ``originProductNo`` 와 **나란히** ``smartstoreChannelProductNo``
    를 최상위에 둔다. 이 키를 인식하지 못하면 상태 변경에 필요한 채널상품번호가
    응답에 있음에도 누락된다 — 본 함수가 존재하는 이유 자체가 사라진다.
    기존 후보 키들은 폴백으로 그대로 둔다(다른 응답 형태를 가정한 기존 테스트 호환).
    """
    if not isinstance(body, dict):
        return None
    smartstore_direct = body.get("smartstoreChannelProductNo")
    if smartstore_direct:
        return smartstore_direct
    direct = body.get("channelProductNo")
    if direct:
        return direct
    for nested_key in ("channelProduct", "originProduct", "smartstoreChannelProduct"):
        nested = body.get(nested_key)
        if isinstance(nested, dict) and nested.get("channelProductNo"):
            return nested["channelProductNo"]
    return None


def write_registration_record(
    product_key,
    *,
    origin_product_no,
    channel_product_no,
    name,
    sale_price,
    category_id,
    requested_status,
    applied_status,
    deferred_notice_fields=None,
    deferred_rejected=None,
    notice_type=None,
):
    """등록 결과를 디스크에 기록한다.

    저장 위치는 prepared payload 가 사는 디렉터리 규약을 따른다(새 규약 금지).
    같은 ``product_key`` 하위의 ``registration_record.json``.

    **빈 값 가드**: ``channel_product_no`` 가 없으면 **조용히 넘기지 않는다** —
    반환 dict 에 ``channel_product_no: None`` 과 ``missing_channel_no: True`` 를
    드러낸다(이후 수정이 불가능해진다는 뜻이므로 사용자가 알아야 한다). 파일은
    채널번호가 있을 때만 기록한다.

    **미루기 정보 기록**: 판매자가 "상세페이지 참조" 로 미루기로 선언한 고시
    필드(``deferred_notice_fields``) 와 미루려 했으나 거부된 목록
    (``deferred_rejected``), 그때의 고시 타입(``notice_type``) 을 항상 기록한다.
    미루기가 없어도 **빈 리스트를 명시적으로** 남긴다(키 자체가 없는 것과
    "미룬 것 없음" 을 구별 — ``missing_channel_no`` 와 같은 결). 이 키들이 없는
    구형 기록은 ``summarize_recorded_deferred`` 가 "기록되지 않음(이 변경 이전)"
    로 구별해 보고한다.

    Returns:
        기록 결과 dict::
            {"written": bool, "path": str | None,
             "channel_product_no": str | None, "missing_channel_no": bool}
    """
    key = str(product_key or "").strip()
    if not key:
        raise ValueError("product_key 가 필요합니다 (등록 기록 저장).")
    sane_key = _sanitize_product_key(key)
    ch_no = str(channel_product_no or "").strip() or None
    _sane_deferred = [
        str(f or "").strip() for f in (deferred_notice_fields or []) if str(f or "").strip()
    ]
    _sane_rejected = [
        str(f or "").strip() for f in (deferred_rejected or []) if str(f or "").strip()
    ]
    record = {
        "product_key": sane_key,
        "origin_product_no": origin_product_no,
        "channel_product_no": ch_no,
        "name": name,
        "salePrice": sale_price,
        "categoryId": category_id,
        "requested_status": requested_status,
        "applied_status": applied_status,
        # 미루기 정보 — 키 항상 존재(조용한 빈 값 금지). 없으면 빈 리스트.
        "deferred_notice_fields": _sane_deferred,
        "deferred_rejected": _sane_rejected,
        "notice_type": str(notice_type).strip() if str(notice_type or "").strip() else None,
        "registered_at": _utc_now_iso(),
    }
    path = _registration_record_path(sane_key)
    common._write_json_file(path, record)
    return {
        "written": True,
        "path": str(path),
        "channel_product_no": ch_no,
        "missing_channel_no": ch_no is None,
    }


def read_registration_record(*, product_key=None, origin_product_no=None):
    """저장된 등록 기록을 읽는다(내부 헬퍼 — MCP 도구로 노출하지 않는다).

    ``product_key`` 또는 ``origin_product_no`` 중 하나를 받는다.
    ``product_key`` 가 주어지면 그 키 하위의 기록 파일을 직접 읽는다.
    ``origin_product_no`` 만 주어지면 prepared 디렉터리를 순회하며
    ``origin_product_no`` 가 일치하는 기록을 찾는다(느리지만 이후 수정 기능의
    폴백 경로가 된다).

    Returns:
        기록 dict. 없으면 ``None``.

    Raises:
        ValueError: 인자가 모두 비어 있을 때.
    """
    pkey = str(product_key or "").strip()
    origin_no = str(origin_product_no or "").strip()
    if not pkey and not origin_no:
        raise ValueError(
            "read_registration_record 는 product_key 또는 origin_product_no 가 필요합니다."
        )
    if pkey:
        path = _registration_record_path(pkey)
        data = common._read_json_file(path, None)
        return data
    # origin_product_no 만 있는 경우: prepared 디렉터리 순회.
    base = _prepared_dir()
    if not base.exists():
        return None
    for key_dir in base.iterdir():
        if not key_dir.is_dir():
            continue
        record_path = key_dir / "registration_record.json"
        data = common._read_json_file(record_path, None)
        if isinstance(data, dict) and str(data.get("origin_product_no") or "") == origin_no:
            return data
    return None


def summarize_recorded_deferred(record):
    """등록 기록에 남은 미루기 정보를 상태 구별과 함께 요약한다.

    서로 다른 상태를 같은 값으로 뭉개지 않는다(조용한 폴백 금지):
      - 기록 파일 자체가 없음(``record is None``) → ``record_present: False``.
      - 기록은 있으나 미루기 키가 없음(이 변경 이전 기록) →
        ``deferred_recorded: False`` + 안내 문구. "미루기 0건" 으로 단정하지
        않는다.
      - 새 형식 기록 → 실제 목록·고시 타입을 그대로 돌려준다(빈 리스트는
        "미룬 것 없음" 의 명시적 표현).

    Args:
        record: ``read_registration_record`` 반환 dict(없으면 ``None``).

    Returns:
        ``{record_present, deferred_recorded, deferred_notice_fields,
        deferred_rejected, notice_type, note}`` dict.
    """
    if not isinstance(record, dict):
        return {
            "record_present": False,
            "deferred_recorded": False,
            "deferred_notice_fields": [],
            "deferred_rejected": [],
            "notice_type": None,
            "note": "등록 기록 파일이 없다 — 미루기 정보를 알 수 없다.",
        }
    if "deferred_notice_fields" not in record or "deferred_rejected" not in record:
        return {
            "record_present": True,
            "deferred_recorded": False,
            "deferred_notice_fields": [],
            "deferred_rejected": [],
            "notice_type": None,
            "note": "미루기 정보가 기록되지 않음(이 변경 이전 기록) — 0건으로 단정할 수 없다.",
        }
    _deferred = record.get("deferred_notice_fields")
    _rejected = record.get("deferred_rejected")
    return {
        "record_present": True,
        "deferred_recorded": True,
        "deferred_notice_fields": list(_deferred) if isinstance(_deferred, list) else [],
        "deferred_rejected": list(_rejected) if isinstance(_rejected, list) else [],
        "notice_type": record.get("notice_type") if "notice_type" in record else None,
        "note": None,
    }


# ---------------------------------------------------------------------------
# 등록 오케스트레이션.
# ---------------------------------------------------------------------------


def _build_product_dict(d, seo_title, category_id):
    """Naver 등록용 상품 dict 구성.

    반영 규칙:
      - 가격은 KRW 직접 입력 (``d.salePrice`` 또는 ``d.sell_price``).
      - ``num_iid``/외부 마켓 ID 사용 금지 — modelName 은 config/입력만.
      - notice 기본값은 ``naver_client._notice_defaults`` 에 위임 (KC/원산지
        하드코딩 금지).
    """
    if not isinstance(d, dict):
        raise ValueError("상품 입력은 dict 여야 합니다.")
    name = str(seo_title or d.get("name") or d.get("title_ko") or "").strip()
    if not name:
        raise ValueError("상품명이 비어 있습니다.")
    sale_price = d.get("salePrice")
    if sale_price is None:
        sale_price = d.get("sell_price") or d.get("price")
    if sale_price is None:
        raise ValueError("salePrice(KRW) 가 필요합니다.")
    result = {
        "name": name[:50],
        "categoryId": str(category_id or d.get("categoryId") or d.get("category_id") or ""),
        "salePrice": int(sale_price),
        "options": d.get("options") or [],
        "tags": d.get("tags") or [],
        "courier": d.get("courier") or "",
        "notice": d.get("notice") or {},
        "as_tel": d.get("as_tel") or "",
        "seller_tel": d.get("seller_tel") or "",
        "as_guide": d.get("as_guide") or "",
        "origin_code": d.get("origin_code") or "",
        "manufacturer": d.get("manufacturer") or "",
        "importer": d.get("importer") or "",
    }
    # delivery_fee: 키가 있을 때만 넣는다 (기본값 3000 은 _notice_defaults
    # 한 곳에서만 결정 — 키가 없으면 config 폴백이 발동해야 한다).
    if "delivery_fee" in d:
        result["delivery_fee"] = d.get("delivery_fee")
    return result


def _apply_qa_to_payload(payload, qa_result):
    """QA 집계 결과를 payload 에 붙인다."""
    if not isinstance(payload, dict):
        return payload
    payload["qa"] = qa_result
    return payload


def _build_register_product_dict(d, name, category_id, *, resolved_tags=None):
    """register 단계가 naver_client.build_payload 에 넘길 상품 dict 와 동일한 형태를 구성.

    준비 단계의 컴플라이언스 검사가 등록 단계와 *같은 해석* 을 보려면, 컴플라이언스에
    넘기는 임시 페이로드를 register 단계가 만드는 것과 같은 빌더(``naver_client.
    build_payload``)로 만들어야 한다. 본 함수는 그 빌더에 들어갈 상품 dict 를
    ``mcp_server.register_product`` 와 동일한 키 셋으로 조립한다.

    빌더 자체는 호출하지 않고 dict 만 반환한다(호출은 호출자의 책임). 상품명 50자
    절단은 빌더 내부에서 이뤄지므로 여기서는 원본 이름을 그대로 둔다.

    Args:
        resolved_tags: 준비 단계에서 ``_resolve_tags`` 가 산출한 최종 태그 리스트.
            주어지면 ``d.tags`` 대신 이 값을 쓴다 — 컴플라이언스 검사가 등록 시
            실제로 들어갈 태그(추천·제한 검사 통과한)와 같은 태그를 보게 한다.
            None 이면 기존대로 ``d.tags`` 를 읽는다(하위호환).
    """
    sale_price = d.get("salePrice")
    if sale_price is None:
        sale_price = d.get("sell_price") or d.get("price")
    if resolved_tags is not None:
        tags_value = list(resolved_tags)
    else:
        tags_value = list(d.get("tags") or [])
    product = {
        "name": name,
        "categoryId": str(category_id or d.get("categoryId") or d.get("category_id") or ""),
        "salePrice": int(sale_price),
        "tags": tags_value,
        "stock": int(d.get("stock", 1)),
        "courier": d.get("courier") or "",
    }
    # delivery_fee: 실질값이 있을 때만 넣는다 (기본값 3000 은 _notice_defaults
    # 한 곳에서만 결정 — 키가 없거나 빈 값이면 config 폴백이 발동해야 한다).
    # 빈 선택 필드(None/""/공백)가 컴플라이언스 실파냐 수준의 예외로 둔갑하면
    # 안 된다 — _resolve_delivery_fee_with_slot 은 None/"" 을 "생략" 으로 본다.
    # 같은 값을 두 곳이 다르게 보는 것(2라운드 감리 ① 의 재발 방지).
    # **5라운드 감리 ⑤**: 진입점에서 ``int()`` 로 깎지 않는다 — 소수점(3000.5)
    # 이 ``int()`` 로 잘려서 통과하는 것을 막는 가드가 정본 해석기에 있는데,
    # 여기서 미리 깎으면 가드가 볼 게 없다. 원값을 그대로 넘긴다.
    raw_fee = d.get("delivery_fee")
    if raw_fee is not None and str(raw_fee).strip():
        product["delivery_fee"] = raw_fee
    if d.get("options"):
        product["options"] = d.get("options")
    notice = d.get("notice")
    if notice is not None:
        product["notice"] = notice
    # 원산지/AS/제조사/수입자 등 규제값 — 빌더가 config 폴백으로 읽는 후보 키.
    # 진짜 조립기(naver_client._notice_defaults/_resolve_*)가 읽는 키 목록
    # (snake_case/camelCase 별칭 포함)을 공유해 두 조립기의 입력 규약이
    # 갈라지지 않게 한다(T3-A). 판매자가 camelCase 로 준 고시값이 임시
    # 조립기에서만 사라지는 결함의 재발 방지.
    from .naver_client import NOTICE_INPUT_KEY_ALIASES as _notice_input_keys

    for key in _notice_input_keys:
        value = d.get(key)
        if value:
            product[key] = value
    # 타입별 고시 필드(releaseDateText/size 등) — 정본이 정의한 필드의 입력
    # 키 후보 합집합(naver_client.notice_typed_input_keys — 정본에서 읽는
    # 공유 헬퍼)을 통째로 넘긴다. 진짜 조립기(build_payload → _product_info
    # _notice → _carry_typed_fields_from_input)가 해당 타입의 정본 필드만
    # 걸러 싣으므로 두 조립기의 본문이 같아진다(T3 구조 — 임시 조립기에서만
    # 타입별 필드가 사라지는 결함의 재발 방지).
    from .naver_client import notice_typed_input_keys as _typed_keys

    for key in _typed_keys():
        if key in product:
            continue
        value = d.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        product[key] = value
    return product


def _build_tentative_register_payload(
    d, name, category_id, listing_urls, detail_html, *, resolved_tags=None
):
    """등록 단계가 만들 페이로드를 임시로 빌드한다 (컴플라이언스 검사용).

    ``naver_client.build_payload`` 는 등록 단계와 *동일한* 규제값 해석(origin/AS/
    고시 기본값/공통 5필드 포함)을 페이로드에 반영한다. 본 함수로 그 빌더를 한 번
    호출해 임시 페이로드를 만들면, 준비 단계의 컴플라이언스 검사가 등록 시 실제로
    만들어질 값과 동일한 문맥을 보게 된다 — 두 단계가 어긋날 수 없다.

    Args:
        resolved_tags: ``_resolve_tags`` 가 산출한 최종 태그. 주어지면 컴플라이언스
            검사용 임시 페이로드에 같은 태그를 넣는다(prepare_listing 본체에서
            산출한 최종 태그와 컴플라이언스 문맥을 일치시킨다).

    Raises:
        ValueError: 필수 설정(원산지 등)이 없어 빌더가 페이로드를 만들 수 없을 때.
            호출자는 이것을 컴플라이언스 위반 + needs_user 로 번역해야 한다
            (준비 단계에서 예외가 그대로 터지면 안 된다).
    """
    from . import naver_client as _nc

    product = _build_register_product_dict(d, name, category_id, resolved_tags=resolved_tags)
    status = d.get("status") or "SALE"
    return _nc.build_payload(product, detail_html, listing_urls, status=status)


def _category_path_for(category_id):
    """``category_id`` 의 카테고리 경로를 반환 (알 수 없으면 빈 문자열).

    준비 단계와 등록 단계가 고시 타입 추론을 같은 입력으로 하게 한다.
    등록 단계(``mcp_server._category_path_for``)와 *동일한* lookup 을 쓴다.

    **조용한 ETC 강등 금지.** 과거에는 모든 예외를 잡아 빈 문자열로
    떨어뜨렸고, 이 빈 문자열은 ``_infer_notice_type`` 에서 ETC 기본값으로
    해석되었다. 이는 카테고리 메타 데이터 파일이 부재하거나 깨진 경우(인프라
    실패)를 "정말 ETC 인 카테고리" 와 구분하지 못하는 근본 결함이다 —
    결과적으로 잘못된 고시 타입으로 규제 필드를 신고하게 된다.

    이제 ``CategoryMetaUnavailableError`` (데이터 파일 부재/손상) 를 잡아 빈
    문자열로 강등하지 않고 그대로 전파한다. 호출자(``prepare_listing`` 의
    try/except 블록) 가 이를 컴플라이언스 FAIL 로 번역한다 — 알 수 없음을
    알 수 없음으로 다룬다(fail-closed).

    ``raise_if_unknown=False`` 이므로 알 수 없는 카테고리 ID 는 예외 없이 빈
    문자열을 반환한다. 이 경로는 "메타 데이터는 있지만 해당 ID 가 없다" 는
    뜻이므로 ETC 기본값이 합리적이다.

    Raises:
        category_meta.CategoryMetaUnavailableError: 데이터 파일이 부재하거나
            읽을 수 없는 경우. 호출자가 컴플라이언스 FAIL 로 번역한다.
    """
    from . import category_meta

    return category_meta.category_path(category_id, raise_if_unknown=False)


def _inject_notice_type(payload, inferred_type):
    """``inferred_type`` 을 payload 의 notice 에 반영한다.

    등록 단계(``mcp_server._build_compliance_context``)가 하는 보정을 그대로
    적용한다: 카테고리 경로에서 추론한 고시 타입이 ETC 가 아니고, payload 의
    notice 가 ETC(또는 미설정)로 되어 있으면 추론된 타입으로 덮어쓴다.
    ``naver_client.build_payload`` 가 FURNITURE 외 카테고리에 대해 ETC 를
    하드코딩하더라도, 실제 의류/신발/가구 등 카테고리는 다른 필수 필드를
    요구하므로 올바른 타입으로 보정해야 한다.

    payload 는 복사해서 반환한다(입력을 변이하지 않는다). 구조가 예상과 다르면
    입력을 그대로 반환한다(조용한 승격 금지 — 구조가 깨졌으면 호출자의 예외
    경로가 작동한다).
    """
    if not isinstance(payload, dict):
        return payload
    if inferred_type == "ETC":
        return payload
    origin_product = payload.get("originProduct")
    if not isinstance(origin_product, dict):
        return payload
    detail_attr = origin_product.get("detailAttribute")
    if not isinstance(detail_attr, dict):
        return payload
    notice = detail_attr.get("productInfoProvidedNotice")
    if not isinstance(notice, dict):
        return payload
    current_type = str(notice.get("productInfoProvidedNoticeType") or "").strip().upper()
    if current_type != "ETC" and current_type:
        return payload
    new_payload = dict(payload)
    new_op = dict(origin_product)
    new_da = dict(detail_attr)
    new_notice = dict(notice)
    new_notice["productInfoProvidedNoticeType"] = inferred_type
    # 추론된 타입의 node 키가 없으면 빈 dict 를 추가한다(필수 필드 검사가
    # 올바른 node 에서 이루어지게). 등록 단계(_build_compliance_context)와
    # 동일한 동작.
    spec = qa_agents._notice_type_spec(inferred_type)
    expected_node = (spec or {}).get("node")
    if expected_node and expected_node not in new_notice:
        # 기존 etc/furniture 노드의 필드를 올바른 노드로 복사(최선 노력).
        for fallback_key in ("etc", "furniture"):
            fb = new_notice.get(fallback_key)
            if isinstance(fb, dict):
                new_notice[expected_node] = dict(fb)
                break
        else:
            new_notice[expected_node] = {}
    new_da["productInfoProvidedNotice"] = new_notice
    new_op["detailAttribute"] = new_da
    new_payload["originProduct"] = new_op
    return new_payload


def register_prepared_listing(d):
    """prepared payload 를 로드해 네이버 상품 등록을 수행.

    스키마 확정 반영:
      - payload 의 ``images.listing_urls`` / ``images.detail_urls`` (CDN URL) 을
        읽는다(예전 ``listing_image_paths``/``detail_segment_paths`` 경로 기반
        키는 더 이상 사용하지 않는다).
      - ``detail_html`` 필드를 읽어 그대로 네이버 페이로드에 넣는다.
      - **이미지 0장 거부**: ``listing_urls`` 가 비어 있으면 ``ValueError``
        (무음 통과 금지).

    흐름:
      1. URL 입력 거부 검사
      2. ``product_key`` 결정
      3. prepared payload 로드 (version 검사 포함)
      4. 이미지 0장 거부 검사
      5. QA 게이트 판정 (``qa_agents.qa_gate``) — PENDING/FAIL 차단
      6. ``naver_client.build_payload`` + ``register_product`` 호출
      7. 등록 후 조회 재검증 (``naver_client.get_product``)

    Returns:
        결과 dict (``ok``, ``originProductNo``, ...).
    """
    _reject_url_inputs(d)
    product_key = resolve_product_key(d)
    payload = load_prepared_payload(product_key=product_key)

    # --- payload 스키마: images.listing_urls / images.detail_urls ---
    # prepared 에서 가져온 이미지도 명시 입력과 *동일한 검증* 을 통과해야 한다.
    # 무효 항목을 조용히 걸러내면 2번 이미지가 대표 이미지로 승격되는
    # 조용한 치환이 일어난다. 따라서 원본 리스트를 정규화 없이 정본 검증기에
    # 그대로 넘겨 무효 항목이 하나라도 섞이면 거부한다 (filter-not-fix).
    from . import naver_client as _nc_for_validation

    images_block = payload.get("images") or {}
    if not isinstance(images_block, dict):
        images_block = {}
    raw_listing_urls = images_block.get("listing_urls") or []
    raw_detail_urls = images_block.get("detail_urls") or []
    # 정본 검증기 재사용 — 새 검증 함수를 만들지 않는다.
    # 무효 항목이 섞이 있으면 ValueError 로 거부 (조용한 필터링 금지).
    _nc_for_validation._require_original_images(raw_listing_urls)

    listing_urls = [str(u).strip() for u in raw_listing_urls if isinstance(u, str) and u.strip()]
    detail_urls = [str(u).strip() for u in raw_detail_urls if isinstance(u, str) and u.strip()]

    # 이미지 0장 거부 (무음 통과 금지) — 정본 검증기 통과 후에도
    # 빈 리스트 케이스(전부 공백 등)를 명시적으로 거부한다.
    if not listing_urls:
        raise ValueError(
            "prepared payload 에 리스팅 이미지(listing_urls)가 0장입니다. "
            "등록을 거부한다(무음 통과 금지). "
            f"product_key={product_key}"
        )

    # QA 게이트 — fail-closed (PENDING/FAIL 차단).
    allowed, reason = qa_agents.qa_gate(payload)
    if not allowed:
        return {
            "ok": False,
            "blocked": True,
            "reason": reason,
            "product_key": product_key,
            "channelProductNo": None,
            "channel_product_no": None,
            "missing_channel_no": True,
        }

    product = payload.get("product") or {}
    # prepared detail_html 를 그대로 사용(payload 의 detail_html).
    detail_html = str(payload.get("detail_html") or "")
    if not detail_html:
        # detail_html 이 없으면 등록 거부(무음 통과 금지).
        raise ValueError(
            "prepared payload 에 detail_html 이 없습니다. " f"product_key={product_key}"
        )

    from . import naver_client as nc

    status = d.get("status") or payload.get("status") or "SALE"
    # naver_client.build_payload 는 image_urls(리스팅) 리스트를 받는다.
    # detail_urls 는 현재 naver_client 가 별도 슬롯을 요구하지 않으므로
    # listing_urls 와 합쳐 전달하되 첫 번째가 대표인 순서는 보존한다.
    all_urls = list(listing_urls) + [u for u in detail_urls if u not in listing_urls]
    api_payload = nc.build_payload(product, detail_html, all_urls, status=status)

    # 등록 API 호출.
    result = nc.register_product(api_payload)
    if isinstance(result, tuple):
        status_code, body = result
    else:
        status_code, body = 200, result

    ok = _is_register_success(status_code, body)
    origin_product_no = _extract_origin_product_no(body)
    channel_product_no = _extract_channel_product_no(body)
    missing_channel_no = channel_product_no is None

    # 등록 후 조회 재검증 (원본에 없는 단계).
    verify = None
    if ok and origin_product_no:
        try:
            verify_status, verify_body = nc.get_product(origin_product_no)
            verify = {
                "status_code": verify_status,
                "ok": verify_status == 200,
                "body": verify_body if verify_status == 200 else None,
            }
        except Exception as exc:
            # 재검증 실패 — fail-closed: 조용히 PASS 로 넘기지 않는다.
            verify = {"ok": False, "error": str(exc)}

    # 결과를 payload 에 기록.
    payload["registration"] = {
        "ok": ok,
        "status_code": status_code,
        "originProductNo": origin_product_no,
        "channelProductNo": channel_product_no,
        "verify": verify,
    }
    try:
        write_prepared_payload(payload)
    except Exception:
        pass

    # 등록 기록(record) 저장 — 채널상품번호를 디스크에 남겨 이후 수정이 가능하게.
    # 빈 값 가드: 채널번호가 없으면 조용히 넘기지 않는다(missing_channel_no 드러남).
    registration_record = None
    if ok and origin_product_no:
        _prod = product if isinstance(product, dict) else {}
        try:
            registration_record = write_registration_record(
                product_key,
                origin_product_no=origin_product_no,
                channel_product_no=channel_product_no,
                name=str(_prod.get("name") or ""),
                sale_price=_prod.get("salePrice"),
                category_id=str(_prod.get("categoryId") or ""),
                requested_status=status,
                applied_status=status,
                # 미루기 정보 — prepared payload 에 준비 단계가 남긴 값 그대로.
                # deferred_notice_fields 키가 없는 구형 prepared 는 빈 리스트로
                # 기록한다(그때 실제로 미루기가 없었다).
                deferred_notice_fields=payload.get("deferred_notice_fields") or [],
                deferred_rejected=payload.get("deferred_rejected") or [],
                notice_type=payload.get("notice_type"),
            )
        except Exception:
            # 기록 저장 실패가 등록 자체를 실패시키지는 않는다 — 하지만 채널번호가
            # 있음에도 기록을 못 쓰면 이후 수정이 불가능해지므로 그 사실은
            # missing_channel_no 와 별개로 반환에 드러나지 않는다(이미 ok 로
            # 보고됨). 단, 기록 파일이 없으면 read_registration_record 가 None 을
            # 반환하므로 이후 수정 기능이 안전하게 차단된다.
            registration_record = None

    return {
        "ok": ok,
        "status_code": status_code,
        "originProductNo": origin_product_no,
        "channelProductNo": channel_product_no,
        "channel_product_no": channel_product_no,
        "missing_channel_no": missing_channel_no,
        "verify": verify,
        "product_key": product_key,
        "registration_record": registration_record,
        "body": body if not ok else None,
    }


def _is_register_success(status_code, body):
    """등록 응답이 성공인지 판정."""
    if status_code != 200:
        return False
    if isinstance(body, dict):
        return bool(body.get("originProduct") or body.get("originProductNo"))
    return False


def _extract_origin_product_no(body):
    """등록 응답에서 ``originProductNo`` 추출."""
    if not isinstance(body, dict):
        return None
    direct = body.get("originProductNo")
    if direct:
        return direct
    origin_product = body.get("originProduct")
    if isinstance(origin_product, dict):
        return origin_product.get("id") or origin_product.get("originProductNo")
    return None


def register_listing(d):
    """하위호환 등록 디스패처.

    반영 규칙:
      - URL 입력은 ``_reject_url_inputs`` 에서 이미 거부됨.
      - ``prepare_listing`` 본체는 별도 구현되어 있다. 본 함수는
        product_key 가 있거나 name+salePrice 가 있을 때 등록(commit)만 수행.
      - 그 외의 경우 ``prepare_listing`` 을 먼저 호출하라는 안내와 함께
        ``ValueError`` (무동작 스텁 금지).

    Args:
        d: 요청 dict. ``product_key`` 또는 ``name``+``salePrice`` 필요.

    Raises:
        ValueError: URL 키 존재, 또는 product_key 생성 불가.
    """
    _reject_url_inputs(d)
    if d.get("product_key") or (d.get("name") and d.get("salePrice") is not None):
        return register_prepared_listing(d)
    raise ValueError(
        "register_listing 은 product_key 가 있거나 name+salePrice 가 있을 때만 "
        "register_prepared_listing 으로 위임합니다. prepared payload 가 없으면 "
        "먼저 prepare_listing 을 호출해 payloads 를 만드세요."
    )


# ---------------------------------------------------------------------------
# 위임 왕복 검증 공유 헬퍼 + submit_reviews.
#
# 신뢰 모델(타협 불가):
#   - 제출 가능 agent 는 {"image","copy"} 로 고정. compliance 제출은 ValueError.
#   - verdict 허용값은 {"PASS","WARN","FAIL","PENDING"} 로 엄격 검증. 그 외/누락은
#     ValueError (기본값 WARN 으로 떨어뜨리지 말 것).
#   - 병합은 "더 나쁜 쪽" 채택(FAIL > PENDING > WARN > PASS). 서버 violations 절대
#     삭제 금지. 클라이언트는 PENDING → PASS 로만 상향 가능, FAIL → PASS 불가.
#   - 무검증 형제 문(inject_prepared_qa)는 본 검증 경로를 공유하도록 봉인한다.
# ---------------------------------------------------------------------------

# 제출 가능 agent 이름(실제 사용 중인 이름).
_SUBMITTABLE_AGENTS = frozenset({"image", "copy"})

# verdict 순위(나쁠수록 큼). qa_agents._VERDICT_RANK 와 동일 기준.
_VERDICT_RANK = {"PASS": 0, "WARN": 1, "PENDING": 2, "FAIL": 3}


def _worse_verdict(server_verdict, client_verdict):
    """서버 verdict 와 클라이언트 verdict 를 병합한다.

    신뢰 모델(타협 불가):
      - **PENDING → PASS 허용**: 서버 verdict 가 PENDING(미회신) 이면 클라이언트
        회신을 그대로 채택한다. PENDING 은 "아직 검수 안 됨" 이지 "나쁨" 이
        아니므로, 클라이언트가 PASS/WARN/FAIL 어떤 것이든 회신하면 그것이
        최종 verdict 가 된다("PENDING → PASS 로만 상향할 수 있다").
      - **FAIL → PASS 차단**: 서버 verdict 가 PENDING 이 아니면(FAIL/WARN/PASS)
        더 나쁜 쪽을 채택한다("FAIL → PASS 는 불가능하다").

    예시:
      - (PENDING, PASS) → PASS  ✓ 업그레이드 허용
      - (PENDING, FAIL) → FAIL  ✓ 클라이언트가 더 나쁘면 그대로 채택
      - (FAIL,    PASS) → FAIL  ✓ 다운그레이드 차단
      - (WARN,    PASS) → WARN  ✓ 더 나쁜 쪽
      - (PASS,    FAIL) → FAIL  ✓ 더 나쁜 쪽
    """
    sv = str(server_verdict or "").strip().upper()
    cv = str(client_verdict or "").strip().upper()
    # 서버가 PENDING(미회신)이면 클라이언트 회신을 채택한다(PENDING → PASS 허용).
    if sv == qa_agents.PENDING:
        return client_verdict
    # 그 외(FAIL/WARN/PASS)는 더 나쁜 쪽을 채택한다(FAIL → PASS 차단).
    ra = _VERDICT_RANK.get(sv, 1)
    rb = _VERDICT_RANK.get(cv, 1)
    return server_verdict if ra >= rb else client_verdict


def _validate_review_submission(d):
    """``submit_reviews`` / ``inject_prepared_qa`` 공유 검증 경로.

    agent 이름과 verdict 값을 엄격 검증한다. 검증 실패 시
    ``ValueError``. 기본값으로 떨어뜨리지 않는다.

    Returns:
        ``(product_key, payload, normalized_reviews)`` —
        ``normalized_reviews`` 는 ``[{"agent": str, "verdict": str,
        "violations": [...], "summary": str}, ...]``.
    """
    _reject_url_inputs(d)
    if not isinstance(d, dict):
        raise ValueError("검수 제출은 dict 여야 합니다.")
    product_key = resolve_product_key(d)
    payload = load_prepared_payload(product_key=product_key)

    reviews = d.get("reviews")
    if not isinstance(reviews, list) or not reviews:
        # reviews 리스트가 없으면 image/copy 개별 키를 허용(편의).
        reviews = []
        for agent_name in ("image", "copy"):
            row = d.get(agent_name)
            if isinstance(row, dict) or isinstance(row, str):
                reviews.append(
                    {**row} if isinstance(row, dict) else {"agent": agent_name, "verdict": row}
                )
    if not reviews:
        raise ValueError(
            "검수 제출(reviews)이 비어 있습니다. image/copy agent 의 verdict 를 " "전달해야 합니다."
        )

    normalized = []
    for row in reviews:
        if not isinstance(row, dict):
            raise ValueError(f"검수 항목은 dict 여야 합니다: {row!r}")
        agent_name = str(row.get("agent") or "").strip()
        if not agent_name:
            raise ValueError("검수 항목에 agent 이름이 없습니다.")
        if agent_name not in _SUBMITTABLE_AGENTS:
            # compliance 제출 포함 — 결정론 검사를 클라이언트가 뒤집는 경로 차단.
            raise ValueError(
                f"제출 불가 agent: {agent_name!r}. "
                f"제출 가능 agent 는 {{'image','copy'}} 뿐입니다. "
                "compliance(결정론 검사)는 클라이언트가 뒤집을 수 없습니다."
            )
        raw_verdict = row.get("verdict")
        if raw_verdict is None or str(raw_verdict).strip() == "":
            raise ValueError(
                f"agent={agent_name!r} 의 verdict 가 누락되었습니다. "
                "허용값: PASS/WARN/FAIL/PENDING (기본값으로 떨어뜨리지 않음)."
            )
        verdict = str(raw_verdict).strip().upper()
        if verdict not in qa_agents._VALID_VERDICTS:
            raise ValueError(
                f"알 수 없는 verdict: {raw_verdict!r} (agent={agent_name!r}). "
                "허용값: PASS/WARN/FAIL/PENDING."
            )
        normalized.append(
            {
                "agent": agent_name,
                "verdict": verdict,
                "violations": list(row.get("violations") or []),
                "summary": str(row.get("summary") or ""),
            }
        )
    return product_key, payload, normalized


def submit_reviews(product_key, reviews):
    """클라이언트 LLM 의 판단 결과를 prepared payload 의 QA 기록에 병합.

    신뢰 모델(타협 불가):
      - **덮어쓰기가 아니라 병합**: 서버 verdict 와 클라이언트 회신의 *더 나쁜
        쪽* 을 채택한다(FAIL > PENDING > WARN > PASS).
      - 서버가 기록한 violations 은 **절대 삭제하지 않는다**.
      - 결과적으로 클라이언트는 ``PENDING → PASS`` 로만 상향할 수 있고,
        ``FAIL → PASS`` 는 불가능하다.
      - 제출 가능 agent 는 ``{"image","copy"}`` 로 고정. ``compliance`` 제출은
        ``ValueError`` (결정론 검사 결과를 클라이언트가 뒤집는 경로 원천 차단).
      - ``verdict`` 허용값 ``{"PASS","WARN","FAIL","PENDING"}`` 엄격 검증.
        그 외/누락은 ``ValueError`` (기본값 WARN 으로 떨어뜨리지 말 것).

    Args:
        product_key: prepared payload 의 product_key.
        reviews: ``[{"agent": "image"|"copy", "verdict": ..., "violations": [...],
            "summary": str}, ...]``.

    Returns:
        갱신된 QA 집계 결과 dict (``verdict``, ``agents``, ``violations`` ...).
    """
    d = {"product_key": product_key, "reviews": reviews}
    pkey, payload, normalized = _validate_review_submission(d)

    qa = payload.get("qa") if isinstance(payload.get("qa"), dict) else {}
    server_agents = {
        str(row.get("agent") or ""): row
        for row in (qa.get("agents") or [])
        if isinstance(row, dict)
    }
    server_violations = list(qa.get("violations") or [])

    # 클라이언트 회신을 서버 agent 결과에 병합(더 나쁜 쪽 채택, violations 보존).
    for client_row in normalized:
        agent_name = client_row["agent"]
        server_row = server_agents.get(agent_name)
        if server_row is None:
            # 서버 기록이 없는 agent — 클라이언트 회신을 그대로 채택.
            merged = qa_agents._normalize_agent_result(client_row, agent_name)
        else:
            server_verdict = qa_agents._clamp_verdict(
                server_row.get("verdict"), default=qa_agents.PENDING
            )
            client_verdict = client_row["verdict"]
            # FAIL → PASS 차단: 더 나쁜 쪽 채택.
            final_verdict = _worse_verdict(server_verdict, client_verdict)
            # violations: 서버 것을 절대 삭제하지 않고 클라이언트 것을 추가.
            merged_violations = list(server_row.get("violations") or [])
            for v in client_row["violations"]:
                if isinstance(v, dict):
                    merged_violations.append(v)
            # 클라이언트 summary 보존(있으면), 서버 summary 도 보존.
            summaries = [
                s
                for s in [
                    str(server_row.get("summary") or ""),
                    client_row["summary"],
                ]
                if s
            ]
            merged = qa_agents._normalize_agent_result(
                {
                    "agent": agent_name,
                    "verdict": final_verdict,
                    "violations": merged_violations,
                    "summary": " | ".join(summaries),
                },
                agent_name,
            )
        server_agents[agent_name] = merged

    # 집계 재계산 — 서버 violations 도 에이전트 결과에 다시 넣어 보존.
    agent_rows = list(server_agents.values())
    # 원래 서버 전체 violations 은 별도로 보존(삭제 금지).
    aggregated = qa_agents.aggregate_qa_results(agent_rows)
    # 서버가 산출한 전체 violations 이 병합 후 누락되지 않도록 다시 합친다.
    preserved_violations = list(server_violations)
    for v in aggregated.get("violations") or []:
        if isinstance(v, dict) and v not in preserved_violations:
            preserved_violations.append(v)
    aggregated["violations"] = preserved_violations
    aggregated["source"] = "merged"

    _apply_qa_to_payload(payload, aggregated)
    write_prepared_payload(payload)
    return aggregated


# ---------------------------------------------------------------------------
# inject_prepared_qa — 봉인.
#
# 원본의 두 정의 중 뒤쪽(다중 agent 입력 지원)을 가져왔으나, 이 경로는 임의
# agents 를 검증 없이 받아들이는 무검증 형제 문이다. ``submit_reviews`` 와 *동일한
# 검증 경로*를 공유하도록 봉인한다 — 외부에서 compliance agent 를 주입해 결정론
# 검사 결과를 뒤집는 경로를 원천 차단한다. 디스포지션: **봉인**(shared validation
# 경로로 위임).
# ---------------------------------------------------------------------------


def inject_prepared_qa(d):
    """외부 검수 결과를 prepared payload 에 반영 (봉인판).

    본 함수는 ``submit_reviews`` 와 **동일한 검증 경로**(``_validate_review_submission``)
    를 공유한다 — agent 이름/verdict 엄격 검증, compliance 제출 거부. 과거 이 함수는
    임의 agents 리스트를 검증 없이 받아 결정론 검사(compliance) 결과를 외부에서
    뒤집을 수 있었다(무검증 형제 문). 본 경로를 봉인했다.

    Args:
        d: ``{"product_key": ..., "reviews": [...]}`` 또는 ``{"product_key": ...,
            "image": {...}, "copy": {...}}``.

    Returns:
        갱신된 QA 집계 결과 dict.
    """
    product_key, payload, normalized = _validate_review_submission(d)
    # submit_reviews 와 동일 병합 로직을 탄다(봉인).
    return submit_reviews(product_key, normalized)


# ---------------------------------------------------------------------------
# 태그 추천·제한 검사 (네이버 태그 API 연동).
#
# prepare_listing 의 태그 조립이 ①상품명 기반 키워드로 추천 조회 →
# ②후보를 제한 검사 → ③restricted:false 만 태그로 쓴다. 등록 시점의 사후
# 제한어 제거(``naver_client.register_product`` 의 ``seller_tags`` 백스톱)를
# 조립 시점 사전 검사로 앞당긴다.
#
# ★ 함정 (실측 확정): 추천 목록에 있어도 제한일 수 있다. "니트" 는
#   ``recommend_tags`` code 877 이면서 동시에 ``restricted:true`` 다. 따라서
#   추천 결과를 그대로 쓰지 말고 반드시 제한 검사를 통과시킨다.
#
# 규약 (티켓 계약):
#   - 사용자가 직접 준 태그는 항상 우선이고 삭제되지 않는다. 단 제한 검사에는
#     같이 태워서 제한이면 **알린다**(조용한 드롭 금지).
#   - 남는 슬롯을 추천→제한통과 태그로 채운다.
#   - 태그 출처(사용자/네이버 추천)를 반환에 구분해 표시한다(조용한 자동
#     채움 금지 — 기존 관례 그대로).
#   - 실패 시 강등(fail-open): 태그 API 실패해도 prepare 는 죽지 않는다.
#     기존 태그 로직으로 진행하되 사유를 반환에 남긴다(조용한 실패 금지).
#
# 태그 상한 관례: ``registration_agent.md`` "~10개" / ``seo.py`` seo_planner_hint
#   "5-10 seller tags" → **MAX_SELLER_TAGS = 10**. 새 수치를 지어내지 않는다.
# ---------------------------------------------------------------------------

# 태그 최대 개수 — 기존 관례(registration_agent.md "~10개", seo.py "5-10")의
# 상한. 새 수치를 지어내지 않는다.
MAX_SELLER_TAGS = 10


def _resolve_tags(
    name,
    user_tags,
    *,
    brand=None,
    category_name=None,
    recommend_fn=None,
    restricted_fn=None,
):
    """태그 조립: 추천 조회 → 제한 검사 → ``restricted:false`` 만 사용.

    흐름:
      1. **사용자 태그가 항상 우선**. 단 제한 검사에 같이 태운다 — 제한이면
         **삭제하지 않고** 알림에 올린다(조용한 드롭 금지).
      2. 상품명 첫 토큰(또는 전체 이름) 으로 ``recommend_tags`` 조회.
      3. 추천 후보 + 사용자 태그를 합쳐 ``restricted_tags`` 로 제한 검사.
      4. ``restricted:false`` 인 추천 태그로 남은 슬롯(``MAX_SELLER_TAGS`` -
         사용자 태그 수)을 채운다.
      5. 반환에 **태그 출처**(사용자/네이버 추천)를 구분해 표시.

    실패 시 강등(fail-open — 규제값이 아니다): 태그 API 가 실패(네트워크·4xx)
    하면 예외를 던지지 않고 ``error`` 에 사유를 남긴 채 사용자 태그만으로
    진행한다. ``prepare_listing`` 본체는 죽지 않는다.

    Args:
        name: 상품명(추천 키워드 후보).
        user_tags: 사용자가 직접 준 태그 리스트.
        brand: 브랜드명. 같은 문자가 태그에 있으면 사전 제거·보고한다.
        category_name: 카테고리명/경로. 같은 문자가 태그에 있으면 사전 제거·보고한다.
        recommend_fn: ``naver_client.recommend_tags`` 대체(테스트 주입용).
        restricted_fn: ``naver_client.restricted_tags`` 대체(테스트 주입용).

    Returns:
        ``{"final_tags": [...], "user_tags": [...], "recommended_tags": [...],
           "restricted": [{"tag": str, "source": "user"|"recommend"}, ...],
           "recommend_lookup": {"ok": bool, ...} | None,
           "restricted_lookup": {"ok": bool, ...} | None,
           "field_duplicates": [{"tag": str, "reason": str}, ...],
           "error": str | None}`` —
        ``final_tags`` 는 ``product.tags`` 에 들어갈 최종 태그(사용자 우선,
        추천으로 남은 슬롯 채움, 최대 ``MAX_SELLER_TAGS`` 개). ``restricted`` 는
        제한 판정된 태그와 그 출처(사용자가 준 것인지 추천에서 온 것인지).
        ``error`` 는 fail-open 사유(성공 시 None).
    """
    from . import naver_client as _nc

    recommend = recommend_fn if recommend_fn is not None else _nc.recommend_tags
    restricted = restricted_fn if restricted_fn is not None else _nc.restricted_tags

    clean_user = [str(t).strip() for t in (user_tags or []) if str(t or "").strip()]
    user_field_check = qa_agents.filter_duplicate_field_tags(
        clean_user, name=name, brand=brand, category_name=category_name
    )
    clean_user = list(user_field_check["tags"])
    result = {
        "final_tags": list(clean_user),
        "user_tags": list(clean_user),
        "recommended_tags": [],
        "restricted": [],
        # 응답 메타 — 중복 태그 자동 제거는 반드시 이 목록으로 보고한다.
        "field_duplicates": list(user_field_check["removed"]),
        "recommend_lookup": None,
        "restricted_lookup": None,
        "error": None,
    }

    # 추천 조회 — fail-open. 실패해도 사용자 태그로 진행.
    recommend_candidates: list[str] = []
    keyword = str(name or "").strip()
    if not keyword:
        # 키워드가 없으면 추천 조회 자체를 건너뛴다(API 가 어차피 400).
        result["recommend_lookup"] = {"ok": False, "status_code": None, "detail": "키워드 없음"}
    else:
        try:
            sc, body = recommend(keyword)
            if sc == 200 and isinstance(body, list):
                recommend_candidates = [
                    str(item.get("text") or "").strip()
                    for item in body
                    if isinstance(item, dict) and str(item.get("text") or "").strip()
                ]
                result["recommend_lookup"] = {
                    "ok": True,
                    "status_code": sc,
                    "count": len(recommend_candidates),
                }
            else:
                detail = ""
                if isinstance(body, dict):
                    detail = str(body.get("message") or body.get("detail") or "")
                elif isinstance(body, str):
                    detail = body
                result["recommend_lookup"] = {
                    "ok": False,
                    "status_code": sc,
                    "detail": detail[:200],
                }
                result["error"] = f"추천 조회 실패: HTTP {sc} {detail[:120]}".strip()
        except Exception as exc:
            result["recommend_lookup"] = {
                "ok": False,
                "status_code": None,
                "detail": str(exc)[:200],
            }
            result["error"] = f"추천 조회 실패: {exc}"[:300]

    # 추천 태그도 제한어 API 호출 전에 같은 결정론 검사로 거른다. 이 검사는
    # 후보를 새로 만들지 않으며, 불필요한 네이버 왕복을 막는다.
    recommended_field_check = qa_agents.filter_duplicate_field_tags(
        recommend_candidates, name=name, brand=brand, category_name=category_name
    )
    recommend_candidates = list(recommended_field_check["tags"])
    result["field_duplicates"].extend(recommended_field_check["removed"])

    # 제한 검사 대상: 사용자 태그 + 추천 후보(중복 제거, 순서 보존).
    # 사용자 태그는 제한이어도 삭제하지 않지만, 제한 검사에는 태운다(알림용).
    check_pool: list[str] = []
    seen: set[str] = set()

    def _add_unique(value: str):
        key = re.sub(r"\s+", "", value).lower()
        if key and key not in seen:
            check_pool.append(value)
            seen.add(key)

    for t in clean_user:
        _add_unique(t)
    for t in recommend_candidates:
        _add_unique(t)

    # 제한 검사 — fail-open. 빈 풀이면 API 호출을 건너뛴다(400 방지).
    restricted_map: dict[str, bool] = {}
    if check_pool:
        try:
            sc, body = restricted(check_pool)
            if sc == 200 and isinstance(body, list):
                for item in body:
                    if isinstance(item, dict):
                        tag_text = str(item.get("tag") or "").strip()
                        is_restricted = bool(item.get("restricted"))
                        if tag_text:
                            restricted_map[tag_text] = is_restricted
                result["restricted_lookup"] = {
                    "ok": True,
                    "status_code": sc,
                    "count": len(restricted_map),
                }
            else:
                detail = ""
                if isinstance(body, dict):
                    detail = str(body.get("message") or body.get("detail") or "")
                elif isinstance(body, str):
                    detail = body
                result["restricted_lookup"] = {
                    "ok": False,
                    "status_code": sc,
                    "detail": detail[:200],
                }
                if result["error"] is None:
                    result["error"] = f"제한 조회 실패: HTTP {sc} {detail[:120]}".strip()
        except Exception as exc:
            result["restricted_lookup"] = {
                "ok": False,
                "status_code": None,
                "detail": str(exc)[:200],
            }
            if result["error"] is None:
                result["error"] = f"제한 조회 실패: {exc}"[:300]

    def _is_restricted(tag_text: str) -> bool:
        # 대소문자/공백 무시 매칭.
        for key, val in restricted_map.items():
            if re.sub(r"\s+", "", key).lower() == re.sub(r"\s+", "", tag_text).lower():
                return val
        return False

    # 사용자 태그 중 제한인 것을 알림에 올린다(삭제하지 않음).
    user_restricted_keys: set[str] = set()
    for t in clean_user:
        if _is_restricted(t):
            result["restricted"].append({"tag": t, "source": "user"})
            user_restricted_keys.add(re.sub(r"\s+", "", t).lower())

    # 추천 태그 중 restricted:false 인 것으로 남은 슬롯을 채운다.
    # 사용자 태그(정규화 기준)와 중복되는 추천은 건너뛴다.
    user_keys = {re.sub(r"\s+", "", t).lower() for t in clean_user}
    final_tags = list(clean_user)
    for t in recommend_candidates:
        if len(final_tags) >= MAX_SELLER_TAGS:
            break
        key = re.sub(r"\s+", "", t).lower()
        if key in user_keys:
            continue
        if _is_restricted(t):
            # 추천 태그가 제한이면 final_tags 에 넣지 않고 알림에만 올린다.
            result["restricted"].append({"tag": t, "source": "recommend"})
            continue
        final_tags.append(t)
        result["recommended_tags"].append(t)
        user_keys.add(key)  # 이후 중복 방지

    result["final_tags"] = final_tags[:MAX_SELLER_TAGS]
    return result


# ---------------------------------------------------------------------------
# prepare_listing 본체.
#
# 흐름(IN 목록만 실행):
#   1. URL 입력 거부 검사(URL 키 존재 시 ValueError).
#   2. images.attach_images 로 이미지 정규화. rejected 가 비어있지 않으면 진행 X.
#   3. detail_render.render_detail_html 로 상세 HTML.
#   4. 카테고리/고시 컨텍스트 구성(최소 — category_id 정도).
#   5. JPEG 비의존 QA만 실행 — 이미지 QA는 PENDING 등록, 카피 QA도 LLM 필요시 PENDING.
#   6. prepared payload 저장.
#   7. 결과 반환(needs_llm, needs_user 포함).
#
# OUT(이번 스코프 아님 — 스텁도 만들지 않는다): 이미지 리터치·업스케일, OCR 정리,
# 상세 시트 병합·밴드 선택, 옵션 이미지 자동 생성, 병렬 실행기, 단계별
# 타이밍/코스트 로깅, 자동 재렌더 보정.
# ---------------------------------------------------------------------------


_SEO_TAG_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]+")
# 태그 후보로 쓸 수 없는 순수 숫자/1글자 조각은 거른다(길이 2 이상).
_SEO_TAG_MIN_LEN = 2


def _option_name_tokens(options) -> list[str]:
    """옵션 조합에서 옵션값 텍스트(name · optionName1..3) 만 모은다."""
    tokens: list[str] = []
    for opt in options or []:
        if not isinstance(opt, dict):
            continue
        for key in ("name", "optionName1", "optionName2", "optionName3"):
            value = opt.get(key)
            if isinstance(value, str) and value.strip():
                tokens.extend(_SEO_TAG_TOKEN_RE.findall(value))
    return tokens


def _build_seo_tags_suggestion(name, options, category_path):
    """로컬 규칙만으로 판매자태그 후보를 만든다 (외부 호출 0회).

    재료(워크오더 Part A): 상품명 유지 키워드 + 옵션명 명사 + 카테고리 경로
    어휘. 토큰화는 단순 문자 클래스(한글·영숫자 연속) — 새 어휘 분석기를
    만들지 않는다. 순수 숫자·1글자 조각은 버리고, 중복을 제거해 최대 10개.

    Returns:
        ``{"tags": [...], "basis": [...], "reason": str|None}`` — 재료가 없어
        만들 수 없으면 ``tags`` 가 빈 리스트이고 ``reason`` 에 사유가 있다
        (조용한 생략 금지).
    """
    basis: list[str] = []
    seen: set[str] = set()
    tags: list[str] = []

    def _take(tokens, source_label):
        for token in tokens:
            token = str(token or "").strip()
            if len(token) < _SEO_TAG_MIN_LEN or token.isdigit():
                continue
            if token in seen:
                continue
            seen.add(token)
            tags.append(token)
            basis.append(f"{source_label}:{token}")
            if len(tags) >= 10:
                return

    _take(_SEO_TAG_TOKEN_RE.findall(str(name or "")), "name")
    if len(tags) < 10:
        _take(_option_name_tokens(options), "option")
    if len(tags) < 10:
        _take(_SEO_TAG_TOKEN_RE.findall(str(category_path or "").replace(">", " ")), "category")
    if not tags:
        return {
            "tags": [],
            "basis": [],
            "reason": (
                "상품명·옵션명·카테고리 경로에서 태그 재료(2글자 이상 명사형) 를 "
                "찾지 못했습니다."
            ),
        }
    return {"tags": tags, "basis": basis, "reason": None}


def _default_attributes_suggest(category_id, product_text):
    """카테고리 속성 제안 기본 구현 — 외부 API(속성/속성값 조회) 1회 경로.

    ``suggest_product_attributes`` MCP 도구와 같은 조회·대조 로직을 쓴다:
    카테고리 속성 목록 → 첫 속성 attributeSeq 로 속성값 전체 조회 →
    ``attribute_suggestions.suggest_category_attributes`` 문자 일치.

    Returns:
        ``{"ok": bool, "suggestions": list|None, "error": str|None}``.
    """
    from . import attribute_suggestions
    from . import naver_client as _nc

    try:
        status, body = _nc.get_category_attributes(category_id)
    except Exception as exc:  # 조회 실패 — 조용히 삼키지 않고 error 로 드러낸다.
        return {"ok": False, "suggestions": None, "error": f"속성 목록 조회 실패: {exc}"}
    if status != 200 or not isinstance(body, list):
        return {
            "ok": False,
            "suggestions": None,
            "error": f"속성 목록 API 반환 상태 {status}(list 아님 가능) — 제안 불가.",
        }
    first = next(
        (
            attr
            for attr in body
            if isinstance(attr, dict) and attr.get("attributeSeq") not in (None, "")
        ),
        None,
    )
    if first is None:
        return {
            "ok": False,
            "suggestions": None,
            "error": "속성 목록이 비어 있어 제안할 수 없습니다.",
        }
    try:
        values_status, values_body = _nc.get_category_attribute_values(
            category_id, first["attributeSeq"]
        )
    except Exception as exc:
        return {"ok": False, "suggestions": None, "error": f"속성값 목록 조회 실패: {exc}"}
    if values_status != 200 or not isinstance(values_body, list) or not values_body:
        return {
            "ok": False,
            "suggestions": None,
            "error": (
                f"속성값 목록 API 반환 상태 {values_status} — 제안 불가" "(빈 목록 성공 취급 금지)."
            ),
        }
    suggestions = attribute_suggestions.suggest_category_attributes(product_text, body, values_body)
    return {"ok": True, "suggestions": suggestions, "error": None}


def prepare_listing(
    d,
    *,
    attach_fn=None,
    generate_fn=None,
    recommend_fn=None,
    restricted_fn=None,
    attributes_fn=None,
):
    """상품 정보 + 이미지 소스 로 prepared payload 를 만든다.

    본 함수는 등록 전 단계를 수행한다: 이미지 정규화, (선택) 이미지 생성
    단계 분기, 상세 HTML 렌더, QA 집계 (이미지 QA 는 PENDING 등록 — JPEG
    의존 항목은 이 파이프라인에서 실행하지 않는다). 결과를 prepared payload
    로 저장하고 반환한다.

    **이미지 생성 단계 분기** (도달 가능 분기 — 과거 "원본 0장이면 생성" 조건이
    원본 게이트와 겹쳐 절대 실행되지 않는 죽은 코드였던 결함을 고쳤다):

      ① 원본 사진이 있는가? → ``image_sources`` 가 비어있거나 정규화 후
         ``listing_urls`` 가 0장이면 ``attach_images`` 게이트가 ``ValueError``
         로 차단한다. **생성이 이 자리를 대체하지 못한다 (원본 게이트 불변).**
      ② 필요한 컷 수를 원본으로 채웠는가? → ``image_gen.images_ready(
         image_sources, needed_cuts)`` 판정. 채웠으면 생성 경로 미진입(생성 0).
         부족하면 ③ 으로.
      ③ 생성을 원하는가? → ``d.generate_images`` 가 참일 때만. 아니면 그대로
         진행(생성 0).
      ④ 생성 API 키가 있는가? → 있으면 자기 키로 **부족분(shortfall)만큼만**
         생성한다. 없으면 명확한 사유 + 발급 안내(조용한 실패 금지).

    안전 규율 (불변):
      - 원본 이미지 0장이면 생성으로 대체할 수 없다 — ``attach_images`` 게이트가
        이미 차단한다. 생성은 **부족한 추가 컷** 만 메운다(원본 자리 아님).
      - 생성 결과는 원본을 대체하지 않는다 (대표이미지 규약·순서 보존). 원본
        ``listing_urls`` 의 *뒤에* 추가한다.
      - 부족분이 0 이하면 생성 경로에 진입하지 않는다 (사용자 돈 누수 방지).

    Args:
        d: 상품 입력 dict. 필수: ``name``, ``salePrice``, ``image_sources``
            (이미지 소스 리스트 — 로컬 경로/CDN URL/외부 URL 혼합).
            선택: ``options``, ``tags``, ``notice``, ``category_id``,
            ``generate_images`` (bool — 이미지 생성 단계 분기),
            ``image_prompt`` (str — 생성 프롬프트),
            ``needed_cuts`` (int — 필요 컷 수, 기본 1),
            ``facts`` (``[{"name": str, "value": str}, ...]`` — 이름↔팩트
            모순 게이트 입력. 생성 트랙 name_ko/value_ko 형태도 받는다).
        attach_fn: ``images.attach_images`` 대체(테스트 주입용).
        generate_fn: ``image_gen.generate`` 대체(테스트 주입용).
        recommend_fn: ``naver_client.recommend_tags`` 대체(테스트 주입용).
            None 이면 실제 ``naver_client.recommend_tags`` 를 쓴다(실호출 —
            테스트 외부 네트워크 차단 컨텍스트에서 테스트는 반드시 주입해야 한다).
        restricted_fn: ``naver_client.restricted_tags`` 대체(테스트 주입용).
            None 이면 실제 ``naver_client.restricted_tags`` 를 쓴다.

    Returns:
        prepared payload dict. 다음 키를 포함한다:
          - ``product_key``: ``sha1(name+price)[:12]``.
          - ``images``: ``{"listing_urls": [...], "detail_urls": [...]}``.
          - ``detail_html``: 상세 HTML 문자열.
          - ``scene``: 조립 결과 문서. ``detail_html`` 과 같은 조립
            결과에서 나온 구조적 표현. ``docs/scene-schema.md`` 참조.
          - ``needs_llm``: LLM 위임이 필요한 항목 리스트.
          - ``needs_user``: 사용자 입력이 필요한 항목 리스트.
          - ``qa``: QA 집계 결과.
          - ``image_generation``: 생성 단계 분기 결과 메타(생성 시도 시에만).
            ``needed_cuts``/``api_call_count``/``output_canvas_count``/
            ``output_layout``/``panel_count_used``/``estimated_cost_usd`` 를
            포함한다 (``IMAGE_GENERATION_PRICE_POLICY.md`` 단위 규약 준수).
          - ``tags_meta``: 태그 추천·제한 검사 결과. ``final_tags``/
            ``user_tags``/``recommended_tags``/``restricted``/``recommend_lookup``/
            ``restricted_lookup``/``field_duplicates``/``error`` 키를 담는다(네이버 태그 API 연동 — ``_resolve_tags``
            참조). 추천·제한 검사를 통과한 최종 태그가 ``product.tags`` 에 들어간다.
          - ``version``: ``common.PREPARED_PAYLOAD_VERSION``.
          - ``name_fact_check``: 이름↔팩트 모순 게이트 결과
            (``{"status": "ok"|"conflict"|"skipped", "conflicts": [...]}``).

    Raises:
        ValueError: URL 키 존재, rejected 이미지 존재, 상품명/가격 누락.
    """
    from . import detail_render
    from . import images as _images_mod

    _reject_url_inputs(d)
    if not isinstance(d, dict):
        raise ValueError("prepare_listing 입력은 dict 여야 합니다.")

    name = str(d.get("name") or d.get("title_ko") or "").strip()
    sale_price = d.get("salePrice")
    if sale_price is None:
        sale_price = d.get("sell_price") or d.get("price")
    if not name:
        raise ValueError("prepare_listing: 상품명(name) 이 필요합니다.")
    if sale_price is None:
        raise ValueError("prepare_listing: 판매가(salePrice, KRW) 가 필요합니다.")

    # --- 0. 이름↔팩트 모순 게이트 (결정론, LLM 0·외부 호출 0) ---
    # 상품명과 번들 팩트(facts) 를 대조해 오역·사실 오류(예: 이름은 "손잡이
    # 있는" 인데 팩트는 "손잡이 없는 디자인") 를 등록 앞에서 드러낸다.
    # facts 가 없으면 조용히 통과하지 않고 skipped 로 보고한다.
    from . import name_fact as _name_fact_mod

    name_fact_check = _name_fact_mod.check_name_facts(name, d.get("facts"))

    # --- 1. 이미지 정규화 (images.attach_images) ---
    image_sources = d.get("image_sources")
    if not isinstance(image_sources, list) or not image_sources:
        raise ValueError(
            "prepare_listing: image_sources(이미지 소스 리스트) 가 필요합니다. "
            "최소 1장 이상의 로컬 경로/CDN URL/외부 URL 을 전달하세요."
        )
    attach = attach_fn if attach_fn is not None else _images_mod.attach_images
    attach_result = attach(image_sources)
    # rejected 가 비어있지 않으면 진행하지 않는다(fail-closed).
    rejected = attach_result.get("rejected") or []
    if rejected:
        details = "; ".join(
            f"[{r.get('index')}] {r.get('source')}: {r.get('reason')}" for r in rejected[:5]
        )
        raise ValueError(
            "prepare_listing: 이미지 검증에서 거부된 항목이 있어 진행하지 않습니다. "
            f"(거부 {len(rejected)}건) {details}"
        )
    listing_urls = list(attach_result.get("urls") or [])
    if not listing_urls:
        raise ValueError(
            "prepare_listing: 정규화된 이미지 URL 이 0장입니다. " "등록을 거부한다(무음 통과 금지)."
        )
    # 상세 이미지 URL 도 같은 소스에서 왔다고 간주(상세 전용 소스는 OUT 범위).
    detail_urls = list(listing_urls)

    # --- 1.5. 이미지 생성 단계 분기 (도달 가능 분기) ---
    # image_gen 모듈이 config.image_providers 를 실제로 읽는 유일한 경로다.
    #
    # 과거 결함 (죽은 코드): 조건이 "want_generation and not user_has_images" 였다.
    #   - image_sources=[]      → 위 게이트(attach_images)가 ValueError 로 차단.
    #   - image_sources=[""]    → 게이트가 차단(또는 listing_urls 0장 → 차단).
    #   - image_sources=["   "] → 동일.
    # 즉 조건이 참이 되는 입력이 존재하지 않았다 → 생성 분기는 절대 도달 불가.
    #
    # 수정된 단계 분기 (원본 게이트 불변):
    #   ① 원본 0장 → attach_images 게이트가 이미 차단 (생성으로 대체 불가).
    #   ② 원본으로 needed_cuts 를 채웠는가? → images_ready(image_sources,
    #      needed_cuts) 판정. 채웠으면 생성 경로 미진입(생성 0).
    #   ③ 부족하면 generate_images 가 참일 때만 생성 시도.
    #   ④ **부족분(shortfall)만큼만** 생성한다 — needed_cuts 전량이 아니라
    #      ``needed_cuts - 보유 유효 원본 컷 수``. 있는 걸 또 만들면 사용자
    #      돈이 샌다. 부족분이 0 이하면 생성 경로 미진입(호출 0).
    #   ⑤ 생성 결과는 원본 listing_urls 의 *뒤에* 추가한다 (대표이미지 규약·
    #      순서 보존 회귀 금지).
    image_generation_meta: dict | None = None
    generation_user_hint: dict | None = None
    try:
        from . import image_gen as _image_gen_mod

        try:
            # 최소 1 보장 — 0 이하 needed_cuts 는 의미 없고 폴백 1 로 고정.
            needed_cuts = max(1, int(d.get("needed_cuts") or 1))
        except (TypeError, ValueError):
            needed_cuts = 1
        # 보유 유효 원본 컷 수 — 빈 문자열/공백은 제외(기존 images_ready 규칙).
        valid_original_count = sum(1 for s in image_sources if isinstance(s, str) and s.strip())
        shortfall = needed_cuts - valid_original_count
        want_generation = bool(d.get("generate_images"))
        # ②③ 원본이 needed_cuts 를 채우지 못했고 + 생성을 원할 때만 진입.
        if want_generation and shortfall > 0:
            # ④ 부족분만큼만 생성 — 필요 컷 전량을 다시 만들지 않는다(돈 누수 방지).
            prompt = str(d.get("image_prompt") or name or "").strip()
            generate = generate_fn if generate_fn is not None else _image_gen_mod.generate
            gen_result = generate(prompt, needed_cuts=shortfall)
            image_generation_meta = {
                "ok": bool(gen_result.get("ok")),
                "provider": gen_result.get("provider"),
                "model": gen_result.get("model"),
                "needed_cuts": gen_result.get("needed_cuts"),
                "api_call_count": gen_result.get("api_call_count"),
                "output_canvas_count": gen_result.get("output_canvas_count"),
                "output_layout": gen_result.get("output_layout"),
                "panel_count_used": gen_result.get("panel_count_used"),
                "estimated_cost_usd": gen_result.get("estimated_cost_usd"),
                "error": gen_result.get("error"),
                # 부족분 추적(정책 문서 단위 규약 추가 메타).
                "requested_needed_cuts": needed_cuts,
                "original_count": valid_original_count,
                "shortfall": shortfall,
            }
            if gen_result.get("ok"):
                # ⑤ 생성 성공 — 원본 뒤에 추가. 원본을 대체하지 않는다(순서 보존).
                gen_urls = [
                    str(u).strip()
                    for u in (gen_result.get("image_urls") or [])
                    if isinstance(u, str) and u.strip()
                ]
                listing_urls.extend(gen_urls)
                detail_urls = list(listing_urls)
            else:
                # 생성 실패 — 조용히 넘기지 않고 안내를 needs_user 에 싣는다.
                # image_gen 이 키 부재 안내 문구를 error 에 담아 반환한다
                # (조용한 실패 금지).
                generation_user_hint = {
                    "field": "image_generation",
                    "label": "이미지 생성",
                    "why": str(
                        gen_result.get("error") or "이미지 생성에 실패했습니다 (사유 미상)."
                    ),
                }
        # shortfall <= 0 이거나 want_generation 이 아니면 생성 경로 미진입(호출 0).
    except Exception as exc:
        # image_gen 로드 자체가 실패하면 생성 불가. 조용한 통과 금지 — 안내 싣기.
        # 단, prepare_listing 본체는 죽이지 않는다(준비 단계 역할 존중).
        generation_user_hint = {
            "field": "image_generation",
            "label": "이미지 생성",
            "why": f"이미지 생성 모듈 로드/실행 중 오류: {exc}",
        }

    # --- 2. 상세 HTML 렌더 (detail_render.render_detail_html) ---
    # scene 도 같은 입력에서 산출한다(detail_html 과 scene 은
    # 같은 조립 결과에서 나와야 한다 — 불일치 금지). build_scene 은
    # render_detail_html 과 동일한 _assemble 로직을 공유하므로, 같은 입력을
    # 넣으면 두 출력은 정합이다.
    options = d.get("options") or []
    product_for_render = {
        "name": name,
        "summary": d.get("summary") or d.get("desc") or "",
        "props": d.get("props") or d.get("attributes") or {},
        "notice": d.get("notice") or {},
    }
    detail_html = detail_render.render_detail_html(product_for_render, listing_urls, options)
    scene = detail_render.build_scene(product_for_render, listing_urls, options)

    # --- 3. 카테고리/고시 컨텍스트 구성(최소) ---
    category_id = str(d.get("categoryId") or d.get("category_id") or "").strip()

    # product_key 는 상품을 구별하는 입력(카테고리·이미지 소스 구성)을
    # 반영해 만든다. 이름·가격만으로는 색상만 다른 SKU 처럼 같은 키가 나와
    # 두 번째 준비가 첫 번째를 조용히 덮는다. category_id 와 image_sources 가
    # 확정된 *지금* 키를 유도한다 (이 시점 이전에는 아직 모를 수 있다).
    product_key = make_product_key(
        name, sale_price, category_id=category_id, image_sources=image_sources
    )

    # 무음 덮어쓰기 탐지: 같은 키의 prepared 가 이미 있으면 내용이 다를 때
    # 조용히 덮지 않는다. 반환값에 사실을 드러낸다(저장소 불변식).
    overwrite_warning = None
    try:
        _existing = load_prepared_payload(product_key=product_key)
        _existing_detail = str(_existing.get("detail_html") or "")
        _existing_images = (
            _existing.get("images", {}).get("listing_urls") if isinstance(_existing, dict) else None
        )
        if _existing_detail != detail_html or list(_existing_images or []) != listing_urls:
            overwrite_warning = (
                "기존 prepared payload 와 내용이 다릅니다(상세HTML 또는 이미지). "
                "같은 키를 덮어쓴다 — 재실행이 아닌 이상 의도된 변경인지 확인하세요."
            )
    except FileNotFoundError:
        pass
    except ValueError:
        # version 불일치 등 — 기존 것을 무시하고 새로 쓴다(스키마 변경 시).
        overwrite_warning = "기존 prepared payload 의 version 이 불일치한다. 덮어쓴다(스키마 변경)."

    # --- 3.5. 태그 필드중복·추천·제한 검사 (네이버 태그 API 연동) ---
    # prepare_listing 본체가 _resolve_tags 를 호출해 최종 태그를 산출한다.
    # 흐름: ①상품명으로 추천 조회 → ②후보+사용자 태그를 제한 검사 →
    # ③상품명·브랜드·카테고리와 문자 그대로 겹치는 태그는 사전 제거·보고 →
    # ④restricted:false 인 추천 태그로 남은 슬롯 채움. 제한어는 기존 정책대로
    # 사용자 태그를 삭제하지 않고 알리기만 한다.
    # 실패 시 강등(fail-open) — prepare_listing 본체는 죽지 않는다.
    #
    # ★ 컴플라이언스 일치: 산출된 final_tags 를 컴플라이언스 검사용 임시
    # 페이로드와 저장용 payload 양쪽에 같은 값으로 넣는다 — 준비 단계가
    # 등록 단계와 같은 태그를 본다.
    brand = str(d.get("brand") or d.get("brand_name") or d.get("brandName") or "").strip()
    category_name = str(
        d.get("category_name") or d.get("category_path") or d.get("categoryPath") or ""
    ).strip()
    if not category_name and category_id:
        try:
            category_name = _category_path_for(category_id)
        except Exception:
            # 아래 컴플라이언스 경로도 같은 조회를 하며 실패를 사용자에게 보고한다.
            # 태그 사전 검사가 그 기존 fail-closed 보고 경로를 예외로 바꾸면 안 된다.
            category_name = ""
    tag_resolution = _resolve_tags(
        name,
        d.get("tags") or [],
        brand=brand,
        category_name=category_name,
        recommend_fn=recommend_fn,
        restricted_fn=restricted_fn,
    )
    final_tags = list(tag_resolution.get("final_tags") or [])

    # --- 3.6. 태그·속성 자동 제안 (워크오더 Part A/B) ---
    # 태그 제안: 로컬 규칙만(외부 호출 0회). 상품명·옵션명·카테고리 경로가
    # 재료다. 사용자 태그가 있더라도 제안은 만들어 반환한다 — 등록 단계에서
    # 채용 여부를 판단한다(명시 tags 항상 우선).
    _suggest_cat_path = ""
    try:
        _suggest_cat_path = _category_path_for(category_id) if category_id else ""
    except Exception:
        _suggest_cat_path = ""
    seo_tags_suggestion = _build_seo_tags_suggestion(name, d.get("options"), _suggest_cat_path)

    # 속성 제안: 카테고리 확정 시에만 외부 API(속성·속성값 조회) 1회 경로.
    # 실패는 조용히 삼키지 않고 attributes_error 로 드러내되 준비를 막지 않는다.
    # 상품명·옵션명 텍스트와 **문자열 일치**하는 속성값만 확신 제안이 된다.
    attributes_suggestion: list[dict] = []
    attributes_suggestion_basis: list[str] = []
    attributes_suggestion_blocked: list[dict] = []
    attributes_error: str | None = None
    attributes_needs_user_hint: dict | None = None
    if category_id:
        _attr_fn = attributes_fn if attributes_fn is not None else _default_attributes_suggest
        _option_text = " ".join(_option_name_tokens(d.get("options")))
        # 전문 텍스트(source_text): 호출자가 전문·속성쌍을 합쳐 전달하는 선택 키.
        # 생성 트랙 파일 파싱은 이 단계 밖(호출자 몫) — 여기선 문자열만 받는다.
        _attr_product: dict[str, str] = {
            "name": f"{name} {_option_text}".strip(),
            "detail": "",
        }
        _source_text = d.get("source_text")
        if isinstance(_source_text, str) and _source_text.strip():
            _attr_product["source_text"] = _source_text
        _attr_result = _attr_fn(category_id, _attr_product)
        if not _attr_result.get("ok"):
            attributes_error = str(_attr_result.get("error") or "속성 제안 조회 실패(사유 미상).")
        else:
            for row in _attr_result.get("suggestions") or []:
                if not isinstance(row, dict) or row.get("status") != "matched":
                    continue
                # attributeSeq 는 속성(row) 에, attributeValueSeq 는 선택값
                # (selected) 에 있다 — attribute_suggestions.suggest_category_
                # attributes 의 반환 형태 그대로(창작 금지).
                row_attr_seq = row.get("attributeSeq")
                if not isinstance(row_attr_seq, int):
                    continue
                for selected in row.get("selected") or []:
                    if not isinstance(selected, dict):
                        continue
                    if not isinstance(selected.get("attributeValueSeq"), int):
                        continue
                    attributes_suggestion.append(
                        {
                            "attributeSeq": row_attr_seq,
                            "attributeValueSeq": selected["attributeValueSeq"],
                        }
                    )
                    attributes_suggestion_basis.append(
                        str(row.get("attributeName") or "")
                        + "="
                        + str(selected.get("minAttributeValue") or "")
                        + " ("
                        + str(selected.get("evidence") or "")
                        + ")"
                    )
            # 일치 없는 속성의 후보는 needs_user 로 드러낸다(조용한 생략 금지).
            # 확신 제안이 일부 있어도, 여전히 일치를 못 한 속성이 있으면
            # 사용자가 후보에서 골라야 한다 — 제안이 있다고 미해결 속성이
            # 사라지는 게 아니다.
            _candidates_summary = [
                f"{row.get('attributeName')}(후보 {len(row.get('candidates') or [])}개)"
                for row in (_attr_result.get("suggestions") or [])
                if isinstance(row, dict)
                and row.get("status") != "matched"
                and (row.get("candidates") or [])
            ]
            # 부정 가드 차단(전문 텍스트 매칭)도 전부 드러낸다 — 조용한 생략
            # 금지. 차단값은 자동 채용되지 않고 후보로 강등되며 사유가 표기된다.
            for row in _attr_result.get("suggestions") or []:
                if not isinstance(row, dict):
                    continue
                for blocked in row.get("blocked") or []:
                    if not isinstance(blocked, dict):
                        continue
                    attributes_suggestion_blocked.append(
                        {
                            "attributeSeq": row.get("attributeSeq"),
                            "attributeName": row.get("attributeName"),
                            "attributeValueSeq": blocked.get("attributeValueSeq"),
                            "minAttributeValue": blocked.get("minAttributeValue"),
                            "evidence": blocked.get("evidence"),
                            "reason": blocked.get("reason"),
                        }
                    )
                    _candidates_summary.append(
                        f"{row.get('attributeName')}="
                        f"{blocked.get('minAttributeValue')}(차단: {blocked.get('reason')})"
                    )
            if _candidates_summary:
                attributes_needs_user_hint = {
                    "field": "attributes",
                    "label": "상품속성 선택 필요",
                    "why": (
                        "상품명·옵션명과 문자열 일치하는 속성값이 없어 자동 채용할 "
                        "확신 제안이 없습니다. 후보: " + ", ".join(_candidates_summary[:10])
                    ),
                }

    # deferred_notice_fields 를 한 번 정제해 컴플라이언스 검사와
    # payload 저장 양쪽에 같은 값을 쓴다. 등록 단계(mcp_server._validate
    # _deferred_notice_fields) 와 같은 원산지/allowlist/boolean-date 검증을
    # 준비 단계도 적용한다 — 준비 통과 → 등록 통과, 준비 거부 → 등록 거부.
    #
    # **게이트를 느슨하게 만들지 않는다**: 원산지 계열 미루기 금지
    # (qa_agents._reject_origin_deferred), allowlist 밖 필드 거부
    # (qa_agents._partition_deferred_by_allowlist), boolean/date 필드 미루기
    # 불가(qa_agents._is_field_deferrable). 이 세 검증을 준비 단계에서 먼저
    # 적용하여, 판매자가 잘못된 미루기를 선언하면 준비 단계에서 needs_user 로
    # 알리고 등록 단계에서도 거부한다(두 단계 합의).
    #
    # 검증에서 거부된 필드는 sane_deferred 에 들어가지 않는다. 그 사실을
    # needs_user 에 실어 판매자에게 알린다(조용한 누락 금지).
    deferred_raw = d.get("deferred_notice_fields")
    sane_deferred: list[str] = []
    deferred_rejected: list[str] = []
    if isinstance(deferred_raw, list):
        for item in deferred_raw:
            if isinstance(item, str) and item.strip():
                sane_deferred.append(item.strip())
    if sane_deferred:
        origin_kept = qa_agents._reject_origin_deferred(sane_deferred)
        origin_hits = [f for f in sane_deferred if f not in origin_kept]
        if origin_hits:
            deferred_rejected.extend(origin_hits)
        allowed_keys, off_list_keys = qa_agents._partition_deferred_by_allowlist(origin_kept)
        sane_deferred = allowed_keys
        if off_list_keys:
            deferred_rejected.extend(off_list_keys)
        if not sane_deferred:
            sane_deferred = []

    # --- 4. JPEG 비의존 QA 실행 ---
    # QA 규칙:
    #   - 이미지 QA 는 래스터 이미지를 요구하므로 실행하지 않고 PENDING 등록.
    #     FAIL 로 만들지 않는다(정상 상품 전건 차단 방지).
    #   - 카피 QA 도 LLM 판단이 필요하면 PENDING.
    #   - 컴플라이언스 QA(compliance)는 결정론 검사이므로 실행 가능하지만,
    #     api_payload 가 없으므로 최소 문맥만으로 검사. 원본 정책을 존중해
    #     compliance 는 context 기반 최소 검사만 수행한다.
    image_result = qa_agents._qa_agent_result(
        "image",
        qa_agents.PENDING,
        [
            {
                "rule": "이미지 QA 대기",
                "severity": qa_agents.PENDING,
                "detail": (
                    "이미지 QA 는 래스터 렌더를 요구하지만 이 파이프라인은 HTML 을 "
                    "만든다. 육안 또는 LLM 확인이 필요하다(PENDING)."
                ),
            }
        ],
        "이미지 QA PENDING — 육안/LLM 확인 필요",
    )
    # 카피 QA: 결정론 코드검사는 detail_html 의 가시 텍스트에 대해서만 수행.
    # CSS/Script 블록 안의 "100%" 등이 금지 표현으로 오탐지되는 것을 방지한다.
    _copy_check_text = re.sub(
        r"<(style|script)\b[^>]*>.*?</\1\s*>",
        "",
        detail_html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    copy_code = qa_agents._copy_code_check(name, _copy_check_text)
    local_copy_verdict = qa_agents._clamp_verdict(copy_code.get("verdict"), default=qa_agents.PASS)
    if local_copy_verdict == qa_agents.FAIL:
        copy_result = qa_agents._normalize_agent_result(copy_code, "copy")
    else:
        # 로컬 검사는 PASS/WARN 이어도 LLM 판단이 필요 → PENDING.
        copy_result = qa_agents._normalize_agent_result(
            {
                "agent": "copy",
                "verdict": qa_agents.PENDING,
                "violations": copy_code.get("violations") or [],
                "summary": (f"카피 QA PENDING — LLM 판단 대기(로컬 코드검사={local_copy_verdict})"),
            },
            "copy",
        )
    # 컴플라이언스: 등록 단계가 만들 페이로드와 *동일한 해석* 으로 검사한다.
    # 등록 단계가 쓰는 빌더(``naver_client.build_payload``)로 임시 페이로드를
    # 만들어 컴플라이언스에 넘긴다 — 원산지/AS/고시 기본값/공통 5필드가 모두
    # 반영된, 등록 시 실제로 만들어질 값으로 검사한다. 두 단계가 다른 것을
    # 보는 근본 원인을 해결한다(컴플라이언스 문맥 불일치).
    #
    # 예외 처리: 빌더는 필수 설정(원산지 등)이 없으면 예외를 던진다. 준비 단계의
    # 역할은 "무엇이 부족한지 알려주는 것" 이므로, 예외를 컴플라이언스 위반 +
    # needs_user 요청으로 번역한다 (예외가 그대로 터지면 정상 흐름이 막힌다).
    # 미리보기가 등록 단계와 같은 고시 값을 보여주려면 임시 페이로드가
    # 필요하다. 컴플라이언스 검사 경로에서 만들어지지만, 예외 시에는
    # None 로 떨어뜨린다 — 미리보기는 있으면 좋고 없어도 준비는 산다.
    tentative_payload = None
    inferred_type = None
    try:
        tentative_payload = _build_tentative_register_payload(
            d, name, category_id, listing_urls, detail_html, resolved_tags=final_tags
        )
        # 컴플라이언스 컨텍스트에 카테고리 경로를 포함한다 — 고시 타입 추론이
        # 등록 단계(mcp_server._build_compliance_context)와 *같은 입력* 으로
        # 이루어지게 한다. 경로가 없으면 ETC 로 떨어지고, 등록 단계도 같은
        # lookup 을 쓰므로 역시 ETC 로 떨어진다(두 단계 합의).
        cat_path = _category_path_for(category_id)
        # 등록 단계(mcp_server._build_compliance_context)와 *동일한* 보정:
        # 카테고리 경로에서 추론한 고시 타입을 notice 의
        # productInfoProvidedNoticeType 에 명시적으로 반영한다.
        # ``_compliance_code_check`` 가 api_payload 의 notice 를 우선 읽으므로,
        # 여기에 명시 타입이 없으면 ``_infer_notice_type`` 은 notice 만 보고
        # 경로를 잃어 ETC 로 떨어진다 — 등록 단계가 FURNITURE 로 보는 같은
        # 카테고리에서 불일치가 생긴다. 등록 단계가 하는 보정을 그대로 적용한다.
        inferred_type = qa_agents._infer_notice_type(
            {"category_path": cat_path, "category_name": cat_path}
        )
        tentative_payload = _inject_notice_type(tentative_payload, inferred_type)
        compliance_context = {
            "category_id": category_id,
            "category_path": cat_path,
            "category_name": cat_path,
        }
        compliance_result = qa_agents._normalize_agent_result(
            qa_agents._compliance_code_check(
                name,
                compliance_context,
                api_payload=tentative_payload,
                deferred_notice_fields=sane_deferred or None,
            ),
            "compliance",
        )
    except Exception as exc:
        compliance_result = qa_agents._qa_agent_result(
            "compliance",
            qa_agents.FAIL,
            [
                {
                    "rule": "등록 페이로드 생성 불가",
                    "severity": qa_agents.FAIL,
                    "detail": (
                        "등록 단계가 만들 페이로드를 조립하는 중 필수값 누락 등으로 "
                        f"실패했습니다: {exc}. 해당 항목을 보완해야 등록할 수 있습니다."
                    ),
                }
            ],
            "컴플라이언스 FAIL — 등록 페이로드 생성 불가",
        )
    qa_result = qa_agents.aggregate_qa_results([image_result, copy_result, compliance_result])

    # --- 5. needs_llm / needs_user 구성 ---
    needs_llm = []
    needs_user = []
    # 카피 QA 가 PENDING 이면 LLM 위임 필요.
    if qa_agents._clamp_verdict(copy_result.get("verdict")) == qa_agents.PENDING:
        needs_llm.append(
            {
                "agent": "copy",
                "why": "카피 품질 LLM 판단이 필요합니다. submit_reviews 로 회신.",
                "hint": detail_render.needs_llm_for_copy(product_for_render),
            }
        )
    # 이미지 QA 가 PENDING 이면 LLM 또는 육안 확인 필요.
    if qa_agents._clamp_verdict(image_result.get("verdict")) == qa_agents.PENDING:
        needs_llm.append(
            {
                "agent": "image",
                "why": "이미지 적합성 LLM/육안 확인이 필요합니다. submit_reviews 로 회신.",
            }
        )
    # 고시 필수 필드 누락 등은 사용자 입력 필요(compliance FAIL/WARN 인 경우).
    comp_verdict = qa_agents._clamp_verdict(
        compliance_result.get("verdict"), default=qa_agents.PASS
    )
    if comp_verdict in (qa_agents.WARN, qa_agents.FAIL):
        for v in compliance_result.get("violations") or []:
            if isinstance(v, dict):
                needs_user.append(
                    {
                        "field": str(v.get("rule") or "고시"),
                        "label": str(v.get("rule") or "고시 항목"),
                        "why": str(v.get("detail") or "사용자 입력이 필요합니다."),
                    }
                )
    # 이미지 생성 단계 분기에서 키 부재/실패 안내가 있으면 needs_user 에 싣는다
    # (조용한 실패 금지 — 생성을 요청했는데 못 한 사실을 사용자가 알아야 한다).
    if generation_user_hint is not None:
        needs_user.append(generation_user_hint)
    # 태그 제한 판정된 사용자 태그를 needs_user 에 알린다 (조용한 드롭 금지).
    # 사용자가 직접 준 태그가 제한어로 판정되어도 삭제하지는 않지만, 판매자가
    # 그 사실을 알아야 한다(네이버 등록 시 seller_tags 백스톱이 최종적으로
    # 제거할 수 있다). 추천 태그가 제한이면 이미 final_tags 에 들어가지 않았다.
    _user_restricted_tags = [
        r["tag"] for r in (tag_resolution.get("restricted") or []) if r.get("source") == "user"
    ]
    if _user_restricted_tags:
        needs_user.append(
            {
                "field": "tags_restricted",
                "label": "태그 제한 경고",
                "why": (
                    "다음 사용자 태그가 네이버 제한어로 판정되었습니다(삭제하지 않음 — "
                    f"등록 시 백스톱이 처리): {', '.join(_user_restricted_tags)}. "
                    "태그를 그대로 두거나 수정하세요."
                ),
            }
        )
    # 태그 API 호출 실패(fail-open) 사유도 needs_user 에 알린다(조용한 실패 금지).
    # 단, 추천/제한 API 가 명시적으로 ok=False 임을 tag_resolution.error 가 나타낼
    # 때만 싣는다 — 정상 경로에서는 error 가 None 이다.
    if tag_resolution.get("error"):
        needs_user.append(
            {
                "field": "tags_lookup_failed",
                "label": "태그 추천/제한 조회 실패",
                "why": (
                    "네이버 태그 API 조회가 실패해 추천 기반 태그 채움을 건너뛰었습니다. "
                    f"사유: {tag_resolution['error']}. 사용자 태그만으로 진행합니다."
                ),
            }
        )
    # deferred_notice_fields 검증에서 거부된 필드를 needs_user 에 알린다
    # (조용한 누락 금지). 판매자가 미루기로 선언했지만 원산지/allowlist 규칙에
    # 의해 거부된 필드는 미루기가 불가능하므로 값을 직접 채워야 한다.
    if deferred_rejected:
        needs_user.append(
            {
                "field": "deferred_notice_fields_rejected",
                "label": "미루기 불가 고시 필드",
                "why": (
                    "다음 필드는 '상세페이지 참조' 로 미룰 수 없습니다 "
                    f"(원산지 계열 또는 allowlist 밖): {', '.join(deferred_rejected)}. "
                    "해당 필드의 실제 값을 입력해야 합니다."
                ),
            }
        )

    # 속성 확신 제안이 없고 후보가 있으면 사용자 선택을 요청한다(조용한 생략 금지).
    if attributes_needs_user_hint is not None:
        needs_user.append(attributes_needs_user_hint)

    # 이름↔팩트 모순이 있으면 사용자 확인을 요청한다(등록 단계가 거부한다).
    if name_fact_check.get("status") == "conflict":
        _nfc_summary = "; ".join(
            f"{c.get('topic')}: 이름={c.get('name_says')}/팩트={c.get('fact_says')}({c.get('rule')})"
            for c in (name_fact_check.get("conflicts") or [])
            if isinstance(c, dict)
        )
        needs_user.append(
            {
                "field": "name",
                "label": "상품명 사실 확인",
                "why": (
                    "상품명과 팩트가 서로 다른 사실을 말합니다(오역·사실 오류 가능 — "
                    f"네이버 기재불일치 랭크다운 사유): {_nfc_summary}. 어느 쪽이 맞는지 "
                    "확인하고 이름 또는 팩트를 고친 뒤 다시 준비하세요."
                ),
                "answer_shape": "text",
            }
        )

    # --- 6. prepared payload 저장 ---
    payload = {
        "product_key": product_key,
        "product": {
            "name": name,
            "categoryId": category_id,
            "salePrice": int(sale_price),
            "options": options,
            "tags": final_tags,
            "notice": d.get("notice") or {},
            "origin_code": d.get("origin_code") or "",
            "manufacturer": d.get("manufacturer") or "",
            "importer": d.get("importer") or "",
            "as_tel": d.get("as_tel") or "",
            "seller_tel": d.get("seller_tel") or "",
            "as_guide": d.get("as_guide") or "",
            "courier": d.get("courier") or "",
            # delivery_fee: 키가 있을 때만 넣는다 (기본값 3000 은 _notice_defaults
            # 한 곳에서만 결정 — 키가 없으면 config 폴백이 발동해야 한다).
            # option_groups: 다축 옵션의 그룹 이름(예: ["색상","사이즈"]).
            # naver_client._option_group_list 가 "option_groups" 키를 읽어
            # optionCombinationGroupNames 를 채운다. 이 키가 빠지면 폴백으로
            # "옵션1"/"옵션2" 번호 이름이 붙는다 — prepared product block 에서
            # 이 키가 빠지면 register_prepared_listing 경로가 그룹 이름을 잃는다
            # (mcp_server.register_product 의 직접 경로와 불일치).
            # mcp_server.register_product 와 동일하게, 값이 주어질 때만 싣는다.
            "option_groups": list(d.get("option_groups") or []),
        },
        "images": {
            "listing_urls": listing_urls,
            "detail_urls": detail_urls,
        },
        "detail_html": detail_html,
        "scene": scene,
        "needs_llm": needs_llm,
        "needs_user": needs_user,
        "qa": qa_result,
        "status": d.get("status") or "SALE",
    }
    # deferred_notice_fields: 판매자가 "상세페이지 참조" 로 미루기로 *준비 단계에서*
    # 명시적으로 선택한 고시 필드명 리스트. 등록 단계(register_product) 가 이 값을
    # 받아 컴플라이언스 게이트의 "고시 필수필드 누락" 위반에서 제외한다.
    #
    # 위에서 이미 원산지/allowlist 검증을 거친 ``sane_deferred`` 를 그대로 저장한다.
    # 컴플라이언스 검사에 넘긴 값과 저장하는 값이 같아야 준비 통과 → 등록 통과
    # 일관성이 성립한다. 여기서 다시 정제하면 검사에 쓴 값과 저장값이 어긋난다.
    # delivery_fee: **실질값이 있을 때만** 넣는다 (기본값 3000 은 _notice_defaults
    # 한 곳에서만 결정 — 키가 없으면 config 폴백이 발동해야 한다).
    # 감리 ⑤ (4라운드): 빈 값(None/""/공백)이 prepared 에 저장되면 다음 단계가
    # "명시값 있음" 으로 오인한다 — 생략 보존 원칙(이 PR 의 핵심 원칙)과 같다.
    _delivery_fee_raw = d.get("delivery_fee")
    if _delivery_fee_raw is not None and str(_delivery_fee_raw).strip() != "":
        payload["product"]["delivery_fee"] = _delivery_fee_raw
    # attributes: 명시적 상품속성 ID 리스트. **값이 주어질 때만** 넣는다
    # (None 보존 — 빈 배열 전송 금지, 미실측 거동). prepare_listing 은 형태를
    # 검증하지 않고 저장만 한다 — 형태 검증은 register_product → build_payload →
    # _validate_product_attributes 가 담당한다(판정 한 곳에서만).
    _attributes_raw = d.get("attributes")
    if _attributes_raw is not None:
        payload["product"]["attributes"] = list(_attributes_raw)
    if sane_deferred:
        payload["deferred_notice_fields"] = list(sane_deferred)
    # deferred_rejected: 미루려 했으나 원산지/allowlist/boolean-date 규칙으로
    # 거부된 필드 목록. 등록 단계가 등록 기록(registration_record.json) 에
    # "거부된 미루기" 로 남기기 위해 필요하다 — 조용히 버리면 판매자는 미뤘다고
    # 믿는데 실제로는 안 미뤄진 상태가 된다. **빈 리스트도 명시적으로** 남긴다
    # (키 없음 ≠ 거부 0건, 조용한 빈 값 금지).
    payload["deferred_rejected"] = list(deferred_rejected)
    # notice_type: 준비 단계에서 추론한 고시 타입. 등록 기록에 "그때의 고시
    # 타입" 으로 남는다(이후 그 타입의 필수필드와 대조 가능). 추론 실패 시 None.
    payload["notice_type"] = inferred_type
    if image_generation_meta is not None:
        payload["image_generation"] = image_generation_meta
    if overwrite_warning is not None:
        payload["overwrite_warning"] = overwrite_warning
    # tags_meta: 태그 추천·제한 검사 결과. ``product.tags`` 가 어디서 왔는지
    # (사용자/네이버 추천), 어떤 태그가 제한 판정을 받았는지, API 호출이
    # 실패했는지를 드러낸다 (조용한 자동 채움/드롭 금지).
    payload["tags_meta"] = tag_resolution
    # 이름↔팩트 모순 게이트 결과. status 는 "ok"|"conflict"|"skipped" —
    # skipped 에도 facts 없음 사유가 담긴다(조용한 통과 금지). conflict 면
    # 등록 단계(register_product) 가 name_conflict_acknowledged 없이는 거부한다.
    payload["name_fact_check"] = name_fact_check
    # 태그·속성 자동 제안(워크오더 Part A/B). 등록 단계(register_product) 가
    # prepared 에서 읽어 자동 채용한다 — 명시 인자가 항상 우선이다.
    # 제안 불가 사유(reason)·조회 실패(attributes_error) 도 항상 남긴다
    # (조용한 생략 금지).
    payload["seo_tags_suggestion"] = seo_tags_suggestion
    if attributes_suggestion:
        payload["attributes_suggestion"] = list(attributes_suggestion)
        payload["attributes_suggestion_basis"] = list(attributes_suggestion_basis)
    # 전문 텍스트 부정 가드 차단(자동 채용 금지·후보 강등 사유) 표기.
    # attributes_suggestion(자동 채용 대상) 과 분리해 실으므로 등록 단계의
    # 자동 채용 경로가 차단값을 절대 줍지 않는다.
    if attributes_suggestion_blocked:
        payload["attributes_suggestion_blocked"] = list(attributes_suggestion_blocked)
    if attributes_error is not None:
        payload["attributes_error"] = attributes_error
    write_prepared_payload(payload)

    # --- 7. 미리보기 HTML 파일 생성 ---
    # prepared payload 와 같은 디렉터리에 preview.html 을 쓴다. 외부 리소스를
    # 참조하지 않는 단일 HTML 이며, 브라우저로 열어 판매자가 직접 눈으로
    # 확인하는 용도다. 미리보기 생성이 실패해도 준비 자체는 죽지 않는다 —
    # 그 사실은 payload 의 preview_path(None) 로 드러난다.
    #
    # **보기 전용 모드**로 쓴다 — 이 시점에 만들어지는 파일이 MCP 우측 패널에
    # 바로 표시되기 때문이다. 패널은 JS 를 실행하지도 폼을 제출하지도 못하므로,
    # ``contenteditable``/``<button>``/``<input>``/``<script>`` 가 있으면 "누를 수
    # 있게 생겼는데 안 눌리는" 죽은 UI 가 된다. 조작 모드(편집·승인 바)는 승인
    # 서버가 포트를 확정한 뒤(mcp_server.register_product 승인 대기 진입) 파일을
    # 갱신할 때 비로소 켜진다. 안전한 쪽이 기본.
    preview_path: str | None = None
    try:
        from . import preview as _preview_mod

        preview_file = _preview_mod.write_preview_html(
            product_key,
            payload,
            api_payload=tentative_payload,
            mode="view_only",
        )
        preview_path = str(preview_file)
    except Exception:
        preview_path = None
    payload["preview_path"] = preview_path
    # preview_path 를 payload 에 기록하기 위해 다시 쓴다. 쓰기 실패는
    # 준비 자체를 망가뜨리지 않는다(이미 한 번 썼다).
    try:
        write_prepared_payload(payload)
    except Exception:
        pass
    return payload


# ---------------------------------------------------------------------------
# prepared 후보 스캔 + 모호성 거부.
#
# 등록 시 product_key 를 명시하지 않으면 이름+가격으로 후보를 찾는다. 같은
# 이름·가격의 SKU 가 여러 개일 때(색상만 다른 옵션 상품 등) 후보가 2개 이상
# 나올 수 있다. 이때 조용히 하나를 고르면 다른 상품의 내용이 전송되는 조용한
# 오등록이 된다 — 모호하면 거부다.
# ---------------------------------------------------------------------------


def find_prepared_candidates(name, price):
    """이름+가격 으로 prepared 후보를 모두 찾는다.

    prepared 디렉터리의 모든 payload 를 훑어 ``product.name`` 과
    ``product.salePrice`` 가 일치하는 항목을 반환한다. 색상만 다른 SKU 처럼
    같은 이름·가격의 서로 다른 상품이 여러 prepared 로 존재할 수 있다.

    Returns:
        ``[{"key": str, "payload": dict}, ...]`` — 이름+가격 이 일치하는 모든
        prepared. 빈 리스트일 수 있다(후보 0개).
    """
    candidates = []
    _name = str(name or "").strip()
    if not _name:
        return candidates
    try:
        _price_int = int(price)
    except (TypeError, ValueError):
        return candidates
    for path in iter_prepared_payload_paths():
        data = read_prepared_payload(path)
        if not isinstance(data, dict):
            continue
        product = data.get("product")
        if not isinstance(product, dict):
            continue
        cand_name = str(product.get("name") or "").strip()
        cand_price = product.get("salePrice")
        try:
            cand_price_int = int(cand_price) if cand_price is not None else None
        except (TypeError, ValueError):
            cand_price_int = None
        if cand_name == _name and cand_price_int == _price_int:
            candidates.append({"key": str(data.get("product_key") or "").strip(), "payload": data})
    return candidates


def resolve_prepared_for_register(name, price, *, product_key=None):
    """등록 시 사용할 prepared payload 와 추적 정보를 결정.

    - **명시 ``product_key`` 가 주어지면** 그것을 그대로 로드한다(정확).
    - **주어지지 않으면** 이름+가격 으로 후보를 찾는다:
      - 후보가 **정확히 1개** → 그것을 사용(하위호환).
      - 후보가 **2개 이상** → ``ValueError`` 로 거부. 네이버 호출 0회.
        ``product_key`` 를 지정하라고 안내한다. **조용히 하나를 고르지 않는다 —
        이것이 이번 결함의 본질이다.**
      - 후보가 0개 → ``(None, {})`` 반환 (호출자가 명시 인자만으로 진행하거나 거부).

    Returns:
        ``(payload_or_None, lookup_info)`` — ``lookup_info`` 는 어느 키를 어디서
        어떻게 찾았는지 드러낸다::

            {"key": str, "source": "explicit"|"derived"|"none",
             "name": str, "salePrice": int|None}

    Raises:
        ValueError: 후보가 2개 이상이어서 모호성으로 거부할 때.
    """
    # 명시 키가 있으면 그것을 쓴다(정확).
    explicit_key = str(product_key or "").strip()
    if explicit_key:
        try:
            payload = load_prepared_payload(product_key=explicit_key)
        except (FileNotFoundError, ValueError):
            return None, {
                "key": explicit_key,
                "source": "explicit",
                "name": "",
                "salePrice": None,
            }
        _p = payload.get("product") if isinstance(payload.get("product"), dict) else {}
        return payload, {
            "key": explicit_key,
            "source": "explicit",
            "name": str(_p.get("name") or ""),
            "salePrice": _p.get("salePrice"),
        }

    # 명시 키가 없으면 이름+가격 으로 후보를 찾는다.
    candidates = find_prepared_candidates(name, price)
    if len(candidates) == 1:
        cand = candidates[0]
        _p = (
            cand["payload"].get("product")
            if isinstance(cand["payload"].get("product"), dict)
            else {}
        )
        return cand["payload"], {
            "key": cand["key"],
            "source": "derived",
            "name": str(_p.get("name") or ""),
            "salePrice": _p.get("salePrice"),
        }
    if len(candidates) >= 2:
        # 모호하면 거부한다 — 조용히 하나를 고르지 않는다.
        keys = [c["key"] for c in candidates]
        raise ValueError(
            f"같은 이름·가격의 prepared 가 {len(candidates)}개 있어 어느 것을 "
            f"등록할지 결정할 수 없다 (조용한 선택 금지). product_key 를 명시적으로 "
            f"지정하세요. 후보 키: {keys}"
        )
    # 후보 0개 — 호출자가 판단.
    return None, {"key": "", "source": "none", "name": "", "salePrice": None}


__all__ = [
    "MAX_SELLER_TAGS",
    "_build_product_dict",
    "_build_register_product_dict",
    "_build_tentative_register_payload",
    "_category_path_for",
    "_extract_channel_product_no",
    "_fingerprint_sources",
    "_inject_notice_type",
    "_prepared_dir",
    "_prepared_item_dir",
    "_prepared_payload_path",
    "_registration_record_path",
    "_reject_url_inputs",
    "_resolve_tags",
    "_sanitize_product_key",
    "_validate_review_submission",
    "find_prepared_candidates",
    "inject_prepared_qa",
    "iter_prepared_payload_paths",
    "load_prepared_payload",
    "make_product_key",
    "prepare_listing",
    "read_prepared_payload",
    "read_registration_record",
    "register_listing",
    "register_prepared_listing",
    "resolve_prepared_for_register",
    "resolve_product_key",
    "submit_reviews",
    "summarize_recorded_deferred",
    "write_prepared_payload",
    "write_registration_record",
]
