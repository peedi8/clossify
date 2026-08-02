# -*- coding: utf-8 -*-
"""등록 오케스트레이션 (T-201b-r).

원본 ``sourcing.py`` 의 등록 파이프라인을 이식하되, 작업지시의 핵심 개정사항을
반영한다:

1. **URL 입력 거부**: 요청 dict 에 ``url``/``source_url``/``item_url``/
   ``detail_url`` 중 하나라도 키가 존재하면 ``ValueError``. 값이 비어 있어도
   키 존재만으로 거부. payload 에 ``source_url`` 필드를 만들지 않는다.
2. **product_key**: 외부 마켓 ID(``num_iid`` 등)를 쓰지 않는다. 호출자가 주지
   않으면 ``sha1(상품명 + 가격)[:12]`` 로 생성. 빈 문자열/공백 키는 거부.
3. **가격 KRW 직입력**: 원가/환율/해외배송 기반 계산은 이식하지 않는다.
4. **이미지 파이프라인**: 리터치·시트 병합·상세 렌더 의존 지점은 명시 스텁
   (``NotImplementedError("T-201c: ...")``).
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

from . import common
from . import qa_agents


# ---------------------------------------------------------------------------
# URL 입력 거부 (작업지시 핵심).
#
# 외부 마켓 수집은 본 제품에서 지원하지 않는다. ``url``/``source_url``/
# ``item_url``/``detail_url`` 키 중 하나라도 존재하면 거부한다 — 값이 비어
# 있어도 키 존재만으로 거부 (조용한 허용 금지).
# ---------------------------------------------------------------------------

_URL_INPUT_KEYS = frozenset({"url", "source_url", "item_url", "detail_url"})


def _reject_url_inputs(d):
    """URL 기반 수집 키가 존재하면 ``ValueError`` 를 발생시킨다.

    작업지시: "값이 비어 있어도 키 존재만으로 거부한다."
    """
    if isinstance(d, dict):
        present = sorted(k for k in _URL_INPUT_KEYS if k in d)
        if present:
            raise ValueError(
                "URL 기반 수집은 지원하지 않습니다. 상품 정보를 직접 전달하세요. "
                f"(감지된 키: {', '.join(present)})"
            )


# ---------------------------------------------------------------------------
# product_key 생성 (작업지시 핵심).
#
# 외부 마켓 ID 를 쓰지 않는다. ``sha1(상품명 + 가격)[:12]`` 로 생성.
# 빈 문자열/공백 키는 거부 (디렉터리 충돌·무음 덮어쓰기 방지).
# ---------------------------------------------------------------------------

def _sanitize_product_key(key):
    """product_key 를 파일시스템 안전 문자열로 정규화.

    ``[0-9A-Za-z_-]`` 외 문자는 ``_`` 로 치환, 80자로 절삭.
    """
    return re.sub(r"[^0-9A-Za-z_-]", "_", str(key or ""))[:80]


def make_product_key(name, price):
    """상품명 + 가격 으로 product_key 생성 (``sha1[:12]``).

    작업지시: "호출자가 주지 않으면 ``sha1(상품명 + 가격)[:12]`` 로 생성."

    Args:
        name: 상품명 (한국어).
        price: KRW 가격 (int/str).

    Returns:
        12자 hex product_key.

    Raises:
        ValueError: 상품명이 빈 문자열/공백인 경우.
    """
    name_str = str(name or "").strip()
    if not name_str:
        raise ValueError(
            "product_key 생성에 필요한 상품명이 비어 있습니다 (빈 키 방지)."
        )
    price_str = str(price if price is not None else "").strip()
    raw = f"{name_str}+{price_str}"
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
        raise ValueError(
            "product_key 를 생성할 수 없습니다 — 상품명이 비어 있습니다."
        )
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
        raise ValueError(
            f"product_key 가 prepared 디렉터리를 벗어납니다: {key}"
        ) from exc
    return item_dir


def _prepared_payload_path(product_key):
    """prepared payload JSON 파일 경로."""
    return _prepared_item_dir(product_key) / "payload.json"


def write_prepared_payload(payload):
    """prepared payload 를 디스크에 쓴다 (source L12660-L12667 보존).

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
    """prepared payload JSON 을 읽는다 (source L12670-L12676 보존)."""
    return common._read_json_file(path, None)


def load_prepared_payload(*, product_key=None):
    """product_key 로 prepared payload 를 로드.

    작업지시가 원본의 ``prepare_token`` 기반 접근 제어를 단순화했다 —
    본 이식판은 product_key 만으로 로드한다 (외부 마켓 ID 불가).

    Raises:
        FileNotFoundError: payload 가 없을 때.
        ValueError: product_key 가 비었을 때.
    """
    key = str(product_key or "").strip()
    if not key:
        raise ValueError("product_key 가 필요합니다.")
    path = _prepared_payload_path(key)
    data = read_prepared_payload(path)
    if data is None:
        raise FileNotFoundError(f"prepared payload 를 찾을 수 없습니다: {path}")
    return data


def iter_prepared_payload_paths():
    """모든 prepared payload 경로를 mtime 역순으로 yield (source L12678-L12681)."""
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
# 등록 오케스트레이션.
# ---------------------------------------------------------------------------

def _build_product_dict(d, seo_title, category_id):
    """Naver 등록용 상품 dict 구성 (source L12864-L12898, 개정).

    작업지시 반영:
      - 가격은 KRW 직접 입력 (``d.salePrice`` 또는 ``d.sell_price``).
      - ``num_iid``/외부 마켓 ID 사용 금지 — modelName 은 config/입력만.
      - notice 기본값은 ``naver_client._notice_defaults`` 에 위임 (KC/원산지
        하드코딩 금지는 이미 naver_client T-107 반영됨).
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
        "name": name[: common.PREPARED_PAYLOAD_VERSION and 50 or 50],
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


def register_prepared_listing(d):
    """prepared payload 를 로드해 네이버 상품 등록을 수행 (source L13326-L13478).

    흐름:
      1. URL 입력 거부 검사
      2. ``product_key`` 결정
      3. prepared payload 로드
      4. QA 게이트 판정 (``qa_agents.qa_gate``) — PENDING/FAIL 차단
      5. 이미지 업로드 스텁 (T-201c)
      6. ``naver_client.build_payload`` + ``register_product`` 호출
      7. 등록 후 조회 재검증 (``naver_client.get_product``) — 원본에 없던 단계

    Returns:
        결과 dict (``ok``, ``originProductNo``, ...).
    """
    _reject_url_inputs(d)
    product_key = resolve_product_key(d)
    payload = load_prepared_payload(product_key=product_key)

    # QA 게이트 — fail-closed (PENDING/FAIL 차단).
    allowed, reason = qa_agents.qa_gate(payload)
    if not allowed:
        return {"ok": False, "blocked": True, "reason": reason, "product_key": product_key}

    # 이미지 업로드 — T-201c 의존 스텁.
    listing_image_paths = payload.get("listing_image_paths") or []
    detail_segment_paths = payload.get("detail_segment_paths") or []
    naver_images = _upload_listing_images(listing_image_paths)
    detail_images = _upload_detail_images(detail_segment_paths)

    product = payload.get("product") or {}
    seo_title = str(product.get("name") or "")
    detail_html = _render_detail_html(payload, detail_images, seo_title)

    from . import naver_client as nc

    status = d.get("status") or payload.get("status") or "SALE"
    api_payload = nc.build_payload(product, detail_html, naver_images, status=status)

    # 등록 API 호출.
    result = nc.register_product(api_payload)
    if isinstance(result, tuple):
        status_code, body = result
    else:
        status_code, body = 200, result

    ok = _is_register_success(status_code, body)
    origin_product_no = _extract_origin_product_no(body)

    # 등록 후 조회 재검증 (원본에 없는 단계 — 작업지시가 요구).
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
        "verify": verify,
    }
    try:
        write_prepared_payload(payload)
    except Exception:
        pass

    return {
        "ok": ok,
        "status_code": status_code,
        "originProductNo": origin_product_no,
        "verify": verify,
        "product_key": product_key,
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
    return body.get("originProductNo") or (
        body.get("originProduct") or {}).get("id") if isinstance(body, dict) else None


def register_listing(d):
    """하위호환 등록 디스패처 (source L13584-L13594, 개정).

    작업지시 반영:
      - URL 입력은 ``_reject_url_inputs`` 에서 이미 거부됨.
      - ``prepare_listing`` 본체는 T-201c 범위 — 본 함수는 호출부를 삭제하지
        않고 명시 스텁으로 둔다.
      - ``product_key`` 또는 ``prepare_token`` 이 있으면 등록(commit)만 수행.

    Args:
        d: 요청 dict. ``product_key`` 또는 ``name``+``salePrice`` 필요.

    Raises:
        ValueError: URL 키 존재, 또는 product_key 생성 불가.
    """
    _reject_url_inputs(d)
    if d.get("product_key") or (d.get("name") and d.get("salePrice") is not None):
        return register_prepared_listing(d)
    # prepared 참조도 없고 상품 입력도 불충분 — prepare_listing 본체(T-201c) 필요.
    raise NotImplementedError(
        "T-201c: prepare_listing 본체가 필요합니다 "
        "(prepare_listing). 현재 register_listing 은 product_key 가 있거나 "
        "name+salePrice 가 있을 때만 register_prepared_listing 으로 위임합니다."
    )


# ---------------------------------------------------------------------------
# inject_prepared_qa — 단일 정의 (원본 중복 정의 통합).
#
# 작업지시: "원본에 같은 이름 함수가 두 번 정의된 것이 있으면(뒤가 이김) 뒤 정의만
# 가져오고 그 사실을 보고한다." 원본의 ``inject_prepared_qa`` 는 두 번 정의되었고
# (L13481-L13507, L13510-L13550), 뒤 정의가 이김 — 다중 에이전트 입력을 지원하는
# 쪽이다. 본 이식판은 단일 정의로 통합하고 다중 에이전트 입력을 지원한다.
# ---------------------------------------------------------------------------

def inject_prepared_qa(d):
    """외부 세션/사용자 검수 결과를 prepared payload 에 반영 (개정 통합판).

    원본의 두 정의를 통합:
      - ``agents``/``qa_agents`` 리스트 또는 ``image``/``copy``/``compliance``
        개별 dict 입력을 모두 지원한다.
      - ``{verdict, violations, summary}`` 레거시 형태도 지원.

    Args:
        d: ``{"product_key": ..., "agents": [...]}`` 또는 ``{"product_key": ...,
            "image": {...}, "copy": {...}, "compliance": {...}}``.

    Returns:
        갱신된 payload 의 QA 블록.
    """
    _reject_url_inputs(d)
    product_key = resolve_product_key(d)
    payload = load_prepared_payload(product_key=product_key)

    agent_rows = []
    if isinstance(d.get("agents"), list):
        agent_rows = d["agents"]
    elif isinstance(d.get("qa_agents"), list):
        agent_rows = d["qa_agents"]
    else:
        for name in ("image", "copy", "compliance"):
            row = d.get(name)
            if isinstance(row, dict):
                row = dict(row)
                row.setdefault("agent", name)
                agent_rows.append(row)
    if not agent_rows and isinstance(d.get("verdict"), str):
        agent_rows = [{"agent": "external", "verdict": d["verdict"],
                       "violations": d.get("violations") or [],
                       "summary": d.get("summary") or ""}]
    qa_result = qa_agents.aggregate_qa_results(agent_rows)
    qa_result["source"] = "external"
    _apply_qa_to_payload(payload, qa_result)
    write_prepared_payload(payload)
    return qa_result


# ---------------------------------------------------------------------------
# T-201c 스텁 — 이미지 파이프라인.
#
# 작업지시: "이미지 파이프라인(리터치·시트 병합·상세 렌더) 의존 지점은 명시
# 스텁으로 두고 waiting_on: T-201c 를 사유에 적는다."
# ---------------------------------------------------------------------------

def _upload_listing_images(paths):
    """리스팅 이미지 업로드 (T-201c 스텁).

    원본은 ``nc.upload_images(paths)`` 로 네이버 이미지서버에 업로드했다.
    본 이식판은 실제 네이버 API 호출을 하지 않는다(Hard Constraint 4) — 호출부를
    스텁으로 둔다.
    """
    raise NotImplementedError(
        "T-201c: 리스팅 이미지 업로드 파이프라인이 필요합니다 "
        "(upload_images)."
    )


def _upload_detail_images(paths):
    """상세 이미지 업로드 (T-201c 스텁)."""
    raise NotImplementedError(
        "T-201c: 상세 이미지 업로드 파이프라인이 필요합니다 "
        "(upload_images)."
    )


def _render_detail_html(payload, detail_images, seo_title):
    """상세 HTML 렌더 (T-201c 스텁).

    원본의 상세 HTML 렌더 함수는 함수명에 금칙어를 포함하므로 이식하지 않는다
    (작업지시: "원본 함수명에 금칙어가 포함되면 그 함수를 이식하지 말고 보고하라
    — 이름을 변형해 우회하지 말 것"). ``templates.build_korean_detail_html`` 이
    llm_hint 를 반환하므로 그것을 사용할 수 있으나, 최종 HTML 확정은 T-201c 범위다.
    """
    raise NotImplementedError(
        "T-201c: 상세 HTML 렌더 파이프라인이 필요합니다 "
        "(detail_html_render). 원본 함수는 금칙어 포함으로 이식 제외됨."
    )


__all__ = [
    "_reject_url_inputs",
    "_sanitize_product_key",
    "make_product_key",
    "resolve_product_key",
    "_prepared_dir",
    "_prepared_item_dir",
    "_prepared_payload_path",
    "write_prepared_payload",
    "read_prepared_payload",
    "load_prepared_payload",
    "iter_prepared_payload_paths",
    "_build_product_dict",
    "register_prepared_listing",
    "register_listing",
    "inject_prepared_qa",
]
