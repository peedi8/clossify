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
):
    """등록 결과를 디스크에 기록한다.

    저장 위치는 prepared payload 가 사는 디렉터리 규약을 따른다(새 규약 금지).
    같은 ``product_key`` 하위의 ``registration_record.json``.

    **빈 값 가드**: ``channel_product_no`` 가 없으면 **조용히 넘기지 않는다** —
    반환 dict 에 ``channel_product_no: None`` 과 ``missing_channel_no: True`` 를
    드러낸다(이후 수정이 불가능해진다는 뜻이므로 사용자가 알아야 한다). 파일은
    채널번호가 있을 때만 기록한다.

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
    record = {
        "product_key": sane_key,
        "origin_product_no": origin_product_no,
        "channel_product_no": ch_no,
        "name": name,
        "salePrice": sale_price,
        "categoryId": category_id,
        "requested_status": requested_status,
        "applied_status": applied_status,
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
    return {
        "name": name[:50],
        "categoryId": str(category_id or d.get("categoryId") or d.get("category_id") or ""),
        "salePrice": int(sale_price),
        "options": d.get("options") or [],
        "tags": d.get("tags") or [],
        "courier": d.get("courier") or "CJGLS",
        "delivery_fee": d.get("delivery_fee", 3000),
        "notice": d.get("notice") or {},
        "as_tel": d.get("as_tel") or "",
        "as_guide": d.get("as_guide") or "",
        "origin_code": d.get("origin_code") or "",
        "manufacturer": d.get("manufacturer") or "",
        "importer": d.get("importer") or "",
    }


def _apply_qa_to_payload(payload, qa_result):
    """QA 집계 결과를 payload 에 붙인다."""
    if not isinstance(payload, dict):
        return payload
    payload["qa"] = qa_result
    return payload


def _build_register_product_dict(d, name, category_id):
    """register 단계가 naver_client.build_payload 에 넘길 상품 dict 와 동일한 형태를 구성.

    준비 단계의 컴플라이언스 검사가 등록 단계와 *같은 해석* 을 보려면, 컴플라이언스에
    넘기는 임시 페이로드를 register 단계가 만드는 것과 같은 빌더(``naver_client.
    build_payload``)로 만들어야 한다. 본 함수는 그 빌더에 들어갈 상품 dict 를
    ``mcp_server.register_product`` 와 동일한 키 셋으로 조립한다.

    빌더 자체는 호출하지 않고 dict 만 반환한다(호출은 호출자의 책임). 상품명 50자
    절단은 빌더 내부에서 이뤄지므로 여기서는 원본 이름을 그대로 둔다.
    """
    sale_price = d.get("salePrice")
    if sale_price is None:
        sale_price = d.get("sell_price") or d.get("price")
    product = {
        "name": name,
        "categoryId": str(category_id or d.get("categoryId") or d.get("category_id") or ""),
        "salePrice": int(sale_price),
        "tags": list(d.get("tags") or []),
        "stock": int(d.get("stock", 1)),
        "delivery_fee": int(d.get("delivery_fee", 3000)),
        "courier": d.get("courier") or "CJGLS",
    }
    if d.get("options"):
        product["options"] = d.get("options")
    notice = d.get("notice")
    if notice is not None:
        product["notice"] = notice
    # 원산지/AS/제조사/수입자 등 규제값 — 빌더가 config 폴백으로 읽는 후보 키.
    for key in (
        "origin_code",
        "as_tel",
        "as_guide",
        "manufacturer",
        "importer",
        "made_in",
        "origin_content",
        "cert_detail",
        "quality_assurance_standard",
        "return_cost_reason",
        "no_refund_reason",
        "compensation_procedure",
        "trouble_shooting_contents",
    ):
        value = d.get(key)
        if value:
            product[key] = value
    return product


def _build_tentative_register_payload(d, name, category_id, listing_urls, detail_html):
    """등록 단계가 만들 페이로드를 임시로 빌드한다 (컴플라이언스 검사용).

    ``naver_client.build_payload`` 는 등록 단계와 *동일한* 규제값 해석(origin/AS/
    고시 기본값/공통 5필드 포함)을 페이로드에 반영한다. 본 함수로 그 빌더를 한 번
    호출해 임시 페이로드를 만들면, 준비 단계의 컴플라이언스 검사가 등록 시 실제로
    만들어질 값과 동일한 문맥을 보게 된다 — 두 단계가 어긋날 수 없다.

    Raises:
        ValueError: 필수 설정(원산지 등)이 없어 빌더가 페이로드를 만들 수 없을 때.
            호출자는 이것을 컴플라이언스 위반 + needs_user 로 번역해야 한다
            (준비 단계에서 예외가 그대로 터지면 안 된다).
    """
    from . import naver_client as _nc

    product = _build_register_product_dict(d, name, category_id)
    status = d.get("status") or "SALE"
    return _nc.build_payload(product, detail_html, listing_urls, status=status)


def _category_path_for(category_id):
    """``category_id`` 의 카테고리 경로를 반환 (알 수 없으면 빈 문자열).

    준비 단계와 등록 단계가 고시 타입 추론을 같은 입력으로 하게 한다.
    등록 단계(``mcp_server._category_path_for``)와 *동일한* lookup 을 쓴다.
    데이터 파일 부재·알 수 없는 ID 는 조용히 빈 문자열로 떨어진다 — 이 경우
    양쪽 모두 ETC 기본값으로 합의하므로 불일치가 생기지 않는다(fail-closed
    규칙을 위반하지 않는다: 알 수 없음을 알 수 없음으로 다룬다).
    """
    try:
        from . import category_meta

        return category_meta.category_path(category_id, raise_if_unknown=False)
    except Exception:
        return ""


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


def prepare_listing(d, *, attach_fn=None):
    """상품 정보 + 이미지 소스 로 prepared payload 를 만든다.

    본 함수는 등록 전 단계를 수행한다: 이미지 정규화, 상세 HTML 렌더, QA 집계
    (이미지 QA 는 PENDING 등록 — JPEG 의존 항목은 이 파이프라인에서 실행하지
    않는다). 결과를 prepared payload 로 저장하고 반환한다.

    Args:
        d: 상품 입력 dict. 필수: ``name``, ``salePrice``, ``image_sources``
            (이미지 소스 리스트 — 로컬 경로/CDN URL/외부 URL 혼합).
            선택: ``options``, ``tags``, ``notice``, ``category_id`` 등.
        attach_fn: ``images.attach_images`` 대체(테스트 주입용).

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
          - ``version``: ``common.PREPARED_PAYLOAD_VERSION``.

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
    try:
        tentative_payload = _build_tentative_register_payload(
            d, name, category_id, listing_urls, detail_html
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
                name, compliance_context, api_payload=tentative_payload
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

    # --- 6. prepared payload 저장 ---
    payload = {
        "product_key": product_key,
        "product": {
            "name": name,
            "categoryId": category_id,
            "salePrice": int(sale_price),
            "options": options,
            "tags": d.get("tags") or [],
            "notice": d.get("notice") or {},
            "origin_code": d.get("origin_code") or "",
            "manufacturer": d.get("manufacturer") or "",
            "importer": d.get("importer") or "",
            "as_tel": d.get("as_tel") or "",
            "as_guide": d.get("as_guide") or "",
            "courier": d.get("courier") or "CJGLS",
            "delivery_fee": d.get("delivery_fee", 3000),
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
    if overwrite_warning is not None:
        payload["overwrite_warning"] = overwrite_warning
    write_prepared_payload(payload)

    # --- 7. 미리보기 HTML 파일 생성 ---
    # prepared payload 와 같은 디렉터리에 preview.html 을 쓴다. 외부 리소스를
    # 참조하지 않는 단일 HTML 이며, 브라우저로 열어 판매자가 직접 눈으로
    # 확인하는 용도다. 미리보기 생성이 실패해도 준비 자체는 죽지 않는다 —
    # 그 사실은 payload 의 preview_path(None) 로 드러난다.
    preview_path: str | None = None
    try:
        from . import preview as _preview_mod

        preview_file = _preview_mod.write_preview_html(
            product_key,
            payload,
            api_payload=tentative_payload,
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
    "write_prepared_payload",
    "write_registration_record",
]
