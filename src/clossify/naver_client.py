# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""검증된 네이버 커머스API 클라이언트 — 인증/이미지업로드/등록/조회/수정.
2026-06-23 풀루프 실증된 흐름을 함수로 정리.

경로 메모 (install-paths 재배치): 본 모듈은 패키지 데이터(``data/*.json``)를
``importlib.resources`` 기반의 ``common.package_data_path`` 로 읽고, 사용자
설정(``config.json``) 은 ``CLOSSIFY_CONFIG`` 환경변수 또는 ``<cwd>/.local/``
에서 찾는다. ``__file__`` 기반 프로젝트 루트 추정은 사용하지 않는다.
"""

import base64
import collections
import copy
import io
import json
import os
import re
import time

import bcrypt
import requests

from . import common
from .text_props import CATEGORY_PATH_NOTICE_HINTS

BASE = "https://api.commerce.naver.com"
# 사용자 설정 파일 기본경로: <cwd>/.local/config.json.
# CLOSSIFY_CONFIG 환경변수로 절대경로를 지정할 수 있다 (우선).
_DEFAULT_CONFIG_PATH = os.path.join(str(common.STATE_DIR), "config.json")
SELLER_TAG_AUTOSTRIP_KEY = "sellerTagsAutoStrip"
MAX_RESTRICTED_SELLER_TAG_RETRIES = 2
KNOWN_RESTRICTED_SELLER_TAGS = {"인테리어", "화병", "도자기", "꽃병"}

# 네이버 커머스 API 상품명 최대 길이(정책). 초과 시 등록 거절.
MAX_PRODUCT_NAME_LEN = 50


# 네이버 커머스 API originAreaInfo.originAreaCode 표준 코드 화이트리스트.
# 특정 해외국 코드를 기본값으로 갖지 않는다. 원산지는 판매자가 config 에
# 명시한 값만 허용하며, 화이트리스트 벗어남/누락 시 ValueError 로 등록 거부(fail-closed).
#
# 과거 이 목록은 하드코딩된 ``{"01".."16"}`` 이었다 — 실측(D64) 으로 535개
# (최상위 6 + 시·도 27 + 시·군·구 502) 코드가 확인되었고, 하드코딩은 ``00 국산``
# 이 빠져 있고 ``10``-``16`` 같은 존재하지 않는 코드가 섞여 있었다.
# 이제 ``data/product_origin_areas.json`` 의 단일 진실 공급원에서 읽는다.
# 파일 부재/손상 시 빈 frozenset 이 되며, 이 경우 모든 코드가 거부된다
# (fail-closed — 파일이 없으면 코드를 임의로 통과시키지 않는다).
def _load_origin_area_codes() -> frozenset:
    try:
        path = common.package_data_path("product_origin_areas.json")
        if not path.exists():
            return frozenset()
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return frozenset()
    codes: set[str] = set()
    for key in ("top_level", "state_level", "city_level"):
        bucket = doc.get(key) if isinstance(doc, dict) else None
        if isinstance(bucket, list):
            for entry in bucket:
                if isinstance(entry, dict):
                    code = str(entry.get("code") or "").strip()
                    if code:
                        codes.add(code)
    return frozenset(codes)


_VALID_ORIGIN_AREA_CODES: frozenset = _load_origin_area_codes()


def resolve_config_path() -> str:
    """설정 파일 경로를 결정한다 (단일 진실 공급원).

    우선순위:
      1. 환경변수 ``CLOSSIFY_CONFIG`` 가 비어있지 않은 경로를 가리키면 그것.
      2. 그 외는 ``<cwd>/.local/config.json`` (``CLOSSIFY_STATE_DIR`` 로
         ``<cwd>/.local`` 부분을 재정의 가능).

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


def _require_original_images(images):
    """유효 이미지 1장 이상을 강제. 실패 시 ValueError.

    이것은 진입 게이트(entry gate)다. "이미지가 존재하는가"만 판정하며,
    이미지의 진위·출처·내용은 판별하지 않는다(그런 코드는 만들지 않는다).

    무효로 취급(전부 거부 사유):
      - ``images`` 가 ``None``
      - 리스트가 아님
      - 빈 리스트
      - 항목이 ``str`` 이 아님
      - ``strip()`` 후 빈 문자열(공백·탭·개행 전용 포함)

    유효 항목과 무효 항목이 섞인 경우 조용히 걸러내지 않고 거부한다.
    (순서·대표 이미지 규약이 깨지는 것을 막기 위함)

    Raises:
        ValueError: 유효 이미지가 0개이거나 무효 항목이 하나라도 섞인 경우.
    """
    if images is None:
        raise ValueError(
            "원본 이미지가 최소 1장 필요합니다. 실재하는 상품의 사진 없이는 "
            "등록을 진행하지 않습니다. (유효 0장 / 입력 None)"
        )
    if not isinstance(images, list):
        raise ValueError(
            "원본 이미지가 최소 1장 필요합니다. 실재하는 상품의 사진 없이는 "
            "등록을 진행하지 않습니다. "
            f"(유효 0장 / 입력 타입 {type(images).__name__} — 리스트여야 함)"
        )
    total = len(images)
    valid = 0
    invalid_count = 0
    for item in images:
        if isinstance(item, str) and item.strip():
            valid += 1
        else:
            invalid_count += 1
    if invalid_count > 0 or valid == 0:
        raise ValueError(
            "원본 이미지가 최소 1장 필요합니다. 실재하는 상품의 사진 없이는 "
            f"등록을 진행하지 않습니다. (유효 {valid}장 / 입력 {total}개)"
        )


def _kc_config():
    """KC 인증정보 설정 블록을 config 에서 읽는다.

    원본(해외 소싱 도구)은 ``kcCertifiedProductExclusionYn="KC_EXEMPTION_OBJECT"``
    와 ``kcExemptionType="OVERSEAS"`` 를 전 상품에 박았다. 후자는 "해외구매대행이라
    KC 면제" 라는 규제 신고로, 국내 직접판매에는 성립하지 않아 허위 신고가 된다.

    KC 값은 config 에 명시된 경우에만 payload 에 싣는다. config 에 없으면
    KC 필드를 아예 넣지 않는다(네이버가 요구하면 API 가 에러로 알려준다 —
    우리가 임의 값을 지어내 신고하는 것보다 안전). 단, 호출자가 알 수 있도록
    반환값 메타에 경고를 포함한다.

    Returns:
        (kc_block, warning) — kc_block 은 payload 에 넣을 dict(빈 dict 이면 필드
        생략). warning 은 KC 설정 부재 시 경고 문자열, 있으면 빈 문자열.
    """
    try:
        c = load_config()
    except Exception:
        return {}, (
            "config 에 KC 인증정보(kc_declaration) 설정이 없습니다 — payload 에 "
            "KC 필드를 포함하지 않습니다. 네이버 커머스 API 가 요구하면 에러로 "
            "알려줍니다."
        )
    kc = c.get("kc_declaration")
    if not isinstance(kc, dict):
        return {}, (
            "config 에 KC 인증정보(kc_declaration) 설정이 없습니다 — payload 에 "
            "KC 필드를 포함하지 않습니다. 네이버 커머스 API 가 요구하면 에러로 "
            "알려줍니다."
        )
    block = {}
    exclusion = _first_value(kc.get("kcCertifiedProductExclusionYn"), default="")
    exemption = _first_value(kc.get("kcExemptionType"), default="")
    if exclusion:
        block["kcCertifiedProductExclusionYn"] = exclusion
    if exemption:
        block["kcExemptionType"] = exemption
    # KC 부분 블록 금지. config.example.json 의 설명("둘 중 하나가 비면
    # 전체 생략")과 실동작을 일치시킨다. 두 필수 키가 모두 갖춰졌을 때만 블록을
    # 싣고, 하나라도 없으면 블록 전체를 생략하며 경고 메타를 남긴다.
    if not (exclusion and exemption):
        return {}, (
            "config 의 kc_declaration 이 완전하지 않습니다 — "
            "kcCertifiedProductExclusionYn 와 kcExemptionType 값이 모두 필요합니다. "
            "어느 하나라도 비면 KC 블록 전체를 payload 에서 생략합니다 "
            "(부분 블록 생성 금지). 네이버 커머스 API 가 요구하면 에러로 알려줍니다."
        )
    return block, ""


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
    """모델명 기본값.

    외부 마켓 ID 필드(``num_iid``/``item_id`` 계열) 에서 특정 접두사를
    만들던 경로를 제거했다. 해당 ID 는 이 제품(셀러 본인 상품 국내 직접판매)에
    존재하지 않는다. 모델명은 config 또는 상품 입력에서만 받는다 — 입력이 없으면
    빈 문자열을 반환하고, 호출자는 필드를 생략한다.
    """
    return ""


def _seller_manufacturer_default(p, cfg_notice):
    """제조자 기본값 후보.

    판매자가 실제 신고하는 규제값이므로 코드가 임의 문구를 지어내지 않는다.
    config/상품 입력 어디에도 값이 없으면 빈 문자열을 반환한다. 호출자는
    빈 문자열을 "누락" 으로 다루고, 컴플라이언스 검사가 해당 필드를
    필수 항목 누락으로 FAIL 지적한다 (사용자에게 요구하는 것이 정답).
    """
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
        default="",
    )


def _resolve_origin_area_code(p, cfg_notice):
    """``originAreaInfo.originAreaCode`` 값을 화이트리스트로 검증(fail-closed).

    원본(해외 소싱 도구)의 ``"04"`` (특정 해외국) 기본값/폴백을 제거했다.
    원산지는 판매자가 실제 신고하는 값이어야 하며, 우리가 임의로 지정하면 안 된다.

    후보 순서: ``p.origin_code`` → ``cfg_notice.origin_area_code``.
    둘 중 하나라도 비어있지 않은 값을 제공해야 한다. 값이 없거나(누락/빈 문자열)
    화이트리스트에 없으면 ``ValueError`` 로 등록을 거부한다(fail-closed).
    조용한 기본값/폴백 금지.

    Raises:
        ValueError: 후보 값이 없거나 화이트리스트 벗어남.
    """
    raw = _first_value(p.get("origin_code"), cfg_notice.get("origin_area_code"), default="")
    code = str(raw or "").strip()
    if not code:
        raise ValueError(
            "config 에 원산지 설정이 필요합니다: smartstore_notice_defaults.origin_area_code"
        )
    if not _VALID_ORIGIN_AREA_CODES:
        # 데이터 파일(product_origin_areas.json) 부재/손상 — 모든 코드가 거부된다.
        # fail-closed: 파일이 없으면 코드를 임의로 통과시키지 않는다.
        raise ValueError(
            "원산지 코드 화이트리스트(data/product_origin_areas.json)를 불러올 수 없습니다. "
            "패키지 데이터 파일이 손상되었거나 누락되었습니다. "
            "scripts/fetch_origin_and_notice_types.py 로 재생성하거나 패키지를 재설치하세요."
        )
    if code not in _VALID_ORIGIN_AREA_CODES:
        raise ValueError(
            f"원산지 코드가 네이버 커머스 API 화이트리스트에 없습니다: {code!r} "
            f"(data/product_origin_areas.json 의 535개 코드 중 하나여야 합니다)"
        )
    return code


def _resolve_delivery_company(p, cfg_notice):
    """``deliveryInfo.deliveryCompany`` 값을 결정한다 (임의값 금지, 빈 값 허용).

    과거 코드는 ``"CJGLS"`` 를 기본값/폴백으로 박았다. 택배사 코드는 **판매자가
    실제 계약한 택배사** 를 신고하는 규제값이며, 우리가 임의로 정하면 안 된다.
    실측(D64) 확인: 이 스토어는 한진택배(HKSTRANS) 를 쓰고 있어 CJGLS 기본값은
    틀린 신고였다.

    후보 순서: ``p.courier`` → ``cfg_notice.delivery_company``.
    둘 다 없으면 **빈 문자열을 반환한다** (``as_tel``/``manufacturer``/``importer``
    와 동일한 패턴). 본 함수는 페이로드 **구성 단계** 에서 호출되므로, 여기서
    예외를 던지면 ``build_payload`` 순수 구성 함수가 깨진다. fail-closed 검증은
    **등록 경계**(``register_product``) 에서 별도로 수행한다 — 빈 택배사 코드가
    네이버 API 로 나가는 것을 거부한다.

    본 함수는 택배사 코드를 **검증하지 않는다.** 네이버 커머스 API 의 택배사
    enum 은 스토어마다 다를 수 있고(판매자가 계약한 택배사만 노출) 실시간 조회
    엔드포인트(``GET /external/v2/product-delivery-info/return-delivery-companies``)
    로 알 수 있다. 정적 데이터 파일로 굳히지 않는다(§4 계약).
    """
    raw = _first_value(
        p.get("courier"),
        p.get("delivery_company"),
        p.get("deliveryCompany"),
        cfg_notice.get("delivery_company"),
        cfg_notice.get("deliveryCompany"),
        default="",
    )
    return str(raw or "").strip()


def _notice_defaults(p):
    cfg_notice = _notice_config()
    product_name = _first_value(p.get("name"), p.get("title_ko"), default="상품명")
    # AS 연락처는 규제 신고값. config/상품 입력에 없으면 빈 문자열.
    # 컴플라이언스 검사가 afterServiceTelephoneNumber 누락을 FAIL 로 차단한다.
    as_tel = _first_value(
        p.get("as_tel"),
        p.get("seller_tel"),
        cfg_notice.get("as_tel"),
        cfg_notice.get("seller_tel"),
        cfg_notice.get("customerServicePhoneNumber"),
        default="",
    )
    manufacturer = _first_value(
        p.get("manufacturer"), default=_seller_manufacturer_default(p, cfg_notice)
    )
    # "해외구매대행" 기본값 제거. 수입자는 판매자가 config/입력으로 제공.
    # 값이 없으면 빈 문자열 — originAreaInfo.importer 필드가 비게 되고,
    # 네이버가 요구하면 API 가 에러로 알려준다(우리가 임의 값을 지어내지 않음).
    importer = _first_value(p.get("importer"), cfg_notice.get("importer"), default="")
    # "중국" (해외 국가명) 기본값 제거 + fail-closed.
    # 원산지 표시 문자열도 코드와 마찬가지로 config 또는 상품 입력에서만 받는다.
    made_in = _first_value(
        p.get("made_in"), p.get("origin_content"), cfg_notice.get("origin_content"), default=""
    )
    if not made_in:
        raise ValueError(
            "config 에 원산지 설정이 필요합니다: smartstore_notice_defaults.origin_content"
        )
    # "해당없음 / KC면제" 기본값 제거 — KC 면제 여부는 규제 신고이므로
    # 임의값을 지어내면 안 됨. 값이 없으면 빈 문자열.
    cert_text = _first_value(p.get("cert_detail"), cfg_notice.get("cert_detail"), default="")
    # 품질보증기준/반품비/환불불가/보상절차/고장대처 는 소비자 고시값.
    # 코드가 만든 기본 문구를 넣지 않는다. 값이 없으면 빈 문자열이며
    # 컴플라이언스 검사가 필수 항목 누락을 지적한다.
    quality = _first_value(
        p.get("quality_assurance_standard"),
        p.get("qualityAssuranceStandard"),
        cfg_notice.get("quality_assurance_standard"),
        cfg_notice.get("qualityAssuranceStandard"),
        default="",
    )
    return_cost_reason = _first_value(
        p.get("return_cost_reason"),
        p.get("returnCostReason"),
        cfg_notice.get("return_cost_reason"),
        cfg_notice.get("returnCostReason"),
        default="",
    )
    no_refund_reason = _first_value(
        p.get("no_refund_reason"),
        p.get("noRefundReason"),
        cfg_notice.get("no_refund_reason"),
        cfg_notice.get("noRefundReason"),
        default="",
    )
    compensation_procedure = _first_value(
        p.get("compensation_procedure"),
        p.get("compensationProcedure"),
        cfg_notice.get("compensation_procedure"),
        cfg_notice.get("compensationProcedure"),
        default="",
    )
    trouble_shooting_contents = _first_value(
        p.get("trouble_shooting_contents"),
        p.get("troubleShootingContents"),
        cfg_notice.get("trouble_shooting_contents"),
        cfg_notice.get("troubleShootingContents"),
        default="",
    )
    # 공통 5필드 중 어떤 것이 "설정에서 채워졌는지" 보고.
    # 상품 입력에 값이 없고(빈 문자열/공백/누락) config 에 비어있지 않은 값이
    # 있으면 해당 필드명(camelCase 고시 필드명 그대로)을 목록에 넣는다.
    # 이 목록은 페이로드 빌드 결과 메타(notice_filled_from_config)에 실려
    # 사용자에게 전달된다 — 묻지 않고 채워진 값이 조용히 딸려가면 잘못 신고된다.
    notice_filled_from_config = _notice_common_filled_from_config(p, cfg_notice)
    return {
        "item_name": product_name[:50],
        "model_name": _first_value(
            p.get("modelName"),
            p.get("model_name"),
            cfg_notice.get("model_name"),
            default=_model_name_default(p),
        ),
        "cert_detail": cert_text,
        "made_in": made_in,
        "manufacturer": manufacturer,
        "importer": importer,
        # manufacturer_importer 는 manufacturer/importer 둘 다 있을 때만
        # 합성한다. 어느 한쪽이라도 없으면 빈 문자열(임의 합성 금지).
        "manufacturer_importer": (
            f"{manufacturer} / {importer}" if (manufacturer and importer) else ""
        ),
        # 제조일자 기본문구("상세페이지 참조") 자동 삽입 제거.
        "manufacture_date": _first_value(
            p.get("manufacture_date"),
            p.get("manufacturedDate"),
            cfg_notice.get("manufacture_date"),
            default="",
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
            default="",
        ),
        "origin_area_code": _resolve_origin_area_code(p, cfg_notice),
        "origin_content": made_in,
        # 택배사 코드 — 판매자가 config/상품입력으로 제공한 값.
        # _resolve_delivery_company 는 상품 입력 → config 순으로 찾고, 둘 다
        # 없으면 빈 문자열을 반환한다 (as_tel/manufacturer/importor 와 동일).
        # fail-closed 검증은 register_product 등록 경계에서 수행한다.
        # 과거 이 자리에 "CJGLS" 하드코딩 폴백이 있었다 — 실측(D64) 으로
        # 이 스토어가 한진택배를 쓰는 것이 확인되어 틀린 신고였다.
        "delivery_company": _resolve_delivery_company(p, cfg_notice),
        "return_delivery_fee": _int_value(
            p.get("return_delivery_fee", cfg_notice.get("return_delivery_fee")),
            3000,
        ),
        "exchange_delivery_fee": _int_value(
            p.get("exchange_delivery_fee", cfg_notice.get("exchange_delivery_fee")),
            6000,
        ),
        # 어떤 공통 5필드가 상품 입력이 아닌 config 에서 채워졌는지.
        # build_payload 가 이 값을 페이로드 루트의 notice_filled_from_config
        # 메타에 싣는다(비어있지 않을 때만). 묻지 않고 채워진 값이 조용히
        # 딸려가면 잘못 신고된다.
        "notice_filled_from_config": notice_filled_from_config,
    }


# data/notice_types.json 을 단일 진실 공급원으로 사용해 35종 전체 고시
# 타입을 동적 생성한다. 타입·노드명·필드를 코드에 하드코딩하지 않는다.
_NOTICE_TYPES_CACHE: list | None = None
_NOTICE_TYPE_INDEX: dict | None = None


def _load_notice_type_specs() -> list:
    """``data/notice_types.json`` 의 verified 타입 목록을 반환 (단일 진실 공급원).

    캐싱되어 한 프로세스 내에서 반복 호출 시 디스크 I/O 가 발생하지 않는다.

    Raises:
        RuntimeError: 파일 부재 또는 구조 오류 (fail-closed).
    """
    global _NOTICE_TYPES_CACHE, _NOTICE_TYPE_INDEX
    if _NOTICE_TYPES_CACHE is not None:
        return _NOTICE_TYPES_CACHE
    path = common.package_data_path("notice_types.json")
    if not path.exists():
        raise RuntimeError(f"notice_types.json 파일이 없습니다: {path} (fail-closed).")
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"notice_types.json 읽기 실패: {path} ({exc})") from exc
    verified = doc.get("verified") if isinstance(doc, dict) else None
    if not isinstance(verified, list) or not verified:
        raise RuntimeError(f"notice_types.json 구조가 올바르지 않습니다: {path}")
    _NOTICE_TYPES_CACHE = verified
    _NOTICE_TYPE_INDEX = {
        str(entry.get("type") or "").strip().upper(): entry
        for entry in verified
        if isinstance(entry, dict) and entry.get("type")
    }
    return verified


def _notice_type_spec(notice_type: str) -> dict | None:
    """특정 고시 타입의 스펙(``{type, node, fields, ...}``)을 반환.

    Returns:
        매칭 dict 또는 ``None`` (알 수 없는 타입).
    """
    if _NOTICE_TYPE_INDEX is None:
        _load_notice_type_specs()
    key = str(notice_type or "").strip().upper()
    return _NOTICE_TYPE_INDEX.get(key) if _NOTICE_TYPE_INDEX else None


# 고시 35종 전체에 공통인 5개 필드. config 의 판매자 기본값에서 채운다.
# 사용자가 상품별로 값을 주면 그 값이 우선.
_NOTICE_COMMON_FIELDS = (
    "returnCostReason",
    "noRefundReason",
    "qualityAssuranceStandard",
    "compensationProcedure",
    "troubleShootingContents",
)

# 공통 5필드 "설정에서 채워졌는지" 보고용 후보.
# 각 항목: (고시 camelCase 필드명, 상품 입력 후보 튜플, config 후보 튜플).
# 별칭 만들지 말 것 — 매핑이 하나 더 생기면 갈라진다.
_NOTICE_COMMON_FIELD_CANDIDATES = (
    (
        "returnCostReason",
        ("return_cost_reason", "returnCostReason"),
        ("return_cost_reason", "returnCostReason"),
    ),
    (
        "noRefundReason",
        ("no_refund_reason", "noRefundReason"),
        ("no_refund_reason", "noRefundReason"),
    ),
    (
        "qualityAssuranceStandard",
        ("quality_assurance_standard", "qualityAssuranceStandard"),
        ("quality_assurance_standard", "qualityAssuranceStandard"),
    ),
    (
        "compensationProcedure",
        ("compensation_procedure", "compensationProcedure"),
        ("compensation_procedure", "compensationProcedure"),
    ),
    (
        "troubleShootingContents",
        ("trouble_shooting_contents", "troubleShootingContents"),
        ("trouble_shooting_contents", "troubleShootingContents"),
    ),
)


def _has_text(value) -> bool:
    """값이 "실질 정보" 인지 판정. placeholder 판정은 단일 진실 공급원에 위임한다.

    과거에는 None/빈 문자열/공백만 "값 없음" 으로 보는 독자 판정을 쓰다가,
    ``qa_agents`` 의 placeholder 판정(해당없음/상세참조/TBD/TODO/...) 과 어긋나
    복사해온 예시 설정의 placeholder 값이 "유효 입력" 으로 둔갑하는 회귀가 있었다
    (감리 지적). 본 함수는 ``qa_agents._is_placeholder_value`` 에 판정을 위임한다
    — 새 판정 함수를 만들지 않고 정본 하나를 쓴다.

    Returns:
        ``True`` = 실질 정보가 있는 값. ``False`` = None/빈/공백/placeholder.
    """
    from . import qa_agents

    return not qa_agents._is_placeholder_value(value)


def _notice_common_filled_from_config(p, cfg_notice) -> list:
    """공통 5필드 중 "상품 입력이 아닌 config 에서만 채워진" 필드명 목록.

    우선순위: **상품 입력 > config > 미설정(묻기)**.

    - 상품 입력에 비어있지 않은 값이 있으면 → config 가 채운 게 아니므로 제외.
      상품 입력의 "명시값"은 두 자리에서 찾는다:
        (1) top-level common 키(``p.returnCostReason`` 등)
        (2) ``p.notice.<node_key>.<field>`` — 사용자가 고시 본문 노드에 직접
            넣은 값. ``_merge_notice`` 가 이 값을 config 채운 본문 위에 덮어쓰므로,
            이 자리에서 주어진 값도 "명시값 우선" 대상이다. 이 경로를 빼면
            사용자가 준 값이 실제 본문에 들어갔는데도 "config 에서 왔다"고
            잘못 보고하게 된다.
    - 상품 입력이 비고 config 에 비어있지 않은 값이 있으면 → "config 에서 채워진"
      것으로 보고 해당 고시 camelCase 필드명을 목록에 넣는다.
    - config 값이 "" / 공백뿐이면 미설정 취급 — 채워지지 않은 것으로 본다
      (빈 값이 유효 입력으로 둔갑하면 안 된다).

    반환값은 페이로드 빌드 결과 메타(notice_filled_from_config)에 실려
    사용자에게 전달된다 — 묻지 않고 채워진 값이 조용히 딸려가면 잘못 신고된다.
    """
    # 사용자 고시 본문 노드(etc/wear/...) 의 모든 후보 본문을 모은다.
    # _merge_notice 의 node_key 선택 규칙을 따라 같은 node 를 찾는다.
    user_bodies: list = []
    user_notice = p.get("notice") if isinstance(p, dict) else None
    if isinstance(user_notice, dict):
        # 같은 node_key 우선, 없으면 etc/furniture 폴백 — _merge_notice 규칙.
        notice_type = (
            str(
                user_notice.get("productInfoProvidedNoticeType")
                or user_notice.get("notice_type")
                or p.get("notice_type")
                or p.get("productInfoProvidedNoticeType")
                or ""
            )
            .strip()
            .upper()
        )
        if not notice_type:
            notice_type = "ETC"
        spec = _notice_type_spec(notice_type)
        node_key = (spec or {}).get("node") or "etc"
        body = user_notice.get(node_key)
        if isinstance(body, dict):
            user_bodies.append(body)
        for fallback in ("etc", "furniture"):
            fb = user_notice.get(fallback)
            if isinstance(fb, dict):
                user_bodies.append(fb)

    filled: list = []
    for notice_field, p_keys, cfg_keys in _NOTICE_COMMON_FIELD_CANDIDATES:
        # (1) top-level common 키 우선.
        if any(_has_text(p.get(k)) for k in p_keys):
            continue
        # (2) 사용자 고시 본문 노드에 같은 camelCase 필드가 있으면 명시값.
        if any(_has_text(b.get(notice_field)) for b in user_bodies):
            continue
        if any(_has_text(cfg_notice.get(k)) for k in cfg_keys):
            filled.append(notice_field)
    return filled


def _common_notice_defaults(defaults) -> dict:
    """공통 5필드를 config 기본값에서 채운다."""
    return {
        "returnCostReason": defaults["return_cost_reason"],
        "noRefundReason": defaults["no_refund_reason"],
        "qualityAssuranceStandard": defaults["quality_assurance_standard"],
        "compensationProcedure": defaults["compensation_procedure"],
        "troubleShootingContents": defaults["trouble_shooting_contents"],
    }


def _category_path_for(category_id: str) -> str:
    """``category_id`` 의 카테고리 경로를 반환.

    판정 지점(build_payload)이 호출자가 경로를 넘겨주기를 기대하지 않고
    ``categoryId`` 만으로 스스로 경로를 조회하도록 돕는다.

    **조용한 ETC 강등 금지.** 과거에는 모든 예외를 잡아 빈 문자열로
    떨어뜨렸고, ``_resolve_notice_type`` 은 빈 경로를 ETC 기본값으로 해석했다.
    이는 데이터 파일 부재/손상(인프라 실패)을 "정말 ETC 인 카테고리" 와 구분하지
    못하는 근본 결함이다 — 결과적으로 잘못된 고시 타입으로 규제 필드를 신고하게
    된다.

    이제 ``CategoryMetaUnavailableError`` (데이터 파일 부재/손상) 를 잡아 빈
    문자열로 강등하지 않고 그대로 전파한다. 호출자(build_payload →
    register_product / prepare_listing) 의 예외 처리가 이를 등록 거부로
    번역한다 — 알 수 없음을 알 수 없음으로 다룬다(fail-closed).

    ``raise_if_unknown=False`` 이므로 알 수 없는 카테고리 ID 는 예외 없이 빈
    문자열을 반환한다. 이 경로는 "메타 데이터는 있지만 해당 ID 가 없다" 는
    뜻이므로 ETC 기본값이 합리적이다.

    mcp_server._category_path_for / register._category_path_for 와 동일한
    lookup(category_meta.category_path)을 쓴다.

    Raises:
        category_meta.CategoryMetaUnavailableError: 데이터 파일이 부재하거나
            읽을 수 없는 경우. 호출자가 등록 거부로 번역한다.
    """
    from . import category_meta

    return category_meta.category_path(category_id, raise_if_unknown=False)


def _resolve_notice_type(p) -> str:
    """상품 입력(p)에서 고시 타입을 결정한다.

    우선순위:
      1. ``p.notice.productInfoProvidedNoticeType`` / ``p.notice.notice_type``
      2. ``p.notice_type`` / ``p.productInfoProvidedNoticeType``
      3. 카테고리 경로 휴리스틱 (``text_props.CATEGORY_PATH_NOTICE_HINTS`` 재사용)

    이 판정 지점은 호출자가 카테고리 경로를 넘겨주기를 기대하지 않는다.
    ``categoryId`` 만 있으면 **스스로** ``_category_path_for`` 로 경로를
    조회해 휴리스틱을 돌린다. 호출자가 ``category_name``/``category_path``
    를 직접 준 경우 그것이 우선한다. 둘 다 없고 ``categoryId`` 도 조회되지
    않으면 ETC 기본값(회귀 없이 보존).

    단, **명시적으로 알 수 없는 타입**이 주어지면 ``ValueError``
    (조용한 etc 폴백 금지 — 잘못된 고시 타입은 규제 위반).

    Raises:
        ValueError: 명시적으로 주어진 타입이 data/notice_types.json 에 없음.
    """
    explicit = ""
    user_notice = p.get("notice") if isinstance(p, dict) else None
    if isinstance(user_notice, dict):
        explicit = (
            user_notice.get("productInfoProvidedNoticeType") or user_notice.get("notice_type") or ""
        )
    if not explicit and isinstance(p, dict):
        explicit = p.get("notice_type") or p.get("productInfoProvidedNoticeType") or ""
    notice_type = str(explicit or "").strip().upper()
    if not notice_type:
        # 카테고리 경로 휴리스틱으로 추론 시도.
        # CATEGORY_PATH_NOTICE_HINTS 의 정본은 :mod:`text_props` 에 있으며,
        # 본 모듈과 :mod:`qa_agents` 모두 거기서 import 한다 (단일 진실 공급원).
        # 호출자가 경로를 직접 주지 않았으면 categoryId 로 스스로 조회한다 —
        # 이 지점이 경로를 스스로 해석하지 않으면 게이트(mcp_server)와
        # 페이로드(build_payload) 가 서로 다른 타입을 보는 불일치가 재발한다.
        category_text = ""
        if isinstance(p, dict):
            category_text = " ".join(
                str(p.get(k) or "") for k in ("category_name", "category_path", "categoryPath")
            )
            if not category_text.strip():
                category_id = str(p.get("categoryId") or p.get("category_id") or "").strip()
                if category_id:
                    category_text = _category_path_for(category_id)
        for needle, inferred_type in CATEGORY_PATH_NOTICE_HINTS:
            if needle in category_text:
                notice_type = inferred_type
                break
    if not notice_type:
        # 회귀 없이 보존: 타입이 주어지지 않고 카테고리 추론도 실패하면
        # 기존 동작(ETC 기본값)을 유지한다. 단, 명시적으로 알 수 없는 타입이
        # 주어진 경우는 아래에서 에러를 낸다(조용한 etc 폴백 금지).
        notice_type = "ETC"
    spec = _notice_type_spec(notice_type)
    if spec is None:
        raise ValueError(
            f"알 수 없는 고시 타입: {notice_type!r} — data/notice_types.json 에 "
            f"등록된 타입이 아닙니다. 조용한 etc 폴백 금지."
        )
    return notice_type


def _is_furniture_notice(p):
    """FURNITURE 타입 판정 (기존 동작 보존)."""
    notice_type = (
        str(p.get("notice_type") or p.get("productInfoProvidedNoticeType") or "").strip().upper()
    )
    if notice_type == "FURNITURE":
        return True
    user_notice = p.get("notice")
    if isinstance(user_notice, dict):
        notice_type = str(user_notice.get("productInfoProvidedNoticeType") or "").strip().upper()
        if notice_type == "FURNITURE":
            return True
    category_text = " ".join(
        str(p.get(k) or "") for k in ("category_name", "category_path", "categoryPath")
    )
    return "가구" in category_text


def _base_etc_notice(defaults):
    """ETC 타입의 기본 본문 — 값이 있는 필드만 싣는다.

    코드가 만든 기본 문구를 넣지 않는다. config/입력 어디에서도 값이 주어지지
    않은 필드는 payload 에서 생략하고, 컴플라이언스 검사가 해당 필드를 필수
    항목 누락으로 FAIL 지적한다. 조용한 채움 금지.
    """
    cert = defaults["cert_detail"]
    notice: dict = {"itemName": defaults["item_name"]}
    if cert:
        notice["certDetail"] = cert
        notice["certificationDetails"] = cert
    notice["madeIn"] = defaults["made_in"]
    notice["countryOfOrigin"] = defaults["made_in"]
    if defaults.get("manufacturer"):
        notice["manufacturer"] = defaults["manufacturer"]
    if defaults.get("manufacturer_importer"):
        notice["manufacturerImporter"] = defaults["manufacturer_importer"]
    if defaults.get("manufacture_date"):
        notice["manufactureDate"] = defaults["manufacture_date"]
    if defaults.get("quality_assurance_standard"):
        notice["qualityAssuranceStandard"] = defaults["quality_assurance_standard"]
    if defaults.get("return_cost_reason"):
        notice["returnCostReason"] = defaults["return_cost_reason"]
    if defaults.get("no_refund_reason"):
        notice["noRefundReason"] = defaults["no_refund_reason"]
    if defaults.get("compensation_procedure"):
        notice["compensationProcedure"] = defaults["compensation_procedure"]
    if defaults.get("trouble_shooting_contents"):
        notice["troubleShootingContents"] = defaults["trouble_shooting_contents"]
    # afterServiceDirector 는 manufacturer 와 as_tel 이 모두 있을 때만
    # 합성한다. 어느 한쪽이라도 비면 임의 문자열을 만들어 넣지 않는다.
    if defaults.get("manufacturer") and defaults.get("as_tel"):
        notice["afterServiceDirector"] = f"{defaults['manufacturer']} {defaults['as_tel']}"
    # modelName 은 config/입력에 있을 때만. importer 도 값이 있을 때만.
    if defaults.get("model_name"):
        notice["modelName"] = defaults["model_name"]
    if defaults.get("importer"):
        notice["importer"] = defaults["importer"]
    return notice


def _base_furniture_notice(p, defaults):
    """FURNITURE 타입의 기본 본문 — 임의 문구 자동삽입 제거.

    소재/크기/구성품/안전기준 필드에 코드가 만든 "상세참조"/"해당없음 / 상세참조"
    같은 기본 문자열을 박던 옛 동작을 제거했다. 해당 필드들은 소비자 고시값이며
    임의값을 지어 넣으면 허위 신고가 된다. 값이 없으면 필드를 생략하고
    컴플라이언스 검사가 필수 항목 누락으로 FAIL 지적한다.
    """
    notice = _base_etc_notice(defaults)
    material = _first_value(p.get("material"), p.get("fabric"), p.get("소재"), default="")
    size = _first_value(p.get("size"), p.get("dimensions"), default="")
    components = _first_value(p.get("components"), p.get("composition"), default="")
    safety = _first_value(p.get("safety_standard"), p.get("safetyStandard"), default="")
    if material:
        notice["material"] = material
    if size:
        notice["size"] = size
    if components:
        notice["components"] = components
    if safety:
        notice["safetyStandard"] = safety
    return notice


def _enforce_notice_as_contact_exclusive(notice_body, user_fields=None):
    """A/S 연락처 단일 노출 정책 — 데이터 기반 XOR 상호배제로 교체 (기존 동작 보존).

    본 함수는 이제 ``data/notice_field_relations.json`` 의 XOR 관계를 읽어
    처리한다. ETC 타입의 ``afterServiceDirector``/``customerServicePhoneNumber``
    상호배제도 이 데이터를 통해 적용된다 — 코드에 박힌 특수처리가 아니라
    확인된 관계 데이터로 통일한다.

    **조용한 선택 금지 (티켓 계약)**: XOR 그룹에서 둘 다 값이 있으면
    **조용히 하나를 버리지 않는다.** 게이트에서 이미 막혀야 하지만,
    방어적으로도 조용한 선택은 금지다. 본 함수는 사용자가 명시적으로 하나만
    제공한 경우(다른 하나는 config 가 채운 경우)에만 상대편을 제거한다.
    둘 다 사용자가 명시적으로 제공한 경우에는 어느 쪽도 버리지 않고 그대로
    둔다 — 게이트가 "고시 필드 상호배제" 위반으로 차단한다.
    """
    user_fields = set(user_fields or ())

    def has_text(value):
        return value is not None and bool(str(value).strip())

    # 사용자가 명시적으로 제공한 필드에 대해 상호배제 처리.
    # 데이터에 기록된 XOR 그룹을 읽어, 사용자가 하나만 명시하고 다른 하나는
    # config 가 채운 경우에만 상대편을 제거한다 (단일 노출 정책).
    user_after = "afterServiceDirector" in user_fields
    user_customer = "customerServicePhoneNumber" in user_fields
    body_after = has_text(notice_body.get("afterServiceDirector"))
    body_customer = has_text(notice_body.get("customerServicePhoneNumber"))

    # 사용자가 customerServicePhoneNumber 만 명시 → afterServiceDirector 제거.
    if user_customer and not user_after and body_after:
        notice_body.pop("afterServiceDirector", None)
    # 그 외에 customerServicePhoneNumber 를 제거해야 하는 두 경우를 합친다:
    #   (a) 사용자가 afterServiceDirector 만 명시 (config 가 customer 채움).
    #   (b) 둘 다 사용자가 명시하지 않음 (config 가 둘 다 채움) — 회귀 방지.
    # 둘 다 사용자가 명시한 경우는 어느 쪽도 버리지 않는다
    # (게이트의 "고시 필드 상호배제" 위반이 이 케이스를 잡는다).
    elif body_customer and not user_customer and (user_after or body_after):
        notice_body.pop("customerServicePhoneNumber", None)


def _base_notice_body_for_type(p, defaults, notice_type, spec):
    """고시 타입별 기본 본문을 생성.

    ETC/FURNITURE 는 기존 빌더를 그대로 사용(회귀 없이 보존).
    그 외 33종은 공통 5필드 + 빈 타입별 필드로 시작 — 값을 지어내지 않는다.
    사용자 입력이 _merge_notice 에서 덮어쓴다.
    """
    if notice_type == "ETC":
        return _base_etc_notice(defaults)
    if notice_type == "FURNITURE":
        return _base_furniture_notice(p, defaults)
    # 나머지 33종: 공통 5필드 + afterServiceDirector(있는 타입만)로 시작.
    body = _common_notice_defaults(defaults)
    fields = spec.get("fields") or []
    # afterServiceDirector/customerServicePhoneNumber 는 제조사·AS 전화가
    # 모두 있을 때만 합성/입력한다. 어느 한쪽이라도 비면 임의 문자열을 넣지
    # 않고 컴플라이언스 검사가 필수 항목 누락으로 FAIL 지적한다.
    if "afterServiceDirector" in fields and defaults.get("manufacturer") and defaults.get("as_tel"):
        body["afterServiceDirector"] = f"{defaults['manufacturer']} {defaults['as_tel']}"
    if "customerServicePhoneNumber" in fields and defaults.get("as_tel"):
        body["customerServicePhoneNumber"] = defaults["as_tel"]
    # manufacturer 필드가 있으면 config/입력에서(값이 있을 때만).
    if "manufacturer" in fields and defaults.get("manufacturer"):
        body["manufacturer"] = defaults["manufacturer"]
    if "importer" in fields and defaults.get("importer"):
        body["importer"] = defaults["importer"]
    return body


def _validate_notice_field_type(field, value):
    """고시 필드 타입에 맞는 값인지 검증 (조용한 변환 금지).

    ``data/notice_field_types.json`` 에 타입이 기록된 필드에 대해서만 검증한다.
    미기재 필드는 문자열(기존 동작)이므로 이 함수는 아무것도 하지 않는다.

    핵심 계약 — **조용한 변환 금지**:
      - ``boolean`` 필드에 문자열이 오면 ``"예"``/``"true"`` 를 알아서 해석하지
        않는다. 잘못 신고되는 것을 막기 위해 ``ValueError`` 로 거부한다.
        거부 사유에 "예/아니오로 답해야 하는 항목" 임을 밝힌다.
        ``True``/``False`` (Python bool) 만 허용한다.
      - ``date`` 필드는 받은 값을 그대로 둔다(형식 미확정 — 가공하지 않는다).
      - ``string``/미기재 필드는 받은 값을 그대로 둔다(기존 동작).

    Returns:
        검증을 통과한 값(변환하지 않고 입력값 그대로).

    Raises:
        ValueError: ``boolean`` 필드에 bool 이 아닌 값이 들어온 경우.
    """
    from . import qa_agents

    ftype = qa_agents._notice_field_type(field)
    if ftype == "boolean":
        # True/False 만 허용. bool 의 서브클래스인 int(True=1, False=0) 중
        # bool 리터럴만 받고 정수 1/0 은 거부한다 — 의도를 명확히 하기 위해.
        if not isinstance(value, bool):
            raise ValueError(
                f"고시 필드 '{field}' 은(는) 예/아니오로 답해야 하는 항목(boolean)입니다. "
                f"true/false(Python bool) 로 답해야 합니다. 받은 값: {value!r} "
                f"(타입 {type(value).__name__}). 문자열을 알아서 해석하지 않습니다 — "
                f"잘못 신고되는 것을 막기 위해 거부합니다."
            )
    # date / string / 미기재: 받은 값을 그대로 둔다(형식 미확정, 가공 금지).
    return value


def _merge_notice(default_notice, user_notice):
    """사용자 notice 를 기본 notice 에 병합 (데이터 기반 노드명 사용).

    노드 키를 ``etc``/``furniture`` 로 고정하지 않고, 고시 타입에
    해당하는 node 이름(data/notice_types.json)을 사용한다.
    사용자 입력은 같은 노드 키 아래에서 우선한다.

    **필드 타입 검증**: 사용자가 제공한 값은 ``_validate_notice_field_type``
    을 거쳐 해당 필드의 타입(string/boolean/date)에 맞는지 검증받는다.
    boolean 필드에 문자열을 주면 거부한다(조용한 변환 금지).
    """
    if not isinstance(user_notice, dict):
        return default_notice
    notice_type = str(default_notice.get("productInfoProvidedNoticeType") or "ETC").strip().upper()
    spec = _notice_type_spec(notice_type)
    node_key = (spec or {}).get("node") or "etc"
    # 기본 본문: default_notice 에서 node_key 본문을 찾고, 없으면 etc/furniture 폴백.
    default_body = default_notice.get(node_key)
    if not isinstance(default_body, dict):
        for fallback in ("etc", "furniture"):
            fb = default_notice.get(fallback)
            if isinstance(fb, dict):
                default_body = dict(fb)
                break
        if not isinstance(default_body, dict):
            default_body = {}
    else:
        default_body = dict(default_body)
    merged = {
        "productInfoProvidedNoticeType": notice_type,
        node_key: default_body,
    }
    # 사용자 본문: 같은 node_key 우선, 없으면 etc/furniture.
    user_body = user_notice.get(node_key)
    if not isinstance(user_body, dict):
        for fallback in ("etc", "furniture"):
            fb = user_notice.get(fallback)
            if isinstance(fb, dict):
                user_body = fb
                break
    if not isinstance(user_body, dict):
        user_body = {}
    user_fields = set()
    for field, value in user_body.items():
        # 사용자가 명시적으로 제공한 값은 그대로 싣는다.
        # 특정 문자열("상세페이지 참조" 등)이라고 조용히 버리는 필터를 제거했다.
        # "상세페이지 참조" 는 한국 커머스에서 판매자가 제조일자·치수 등에 일상적으로
        # 쓰는 정당한 표기이며 시스템이 임의로 폐기할 값이 아니다.
        # 빈 문자열·공백만·None 은 기존대로 싣지 않는다(값이 없는 것과 값이 있는
        # 것은 구분).
        text = str(value).strip() if value is not None else ""
        if text:
            # 필드 타입 검증: boolean 필드에 문자열을 주면 거부(조용한 변환 금지).
            # date/string 필드는 받은 값을 그대로 둔다.
            validated = _validate_notice_field_type(field, value)
            merged[node_key][field] = validated
            user_fields.add(field)
    _enforce_notice_as_contact_exclusive(merged[node_key], user_fields)
    return merged


def _product_info_notice(p, defaults):
    """고시 payload 조립 (35종 전체 지원).

    고시 타입이 무엇이든 ``data/notice_types.json`` 에서 해당 타입의 ``node``
    이름을 찾아 그 이름으로 본문을 싣는다. 타입이 데이터에 없으면 에러.
    """
    notice_type = _resolve_notice_type(p)
    spec = _notice_type_spec(notice_type)
    # spec 은 _resolve_notice_type 이 이미 검증했으나 방어적으로 확인.
    if spec is None:
        raise ValueError(
            f"알 수 없는 고시 타입: {notice_type!r} — data/notice_types.json 에 "
            f"등록된 타입이 아닙니다."
        )
    body = _base_notice_body_for_type(p, defaults, notice_type, spec)
    base = {
        "productInfoProvidedNoticeType": notice_type,
        spec["node"]: body,
    }
    return _merge_notice(base, p.get("notice"))


def get_token():
    """OAuth2 client_credentials + bcrypt 서명. 토큰 문자열 반환(시크릿 노출 안 함)."""
    c = load_config()["naver"]
    cid, csec = c["client_id"], c["client_secret"]
    ts = str(int(time.time() * 1000))
    sign = base64.b64encode(bcrypt.hashpw(f"{cid}_{ts}".encode(), csec.encode())).decode()
    r = requests.post(
        BASE + "/external/v1/oauth2/token",
        timeout=20,
        data={
            "client_id": cid,
            "timestamp": ts,
            "client_secret_sign": sign,
            "grant_type": "client_credentials",
            "type": c.get("type", "SELF"),
        },
    )
    r.raise_for_status()
    return r.json()["access_token"]


def _probe_token_endpoint():
    """토큰 엔드포인트(POST /external/v1/oauth2/token)에 실제로 한 번 호출해 본다.

    ``get_token`` 과 같은 인증 절차(OAuth2 client_credentials + bcrypt 서명)를
    따르되, 성공한 액세스 토큰 값을 반환하지 않고 **연결 가능성만** 보고한다.

    본 함수는 ``get_token`` 이 던지는 ``requests.HTTPError``/``RequestException``
    을 잡아 HTTP 상태/예외 타입을 사람 말로 해석한 진단 dict 를 반환한다.
    성공 시 토큰 값을 반환에 싣지 **않는다** — 이 함수의 목적은 "되는가?"
    이지 "토큰 값을 얻는 것" 이 아니다.

    Returns:
        ``{"ok": bool, "status_code": int | None, "detail": str}`` —
        - ``ok``: HTTP 2xx 면 True.
        - ``status_code``: HTTP 상태 코드(네트워크 예외 시 None).
        - ``detail``: 정화된 사유 문자열(``common.sanitize_text`` 경유).
          예외 타입/HTTP 상태는 남고, 민감 정보(시크릿·경로)는 가려진다.
    """
    c = load_config()["naver"]
    cid, csec = c["client_id"], c["client_secret"]
    ts = str(int(time.time() * 1000))
    sign = base64.b64encode(bcrypt.hashpw(f"{cid}_{ts}".encode(), csec.encode())).decode()
    try:
        r = requests.post(
            BASE + "/external/v1/oauth2/token",
            timeout=20,
            data={
                "client_id": cid,
                "timestamp": ts,
                "client_secret_sign": sign,
                "grant_type": "client_credentials",
                "type": c.get("type", "SELF"),
            },
        )
    except requests.RequestException as exc:
        # 네트워크/연결 실패 — 사유(예외 타입)는 남기고 본문은 정화.
        return {"ok": False, "status_code": None, "detail": common.sanitize_error(exc)}
    sc = r.status_code
    if 200 <= sc < 300:
        return {"ok": True, "status_code": sc, "detail": "정상"}
    # 비 2xx — 응답 본문 전체를 정화해서 사유에 싣는(본문에 시크릿·경로가
    # 섞여 있으면 가린다). 사유(상태 코드)는 detail 접두사로 남긴다.
    body_text = ""
    try:
        if r.headers.get("content-type", "").startswith("application/json"):
            body_text = json.dumps(r.json(), ensure_ascii=False)
        else:
            body_text = r.text or ""
    except Exception:
        body_text = ""
    detail = common.sanitize_text(f"HTTP {sc}: {body_text}".strip())
    return {"ok": False, "status_code": sc, "detail": detail}


def _h(tk, json_ct=True):
    h = {"Authorization": f"Bearer {tk}"}
    if json_ct:
        h["Content-Type"] = "application/json"
    return h


# ---------------------------------------------------------------------------
# 토큰 만료(GW.AUTHN) 재시도 공통 지점.
#
# 네이버 문서 계약: 401 + 응답 본문 ``code == "GW.AUTHN"`` 은 토큰 만료.
# 이 지점 하나에서 토큰을 1회 재발급하고 요청을 1회 재시도한다.
# - 재시도는 정확히 1회. 두 번째 실패는 그대로 올린다(무한 재시도 금지).
# - 401 만으로 재시도하지 않는다 — 본문 ``code == "GW.AUTHN"`` 을 함께 본다.
# - 본문이 JSON 이 아니거거나 ``code`` 가 없으면 재시도하지 않는다.
# - 토큰·시크릿·서명 값은 절대 남기지 않는다(비밀값 로그 유출 금지).
#
# 10곳의 API 호출부가 이 지점을 경유한다(중복 방지). 기존 호출부 시그니처는
# 유지된다 — 헬퍼가 응답을 그대로 돌려주고, 호출부가 기존 방식으로 소비한다.
# (호출부 세기 방법: 변수에 대입하며 ``_api_request`` 여는 괄호를 부르는 줄을
# 정규식 ``=\\s*_api_request\\(`` 로 잡아 센다 — 명령은
# ``grep -nE`` ``'=\\s*_api_request\\('`` ``src/clossify/naver_client.py``.
# 정의부(``def ...``) 와 이 주석 자신은 이 패턴과 맞지 않는 형태로 적었으므로
# 세지 않는다. 따라서 위 숫자를 손으로 갱신할 필요 없이 명령을 돌려 실측하면
# 된다. ``_post_product_payload`` 안의 호출도 변수 대입형이므로 같이 센다.)
# 2026-08-12 실측: 10건.
#
# **버퍼 상한(3라운드 감리)**: ``mcp_server`` 처럼 오래 떠 있는 프로세스에서
# 토큰 만료가 반복되면 ``_AUTHN_RETRY_EVENTS`` 가 무한히 자란다(운영 코드에
# 비우는 곳이 없었다). 상한 있는 ``collections.deque(maxlen=...)`` 로 바꿔
# 가장 오래된 이벤트부터 자동으로 밀려난다. ``len()``/``[i]``/``.clear()``
# 등 시험이 쓰는 인터페이스는 list 와 동일하므로 기존 소비처가 깨지지 않는다.
# ``_AUTHN_RETRY_EVENTS_MAXLEN`` — 재시도 1회당 1행이 쌓이므로, 등록 작업
# 하나(``register_product``) 의 다중 POST 안에서는 최대 수 건이다.
# ``mcp_server`` 가 하루 수백 건의 상품을 등록해도 수천 건을 넘지 않는다.
# 1000은 넉넉한 진단 예산이면서 메모리 부담은 무시할 만한 값이다(행당
# 수십 바이트).
# ---------------------------------------------------------------------------
_AUTHN_RETRY_EVENTS_MAXLEN = 1000
_AUTHN_RETRY_EVENTS: collections.deque[dict] = collections.deque(maxlen=_AUTHN_RETRY_EVENTS_MAXLEN)
# **단조 증가 재시도 카운터 (WO PR #27 8라운드 ①)**: ``_AUTHN_RETRY_EVENTS`` 가
# ``deque(maxlen=1000)`` 이라 **가득 차면 ``len()`` 이 안 변한다**. 그래서 길이
# 변화로 "재시도가 있었나"를 판정하던 ``mcp_server`` 삭제 예외 로직이
# 1000회째부터 영구히 "재시도 없었음"으로 잘못 판정했다. 이 단조 증가 정수
# 카운터는 버퍼 크기와 무관하게 매 재시도마다 1씩 올라가므로, 호출자는
# 이전·이후 값을 비교해 정확히 "이번 요청 안에 재시도가 있었나"를 알 수 있다.
_AUTHN_RETRY_COUNT: int = 0

# 게이트웨이 인증만료 코드 (네이버 문서 고정값).
_GW_AUTHN_CODE = "GW.AUTHN"


class _TokenRef:
    """한 **논리적 작업** 안에서 토큰을 들고 다니는 변경 가능한 홀더.

    네이버 상품 등록(``register_product``) 은 한 작업이 여러 API 요청으로
    이뤄진다(첫 POST → 제한태그 응답 → 태그 제거 후 재시도 → 전체 제거
    재시도). ``_api_request`` 안에서 401+``GW.AUTHN`` 시 재발급한 토큰이
    **지역 변수로 끝나면** 뒤따르는 요청들이 다시 만료된 토큰을 써 매번
    확정적으로 한 번 더 401 을 맞고 토큰을 또 발급한다. 토큰 엔드포인트가
    흔들리거나 제한되면 등록이 불필요하게 실패한다.

    본 홀더는 작업 단위로 토큰을 들고 다니기 위한 최소한의 장치다.
    **전역 캐시가 아니다** — 작업이 끝나면 홀더도 같이 버린다. 토큰 만료
    시점 관리가 딸려오는 전역 캐시는 이 작업의 범위 밖이다.

    ``_api_request`` 에 ``tk_ref`` 로 넘기면:
      - 최초 요청은 ``tk_ref.value`` 토큰으로 나간다.
      - 401+``GW.AUTHN`` 재발급 시 ``get_token()`` 결과를 ``tk_ref.value``
        에 **다시 쓴다** → 같은 작업 안의 뒤따르는 요청들은 새 토큰으로
        나간다(401 재발생 없음).

    단일 요청 호출부(``get_product`` 등)는 기존대로 ``tk=`` 문자열만 넘기고
    ``_TokenRef`` 를 쓰지 않는다 — 거기에는 전파 문제가 없다.
    """

    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


def _is_authn_expired_response(response) -> bool:
    """응답이 401 + ``{"code": "GW.AUTHN"}`` 인지 판정.

    본문이 JSON 이 아니거나 ``code`` 키가 없으면 False (재시도 안 함).
    """
    if response is None or response.status_code != 401:
        return False
    try:
        body = response.json()
    except (ValueError, OSError):
        return False
    return isinstance(body, dict) and body.get("code") == _GW_AUTHN_CODE


# 지원하는 HTTP 메서드 집합. ``_api_request`` 는 ``getattr(requests, <verb>)``
# 로 런타임 조회하여 호출한다 — 시험이 ``naver_client.requests.get`` 등을
# 몽키패치하면 그 패치가 즉시 반영되어야 한다. 미리 캡처한 함수 참조를
# 딕셔너리에 넣으면 패치가 반영되지 않아 기존 시험이 실제 네트워크로
# 나가는 회귀가 생긴다.
_SUPPORTED_METHODS = frozenset({"GET", "POST", "PUT", "DELETE"})


def _extract_file_spec(entry):
    """``requests`` 멀티파트 ``files`` 의 한 항목에서 ``(filename, file_obj[, mime])``
    튜플을 안전하게 꺼낸다.

    ``requests`` 는 ``files`` 를 두 가지 표준 형태로 받는다:

      1. **리스트형**: ``[(field_name, (filename, file_obj, [mime])), ...]``
         → 각 원소는 ``(field_name, file_tuple)`` 2-튜플.
      2. **dict형**: ``{field_name: (filename, file_obj, [mime])}``
         → ``.values()`` 로 꺼낸 항목이 **file_tuple 자체** 다.

    이 함수는 두 형태를 구분한다. ``entry`` 가 2-튜플이고 첫 원소가 문자열
    (field_name) 이며 둘째 원소가 tuple/list 인 경우 → 리스트형 으로 보고
    ``entry[1]`` 을 반환한다. 그 외에는 ``entry`` 자체를 file_tuple 으로
    본다(dict형 또는 ``requests`` 가 허용하는 ``bytes``/``str`` 단일값).

    단순 ``bytes``/``str`` 값(``{"f": b"..."}`` 형태)은 file_tuple 이 아닌
    그 값 자체를 반환한다 — 호출자는 이를 다시 isinstance 검사로 걸러낸다.

    Args:
        entry: ``files.values()`` (dict형) 또는 ``files`` 직접 순회 (리스트형)
            의 개별 항목.

    Returns:
        ``(filename, file_obj[, mime])`` 튜플, 또는 단순 ``bytes``/``str`` 값,
        또는 그 외 식별 불가 형태.
    """
    # 리스트형: (field_name, (filename, file_obj, [mime]))
    # field_name 은 항상 문자열. file_tuple 은 tuple/list.
    if (
        isinstance(entry, tuple | list)
        and len(entry) == 2
        and isinstance(entry[0], str)
        and isinstance(entry[1], tuple | list)
    ):
        return entry[1]
    # dict형(file_tuple 자체) 또는 단순 bytes/str 값.
    return entry


def _rewind_files_for_retry(kwargs) -> bool:
    """``kwargs["files"]`` 안의 모든 파일 스트림을 ``seek(0)`` 으로 되감는다.

    ``requests`` 는 멀티파트 바디를 만들 때 각 파일 객체를 ``read()`` 한다.
    첫 요청이 끝나면 핸들은 EOF 다. 같은 ``files`` 로 재시도하면 **0바이트** 가
    올라간다. 이 함수는 재시도 직전에 모든 핸들을 되감아 바디를 다시 만들 수
    있게 한다.

    ``files`` 구조는 ``requests`` 의 multipart 형식을 따른다:
    ``[(field_name, (filename, file_obj, mime)), ...]`` 또는
    ``{field_name: (filename, file_obj, mime), ...}``. ``file_obj`` 자리가
    ``bytes`` 인 경우는 되감을 필요가 없다(불변).

    되감을 수 없는 스트림(``seek`` 불가·``seek`` 실패)이 하나라도 있으면
    **False** 를 반환한다 — 호출자는 이 경우 **재시도하지 않고** 원 응답을
    그대로 반환해야 한다(빈 본문으로 재시도하는 것보다 낫다).
    ``files`` 가 없거나 비어 있으면 True (되감을 것 없음 — 재시도 가능).
    """
    files = kwargs.get("files")
    if not files:
        return True
    # requests 의 ``files`` 는 두 가지 표준 형태를 받는다(둘 다 널리 쓰인다):
    #
    #   1. 리스트형: ``[(field_name, (filename, file_obj, [mime])), ...]``
    #      → 각 원소는 ``(field_name, file_tuple)``. ``upload_images`` 가 쓰는 형태.
    #   2. dict형: ``{field_name: (filename, file_obj, [mime])}``
    #      → ``.values()`` 는 **이미 file_tuple 자체**를 내놓는다.
    #
    # 이전 구현은 두 형태를 동일한 ``entry`` 로 취급해 ``entry[1]`` 을
    # ``file_spec`` 으로 썼다 — 리스트형에서는 ``(filename, file_obj)`` 가
    # 나오지만, dict형에서는 **스트림 자체** 가 나온다. 뒤따르는 튜플/리스트
    # 검사가 스트림을 건너뛰어 ``True`` 를 반환 → 되감기가 일어나지 않은 채
    # 재시도가 0바이트 본문을 보냈다(T3, 머지 차단급).
    #
    # 아래는 dict 값(file_tuple 자체) 과 리스트 항목 ``(field_name, file_tuple)``
    # 을 구분한다. ``bytes``/``str`` 만 들어 있는 단순 형태(``{"f": b"..."}``
    # 또는 ``[(field_name, b"...")]``)도 안전하게 건너뛴다.
    iterable = files.values() if isinstance(files, dict) else files
    for entry in iterable:
        file_spec = _extract_file_spec(entry)
        # file_spec: (filename, file_obj) 또는 (filename, file_obj, mime)
        if not isinstance(file_spec, tuple | list) or len(file_spec) < 2:
            continue
        file_obj = file_spec[1]
        # bytes/str 은 불변 — 매 요청마다 새로 읽혀도 같은 내용.
        if isinstance(file_obj, bytes | str):
            continue
        # file-like 객체 — seek 가 있어야 되감을 수 있다.
        seek = getattr(file_obj, "seek", None)
        if seek is None:
            return False
        try:
            seek(0)
        except (OSError, ValueError, io.UnsupportedOperation):
            return False
    return True


def _api_request(
    method,
    url,
    *,
    tk,
    header_builder,
    tk_ref=None,
    allow_retry=False,
    injected_token=False,
    **kwargs,
):
    """``requests.<verb>`` 로 위임하는 공통 API 호출 지점.

    ``method`` 에 따라 ``requests.get`` / ``requests.post`` / ``requests.put`` /
    ``requests.delete`` 로 위임한다(런타임 ``getattr`` 조회). 이 위임은
    기존 시험이 감시하던 호출 표면을 유지하기 위함이다.

    401 + ``GW.AUTHN`` **이고** ``allow_retry=True`` 일 때만 토큰을
    1회 재발급(``get_token()``) 하고 요청을 1회 재시도한다. 재시도도 같은
    위임 함수를 탄다. 그 외(403·500·401-타코드·JSON아님·``allow_retry=False``)
    는 그대로 반환한다.

    **``allow_retry`` 기본값은 ``False`` (재시도 안 함)** 이 안전하다.
    재시도는 **명시적으로 안전하다고 판단한 호출부만** ``allow_retry=True``
    로 켠다. 되돌리기 어려운 요청(상품 신규 등록 POST) 이 기본값으로
    재시도되는 일이 없도록 기본을 닫아둔다.

    **재시도 안전 전제(근거) — 네이버 커머스API 인증 문서(공개 문서) 원문**:

      "API 요청 및 응답을 정상적으로 수신하는 중 API 응답에 401 HTTP 상태
       코드와 함께 게이트웨이 서버 오류 코드 ``GW.AUTHN`` 이 포함되어 있다면
       인증 토큰이 만료되었을 가능성이 높습니다. … 인증 토큰 발급 요청 API 를
       재호출하여 인증 토큰을 재발급받는 fallback 처리를 권장합니다."

    위 문서는 **재발급 후 재호출을 권장할 뿐**, 원 요청이 서버에서
    처리되지 않았음을 **보장하지 않는다**. 즉 게이트웨이가 인증 단계에서
    잘랐다면 원 요청은 백엔드에 도달하지 않았을 것이지만, 우리는 그것을
    증명할 수 없다. 따라서:

    - **상품 신규 등록 POST(``_post_product_payload``)**: ``allow_retry``
      를 끄지 않는다(기본값 ``False``). 401+``GW.AUTHN`` 을 받으면 재시도
      없이 호출자에게 올린다 — 중복 상품이 라이브 마켓에 올라가는 위험을
      감수할 이유가 없다.
    - **그 외 경로(조회·이미지 업로드·태그 추천·수정·삭제)**: 각 호출부에서
      ``allow_retry=True`` 로 켠다. 이유는 각 호출부 주석에 한 줄씩 적는다.

    **파일 업로드 재시도**: ``files`` 가 포함된 경우, 재시도 직전에
    ``_rewind_files_for_retry`` 로 모든 파일 스트림을 ``seek(0)`` 되감는다.
    되감을 수 없는 스트림이 섞이면 **재시도를 포기하고 원 401 응답을 그대로
    반환**한다 — 빈 바이트로 재시도하는 것(0바이트 이미지 업로드)보다 낫다.

    **토큰 전파(작업 단위)**: ``tk_ref`` 로 :class:`_TokenRef` 를 넘기면,
    401+``GW.AUTHN`` 재발급 시 새 토큰을 ``tk_ref.value`` 에 **다시 쓴다**.
    같은 논리적 작업(예: ``register_product`` 의 다중 POST 루프) 안의
    뒤따르는 요청들이 재발급된 토큰으로 나간다 — 매번 401 을 다시 맞고
    토큰을 또 발급하는 비효율·실패 원인이 사라진다. ``tk_ref`` 를 생략하면
    기존 동작(재발급 토큰이 이 호출 안에서만 쓰임)을 유지한다.

    Args:
        method: HTTP 메서드 문자열 (``"GET"``·``"POST"``·``"PUT"``·``"DELETE"``).
        url: 전체 URL.
        tk: 현재 액세스 토큰.
        header_builder: ``tk`` 를 받아 헤더 dict 를 반환하는 호출 가능 객체
            (``_h`` 의 부분 적용). 토큰 재발급 시 새 토큰으로 재생성한다.
        tk_ref: 작업 단위 토큰 홀더(:class:`_TokenRef`) 또는 ``None``.
            ``None`` 이 아니면 재발급한 토큰을 이 홀더에 다시 써서 같은
            작업의 뒤따르는 요청들이 새 토큰으로 나가게 한다.
        allow_retry: 401+``GW.AUTHN`` 시 재시도 허용 여부. 기본값 ``False``
            (안전 쪽). 명시적으로 ``True`` 를 넘긴 호출부만 재시도한다.
        injected_token: ``tk`` 가 외부에서 주입된 토큰인지 여부. ``True`` 면
            401+``GW.AUTHN`` 시 **재시도/갱신하지 않고** 사유 있는 ``RuntimeError``
            를 올린다. 외부 토큰은 출처(자격증명) 를 알 수 없어, 우리 설정
            기반 ``get_token()`` 으로 갱신하면 **다른 신원**으로 요청이
            나간다 (WO PR #27 8라운드 ②). 래퍼 함수가 ``tk is not None`` 일 때
            ``True`` 로 넘긴다.
        **kwargs: 위임 함수에 전달할 추가 인자(``data``·``timeout``·…).
            ``headers`` 는 ``header_builder`` 가 생성하므로 넘기지 않는다.

    Returns:
        ``requests.Response``.

    **요청 단위 재시도 사실 (WO PR #27 9라운드)**: 이 함수가 **이 호출 한 건**
        안에 재시도가 일어났는지를 응답 객체의 ``_clossify_retried`` 속성으로
        호출자에게 직접 알려준다. ``True`` 면 이 응답이 재시도의 결과(또는
        재시도 후의 최종 응답) 임을 뜻한다. 재시도가 없었으면 속성을 아예
        두지 않는다(예: ``r.__dict__`` 에 ``_clossify_retried`` 가 없음).
        이 사실은 **오직 이 응답 객체에 묶여 있다** — 프로세스 전역
        카운터(``_AUTHN_RETRY_COUNT``) 나 공유 버퍼(``_AUTHN_RETRY_EVENTS``)
        를 읽지 않는다. MCP 도구 호출이 겹쳐 다른 요청이 전역 카운터를 올려도
        이 응답의 속성은 영향받지 않는다.
    """
    global _AUTHN_RETRY_COUNT
    verb_name = method.upper()
    if verb_name not in _SUPPORTED_METHODS:
        raise ValueError(f"지원하지 않는 HTTP 메서드: {method!r}")
    # 런타임 조회 — 시험의 몽키패치(naverc_client.requests.get 등) 가 반영된다.
    verb = getattr(requests, verb_name.lower())
    kwargs["headers"] = header_builder(tk)
    r = verb(url, **kwargs)
    if not _is_authn_expired_response(r):
        # 재시도 없음 → 속성 미부여(속성 부재 = 재시도 안 함).
        return r
    # 401 + GW.AUTHN 이지만 allow_retry=False → 재시도하지 않고 원 응답 반환.
    # 생성 POST 등 되돌리기 어려운 요청은 여기서 멈춘다.
    if not allow_retry:
        return r
    # **주입된 토큰은 자동 갱신하지 않는다 (WO PR #27 8라운드 ②)**.
    # ``allow_retry=True`` 경로에 도달했다는 건 호출자가 재시도를 원했다는 뜻이지만,
    # 호출자가 ``tk=`` 로 외부에서 받은 토큰을 넘겼는데 만료됐다면, ``get_token()``
    # (``load_config()`` 기반) 으로 갱신하면 **다른 판매자 신원**으로 요청이
    # 나간다 — GET 은 엉뚱한 데이터, PUT/DELETE 는 다른 스토어를 건드린다.
    # 따라서 주입 토큰은 출처를 알 수 없어 **재시도 없이** 사유를 붙여 올린다.
    # 내부에서 ``get_token()`` 으로 발급한 토큰만 갱신 대상이다.
    # 설계 참고: "갱신 콜백" 설계도 가능하지만(호출자가 갱신 함수를 넘기면
    # 그것으로 갱신), 현재 호출자 중 **갱신 방법을 아는 경로가 없다** —
    # 외부 토큰을 주입하는 쪽은 우리 설정(``load_config``) 이 아닌 다른
    # 자격증명에서 받았으므로, 우리의 ``get_token`` 은 잘못된 신원이다.
    # 갱신 콜백을 넘길 수 있는 호출자는 애초에 주입 토큰 경로가 아니라
    # ``tk=None`` (내부 발급) 경로를 쓰면 된다. 그러므로 갱신 콜백은
    # 불필요하다 — 주입 토큰엔 갱신 자체가 위험하고, 내부 토큰엔 기존
    # ``get_token`` 경로가 이미 갱신을 담당한다.
    if injected_token:
        raise RuntimeError(
            "주입된 토큰이 만료(401+GW.AUTHN)되었으나, 출처를 알 수 없어 "
            "자동 재발급하지 않는다. 자동 갱신은 ``load_config()`` 기반 "
            "``get_token()`` 으로 새 토큰을 받는데, 이는 다른 자격증명의 "
            "신원으로 요청을 내보낼 위험이 있다 (GET → 엉뚱한 데이터, "
            "PUT/DELETE → 다른 스토어 수정). 토큰을 새로 발급받아 다시 "
            "호출하라."
        )
    # 파일 스트림을 되감을 수 없으면 재시도를 포기한다.
    # 빈 본문(0바이트 이미지) 으로 재시도하는 것을 절대 허용하지 않는다.
    if not _rewind_files_for_retry(kwargs):
        return r
    # 토큰 재발급 1회 + 재시도 1회.
    # 재시도도 같은 위임 함수(verb)를 탄다 — 두 번째 호출도 시험 패치에 잡혀야 한다.
    new_tk = get_token()
    # 작업 단위 토큰 전파: tk_ref 가 있으면 새 토큰을 다시 써서 같은 작업의
    # 뒤따르는 요청들이 새 토큰으로 나가게 한다. 이게 없으면 register_product
    # 같은 다중 POST 작업에서 매번 401 을 다시 맞고 get_token 을 또 부른다.
    if tk_ref is not None:
        tk_ref.value = new_tk
    _AUTHN_RETRY_EVENTS.append(
        {
            "url": url,
            "method": verb_name,
            "retried": True,
            # 비밀값(토큰/시크릿) 은 남기지 않는다 — 사실(재시도함)만 기록.
        }
    )
    # 단조 증가 카운터 — 관측용(진단) 으로 남긴다. 단, **삭제 판정은 이 전역
    # 값을 읽지 않는다** (WO PR #27 9라운드) — MCP 호출이 겹치면 무관한 요청이
    # 카운터를 올려 이 요청이 "재시도됐다"고 거짓으로 읽힌다. 대신 이 응답
    # 객체의 ``_clossify_retried`` 속성으로 호출자에게 **이 요청 한 건** 의
    # 재시도 사실을 직접 전달한다.
    _AUTHN_RETRY_COUNT += 1
    kwargs["headers"] = header_builder(new_tk)
    retried_r = verb(url, **kwargs)
    # 요청 단위 사실: 이 응답 객체에만 "재시도했다"를 표시한다.
    # 전역/공유 상태를 읽는 호출자는 없다.
    retried_r._clossify_retried = True
    return retried_r


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
    injected_tk = tk is not None
    tk = tk or get_token()
    opened_files = []
    files = []
    try:
        for p in paths:
            fh = open(p, "rb")
            opened_files.append(fh)
            files.append(("imageFiles", (os.path.basename(p), fh, _guess_image_mime(p))))
        # 재시도 안전: 이미지 업로드는 중복 피해가 없다 — 같은 이미지를
        # 두 번 올려도 스토리지에 사본이 하나 더 생길 뿐 상품 데이터가
        # 변하지 않는다(반환된 URL 만 달라질 수 있고 호출자가 최종 것을 쓴다).
        r = _api_request(
            "POST",
            BASE + "/external/v1/product-images/upload",
            tk=tk,
            header_builder=lambda t: _h(t, False),
            allow_retry=True,
            injected_token=injected_tk,
            files=files,
            timeout=120,
        )
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


def _post_product_payload(payload, tk, *, tk_ref=None, injected_token=False):
    # **재시도하지 않는다** — 상품 신규 등록(POST /external/v2/products).
    # 401+GW.AUTHN 을 받으면 재시도 없이 (status_code, body) 를 올려보낸다.
    # 이유: 게이트웨이가 인증 단계에서 잘랐다면 원 요청은 백엔드에
    # 도달하지 않았을 것이지만, 우리는 그것을 증명할 수 없다. 증명 못 하는
    # 전제 위에서 중복 상품이 라이브 마켓에 올라가는 위험을 감수할 이유가
    # 없다(인증 문서는 재호출을 권장할 뿐, 원 요청 미처리를 보장하지 않는다).
    # allow_retry 를 명시적으로 False 로 적는다 — 기본값에 의존하지 않고
    # 의도를 소스에서 읽을 수 있게 한다.
    # ``injected_token`` 은 ``allow_retry=False`` 이므로 본 경로에선 의미가
    # 없지만, ``register_product`` 가 외부 토큰을 받은 사실을 소스에 남겨
    # 독자가 신원 흐름을 추적할 수 있게 한다.
    r = _api_request(
        "POST",
        BASE + "/external/v2/products",
        tk=tk,
        header_builder=lambda t: _h(t),
        tk_ref=tk_ref,
        allow_retry=False,
        injected_token=injected_token,
        data=json.dumps(payload).encode("utf-8"),
        timeout=60,
    )
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
    restricted = {
        _normalize_seller_tag_text(term) for term in restricted_terms if str(term or "").strip()
    }
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
                if isinstance(child, dict | list):
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                if isinstance(child, dict | list):
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


# 페이로드 최상위에 붙는 내부 메타 키 목록 — 네이버 API 로 절대 나가면 안 된다.
# 감리 지적: notice_filled_from_config 가 사용자 반환에 없는 대신 네이버 요청
# 최상위 JSON 으로 전송되고 있었다. 형제 키(_kcWarning)도 같은 성격이므로
# 한꺼번에 다룬다 — 하나만 고치고 형제를 남기지 않는다.
_INTERNAL_PAYLOAD_META_KEYS = frozenset(
    {
        "notice_filled_from_config",
        "_kcWarning",
    }
)


def _strip_internal_meta(payload):
    """네이버 API 로 나가면 안 되는 내부 메타 키를 페이로드 최상위에서 제거한다.

    ``build_payload`` 는 내부 보고/사용자 안내용 메타를 페이로드 루트에 싣는다:
      - ``notice_filled_from_config``: 공통 5필드 중 config 에서 채운 항목 보고.
      - ``_kcWarning``: KC 설정 부재 경고.

    이 값들은 사용자에게는 보여야 하지만 **네이버 요청 JSON 에는 없어야 한다.**
    네이버 스키마에 없는 키라 거절될 수 있고, 애초에 내부 메타를 외부로 보내면
    안 된다. 본 함수는 송신(dry-run 덤프 포함) 직전에 이 키들을 제거한다.

    ``payload`` 를 직접 변경하지 않고 얕은 복사를 반환하지 않는다 — 호출자가
    이미 deepcopy 한 working_payload 에 대해 in-place 로 pop 한다.
    """
    if not isinstance(payload, dict):
        return
    for key in _INTERNAL_PAYLOAD_META_KEYS:
        payload.pop(key, None)


def register_product(payload, tk=None):
    """POST /external/v2/products. (origin/channel No 반환)"""
    # 등록 경계 게이트: 페이로드 구성 단계(build_payload)와 분리된 최종 검증.
    # 택배사 코드(deliveryCompany)가 빈 값이면 네이버 API 로 나가는 것을 거부한다.
    # _resolve_delivery_company 는 as_tel/manufacturer/importer 와 동일하게 빈
    # 문자열을 반환하므로, 실제 API 송신 직전에 이 지점에서 fail-closed 한다.
    # "CJGLS" 같은 임의 기본값을 박는 대신 사용자에게 요구하는 것이 정답이다.
    try:
        delivery_company = (
            payload.get("originProduct", {}).get("deliveryInfo", {}).get("deliveryCompany", "")
        )
    except AttributeError:
        delivery_company = ""
    if not isinstance(delivery_company, str) or not delivery_company.strip():
        raise ValueError(
            "config 에 택배사(delivery_company) 설정이 필요합니다: "
            "smartstore_notice_defaults.delivery_company — 판매자가 실제 사용하는 "
            "택배사 코드(네이버 커머스 API enum). 미입력 시 코드가 임의 값을 "
            "지어내지 않고 사용자에게 요구합니다."
        )
    # 디스크의 prepared payload 를 그대로 등록하는 경로의 마지막 방어선.
    # 대표 이미지 URL 이 비어 있으면 페이로드 생성 단계의 게이트가 우회됐을 수 있으므로
    # POST 송신 전에 한 번 더 검증한다 (fail-closed).
    try:
        rep_url = (
            payload.get("originProduct", {})
            .get("images", {})
            .get("representativeImage", {})
            .get("url", "")
        )
    except AttributeError:
        rep_url = ""
    if not isinstance(rep_url, str) or not rep_url.strip():
        raise ValueError(
            "대표 이미지 URL 이 비어 있습니다. 원본 이미지가 최소 1장 필요합니다. "
            "실재하는 상품의 사진 없이는 등록을 진행하지 않습니다."
        )
    working_payload = copy.deepcopy(payload)
    # 내부 메타 키(notice_filled_from_config/_kcWarning)를 송신 페이로드에서 제거.
    # 이 값들은 사용자 반환에만 쓰이고 네이버 API 로 나가면 안 된다(감리 지적).
    # dry-run 덤프 직전에 제거하여 dry-run 파일에도 내부 키가 없다.
    _strip_internal_meta(working_payload)
    meta = {"removed": [], "restricted_terms": [], "attempts": []}
    prefilter_removed = _strip_seller_tags(working_payload, KNOWN_RESTRICTED_SELLER_TAGS)
    if prefilter_removed:
        meta["prefilter_removed"] = prefilter_removed
        _append_unique(meta["removed"], prefilter_removed)
        _append_unique(meta["restricted_terms"], prefilter_removed)

    if os.environ.get("COMMERCE_DRY_RUN") == "1":
        payload_path = common.STATE_DIR / "dry_run_payload.json"
        payload_path.parent.mkdir(parents=True, exist_ok=True)
        with open(payload_path, "w", encoding="utf-8") as f:
            json.dump(working_payload, f, ensure_ascii=False, indent=2)
        origin = working_payload.get("originProduct") if isinstance(working_payload, dict) else {}
        return {
            "ok": True,
            "dry_run": True,
            "originProductNo": None,
            "payload_path": str(payload_path),
            "statusType": origin.get("statusType") if isinstance(origin, dict) else None,
        }

    injected_tk = tk is not None
    tk = tk or get_token()
    # 작업 단위 토큰 홀더 — 한 번의 논리적 등록 작업이 여러 POST 로 이뤄지므로,
    # ``_api_request`` 안에서 401+``GW.AUTHN`` 재발급한 토큰을 같은 작업의
    # 뒤따르는 POST 들이 이어받게 한다. 전역 캐시가 아니다 — 이 함수가
    # 반환하면 홀더도 같이 버린다. 전역 캐시는 만료 관리가 딸려와 범위 밖.
    tk_ref = _TokenRef(tk)
    last_sc, last_body = None, None
    for attempt_no in range(MAX_RESTRICTED_SELLER_TAG_RETRIES + 1):
        sc, body = _post_product_payload(
            working_payload, tk_ref.value, tk_ref=tk_ref, injected_token=injected_tk
        )
        last_sc, last_body = sc, body
        if not _is_restricted_seller_tags_response(sc, body):
            return sc, _attach_seller_tag_autostrip_meta(body, meta)

        terms = _restricted_seller_tag_terms(body)
        removed = _strip_seller_tags(working_payload, terms)
        _append_unique(meta["restricted_terms"], terms)
        _append_unique(meta["removed"], removed)
        meta["attempts"].append(
            {
                "attempt": attempt_no + 1,
                "http": sc,
                "terms": terms,
                "removed": removed,
                "action": "strip_and_retry"
                if attempt_no < MAX_RESTRICTED_SELLER_TAG_RETRIES
                else "clear_all_next",
            }
        )
        if attempt_no >= MAX_RESTRICTED_SELLER_TAG_RETRIES:
            break
        if not terms and not removed:
            break

    cleared = _clear_seller_tags(working_payload)
    if cleared:
        meta["cleared_all"] = True
        _append_unique(meta["removed"], cleared)
        meta["attempts"].append(
            {"attempt": len(meta["attempts"]) + 1, "removed": cleared, "action": "clear_all"}
        )
        sc, body = _post_product_payload(
            working_payload, tk_ref.value, tk_ref=tk_ref, injected_token=injected_tk
        )
        return sc, _attach_seller_tag_autostrip_meta(body, meta)
    return last_sc, _attach_seller_tag_autostrip_meta(last_body, meta)


def seller_tag_autostrip_meta(body):
    if isinstance(body, dict) and isinstance(body.get(SELLER_TAG_AUTOSTRIP_KEY), dict):
        return body.get(SELLER_TAG_AUTOSTRIP_KEY)
    return None


def update_product(channel_no, payload, tk=None):
    """PUT /external/v2/products/channel-products/{channelNo}."""
    injected_tk = tk is not None
    tk = tk or get_token()
    # 재시도 안전: 수정(PUT)은 멱등이다 — 같은 페이로드를 두 번 PUT 해도
    # 최종 상태가 같다(덮어쓰기). 중복 생성이 일어나지 않는다.
    r = _api_request(
        "PUT",
        BASE + f"/external/v2/products/channel-products/{channel_no}",
        tk=tk,
        header_builder=lambda t: _h(t),
        allow_retry=True,
        injected_token=injected_tk,
        data=json.dumps(payload).encode("utf-8"),
        timeout=60,
    )
    return r.status_code, _json_or_text_response(r)


def get_product(origin_no, tk=None):
    injected_tk = tk is not None
    tk = tk or get_token()
    # 재시도 안전: 조회(GET)는 부수효과가 없다 — 두 번 읽어도 데이터가 변하지 않는다.
    r = _api_request(
        "GET",
        BASE + f"/external/v2/products/origin-products/{origin_no}",
        tk=tk,
        header_builder=lambda t: _h(t, False),
        allow_retry=True,
        injected_token=injected_tk,
        timeout=20,
    )
    return r.status_code, (r.json() if r.status_code == 200 else r.text)


def delete_origin_product(origin_product_no, tk=None, *, retried_out=None):
    """DELETE /external/v2/products/origin-products/{originProductNo}.

    2026-08-05 실측 확인: HTTP 200, 본문 ``{"data": true}``. 라이브 스토어에서
    테스트 listing 10건을 이 엔드포인트로 삭제했다.

    인증·타임아웃·반환형(``(status_code, body)``)·에러 처리는 이웃 호출
    (``get_product``/``update_product``) 규약을 그대로 따른다 — 일관성을 해치는
    변형을 만들지 않는다. 본 함수는 순수 API 래퍼다: 단일 대상만 지우고,
    호출자(mcp_server)가 입력 검증·확인·로컬 기록 정리를 담당한다.

    Args:
        origin_product_no: 삭제 대상 원상품 번호.
        tk: 액세스 토큰. ``None`` 이면 ``get_token()`` 으로 발급.
        retried_out: **요청 단위 재시도 사실 전달 (WO PR #27 9라운드)**.
            ``None`` 이 아닌 ``dict`` 를 넘기면, 이 호출 한 건 안에 401+
            ``GW.AUTHN`` 재시도가 일어났을 때 ``retried_out["retried"] = True``
            를 **쓴다**. 재시도가 없으면 이 키를 **쓰지 않는다** (키 부재 =
            재시도 안 함). 호출자는 이 컨테이너에서 **이 요청 한 건** 의 재시도
            사실을 읽는다 — 전역 카운터(``_AUTHN_RETRY_COUNT``) 를 읽으면 MCP
            도구 호출이 겹칠 때 무관한 요청이 카운터를 올려 거짓 양성이 난다.
            기본값 ``None`` 이면 기존 동작(재시도 사실을 호출자에게 넘기지 않음)
            이 유지되므로 **기존 호출부 시그니처가 깨지지 않는다**.
    """
    injected_tk = tk is not None
    tk = tk or get_token()
    # 재시도 안전: 삭제(DELETE)는 멱등이다 — 두 번 삭제해도 최종 상태(삭제됨)가 같다.
    r = _api_request(
        "DELETE",
        BASE + f"/external/v2/products/origin-products/{origin_product_no}",
        tk=tk,
        header_builder=lambda t: _h(t, False),
        allow_retry=True,
        injected_token=injected_tk,
        timeout=20,
    )
    # 요청 단위 재시도 사실을 응답 객체에서 직접 읽어 호출자 컨테이너에 기록.
    # 전역 상태를 읽지 않는다 — 이 응답에만 사실이 묶여 있다.
    if isinstance(retried_out, dict) and getattr(r, "_clossify_retried", False):
        retried_out["retried"] = True
    return r.status_code, _json_or_text_response(r)


def search_products(page: int = 1, size: int = 10, tk=None):
    """기존 등록 상품 목록을 조회한다 (POST /external/v1/products/search).

    본 함수는 첫 대화 온보딩에서 판매자의 **기존 상품에서 스토어 정책값을 읽어
    제안** 하는 경로만을 위해 존재한다. 상품 등록 흐름이나 다른 도구는 이 함수를
    호출하지 않는다.

    Args:
        page: 1-base 페이지 번호. 최근 상품을 우선하려면 size 와 함께 1페이지만.
        size: 페이지당 상품 수. 온보딩 제안에는 소수(기본 10)면 충분하다.
        tk:  이미 발급받은 액세스 토큰. None 이면 새로 발급한다.

    Returns:
        ``(status_code, body)`` — 기존 호출자 규약(``get_product`` 등)과 동일.
        성공 시 body 의 ``contents`` 배열(2026-08-05 실측 녹화 형태) 각 원소는
        최상위 ``originProductNo`` 와 중첩 ``channelProducts`` 배열을 가지며,
        채널 수준값(``channelProductNo``/``name``/``statusType``)은
        ``channelProducts[0]`` 안에 있다. 과거 스키마 호환을 위해 ``products``
        키도 폴백으로 읽는 쪽(check_config)에서 다룬다 — 본 함수는 응답을
        있는 그대로 돌려줄 뿐 키를 해석하지 않는다.
        실패 시 body 는 응답 본문(문자열 또는 dict).

    Note:
        이 함수 자체는 "무엇을 읽을지" 결정하지 않는다. 호출자(check_config 의
        ``read_existing=True`` 경로)가 정책값 추출을 담당한다. 함수는 순수한
        API 호출 래퍼다 — 값을 해석·변환·추정하지 않는다.
    """
    injected_tk = tk is not None
    tk = tk or get_token()
    # 재시도 안전: 상품 검색(POST /search)은 조회다 — 부수효과가 없고 중복 피해가 없다.
    r = _api_request(
        "POST",
        BASE + "/external/v1/products/search",
        tk=tk,
        header_builder=lambda t: _h(t),
        allow_retry=True,
        injected_token=injected_tk,
        data=json.dumps({"page": int(page), "size": int(size)}).encode("utf-8"),
        timeout=20,
    )
    return r.status_code, _json_or_text_response(r)


def fetch_product_inspections(page: int = 1, size: int = 100, tk=None):
    """GET /external/v1/product-inspections/channel-products — 검수(수정요청) 목록.

    네이버가 스토어 상품에 대해 발령한 **수정요청/제재 지적** 목록을 조회한다.
    판매자는 이 목록을 모르면 스토어가 제재 상태로 빠지는 것을 알 수 없다.

    2026-08-10 실측 계약:
      - GET 엔드포인트. 파라미터: ``page`` (1-base), ``size``.
      - 200 응답 본문 형태 (0건):
        ``{"page":1,"size":100,"totalElements":0,"totalPages":0,
           "first":true,"last":true}``
      - ``totalElements>0`` 일 때 항목 배열 키가 본문에 추가로 나타난다.
        ★ 항목 배열 키 이름은 ``totalElements>0`` 일 때만 알 수 있다 —
        응답에서 **방어적으로** 찾는다(``contents``/``content`` 폴백).
        없는 키 이름을 단정하지 마라.
      - 본 함수는 항목 배열 키를 해석하지 않는다. 호출자가 방어적으로 찾는다.

    본 함수는 순수 API 래퍼다 — 이웃 호출(``search_products``/``get_product``)
    규약을 그대로 따른다: ``(status_code, body)`` 반환, ``tk=None`` 이면
    ``get_token()``, 타임아웃·헤더는 ``_h(tk, False)`` (GET). 응답을
    해석·필터·가공하지 않는다.

    Args:
        page: 1-base 페이지 번호. 기본 1.
        size: 페이지당 항목 수. 기본 100(수정요청은 보통 적으니 한 페이지면 충분).
        tk:  이미 발급받은 액세스 토큰. None 이면 새로 발급한다.

    Returns:
        ``(status_code, body)`` — 기존 호출자 규약과 동일. 성공 시 body 는
        ``{"page":..,"size":..,"totalElements":..,"totalPages":..,"first":..,"last":..}``
        (``totalElements>0`` 이면 항목 배열 키 추가). 실패 시 body 는 응답 본문.
    """
    injected_tk = tk is not None
    tk = tk or get_token()
    params: dict = {}
    try:
        p_int = int(page)
        s_int = int(size)
    except (TypeError, ValueError):
        p_int, s_int = 1, 100
    params["page"] = p_int
    params["size"] = s_int
    # 재시도 안전: 검수 목록 조회(GET)는 부수효과가 없다 — 중복 피해가 없다.
    r = _api_request(
        "GET",
        BASE + "/external/v1/product-inspections/channel-products",
        tk=tk,
        header_builder=lambda t: _h(t, False),
        allow_retry=True,
        injected_token=injected_tk,
        params=params,
        timeout=20,
    )
    return r.status_code, _json_or_text_response(r)


def recommend_tags(keyword, tk=None):
    """GET /external/v2/tags/recommend-tags?keyword=... — 네이버 공식 추천 태그.

    2026-08-10 실측 계약:
      - 파라미터: ``keyword=<단일 키워드>``. 없으면 400 "입력정보가 올바르지 않습니다".
      - 200 응답 본문: ``[{"code":877,"text":"니트"}, ...]`` (배열).
      - 각 항목은 ``code``(정수) 와 ``text``(문자열) 을 가진다.

    본 함수는 순수 API 래퍼다 — 이웃 호출(``get_product``/``search_products``)
    규약을 그대로 따른다: ``(status_code, body)`` 반환, ``tk=None`` 이면
    ``get_token()``, 타임아웃·헤더는 ``_h(tk, False)`` (GET). **추천 결과를
    해석·필터·가공하지 않는다** — "추천 목록에 있어도 제한일 수 있다" 는
    함정(``_restricted_tags`` 참조) 은 호출자가 다룬다. 본 함수가 그 함정을
    여는 순간, 추천→제한 검사 파이프라인이 우회될 수 있다.

    Args:
        keyword: 추천을 조회할 키워드(보통 상품명 첫 토큰). 빈 문자열/공백이면
            API 가 400 을 반환한다(호출자가 fail-open 으로 처리).
        tk: 이미 발급받은 액세스 토큰. None 이면 새로 발급한다.

    Returns:
        ``(status_code, body)`` — 기존 호출자 규약과 동일. 성공 시 body 는
        ``[{"code": int, "text": str}, ...]``. 실패 시 body 는 응답 본문.
    """
    injected_tk = tk is not None
    tk = tk or get_token()
    # 재시도 안전: 추천 태그 조회(GET)는 부수효과가 없다 — 중복 피해가 없다.
    r = _api_request(
        "GET",
        BASE + "/external/v2/tags/recommend-tags",
        tk=tk,
        header_builder=lambda t: _h(t, False),
        allow_retry=True,
        injected_token=injected_tk,
        params={"keyword": str(keyword or "")},
        timeout=20,
    )
    return r.status_code, _json_or_text_response(r)


def get_category_attributes(category_id, tk=None):
    """GET /v1/product-attributes/attributes — 카테고리별 상품속성 조회.

    [미실측] 실응답 1콜 확인 전. 확정 문서(A5) 경로와 쿼리 파라미터명은 문서
    스크랩에 있으나 응답 스키마 상세는 확인되지 않았다. 따라서:

    - 쿼리 파라미터명 ``categoryId`` 는 [가정] (문서에 명시된 것이 아니라
      커머스 API 관례에서 유추). 실측 시 수정 필요.
    - 응답 본문은 그대로 반환한다(해석·필터·가공하지 않는다).
    - 카테고리별 속성값 조회 경로는 문서에서 확인 안 되어 **만들지 않는다**
      (정지·보고 — 있는 것만 배선한다).

    본 함수는 순수 API 래퍼다 — 이웃 호출(``get_product``/``search_products``)
    규약을 그대로 따른다: ``(status_code, body)`` 반환, ``tk=None`` 이면
    ``get_token()``, 타임아웃·헤더는 ``_h(tk, False)`` (GET).

    Args:
        category_id: 카테고리 ID (네이버 커머스 API leaf category ID).
        tk: 이미 발급받은 액세스 토큰. None 이면 새로 발급한다.

    Returns:
        ``(status_code, body)`` — 기존 호출자 규약과 동일. 성공 시 body 는
        카테고리에 해당하는 상품속성 목록(응답 스키마 미실측). 실패 시 body 는
        응답 본문.
    """
    injected_tk = tk is not None
    tk = tk or get_token()
    # [가정] 쿼리 파라미터명 categoryId — 문서 실측 전 관례 추정.
    # 재시도 안전: 카테고리 속성 조회(GET)는 부수효과가 없다 — 중복 피해가 없다.
    r = _api_request(
        "GET",
        BASE + "/v1/product-attributes/attributes",
        tk=tk,
        header_builder=lambda t: _h(t, False),
        allow_retry=True,
        injected_token=injected_tk,
        params={"categoryId": str(category_id or "")},
        timeout=20,
    )
    return r.status_code, _json_or_text_response(r)


def restricted_tags(tags, tk=None):
    """GET /external/v2/tags/restricted-tags?tags=... — 태그 제한 여부 조회.

    2026-08-10 실측 계약:
      - 파라미터: ``tags=<쉼표연결>`` (예: ``tags=니트,가디건,니트가디건``).
        **단수 ``tag=`` 파라미터는 400** — 반드시 ``tags=`` 복수형.
      - 200 응답 본문: ``[{"tag":"니트","restricted":true}, ...]`` (배열).
      - 각 항목은 ``tag``(문자열) 와 ``restricted``(bool) 을 가진다.

    실측 해석: 단독 일반명사(니트·가디건) 와 금지어(쩐다) 가 ``restricted:true``,
    복합 태그(니트가디건·우드슬랩) 가 ``restricted:false``.

    ★ 함정 (티켓 계약): **추천 목록에 있어도 제한일 수 있다.** "니트"는
    ``recommend_tags`` code 877 이면서 동시에 ``restricted:true`` 다. 따라서
    추천 결과를 그대로 쓰지 말고 반드시 본 함수로 제한 검사를 통과시켜라.
    본 함수는 판정만 돌려준다 — "어떤 태그를 빼야 하는가" 는 호출자가
    ``restricted==True`` 항목을 걸러내서 정한다(조용한 드롭 금지 — 호출자가
    반환에 사유를 남겨야 한다).

    Args:
        tags: 제한 여부를 조회할 태그 컬렉션(리스트/튜플/쉼표 문자열). 빈 입력은
            API 가 400 을 반환한다(호출자가 fail-open 으로 처리).
        tk: 이미 발급받은 액세스 토큰. None 이면 새로 발급한다.

    Returns:
        ``(status_code, body)`` — 기존 호출자 규약과 동일. 성공 시 body 는
        ``[{"tag": str, "restricted": bool}, ...]``. 실패 시 body 는 응답 본문.
    """
    if isinstance(tags, str):
        joined = tags
    else:
        # 쉼표 연결 — 실측 계약. None/빈 값은 빈 문자열로(400 유도).
        joined = ",".join(str(t or "").strip() for t in tags if str(t or "").strip())
    injected_tk = tk is not None
    tk = tk or get_token()
    # 재시도 안전: 제한 태그 조회(GET)는 부수효과가 없다 — 중복 피해가 없다.
    r = _api_request(
        "GET",
        BASE + "/external/v2/tags/restricted-tags",
        tk=tk,
        header_builder=lambda t: _h(t, False),
        allow_retry=True,
        injected_token=injected_tk,
        params={"tags": joined},
        timeout=20,
    )
    return r.status_code, _json_or_text_response(r)


def _option_stock(option):
    """옵션 재고 수량을 fail-closed 로 파싱.

    이전 버전은 stock 이 누락되거나 파싱 불가능할 때 **가짜 기본값 99** 를
    조용히 반환했다. 이는 실재고가 0 인 것을 99 인 것처럼 등록하는 심각한
    결함을 유발한다. 따라서:

    - stockQuantity 또는 stock 키가 존재하고 유효한 정수로 파싱되면 그 값.
    - 키 자체가 없거나(누락), 값이 None 이거나, 파싱 불가(예: ``"bad"``)면
      ``ValueError`` 를 발생시킨다 (fail-closed).
    """
    raw = option.get("stockQuantity", option.get("stock"))
    if raw is None:
        raise ValueError("option 에 stockQuantity 또는 stock 값이 없습니다 (fail-closed).")
    return int(raw)


def _option_price(option):
    try:
        return int(option.get("price", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _option_group_list(p):
    groups = (
        p.get("option_groups") or p.get("optionGroupNames") or p.get("optionCombinationGroupNames")
    )
    if isinstance(groups, dict):
        return [
            groups.get(f"optionGroupName{i}")
            for i in range(1, 4)
            if groups.get(f"optionGroupName{i}")
        ]
    if isinstance(groups, str):
        return [groups]
    if isinstance(groups, list | tuple):
        return [str(x) for x in groups if x]
    return []


def _option_width(opts):
    width = 0
    for option in opts:
        if not isinstance(option, dict):
            continue
        names = option.get("names")
        if isinstance(names, list | tuple):
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
    # 과거 이 지점은 len(groups) < width 일 때 "옵션1"/"옵션2" 같은
    # 이름을 **조용히 지어 붙였고**, len(groups) > width 일 때 초과분을 조용히
    # 잘라냈다. 둘 다 "판매자가 이름을 줬다고 믿는데 실제로는 다른 이름이
    # 전송된다" 는 조용한 손실/조용한 보충 이다. 이제 count mismatch 는
    # ValueError 로 거부한다 — 호출자(mcp_server) 가 사전에 검증하겠지만,
    # build_payload 를 직접 호출하는 경로까지 방어적으로 막는다(fail-closed).
    if groups and len(groups) != width:
        raise ValueError(
            f"option_groups 개수({len(groups)})가 옵션 축 수({width})와 일치하지 않습니다. "
            f"그룹 이름 {len(groups)}개를 줬는데 옵션 데이터는 {width}축입니다. "
            "조용한 절삭/조용한 보충 금지 — 개수를 맞추거나 옵션 데이터를 점검하세요."
        )
    if not groups:
        # groups 미제공 시 폴백 번호매김은 기존 동작으로 유지한다 — 이 경로는
        # "판매자가 이름을 안 줬다" 이지 "틀린 개수를 줬다" 가 아니므로.
        if width == 1:
            groups = [p.get("option_group", "사이즈")]
        while len(groups) < width:
            groups.append(f"옵션{len(groups) + 1}")
    group_names = {f"optionGroupName{i}": groups[i - 1] for i in range(1, width + 1)}

    combinations = []
    seen = set()
    for option in opts:
        if not isinstance(option, dict):
            continue
        names = option.get("names") if isinstance(option.get("names"), list | tuple) else None
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


def _fill_deferred_notice_fields(notice, deferred_notice_fields):
    """판매자가 미루기로 선택한 고시 필드 중 빈 자리를 표준 문구로 채운다.

    본 함수가 하는 일은 **빈 자리 채우기** 한 가지다:
      - 판매자가 ``deferred_notice_fields`` 로 넘긴 필드명 중,
      - notice 본문에 **값이 없거나(키 부재/None) 공백류/placeholder** 인 자리에
        한해 ``DEFERRED_NOTICE_PLACEHOLDER`` ("상세페이지 참조") 를 채운다.

    하지 않는 일 (명시적 계약):
      - **실값이 있는 필드는 덮어쓰지 않는다.** 판매자가 진짜 값을 준 필드는 그
        값이 그대로 전송된다. 미루기로 이름만 올렸더라도 실값이 우선한다.
      - **원산지 필드는 건드리지 않는다.** 원산지는 법적 선언이므로 미루기를
        허용하지 않는다(``ORIGIN_FIELDS_NOT_DEFERRABLE``). mcp_server 게이트가
        사전에 원산지 미루기 선택을 거부하지만, 본 함수도 방어적으로 한 번 더
        거른다 — 게이트를 우회하는 직접 호출 경로까지 보호하기 위함.
      - **미루기 선택이 없으면 아무 것도 하지 않는다.** ``None``/빈 리스트면
        그대로 반환한다. 조용한 자동 채움은 영구 금지다.

    본 함수는 게이트(``_field_missing_with_deferred``)가 "미루기 필드는 채워진
    것으로 본다" 는 판정과 **전송 사실을 일치시킨다** — 게이트가 빈 자리를
    누락에서 제외하므로, 그 자리에 실제로 값이 가야 허위 신고가 되지 않는다.

    Args:
        notice: ``_product_info_notice`` 반환값 (dict). ``productInfoProvidedNoticeType``
            키와 노드 키(예: ``etc``/``wear``)를 가진다. 본문은 노드 키 아래 dict.
        deferred_notice_fields: 판매자가 선택한 미루기 필드명 컬렉션.

    Returns:
        ``notice`` 자체(in-place 변경). 호출자가 같은 객체를 계속 쓴다.
    """
    from . import qa_agents

    if not isinstance(notice, dict):
        return notice
    # 원산지 필드는 미루기에서 제외(방어적 2차 필터).
    deferred = qa_agents._reject_origin_deferred(deferred_notice_fields)
    if not deferred:
        return notice
    # notice 본문 노드를 찾는다: 타입에서 node 를 조회하고, 없으면 etc/furniture 폴백.
    notice_type = str(notice.get("productInfoProvidedNoticeType") or "ETC").strip().upper()
    spec = _notice_type_spec(notice_type)
    node_key = (spec or {}).get("node") or "etc"
    body = notice.get(node_key)
    if not isinstance(body, dict):
        for fallback in ("etc", "furniture"):
            fb = notice.get(fallback)
            if isinstance(fb, dict):
                body = fb
                node_key = fallback
                break
    if not isinstance(body, dict):
        return notice
    for field in deferred:
        # boolean/date 타입 필드는 미루기 불가 — 방어적으로 건너뛴다.
        # _partition_deferred_by_allowlist 에서 이미 걸러졌지만, build_payload 를
        # 직접 호출하는 경로까지 보호하기 위해 여기서도 한 번 더 확인한다.
        if not qa_agents._is_field_deferrable(field):
            continue
        raw = body.get(field)
        # 빈 값/placeholder 인 자리만 채운다. 실값이 있으면 건드리지 않는다.
        # 필드별 값 분기: 고시 35종 공통 5필드는 "1", 그 외는 "상세페이지 참조".
        # 분기 단일 지점은 qa_agents._deferred_value_for_field 이다.
        if qa_agents._is_placeholder_value(raw):
            body[field] = qa_agents._deferred_value_for_field(field)
    return notice


# 상품속성(productAttributes) 허용 키 — A5 문서 확정.
# 이 4개 키 외의 입력은 ValueError 로 거부한다(조용한 무시 금지).
_ATTRIBUTE_ALLOWED_KEYS = frozenset(
    {
        "attributeSeq",
        "attributeValueSeq",
        "attributeRealValue",
        "attributeRealValueUnitCode",
    }
)


def _validate_product_attributes(raw):
    """명시적 상품속성 입력의 **형태만** 검증한다 (값 창작·추론 금지).

    A5 문서 확정 계약:
      - ``attributeSeq``(속성ID) · ``attributeValueSeq``(속성값ID) — 정수.
      - ``attributeRealValue``(범위형 실값) · ``attributeRealValueUnitCode``(단위) —
        문자열, **선택** (범위형 속성에만 해당).
      - 허용 키 4종 외의 키가 있으면 ValueError.
      - ``attributeSeq``/``attributeValueSeq`` 이 정수가 아니면(또는 누락되면)
        ValueError. 정수로 변환 가능한 문자열도 거부한다 — ID 는 처음부터
        정수로 와야 한다(조용한 변환 금지).
      - 빈 리스트 → None 을 반환하여 호출자가 키 자체를 넣지 않게 한다.
      - ``None``/누락 → None 반환 (키 부재 = 미전송).

    본 함수는 **값을 만들거나 추론하지 않는다.** 입력이 있으면 그 형태가
    올바른지만 확인하고, 올바르면 그대로 돌려준다. 입력이 없으면 None 을
    반환하여 호출자가 ``detailAttribute.productAttributes`` 키를 아예 넣지
    않게 한다(빈 배열 전송 금지 — 미실측 거동).

    Returns:
        검증을 통과한 속성 리스트(그대로), 또는 ``None`` (입력 없음/빈 리스트).

    Raises:
        ValueError: 형태 위반(문자 ID·모르는 키·필수 키 누락·리스트 아님).
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError(
            "attributes 입력은 리스트여야 합니다. "
            f"받은 타입: {type(raw).__name__} (형태 위반 — 조용한 무시 없이 거부)."
        )
    if not raw:
        # 빈 리스트 → 키 자체를 넣지 않는다(빈 배열 전송 금지).
        return None
    validated: list[dict] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(
                f"attributes[{idx}] 가 dict 가 아닙니다 (got {type(item).__name__}). "
                "각 속성은 {attributeSeq, attributeValueSeq, ...} dict 여야 합니다."
            )
        # 허용 키 외의 키가 있는지 검사.
        unknown = set(item.keys()) - _ATTRIBUTE_ALLOWED_KEYS
        if unknown:
            raise ValueError(
                f"attributes[{idx}] 에 허용되지 않은 키가 있습니다: "
                f"{sorted(unknown)}. 허용 키: {sorted(_ATTRIBUTE_ALLOWED_KEYS)}. "
                "형태 위반 — 조용한 무시 없이 거부."
            )
        # 필수 키: attributeSeq, attributeValueSeq (정수).
        for required in ("attributeSeq", "attributeValueSeq"):
            if required not in item:
                raise ValueError(
                    f"attributes[{idx}] 에 필수 키 '{required}' 가 없습니다. "
                    "attributeSeq(속성ID) 와 attributeValueSeq(속성값ID) 모두 필요."
                )
            val = item[required]
            # bool 은 int 의 서브클래스이므로 명시적으로 거부.
            if isinstance(val, bool) or not isinstance(val, int):
                raise ValueError(
                    f"attributes[{idx}].{required} 가 정수가 아닙니다 "
                    f"(got {val!r}, 타입 {type(val).__name__}). "
                    "속성 ID 는 정수여야 한다 — 문자열 ID 를 조용히 변환하지 않는다."
                )
        entry: dict = {
            "attributeSeq": item["attributeSeq"],
            "attributeValueSeq": item["attributeValueSeq"],
        }
        # 선택 키: 범위형 속성에만 해당.
        if "attributeRealValue" in item:
            entry["attributeRealValue"] = item["attributeRealValue"]
        if "attributeRealValueUnitCode" in item:
            entry["attributeRealValueUnitCode"] = item["attributeRealValueUnitCode"]
        validated.append(entry)
    return validated


def build_payload(p, detail_html, images, status="SALE", deferred_notice_fields=None):
    """상품 dict(p) + 상세HTML + 이미지URL들 → 등록 payload.
    p keys: name, categoryId, salePrice, options[{name,stock}], tags[], notice{...}, as_tel, as_guide, origin_code, display

    ``deferred_notice_fields`` 는 판매자가 명시적으로 "상세페이지 참조" 로 미루기로
    선택한 고시 필드명 리스트다. 빈 자리에 한해 표준 문구(``DEFERRED_NOTICE_PLACEHOLDER``)
    를 채워 전송한다 — 실값이 있는 필드는 덮어쓰지 않는다. 원산지 필드는 미루기에서
    제외된다. ``None``/빈 리스트면 아무 것도 채우지 않는다(기존 동작).

    ``p.attributes`` (선택): 명시적 상품속성 ID 리스트. 형태는
    ``[{"attributeSeq": int, "attributeValueSeq": int,
       "attributeRealValue"?: str, "attributeRealValueUnitCode"?: str}, ...]``.
    있으면 ``originProduct.detailAttribute.productAttributes`` 로 싣는다(키 4종만).
    형태 위반(문자 ID·모르는 키)은 ValueError 로 거부한다(fail-closed).
    없거나 빈 리스트면 키 자체를 넣지 않는다(빈 배열 전송 금지).
    값을 만들거나 추론하지 않는다 — ID 참조 원칙.
    """
    if status not in {"SALE", "SUSPENSION"}:
        raise ValueError("status must be one of {'SALE', 'SUSPENSION'}")
    # 진입 게이트: 유효 원본 이미지 1장 이상을 본 함수에서 가장 먼저 강제한다.
    # images[0] 접근보다 먼저여야 빈 리스트/무효 항목이 대표 이미지로 승격되는
    # 것을 막는다 (IndexError 로 터지는 대신 명확한 ValueError 로 거부).
    _require_original_images(images)
    opts = p.get("options", [])
    option_info = _build_option_info(p, opts)
    defaults = _notice_defaults(p)
    notice = _product_info_notice(p, defaults)
    # 판매자가 명시적으로 미루기로 선택한 고시 필드의 빈 자리를 표준 문구로 채운다.
    # 실값이 있는 필드는 건드리지 않으며, 원산지 필드는 미루기에서 제외된다.
    _fill_deferred_notice_fields(notice, deferred_notice_fields)
    # 실측 확인: status="SUSPENSION" 일 때 channelProductDisplayStatusType
    # 으로 "OFF" 를 보내면 네이버 API 가 NotValidEnum 으로 거절한다. 살아있는
    # API 로 등록 성공이 확인된 값은 "SUSPENSION" 이다. status="SALE" 일 때의
    # "ON" 도 등록 성공이 확인된 값이므로 그대로 둔다.
    display_default = "SUSPENSION" if status == "SUSPENSION" else "ON"
    # 상품명 정책 컷: 네이버 커머스 API 는 50자 초과 시 400 거절.
    # mcp_server.register_product 도 사전에 자르지만, build_payload 를
    # 직접 호출하는 경로까지 보호하기 위해 여기서도 컷한다.
    product_name = str(p["name"])[:MAX_PRODUCT_NAME_LEN]

    # originAreaInfo 는 필수지만 importer 는 값이 있을 때만 넣는다
    # (빈 문자열을 보내면 API 가 400 을 반환할 수 있으나, 우리가 임의 수입사를
    # 지어내는 것보다 안전). KC 신고값도 config 에서만 받는다.
    origin_area_info = {
        "originAreaCode": defaults["origin_area_code"],
        "content": defaults["origin_content"],
    }
    if defaults.get("importer"):
        origin_area_info["importer"] = defaults["importer"]

    # KC 신고값 하드코딩(kcCertifiedProductExclusionYn="KC_EXEMPTION_OBJECT",
    # kcExemptionType="OVERSEAS") 제거. config 의 kc_declaration 블록이 있으면
    # 그 값을 그대로 싣고, 없으면 certificationTargetExcludeContent 필드 자체를
    # 생략한다(네이버가 요구하면 API 가 에러로 알려준다). 호출자가 알 수 있도록
    # KC 부재 경고를 페이로드 메타에 포함한다.
    kc_block, kc_warning = _kc_config()

    detail_attribute = {
        "afterServiceInfo": {
            "afterServiceTelephoneNumber": defaults["as_tel"],
            "afterServiceGuideContent": defaults["as_guide"],
        },
        "originAreaInfo": origin_area_info,
        "minorPurchasable": True,
        "taxType": "TAX",
        "seoInfo": {"sellerTags": [{"text": t} for t in p.get("tags", [])]},
        "productInfoProvidedNotice": notice,
        "optionInfo": option_info,
    }
    if kc_block:
        detail_attribute["certificationTargetExcludeContent"] = kc_block
    # 상품속성: 명시적 ID 입력이 있을 때만 productAttributes 로 싣는다.
    # 형태 위반은 _validate_product_attributes 가 ValueError 로 거부(fail-closed).
    # 입력이 없거나 빈 리스트면 키 자체를 넣지 않는다(빈 배열 전송 금지).
    validated_attributes = _validate_product_attributes(p.get("attributes"))
    if validated_attributes is not None:
        detail_attribute["productAttributes"] = validated_attributes

    payload = {
        "originProduct": {
            "statusType": status,
            "saleType": "NEW",
            "leafCategoryId": p["categoryId"],
            "name": product_name,
            "detailContent": detail_html,
            "images": {
                "representativeImage": {"url": images[0]},
                "optionalImages": [{"url": u} for u in images[1:]],
            },
            "salePrice": int(p["salePrice"]),
            "stockQuantity": sum(_option_stock(o) for o in opts)
            if opts
            else int(p.get("stock", 1)),
            "deliveryInfo": {
                "deliveryType": "DELIVERY",
                "deliveryAttributeType": "NORMAL",
                # 택배사 코드 — defaults["delivery_company"] 에서만 받는다.
                # _resolve_delivery_company 가 상품 입력 → config 순으로 찾고,
                # 둘 다 없으면 빈 문자열 (register_product 경계에서 fail-closed).
                # "CJGLS" 기본값 제거.
                "deliveryCompany": defaults["delivery_company"],
                "deliveryBundleGroupUsable": False,
                "deliveryFee": {
                    "deliveryFeeType": "PAID",
                    "baseFee": int(p.get("delivery_fee", 3000)),
                    "deliveryFeePayType": "PREPAID",
                },
                "claimDeliveryInfo": {
                    "returnDeliveryFee": defaults["return_delivery_fee"],
                    "exchangeDeliveryFee": defaults["exchange_delivery_fee"],
                },
            },
            "detailAttribute": detail_attribute,
        },
        "smartstoreChannelProduct": {
            "naverShoppingRegistration": True,
            "channelProductDisplayStatusType": p.get("display", display_default),
        },
    }
    # KC 설정 부재 경고를 페이로드 메타에 포함(조용한 생략 금지).
    if kc_warning:
        payload["_kcWarning"] = kc_warning
    # 공통 5필드 중 config 에서 채워진 항목이 있으면 페이로드 메타에 알린다.
    # 빈 목록(아무것도 config 에서 채워지지 않았을 때)은 메타 키 자체를 싣지
    # 않는다 — 비어있지 않은데 표시되지 않는 경로가 있으면 안 된다.
    filled_from_config = defaults.get("notice_filled_from_config") or []
    if filled_from_config:
        payload["notice_filled_from_config"] = list(filled_from_config)
    return payload
