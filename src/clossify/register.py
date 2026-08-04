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

    규칙: "호출자가 주지 않으면 ``sha1(상품명 + 가격)[:12]`` 로 생성."

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
        raise ValueError("product_key 생성에 필요한 상품명이 비어 있습니다 (빈 키 방지).")
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
    images_block = payload.get("images") or {}
    if not isinstance(images_block, dict):
        images_block = {}
    listing_urls = [
        str(u).strip()
        for u in (images_block.get("listing_urls") or [])
        if isinstance(u, str) and u.strip()
    ]
    detail_urls = [
        str(u).strip()
        for u in (images_block.get("detail_urls") or [])
        if isinstance(u, str) and u.strip()
    ]

    # 이미지 0장 거부 (무음 통과 금지).
    if not listing_urls:
        raise ValueError(
            "prepared payload 에 리스팅 이미지(listing_urls)가 0장입니다. "
            "등록을 거부한다(무음 통과 금지). "
            f"product_key={product_key}"
        )

    # QA 게이트 — fail-closed (PENDING/FAIL 차단).
    allowed, reason = qa_agents.qa_gate(payload)
    if not allowed:
        return {"ok": False, "blocked": True, "reason": reason, "product_key": product_key}

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

    product_key = make_product_key(name, sale_price)

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
    # 컴플라이언스: context 기반 최소 검사만. api_payload 가 없으므로 일부 검사는
    # PENDING 으로 둘 수 있다(단, FAIL 로 만들지는 않는다 — 원본 정책).
    try:
        compliance_ctx = {
            "category_id": category_id,
            "notice": d.get("notice") or {},
        }
        compliance_result = qa_agents._normalize_agent_result(
            qa_agents._compliance_code_check(name, compliance_ctx), "compliance"
        )
    except Exception:
        compliance_result = qa_agents._qa_agent_result(
            "compliance",
            qa_agents.PENDING,
            [
                {
                    "rule": "컴플라이언스 검사 대기",
                    "severity": qa_agents.PENDING,
                    "detail": "컴플라이언스 검사 중 예외 — PENDING 등록.",
                }
            ],
            "컴플라이언스 PENDING",
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
    write_prepared_payload(payload)
    return payload


__all__ = [
    "_build_product_dict",
    "_prepared_dir",
    "_prepared_item_dir",
    "_prepared_payload_path",
    "_reject_url_inputs",
    "_sanitize_product_key",
    "_validate_review_submission",
    "inject_prepared_qa",
    "iter_prepared_payload_paths",
    "load_prepared_payload",
    "make_product_key",
    "prepare_listing",
    "read_prepared_payload",
    "register_listing",
    "register_prepared_listing",
    "resolve_product_key",
    "submit_reviews",
    "write_prepared_payload",
]
