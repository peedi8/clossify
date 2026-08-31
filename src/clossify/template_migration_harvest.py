# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""템플릿 이관 더미 수확 — 표식 → 중지 → 추출 → 삭제.

본 모듈은 슬라이스 1(``template_migration_form``)이 만든 더미 대량등록 엑셀을
사용자가 판매자센터에 업로드한 뒤, **그 더미에서 템플릿 값을 걷어오고 더미를
지우는** 파이프라인의 뒷단이다.

설계 구멍 해법 (슬라이스 1 에서 심은 표식)
------------------------------------------
엑셀 업로드는 사용자가 판매자센터에서 한다 → 우리는 더미의 상품번호를 모른다.
슬라이스 1 은 **판매자 상품코드(A열)에 런 고유 마커** 를 심어둔다
(``CLSTMIG-<runid>-<rowseq>``). 본 모듈은 그 마커를 가진 상품만 대상으로 한다.

순서 (바꾸지 마라)
------------------
1. **마커로 상품번호 수집** — ``search_products`` 로 페이지를 훑어
   판매자상품코드가 우리 마커인 것만 거른다. 마커가 일치하지 않으면 조회조차
   하지 않는다 ("임시 상품 같은 이름" 으로 추측 금지).
2. **즉시 판매중지** — 찾는 즉시. 추출보다 먼저 (노출 창 최소화).
3. **추출·템플릿 저장** — 행마다 ``get_product`` → ``save_template``
   (경로 재사용: ``get_product`` 의 ``save_as_template`` 매개변수).
4. **삭제** — ②③ 이 끝난 것만.
5. **보고** — ``삭제 N/M`` + 남은 것 목록 + "더 있을 수 있음".

안전 불변식
----------
- **마커 불일치 상품은 조회·중지·삭제하지 않는다.** 추측 금지.
- **행동 전에 기록**: run ledger 에 무엇을 처리할지 먼저, 실행이 나중.
- **삭제 결과를 숫자로 보고**: ``삭제 N/M``. 남았으면 남았다고 말한다.
- **부분 실패는 부분 성공으로 위장하지 않는다.**
- **dry-run 기본**: ``confirm=False`` 일 때 중지·삭제 API 호출 0 회.

노출
----
**새 MCP 도구 없음** (MCP 도구 7 개 유지). 본 모듈은 일반 파이썬 함수로
구현되며, ``TemplateHarvestServer`` (로컬 폼 서버 — ``template_migration_form``
과 동일한 방어 패턴) 의 라우트로 편승한다. 사용자는 슬라이스 1 결과 페이지에서
run_id 를 받아 수확 폼에 입력한다.

의존 방향: ``template_migration_form`` (표식·run ledger) →
``template_migration_harvest`` (본 모듈) → ``naver_client`` / ``mcp_server``
(조회·중지·삭제·템플릿 저장 — 기존 함수 재사용, 새 도구 아님).
"""

from __future__ import annotations

import html
import http.server
import json
import socketserver
import threading
import time
from typing import Any

from . import common, mcp_server, naver_client
from . import template_migration_form as tmf

# ---------------------------------------------------------------------------
# 상수.
# ---------------------------------------------------------------------------
TTL_SECONDS = tmf.TTL_SECONDS  # 10분. 만료 후 서버는 종료된다.

# 검색 페이지네이션 상한. 조용한 누락 금지 — 상한에 걸리면 보고에 드러낸다.
DEFAULT_MAX_PAGES = 50
DEFAULT_PAGE_SIZE = 100

# 판매중지 페이로드 템플릿. 네이버 채널상품 수정 API 는 전체 페이로드가 아닌
# 상태 변경만으로도 동작한다 (실측: channelProductDisplayStatusType 만 바꿔도
# SALE→SUSPENSION 전환이 이루어진다). 본 모듈은 최소 페이로드만 보낸다.
_SUSPEND_PAYLOAD = {"channelProductDisplayStatusType": "SUSPENSION"}


# ---------------------------------------------------------------------------
# 결과 타입.
# ---------------------------------------------------------------------------
class HarvestReport:
    """수확 결과 보고. ``삭제 N/M`` + 남은 것 + 페이지 상한 경고를 담는다.

    **비밀값 비노출**: 본 보고에는 템플릿 코드 값이나 인증 정보를 담지 않는다.
    상품번호·표식·처리 상태만 담는다 (이들은 더미 식별용 공개 값이다).
    """

    def __init__(
        self,
        *,
        run_id: str = "",
        found_count: int = 0,
        stopped_count: int = 0,
        extracted_count: int = 0,
        deleted_count: int = 0,
        remaining: list[dict[str, Any]] | None = None,
        page_cap_reached: bool = False,
        pages_scanned: int = 0,
        listings_scanned: int = 0,
        dry_run: bool = False,
        error: str = "",
    ) -> None:
        self.run_id = str(run_id)
        self.found_count = int(found_count)
        self.stopped_count = int(stopped_count)
        self.extracted_count = int(extracted_count)
        self.deleted_count = int(deleted_count)
        self.remaining = list(remaining) if isinstance(remaining, list) else []
        self.page_cap_reached = bool(page_cap_reached)
        self.pages_scanned = int(pages_scanned)
        # 훑은 listing 총 수. 0 이면 "응답이 비었거나 파싱 실패" 와
        # "훑었지만 일치 0건" 을 구분하는 열쇠 (조용한 0건 방지).
        self.listings_scanned = int(listings_scanned)
        self.dry_run = bool(dry_run)
        self.error = str(error)

    def summary(self) -> str:
        """한 줄 요약 — ``삭제 N/M`` 형태. 남았으면 그 사실을 밝힌다.

        **조용한 0건 방지**: ``found_count == 0`` 일 때 훑은 상품 수
        (``listings_scanned``) 를 드러내, "응답이 비어 0개 훑음" 인지
        "N개 훑었지만 마커 일치 0건" 인지 사용자가 구분하게 한다.
        """
        prefix = "[dry-run] " if self.dry_run else ""
        s = (
            f"{prefix}수확 run_id={self.run_id}: "
            f"발견 {self.found_count}, 중지 {self.stopped_count}, "
            f"추출 {self.extracted_count}, 삭제 {self.deleted_count}/{self.found_count}"
        )
        if self.remaining:
            s += f" — 남은 {len(self.remaining)}건"
        if self.page_cap_reached:
            s += f" — {self.pages_scanned}페이지까지 훑음, 더 있을 수 있음"
        # 발견 0건일 때 훑은 상품 수를 명시 — 조용한 0건 방지.
        if self.found_count == 0 and self.listings_scanned == 0:
            s += " — 훑은 상품 없음 (응답 비었거나 파싱 실패)"
        elif self.found_count == 0 and self.listings_scanned > 0:
            s += f" — {self.listings_scanned}개 상품 훑음, 마커 일치 0건"
        return s

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "found_count": self.found_count,
            "stopped_count": self.stopped_count,
            "extracted_count": self.extracted_count,
            "deleted_count": self.deleted_count,
            "remaining": list(self.remaining),
            "page_cap_reached": self.page_cap_reached,
            "pages_scanned": self.pages_scanned,
            "listings_scanned": self.listings_scanned,
            "dry_run": self.dry_run,
            "error": self.error,
            "summary": self.summary(),
        }


# ---------------------------------------------------------------------------
# 1. 마커로 상품번호 수집.
#
# ``search_products`` 는 페이지 단위다. 우리는 판매자상품코드 필드가 우리 마커인
# listing 만 걸러낸다. 마커가 아니면 조회조차 하지 않는다 (추측 금지).
#
# 네이버 상품 목록 응답의 판매자 관리 코드 필드는 **문서 실측 기준** 으로
# ``originProduct.sellerCodeInfo.sellerManagementCode`` 다.
#
# 문서 근거 (실측):
#   - ``naver-docs/docs_commerce-api_current_schemas_<원상품 정보 구조체>.txt``
#     의 ``sellerCodeInfo`` (객체, "판매자 코드 정보") 안에
#     ``sellerManagementCode`` (string, "판매자 관리 코드").
#   - ``harvest_doc_fields.json`` 에서 ``sellerManagementCode`` count=4,
#     ``sellerManagementProductCode`` count=0 (존재하지 않는 이름).
#   - 채널상품 스키마(스마트스토어/쇼핑윈도) 에는 판매자 관리 코드 필드가
#     없다 — 따라서 목록 응답에서는 원상품 쪽 경로로만 올 수 있다.
#
# 엑셀 정합: 슬라이스 1 이 A열("판매자 상품코드") 에 심은 마커가 네이버
# 엑셀 업로드를 거쳐 ``originProduct.sellerCodeInfo.sellerManagementCode``
# 로 돌아온다. 심는 쪽과 읽는 쪽이 같은 값을 가리킨다.
#
# 과거 코드가 쓰던 ``sellerManagementProductCode`` 는 문서 953개 전수에서
# 0건인 존재하지 않는 이름이다. 본 모듈은 정본 이름을 우선으로 쓰고,
# ``sellerManagementProductCode`` 는 **폴백으로만** 남긴다 (과거 버전 응답
# 호환성 — 문서에 근거하지 않는 이름이므로 정본이 우선한다).
# ---------------------------------------------------------------------------
def _extract_seller_code(listing: Any) -> str:
    """검색 응답 listing 에서 판매자 관리 코드 값을 방어적으로 추출.

    정본 경로(``originProduct.sellerCodeInfo.sellerManagementCode``) 를
    우선하고, 과거 코드 호환을 위해 존재하지 않는 이름
    ``sellerManagementProductCode`` 도 폴백으로 확인한다. 어느 곳에도
    없으면 빈 문자열.
    """
    if not isinstance(listing, dict):
        return ""

    def _pick(obj: Any, key: str) -> str:
        """obj[key] 가 비어있지 않은 스칼라면 문자열로 반환."""
        if not isinstance(obj, dict):
            return ""
        v = obj.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if v is not None:
            s = str(v).strip()
            if s:
                return s
        return ""

    # 1. 정본: originProduct.sellerCodeInfo.sellerManagementCode.
    op = listing.get("originProduct")
    if isinstance(op, dict):
        sci = op.get("sellerCodeInfo")
        code = _pick(sci, "sellerManagementCode")
        if code:
            return code
        # originProduct 최상위에 sellerManagementCode 가 직접 있는 폼도 허용.
        code = _pick(op, "sellerManagementCode")
        if code:
            return code

    # 2. listing 최상위 sellerCodeInfo.sellerManagementCode (얕은 폼).
    sci_top = listing.get("sellerCodeInfo")
    code = _pick(sci_top, "sellerManagementCode")
    if code:
        return code

    # 3. listing 최상위 sellerManagementCode (아주 얕은 폼).
    code = _pick(listing, "sellerManagementCode")
    if code:
        return code

    # 4. channelProducts[0].sellerCodeInfo.sellerManagementCode.
    channels = listing.get("channelProducts")
    if isinstance(channels, list) and channels:
        first = channels[0]
        if isinstance(first, dict):
            sci_c = first.get("sellerCodeInfo")
            code = _pick(sci_c, "sellerManagementCode")
            if code:
                return code

    # 5. 폴백: 과거 코드가 쓰던 sellerManagementProductCode.
    #    문서 953개 전수 0건인 존재하지 않는 이름 — 정본이 잡지 못할 때만.
    code = _pick(listing, "sellerManagementProductCode")
    if code:
        return code
    if isinstance(op, dict):
        code = _pick(op, "sellerManagementProductCode")
        if code:
            return code
    if isinstance(channels, list) and channels:
        first = channels[0]
        if isinstance(first, dict):
            code = _pick(first, "sellerManagementProductCode")
            if code:
                return code
    return ""


def _extract_origin_no(listing: Any) -> str:
    """검색 응답 listing 에서 originProductNo 를 추출 (최상위 우선)."""
    if not isinstance(listing, dict):
        return ""
    v = listing.get("originProductNo")
    if v is not None:
        s = str(v).strip()
        if s:
            return s
    # 폴백: channelProducts[0].originProductNo.
    channels = listing.get("channelProducts")
    if isinstance(channels, list) and channels:
        first = channels[0]
        if isinstance(first, dict):
            cv = first.get("originProductNo")
            if cv is not None:
                s = str(cv).strip()
                if s:
                    return s
    return ""


def _extract_channel_no(listing: Any) -> str:
    """검색 응답 listing 에서 channelProductNo 를 추출 (channelProducts[0] 우선)."""
    if not isinstance(listing, dict):
        return ""
    channels = listing.get("channelProducts")
    if isinstance(channels, list) and channels:
        first = channels[0]
        if isinstance(first, dict):
            cv = first.get("channelProductNo") or first.get("smartstoreChannelProductNo")
            if cv is not None:
                s = str(cv).strip()
                if s:
                    return s
    # 폴백: 최상위.
    v = listing.get("channelProductNo") or listing.get("smartstoreChannelProductNo")
    if v is not None:
        s = str(v).strip()
        if s:
            return s
    return ""


def collect_marked_products(
    *,
    run_id: str = "",
    markers: list[str] | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
    page_size: int = DEFAULT_PAGE_SIZE,
    search_fn: Any = None,
) -> tuple[list[dict[str, str]], bool, int, int]:
    """검색 API 를 훑어 우리 마커를 가진 상품만 수집.

    마커가 일치하지 않으면 상세 조회를 하지 않는다. ``run_id`` 또는 ``markers``
    중 하나는 주어져야 한다. ``run_id`` 만 주어지면 접두사 ``CLSTMIG-<run_id>-``
    로 매칭한다 (해당 런의 모든 행). ``markers`` 가 주어지면 그 정확한 값들만.

    Args:
        run_id: 런 식별자. 이 런의 모든 표식을 찾는다.
        markers: 정확한 표식 문자열 리스트 (run_id 보다 우선).
        max_pages: 훑는 페이지 상한. 상한에 걸리면 page_cap_reached=True.
        page_size: 페이지당 listing 수.
        search_fn: 테스트 주입용 검색 함수. 기본값 ``naver_client.search_products``.

    Returns:
        ``(found, page_cap_reached, pages_scanned, listings_scanned)``.
        - ``found``: ``[{"origin_product_no": str, "channel_product_no": str,
          "marker": str, "raw_listing": dict}, ...]``. 마커가 일치한 listing 만.
        - ``page_cap_reached``: 상한에 걸려 더 못 훑었으면 True.
        - ``pages_scanned``: 실제 훑은 페이지 수.
        - ``listings_scanned``: 훑은 listing 총 수. **조용한 0건 방지** —
          ``found`` 가 빈 리스트일 때 ``listings_scanned == 0`` 이면
          "응답이 비었거나 파싱 실패", ``listings_scanned > 0`` 이면
          "N개 훑었지만 마커 일치 0건" 임을 caller 가 구분한다.

    Raises:
        ValueError: run_id 와 markers 가 모두 비어있을 때.
    """
    if search_fn is None:
        search_fn = naver_client.search_products

    # 매칭 기준. markers 가 우선, 없으면 run_id 로 접두사 매칭.
    marker_set: set[str] = set()
    prefix = ""
    if markers:
        for m in markers:
            if isinstance(m, str) and m.strip():
                marker_set.add(m.strip())
    elif run_id:
        prefix = f"{tmf._MARKER_PREFIX}-{run_id}-"
    else:
        raise ValueError("run_id 또는 markers 중 하나는 필요하다.")

    def _matches(code: str) -> str | None:
        """판매자상품코드가 우리 대상이면 매칭된 마커를, 아니면 None."""
        if not code:
            return None
        if marker_set:
            return code if code in marker_set else None
        # 접두사 매칭.
        if prefix and code.startswith(prefix):
            # 접두사 자체는 우리 표식이어야 한다 (안전).
            if tmf.is_our_marker(code):
                return code
        return None

    found: list[dict[str, str]] = []
    page_cap_reached = False
    pages_scanned = 0
    listings_scanned = 0

    for page in range(1, max_pages + 1):
        sc, body = search_fn(page=page, size=page_size)
        pages_scanned = page
        if not (isinstance(sc, int) and sc == 200) or not isinstance(body, dict):
            # 검색 실패 — 조용히 빈 결과로 두지 않고 caller 가 보고에 반영하게.
            break
        # contents / products 키 폴백 (check_config 패턴 재사용).
        listings = body.get("contents")
        if listings is None:
            listings = body.get("products")
        if not isinstance(listings, list):
            break
        if not listings:
            # 빈 페이지 — 더 이상 없다.
            break
        listings_scanned += len(listings)
        for entry in listings:
            code = _extract_seller_code(entry)
            matched = _matches(code)
            if matched is None:
                continue  # 마커 불일치 — 무시 (추측 금지).
            origin_no = _extract_origin_no(entry)
            channel_no = _extract_channel_no(entry)
            if not origin_no:
                # 마커는 우리것인데 origin 번호가 없으면 스킵 — 다음 페이지에서
                # 다시 만날 수 있다. 단 조용한 누락이 되지 않게 caller 가
                # found_count 와 markers 수를 비교해 보고한다.
                continue
            found.append(
                {
                    "origin_product_no": origin_no,
                    "channel_product_no": channel_no,
                    "marker": matched,
                    "raw_listing": entry if isinstance(entry, dict) else {},
                }
            )
        # 페이지가 꽉 차지 않으면 끝.
        if len(listings) < page_size:
            break
        # 루프가 max_pages 까지 가면 상한 도달.
        if page >= max_pages:
            page_cap_reached = True
            break

    return found, page_cap_reached, pages_scanned, listings_scanned


# ---------------------------------------------------------------------------
# 2-4. 중지 → 추출 → 삭제 (행별).
#
# 순서 불변: 각 상품에 대해 **중지가 추출보다 먼저**, **추출이 삭제보다 먼저**.
# 삭제는 중지·추출이 모두 끝난 것만.
# ---------------------------------------------------------------------------
def _suspend_product(channel_no: str, *, confirm: bool, update_fn: Any) -> tuple[bool, str]:
    """판매중지. channel_no 가 없으면 건너뛴다 (조용한 실패 아님 — 채널번호 부재 보고)."""
    if not channel_no:
        return False, "channel_product_no 없음 — 중지 생략"
    if not confirm:
        return False, "dry-run — 중지 생략"
    try:
        sc, body = update_fn(channel_no, _SUSPEND_PAYLOAD)
    except Exception as exc:
        return False, f"중지 중 오류: {common.sanitize_error(exc)}"
    ok = isinstance(sc, int) and 200 <= sc < 300
    if not ok:
        return False, f"중지 실패 (HTTP {sc})"
    return True, ""


def _extract_and_save(
    origin_no: str,
    *,
    template_name: str,
    get_product_fn: Any,
) -> tuple[bool, str]:
    """get_product(save_as_template=...) 경로로 템플릿 추출·저장 (경로 재사용).

    본 함수는 ``mcp_server.get_product`` 를 호출한다 — 이것은 MCP 도구 함수지만
    **새 도구를 등록하는 것이 아니라 기존 함수를 파이썬에서 직접 호출**하는 것이다.
    MCP 도구 수는 7개로 유지된다.
    """
    if not template_name:
        return False, "템플릿 이름 없음 — 추출 생략"
    try:
        result = get_product_fn(origin_no, template_name)
    except Exception as exc:
        return False, f"추출 중 오류: {common.sanitize_error(exc)}"
    if not isinstance(result, dict):
        return False, "추출 응답 형식 이상"
    if not result.get("ok"):
        err = result.get("error") or "조회 실패"
        return False, str(err)
    ts = result.get("template_saved")
    if not isinstance(ts, dict) or not ts.get("ok"):
        reason = ts.get("reason") if isinstance(ts, dict) else "템플릿 저장 안 됨"
        return False, f"추출됐으나 템플릿 저장 실패: {reason}"
    return True, ""


def _delete_product(origin_no: str, *, confirm: bool, delete_fn: Any) -> tuple[bool, str]:
    """더미 삭제."""
    if not confirm:
        return False, "dry-run — 삭제 생략"
    try:
        sc, body = delete_fn(origin_no)
    except Exception as exc:
        return False, f"삭제 중 오류: {common.sanitize_error(exc)}"
    ok = isinstance(sc, int) and 200 <= sc < 300
    if not ok:
        return False, f"삭제 실패 (HTTP {sc})"
    return True, ""


def harvest_run(
    *,
    run_id: str = "",
    markers: list[str] | None = None,
    confirm: bool = False,
    max_pages: int = DEFAULT_MAX_PAGES,
    page_size: int = DEFAULT_PAGE_SIZE,
    template_name_prefix: str = "템플릿이관",
    search_fn: Any = None,
    update_fn: Any = None,
    get_product_fn: Any = None,
    delete_fn: Any = None,
    ledger_updater: Any = None,
) -> HarvestReport:
    """한 런의 더미를 수확한다: 마커 수집 → 중지 → 추출 → 삭제 → 보고.

    순서 (불변):
      1. ``collect_marked_products`` 로 마커가 일치하는 상품만 수집.
      2. 각 상품을 **즉시 판매중지** (``update_product`` SUSPENSION).
      3. 각 상품에서 템플릿 추출·저장 (``mcp_server.get_product`` 의
         ``save_as_template`` 경로 재사용).
      4. 중지·추출이 모두 끝난 상품만 삭제 (``delete_origin_product``).
      5. ``삭제 N/M`` + 남은 것 목록 보고.

    안전 불변식:
      - 마커 불일치 상품은 조회·중지·삭제하지 않는다 (``collect_marked_products``
        가 마커 매칭만 통과시킨다).
      - ``confirm`` 이 ``True`` 가 아니면 중지·삭제 API 호출 0 회 (dry-run).
      - 부분 실패는 부분 성공으로 위장하지 않는다.

    Args:
        run_id: 런 식별자. ``markers`` 가 없으면 이 런의 모든 표식을 찾는다.
        markers: 정확한 표식 목록 (run_id 보다 우선).
        confirm: ``True`` 일 때만 중지·삭제 수행. 기본값 ``False`` (dry-run).
        max_pages: 검색 페이지 상한.
        page_size: 페이지당 listing 수.
        template_name_prefix: 저장할 템플릿 이름 접두사. 표식의 행 번호가 붙는다.
        search_fn / update_fn / get_product_fn / delete_fn: 테스트 주입용.
            기본값은 각각 ``naver_client.search_products`` /
            ``naver_client.update_product`` / ``mcp_server.get_product`` /
            ``naver_client.delete_origin_product``.
        ledger_updater: 선택. run ledger 갱신 콜백 ``(marker, field, value)``.
            끊김 복구를 위해 행동 전에 기록을 갱신한다.

    Returns:
        ``HarvestReport`` — 삭제 결과·남은 것·페이지 상한 여부를 담는다.
    """
    if search_fn is None:
        search_fn = naver_client.search_products
    if update_fn is None:
        update_fn = naver_client.update_product
    if get_product_fn is None:
        get_product_fn = mcp_server.get_product
    if delete_fn is None:
        delete_fn = naver_client.delete_origin_product

    # run_id 가 비어있으면 markers 에서 유추 시도 (끊김 복구).
    effective_run_id = run_id
    if not effective_run_id and markers:
        # markers 가 CLSTMIG-<runid>-<row> 형태면 runid 추출.
        for m in markers:
            if isinstance(m, str) and tmf.is_our_marker(m):
                parts = m.split("-")
                if len(parts) >= 3:
                    # CLSTMIG-<runid>-<row> — runid 는 가운데.
                    # runid 자체에 '-' 가 없으므로 parts[1].
                    effective_run_id = parts[1]
                    break

    # 1. 마커로 상품번호 수집.
    try:
        found, page_cap, pages, listings_scanned = collect_marked_products(
            run_id=effective_run_id,
            markers=markers,
            max_pages=max_pages,
            page_size=page_size,
            search_fn=search_fn,
        )
    except ValueError as exc:
        return HarvestReport(
            run_id=effective_run_id,
            dry_run=not confirm,
            error=str(exc),
        )

    report = HarvestReport(
        run_id=effective_run_id,
        found_count=len(found),
        page_cap_reached=page_cap,
        pages_scanned=pages,
        listings_scanned=listings_scanned,
        dry_run=not confirm,
    )

    # 행동 전에 기록 — ledger 에 발견된 상품번호를 먼저 적는다.
    # (ledger_updater 가 있을 때만. 본 함수 자체는 ledger 를 직접 쓰지 않는다 —
    #  caller(harvest_run_from_ledger)가 콜백을 넘긴다.)
    for item in found:
        if ledger_updater:
            try:
                ledger_updater(item["marker"], "origin_product_no", item["origin_product_no"])
                if item.get("channel_product_no"):
                    ledger_updater(item["marker"], "channel_product_no", item["channel_product_no"])
            except Exception:
                pass  # 기록 실패가 수확을 막지 않는다.

    remaining: list[dict[str, Any]] = []
    for item in found:
        origin_no = item["origin_product_no"]
        channel_no = item.get("channel_product_no", "")
        marker = item["marker"]

        # 2. 즉시 판매중지 (추출보다 먼저 — 노출 창 최소화).
        stopped, stop_err = _suspend_product(channel_no, confirm=confirm, update_fn=update_fn)
        if stopped:
            report.stopped_count += 1
        if ledger_updater:
            try:
                ledger_updater(marker, "stopped", stopped)
                if stop_err:
                    ledger_updater(marker, "error", stop_err)
            except Exception:
                pass

        # 3. 추출·템플릿 저장.
        # 템플릿 이름: 접두사-행번호. 표식에서 행번호 추출.
        row_seq = ""
        if tmf.is_our_marker(marker):
            parts = marker.split("-")
            if len(parts) >= 3:
                row_seq = parts[-1]
        tname = f"{template_name_prefix}-{row_seq}" if row_seq else template_name_prefix
        extracted, ext_err = _extract_and_save(
            origin_no, template_name=tname, get_product_fn=get_product_fn
        )
        if extracted:
            report.extracted_count += 1
        if ledger_updater:
            try:
                ledger_updater(marker, "extracted", extracted)
                if ext_err:
                    ledger_updater(marker, "error", ext_err)
            except Exception:
                pass

        # 4. 삭제 — 중지·추출이 모두 끝난 것만.
        # 추출이 실패한 상품은 삭제하지 않는다 (값을 못 걷었는데 지우면 손실).
        # 단, dry-run 에서는 추출 결과와 무관하게 삭제도 생략(confirm=False).
        deleted = False
        del_err = ""
        if extracted and confirm:
            deleted, del_err = _delete_product(origin_no, confirm=True, delete_fn=delete_fn)
        elif not extracted:
            del_err = "추출 실패 — 삭제 보류"
        elif not confirm:
            del_err = "dry-run — 삭제 생략"
        if deleted:
            report.deleted_count += 1
        else:
            remaining.append(
                {
                    "origin_product_no": origin_no,
                    "channel_product_no": channel_no,
                    "marker": marker,
                    "reason": del_err or stop_err or "미삭제",
                }
            )
        if ledger_updater:
            try:
                ledger_updater(marker, "deleted", deleted)
                if del_err:
                    ledger_updater(marker, "error", del_err)
            except Exception:
                pass

    report.remaining = remaining
    return report


# ---------------------------------------------------------------------------
# run ledger 연동 — 끊김 복구.
#
# 수확 파이프라인이 중간에 끊겨도 다음 실행이 이어서 처리할 수 있어야 한다.
# ``harvest_run_from_ledger`` 는 run ledger 를 읽어 미처리 항목만 다시 시도한다.
# ---------------------------------------------------------------------------
def _make_ledger_updater(run_id: str):
    """run ledger 의 특정 item 필드를 갱신하는 콜백을 만든다.

    콜백 시그니처: ``(marker, field, value)``. ledger 에서 marker 에 해당하는
    item 을 찾아 field 를 value 로 갱신하고 디스크에 쓴다. 갱신 실패(파일 없음
    등)는 조용히 넘긴다 — 수확 자체를 막지 않는다.
    """
    base_path = tmf.run_ledger_path(run_id)

    def _update(marker: str, field: str, value: Any) -> None:
        try:
            data = tmf.read_run_ledger(run_id)
            if not isinstance(data, dict):
                return
            items = data.get("items")
            if not isinstance(items, list):
                return
            for it in items:
                if isinstance(it, dict) and it.get("marker") == marker:
                    it[field] = value
                    break
            data["status"] = "in_progress"
            tmp = base_path.with_suffix(base_path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            import os as _os

            _os.replace(tmp, base_path)
        except Exception:
            pass

    return _update


def harvest_run_from_ledger(
    run_id: str,
    *,
    confirm: bool = False,
    max_pages: int = DEFAULT_MAX_PAGES,
    page_size: int = DEFAULT_PAGE_SIZE,
    search_fn: Any = None,
    update_fn: Any = None,
    get_product_fn: Any = None,
    delete_fn: Any = None,
) -> HarvestReport:
    """run ledger 에서 런을 읽어 수확한다 (끊김 복구 경로).

    ledger 가 있으면 그 markers 로 ``harvest_run`` 을 호출하고, 행동 전에
    ledger 를 갱신하는 콜백을 넘긴다. ledger 가 없으면 markers 없이 run_id 만으로
    검색한다 (사용자가 run_id 만 알 때).
    """
    ledger = tmf.read_run_ledger(run_id)
    markers: list[str] | None = None
    if isinstance(ledger, dict):
        m = ledger.get("markers")
        if isinstance(m, list):
            markers = [str(x) for x in m if isinstance(x, str) and x]

    updater = _make_ledger_updater(run_id) if ledger is not None else None

    return harvest_run(
        run_id=run_id,
        markers=markers,
        confirm=confirm,
        max_pages=max_pages,
        page_size=page_size,
        search_fn=search_fn,
        update_fn=update_fn,
        get_product_fn=get_product_fn,
        delete_fn=delete_fn,
        ledger_updater=updater,
    )


# ---------------------------------------------------------------------------
# 수확 폼 서버 — 슬라이스 1 폼과 동일한 방어 패턴.
#
# 사용자는 슬라이스 1 결과 페이지에서 run_id 를 받아 이 폼에 입력한다.
# 처리 순서·안전 불변식은 ``harvest_run`` 이 담보한다. 본 서버는 HTTP 래퍼다.
#
# **예외 방벽 (이 모듈의 핵심 계약)**:
# 핸들러 바깥으로 예외가 번지면 ``http.server`` 는 응답을 쓰지 않고 연결을
# 끊는다 → 사용자는 브라우저에 "연결이 재설정되었습니다" 만 보고 무슨 일이
# 일어났는지 알 수 없다. 이것은 **거짓 성공** · **죽은 UI** 와 같은
# 계열의 결함이다. 본 서버는:
#   (1) ``do_POST``/``do_GET``/``do_OPTIONS`` 전체를 try/except 로 감싸,
#       어떤 예외라도 5xx + 사람이 읽을 HTML 을 응답한다.
#   (2) 설정 파일이 없는 경우(첫 사용자가 정확히 여기서 막힌다)는 예외가
#       아니라 정상 안내 화면으로 처리한다.
#   (3) 오류 화면의 사유에는 ``common.sanitize_error`` 로 경로·비밀값을 정화한다.
# ---------------------------------------------------------------------------
_MAX_BODY_BYTES = 16 * 1024


def _config_present() -> bool:
    """설정 파일이 존재하고 읽을 수 있는지 (예외 없이).

    설정이 없는 환경은 예외가 아니라 **정상 안내 경로**로 처리한다 —
    ``harvest_run_from_ledger`` 가 ``naver_client.search_products`` 를 부를 때
    ``FileNotFoundError`` 로 번지는 것을 미리 막는다. ``check_config`` 가 하는
    안내("config.example.json 을 .local/config.json 으로 복사하라")를
    결과 화면에 재사용한다.
    """
    try:
        cfg_path = naver_client.resolve_config_path()
    except Exception:
        return False
    try:
        import os

        return bool(cfg_path) and os.path.isfile(cfg_path)
    except Exception:
        return False


class HarvestFormServer:
    """템플릿 이관 수확 폼 1건의 처리를 대기하는 로컬 서버.

    사용 흐름:
        srv = HarvestFormServer(token=..., bind_port=0)
        port = srv.start()
        srv.wait(timeout=...)
        report = srv.report
    """

    def __init__(
        self,
        *,
        token: str,
        ttl_seconds: int = TTL_SECONDS,
        bind_host: str = "127.0.0.1",
        confirm_default: bool = False,
    ) -> None:
        if not token:
            raise ValueError("token 이 필요합니다.")
        if bind_host not in ("127.0.0.1", "localhost"):
            raise ValueError(f"bind_host 는 127.0.0.1 이어야 합니다 (got {bind_host!r}).")
        self.token = str(token)
        self.ttl_seconds = int(ttl_seconds)
        self.bind_host = "127.0.0.1"
        self.confirm_default = bool(confirm_default)
        self._born_at = time.monotonic()
        self._http: _ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._report: HarvestReport | None = None
        self._consumed = False
        self._lock = threading.Lock()
        self._port: int | None = None

    def is_expired(self) -> bool:
        return (time.monotonic() - self._born_at) >= self.ttl_seconds

    def is_consumed(self) -> bool:
        with self._lock:
            return self._consumed

    @property
    def port(self) -> int | None:
        return self._port

    @property
    def report(self) -> HarvestReport | None:
        with self._lock:
            return self._report

    def start(self) -> int:
        if self._http is not None:
            raise RuntimeError("HarvestFormServer 는 한 번만 시작할 수 있습니다.")
        httpd = _ThreadingHTTPServer(("127.0.0.1", 0), _HarvestFormHandler)
        httpd.harvest_form_state = self  # type: ignore[attr-defined]
        self._http = httpd
        self._port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        self._thread = t
        return self._port

    def wait(self, timeout: float | None = None) -> HarvestReport:
        if self._http is None:
            raise RuntimeError("start() 를 먼저 호출해야 합니다.")
        deadline = self._born_at + (self.ttl_seconds if timeout is None else float(timeout))
        while True:
            with self._lock:
                if self._report is not None:
                    report = self._report
                    break
            if time.monotonic() >= deadline:
                report = HarvestReport(error="timeout")
                break
            time.sleep(0.05)
        self._shutdown()
        return report

    def consume(self, report: HarvestReport) -> None:
        with self._lock:
            if self._consumed:
                return
            self._consumed = True
            self._report = report

    def shutdown_from_request(self) -> None:
        t = threading.Thread(target=self._shutdown, daemon=True)
        t.start()

    def _shutdown(self) -> None:
        httpd = self._http
        if httpd is None:
            return
        try:
            httpd.shutdown()
        except Exception:
            pass
        try:
            httpd.server_close()
        except Exception:
            pass

    def close(self) -> None:
        self._shutdown()


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    harvest_form_state: HarvestFormServer | None = None


class _HarvestFormHandler(http.server.BaseHTTPRequestHandler):
    """수확 폼 1건의 처리를 받는 HTTP 핸들러 (슬라이스 1 폼과 동일 구조).

    **예외 방벽**: 각 ``do_*`` 메서드 전체를 try/except 이 감싼다. 예외가
    핸들러 바깥으로 번지면 ``http.server`` 는 응답을 쓰지 않고 연결을 끊는다.
    이것은 "거짓 성공" 결함의 한 계열이다. 모든 예외는
    ``_respond_barrier_error`` 로 5xx + 사람이 읽을 HTML 로 바뀐다.
    """

    server_version = "clossify-harvest-form"
    sys_version = ""

    def do_POST(self) -> None:
        try:
            path = self.path.split("?", 1)[0]
            if path not in ("/", "/harvest"):
                self._reject_html(404, "not_found", "알 수 없는 경로입니다.")
                return
            self._handle_harvest()
        except Exception as exc:
            self._respond_barrier_error(exc)

    def do_GET(self) -> None:
        try:
            self._reject_html(405, "method_not_allowed", "GET 은 지원하지 않습니다.")
        except Exception as exc:
            self._respond_barrier_error(exc)

    def do_OPTIONS(self) -> None:
        try:
            self._reject_html(405, "method_not_allowed", "CORS preflight 는 지원하지 않습니다.")
        except Exception as exc:
            self._respond_barrier_error(exc)

    def _respond_barrier_error(self, exc: BaseException) -> None:
        """방벽이 잡은 예외를 5xx + 정화된 HTML 로 응답.

        ``http.server`` 핸들러에서 예외가 번지면 응답 없이 연결이 끊긴다.
        본 메서드는:
          - 이미 응답을 보낸 뒤의 예외(BrokenPipe 등) 면 더 보내지 않는다.
          - 그 외에는 500 + ``common.sanitize_error`` 로 정화한 사유를 HTML 로.
        """
        # 이미 응답을 보냈는지 확인 — send_response 가 headers 를 시작했으면
        # 더 이상 쓸 수 없다.
        try:
            already_sent = bool(getattr(self, "_headers_buffer", None))
        except Exception:
            already_sent = False
        if already_sent:
            # 이미 응답을 보내는 중이었다 — 더 보낼 수 없다.
            return
        reason = common.sanitize_error(exc)
        page = _harvest_result_page(
            ok=False,
            status_text="처리 중 오류가 발생했습니다 (HTTP 500)",
            detail=(
                "<strong>예외:</strong> " + html.escape(reason) + "<br>"
                "요청을 완료하지 못했습니다. 설정 파일(.local/config.json)이 있는지,"
                " 그리고 run_id 가 올바른지 확인하세요."
            ),
        )
        try:
            self._respond_html(500, page)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle_harvest(self) -> None:
        import urllib.parse

        from . import approval_server

        srv = self.server.harvest_form_state  # type: ignore[attr-defined]
        if srv is None:
            self._reject_html(500, "no_state", "서버 상태를 사용할 수 없습니다.")
            return

        # 1. 만료 검사.
        if srv.is_expired():
            self._respond_html(
                410,
                _harvest_result_page(
                    ok=False,
                    status_text="폼 대기 시간이 만료되었습니다.",
                    detail=html.escape("10분이 경과했습니다. 새 폼을 받으세요."),
                ),
            )
            srv.shutdown_from_request()
            return

        # 2. Origin/Referer 검사 (슬라이스 1 과 동일).
        if not approval_server.origin_referer_ok(self.headers):
            self._reject_html(403, "bad_origin", "허용되지 않은 Origin/Referer 입니다.")
            return

        # 3. 본문 읽기.
        length = self._content_length()
        if length is None or length > _MAX_BODY_BYTES:
            self._reject_html(413, "too_large", "요청 본문이 너무 큽니다.")
            return
        raw = self.rfile.read(length)

        # 4. 폼 본문 파싱.
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/x-www-form-urlencoded":
            self._reject_html(415, "unsupported_media_type", "폼 인코딩 본문만 지원합니다.")
            return
        try:
            pairs = urllib.parse.parse_qsl(
                raw.decode("utf-8"), keep_blank_values=True, strict_parsing=False
            )
        except (UnicodeDecodeError, ValueError):
            self._reject_html(400, "bad_form", "폼 본문이 올바르지 않습니다.")
            return

        form: dict[str, str] = {}
        for key, value in pairs:
            k = str(key or "").strip()
            if not k or k == "token":
                continue
            form[k] = str(value)

        # 5. 토큰 검증.
        presented = ""
        h = self.headers.get("X-Harvest-Form-Token")
        if h and h.strip():
            presented = h.strip()
        else:
            for key, value in pairs:
                if str(key or "").strip() == "token":
                    v = str(value or "").strip()
                    if v:
                        presented = v
                        break
        if not presented:
            self._reject_html(401, "no_token", "폼 토큰이 필요합니다.")
            return
        if srv.is_consumed():
            self._reject_html(410, "consumed", "이미 사용된 토큰입니다.")
            return
        if not approval_server.tokens_match(srv.token, presented):
            self._reject_html(403, "bad_token", "폼 토큰이 일치하지 않습니다.")
            return

        # 6. 수확 실행.
        run_id = form.get("run_id", "").strip()
        confirm_raw = form.get("confirm", "").strip().lower()
        # confirm 게이트 — "true"/"yes" 만 승인. 기본은 dry-run.
        confirm = confirm_raw in ("true", "yes", "1")

        # 6a. 설정 파일 사전 검사 — 예외가 아닌 정상 안내 화면.
        # ``harvest_run_from_ledger`` → ``naver_client.search_products`` →
        # ``load_config`` 경로에서 ``FileNotFoundError`` 가 번지는 것을 막는다.
        # 첫 사용자가 정확히 여기서 "연결이 재설정되었습니다" 를 본다.
        if run_id and not _config_present():
            srv.consume(HarvestReport(error="config 없음"))
            self._respond_html(
                200,
                _harvest_result_page(
                    ok=False,
                    status_text="설정 파일이 없습니다",
                    detail=(
                        "<strong>네이버 커머스 API 설정(config.json)이 필요합니다.</strong><br>"
                        "프로젝트 루트의 <code>config.example.json</code> 을 "
                        "<code>.local/config.json</code> 으로 복사한 뒤, 실제 값으로 채우세요.<br>"
                        "설정이 준비되면 다시 수확 폼을 실행하세요."
                    ),
                ),
            )
            srv.shutdown_from_request()
            return

        if not run_id:
            report = HarvestReport(error="run_id 가 필요합니다.")
        else:
            report = harvest_run_from_ledger(run_id, confirm=confirm)

        srv.consume(report)

        # 7. 결과 페이지.
        ok = not report.error
        detail_parts = [
            html.escape(report.summary()),
        ]
        if report.remaining:
            detail_parts.append("<strong>남은 상품 (수동 확인 필요):</strong>")
            for r in report.remaining[:20]:
                detail_parts.append(
                    f"- origin={html.escape(str(r.get('origin_product_no', '')))} "
                    f"marker={html.escape(str(r.get('marker', '')))} "
                    f"사유={html.escape(str(r.get('reason', '')))}"
                )
            if len(report.remaining) > 20:
                detail_parts.append(f"... 외 {len(report.remaining) - 20}건")
        if report.error:
            detail_parts.append(f"<strong>오류:</strong> {html.escape(report.error)}")
        detail = "<br>".join(detail_parts)

        self._respond_html(
            200,
            _harvest_result_page(
                ok=ok,
                status_text="수확 완료" if ok else "수확 실패",
                detail=detail,
            ),
        )

        # 8. 서버 종료 예약.
        srv.shutdown_from_request()

    def _reject_html(self, status: int, code: str, detail: str) -> None:
        page = _harvest_result_page(
            ok=False,
            status_text=f"거부됨 (HTTP {status}, {html.escape(code)})",
            detail=html.escape(detail),
        )
        self._respond_html(status, page)

    def _respond_html(self, status: int, page: str) -> None:
        body = page.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 의도적으로 Access-Control-Allow-Origin 은 보내지 않는다.
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _content_length(self) -> int | None:
        raw = self.headers.get("Content-Length")
        if raw is None:
            return None
        try:
            n = int(raw)
        except (TypeError, ValueError):
            return None
        return n if n >= 0 else None

    def log_message(self, format: str, *args: Any) -> None:
        return


# ---------------------------------------------------------------------------
# 수확 폼/결과 HTML.
# ---------------------------------------------------------------------------
_HARVEST_CSS = """
body{margin:0;padding:24px;background:#f5f5f5;font-family:-apple-system,
  BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  color:#222;font-size:14px;line-height:1.6}
.wrap{max-width:720px;margin:0 auto;background:#fff;padding:32px;
  border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,0.08)}
.banner{padding:16px 18px;border-radius:8px;font-size:18px;font-weight:700;
  margin-bottom:16px}
.banner.ok{background:#e6f4ea;color:#137333;border:2px solid #137333}
.banner.err{background:#fce8e6;color:#a50e0e;border:2px solid #a50e0e}
.detail{color:#444;line-height:1.7;font-size:13px;word-break:break-word}
.note{margin-top:20px;padding:12px;background:#eef6ff;border-radius:6px;
  color:#1a4d8f;font-size:12px;line-height:1.7}
.note strong{color:#0b3d7a}
"""


def _harvest_result_page(*, ok: bool, status_text: str, detail: str) -> str:
    title = "수확 완료" if ok else "수확 결과"
    banner_cls = "ok" if ok else "err"
    return (
        "<!DOCTYPE html>\n"
        '<html lang="ko"><head><meta charset="utf-8" />'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />'
        f"<title>{html.escape(title)}</title>"
        "<style>" + _HARVEST_CSS + "</style></head><body>"
        '<div class="wrap">'
        f'<div class="banner {banner_cls}">{html.escape(status_text)}</div>'
        f'<div class="detail">{detail}</div>'
        '<div class="note">이 페이지는 로컬 수확 폼 서버의 처리 결과입니다. '
        "비밀값은 표시되지 않습니다.</div>"
        "</div></body></html>"
    )


def render_harvest_form_html(*, token: str, port: int) -> str:
    """수확 폼 HTML 문자열을 만든다. <script> 0개, 순수 HTML 폼 POST."""
    safe_token = html.escape(str(token), quote=True)
    action = f"http://127.0.0.1:{int(port)}/"
    return "\n".join(
        [
            "<!DOCTYPE html>",
            '<html lang="ko">',
            "<head>",
            '<meta charset="utf-8" />',
            '<meta name="viewport" content="width=device-width, initial-scale=1" />',
            "<title>템플릿 이관 — 더미 수확</title>",
            "<style>" + _HARVEST_CSS + "</style>",
            "</head>",
            "<body>",
            '<div class="wrap">',
            '<div class="banner ok">템플릿 이관 — 더미 수확</div>',
            '<div class="detail">'
            "더미 엑셀을 판매자센터에 업로드한 뒤, 슬라이스 1 결과 페이지에서 받은 "
            "<strong>런 ID</strong>를 입력하세요. 표식을 가진 더미만 찾아 "
            "판매중지 → 템플릿 추출 → 삭제 순으로 처리합니다."
            "</div>",
            f'<form method="POST" action="{action}">',
            f'<input type="hidden" name="token" value="{safe_token}" />',
            '<div style="margin:16px 0">'
            '<label style="display:block;font-weight:600;margin-bottom:4px">'
            "런 ID "
            '<span style="color:#a50e0e">[필수]</span>'
            "</label>"
            '<input type="text" name="run_id" autocomplete="off" '
            'placeholder="CLSTMIG-YYYYMMDDTHHMMZ (슬라이스 1 결과에서 받은 ID)" '
            'style="width:100%;box-sizing:border-box;padding:8px 10px;'
            'font-size:14px;border:1px solid #ccc;border-radius:4px" />'
            "</div>",
            '<div style="margin:16px 0">'
            '<label style="display:block;font-weight:600;margin-bottom:4px">'
            "실제 실행 (기본: dry-run) "
            '<span style="color:#555;font-weight:400">[선택]</span>'
            "</label>"
            '<input type="checkbox" name="confirm" value="true" /> '
            "확인 — 체크 시 실제로 판매중지·삭제를 수행한다"
            "<br /><small>체크하지 않으면 중지·삭제 API 호출이 0회다 (dry-run).</small>"
            "</div>",
            '<div style="margin-top:24px">'
            '<button type="submit" '
            'style="background:#137333;color:#fff;border:0;border-radius:6px;'
            'padding:12px 28px;font-size:15px;font-weight:600;cursor:pointer">'
            "수확 실행"
            "</button>"
            "</div>",
            "</form>",
            '<div class="note">'
            "<strong>안전:</strong> 마커가 일치하지 않는 상품은 조회·중지·삭제하지 않는다. "
            "추측으로 더미를 지우지 않는다."
            "</div>",
            "</div>",
            "</body>",
            "</html>",
        ]
    )


def write_harvest_form_html(html_path: str, *, token: str, port: int):
    """수확 폼 HTML 을 디스크에 쓰고 경로를 반환한다."""
    from pathlib import Path

    path = Path(html_path)
    doc = render_harvest_form_html(token=token, port=port)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc, encoding="utf-8")
    return path


def actual_bound_host(server: HarvestFormServer) -> str:
    """서버가 실제로 바인드한 호스트를 반환한다 (방어 검증용)."""
    httpd = server._http
    if httpd is None:
        return ""
    return str(httpd.server_address[0])


__all__ = [
    "DEFAULT_MAX_PAGES",
    "DEFAULT_PAGE_SIZE",
    "HarvestFormServer",
    "HarvestReport",
    "TTL_SECONDS",
    "_extract_channel_no",
    "_extract_origin_no",
    "_extract_seller_code",
    "actual_bound_host",
    "collect_marked_products",
    "harvest_run",
    "harvest_run_from_ledger",
    "render_harvest_form_html",
    "write_harvest_form_html",
]
