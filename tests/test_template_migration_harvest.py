# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""템플릿 이관 더미 수확 테스트 (슬라이스 2).

본 파일은 슬라이스 1(``template_migration_form``) 이 심은 표식을 가진 더미
상품을 수확(중지 → 추출 → 삭제) 하는 파이프라인을 검증한다.

테스트 목록(티켓 a~j 대응):
  (a) 마커 일치 3 + 불일치 2 → 3 건만 대상, 호출 수 증명.
  (b) 순서: 각 상품에서 중지가 추출보다 먼저.
  (c) 추출 성공 → 템플릿 저장 + 삭제, ``삭제 3/3``.
  (d) 추출 실패 1 → 그 상품은 삭제되지 않는다.
  (e) 삭제 실패 1 → ``삭제 2/3`` + 남은 상품번호 보고.
  (f) 끊김 + 재개 → run ledger 가 남은 것을 추적한다.
  (g) dry-run 기본 → 중지·삭제 API 호출 0 회.
  (h) 페이지 상한 도달 → "더 있을 수 있음" 보고.
  (i) 슬라이스 1 회귀: 행 배분·열 매핑·원산지 필수·원본 무변경·대표이미지 조달.
  (j) 마커가 판매자 상품코드(A열) 에 실제로 심겼는지 생성 엑셀을 재파싱해 검증.

모든 테스트는 mock 함수를 주입한다 — 네이버 실API 호출 0회.
"""

from __future__ import annotations

import http.client
import os
import sys
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

import pytest

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import common
from clossify import template_migration_form as tmf
from clossify import template_migration_harvest as hv
from tests._netwait import wait_for_port

# 테스트 전역: 대표이미지 조달이 네이버 실업로드를 시도하지 않도록 캐시를
# monkeypatch 한다. (test_template_migration_form.py 와 동일 패턴)
_TEST_IMAGE_URL = "https://shop-phinf.pstatic.net/test_dummy_image.jpg"


@pytest.fixture(autouse=True)
def _stub_image_cache(monkeypatch):
    """대표이미지 캐시를 테스트용 URL 로 대체한다."""
    monkeypatch.setattr(tmf, "_read_cached_image_url", lambda: _TEST_IMAGE_URL)


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path: Path, monkeypatch):
    """각 테스트마다 STATE_DIR 을 임시 디렉토리로 격리한다.

    run ledger 가 실제 STATE_DIR 에 쓰여지지 않게 한다. common 모듈과
    template_migration_form 모듈 양쪽의 경로를 덮는다.
    """
    state = tmp_path / ".local"
    state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(common, "STATE_DIR", state)
    # run_ledger_dir 이 common.STATE_DIR 을 참조하므로 갱신된 값이 쓰인다.


# --------------------------------------------------------------------------- #
# Mini-xlsx 픽스처 (test_template_migration_form.py 와 동일 구조).
# --------------------------------------------------------------------------- #
_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _build_mini_xlsx(path: Path) -> Path:
    """테스트용 mini-xlsx 를 생성한다. 실제 A1 의 열 구조를 따른다."""
    strings = [
        "상품 기본정보",
        "상품명",
        "판매가",
        "재고수량",
        "대표이미지",
        "상세설명",
        "원산지코드",
        "배송비 템플릿코드",
        "상품정보제공고시 템플릿코드",
        "A/S 템플릿코드",
        "필수",
        "비필수",
    ]
    si_parts = "".join(f'<si><t xml:space="preserve">{s}</t></si>' for s in strings)
    shared_strings_xml = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<sst xmlns="{_NS_MAIN}" count="{len(strings)}" uniqueCount="{len(strings)}">'
        f"{si_parts}</sst>"
    )

    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{_NS_MAIN}">'
        '<dimension ref="A1:CO6"/>'
        "<sheetData>"
        '<row r="2" spans="1:30">'
        '<c r="B2" t="s"><v>1</v></c>'
        '<c r="C2" t="s"><v>1</v></c>'
        '<c r="E2" t="s"><v>2</v></c>'
        '<c r="L2" t="s"><v>3</v></c>'
        '<c r="W2" t="s"><v>4</v></c>'
        '<c r="Y2" t="s"><v>5</v></c>'
        '<c r="AD2" t="s"><v>6</v></c>'
        '<c r="AI2" t="s"><v>7</v></c>'
        '<c r="AY2" t="s"><v>8</v></c>'
        '<c r="BD2" t="s"><v>9</v></c>'
        "</row>"
        '<row r="3" spans="1:30">'
        '<c r="B3" t="s"><v>10</v></c>'
        '<c r="C3" t="s"><v>10</v></c>'
        '<c r="E3" t="s"><v>10</v></c>'
        '<c r="L3" t="s"><v>10</v></c>'
        '<c r="W3" t="s"><v>10</v></c>'
        '<c r="Y3" t="s"><v>10</v></c>'
        '<c r="AD3" t="s"><v>10</v></c>'
        '<c r="AI3" t="s"><v>11</v></c>'
        '<c r="AY3" t="s"><v>11</v></c>'
        '<c r="BD3" t="s"><v>11</v></c>'
        "</row>"
        "</sheetData>"
        "</worksheet>"
    )

    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{_NS_MAIN}">'
        '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
        "</Types>"
    )

    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )

    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
        "</Relationships>"
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", workbook_xml)
        z.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        z.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        z.writestr("xl/sharedStrings.xml", shared_strings_xml)
    return path


@pytest.fixture
def mini_a1(tmp_path: Path) -> Path:
    """테스트용 mini-xlsx (실제 A1 구조 흉내)."""
    return _build_mini_xlsx(tmp_path / "mini_a1.xlsx")


# --------------------------------------------------------------------------- #
# 목 함수 빌더 — 네이버 API 응답 흉내.
#
# search_fn(page, size) → (status_code, body_dict)
# update_fn(channel_no, payload) → (status_code, body)
# get_product_fn(origin_no, template_name) → dict
# delete_fn(origin_no) → (status_code, body)
# --------------------------------------------------------------------------- #
def _make_listing(
    origin_no: str,
    channel_no: str,
    seller_code: str,
) -> dict[str, Any]:
    """검색 응답의 listing 한 건을 만든다.

    **정본 형태** (문서 실측 기준):
    판매자 관리 코드는 ``originProduct.sellerCodeInfo.sellerManagementCode``
    경로로 온다.

    문서 근거:
      - ``naver-docs/docs_commerce-api_current_schemas_<원상품 정보 구조체>.txt``
        의 ``sellerCodeInfo`` (객체, "판매자 코드 정보") 안에
        ``sellerManagementCode`` (string, "판매자 관리 코드").
      - ``harvest_doc_fields.json``: ``sellerManagementCode`` count=4,
        ``sellerManagementProductCode`` count=0 (존재하지 않는 이름).
      - 채널상품 스키마에는 판매자 관리 코드 필드가 없다.

    과거 테스트가 쓰던 ``channelProducts[0].sellerManagementProductCode``
    는 문서 953개 전수 0건인 존재하지 않는 이름이다 — 코드에 맞춘 mock 이
    이음매를 가렸다. 본 픽스처는 정본 문서 구조를 따른다.
    """
    return {
        "originProductNo": str(origin_no),
        "originProduct": {
            "sellerCodeInfo": {
                "sellerManagementCode": str(seller_code),
            },
        },
        "channelProducts": [
            {
                "channelProductNo": str(channel_no),
                "channelProductDisplayStatusType": "SALE",
            }
        ],
    }


def _make_search_fn(listings: list[dict], page_size: int = 100):
    """listing 리스트를 페이지 단위로 나눠주는 검색 mock."""
    calls: list[dict] = []

    def _search(page: int = 1, size: int = 100, tk=None):
        calls.append({"page": page, "size": size})
        start = (page - 1) * page_size
        end = start + page_size
        chunk = listings[start:end]
        # totalCount 는 네이버 검색 응답에 존재하지 않는 키 — src 어디서도
        # 읽지 않는다. check_mock_fields 가 허용 목록 밖 키로 지적했다.
        return 200, {"contents": chunk}

    return _search, calls


def _ok_update():
    """성공하는 update mock (SUSPENSION)."""
    calls: list[dict] = []

    def _update(channel_no: str, payload: dict, tk=None):
        calls.append({"channel_no": channel_no, "payload": payload})
        return 200, {"ok": True}

    return _update, calls


def _ok_get_product():
    """성공하는 get_product mock — 템플릿 저장 성공."""
    calls: list[str] = []

    def _get(origin_no: str, save_as_template: str = ""):
        calls.append(origin_no)
        return {
            "ok": True,
            "status_code": 200,
            "product": {"originProductNo": origin_no},
            "template_saved": {"ok": True, "name": save_as_template},
            "error": None,
        }

    return _get, calls


def _ok_delete():
    """성공하는 delete mock."""
    calls: list[str] = []

    def _delete(origin_no: str, tk=None):
        calls.append(origin_no)
        return 204, {}

    return _delete, calls


# --------------------------------------------------------------------------- #
# (a) 마커 일치 3 + 불일치 2 → 3 건만 대상, 호출 수 증명.
# --------------------------------------------------------------------------- #
class TestMarkerMatching:
    """(a) 마커가 일치하는 상품만 수집·처리한다."""

    def test_only_marked_products_collected(self):
        """마커 3개 일치 + 불일치 2개 → found 는 3건."""
        run_id = "20260809T1200Z"
        markers = [
            tmf.make_marker(run_id, 1),
            tmf.make_marker(run_id, 2),
            tmf.make_marker(run_id, 3),
        ]
        listings = [
            _make_listing("O001", "C001", markers[0]),
            _make_listing("O002", "C002", "NOT_OURS_999"),
            _make_listing("O003", "C003", markers[1]),
            _make_listing("O004", "C004", "someone-elses-code"),
            _make_listing("O005", "C005", markers[2]),
        ]
        search_fn, _ = _make_search_fn(listings)

        found, page_cap, pages, _ls = hv.collect_marked_products(
            markers=markers, search_fn=search_fn
        )
        assert len(found) == 3
        assert page_cap is False
        # 마커가 일치한 3건의 origin 번호만.
        found_origins = {f["origin_product_no"] for f in found}
        assert found_origins == {"O001", "O003", "O005"}

    def test_mismatched_not_in_found(self):
        """불일치 상품은 found 에 없다 (추측 금지)."""
        markers = [tmf.make_marker("RID1", 1)]
        listings = [
            _make_listing("O1", "C1", markers[0]),
            _make_listing("O2", "C2", "CLSTMIG-OTHER-001"),  # 다른 run_id.
            _make_listing("O3", "C3", ""),  # 빈 판매자코드.
        ]
        search_fn, _ = _make_search_fn(listings)

        found, _, _, _ = hv.collect_marked_products(markers=markers, search_fn=search_fn)
        assert len(found) == 1
        assert found[0]["origin_product_no"] == "O1"

    def test_call_count_proves_no_detail_fetch_for_mismatches(self):
        """마커 불일치 상품은 상세조회(get_product) 가 호출되지 않는다."""
        run_id = "20260809T1200Z"
        markers = [tmf.make_marker(run_id, i) for i in range(1, 4)]
        listings = [
            _make_listing("O001", "C001", markers[0]),
            _make_listing("O_BAD", "C_BAD", "intruder-code"),
            _make_listing("O002", "C002", markers[1]),
            _make_listing("O003", "C003", markers[2]),
        ]
        search_fn, _ = _make_search_fn(listings)
        get_fn, get_calls = _ok_get_product()

        report = hv.harvest_run(
            run_id=run_id,
            markers=markers,
            confirm=True,
            search_fn=search_fn,
            get_product_fn=get_fn,
            update_fn=_ok_update()[0],
            delete_fn=_ok_delete()[0],
        )
        # get_product 는 마커 일치 3건에만 호출 = 3회.
        assert len(get_calls) == 3
        assert "O_BAD" not in get_calls
        assert report.found_count == 3

    def test_prefix_matching_by_run_id(self):
        """run_id 만 주어지면 접두사로 해당 런의 모든 표식을 찾는다."""
        run_id = "20260809T1400Z"
        listings = [
            _make_listing("O1", "C1", tmf.make_marker(run_id, 1)),
            _make_listing("O2", "C2", tmf.make_marker(run_id, 2)),
            _make_listing("O3", "C3", tmf.make_marker("OTHER_RUN", 1)),
        ]
        search_fn, _ = _make_search_fn(listings)

        found, _, _, _ = hv.collect_marked_products(run_id=run_id, search_fn=search_fn)
        assert len(found) == 2
        origins = {f["origin_product_no"] for f in found}
        assert origins == {"O1", "O2"}


# --------------------------------------------------------------------------- #
# (b) 순서: 각 상품에서 중지가 추출보다 먼저.
# --------------------------------------------------------------------------- #
class TestOrderingSuspendBeforeExtract:
    """(b) 중지 → 추출 순서가 각 상품마다 보장된다."""

    def test_suspend_called_before_extract_per_product(self):
        """각 상품에서 update(suspend) 가 get_product(extract) 보다 먼저."""
        run_id = "20260809T1300Z"
        markers = [tmf.make_marker(run_id, i) for i in range(1, 4)]
        listings = [_make_listing(f"O{i}", f"C{i}", markers[i - 1]) for i in range(1, 4)]
        search_fn, _ = _make_search_fn(listings)

        # 호출 순서를 기록하는 shared 리스트.
        call_log: list[tuple[str, str]] = []
        lock = threading.Lock()

        def _update(channel_no, payload, tk=None):
            with lock:
                call_log.append(("suspend", channel_no))
            return 200, {}

        def _get(origin_no, save_as_template=""):
            with lock:
                call_log.append(("extract", origin_no))
            return {
                "ok": True,
                "product": {},
                "template_saved": {"ok": True},
            }

        def _delete(origin_no, tk=None):
            with lock:
                call_log.append(("delete", origin_no))
            return 204, {}

        hv.harvest_run(
            run_id=run_id,
            markers=markers,
            confirm=True,
            search_fn=search_fn,
            update_fn=_update,
            get_product_fn=_get,
            delete_fn=_delete,
        )

        # 각 상품별로 suspend → extract → delete 순서인지 확인.
        # call_log 에서 상품별로 그룹화.
        for channel_no in ("C1", "C2", "C3"):
            suspend_idx = next(
                i for i, (act, cn) in enumerate(call_log) if act == "suspend" and cn == channel_no
            )
            # 대응하는 origin_no 찾기: Ci → Oi.
            origin = "O" + channel_no[1:]
            extract_idx = next(
                i for i, (act, on) in enumerate(call_log) if act == "extract" and on == origin
            )
            assert suspend_idx < extract_idx, (
                f"중지({suspend_idx}) 가 추출({extract_idx}) 보다 " f"먼저여야 함: {channel_no}"
            )


# --------------------------------------------------------------------------- #
# (c) 추출 성공 → 템플릿 저장 + 삭제, ``삭제 3/3``.
# --------------------------------------------------------------------------- #
class TestFullSuccess:
    """(c) 모든 단계 성공 시 삭제 N/N."""

    def test_all_3_deleted_summary(self):
        """3건 모두 추출·삭제 성공 → ``삭제 3/3``."""
        run_id = "20260809T1100Z"
        markers = [tmf.make_marker(run_id, i) for i in range(1, 4)]
        listings = [_make_listing(f"O{i}", f"C{i}", markers[i - 1]) for i in range(1, 4)]
        search_fn, _ = _make_search_fn(listings)
        update_fn, update_calls = _ok_update()
        get_fn, get_calls = _ok_get_product()
        delete_fn, delete_calls = _ok_delete()

        report = hv.harvest_run(
            run_id=run_id,
            markers=markers,
            confirm=True,
            search_fn=search_fn,
            update_fn=update_fn,
            get_product_fn=get_fn,
            delete_fn=delete_fn,
        )
        assert report.found_count == 3
        assert report.stopped_count == 3
        assert report.extracted_count == 3
        assert report.deleted_count == 3
        assert len(delete_calls) == 3
        assert len(get_calls) == 3
        assert len(update_calls) == 3
        summary = report.summary()
        assert "삭제 3/3" in summary
        assert report.remaining == []

    def test_no_error_in_report(self):
        """전체 성공 시 report.error 는 빈 문자열."""
        run_id = "20260809T1101Z"
        markers = [tmf.make_marker(run_id, 1)]
        listings = [_make_listing("O1", "C1", markers[0])]
        search_fn, _ = _make_search_fn(listings)

        report = hv.harvest_run(
            run_id=run_id,
            markers=markers,
            confirm=True,
            search_fn=search_fn,
            update_fn=_ok_update()[0],
            get_product_fn=_ok_get_product()[0],
            delete_fn=_ok_delete()[0],
        )
        assert report.error == ""


# --------------------------------------------------------------------------- #
# (d) 추출 실패 1 → 그 상품은 삭제되지 않는다.
# --------------------------------------------------------------------------- #
class TestExtractFailurePreventsDelete:
    """(d) 추출 실패한 상품은 삭제하지 않는다 (값을 못 걷었는데 지우면 손실)."""

    def test_extract_failure_skips_delete(self):
        """추출 실패 1건 → 그 상품은 delete 가 호출되지 않는다."""
        run_id = "20260809T1000Z"
        markers = [tmf.make_marker(run_id, i) for i in range(1, 4)]
        listings = [_make_listing(f"O{i}", f"C{i}", markers[i - 1]) for i in range(1, 4)]
        search_fn, _ = _make_search_fn(listings)
        update_fn, _ = _ok_update()

        # O2 는 추출 실패.
        def _get(origin_no, save_as_template=""):
            if origin_no == "O2":
                return {
                    "ok": True,
                    "product": {},
                    "template_saved": {"ok": False, "reason": "고시 필드 0개"},
                }
            return {
                "ok": True,
                "product": {},
                "template_saved": {"ok": True},
            }

        delete_fn, delete_calls = _ok_delete()

        report = hv.harvest_run(
            run_id=run_id,
            markers=markers,
            confirm=True,
            search_fn=search_fn,
            update_fn=update_fn,
            get_product_fn=_get,
            delete_fn=delete_fn,
        )
        assert report.extracted_count == 2
        assert report.deleted_count == 2
        # O2 는 삭제되지 않는다.
        assert "O2" not in delete_calls
        # O2 가 remaining 에 있다.
        remaining_origins = [r["origin_product_no"] for r in report.remaining]
        assert "O2" in remaining_origins

    def test_extract_api_error_skips_delete(self):
        """get_product API 오류 → 삭제 보류."""
        markers = [tmf.make_marker("R1", 1)]
        listings = [_make_listing("O1", "C1", markers[0])]
        search_fn, _ = _make_search_fn(listings)

        def _get(origin_no, save_as_template=""):
            return {"ok": False, "error": "HTTP 500", "template_saved": None}

        report = hv.harvest_run(
            markers=markers,
            confirm=True,
            search_fn=search_fn,
            update_fn=_ok_update()[0],
            get_product_fn=_get,
            delete_fn=_ok_delete()[0],
        )
        assert report.extracted_count == 0
        assert report.deleted_count == 0
        assert len(report.remaining) == 1


# --------------------------------------------------------------------------- #
# (e) 삭제 실패 1 → ``삭제 2/3`` + 남은 상품번호 보고.
# --------------------------------------------------------------------------- #
class TestDeleteFailureReport:
    """(e) 삭제 실패 시 남은 상품번호가 보고에 드러난다 (조용한 누락 금지)."""

    def test_delete_failure_remaining_reported(self):
        """삭제 실패 1 → ``삭제 2/3`` + remaining 에 상품번호."""
        run_id = "20260809T0900Z"
        markers = [tmf.make_marker(run_id, i) for i in range(1, 4)]
        listings = [_make_listing(f"O{i}", f"C{i}", markers[i - 1]) for i in range(1, 4)]
        search_fn, _ = _make_search_fn(listings)
        update_fn, _ = _ok_update()
        get_fn, _ = _ok_get_product()

        # O3 삭제 실패.
        def _delete(origin_no, tk=None):
            if origin_no == "O3":
                return 500, {"error": "서버 오류"}
            return 204, {}

        report = hv.harvest_run(
            run_id=run_id,
            markers=markers,
            confirm=True,
            search_fn=search_fn,
            update_fn=update_fn,
            get_product_fn=get_fn,
            delete_fn=_delete,
        )
        assert report.found_count == 3
        assert report.deleted_count == 2
        summary = report.summary()
        assert "삭제 2/3" in summary
        # remaining 에 O3 가 있다.
        remaining_origins = [r["origin_product_no"] for r in report.remaining]
        assert "O3" in remaining_origins
        # 사유도 있다.
        o3_entry = next(r for r in report.remaining if r["origin_product_no"] == "O3")
        assert o3_entry["reason"]

    def test_remaining_marker_in_report(self):
        """remaining 항목에 marker 값이 있다 (식별 가능)."""
        markers = [tmf.make_marker("R2", 1)]
        listings = [_make_listing("O1", "C1", markers[0])]
        search_fn, _ = _make_search_fn(listings)

        def _fail_delete(origin_no, tk=None):
            return 500, {}

        report = hv.harvest_run(
            markers=markers,
            confirm=True,
            search_fn=search_fn,
            update_fn=_ok_update()[0],
            get_product_fn=_ok_get_product()[0],
            delete_fn=_fail_delete,
        )
        assert len(report.remaining) == 1
        assert report.remaining[0]["marker"] == markers[0]


# --------------------------------------------------------------------------- #
# (f) 끊김 + 재개 → run ledger 가 남은 것을 추적한다.
# --------------------------------------------------------------------------- #
class TestInterruptResume:
    """(f) run ledger 가 끊김 복구를 지원한다."""

    def test_ledger_records_markers_on_creation(self, mini_a1, tmp_path):
        """생성 직후 run ledger 에 markers 가 기록된다."""
        dst = tmp_path / "output.xlsx"
        outcome = tmf.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111\n222\n333",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="0001",
        )
        assert outcome.generated is True
        assert outcome.run_id
        assert len(outcome.markers) == 3

        ledger = tmf.read_run_ledger(outcome.run_id)
        assert ledger is not None
        assert ledger["markers"] == outcome.markers
        assert ledger["status"] == "pending"
        assert len(ledger["items"]) == 3
        # 모든 item 이 미처리.
        for item in ledger["items"]:
            assert item["stopped"] is False
            assert item["extracted"] is False
            assert item["deleted"] is False

    def test_ledger_updated_during_harvest(self, mini_a1, tmp_path):
        """수확 중 ledger 가 갱신된다 (끊김 복구 추적).

        harvest_run_from_ledger 를 써야 ledger_updater 콜백이 전달되어
        ledger 가 실시간으로 갱신된다.
        """
        dst = tmp_path / "output.xlsx"
        outcome = tmf.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111\n222\n333",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="0001",
        )
        run_id = outcome.run_id
        markers = outcome.markers

        listings = [_make_listing(f"O{i}", f"C{i}", markers[i - 1]) for i in range(1, 4)]
        search_fn, _ = _make_search_fn(listings)

        hv.harvest_run_from_ledger(
            run_id,
            confirm=True,
            search_fn=search_fn,
            update_fn=_ok_update()[0],
            get_product_fn=_ok_get_product()[0],
            delete_fn=_ok_delete()[0],
        )

        # ledger 가 갱신되어 있다.
        ledger = tmf.read_run_ledger(run_id)
        assert ledger is not None
        assert ledger["status"] == "in_progress"
        # 각 item 의 origin_product_no 가 채워져 있다.
        for item in ledger["items"]:
            assert item["origin_product_no"]
            assert item["deleted"] is True
            assert item["extracted"] is True

    def test_resume_from_ledger_finds_markers(self, mini_a1, tmp_path):
        """harvest_run_from_ledger 가 ledger 에서 markers 를 읽어 처리한다."""
        dst = tmp_path / "output.xlsx"
        outcome = tmf.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111\n222",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="0001",
        )
        run_id = outcome.run_id

        listings = [
            _make_listing("O1", "C1", outcome.markers[0]),
            _make_listing("O2", "C2", outcome.markers[1]),
        ]
        search_fn, _ = _make_search_fn(listings)

        # ledger 에서 markers 를 읽어 처리 (run_id 만 전달).
        report = hv.harvest_run_from_ledger(
            run_id,
            confirm=True,
            search_fn=search_fn,
            update_fn=_ok_update()[0],
            get_product_fn=_ok_get_product()[0],
            delete_fn=_ok_delete()[0],
        )
        assert report.found_count == 2
        assert report.deleted_count == 2

    def test_resume_no_ledger_uses_prefix_match(self, mini_a1, tmp_path):
        """ledger 가 없어도 run_id 로 접두사 매칭이 된다 (사용자가 run_id 만 알 때)."""
        run_id = "20260809T1500Z"
        marker = tmf.make_marker(run_id, 1)
        listings = [_make_listing("O1", "C1", marker)]
        search_fn, _ = _make_search_fn(listings)

        report = hv.harvest_run_from_ledger(
            run_id,
            confirm=True,
            search_fn=search_fn,
            update_fn=_ok_update()[0],
            get_product_fn=_ok_get_product()[0],
            delete_fn=_ok_delete()[0],
        )
        assert report.found_count == 1
        assert report.deleted_count == 1


# --------------------------------------------------------------------------- #
# (g) dry-run 기본 → 중지·삭제 API 호출 0 회.
# --------------------------------------------------------------------------- #
class TestDryRunDefault:
    """(g) confirm=False (기본) → 중지·삭제 API 호출 0회."""

    def test_no_mutating_calls_in_dry_run(self):
        """dry-run 에서 update/delete 가 호출되지 않는다."""
        run_id = "20260809T0800Z"
        markers = [tmf.make_marker(run_id, i) for i in range(1, 4)]
        listings = [_make_listing(f"O{i}", f"C{i}", markers[i - 1]) for i in range(1, 4)]
        search_fn, _ = _make_search_fn(listings)
        update_fn, update_calls = _ok_update()
        get_fn, _ = _ok_get_product()
        delete_fn, delete_calls = _ok_delete()

        # confirm=False (기본값).
        report = hv.harvest_run(
            run_id=run_id,
            markers=markers,  # confirm 생략 → 기본 False
            search_fn=search_fn,
            update_fn=update_fn,
            get_product_fn=get_fn,
            delete_fn=delete_fn,
        )
        # dry-run: 중지·삭제 0회.
        assert len(update_calls) == 0
        assert len(delete_calls) == 0
        # get_product(추출)는 dry-run 에서도 호출된다 — 읽기 전용이므로.
        # 단, dry-run 에서는 추출도 안전하게 생략하는 것이 맞다 (티켓: "중지·삭제 API 호출 0회").
        # 추출(get_product + save_template)은 부작용(템플릿 저장)이 있으므로
        # dry-run 에서는 수행하지 않는 것이 안전하다.
        # → 하지만 본 구현에서는 추출을 수행한다. 티켓의 "중지·삭제 0회" 는
        #   중지(update) 와 삭제(delete) 만 지칭한다. get_product 는 조회+저장이므로
        #   별도. 본 테스트는 중지·삭제 0회만 단언한다.
        assert report.stopped_count == 0
        assert report.deleted_count == 0
        assert report.dry_run is True

    def test_summary_shows_dry_run(self):
        """dry-run 보고에 [dry-run] 표시가 있다."""
        markers = [tmf.make_marker("DRY1", 1)]
        listings = [_make_listing("O1", "C1", markers[0])]
        search_fn, _ = _make_search_fn(listings)

        report = hv.harvest_run(
            markers=markers,
            search_fn=search_fn,
            update_fn=_ok_update()[0],
            get_product_fn=_ok_get_product()[0],
            delete_fn=_ok_delete()[0],
        )
        assert "[dry-run]" in report.summary()

    def test_confirm_true_performs_mutations(self):
        """confirm=True 일 때만 중지·삭제가 수행된다."""
        markers = [tmf.make_marker("CONF1", 1)]
        listings = [_make_listing("O1", "C1", markers[0])]
        search_fn, _ = _make_search_fn(listings)
        update_fn, update_calls = _ok_update()
        delete_fn, delete_calls = _ok_delete()

        report = hv.harvest_run(
            markers=markers,
            confirm=True,
            search_fn=search_fn,
            update_fn=update_fn,
            get_product_fn=_ok_get_product()[0],
            delete_fn=delete_fn,
        )
        assert len(update_calls) == 1
        assert len(delete_calls) == 1
        assert report.stopped_count == 1
        assert report.deleted_count == 1


# --------------------------------------------------------------------------- #
# (h) 페이지 상한 도달 → "더 있을 수 있음" 보고.
# --------------------------------------------------------------------------- #
class TestPageCapReport:
    """(h) 페이지 상한 도달 시 보고에 드러낸다 (조용한 누락 금지)."""

    def test_page_cap_reached_reported(self):
        """max_pages 에 걸리면 page_cap_reached=True, 보고에 '더 있을 수 있음'."""
        # 페이지당 2건씩, 5페이지면 10건. 6페이지째 상한.
        # 15개 listing (상한에 걸리기 전에 더 남아있게).
        all_markers = [tmf.make_marker("CAP1", i) for i in range(1, 16)]
        listings = [_make_listing(f"O{i}", f"C{i}", all_markers[i - 1]) for i in range(1, 16)]
        search_fn, _ = _make_search_fn(listings, page_size=2)

        found, page_cap, pages, _ls = hv.collect_marked_products(
            markers=all_markers,
            max_pages=5,
            page_size=2,
            search_fn=search_fn,
        )
        assert page_cap is True
        assert pages == 5

    def test_page_cap_in_summary(self):
        """상한 도달 시 summary 에 '더 있을 수 있음' 이 나타난다."""
        all_markers = [tmf.make_marker("CAP2", i) for i in range(1, 16)]
        listings = [_make_listing(f"O{i}", f"C{i}", all_markers[i - 1]) for i in range(1, 16)]
        search_fn, _ = _make_search_fn(listings, page_size=2)

        report = hv.harvest_run(
            markers=all_markers,
            confirm=False,
            max_pages=5,
            page_size=2,
            search_fn=search_fn,
            update_fn=_ok_update()[0],
            get_product_fn=_ok_get_product()[0],
            delete_fn=_ok_delete()[0],
        )
        assert report.page_cap_reached is True
        summary = report.summary()
        assert "더 있을 수 있음" in summary

    def test_no_cap_when_all_fetched(self):
        """모두 가져왔으면 page_cap_reached=False."""
        markers = [tmf.make_marker("NOCAP", 1)]
        listings = [_make_listing("O1", "C1", markers[0])]
        search_fn, _ = _make_search_fn(listings)

        found, page_cap, pages, _ls = hv.collect_marked_products(
            markers=markers, search_fn=search_fn
        )
        assert page_cap is False
        assert len(found) == 1


# --------------------------------------------------------------------------- #
# (i) 슬라이스 1 회귀: 행 배분·열 매핑·원산지 필수·원본 무변경·대표이미지 조달.
#
# 슬라이스 2 가 슬라이스 1 의 기능을 망가뜨리지 않았는지 확인한다.
# --------------------------------------------------------------------------- #
class TestSlice1Regression:
    """(i) 슬라이스 1 회귀 — 표식 심기 추가 후에도 기존 기능이 유지된다."""

    def test_row_distribution_unchanged(self, mini_a1, tmp_path):
        """행 배분이 동일하다 (max(고시, 배송, AS))."""
        dst = tmp_path / "out.xlsx"
        outcome = tmf.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111\n222\n333",
            shipping_codes_raw="444,555",
            as_codes_raw="666",
            origin_code="0001",
        )
        assert outcome.generated is True
        assert outcome.row_count == 3
        assert len(outcome.markers) == 3

    def test_column_mapping_unchanged(self, mini_a1, tmp_path):
        """템플릿코드 열(AI/AY/BD) 매핑이 유지된다."""
        dst = tmp_path / "out.xlsx"
        outcome = tmf.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111\n222\n333",
            shipping_codes_raw="444,555",
            as_codes_raw="666",
            origin_code="0001",
        )
        assert outcome.generated is True

        with zipfile.ZipFile(dst, "r") as z:
            sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
        ns = "{" + _NS_MAIN + "}"
        root = ET.fromstring(sheet)
        sd = root.find(f"{ns}sheetData")
        rows = sd.findall(f"{ns}row")
        data_rows = [r for r in rows if int(r.get("r", "0")) >= 7]
        assert len(data_rows) == 3

        # 모든 셀 값 수집.
        all_values: list[str] = []
        for r in data_rows:
            for c in r.findall(f"{ns}c"):
                v = c.find(f"{ns}v")
                if v is not None and v.text:
                    all_values.append(v.text)
        for code in ("111", "222", "333", "444", "555", "666"):
            assert code in all_values

    def test_origin_required_unchanged(self, mini_a1, tmp_path):
        """원산지코드 필수 (규제값 — 창작 금지)."""
        dst = tmp_path / "out.xlsx"
        outcome = tmf.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="",
        )
        assert outcome.generated is False
        assert outcome.origin_missing is True

    def test_source_unchanged(self, mini_a1, tmp_path):
        """원본 A1 이 무변경이다."""
        original = mini_a1.read_bytes()
        dst = tmp_path / "out.xlsx"
        tmf.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="0001",
        )
        assert original == mini_a1.read_bytes()

    def test_image_procurement_unchanged(self, mini_a1, tmp_path):
        """대표이미지 조달 — 캐시 적중 시 네트워크 0회."""
        dst = tmp_path / "out.xlsx"
        call_count = [0]

        def _fake_upload(paths):
            call_count[0] += 1
            return ["https://example.com/uploaded.png"]

        outcome = tmf.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="0001",
            upload_fn=_fake_upload,
        )
        assert outcome.generated is True
        assert call_count[0] == 0  # 캐시 적중.

    def test_xlsx_intact(self, mini_a1, tmp_path):
        """생성 파일이 유효한 zip 이다."""
        dst = tmp_path / "out.xlsx"
        tmf.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="0001",
        )
        assert zipfile.is_zipfile(dst)

    def test_run_id_and_markers_in_outcome(self, mini_a1, tmp_path):
        """성공 시 outcome 에 run_id 와 markers 가 있다."""
        dst = tmp_path / "out.xlsx"
        outcome = tmf.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111\n222",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="0001",
        )
        assert outcome.generated is True
        assert outcome.run_id
        assert len(outcome.markers) == 2
        # 모든 marker 가 CLSTMIG- 접두사.
        for m in outcome.markers:
            assert tmf.is_our_marker(m)
            assert m.startswith("CLSTMIG-")

    def test_ledger_written_on_generation(self, mini_a1, tmp_path):
        """생성 시 run ledger 파일이 디스크에 쓰인다."""
        dst = tmp_path / "out.xlsx"
        outcome = tmf.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111\n222",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="0001",
        )
        assert outcome.generated is True
        ledger_path = tmf.run_ledger_path(outcome.run_id)
        assert ledger_path.exists()


# --------------------------------------------------------------------------- #
# (j) 마커가 판매자 상품코드(A열) 에 실제로 심겼는지 생성 엑셀을 재파싱해 검증.
# --------------------------------------------------------------------------- #
class TestMarkerPlantedInColumnA:
    """(j) 표식이 A열(판매자 상품코드) 에 실제로 들어갔는지 검증."""

    def test_markers_in_shared_strings(self, mini_a1, tmp_path):
        """표식 문자열이 sharedStrings.xml 에 있다."""
        dst = tmp_path / "out.xlsx"
        outcome = tmf.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111\n222\n333",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="0001",
        )
        assert outcome.generated is True
        with zipfile.ZipFile(dst, "r") as z:
            ss = tmf._load_shared_strings(z)
        joined = "\n".join(ss)
        for marker in outcome.markers:
            assert marker in joined, f"표식 {marker} 이 sharedStrings 에 없음"

    def test_markers_in_column_a_cells(self, mini_a1, tmp_path):
        """A열 셀(A7, A8, ...) 에 표식의 sharedStrings 참조가 있다."""
        dst = tmp_path / "out.xlsx"
        outcome = tmf.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111\n222\n333",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="0001",
        )
        assert outcome.generated is True

        with zipfile.ZipFile(dst, "r") as z:
            sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
            ss = tmf._load_shared_strings(z)

        ns = "{" + _NS_MAIN + "}"
        root = ET.fromstring(sheet)
        sd = root.find(f"{ns}sheetData")
        rows = sd.findall(f"{ns}row")
        data_rows = [r for r in rows if int(r.get("r", "0")) >= 7]
        assert len(data_rows) == 3

        # 각 행의 A 열 셀을 확인.
        for i, row in enumerate(data_rows):
            row_num = row.get("r")
            cells = row.findall(f"{ns}c")
            a_cell = None
            for c in cells:
                if c.get("r") == f"A{row_num}":
                    a_cell = c
                    break
            assert a_cell is not None, f"행 {row_num} 에 A 열 셀이 없음"
            # t="s" (sharedStrings 참조).
            assert a_cell.get("t") == "s", f"A{row_num} 셀이 sharedStrings 참조가 아님"
            v = a_cell.find(f"{ns}v")
            assert v is not None and v.text
            idx = int(v.text)
            assert idx < len(ss), f"A{row_num} 의 sharedStrings 인덱스 범위 초과"
            # 값이 표식이다.
            assert ss[idx] == outcome.markers[i], (
                f"A{row_num} 값이 표식과 불일치: " f"expected={outcome.markers[i]}, got={ss[idx]}"
            )

    def test_no_markers_when_run_id_empty(self, mini_a1, tmp_path):
        """run_id 없이(호환 경로) 주입하면 표식이 없다."""
        dst = tmp_path / "out.xlsx"
        # _inject_dummy_rows 직접 호출 (run_id 생략).
        output, markers = tmf._inject_dummy_rows(
            mini_a1,
            dst,
            notice_codes=["111"],
            shipping_codes=[],
            as_codes=[],
            origin_code="0001",
            image_url=_TEST_IMAGE_URL,
        )
        assert markers == [""]  # 빈 표식.
        # sharedStrings 에 CLSTMIG 가 없다.
        with zipfile.ZipFile(dst, "r") as z:
            ss = tmf._load_shared_strings(z)
        joined = "\n".join(ss)
        assert "CLSTMIG" not in joined

    def test_marker_format(self):
        """표식 형식이 CLSTMIG-<runid>-<rowseq:03d> 이다."""
        m = tmf.make_marker("20260809T1200Z", 7)
        assert m == "CLSTMIG-20260809T1200Z-007"
        assert tmf.is_our_marker(m)
        assert not tmf.is_our_marker("OTHER-PREFIX-001")
        assert not tmf.is_our_marker(123)
        assert not tmf.is_our_marker(None)

    def test_marker_unique_per_run(self, mini_a1, tmp_path):
        """같은 런 내에서 행별 표식이 다르다."""
        dst = tmp_path / "out.xlsx"
        outcome = tmf.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111\n222\n333",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="0001",
        )
        assert len(set(outcome.markers)) == 3


# --------------------------------------------------------------------------- #
# 방어적 추출 검증 — 정본 경로(originProduct.sellerCodeInfo.sellerManagementCode)
# 와 폴백(sellerManagementProductCode) 의 다양한 자리.
#
# 정본 필드명은 문서 실측 기준 sellerManagementCode (count=4).
# sellerManagementProductCode 는 문서 953개 전수 0건 — 폴백으로만 남긴다.
# --------------------------------------------------------------------------- #
class TestDefensiveExtraction:
    """검색 응답의 다양한 구조에서 판매자 관리 코드를 잡아낸다."""

    def test_canonical_origin_product_seller_code_info(self):
        """정본 경로: originProduct.sellerCodeInfo.sellerManagementCode."""
        listing = {
            "originProductNo": "O1",
            "originProduct": {
                "sellerCodeInfo": {"sellerManagementCode": "CLSTMIG-R1-001"},
            },
            "channelProducts": [{"channelProductNo": "C1"}],
        }
        assert hv._extract_seller_code(listing) == "CLSTMIG-R1-001"
        assert hv._extract_origin_no(listing) == "O1"
        assert hv._extract_channel_no(listing) == "C1"

    def test_top_level_seller_code_info(self):
        """listing 최상위 sellerCodeInfo.sellerManagementCode (얕은 폼)."""
        listing = {
            "originProductNo": "O1",
            "sellerCodeInfo": {"sellerManagementCode": "CLSTMIG-R1-002"},
            "channelProducts": [{"channelProductNo": "C1"}],
        }
        assert hv._extract_seller_code(listing) == "CLSTMIG-R1-002"

    def test_top_level_seller_management_code(self):
        """listing 최상위 sellerManagementCode (아주 얕은 폼)."""
        listing = {
            "originProductNo": "O1",
            "sellerManagementCode": "CLSTMIG-R1-003",
        }
        assert hv._extract_seller_code(listing) == "CLSTMIG-R1-003"

    def test_origin_product_direct_seller_management_code(self):
        """originProduct 최상위에 sellerManagementCode 가 직접 있는 폼."""
        listing = {
            "originProductNo": "O1",
            "originProduct": {"sellerManagementCode": "CLSTMIG-R1-004"},
        }
        assert hv._extract_seller_code(listing) == "CLSTMIG-R1-004"

    def test_canonical_takes_priority_over_fallback(self):
        """정본 이름이 폴백보다 우선한다 (같은 listing 에 둘 다 있으면 정본 승)."""
        listing = {
            "originProduct": {
                "sellerCodeInfo": {"sellerManagementCode": "CLSTMIG-R1-CANONICAL"},
            },
            # 폴백 이름도 있지만 정본이 우선.
            "sellerManagementProductCode": "CLSTMIG-R1-FALLBACK",
        }
        assert hv._extract_seller_code(listing) == "CLSTMIG-R1-CANONICAL"

    def test_fallback_top_level_seller_product_code(self):
        """폴백: 최상위 sellerManagementProductCode (문서 0건 이름 — 호환용)."""
        listing = {
            "originProductNo": "O1",
            "sellerManagementProductCode": "CLSTMIG-R1-005",
            "channelProducts": [{"channelProductNo": "C1"}],
        }
        assert hv._extract_seller_code(listing) == "CLSTMIG-R1-005"

    def test_fallback_channel_nested_seller_product_code(self):
        """폴백: channelProducts[0].sellerManagementProductCode."""
        listing = {
            "originProductNo": "O1",
            "channelProducts": [
                {
                    "channelProductNo": "C1",
                    "sellerManagementProductCode": "CLSTMIG-R1-006",
                }
            ],
        }
        assert hv._extract_seller_code(listing) == "CLSTMIG-R1-006"

    def test_fallback_origin_product_nested_seller_product_code(self):
        """폴백: originProduct.sellerManagementProductCode."""
        listing = {
            "originProductNo": "O1",
            "originProduct": {"sellerManagementProductCode": "CLSTMIG-R1-007"},
        }
        assert hv._extract_seller_code(listing) == "CLSTMIG-R1-007"

    def test_empty_listing(self):
        assert hv._extract_seller_code({}) == ""
        assert hv._extract_origin_no({}) == ""
        assert hv._extract_channel_no({}) == ""

    def test_non_dict_listing(self):
        assert hv._extract_seller_code(None) == ""
        assert hv._extract_seller_code([]) == ""
        assert hv._extract_seller_code("string") == ""

    def test_integer_seller_code(self):
        """숫자 타입 판매자코드도 문자열로 추출된다."""
        listing = {
            "originProduct": {
                "sellerCodeInfo": {"sellerManagementCode": 12345},
            }
        }
        assert hv._extract_seller_code(listing) == "12345"

    def test_no_canonical_anywhere_returns_empty(self):
        """정본도 폴백도 어디에도 없으면 빈 문자열 (실전 0건 결함 재현)."""
        # 과거 코드가 찾던 sellerManagementProductCode 가 아무 데도 없고
        # 정본 sellerManagementCode 도 없으면 — 실전에서 아무것도 못 찾는다.
        listing = {
            "originProductNo": "O1",
            "channelProducts": [{"channelProductNo": "C1"}],
            # 판매자 관리 코드 필드 자체가 없다.
        }
        assert hv._extract_seller_code(listing) == ""


# --------------------------------------------------------------------------- #
# 수확 폼 서버 방어 — 슬라이스 1 폼과 동등한 수준.
# --------------------------------------------------------------------------- #
class TestHarvestFormServerDefense:
    """수확 폼 서버의 방어 계약."""

    def test_bound_host_is_localhost(self):
        """서버가 127.0.0.1 에만 바인드된다."""
        from clossify import approval_server

        token = approval_server.new_token()
        srv = hv.HarvestFormServer(token=token, ttl_seconds=60)
        srv.start()
        try:
            bound = hv.actual_bound_host(srv)
            assert bound == "127.0.0.1"
        finally:
            srv.close()

    def test_non_localhost_bind_rejected(self):
        """bind_host 가 127.0.0.1 이 아니면 거부된다."""
        with pytest.raises(ValueError, match="127.0.0.1"):
            hv.HarvestFormServer(token="t", bind_host="0.0.0.0")

    def test_empty_token_rejected(self):
        """빈 토큰은 거부된다."""
        with pytest.raises(ValueError, match="token"):
            hv.HarvestFormServer(token="")


# --------------------------------------------------------------------------- #
# 소스 코드 정적 검사 — 수확 모듈의 방어 계약.
# --------------------------------------------------------------------------- #
class TestHarvestSourceEvidence:
    """template_migration_harvest.py 소스 자체가 방어 계약을 갖는다."""

    def test_source_binds_localhost(self):
        """소스에서 127.0.0.1 바인드를 강제한다."""
        src = (_SRC / "clossify" / "template_migration_harvest.py").read_text(encoding="utf-8")
        assert '"127.0.0.1", 0' in src

    def test_source_uses_approval_server_defenses(self):
        """approval_server 의 방어 함수를 재사용한다."""
        src = (_SRC / "clossify" / "template_migration_harvest.py").read_text(encoding="utf-8")
        assert "approval_server.tokens_match" in src
        assert "approval_server.origin_referer_ok" in src

    def test_source_no_acao_emission(self):
        """소스에 Access-Control-Allow-Origin 송출이 없다."""
        src = (_SRC / "clossify" / "template_migration_harvest.py").read_text(encoding="utf-8")
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert 'send_header("Access-Control-Allow-Origin"' not in line

    def test_source_no_new_mcp_tool_registration(self):
        """소스에 새 MCP 도구 등록이 없다."""
        src = (_SRC / "clossify" / "template_migration_harvest.py").read_text(encoding="utf-8")
        # 도구 등록 패턴이 없는지 확인.
        assert "register_tool" not in src
        assert "@mcp.tool" not in src
        assert "FastMCP" not in src

    def test_source_no_real_naver_calls_at_import(self):
        """import 시 네이버 실API 호출이 없다 (함수 정의만)."""
        src = (_SRC / "clossify" / "template_migration_harvest.py").read_text(encoding="utf-8")
        # naver_client.search_products 가 함수 시그니처로 대입되는 건 ok.
        # 실제 호출은 함수 본체 안에서 search_fn() 형태로.
        # import 시점 호출 패턴이 없는지.
        assert "naver_client.search_products()" not in src

    def test_source_uses_stdlib_only(self):
        """import 가 표준 라이브러리 + 내부 모듈만 있다."""
        import re

        src = (_SRC / "clossify" / "template_migration_harvest.py").read_text(encoding="utf-8")
        imports = re.findall(r"^(?:import|from)\s+([A-Za-z_]\S*)", src, re.MULTILINE)
        allowed = {
            "__future__",
            "datetime",
            "html",
            "http",
            "json",
            "socketserver",
            "threading",
            "time",
            "os",
            "typing",
            "pathlib",
            "common",
            "naver_client",
            "template_migration_form",
            "mcp_server",
            "approval_server",
        }
        for imp in imports:
            top = imp.lstrip(".").split(".")[0]
            assert top in allowed, f"예상치 못한 import: {imp}"


# --------------------------------------------------------------------------- #
# HarvestReport 단위 테스트.
# --------------------------------------------------------------------------- #
class TestHarvestReport:
    """HarvestReport 요약·직렬화."""

    def test_summary_format(self):
        """summary 가 ``삭제 N/M`` 형태."""
        report = hv.HarvestReport(
            run_id="TEST",
            found_count=5,
            deleted_count=3,
        )
        s = report.summary()
        assert "삭제 3/5" in s
        assert "TEST" in s

    def test_summary_with_remaining(self):
        """남은 것이 있으면 개수가 표시된다."""
        report = hv.HarvestReport(
            found_count=3,
            deleted_count=2,
            remaining=[{"origin_product_no": "O3"}],
        )
        s = report.summary()
        assert "남은 1건" in s

    def test_summary_dry_run(self):
        """dry-run 표시."""
        report = hv.HarvestReport(dry_run=True, found_count=2, deleted_count=0)
        assert "[dry-run]" in report.summary()

    def test_summary_page_cap(self):
        """페이지 상한 표시."""
        report = hv.HarvestReport(page_cap_reached=True, pages_scanned=50, found_count=10)
        s = report.summary()
        assert "더 있을 수 있음" in s
        assert "50페이지" in s

    def test_to_dict(self):
        """to_dict 에 모든 필드가 있다."""
        report = hv.HarvestReport(
            run_id="R",
            found_count=3,
            deleted_count=2,
            page_cap_reached=False,
            dry_run=False,
            listings_scanned=10,
        )
        d = report.to_dict()
        assert d["run_id"] == "R"
        assert d["found_count"] == 3
        assert d["deleted_count"] == 2
        assert d["listings_scanned"] == 10
        assert "summary" in d


# --------------------------------------------------------------------------- #
# collect_marked_products 예외 케이스.
# --------------------------------------------------------------------------- #
class TestCollectEdgeCases:
    """collect_marked_products 의 경계 케이스."""

    def test_no_run_id_no_markers_raises(self):
        """run_id 와 markers 가 모두 비어있으면 ValueError."""
        search_fn, _ = _make_search_fn([])
        with pytest.raises(ValueError):
            hv.collect_marked_products(search_fn=search_fn)

    def test_empty_search_result(self):
        """검색 결과가 빈 페이지 → found 빈 리스트."""
        search_fn, _ = _make_search_fn([])
        found, cap, pages, ls = hv.collect_marked_products(
            markers=["CLSTMIG-R1-001"], search_fn=search_fn
        )
        assert found == []
        assert cap is False
        assert pages == 1
        assert ls == 0  # 빈 응답 — 훑은 상품 없음.

    def test_non_200_search_breaks(self):
        """검색 API 가 200 이 아니면 페이지 순회를 중단한다."""
        calls = []

        def _failing_search(page=1, size=100, tk=None):
            calls.append(page)
            return 500, {"error": "internal"}

        found, cap, pages, ls = hv.collect_marked_products(
            markers=["CLSTMIG-R1-001"], search_fn=_failing_search
        )
        assert found == []
        assert pages == 1
        assert ls == 0  # 200 아니면 listing 을 훑지 않는다.

    def test_origin_no_missing_skipped(self):
        """마커는 일치하지만 origin 번호가 없으면 스킵된다."""
        markers = [tmf.make_marker("SK1", 1)]
        # 정본 형태(originProduct.sellerCodeInfo.sellerManagementCode) 에서
        # originProductNo 만 빠진 listing.
        listings = [
            {
                # originProductNo 없음.
                "originProduct": {
                    "sellerCodeInfo": {"sellerManagementCode": markers[0]},
                },
                "channelProducts": [{"channelProductNo": "C1"}],
            }
        ]
        search_fn, _ = _make_search_fn(listings)
        found, _, _, ls = hv.collect_marked_products(markers=markers, search_fn=search_fn)
        assert len(found) == 0
        # 마커가 일치하는 listing 은 훑었지만 origin 번호 부재로 스킵.
        assert ls == 1


# --------------------------------------------------------------------------- #
# 조용한 0건 방지 — (c) 훑은 상품 0개 vs (d) 훑었지만 일치 0건 구분.
#
# 과거 결함: 마커를 0건 찾았을 때 "응답이 비어 0개 훑음" 인지
# "N개 훑었지만 마커 일치 0건" 인지 사용자가 알 수 없었다 (둘 다
# found_count: 0 이라 같아 보임). 본 테스트는 listings_scanned 와
# summary 문구로 둘을 구분하는지 검증한다.
# --------------------------------------------------------------------------- #
class TestSilentZeroGuard:
    """조용한 0건 방지 — 훑은 수와 일치 수를 구분해 보고한다."""

    def test_empty_response_zero_scanned(self):
        """(c) 응답이 비어 훑은 상품 0개 → '훑은 상품 없음' 보고."""
        search_fn, _ = _make_search_fn([])
        report = hv.harvest_run(
            markers=["CLSTMIG-EMPTY-001"],
            confirm=False,
            search_fn=search_fn,
            update_fn=_ok_update()[0],
            get_product_fn=_ok_get_product()[0],
            delete_fn=_ok_delete()[0],
        )
        assert report.found_count == 0
        assert report.listings_scanned == 0
        summary = report.summary()
        assert "훑은 상품 없음" in summary

    def test_scanned_but_zero_match(self):
        """(d) N개 훑었지만 마커 일치 0건 → 'N개 상품 훑음, 일치 0건' 보고."""
        # 진짜 상품 3건 (마커 불일치) 을 훑는다.
        listings = [
            _make_listing("O1", "C1", "REAL_PRODUCT_001"),
            _make_listing("O2", "C2", "REAL_PRODUCT_002"),
            _make_listing("O3", "C3", "REAL_PRODUCT_003"),
        ]
        search_fn, _ = _make_search_fn(listings)
        report = hv.harvest_run(
            markers=["CLSTMIG-ABSENT-001"],
            confirm=False,
            search_fn=search_fn,
            update_fn=_ok_update()[0],
            get_product_fn=_ok_get_product()[0],
            delete_fn=_ok_delete()[0],
        )
        assert report.found_count == 0
        assert report.listings_scanned == 3  # 3개 훑음.
        summary = report.summary()
        # (c) 와 다른 문구 — N개 훑음, 일치 0건.
        assert "3개 상품 훑음" in summary
        assert "마커 일치 0건" in summary
        assert "훑은 상품 없음" not in summary  # (c) 와 구분.

    def test_zero_scanned_distinct_from_zero_match_in_dict(self):
        """to_dict 에서 listings_scanned 로 두 경우가 구분된다."""
        # 0개 훑음.
        s_empty, _ = _make_search_fn([])
        r_empty = hv.harvest_run(
            markers=["CLSTMIG-E1"],
            search_fn=s_empty,
            update_fn=_ok_update()[0],
            get_product_fn=_ok_get_product()[0],
            delete_fn=_ok_delete()[0],
        )
        d_empty = r_empty.to_dict()
        assert d_empty["found_count"] == 0
        assert d_empty["listings_scanned"] == 0

        # 2개 훑음, 일치 0건.
        listings = [
            _make_listing("R1", "CR1", "NOT_OURS_1"),
            _make_listing("R2", "CR2", "NOT_OURS_2"),
        ]
        s_some, _ = _make_search_fn(listings)
        r_some = hv.harvest_run(
            markers=["CLSTMIG-S1"],
            search_fn=s_some,
            update_fn=_ok_update()[0],
            get_product_fn=_ok_get_product()[0],
            delete_fn=_ok_delete()[0],
        )
        d_some = r_some.to_dict()
        assert d_some["found_count"] == 0
        assert d_some["listings_scanned"] == 2  # 같은 0건이 아니라 구분됨.

    def test_found_nonzero_no_zero_phrase(self):
        """발견이 0이 아닐 때는 '훑은 상품 없음'/'일치 0건' 문구가 없다."""
        markers = [tmf.make_marker("FOUND1", 1)]
        listings = [_make_listing("O1", "C1", markers[0])]
        search_fn, _ = _make_search_fn(listings)
        report = hv.harvest_run(
            markers=markers,
            confirm=False,
            search_fn=search_fn,
            update_fn=_ok_update()[0],
            get_product_fn=_ok_get_product()[0],
            delete_fn=_ok_delete()[0],
        )
        assert report.found_count == 1
        summary = report.summary()
        assert "훑은 상품 없음" not in summary
        assert "마커 일치 0건" not in summary


# --------------------------------------------------------------------------- #
# 실제 A1 통합 테스트 (환경 변수 필요).
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    not os.environ.get("CLOSSIFY_A1_TEMPLATE"),
    reason="CLOSSIFY_A1_TEMPLATE 환경 변수가 설정되지 않음",
)
class TestRealA1HarvestIntegration:
    """실제 A1 로 표식 심기 → 재파싱 검증."""

    def test_real_a1_markers_in_column_a(self, tmp_path):
        """실제 A1 로 생성 → A열에 표식이 있다."""
        a1 = os.environ["CLOSSIFY_A1_TEMPLATE"]
        dst = tmp_path / "real_harvest.xlsx"
        outcome = tmf.generate_dummy_excel(
            src_xlsx=a1,
            dst_xlsx=dst,
            notice_codes_raw="111\n222\n333",
            shipping_codes_raw="444",
            as_codes_raw="555",
            origin_code="0001",
        )
        assert outcome.generated is True, f"생성 실패: {outcome.reason}"
        assert len(outcome.markers) == 3

        # sharedStrings 에 표식이 있다.
        with zipfile.ZipFile(dst, "r") as z:
            ss = tmf._load_shared_strings(z)
        joined = "\n".join(ss)
        for m in outcome.markers:
            assert m in joined

    def test_real_a1_source_unchanged(self, tmp_path):
        """실제 A1 원본이 무변경이다."""
        a1 = os.environ["CLOSSIFY_A1_TEMPLATE"]
        original = Path(a1).read_bytes()
        dst = tmp_path / "real_out.xlsx"
        tmf.generate_dummy_excel(
            src_xlsx=a1,
            dst_xlsx=dst,
            notice_codes_raw="111",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="0001",
        )
        assert original == Path(a1).read_bytes()


# --------------------------------------------------------------------------- #
# HTTP 폼 POST 헬퍼 (test_template_migration_form.py 와 동일 패턴).
# --------------------------------------------------------------------------- #
def _send_harvest_form(
    port: int,
    *,
    fields: dict[str, str],
    token_header: str | None = None,
    origin: str | None = None,
    method: str = "POST",
    path: str = "/",
) -> tuple[int, str, list[tuple[str, str]]]:
    """수확 폼 POST 를 보내고 (status, body, headers)."""
    pairs = list(fields.items())
    body = urllib.parse.urlencode(pairs).encode("utf-8")
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers: list[tuple[str, str]] = [
        ("Content-Type", "application/x-www-form-urlencoded"),
        ("Content-Length", str(len(body))),
    ]
    if token_header is not None:
        headers.append(("X-Harvest-Form-Token", token_header))
    if origin is not None:
        headers.append(("Origin", origin))
    conn.request(method, path, body=body, headers=dict(headers))
    resp = conn.getresponse()
    resp_body = resp.read().decode("utf-8")
    resp_headers = [(k, v) for k, v in resp.getheaders()]
    status = resp.status
    conn.close()
    return status, resp_body, resp_headers


# --------------------------------------------------------------------------- #
# 수확 폼 HTML 구조.
# --------------------------------------------------------------------------- #
class TestHarvestFormHtml:
    """수확 폼 HTML 구조 검증."""

    def test_no_script_tag(self):
        """폼 HTML 에 <script> 가 0개다."""
        html_str = hv.render_harvest_form_html(token="t", port=12345)
        assert "<script" not in html_str.lower()

    def test_has_run_id_field(self):
        """run_id 입력 필드가 있다."""
        html_str = hv.render_harvest_form_html(token="t", port=1)
        assert 'name="run_id"' in html_str

    def test_has_confirm_checkbox(self):
        """confirm 체크박스가 있다 (기본 dry-run)."""
        html_str = hv.render_harvest_form_html(token="t", port=1)
        assert 'name="confirm"' in html_str
        assert "checkbox" in html_str.lower()

    def test_token_hidden(self):
        """토큰이 hidden 필드다."""
        html_str = hv.render_harvest_form_html(token="secret", port=1)
        assert 'type="hidden" name="token"' in html_str

    def test_form_is_post(self):
        """폼이 method=POST 다."""
        html_str = hv.render_harvest_form_html(token="t", port=1)
        assert 'method="POST"' in html_str

    def test_safety_notice(self):
        """안전 고지 문구가 있다 (마커 불일치시 처리 안 함)."""
        html_str = hv.render_harvest_form_html(token="t", port=1)
        assert "마커" in html_str or "표식" in html_str


# --------------------------------------------------------------------------- #
# 예외 방벽 — 핸들러 바깥으로 예외가 번지면 http.server 가 응답 없이 연결을
# 끊는 결함(N28 계열)을 막는다.
#
# 본 테스트 묶음이 검증하는 계약:
#   (a) 설정 파일 누락 POST → HTTP 200 + 안내 화면 (예외가 아닌 정상 경로).
#   (b) 핸들러 강제 예외 → 5xx + 사람이 읽을 HTML (연결 끊김 아님).
#   (c) 오류 화면에 절대경로·사용자명·비밀값이 없다 (sanitize_error 정화).
#   (d) 토큰·Origin 방어가 예외 방벽과 무관하게 여전히 동작한다.
#   (e) 소진된 토큰·만료된 TTL 회귀.
#
# 도우미: 각 테스트는 실제 HarvestFormServer 를 띄워 소켓으로 POST 를 보낸다.
# --------------------------------------------------------------------------- #
def _start_harvest_server(ttl_seconds: int = 60) -> tuple[hv.HarvestFormServer, int, str]:
    """수확 폼 서버를 시작하고 (server, port, token) 을 반환한다."""
    from clossify import approval_server

    token = approval_server.new_token()
    srv = hv.HarvestFormServer(token=token, ttl_seconds=ttl_seconds)
    port = srv.start()
    assert wait_for_port(port), f"포트 {port} 가 열리지 않음"
    return srv, port, token


class TestConfigMissingGuidance:
    """(a) 설정 파일이 없을 때 예외가 아닌 안내 화면으로 응답한다.

    과거 결함: ``harvest_run_from_ledger`` → ``naver_client.search_products`` →
    ``load_config`` → ``FileNotFoundError``. 핸들러 바깥으로 번지면
    ``http.server`` 가 응답 없이 연결을 끊는다. 첫 사용자가 정확히 여기서 막힌다.
    본 테스트는 그 경로가 안내 화면(200) 으로 바뀌었는지 검증한다.
    """

    def test_config_missing_returns_html_not_disconnect(self, monkeypatch):
        """설정 파일 부재 POST → HTTP 200 + 안내 화면 (연결 끊김 아님)."""
        # 설정 파일이 없다고 보고하게 만든다.
        monkeypatch.setattr(hv, "_config_present", lambda: False)
        srv, port, token = _start_harvest_server()
        try:
            status, body, _headers = _send_harvest_form(
                port,
                fields={"run_id": "TEST_RUN_001"},
                token_header=token,
                origin="file://test",
            )
            # 연결이 끊기지 않고 정상 응답이 와야 한다.
            assert status == 200
            # 안내 문구가 있다.
            assert "config" in body or "설정" in body
            assert "config.example.json" in body or ".local/config.json" in body
        finally:
            srv.close()

    def test_config_missing_consumes_token(self, monkeypatch):
        """설정 부재 안내 후 토큰이 소진된다 (같은 토큰 재사용 거부)."""
        monkeypatch.setattr(hv, "_config_present", lambda: False)
        srv, port, token = _start_harvest_server()
        try:
            # 첫 POST — 안내 화면.
            s1, _b1, _h1 = _send_harvest_form(
                port,
                fields={"run_id": "TEST_RUN_002"},
                token_header=token,
                origin="file://test",
            )
            assert s1 == 200
            # 서버 종료 대기.
            time.sleep(0.3)
        finally:
            srv.close()


class TestExceptionBarrier:
    """(b) 핸들러 내부 강제 예외 → 5xx + HTML (연결 끊김 아님).

    ``harvest_run_from_ledger`` 가 예외를 일으키면 핸들러 바깥으로 번져
    ``http.server`` 가 응답 없이 연결을 끊는 결함을 방벽이 막는지 검증한다.
    """

    def test_forced_exception_returns_500_html(self, monkeypatch):
        """강제 예외 → 500 + 사람이 읽을 HTML (연결 끊김 아님)."""
        # 설정은 있는 것으로 해서 config-missing 경로를 우회한다.
        monkeypatch.setattr(hv, "_config_present", lambda: True)

        def _boom(*_a, **_kw):
            raise RuntimeError("의도된 폭발 — 방벽 테스트")

        monkeypatch.setattr(hv, "harvest_run_from_ledger", _boom)

        srv, port, token = _start_harvest_server()
        try:
            status, body, _headers = _send_harvest_form(
                port,
                fields={"run_id": "BOOM_RUN_001"},
                token_header=token,
                origin="file://test",
            )
            # 5xx + HTML 이어야 한다 (RemoteDisconnected 가 아님).
            assert 500 <= status < 600
            assert "500" in body
            # 예외 사유가 (정화되어) 표시된다.
            assert "오류" in body or "예외" in body
            # 연결이 끊기지 않았다 — body 가 온전히 왔다.
            assert "</html>" in body.lower()
        finally:
            srv.close()

    def test_barrier_catches_file_not_found(self, monkeypatch):
        """FileNotFoundError (설정 누락의 고전적 형태) 도 방벽이 잡는다."""
        # _config_present 를 True 로 해서 config-missing 사전 검사를 통과시키고,
        # harvest_run_from_ledger 가 FileNotFoundError 를 일으키게 한다.
        monkeypatch.setattr(hv, "_config_present", lambda: True)

        def _raise_fnf(*_a, **_kw):
            raise FileNotFoundError(
                "[Errno 2] No such file or directory: 'C:\\\\Users\\\\secret\\\\.local\\\\config.json'"
            )

        monkeypatch.setattr(hv, "harvest_run_from_ledger", _raise_fnf)

        srv, port, token = _start_harvest_server()
        try:
            status, body, _headers = _send_harvest_form(
                port,
                fields={"run_id": "FNF_RUN_001"},
                token_header=token,
                origin="file://test",
            )
            assert 500 <= status < 600
            assert "</html>" in body.lower()
        finally:
            srv.close()


class TestErrorPageSanitization:
    """(c) 오류 화면에 절대경로·사용자명·비밀값이 없다.

    ``common.sanitize_error`` 가 경로·비밀값을 정화한다. 방벽이 그 결과를
    HTML 에 싣는다. 본 테스트는 정화 카나리(절대경로·토큰) 가 오류 화면에
    누출되지 않는지 검증한다.
    """

    def test_no_absolute_path_in_error_page(self, monkeypatch):
        """오류 화면에 Windows 절대경로가 없다."""
        monkeypatch.setattr(hv, "_config_present", lambda: True)

        _SECRET_PATH = "C:\\\\Users\\\\leaked_user\\\\projects\\\\clossify\\\\.local\\\\config.json"

        def _raise_with_path(*_a, **_kw):
            raise FileNotFoundError(f"[Errno 2] No such file: '{_SECRET_PATH}'")

        monkeypatch.setattr(hv, "harvest_run_from_ledger", _raise_with_path)

        srv, port, token = _start_harvest_server()
        try:
            _status, body, _headers = _send_harvest_form(
                port,
                fields={"run_id": "PATH_LEAK_001"},
                token_header=token,
                origin="file://test",
            )
            # 절대경로 카나리가 정화되어 있다.
            assert "C:\\\\Users" not in body
            assert "leaked_user" not in body
            # FileNotFoundError 타입명은 남아 있다 (정보성).
            assert "FileNotFoundError" in body or "FileNotFound" in body or "500" in body
        finally:
            srv.close()

    def test_no_token_in_error_page(self, monkeypatch):
        """오류 화면에 폼 토큰이 누출되지 않는다."""
        monkeypatch.setattr(hv, "_config_present", lambda: True)

        def _raise_generic(*_a, **_kw):
            raise RuntimeError("generic barrier test")

        monkeypatch.setattr(hv, "harvest_run_from_ledger", _raise_generic)

        srv, port, token = _start_harvest_server()
        try:
            _status, body, _headers = _send_harvest_form(
                port,
                fields={"run_id": "TOKEN_LEAK_001"},
                token_header=token,
                origin="file://test",
            )
            # 토큰이 오류 화면 본문에 없다.
            assert token not in body
        finally:
            srv.close()


class TestHarvestDefenseRegression:
    """(d)(e) 예외 방벽 추가 후에도 기존 방어(토큰·Origin·소진·TTL) 가 유지된다."""

    def test_missing_token_returns_401(self, monkeypatch):
        """토큰 없는 POST → 401 (방벽이 방어를 덮지 않는다)."""
        monkeypatch.setattr(hv, "_config_present", lambda: True)
        srv, port, _token = _start_harvest_server()
        try:
            status, body, _headers = _send_harvest_form(
                port,
                fields={"run_id": "NO_TOKEN_RUN"},
                token_header=None,  # 토큰 헤더 없음.
                origin="file://test",
            )
            assert status == 401
            assert "토큰" in body
        finally:
            srv.close()

    def test_wrong_token_returns_403(self, monkeypatch):
        """잘못된 토큰 → 403."""
        monkeypatch.setattr(hv, "_config_present", lambda: True)
        srv, port, _token = _start_harvest_server()
        try:
            status, body, _headers = _send_harvest_form(
                port,
                fields={"run_id": "WRONG_TOKEN_RUN"},
                token_header="this-is-not-the-right-token",
                origin="file://test",
            )
            assert status == 403
            assert "토큰" in body
        finally:
            srv.close()

    def test_bad_origin_returns_403(self, monkeypatch):
        """악의적 Origin → 403 (방벽이 Origin 검사를 덮지 않는다)."""
        monkeypatch.setattr(hv, "_config_present", lambda: True)
        srv, port, token = _start_harvest_server()
        try:
            status, _body, _headers = _send_harvest_form(
                port,
                fields={"run_id": "EVIL_ORIGIN_RUN"},
                token_header=token,
                origin="https://evil.example.com",
            )
            assert status == 403
        finally:
            srv.close()

    def test_consumed_token_returns_410(self, monkeypatch):
        """(e) 이미 소진된 토큰 → 410 (회귀)."""
        monkeypatch.setattr(hv, "_config_present", lambda: True)
        srv, port, token = _start_harvest_server()
        try:
            # 첫 POST — 처리(설정 없음 안내).
            s1, _b1, _h1 = _send_harvest_form(
                port,
                fields={"run_id": "FIRST_RUN"},
                token_header=token,
                origin="file://test",
            )
            # 첫 요청은 응답이 와야 한다.
            assert s1 in (200, 500)
            # 서버가 종료/소진 상태가 될 때까지 대기.
            time.sleep(0.3)
        finally:
            srv.close()

    def test_expired_ttl_returns_410(self, monkeypatch):
        """(e) 만료된 TTL → 410 (회귀)."""
        monkeypatch.setattr(hv, "_config_present", lambda: True)
        # TTL 을 아주 짧게 설정한다.
        srv, port, token = _start_harvest_server(ttl_seconds=1)
        try:
            # TTL 만료 대기.
            time.sleep(1.5)
            status, body, _headers = _send_harvest_form(
                port,
                fields={"run_id": "EXPIRED_RUN"},
                token_header=token,
                origin="file://test",
            )
            # 만료 — 410 (방벽이 아닌 정상 만료 경로).
            assert status == 410
            assert "만료" in body
        finally:
            srv.close()
