# -*- coding: utf-8 -*-
"""검증된 네이버 커머스API 클라이언트 — 인증/이미지업로드/등록/조회/수정.
2026-06-23 풀루프 실증된 흐름을 함수로 정리.

경로 메모: 본 모듈은 src/clossify/ 에 위치하므로, 프로젝트 루트의
.local/config.json 을 가리키기 위해 __file__ 기준 상위 2단계를 사용한다.
"""
import copy, re, time, base64, json, os
import bcrypt, requests

BASE = "https://api.commerce.naver.com"
# src/clossify/naver_client.py -> ../../. = project root
_PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
_DEFAULT_CONFIG_PATH = os.path.join(_PROJECT_ROOT, ".local", "config.json")
SELLER_TAG_AUTOSTRIP_KEY = "sellerTagsAutoStrip"
MAX_RESTRICTED_SELLER_TAG_RETRIES = 2
KNOWN_RESTRICTED_SELLER_TAGS = {"인테리어", "화병", "도자기", "꽃병"}

# 네이버 커머스 API 상품명 최대 길이(정책). 초과 시 등록 거절.
MAX_PRODUCT_NAME_LEN = 50

# 네이버 커머스 API originAreaInfo.originAreaCode 표준 코드(예: "04"=중국).
# 잘못된 코드는 400 응답을 유발하므로 화이트리스트로 사전 차단(fail-closed).
_VALID_ORIGIN_AREA_CODES = frozenset({
    "01", "02", "03", "04", "05", "06", "07", "08",
    "09", "10", "11", "12", "13", "14", "15", "16",
})


def resolve_config_path() -> str:
    """설정 파일 경로를 결정한다 (단일 진실 공급원).

    우선순위:
      1. 환경변수 ``CLOSSIFY_CONFIG`` 가 비어있지 않은 경로를 가리키면 그것.
      2. 그 외는 프로젝트 루트의 ``.local/config.json``.

    반환값은 정규화된 경로 문자열. 파일 존재 여부는 검사하지 않는다
    (``check_config`` 같은 호출자가 부재 케이스를 다룬다).
    """
    env_path = os.environ.get("CLOSSIFY_CONFIG")
    if env_path and env_path.strip():
        return os.path.normpath(os.path.expandvars(os.path.expanduser(env_path.strip())))
    return _DEFAULT_CONFIG_PATH


def config_path() -> str:
    """``resolve_config_path`` 의 public 별칭 (외부 모듈 참조용)."""
    return resolve_config_path()


# 하위 호환: 모듈 수준 상수도 동일 경로로 노출.
_CFG_PATH = _DEFAULT_CONFIG_PATH


def load_config():
    """설정 JSON 을 로드한다. 경로는 ``resolve_config_path()`` 를 따른다."""
    with open(resolve_config_path(), encoding="utf-8-sig") as f:
        return json.load(f)


def _notice_config():
    try:
        c = load_config()
    except Exception:
        return {}
    for key in ("smartstore_notice_defaults", "notice_defaults", "product_notice_defaults"):
        section = c.get(key)
        if isinstance(section, dict):
            return section
    return {}


def _first_value(*values, default=""):
    for value in values:
        if value not in (None, ""):
            text = str(value).strip()
            if text:
                return text
    return default


def _int_value(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _model_name_default(p):
    num_iid = _first_value(
        p.get("num_iid"),
        p.get("numIid"),
        p.get("item_id"),
        p.get("itemId"),
        p.get("source_item_id"),
        default="",
    )
    return f"TB-{num_iid}" if num_iid else "상세페이지 참조"


def _seller_manufacturer_default(p, cfg_notice):
    return _first_value(
        p.get("seller_name_ko"),
        p.get("sellerNameKo"),
        p.get("seller_name"),
        p.get("sellerName"),
        p.get("shop_name_ko"),
        p.get("shopNameKo"),
        p.get("shop_name"),
        p.get("shopName"),
        p.get("nick"),
        p.get("nickName"),
        cfg_notice.get("manufacturer"),
        default="상세페이지 참조",
    )


def _resolve_origin_area_code(p, cfg_notice):
    """``originAreaInfo.originAreaCode`` 값을 화이트리스트로 검증(fail-closed).

    네이버 커머스 API 원산지 코드는 2자리 문자열(예: ``"04"``=중국).
    잘못된 코드로 페이로드를 보내면 400 응답이 발생하므로, 사전에
    ``_VALID_ORIGIN_AREA_CODES`` 화이트리스트로 검사한다.

    후보 순서: ``p.origin_code`` → ``cfg_notice.origin_area_code`` → ``"04"``(중국).
    후보가 없거나 화이트리스트에 없으면 ``"04"`` 로 폴백하지만,
    *사용자가 명시적으로* 지정한 값이 화이트리스트에 없다면 검증 에러로
    fail-closed 처리하기 위해 검증 결과를 함께 반환한다.

    Returns:
        (code, ok) — code 는 화이트리스트 통과한 최종 코드.
        ok 는 사용자 명시 값이 검증을 통과했는지.
    """
    raw = _first_value(p.get("origin_code"), cfg_notice.get("origin_area_code"), default="04")
    code = str(raw or "").strip()
    if code in _VALID_ORIGIN_AREA_CODES:
        return code, True
    # fail-closed fallback: 인식 불가 코드면 안전한 기본값(중국) 사용.
    return "04", False


def _notice_defaults(p):
    cfg_notice = _notice_config()
    product_name = _first_value(p.get("name"), p.get("title_ko"), default="상품명")
    as_tel = _first_value(
        p.get("as_tel"),
        p.get("seller_tel"),
        cfg_notice.get("as_tel"),
        cfg_notice.get("seller_tel"),
        cfg_notice.get("customerServicePhoneNumber"),
        default="판매자연락처",
    )
    manufacturer = _first_value(p.get("manufacturer"), default=_seller_manufacturer_default(p, cfg_notice))
    importer = _first_value(p.get("importer"), cfg_notice.get("importer"), default="해외구매대행")
    made_in = _first_value(p.get("made_in"), p.get("origin_content"), cfg_notice.get("origin_content"), default="중국")
    cert_text = _first_value(p.get("cert_detail"), cfg_notice.get("cert_detail"), default="해당없음 / KC면제")
    quality = _first_value(
        p.get("quality_assurance_standard"),
        p.get("qualityAssuranceStandard"),
        cfg_notice.get("quality_assurance_standard"),
        cfg_notice.get("qualityAssuranceStandard"),
        default="관련 법 및 소비자분쟁해결기준에 따름",
    )
    return_cost_reason = _first_value(
        p.get("return_cost_reason"),
        p.get("returnCostReason"),
        cfg_notice.get("return_cost_reason"),
        cfg_notice.get("returnCostReason"),
        default="단순 변심에 의한 반품/교환 시 왕복 배송비는 구매자 부담, 상품 하자 시 판매자 부담",
    )
    no_refund_reason = _first_value(
        p.get("no_refund_reason"),
        p.get("noRefundReason"),
        cfg_notice.get("no_refund_reason"),
        cfg_notice.get("noRefundReason"),
        default="주문제작/해외구매대행 등 전자상거래법 제17조 청약철회 제한 사유에 해당하는 경우 청약철회가 제한될 수 있습니다",
    )
    compensation_procedure = _first_value(
        p.get("compensation_procedure"),
        p.get("compensationProcedure"),
        cfg_notice.get("compensation_procedure"),
        cfg_notice.get("compensationProcedure"),
        default="소비자분쟁해결기준 및 관계 법령에 따라 보상",
    )
    trouble_shooting_contents = _first_value(
        p.get("trouble_shooting_contents"),
        p.get("troubleShootingContents"),
        cfg_notice.get("trouble_shooting_contents"),
        cfg_notice.get("troubleShootingContents"),
        default="소비자 상담은 고객센터 전화로 문의, 소비자분쟁해결기준에 따라 처리",
    )
    return {
        "item_name": product_name[:50],
        "model_name": _first_value(p.get("modelName"), p.get("model_name"), cfg_notice.get("model_name"), default=_model_name_default(p)),
        "cert_detail": cert_text,
        "made_in": made_in,
        "manufacturer": manufacturer,
        "importer": importer,
        "manufacturer_importer": _first_value(
            p.get("manufacturer_importer"),
            cfg_notice.get("manufacturer_importer"),
            default=f"{manufacturer} / {importer}",
        ),
        "manufacture_date": _first_value(
            p.get("manufacture_date"),
            p.get("manufacturedDate"),
            cfg_notice.get("manufacture_date"),
            default="상세페이지 참조",
        ),
        "quality_assurance_standard": quality,
        "return_cost_reason": return_cost_reason,
        "no_refund_reason": no_refund_reason,
        "compensation_procedure": compensation_procedure,
        "trouble_shooting_contents": trouble_shooting_contents,
        "as_tel": as_tel,
        "as_guide": _first_value(
            p.get("as_guide"),
            cfg_notice.get("as_guide"),
            default="해외구매대행 상품입니다. 판매자에게 문의해 주세요.",
        ),
        "origin_area_code": _resolve_origin_area_code(p, cfg_notice)[0],
        "origin_content": made_in,
        "return_delivery_fee": _int_value(
            p.get("return_delivery_fee", cfg_notice.get("return_delivery_fee")),
            3000,
        ),
        "exchange_delivery_fee": _int_value(
            p.get("exchange_delivery_fee", cfg_notice.get("exchange_delivery_fee")),
            6000,
        ),
    }


def _is_furniture_notice(p):
    notice_type = str(p.get("notice_type") or p.get("productInfoProvidedNoticeType") or "").strip().upper()
    if notice_type == "FURNITURE":
        return True
    user_notice = p.get("notice")
    if isinstance(user_notice, dict):
        notice_type = str(user_notice.get("productInfoProvidedNoticeType") or "").strip().upper()
        if notice_type == "FURNITURE":
            return True
    category_text = " ".join(str(p.get(k) or "") for k in ("category_name", "category_path", "categoryPath"))
    return "가구" in category_text


def _base_etc_notice(defaults):
    cert = defaults["cert_detail"]
    return {
        "itemName": defaults["item_name"],
        "modelName": defaults["model_name"],
        "certDetail": cert,
        "certificationDetails": cert,
        "madeIn": defaults["made_in"],
        "countryOfOrigin": defaults["made_in"],
        "manufacturer": defaults["manufacturer"],
        "importer": defaults["importer"],
        "manufacturerImporter": defaults["manufacturer_importer"],
        "manufactureDate": defaults["manufacture_date"],
        "qualityAssuranceStandard": defaults["quality_assurance_standard"],
        "returnCostReason": defaults["return_cost_reason"],
        "noRefundReason": defaults["no_refund_reason"],
        "compensationProcedure": defaults["compensation_procedure"],
        "troubleShootingContents": defaults["trouble_shooting_contents"],
        "afterServiceDirector": f"{defaults['manufacturer']} {defaults['as_tel']}",
    }


def _base_furniture_notice(p, defaults):
    notice = _base_etc_notice(defaults)
    notice.update({
        "material": _first_value(p.get("material"), p.get("fabric"), p.get("소재"), default="상세참조"),
        "size": _first_value(p.get("size"), p.get("dimensions"), default="상세참조"),
        "components": _first_value(p.get("components"), p.get("composition"), default="상세참조"),
        "safetyStandard": _first_value(p.get("safety_standard"), p.get("safetyStandard"), default="해당없음 / 상세참조"),
    })
    return notice


def _enforce_notice_as_contact_exclusive(notice_body, user_fields=None):
    user_fields = set(user_fields or ())

    def has_text(value):
        return value is not None and bool(str(value).strip())

    user_after = "afterServiceDirector" in user_fields
    user_customer = "customerServicePhoneNumber" in user_fields
    body_after = has_text(notice_body.get("afterServiceDirector"))
    body_customer = has_text(notice_body.get("customerServicePhoneNumber"))

    if user_customer and not user_after:
        notice_body.pop("afterServiceDirector", None)
    elif body_after and body_customer:
        notice_body.pop("customerServicePhoneNumber", None)


def _merge_notice(default_notice, user_notice):
    if not isinstance(user_notice, dict):
        return default_notice
    default_type = str(default_notice.get("productInfoProvidedNoticeType") or "ETC").strip().upper()
    user_type = str(user_notice.get("productInfoProvidedNoticeType") or "").strip().upper()
    notice_type = default_type if default_type != "ETC" else (user_type or default_type)
    key = "furniture" if notice_type == "FURNITURE" else "etc"
    merged = {
        "productInfoProvidedNoticeType": notice_type,
        key: dict(default_notice.get(key) or {}),
    }
    user_body = user_notice.get(key) if isinstance(user_notice.get(key), dict) else {}
    user_fields = set()
    for field, value in user_body.items():
        text = str(value).strip() if value is not None else ""
        if text and text not in {"상세페이지 참조", "상세페이지참조"}:
            merged[key][field] = value
            user_fields.add(field)
    _enforce_notice_as_contact_exclusive(merged[key], user_fields)
    return merged


def _product_info_notice(p, defaults):
    if _is_furniture_notice(p):
        base = {"productInfoProvidedNoticeType": "FURNITURE", "furniture": _base_furniture_notice(p, defaults)}
    else:
        base = {"productInfoProvidedNoticeType": "ETC", "etc": _base_etc_notice(defaults)}
    return _merge_notice(base, p.get("notice"))


def get_token():
    """OAuth2 client_credentials + bcrypt 서명. 토큰 문자열 반환(시크릿 노출 안 함)."""
    c = load_config()["naver"]
    cid, csec = c["client_id"], c["client_secret"]
    ts = str(int(time.time() * 1000))
    sign = base64.b64encode(bcrypt.hashpw(f"{cid}_{ts}".encode(), csec.encode())).decode()
    r = requests.post(BASE + "/external/v1/oauth2/token", timeout=20, data={
        "client_id": cid, "timestamp": ts, "client_secret_sign": sign,
        "grant_type": "client_credentials", "type": c.get("type", "SELF")})
    r.raise_for_status()
    return r.json()["access_token"]


def _h(tk, json_ct=True):
    h = {"Authorization": f"Bearer {tk}"}
    if json_ct:
        h["Content-Type"] = "application/json"
    return h


def _guess_image_mime(path):
    """파일 확장자에서 MIME 타입 추정. (mimetypes 모듈이 종종 누락하는 케이스 보강)"""
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    return {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
    }.get(ext, "application/octet-stream")


def upload_images(paths, tk=None):
    """로컬 이미지들을 네이버 이미지서버에 업로드 → secure URL 리스트.

    파일 핸들은 ``with`` 컨텍스트로 닫힘(리소스 누수 방지).
    MIME 타입은 확장자 기반으로 추정한다.
    """
    tk = tk or get_token()
    opened_files = []
    files = []
    try:
        for p in paths:
            fh = open(p, "rb")
            opened_files.append(fh)
            files.append(("imageFiles", (os.path.basename(p), fh, _guess_image_mime(p))))
        r = requests.post(BASE + "/external/v1/product-images/upload", headers=_h(tk, False), files=files, timeout=120)
        r.raise_for_status()
        return [im["url"] for im in r.json().get("images", [])]
    finally:
        for fh in opened_files:
            try:
                fh.close()
            except Exception:
                pass


def _json_or_text_response(response):
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()
    return response.text


def _post_product_payload(payload, tk):
    r = requests.post(BASE + "/external/v2/products", headers=_h(tk),
                      data=json.dumps(payload).encode("utf-8"), timeout=60)
    return r.status_code, _json_or_text_response(r)


def _normalize_seller_tag_text(value):
    text = str(value or "").strip().lstrip("#")
    return re.sub(r"\s+", "", text).lower()


def _seller_tags_list(payload):
    if not isinstance(payload, dict):
        return None
    origin = payload.get("originProduct")
    if not isinstance(origin, dict):
        return None
    detail = origin.get("detailAttribute")
    if not isinstance(detail, dict):
        return None
    seo = detail.get("seoInfo")
    if not isinstance(seo, dict):
        return None
    tags = seo.get("sellerTags")
    return tags if isinstance(tags, list) else None


def _seller_tag_text(tag):
    if isinstance(tag, dict):
        return str(tag.get("text") or "").strip()
    return str(tag or "").strip()


def _strip_seller_tags(payload, restricted_terms):
    tags = _seller_tags_list(payload)
    if not tags:
        return []
    restricted = {_normalize_seller_tag_text(term) for term in restricted_terms if str(term or "").strip()}
    if not restricted:
        return []
    kept, removed = [], []
    seen_removed = set()
    for tag in tags:
        text = _seller_tag_text(tag)
        if _normalize_seller_tag_text(text) in restricted:
            key = _normalize_seller_tag_text(text)
            if key not in seen_removed:
                removed.append(text)
                seen_removed.add(key)
            continue
        kept.append(tag)
    if len(kept) != len(tags):
        tags[:] = kept
    return removed


def _clear_seller_tags(payload):
    tags = _seller_tags_list(payload)
    if not tags:
        return []
    removed = []
    seen_removed = set()
    for tag in tags:
        text = _seller_tag_text(tag)
        key = _normalize_seller_tag_text(text)
        if text and key not in seen_removed:
            removed.append(text)
            seen_removed.add(key)
    tags[:] = []
    return removed


def _collect_invalid_inputs(body):
    found = []

    def visit(value):
        if isinstance(value, dict):
            invalids = value.get("invalidInputs")
            if isinstance(invalids, list):
                found.extend([x for x in invalids if isinstance(x, dict)])
            for child in value.values():
                if isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                if isinstance(child, (dict, list)):
                    visit(child)

    visit(body)
    return found


def _restricted_seller_tag_inputs(body):
    matches = []
    for item in _collect_invalid_inputs(body):
        typ = str(item.get("type") or "")
        if typ == "Restricted.sellerTags":
            matches.append(item)
    return matches


def _is_restricted_seller_tags_response(status_code, body):
    if not isinstance(body, dict):
        return False
    code = str(body.get("code") or body.get("status") or "").upper()
    if status_code != 400 and code != "BAD_REQUEST":
        return False
    return bool(_restricted_seller_tag_inputs(body))


def _parse_restricted_seller_tag_terms_from_message(message):
    terms = []
    seen = set()
    text = str(message or "")
    for group in re.findall(r"[\(\uff08]([^()\uff08\uff09]+)[\)\uff09]", text):
        for raw in re.split(r"[,，、/|]", group):
            term = raw.strip().strip("'\"`[]{}")
            if not term:
                continue
            key = _normalize_seller_tag_text(term)
            if not key or key in seen:
                continue
            terms.append(term)
            seen.add(key)
    return terms


def _restricted_seller_tag_terms(body):
    terms = []
    seen = set()
    sources = []
    if isinstance(body, dict):
        sources.append(body.get("message"))
    for item in _restricted_seller_tag_inputs(body):
        sources.extend([item.get("message"), item.get("reason"), item.get("invalidReason")])
    for source in sources:
        for term in _parse_restricted_seller_tag_terms_from_message(source):
            key = _normalize_seller_tag_text(term)
            if key and key not in seen:
                terms.append(term)
                seen.add(key)
    return terms


def _append_unique(values, new_values):
    seen = {_normalize_seller_tag_text(v) for v in values}
    for value in new_values:
        key = _normalize_seller_tag_text(value)
        if key and key not in seen:
            values.append(value)
            seen.add(key)


def _seller_tag_autostrip_active(meta):
    return bool(meta.get("attempts") or meta.get("removed") or meta.get("restricted_terms"))


def _attach_seller_tag_autostrip_meta(body, meta):
    if not _seller_tag_autostrip_active(meta):
        return body
    clean_meta = {
        "removed": meta.get("removed", []),
        "restricted_terms": meta.get("restricted_terms", []),
        "attempts": meta.get("attempts", []),
    }
    if meta.get("prefilter_removed"):
        clean_meta["prefilter_removed"] = meta.get("prefilter_removed", [])
    if meta.get("cleared_all"):
        clean_meta["cleared_all"] = True
    if isinstance(body, dict):
        body[SELLER_TAG_AUTOSTRIP_KEY] = clean_meta
        return body
    return {"body": body, SELLER_TAG_AUTOSTRIP_KEY: clean_meta}


def register_product(payload, tk=None):
    """POST /external/v2/products. (origin/channel No 반환)"""
    working_payload = copy.deepcopy(payload)
    meta = {"removed": [], "restricted_terms": [], "attempts": []}
    prefilter_removed = _strip_seller_tags(working_payload, KNOWN_RESTRICTED_SELLER_TAGS)
    if prefilter_removed:
        meta["prefilter_removed"] = prefilter_removed
        _append_unique(meta["removed"], prefilter_removed)
        _append_unique(meta["restricted_terms"], prefilter_removed)

    if os.environ.get("COMMERCE_DRY_RUN") == "1":
        payload_path = os.path.join(os.path.dirname(__file__), "..", "..", ".local", "dry_run_payload.json")
        os.makedirs(os.path.dirname(payload_path), exist_ok=True)
        with open(payload_path, "w", encoding="utf-8") as f:
            json.dump(working_payload, f, ensure_ascii=False, indent=2)
        origin = working_payload.get("originProduct") if isinstance(working_payload, dict) else {}
        return {
            "ok": True,
            "dry_run": True,
            "originProductNo": None,
            "payload_path": ".local/dry_run_payload.json",
            "statusType": origin.get("statusType") if isinstance(origin, dict) else None,
        }

    tk = tk or get_token()
    last_sc, last_body = None, None
    for attempt_no in range(MAX_RESTRICTED_SELLER_TAG_RETRIES + 1):
        sc, body = _post_product_payload(working_payload, tk)
        last_sc, last_body = sc, body
        if not _is_restricted_seller_tags_response(sc, body):
            return sc, _attach_seller_tag_autostrip_meta(body, meta)

        terms = _restricted_seller_tag_terms(body)
        removed = _strip_seller_tags(working_payload, terms)
        _append_unique(meta["restricted_terms"], terms)
        _append_unique(meta["removed"], removed)
        meta["attempts"].append({
            "attempt": attempt_no + 1,
            "http": sc,
            "terms": terms,
            "removed": removed,
            "action": "strip_and_retry" if attempt_no < MAX_RESTRICTED_SELLER_TAG_RETRIES else "clear_all_next",
        })
        if attempt_no >= MAX_RESTRICTED_SELLER_TAG_RETRIES:
            break
        if not terms and not removed:
            break

    cleared = _clear_seller_tags(working_payload)
    if cleared:
        meta["cleared_all"] = True
        _append_unique(meta["removed"], cleared)
        meta["attempts"].append({"attempt": len(meta["attempts"]) + 1, "removed": cleared, "action": "clear_all"})
        sc, body = _post_product_payload(working_payload, tk)
        return sc, _attach_seller_tag_autostrip_meta(body, meta)
    return last_sc, _attach_seller_tag_autostrip_meta(last_body, meta)


def seller_tag_autostrip_meta(body):
    if isinstance(body, dict) and isinstance(body.get(SELLER_TAG_AUTOSTRIP_KEY), dict):
        return body.get(SELLER_TAG_AUTOSTRIP_KEY)
    return None


def update_product(channel_no, payload, tk=None):
    """PUT /external/v2/products/channel-products/{channelNo}."""
    tk = tk or get_token()
    r = requests.put(BASE + f"/external/v2/products/channel-products/{channel_no}", headers=_h(tk),
                     data=json.dumps(payload).encode("utf-8"), timeout=60)
    return r.status_code, _json_or_text_response(r)


def get_product(origin_no, tk=None):
    tk = tk or get_token()
    r = requests.get(BASE + f"/external/v2/products/origin-products/{origin_no}", headers=_h(tk, False), timeout=20)
    return r.status_code, (r.json() if r.status_code == 200 else r.text)


def _option_stock(option):
    try:
        return int(option.get("stockQuantity", option.get("stock", 99)))
    except (TypeError, ValueError):
        return 99


def _option_price(option):
    try:
        return int(option.get("price", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _option_group_list(p):
    groups = p.get("option_groups") or p.get("optionGroupNames") or p.get("optionCombinationGroupNames")
    if isinstance(groups, dict):
        return [groups.get(f"optionGroupName{i}") for i in range(1, 4) if groups.get(f"optionGroupName{i}")]
    if isinstance(groups, str):
        return [groups]
    if isinstance(groups, (list, tuple)):
        return [str(x) for x in groups if x]
    return []


def _option_width(opts):
    width = 0
    for option in opts:
        if not isinstance(option, dict):
            continue
        names = option.get("names")
        if isinstance(names, (list, tuple)):
            width = max(width, min(3, len([x for x in names if x])))
        for i in range(1, 4):
            if option.get(f"optionName{i}"):
                width = max(width, i)
        if option.get("name"):
            width = max(width, 1)
    return max(1, min(3, width))


def _build_option_info(p, opts):
    if not opts:
        return {}
    width = _option_width(opts)
    groups = _option_group_list(p)
    if len(groups) < width:
        if width == 1:
            groups = groups or [p.get("option_group", "사이즈")]
        while len(groups) < width:
            groups.append(f"옵션{len(groups) + 1}")
    group_names = {f"optionGroupName{i}": groups[i - 1] for i in range(1, width + 1)}

    combinations = []
    seen = set()
    for option in opts:
        if not isinstance(option, dict):
            continue
        names = option.get("names") if isinstance(option.get("names"), (list, tuple)) else None
        combo, key = {}, []
        for i in range(1, width + 1):
            value = option.get(f"optionName{i}")
            if value is None and names and len(names) >= i:
                value = names[i - 1]
            if value is None and i == 1:
                value = option.get("name")
            value = str(value or "").strip()
            if not value:
                combo = None
                break
            combo[f"optionName{i}"] = value[:25]
            key.append(value)
        if not combo:
            continue
        tkey = tuple(key)
        if tkey in seen:
            continue
        seen.add(tkey)
        combo["stockQuantity"] = _option_stock(option)
        combo["price"] = _option_price(option)
        combinations.append(combo)
    if not combinations:
        return {}
    return {
        "optionCombinationSortType": "CREATE",
        "optionCombinationGroupNames": group_names,
        "optionCombinations": combinations,
    }


def build_payload(p, detail_html, images, status="SALE"):
    """상품 dict(p) + 상세HTML + 이미지URL들 → 등록 payload.
    p keys: name, categoryId, salePrice, options[{name,stock}], tags[], notice{...}, as_tel, as_guide, origin_code, display"""
    if status not in {"SALE", "SUSPENSION"}:
        raise ValueError("status must be one of {'SALE', 'SUSPENSION'}")
    opts = p.get("options", [])
    option_info = _build_option_info(p, opts)
    defaults = _notice_defaults(p)
    notice = _product_info_notice(p, defaults)
    display_default = "OFF" if status == "SUSPENSION" else "ON"
    return {"originProduct": {
        "statusType": status, "saleType": "NEW", "leafCategoryId": p["categoryId"],
        "name": p["name"], "detailContent": detail_html,
        "images": {"representativeImage": {"url": images[0]},
                   "optionalImages": [{"url": u} for u in images[1:]]},
        "salePrice": int(p["salePrice"]), "stockQuantity": sum(_option_stock(o) for o in opts) if opts else int(p.get("stock", 1)),
        "deliveryInfo": {"deliveryType": "DELIVERY", "deliveryAttributeType": "NORMAL",
            "deliveryCompany": p.get("courier", "CJGLS"), "deliveryBundleGroupUsable": False,
            "deliveryFee": {"deliveryFeeType": "PAID", "baseFee": int(p.get("delivery_fee", 3000)), "deliveryFeePayType": "PREPAID"},
            "claimDeliveryInfo": {"returnDeliveryFee": defaults["return_delivery_fee"], "exchangeDeliveryFee": defaults["exchange_delivery_fee"]}},
        "detailAttribute": {
            "afterServiceInfo": {"afterServiceTelephoneNumber": defaults["as_tel"],
                                 "afterServiceGuideContent": defaults["as_guide"]},
            "originAreaInfo": {"originAreaCode": defaults["origin_area_code"],
                               "content": defaults["origin_content"],
                               "importer": defaults["importer"]},
            "certificationTargetExcludeContent": {
                "kcCertifiedProductExclusionYn": "KC_EXEMPTION_OBJECT",
                "kcExemptionType": "OVERSEAS"},
            "minorPurchasable": True, "taxType": "TAX",
            "seoInfo": {"sellerTags": [{"text": t} for t in p.get("tags", [])]},
            "productInfoProvidedNotice": notice,
            "optionInfo": option_info}},
        "smartstoreChannelProduct": {"naverShoppingRegistration": True,
            "channelProductDisplayStatusType": p.get("display", display_default)}}
