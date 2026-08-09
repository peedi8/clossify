# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""템플릿 이관 더미 엑셀 생성 폼 테스트.

본 파일은 두 층으로 구성된다:

1. **단위 테스트** (환경 독립): mini-xlsx 픽스처를 생성해 주입 로직·검증·
   원본 무변경·행 배분·코드 검증 등을 검증한다. 실제 A1 이 없어도 동작한다.
2. **통합 테스트** (실제 A1 필요): ``CLOSSIFY_A1_TEMPLATE`` 환경 변수로
   실제 A1 경로가 주어지면 실물로 생성·재파싱 검증을 수행한다. 없으면 skip.

테스트 목록(티켓 a~g 대응):
  (a) 폼 HTML: <script> 0 · 입력 필드 구성 · 고지 문구 존재.
  (b) 코드 3+2+1 개 입력 → 생성 엑셀에 행 3개, 각 코드 1회 이상 등장.
  (c) 생성 파일을 zipfile 로 재파싱 → 주입 행이 읽힌다 (깨짐 검증).
  (d) 원산지코드 미입력 → 생성 거부 + 사유. 예시값·기본값 없음.
  (e) 이상 코드 줄 → 탈락 사유 명시 (조용한 드롭 없음).
  (f) 사용자 A1 원본 파일 무변경 (바이트 동일).
  (g) 비밀값 미출력 · 1회용 토큰 · 127.0.0.1 (방어 회귀).
"""

from __future__ import annotations

import http.client
import os
import re
import socket
import sys
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import approval_server, template_migration_form

# 테스트 전역: 대표이미지 조달이 네이버 실업로드를 시도하지 않도록 캐시를
# monkeypatch 한다. 개별 테스트에서 upload_fn/main_image_url 로 덮을 수 있다.
_TEST_IMAGE_URL = "https://shop-phinf.pstatic.net/test_dummy_image.jpg"


@pytest.fixture(autouse=True)
def _stub_image_cache(monkeypatch):
    """대표이미지 캐시를 테스트용 URL 로 대체한다.

    generate_dummy_excel 이 네이버 실업로드를 시도하지 않게 한다.
    실업로드/실패 경로는 별도 테스트(ContractImageResolve)에서 upload_fn 으로
    직접 검증한다.
    """
    monkeypatch.setattr(
        template_migration_form,
        "_read_cached_image_url",
        lambda: _TEST_IMAGE_URL,
    )


# --------------------------------------------------------------------------- #
# Mini-xlsx 픽스처 — 실제 A1 구조를 최소한으로 흉내낸 테스트용 엑셀.
#
# 실제 A1 (ExcelSaveTemplate_20260324.xlsx) 의 구조:
#   행 1: 섹션 헤더
#   행 2: 컬럼 헤더
#   행 3: 필수/비필수 표기
#   행 4: 예시 행
#   행 5-6: 가이드
#   행 7+: 데이터 영역
#
# 본 픽스처는 sharedStrings + sheet1.xml 의 최소 구조만 갖춘다.
# 템플릿코드 열(AI/AY/BD)과 필수 열(B/C/E/L/W/Y/AD)의 위치가 실제 A1 과
# 동일하다. 실제 A1 의 복잡한 스타일·데이터검증·하이퍼링크는 생략한다.
# --------------------------------------------------------------------------- #
_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _build_mini_xlsx(path: Path) -> Path:
    """테스트용 mini-xlsx 를 생성한다. 실제 A1 의 열 구조를 따른다."""
    # sharedStrings — 최소 문자열들.
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

    # sheet1.xml — 행 2(헤더)와 행 3(필수표기)만 최소한으로.
    # 열 구조는 실제 A1 과 동일 (B/C/E/L/W/Y/AD/AI/AY/BD).
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{_NS_MAIN}">'
        '<dimension ref="A1:CO6"/>'
        "<sheetData>"
        # 행 2 — 헤더.
        '<row r="2" spans="1:30">'
        '<c r="B2" t="s"><v>1</v></c>'  # 상품명
        '<c r="C2" t="s"><v>1</v></c>'  # 상품명 (예시)
        '<c r="E2" t="s"><v>2</v></c>'  # 판매가
        '<c r="L2" t="s"><v>3</v></c>'  # 재고수량
        '<c r="W2" t="s"><v>4</v></c>'  # 대표이미지
        '<c r="Y2" t="s"><v>5</v></c>'  # 상세설명
        '<c r="AD2" t="s"><v>6</v></c>'  # 원산지코드
        '<c r="AI2" t="s"><v>7</v></c>'  # 배송비 템플릿코드
        '<c r="AY2" t="s"><v>8</v></c>'  # 고시 템플릿코드
        '<c r="BD2" t="s"><v>9</v></c>'  # AS 템플릿코드
        "</row>"
        # 행 3 — 필수표기.
        '<row r="3" spans="1:30">'
        '<c r="B3" t="s"><v>10</v></c>'  # 필수
        '<c r="C3" t="s"><v>10</v></c>'  # 필수
        '<c r="E3" t="s"><v>10</v></c>'  # 필수
        '<c r="L3" t="s"><v>10</v></c>'  # 필수
        '<c r="W3" t="s"><v>10</v></c>'  # 필수
        '<c r="Y3" t="s"><v>10</v></c>'  # 필수
        '<c r="AD3" t="s"><v>10</v></c>'  # 필수
        '<c r="AI3" t="s"><v>11</v></c>'  # 비필수
        '<c r="AY3" t="s"><v>11</v></c>'  # 비필수
        '<c r="BD3" t="s"><v>11</v></c>'  # 비필수
        "</row>"
        "</sheetData>"
        "</worksheet>"
    )

    # workbook.xml.
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{_NS_MAIN}">'
        '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )

    # [Content_Types].xml.
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

    # .rels.
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )

    # workbook.xml.rels.
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


@pytest.fixture
def real_a1_path() -> str:
    """실제 A1 경로 (환경 변수로 제공된 경우만)."""
    return str(os.environ.get("CLOSSIFY_A1_TEMPLATE") or "").strip()


# --------------------------------------------------------------------------- #
# (a) 폼 HTML: <script> 0 · 입력 필드 구성 · 고지 문구 존재.
# --------------------------------------------------------------------------- #
class TestFormHtmlStructure:
    """(a) 폼 HTML 의 구조 검증."""

    def _render(self) -> str:
        return template_migration_form.render_template_migration_form_html(
            token="dummy_token",
            port=54321,
            src_xlsx_path="/tmp/a1.xlsx",
        )

    def test_no_script_tag(self):
        """폼 HTML 에 <script> 태그가 0개다."""
        html_str = self._render()
        assert "<script" not in html_str.lower()
        assert "</script>" not in html_str.lower()

    def test_has_notice_codes_field(self):
        """고시 템플릿코드 입력 필드가 있다."""
        html_str = self._render()
        assert 'name="notice_codes"' in html_str
        assert "textarea" in html_str.lower()

    def test_has_shipping_codes_field(self):
        """배송비 템플릿코드 입력 필드가 있다."""
        html_str = self._render()
        assert 'name="shipping_codes"' in html_str

    def test_has_as_codes_field(self):
        """AS 템플릿코드 입력 필드가 있다."""
        html_str = self._render()
        assert 'name="as_codes"' in html_str

    def test_has_origin_code_field_required(self):
        """원산지코드 필드가 있고 필수 표시가 있다."""
        html_str = self._render()
        assert 'name="origin_code"' in html_str
        assert "[필수]" in html_str

    def test_has_category_code_field_optional(self):
        """카테고리코드 필드가 있다 (선택)."""
        html_str = self._render()
        assert 'name="category_code"' in html_str

    def test_has_main_image_url_field_optional(self):
        """대표이미지 주소 필드가 있다 (선택)."""
        html_str = self._render()
        assert 'name="main_image_url"' in html_str

    def test_has_provisional_registration_notice(self):
        """가등록 고지 문구가 있다."""
        html_str = self._render()
        assert "가등록" in html_str
        assert "판매중지" in html_str
        assert "삭제" in html_str

    def test_has_1row_pilot_recommendation(self):
        """1행 파일럿 권고 문구가 있다."""
        html_str = self._render()
        assert "1행" in html_str or "파일럿" in html_str

    def test_form_is_post(self):
        """폼이 method=POST 다."""
        html_str = self._render()
        assert 'method="POST"' in html_str

    def test_token_is_hidden(self):
        """토큰이 hidden 필드로 싣는다."""
        html_str = self._render()
        assert 'type="hidden" name="token"' in html_str

    def test_no_example_origin_value(self):
        """원산지코드에 구체적 예시값이 없다 (규제값 창작 금지)."""
        html_str = self._render()
        assert "예: 0001" not in html_str
        assert "예: 국산" not in html_str
        # placeholder 에는 형식/필수여부만 있어야 한다.
        assert "원산지 찾기" in html_str or "숫자" in html_str

    def test_no_fetch_or_xhr(self):
        """fetch/XMLHttpRequest 가 없다."""
        html_str = self._render()
        assert "fetch(" not in html_str
        assert "XMLHttpRequest" not in html_str

    def test_submit_button_is_real_submit(self):
        """생성 버튼이 type=submit 이다."""
        html_str = self._render()
        btns = re.findall(r'<button[^>]*type="submit"[^>]*>([^<]*)</button>', html_str)
        assert any("엑셀 생성" in b for b in btns)


# --------------------------------------------------------------------------- #
# (b) 코드 3+2+1 개 입력 → 생성 엑셀에 행 3개, 각 코드 1회 이상 등장.
# --------------------------------------------------------------------------- #
class TestRowDistribution:
    """(b) 행 배분 — max(고시, 배송, AS) 행, 각 코드 1회 이상."""

    def test_3_codes_generates_3_rows(self, mini_a1: Path, tmp_path: Path):
        """고시 3 + 배송 2 + AS 1 → 행 3개."""
        dst = tmp_path / "output.xlsx"
        outcome = template_migration_form.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111\n222\n333",
            shipping_codes_raw="444,555",
            as_codes_raw="666",
            origin_code="0001",
        )
        assert outcome.generated is True, f"생성 실패: {outcome.reason}"
        assert outcome.row_count == 3
        assert dst.is_file()

    def test_each_code_appears_at_least_once(self, mini_a1: Path, tmp_path: Path):
        """각 코드가 생성된 엑셀에 최소 1회 등장한다."""
        dst = tmp_path / "output.xlsx"
        outcome = template_migration_form.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111\n222\n333",
            shipping_codes_raw="444,555",
            as_codes_raw="666",
            origin_code="0001",
        )
        assert outcome.generated is True

        # 엑셀을 다시 파싱해 템플릿코드 열(AI/AY/BD) 값을 확인.
        with zipfile.ZipFile(dst, "r") as z:
            sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8")

        # 행 7-9 의 AI(배송비), AY(고시), BD(AS) 셀 값 추출.
        ns = "{" + _NS_MAIN + "}"
        root = ET.fromstring(sheet)
        sd = root.find(f"{ns}sheetData")
        rows = sd.findall(f"{ns}row")
        data_rows = [r for r in rows if int(r.get("r", "0")) >= 7]
        assert len(data_rows) == 3

        # 모든 셀 값을 수집.
        all_values: list[str] = []
        for r in data_rows:
            for c in r.findall(f"{ns}c"):
                v = c.find(f"{ns}v")
                if v is not None and v.text:
                    all_values.append(v.text)

        # 각 코드가 최소 1회 등장.
        for code in ("111", "222", "333", "444", "555", "666"):
            assert code in all_values, f"코드 {code} 가 생성 엑셀에 없음"

    def test_single_notice_code_1_row(self, mini_a1: Path, tmp_path: Path):
        """고시 1개만 넣으면 행 1개."""
        dst = tmp_path / "output.xlsx"
        outcome = template_migration_form.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="0001",
        )
        assert outcome.generated is True
        assert outcome.row_count == 1


# --------------------------------------------------------------------------- #
# (c) 생성 파일을 zipfile 로 재파싱 → 주입 행이 읽힌다 (깨짐 검증).
# --------------------------------------------------------------------------- #
class TestXlsxIntegrity:
    """(c) 생성된 xlsx 가 깨지지 않았는지 검증."""

    def test_generated_xlsx_is_valid_zip(self, mini_a1: Path, tmp_path: Path):
        """생성 파일이 유효한 zip 이다."""
        dst = tmp_path / "output.xlsx"
        outcome = template_migration_form.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111\n222",
            shipping_codes_raw="333",
            as_codes_raw="444",
            origin_code="0001",
        )
        assert outcome.generated is True
        # zipfile 으로 열리는지.
        assert zipfile.is_zipfile(dst)

    def test_generated_xlsx_sharedstrings_readable(self, mini_a1: Path, tmp_path: Path):
        """sharedStrings.xml 이 파싱된다."""
        dst = tmp_path / "output.xlsx"
        outcome = template_migration_form.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="0001",
        )
        assert outcome.generated is True
        with zipfile.ZipFile(dst, "r") as z:
            ss = z.read("xl/sharedStrings.xml").decode("utf-8")
        # XML 파싱이 성공하는지.
        root = ET.fromstring(ss)
        assert root is not None

    def test_generated_xlsx_sheet_readable(self, mini_a1: Path, tmp_path: Path):
        """sheet1.xml 이 파싱되고 주입 행이 있다."""
        dst = tmp_path / "output.xlsx"
        outcome = template_migration_form.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111\n222\n333",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="0001",
        )
        assert outcome.generated is True
        assert outcome.row_count == 3

        with zipfile.ZipFile(dst, "r") as z:
            sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
        ns = "{" + _NS_MAIN + "}"
        root = ET.fromstring(sheet)
        sd = root.find(f"{ns}sheetData")
        rows = sd.findall(f"{ns}row")
        data_rows = [r for r in rows if int(r.get("r", "0")) >= 7]
        assert len(data_rows) == 3

    def test_dummy_product_names_present(self, mini_a1: Path, tmp_path: Path):
        """주입된 행의 상품명에 '템플릿 이관용 임시 상품' 이 있다."""
        dst = tmp_path / "output.xlsx"
        outcome = template_migration_form.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="0001",
        )
        assert outcome.generated is True
        with zipfile.ZipFile(dst, "r") as z:
            ss = template_migration_form._load_shared_strings(z)
        # 더미 상품명 문자열이 sharedStrings 에 있다.
        assert any("템플릿 이관용 임시 상품" in s for s in ss)

    def test_corrupt_xlsx_rejected(self, mini_a1: Path, tmp_path: Path):
        """깨진 xlsx 로 주입 시도 시 ValueError 로 전환된다."""
        dst = tmp_path / "output.xlsx"
        # mini_a1 이 아닌 깨진 파일을 src 로 지정.
        corrupt = tmp_path / "corrupt.xlsx"
        corrupt.write_bytes(b"not a zip file")
        outcome = template_migration_form.generate_dummy_excel(
            src_xlsx=corrupt,
            dst_xlsx=dst,
            notice_codes_raw="111",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="0001",
        )
        assert outcome.generated is False
        assert (
            "실패" in outcome.reason or "Error" in outcome.reason or "BadZipFile" in outcome.reason
        )


# --------------------------------------------------------------------------- #
# (d) 원산지코드 미입력 → 생성 거부 + 사유. 예시값·기본값 없음.
# --------------------------------------------------------------------------- #
class TestOriginCodeRequired:
    """(d) 원산지코드 필수 (규제값 — 창작 금지)."""

    def test_empty_origin_rejected(self, mini_a1: Path, tmp_path: Path):
        """원산지코드 없으면 생성 거부."""
        dst = tmp_path / "output.xlsx"
        outcome = template_migration_form.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="",
        )
        assert outcome.generated is False
        assert outcome.origin_missing is True
        assert "원산지" in outcome.reason

    def test_whitespace_origin_rejected(self, mini_a1: Path, tmp_path: Path):
        """공백만 있는 원산지코드도 거부."""
        dst = tmp_path / "output.xlsx"
        outcome = template_migration_form.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="   ",
        )
        assert outcome.generated is False
        assert outcome.origin_missing is True

    def test_no_default_origin_in_form_html(self):
        """폼 HTML 에 원산지코드 기본값이 없다."""
        html_str = template_migration_form.render_template_migration_form_html(
            token="t", port=1, src_xlsx_path="/tmp/a.xlsx"
        )
        # value= 속성에 원산지 코드값이 있으면 안 됨.
        # input 필드에 value= 가 없어야 한다.
        assert 'name="origin_code"' in html_str
        # origin_code 필드 근처에 value 속성이 없어야 한다.
        idx = html_str.index('name="origin_code"')
        nearby = html_str[max(0, idx - 100) : idx + 100]
        assert 'value="' not in nearby


# --------------------------------------------------------------------------- #
# (e) 이상 코드 줄 → 탈락 사유 명시 (조용한 드롭 없음).
# --------------------------------------------------------------------------- #
class TestCodeValidationRejection:
    """(e) 이상 코드 줄의 탈락 사유가 명시된다."""

    def test_non_numeric_code_rejected_with_reason(self):
        """숫자가 아닌 코드는 탈락 사유가 명시된다."""
        valid, rejected = template_migration_form.parse_codes("111\nabc\n222")
        assert valid == ["111", "222"]
        assert len(rejected) == 1
        assert "abc" in rejected[0]
        assert "숫자" in rejected[0]

    def test_empty_input_returns_empty(self):
        """빈 입력은 빈 리스트."""
        valid, rejected = template_migration_form.parse_codes("")
        assert valid == []
        assert rejected == []

    def test_mixed_separators(self):
        """줄바꿈·쉼표·공백 혼합 구분."""
        valid, rejected = template_migration_form.parse_codes("111, 222\n333 444")
        assert valid == ["111", "222", "333", "444"]
        assert rejected == []

    def test_rejected_lines_in_outcome(self, mini_a1: Path, tmp_path: Path):
        """이상 코드가 있어도 정상 코드는 처리되고, 탈락 사유가 outcome 에 담긴다."""
        dst = tmp_path / "output.xlsx"
        outcome = template_migration_form.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111\nabc\n222",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="0001",
        )
        assert outcome.generated is True
        assert outcome.row_count == 2  # 정상 코드 2개.
        assert len(outcome.rejected_lines) == 1
        assert "abc" in outcome.rejected_lines[0]

    def test_all_codes_empty_rejected(self, mini_a1: Path, tmp_path: Path):
        """셋 다 비어 있으면 생성하지 않고 사유를 알린다."""
        dst = tmp_path / "output.xlsx"
        outcome = template_migration_form.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="0001",
        )
        assert outcome.generated is False
        assert "비어" in outcome.reason or "모두" in outcome.reason

    def test_all_codes_invalid_rejected(self, mini_a1: Path, tmp_path: Path):
        """모든 코드가 이상값이면 생성하지 않는다."""
        dst = tmp_path / "output.xlsx"
        outcome = template_migration_form.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="abc",
            shipping_codes_raw="def",
            as_codes_raw="ghi",
            origin_code="0001",
        )
        assert outcome.generated is False
        assert len(outcome.rejected_lines) == 3


# --------------------------------------------------------------------------- #
# (f) 사용자 A1 원본 파일 무변경 (바이트 동일).
# --------------------------------------------------------------------------- #
class TestSourceUnchanged:
    """(f) 원본 A1 이 바뀌지 않는다."""

    def test_source_bytes_unchanged(self, mini_a1: Path, tmp_path: Path):
        """생성 후 원본 바이트가 동일하다."""
        original_bytes = mini_a1.read_bytes()
        dst = tmp_path / "output.xlsx"
        outcome = template_migration_form.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111\n222\n333",
            shipping_codes_raw="444",
            as_codes_raw="555",
            origin_code="0001",
        )
        assert outcome.generated is True
        after_bytes = mini_a1.read_bytes()
        assert original_bytes == after_bytes, "원본 A1 이 변경됨"

    def test_output_is_different_file(self, mini_a1: Path, tmp_path: Path):
        """출력 파일이 원본과 다른 파일이다."""
        dst = tmp_path / "output.xlsx"
        outcome = template_migration_form.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="0001",
        )
        assert outcome.generated is True
        assert dst != mini_a1
        assert dst.is_file()


# --------------------------------------------------------------------------- #
# (g) 비밀값 미출력 · 1회용 토큰 · 127.0.0.1 (방어 회귀).
# --------------------------------------------------------------------------- #
class TestFormServerDefense:
    """(g) 폼 서버 방어 — config_form_server 와 동등한 수준."""

    def test_bound_host_is_localhost(self, tmp_path):
        """서버가 127.0.0.1 에만 바인드된다 (방어 1)."""
        token = approval_server.new_token()
        srv = template_migration_form.TemplateFormServer(
            src_xlsx_path=str(tmp_path / "a1.xlsx"),
            token=token,
            ttl_seconds=60,
        )
        srv.start()
        try:
            bound = template_migration_form.actual_bound_host(srv)
            assert bound == "127.0.0.1"
        finally:
            srv.close()

    def test_non_localhost_bind_rejected(self):
        """bind_host 가 127.0.0.1 이 아니면 거부된다."""
        with pytest.raises(ValueError, match="127.0.0.1"):
            template_migration_form.TemplateFormServer(
                src_xlsx_path="/tmp/a1.xlsx",
                token="dummy",
                bind_host="0.0.0.0",
            )

    def test_no_token_rejected(self, mini_a1: Path, tmp_path: Path):
        """토큰 없는 요청은 거부된다 (방어 2)."""
        token = approval_server.new_token()
        srv = template_migration_form.TemplateFormServer(
            src_xlsx_path=str(mini_a1),
            output_dir=str(tmp_path),
            token=token,
            ttl_seconds=60,
        )
        port = srv.start()
        try:
            status, body, _ = _send_form(
                port, fields={"notice_codes": "111", "origin_code": "0001"}
            )
            assert status == 401
            assert "토큰" in body
        finally:
            srv.close()

    def test_wrong_token_rejected(self, mini_a1: Path, tmp_path: Path):
        """틀린 토큰은 거부된다 (방어 2)."""
        token = approval_server.new_token()
        srv = template_migration_form.TemplateFormServer(
            src_xlsx_path=str(mini_a1),
            output_dir=str(tmp_path),
            token=token,
            ttl_seconds=60,
        )
        port = srv.start()
        try:
            status, body, _ = _send_form(
                port,
                fields={"token": "wrong", "notice_codes": "111", "origin_code": "0001"},
            )
            assert status == 403
            assert "토큰" in body
        finally:
            srv.close()

    def test_no_acao_header(self, mini_a1: Path, tmp_path: Path):
        """응답에 Access-Control-Allow-Origin 이 없다 (방어 6)."""
        token = approval_server.new_token()
        srv = template_migration_form.TemplateFormServer(
            src_xlsx_path=str(mini_a1),
            output_dir=str(tmp_path),
            token=token,
            ttl_seconds=60,
        )
        port = srv.start()
        try:
            _, _, headers = _send_form(port, fields={"notice_codes": "111"})
            acao = [v for k, v in headers if k.lower() == "access-control-allow-origin"]
            assert acao == []
        finally:
            srv.close()

    def test_evil_origin_rejected(self, mini_a1: Path, tmp_path: Path):
        """악성 Origin 은 거부된다 (방어 5)."""
        token = approval_server.new_token()
        srv = template_migration_form.TemplateFormServer(
            src_xlsx_path=str(mini_a1),
            output_dir=str(tmp_path),
            token=token,
            ttl_seconds=60,
        )
        port = srv.start()
        try:
            status, body, _ = _send_form(
                port,
                fields={"token": token, "notice_codes": "111", "origin_code": "0001"},
                origin="https://evil.example",
            )
            assert status == 403
            assert "Origin" in body or "origin" in body.lower()
        finally:
            srv.close()

    def test_successful_generate_consumes_token(self, mini_a1: Path, tmp_path: Path):
        """성공 처리 후 토큰이 폐기된다 (방어 3: 1회 소진)."""
        token = approval_server.new_token()
        srv = template_migration_form.TemplateFormServer(
            src_xlsx_path=str(mini_a1),
            output_dir=str(tmp_path),
            token=token,
        )
        port = srv.start()
        try:
            assert _wait_for_port(port)

            def _submit():
                time.sleep(0.1)
                _send_form(
                    port,
                    fields={
                        "token": token,
                        "notice_codes": "111",
                        "origin_code": "0001",
                        "src_xlsx": str(mini_a1),
                    },
                )

            t = threading.Thread(target=_submit, daemon=True)
            t.start()
            outcome = srv.wait(timeout=5)
            assert outcome.generated is True
            assert srv.is_consumed() is True
        finally:
            srv.close()

    def test_no_secret_values_in_result_page(self, mini_a1: Path, tmp_path: Path):
        """결과 페이지에 입력한 코드값이 노출되지 않는다 (비밀값 미출력).

        템플릿코드는 숫자이므로, 실제 코드값(예: 12345678) 이 결과 페이지에
        노출되지 않아야 한다. 탈락한 코드의 사유에는 코드값이 포함될 수 있지만,
        성공한 경우의 결과 페이지에는 어떤 코드값도 표시하지 않는다.
        """
        token = approval_server.new_token()
        srv = template_migration_form.TemplateFormServer(
            src_xlsx_path=str(mini_a1),
            output_dir=str(tmp_path),
            token=token,
            ttl_seconds=60,
        )
        port = srv.start()
        try:
            status, body, _ = _send_form(
                port,
                fields={
                    "token": token,
                    "notice_codes": "12345678901234567890",
                    "origin_code": "0001",
                    "src_xlsx": str(mini_a1),
                },
            )
            # 성공 응답에 코드값 자체가 노출되지 않아야 한다.
            assert "12345678901234567890" not in body
        finally:
            srv.close()


# --------------------------------------------------------------------------- #
# 소스 코드 정적 검사 — 방어 계약.
# --------------------------------------------------------------------------- #
class TestSourceEvidence:
    """template_migration_form.py 소스 자체가 방어 계약을 갖는다."""

    def test_source_no_acao_emission(self):
        """소스에 Access-Control-Allow-Origin 송출이 없다."""
        src = (_SRC / "clossify" / "template_migration_form.py").read_text(encoding="utf-8")
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert 'send_header("Access-Control-Allow-Origin"' not in line

    def test_source_uses_approval_server_defenses(self):
        """approval_server 의 방어 함수를 재사용한다."""
        src = (_SRC / "clossify" / "template_migration_form.py").read_text(encoding="utf-8")
        assert "approval_server.tokens_match" in src
        assert "approval_server.origin_referer_ok" in src

    def test_source_binds_localhost(self):
        """소스에서 127.0.0.1 바인드를 강제한다."""
        src = (_SRC / "clossify" / "template_migration_form.py").read_text(encoding="utf-8")
        assert '"127.0.0.1", 0' in src

    def test_source_no_openpyxl(self):
        """openpyxl 을 import 하지 않는다 (새 의존성 금지)."""
        src = (_SRC / "clossify" / "template_migration_form.py").read_text(encoding="utf-8")
        assert "import openpyxl" not in src
        assert "from openpyxl" not in src

    def test_source_no_placehold_co(self):
        """소스에 placehold.co 외부 서비스 참조가 없다 (주석 제외)."""
        src = (_SRC / "clossify" / "template_migration_form.py").read_text(encoding="utf-8")
        # 주석과 독스트링은 역사적 맥락으로 남아있을 수 있으므로 코드 라인만 검사.
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert "placehold.co" not in line, f"placehold.co 참조가 코드에 남음: {line}"

    def test_source_uses_stdlib_only(self):
        """import 가 표준 라이브러리 + 내부 모듈만 있다."""
        src = (_SRC / "clossify" / "template_migration_form.py").read_text(encoding="utf-8")
        # import 문 추출. 상대 import(from . import ...)는 별도 처리.
        imports = re.findall(r"^(?:import|from)\s+([A-Za-z_]\S*)", src, re.MULTILINE)
        for imp in imports:
            # 내부 모듈(. approval_server, . common) 또는 stdlib 만 허용.
            top = imp.lstrip(".").split(".")[0]
            assert top in (
                "__future__",
                "datetime",
                "html",
                "http",
                "os",
                "re",
                "socketserver",
                "threading",
                "time",
                "urllib",
                "zipfile",
                "xml",
                "pathlib",
                "typing",
                "approval_server",
                "common",
            ), f"예상치 못한 import: {imp}"


# --------------------------------------------------------------------------- #
# 대표이미지 조달 계약 (외부 서비스 의존 제거).
#
# (a) 생성 엑셀에 placehold.co 가 없다.
# (b) 캐시 적중 시 upload_fn 이 호출되지 않는다 (네트워크 호출 0회).
# (c) 업로드 실패(mock) → 엑셀 생성 거부 + 사유.
# (d) 사용자 지정 대표이미지 주소가 우리 기본값을 덮지 않는다 (우선순위 1).
# (e) 행 배분·열 매핑·원본 무변경 회귀 (기존 테스트 + 대표이미지 주소 확인).
# (f) 패키지 자산 PNG 가 importlib.resources 로 읽힌다.
# --------------------------------------------------------------------------- #
class TestDummyImageContract:
    """대표이미지 조달 계약 (a)-(f)."""

    def test_a_no_placehold_in_generated_xlsx(self, mini_a1: Path, tmp_path: Path):
        """(a) 생성된 엑셀의 sharedStrings 에 placehold.co 가 없다."""
        dst = tmp_path / "output.xlsx"
        outcome = template_migration_form.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="0001",
        )
        assert outcome.generated is True
        with zipfile.ZipFile(dst, "r") as z:
            ss = template_migration_form._load_shared_strings(z)
        joined = "\n".join(ss)
        assert "placehold.co" not in joined
        # 대표이미지 URL 이 실제로 들어가 있다.
        assert any("http" in s for s in ss), "대표이미지 URL 이 엑셀에 없음"

    def test_b_cache_hit_zero_network_calls(self, mini_a1: Path, tmp_path: Path):
        """(b) 캐시 적중 시 upload_fn 이 호출되지 않는다."""
        call_count = [0]

        def _fake_upload(paths):
            call_count[0] += 1
            return ["https://example.com/uploaded.png"]

        # _stub_image_cache fixture 가 _read_cached_image_url 을 stub 하므로
        # 캐시가 항상 적중한다. upload_fn 은 호출되지 않아야 한다.
        dst = tmp_path / "output.xlsx"
        outcome = template_migration_form.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="0001",
            upload_fn=_fake_upload,
        )
        assert outcome.generated is True
        assert call_count[0] == 0, "캐시 적중 시 upload_fn 이 호출되면 안 됨"

    def test_c_upload_failure_rejects_generation(self, mini_a1: Path, tmp_path: Path, monkeypatch):
        """(c) 업로드 실패(mock) → 엑셀 생성 거부 + 사유."""
        # 캐시를 비워서 업로드 경로를 강제한다.
        monkeypatch.setattr(template_migration_form, "_read_cached_image_url", lambda: "")

        def _failing_upload(paths):
            raise ConnectionError("network down (test)")

        dst = tmp_path / "output.xlsx"
        outcome = template_migration_form.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="0001",
            upload_fn=_failing_upload,
        )
        assert outcome.generated is False
        assert "대표이미지" in outcome.reason or "업로드" in outcome.reason
        assert dst.exists() is False, "실패 시 엑셀 파일이 생성되면 안 됨"

    def test_c_empty_upload_result_rejects(self, mini_a1: Path, tmp_path: Path, monkeypatch):
        """(c) 업로드가 빈 결과를 반환해도 생성이 거부된다."""
        monkeypatch.setattr(template_migration_form, "_read_cached_image_url", lambda: "")

        def _empty_upload(paths):
            return []

        dst = tmp_path / "output.xlsx"
        outcome = template_migration_form.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="0001",
            upload_fn=_empty_upload,
        )
        assert outcome.generated is False
        assert "빈 결과" in outcome.reason or "업로드" in outcome.reason

    def test_d_user_image_url_used_over_default(self, mini_a1: Path, tmp_path: Path):
        """(d) 사용자 지정 대표이미지 주소가 우리 기본값을 덮지 않는다."""
        user_url = "https://my.cdn.example.com/user_specified.png"
        call_count = [0]

        def _should_not_call(paths):
            call_count[0] += 1
            return ["https://example.com/should_not_be_used.png"]

        dst = tmp_path / "output.xlsx"
        outcome = template_migration_form.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="0001",
            main_image_url=user_url,
            upload_fn=_should_not_call,
        )
        assert outcome.generated is True
        assert call_count[0] == 0, "사용자 지정 URL 이 있을 때 upload 가 호출되면 안 됨"
        # 생성된 엑셀에 사용자 지정 URL 이 들어가야 한다.
        with zipfile.ZipFile(dst, "r") as z:
            ss = template_migration_form._load_shared_strings(z)
        assert any(user_url in s for s in ss), "사용자 지정 URL 이 엑셀에 없음"

    def test_d_user_image_url_overrides_cache(self, mini_a1: Path, tmp_path: Path):
        """(d) 사용자 지정 URL 은 캐시보다도 우선한다."""
        user_url = "https://my.cdn.example.com/user_priority.png"
        dst = tmp_path / "output.xlsx"
        outcome = template_migration_form.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="0001",
            main_image_url=user_url,
        )
        assert outcome.generated is True
        with zipfile.ZipFile(dst, "r") as z:
            ss = template_migration_form._load_shared_strings(z)
        assert any(user_url in s for s in ss)
        # 캐시 URL (_TEST_IMAGE_URL) 이 아니어야 한다.
        assert all(_TEST_IMAGE_URL not in s for s in ss)

    def test_e_row_distribution_unchanged_with_image_url(self, mini_a1: Path, tmp_path: Path):
        """(e) 대표이미지 조달 변경 후에도 행 배분·열 매핑이 동일하다."""
        dst = tmp_path / "output.xlsx"
        outcome = template_migration_form.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111\n222\n333",
            shipping_codes_raw="444,555",
            as_codes_raw="666",
            origin_code="0001",
            main_image_url="https://example.com/test.png",
        )
        assert outcome.generated is True
        assert outcome.row_count == 3

        # 열 매핑 확인 — W(대표이미지) 열에 URL 이 있다.
        with zipfile.ZipFile(dst, "r") as z:
            sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
        ns = "{" + _NS_MAIN + "}"
        root = ET.fromstring(sheet)
        sd = root.find(f"{ns}sheetData")
        rows = sd.findall(f"{ns}row")
        data_rows = [r for r in rows if int(r.get("r", "0")) >= 7]
        assert len(data_rows) == 3

        # 각 행에 W 열 셀이 있는지.
        for r in data_rows:
            cells = r.findall(f"{ns}c")
            refs = [c.get("r") for c in cells]
            row_num = r.get("r")
            assert f"W{row_num}" in refs, f"행 {row_num} 에 W 열(대표이미지) 이 없음"

    def test_e_source_unchanged_with_image_resolution(self, mini_a1: Path, tmp_path: Path):
        """(e) 대표이미지 조달 후에도 원본 A1 이 무변경이다."""
        original = mini_a1.read_bytes()
        dst = tmp_path / "output.xlsx"
        template_migration_form.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="0001",
            main_image_url="https://example.com/test.png",
        )
        assert original == mini_a1.read_bytes()

    def test_f_dummy_image_asset_exists(self):
        """(f) 패키지 자산 PNG 가 importlib.resources 로 읽힌다."""
        from clossify import common

        asset_path = common.package_data_path("dummy_main_image.png")
        assert asset_path.exists(), f"패키지 자산이 없음: {asset_path}"
        # PNG 시그니처 확인.
        data = asset_path.read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n", "유효한 PNG 가 아님"
        assert len(data) > 100, "PNG 가 너무 작음 (손상 의심)"

    def test_upload_success_caches_url(self, mini_a1: Path, tmp_path: Path, monkeypatch):
        """업로드 성공 시 CDN URL 이 캐시에 저장된다."""
        monkeypatch.setattr(template_migration_form, "_read_cached_image_url", lambda: "")
        cached_urls = []
        monkeypatch.setattr(
            template_migration_form,
            "_write_cached_image_url",
            lambda url: cached_urls.append(url),
        )

        cdn_url = "https://shop-phinf.pstatic.net/uploaded_123.jpg"

        def _upload(paths):
            return [cdn_url]

        dst = tmp_path / "output.xlsx"
        outcome = template_migration_form.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="0001",
            upload_fn=_upload,
        )
        assert outcome.generated is True
        assert len(cached_urls) == 1
        assert cached_urls[0] == cdn_url


# --------------------------------------------------------------------------- #
# (업로드 실패 경로 정화) 템플릿 이관 폼의 업로드 실패 사유에서 사용자명·
# 세션ID 가 노출되지 않는다 (실제 함수 호출로 검증).
#
# 과거 결함: ``common.sanitize_error`` 가 이중 역슬래시 경로(repr 형태)를
# 놓쳤다. 파이썬 예외 메시지는 경로를 repr 형태로 담아 내보내므로, 실제로
# 가장 자주 만나는 형태를 정확히 놓치고 있었다. ``_resolve_dummy_image_url``
# 은 업로드 실패 시 이 정화기를 거쳐 ``ImageResolveError`` 사유를 만들고,
# 그 사유가 결과 페이지에까지 흘러간다. 문자열 단위 테스트만으로는 이 결함을
# 또 놓친다 — 실제 호출 경로를 끝까지 타야 잡힌다.
# --------------------------------------------------------------------------- #
class TestUploadFailurePathRedaction:
    """업로드 실패 사유에서 절대경로(사용자명·세션ID)가 새어나가지 않는다.

    upload_fn 이 FileNotFoundError-스타일 예외(절대경로 포함) 를 일으키면,
    generate_dummy_excel → _resolve_dummy_image_url → sanitize_error →
    Outcome.reason → 결과 페이지 로 사유가 흘러간다. 경로 값은 가려지고
    사유(예외 타입·골격) 는 보여야 한다.
    """

    def test_filenotfounderror_username_not_leaked(
        self, mini_a1: Path, tmp_path: Path, monkeypatch
    ):
        """FileNotFoundError(절대경로) 사유에 사용자명이 새어나가지 않는다."""
        monkeypatch.setattr(template_migration_form, "_read_cached_image_url", lambda: "")

        # 사용자명을 포함한 절대경로로 FileNotFoundError 를 일으킨다.
        # 이것이 "실사용 노출" 경로다 — 업로드 대상 파일이 빠졌을 때 이런
        # 예외가 발생하고, 그 사유가 결과 페이지에 실린다.
        leaky_path = r"C:\Users\speedy\AppData\Local\Temp\missing_probe.png"

        def _upload(paths):
            open(leaky_path)  # FileNotFoundError 발생.

        dst = tmp_path / "output.xlsx"
        outcome = template_migration_form.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="0001",
            upload_fn=_upload,
        )
        assert outcome.generated is False
        # 사용자명(speedy) 이 사유에 새어나가면 안 된다.
        assert "speedy" not in outcome.reason, f"사용자명이 사유에 노출됨: {outcome.reason!r}"
        # 경로 전체도 새어나가면 안 된다.
        assert "missing_probe.png" not in outcome.reason
        assert "AppData" not in outcome.reason
        # 사유 골격(예외 타입·이미지 업로드 언급)은 남는다 (조용한 실패 금지).
        assert "FileNotFoundError" in outcome.reason or "업로드" in outcome.reason
        assert "[REDACTED]" in outcome.reason

    def test_filenotfounderror_sessionid_not_leaked(
        self, mini_a1: Path, tmp_path: Path, monkeypatch
    ):
        """FileNotFoundError(세션ID 포함 경로) 사유에 세션ID 가 새지 않는다."""
        monkeypatch.setattr(template_migration_form, "_read_cached_image_url", lambda: "")

        # 세션ID 가 디렉토리명에 박힌 경로 — 네이버 업로드 임시 경로 흉내.
        leaky_path = r"C:\Users\operator\AppData\Local\Temp\session-7f3a2b9c\img.png"

        def _upload(paths):
            open(leaky_path)

        dst = tmp_path / "output.xlsx"
        outcome = template_migration_form.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="0001",
            upload_fn=_upload,
        )
        assert outcome.generated is False
        assert "7f3a2b9c" not in outcome.reason
        assert "operator" not in outcome.reason
        assert "[REDACTED]" in outcome.reason

    def test_posix_path_not_leaked_in_outcome(self, mini_a1: Path, tmp_path: Path, monkeypatch):
        """POSIX 절대경로(/home/...) 도 사유에 새지 않는다 (리눅스 CI 대비)."""
        monkeypatch.setattr(template_migration_form, "_read_cached_image_url", lambda: "")

        def _upload(paths):
            raise FileNotFoundError(
                "[Errno 2] No such file or directory: '/home/ci-runner/missing.png'"
            )

        dst = tmp_path / "output.xlsx"
        outcome = template_migration_form.generate_dummy_excel(
            src_xlsx=mini_a1,
            dst_xlsx=dst,
            notice_codes_raw="111",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="0001",
            upload_fn=_upload,
        )
        assert outcome.generated is False
        assert "ci-runner" not in outcome.reason
        assert "missing.png" not in outcome.reason
        assert "[REDACTED]" in outcome.reason
        # 사유 골격은 보인다.
        assert "FileNotFoundError" in outcome.reason


@pytest.mark.skipif(
    not os.environ.get("CLOSSIFY_A1_TEMPLATE"),
    reason="CLOSSIFY_A1_TEMPLATE 환경 변수가 설정되지 않음",
)
class TestRealA1Integration:
    """실제 A1 (ExcelSaveTemplate) 로 통합 테스트."""

    def test_real_a1_3_rows_generated(self, tmp_path: Path):
        """실제 A1 로 3행 생성 후 재파싱 검증."""
        a1 = os.environ["CLOSSIFY_A1_TEMPLATE"]
        dst = tmp_path / "real_output.xlsx"
        outcome = template_migration_form.generate_dummy_excel(
            src_xlsx=a1,
            dst_xlsx=dst,
            notice_codes_raw="111\n222\n333",
            shipping_codes_raw="444,555",
            as_codes_raw="666",
            origin_code="0001",
        )
        assert outcome.generated is True, f"생성 실패: {outcome.reason}"
        assert outcome.row_count == 3
        assert zipfile.is_zipfile(dst)

    def test_real_a1_source_unchanged(self, tmp_path: Path):
        """실제 A1 원본이 무변경이다."""
        a1 = os.environ["CLOSSIFY_A1_TEMPLATE"]
        original = Path(a1).read_bytes()
        dst = tmp_path / "real_output.xlsx"
        template_migration_form.generate_dummy_excel(
            src_xlsx=a1,
            dst_xlsx=dst,
            notice_codes_raw="111",
            shipping_codes_raw="",
            as_codes_raw="",
            origin_code="0001",
        )
        after = Path(a1).read_bytes()
        assert original == after


# --------------------------------------------------------------------------- #
# HTTP 폼 POST 헬퍼 (test_config_form_server.py 와 동일 패턴).
# --------------------------------------------------------------------------- #
def _send_form(
    port: int,
    *,
    fields: dict[str, str],
    token_header: str | None = None,
    origin: str | None = None,
    method: str = "POST",
    path: str = "/",
) -> tuple[int, str, list[tuple[str, str]]]:
    """``application/x-www-form-urlencoded`` 폼 본문을 보내고 (status, body, headers)."""
    pairs = list(fields.items())
    body = urllib.parse.urlencode(pairs).encode("utf-8")
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers: list[tuple[str, str]] = [
        ("Content-Type", "application/x-www-form-urlencoded"),
        ("Content-Length", str(len(body))),
    ]
    if token_header is not None:
        headers.append(("X-Template-Form-Token", token_header))
    if origin is not None:
        headers.append(("Origin", origin))
    conn.request(method, path, body=body, headers=dict(headers))
    resp = conn.getresponse()
    resp_body = resp.read().decode("utf-8")
    resp_headers = [(k, v) for k, v in resp.getheaders()]
    status = resp.status
    conn.close()
    return status, resp_body, resp_headers


def _wait_for_port(port: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.05)
    return False


# --------------------------------------------------------------------------- #
# 예외 방벽 — 슬라이스 1 폼 핸들러의 예외 방벽 검증 (티켓 f).
#
# 슬라이스 1 폼 서버의 ``_TemplateFormHandler.do_POST`` 가 예외를 일으키면
# ``http.server`` 가 응답 없이 연결을 끊는 결함(N28 계열)을 방벽이 막는지
# 검증한다. 슬라이스 1 폼은 설정 파일(config.json) 에 직접 의존하지 않으므로
# config-missing 경로는 없다 — (a) 는 강제 예외로 대신한다.
#
# 본 테스트가 검증하는 계약:
#   (a) 핸들러 강제 예외 → 5xx + 사람이 읽을 HTML (연결 끊김 아님).
#   (b) 오류 화면에 절대경로·토큰이 없다 (sanitize_error 정화).
# --------------------------------------------------------------------------- #
def _start_template_server(
    mini_a1: Path, tmp_path: Path, ttl_seconds: int = 60
) -> tuple[template_migration_form.TemplateFormServer, int, str]:
    """슬라이스 1 폼 서버를 시작하고 (server, port, token) 을 반환한다."""
    token = approval_server.new_token()
    srv = template_migration_form.TemplateFormServer(
        src_xlsx_path=str(mini_a1),
        output_dir=str(tmp_path),
        token=token,
        ttl_seconds=ttl_seconds,
    )
    port = srv.start()
    assert _wait_for_port(port), f"포트 {port} 가 열리지 않음"
    return srv, port, token


class TestTemplateFormExceptionBarrier:
    """(f) 슬라이스 1 폼 핸들러의 예외 방벽.

    ``generate_dummy_excel`` 이 예외를 일으키면(예: zipfile 깨짐이 아닌
    예상치 못한 런타임 오류), 핸들러 바깥으로 번져 ``http.server`` 가 응답
    없이 연결을 끊는 결함을 방벽이 막는지 검증한다.
    """

    def test_forced_exception_returns_500_html(self, mini_a1: Path, tmp_path: Path, monkeypatch):
        """강제 예외 → 500 + 사람이 읽을 HTML (연결 끊김 아님)."""

        def _boom(*_a, **_kw):
            raise RuntimeError("의도된 폭발 — 슬라이스 1 방벽 테스트")

        monkeypatch.setattr(template_migration_form, "generate_dummy_excel", _boom)

        srv, port, token = _start_template_server(mini_a1, tmp_path)
        try:
            status, body, _headers = _send_form(
                port,
                fields={
                    "token": token,
                    "notice_codes": "111",
                    "origin_code": "0001",
                    "src_xlsx": str(mini_a1),
                },
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

    def test_barrier_catches_unexpected_error(self, mini_a1: Path, tmp_path: Path, monkeypatch):
        """TypeError 같은 예상치 못한 예외도 방벽이 잡는다."""

        def _type_err(*_a, **_kw):
            raise TypeError("NoneType has no attribute 'write'")

        monkeypatch.setattr(template_migration_form, "generate_dummy_excel", _type_err)

        srv, port, token = _start_template_server(mini_a1, tmp_path)
        try:
            status, body, _headers = _send_form(
                port,
                fields={
                    "token": token,
                    "notice_codes": "111",
                    "origin_code": "0001",
                    "src_xlsx": str(mini_a1),
                },
            )
            assert 500 <= status < 600
            assert "</html>" in body.lower()
        finally:
            srv.close()


class TestTemplateFormErrorSanitization:
    """(f) 슬라이스 1 폼 오류 화면에 절대경로·토큰이 없다."""

    def test_no_absolute_path_in_error_page(self, mini_a1: Path, tmp_path: Path, monkeypatch):
        """오류 화면에 Windows 절대경로가 없다."""
        _SECRET_PATH = "C:\\\\Users\\\\leaked_user\\\\projects\\\\.local\\\\config.json"

        def _raise_with_path(*_a, **_kw):
            raise FileNotFoundError(f"[Errno 2] No such file: '{_SECRET_PATH}'")

        monkeypatch.setattr(template_migration_form, "generate_dummy_excel", _raise_with_path)

        srv, port, token = _start_template_server(mini_a1, tmp_path)
        try:
            _status, body, _headers = _send_form(
                port,
                fields={
                    "token": token,
                    "notice_codes": "111",
                    "origin_code": "0001",
                    "src_xlsx": str(mini_a1),
                },
            )
            # 절대경로 카나리가 정화되어 있다.
            assert "C:\\\\Users" not in body
            assert "leaked_user" not in body
        finally:
            srv.close()

    def test_no_token_in_error_page(self, mini_a1: Path, tmp_path: Path, monkeypatch):
        """오류 화면에 폼 토큰이 누출되지 않는다."""

        def _raise_generic(*_a, **_kw):
            raise RuntimeError("generic slice1 barrier test")

        monkeypatch.setattr(template_migration_form, "generate_dummy_excel", _raise_generic)

        srv, port, token = _start_template_server(mini_a1, tmp_path)
        try:
            _status, body, _headers = _send_form(
                port,
                fields={
                    "token": token,
                    "notice_codes": "111",
                    "origin_code": "0001",
                    "src_xlsx": str(mini_a1),
                },
            )
            # 토큰이 오류 화면 본문에 없다.
            assert token not in body
        finally:
            srv.close()
