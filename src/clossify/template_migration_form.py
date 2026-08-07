# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""템플릿 이관 더미 대량등록 엑셀 생성 폼 서버.

본 모듈은 **판매자센터의 고시·AS·배송비 템플릿을 API 로 못 읽는 문제**를
해결하는 파이프라인의 앞단이다. 유일한 통로는 **대량등록 엑셀에 템플릿코드를
넣어 더미 상품을 가등록**하고, 상품 응답에서 펼쳐진 값을 수확하는 것.
본 티켓은 그 앞단 — **코드를 받아 더미 엑셀을 만들어주는 폼** — 이다.
수확(중지→추출→삭제)은 다음 티켓에서 구현한다.

방어 — config_form_server 와 동등한 수준 (approval_server 방어 상속)
-------------------------------------------------------------------
로컬 포트를 열면 같은 컴퓨터의 아무 웹페이지나 그 포트를 찌를 수 있다.
본 모듈의 *전부* 가 방어다. config_form_server 의 방어 10가지를 그대로
계승한다:

1. **바인딩**: ``127.0.0.1`` 에만 바인드 (``0.0.0.0`` 금지).
2. **일회용 토큰**: 폼마다 새로 생성 (``secrets.token_urlsafe``).
   비교는 ``secrets.compare_digest`` (타이밍 공격 방지).
3. **1회 소진**: 처리 1건 성공 시 토큰 즉시 폐기.
4. **수명 제한**: 10분 경과 시 자동 만료·서버 종료.
5. **Origin/Referer 전값 검사**: ``null``/``file://`` 만 허용.
6. **CORS 금지**: ``Access-Control-Allow-Origin`` 헤더를 절대 내보내지 않는다.
7. **범위 제한**: 이 서버가 처리하는 것은 더미 엑셀 생성 1건 뿐.
8. **기본 OFF**: 별도 설정 ``enable_template_form`` (기본 ``false``) 로 제어.
9. **수명주기**: 요청 처리 후 또는 만료 시 반드시 서버 종료 (좀비 포트 금지).
10. **로그에 토큰 금지** + **비밀값 미출력**.

추가 방어(엑셀 생성 특유):
- **원본 무변경**: 사용자의 A1 사본을 복사해서 채운다. 원본은 건드리지 않는다.
- **표준 라이브러리만**: openpyxl 등 새 의존성 없이 zipfile+XML 로 주입.
- **규제값 창작 금지**: 원산지코드는 사용자 입력값만. 기본값·예시값 창작 안 함.
- **조용한 드롭 금지**: 이상 코드 줄은 어느 줄이 왜 탈락했는지 명시.

의존 방향: ``approval_server`` (방어 공통) → ``config_form_server`` (패턴 원형)
→ ``template_migration_form`` (본 모듈).
"""

from __future__ import annotations

import datetime
import html
import http.server
import os
import re
import socketserver
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

from . import approval_server, common

# ---------------------------------------------------------------------------
# 상수.
# ---------------------------------------------------------------------------
TTL_SECONDS = approval_server.TTL_SECONDS  # 10분. 만료 후 서버는 종료된다.

# POST 본문 최대 크기. 템플릿 코드 여러 줄 + 원산지코드. 충분한 여유치.
_MAX_BODY_BYTES = 128 * 1024

# XLSX 내부 XML 네임스페이스.
_NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_XML_NS_URI = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_XMLSPACE = "{http://www.w3.org/XML/1998/namespace}space"

# ---------------------------------------------------------------------------
# A1 실측 결과 (ExcelSaveTemplate_20260324.xlsx).
#
# 시트 구조:
#   행 1: 섹션 그룹 헤더 (병합 셀).
#   행 2: 컬럼 헤더 (필드명).
#   행 3: 필수/비필수/조건부필수 표기.
#   행 4: 예시 더미 행 (사용자가 참고용).
#   행 5-6: 작성 가이드.
#   행 7+: 실제 데이터 영역 (우리가 채울 곳).
#
# 더미 1행을 만들기 위한 최소 필수 열 집합 (행 3 기준 "필수" 표기):
#   B  카테고리코드     필수
#   C  상품명           필수
#   D  상품상태         비필수 (공란 시 신상품)
#   E  판매가           필수
#   L  재고수량         필수
#   W  대표이미지        필수  ← 더미이므로 임의 URL 허용
#   Y  상세설명         필수
#   AD 원산지코드       필수  ← 규제값, 사용자 입력만
#
# 템플릿코드 열 (이 티켓의 핵심 — 비필수지만 더미에 싣는 대상):
#   AI 배송비 템플릿코드          비필수
#   AY 상품정보제공고시 템플릿코드 비필수
#   BD A/S 템플릿코드             비필수
# ---------------------------------------------------------------------------
_COL_CATEGORY = "B"
_COL_PRODUCT_NAME = "C"
_COL_PRODUCT_STATE = "D"
_COL_PRICE = "E"
_COL_STOCK = "L"
_COL_MAIN_IMAGE = "W"
_COL_DETAIL = "Y"
_COL_ORIGIN_CODE = "AD"
_COL_SHIPPING_TEMPLATE = "AI"
_COL_NOTICE_TEMPLATE = "AY"
_COL_AS_TEMPLATE = "BD"

# 행 4(예시 행)의 셀 스타일 참조(s 속성). 더미 행에 같은 스타일을 적용해
# 네이버 업로드 파서가 형식을 오인하지 않게 한다.
_STYLE_TEXT = "12"  # 텍스트 서식 셀 (원산지코드 등).
_STYLE_GENERAL = "11"  # 일반 셀.
_STYLE_IMAGE = "15"  # 이미지 셀.
_STYLE_ORIGIN = "18"  # 원산지 전용 텍스트 셀.

# 더미 상품의 고정값들 (규제값 아님 — 명시적 더미이므로 허용).
_DUMMY_PRICE = 100  # 판매가 최소값 근사.
_DUMMY_STOCK = 1  # 재고 최소값.
_DUMMY_DETAIL = "<p>템플릿 이관용 임시 상품입니다. 자동 삭제 예정.</p>"

# ---------------------------------------------------------------------------
# 대표이미지 조달 (외부 서비스 의존 제거).
#
# 과거: ``https://placehold.co/100x100.png`` (타사 플레이스홀더 서비스).
#   - 그 서비스가 죽거나 네이버가 차단하면 업로드가 통째로 실패하고 원인이
#     화면에 드러나지 않는다 (조용한 통과).
#   - 우리 산출물에 선언되지 않은 외부 의존이 들어있었다.
#
# 현재: ``data/dummy_main_image.png`` (100x100 단색 PNG, 패키지 자산).
#   - 215바이트짜리 표준 PNG. 표준 라이브러리(zlib+struct)로 생성 가능한
#     포맷이지만, **패키지 자산으로 동봉**해 생성 코드가 필요 없게 한다
#     (과거 이 프로젝트에서 자산 누락 사고가 있었다 — importlib.resources 관례).
#   - common.package_data_path 로 읽는다 (소스 트리·editable·wheel 모두 동일).
#   - 최초 1회 업로드 후 CDN 주소를 캐시(STATE_DIR/template_migration_dummy_image.json)
#     해 재사용 — 캐시 적중 시 네트워크 호출 0회.
#   - 업로드는 기존 이미지 업로드 경로(naver_client.upload_images)를 재사용.
#   - 업로드 실패 시 엑셀 생성을 거부하고 사유를 알린다 (조용한 통과 금지).
#   - 사용자가 직접 대표이미지 주소를 지정할 수 있는 선택 입력을 둔다.
# ---------------------------------------------------------------------------
_DUMMY_IMAGE_ASSET = "dummy_main_image.png"
_DUMMY_IMAGE_CACHE_NAME = "template_migration_dummy_image.json"


# ---------------------------------------------------------------------------
# 결과 타입.
# ---------------------------------------------------------------------------
class Outcome:
    """더미 엑셀 생성 결과. 성공이면 output_path 가 있고, 실패면 reason 이 있다.

    **비밀값 비노출 계약**: 본 결과에는 사용자가 입력한 코드 값이나 원산지코드
    값 자체를 담지 않는다. 생성된 파일 경로·통계(코드 수 등)만 담는다.
    """

    def __init__(
        self,
        *,
        generated: bool,
        reason: str = "",
        output_path: str = "",
        row_count: int = 0,
        rejected_lines: list[str] | None = None,
        origin_missing: bool = False,
    ) -> None:
        self.generated = bool(generated)
        self.reason = str(reason)
        self.output_path = str(output_path)
        self.row_count = int(row_count)
        self.rejected_lines = list(rejected_lines) if isinstance(rejected_lines, list) else []
        self.origin_missing = bool(origin_missing)


# ---------------------------------------------------------------------------
# 코드값 검증.
#
# 네이버 템플릿코드는 숫자(길이 무관). 원산지코드도 숫자 문자열(예: "0001").
# 허용 문자: 숫자만. 줄바꿈/쉼표/공백으로 구분된 여러 값을 파싱한다.
# 이상값은 어느 줄이 왜 탈락했는지 명시한다 (조용한 드롭 금지).
# ---------------------------------------------------------------------------
_CODE_PATTERN = re.compile(r"^[0-9]+$")


def parse_codes(raw: str) -> tuple[list[str], list[str]]:
    """여러 줄/쉼표/공백으로 구분된 코드 문자열을 파싱한다.

    Returns:
        ``(valid_codes, rejected_descriptions)``.
        - ``valid_codes``: 검증을 통과한 코드 문자열 리스트 (입력 순서 유지).
        - ``rejected_descriptions``: 탈락한 항목의 사유 설명 리스트
          (예: ``"'abc' — 숫자가 아님"``). 조용한 드롭 금지.
    """
    if not raw:
        return [], []
    # 줄바꿈, 쉼표, 공백(탭 포함) 으로 분할.
    tokens = re.split(r"[\n,\s]+", raw)
    valid: list[str] = []
    rejected: list[str] = []
    for tok in tokens:
        t = tok.strip()
        if not t:
            continue
        if _CODE_PATTERN.match(t):
            valid.append(t)
        else:
            rejected.append(f"'{t}' — 숫자가 아님")
    return valid, rejected


# ---------------------------------------------------------------------------
# XLSX 더미 행 주입 (표준 라이브러리만).
#
# XLSX 는 ZIP+XML 이다. 본 함수는:
#   1. 원본 A1 사본을 읽는다 (원본 무변경).
#   2. sharedStrings.xml 에 더미 문자열들을 추가하고 count/uniqueCount 갱신.
#   3. sheet1.xml 의 </sheetData> 앞에 더미 행들을 주입.
#   4. 새 ZIP 파일로 쓴다.
#   5. 생성 후 zipfile 으로 다시 파싱해 주입 행이 읽히는지 검증 (깨짐 검증).
#
# 템플릿코드(AI/AY/BD)는 숫자이므로 inline 숫자(<v>)로 쓴다.
# 상품명·원산지코드·대표이미지·상세설명은 문자열이므로 sharedStrings 참조(t="s").
# 판매가·재고는 숫자 inline.
# ---------------------------------------------------------------------------
def _load_shared_strings(zip_reader: zipfile.ZipFile) -> list[str]:
    """sharedStrings.xml 을 파싱해 문자열 리스트를 반환.

    테스트가 생성된 엑셀의 sharedStrings 를 읽을 때 사용한다.
    """
    try:
        ss_xml = zip_reader.read("xl/sharedStrings.xml").decode("utf-8")
    except KeyError:
        return []
    root = ET.fromstring(ss_xml)
    strings: list[str] = []
    for si in root.findall(f"{_NS_MAIN}si"):
        parts = [t.text or "" for t in si.iter(f"{_NS_MAIN}t")]
        strings.append("".join(parts))
    return strings


def _build_row_xml(
    row_num: int,
    cells: list[tuple[str, str | None, str | None, str]],
) -> str:
    """행 XML 문자열을 빌드.

    Args:
        row_num: 행 번호 (7, 8, ...).
        cells: ``(col_letter, cell_type, style_ref, value)`` 리스트.
            - cell_type: ``"s"`` (sharedStrings 참조) · ``None`` (inline 숫자)
              · ``"inlineStr"`` (inline 문자열).
            - style_ref: 셀 스타일 인덱스 (행 4 참조) 또는 None.
            - value: 셀 값.

    Returns:
        ``<row r="N" ...>...</row>`` XML 문자열.
    """
    cell_parts: list[str] = []
    for col, ctype, style, value in cells:
        ref = f"{col}{row_num}"
        attribs = f' r="{ref}"'
        if ctype:
            attribs += f' t="{ctype}"'
        if style:
            attribs += f' s="{style}"'
        if ctype == "s":
            cell_parts.append(f"<c{attribs}><v>{value}</v></c>")
        else:
            cell_parts.append(f"<c{attribs}><v>{value}</v></c>")
    return f'<row r="{row_num}" spans="1:93">' + "".join(cell_parts) + "</row>"


def _inject_dummy_rows(
    src_xlsx: str | Path,
    dst_xlsx: str | Path,
    *,
    notice_codes: list[str],
    shipping_codes: list[str],
    as_codes: list[str],
    origin_code: str,
    image_url: str,
    category_code: str = "",
) -> str:
    """원본 A1 사본에 더미 행들을 주입해 새 xlsx 를 만든다.

    **원본 무변경**: src_xlsx 는 읽기만 한다.

    행 배분: 행 수 = max(고시, 배송, AS 코드 수). 각 코드가 최소 1회 등장.
    조합 전개는 하지 않는다 — 행 N 은 고시[N] / 배송[N] / AS[N] 을 같이 싣고,
    한쪽이 짧으면 해당 열은 비운다.

    Args:
        src_xlsx: 원본 A1 엑셀 경로.
        dst_xlsx: 출력 엑셀 경로.
        notice_codes: 고시 템플릿코드 리스트.
        shipping_codes: 배송비 템플릿코드 리스트.
        as_codes: AS 템플릿코드 리스트.
        origin_code: 원산지코드 (사용자 입력값).
        image_url: 대표이미지 URL (조달된 값 — 사용자 지정 또는 업로드된 CDN).
        category_code: 카테고리코드 (선택).

    Returns:
        생성된 파일 경로 (dst_xlsx).

    Raises:
        ValueError: 주입 실패 (깨짐 검증 탈락 등).
        OSError: 파일 읽기/쓰기 실패.
    """
    src_path = Path(src_xlsx)
    dst_path = Path(dst_xlsx)

    # 1. 원본 zip 의 모든 파일을 읽기 (원본 무변경).
    with zipfile.ZipFile(src_path, "r") as zin:
        all_files: dict[str, bytes] = {}
        for info in zin.infolist():
            all_files[info.filename] = zin.read(info.filename)

    # 2. sharedStrings 파싱 및 더미 문자열 추가.
    ss_xml = all_files["xl/sharedStrings.xml"].decode("utf-8")
    ss_root = ET.fromstring(ss_xml)
    strings: list[str] = []
    for si in ss_root.findall(f"{_NS_MAIN}si"):
        parts = [t.text or "" for t in si.iter(f"{_NS_MAIN}t")]
        strings.append("".join(parts))

    # 더미 행에 들어갈 문자열들을 sharedStrings 에 추가.
    # 행 수 계산.
    row_count = max(len(notice_codes), len(shipping_codes), len(as_codes), 1)

    # 각 행별 더미 문자열을 sharedStrings 에 추가하고 인덱스 매핑.
    # 행별로: 상품명(고유), 원산지코드(공통), 대표이미지(공통), 상세설명(공통),
    #          카테고리코드(공통).
    name_indices: list[int] = []
    for i in range(row_count):
        name = f"템플릿 이관용 임시 상품 {i + 1} (자동 삭제 예정)"
        strings.append(name)
        name_indices.append(len(strings) - 1)

    # 공통 문자열.
    strings.append(origin_code)
    origin_idx = len(strings) - 1
    strings.append(image_url)
    image_idx = len(strings) - 1
    strings.append(_DUMMY_DETAIL)
    detail_idx = len(strings) - 1

    category_idx = -1
    if category_code:
        strings.append(category_code)
        category_idx = len(strings) - 1

    # count 는 기존 count (참조 횟수) 에 새 참조 횟수를 더한다.
    # 정확한 count 계산: 각 문자열이 참조되는 횟수의 합.
    # 기존 count 를 읽고, 새로 추가되는 참조 수를 더한다.
    orig_count_match = re.search(r'count="(\d+)"', ss_xml)
    orig_count = int(orig_count_match.group(1)) if orig_count_match else len(strings)

    # 새 참조 수: 행별 문자열 참조.
    # 상품명: row_count 회, 원산지: row_count, 이미지: row_count, 상세: row_count,
    # 카테고리: row_count (있을 때).
    new_refs = row_count * 4  # name + origin + image + detail.
    if category_code:
        new_refs += row_count
    new_count = orig_count + new_refs

    # sharedStrings.xml 재직렬화.
    new_ss_xml = _serialize_shared_strings_full(strings, new_count)
    all_files["xl/sharedStrings.xml"] = new_ss_xml

    # 3. sheet1.xml 에 더미 행 주입.
    sheet_xml = all_files["xl/worksheets/sheet1.xml"].decode("utf-8")
    rows_xml: list[str] = []
    for i in range(row_count):
        row_num = 7 + i  # 행 7부터 시작.
        cells: list[tuple[str, str | None, str | None, str]] = []

        # 카테고리코드 (선택).
        if category_code:
            cells.append((_COL_CATEGORY, "s", _STYLE_TEXT, str(category_idx)))

        # 상품명 (필수) — sharedStrings 참조.
        cells.append((_COL_PRODUCT_NAME, "s", _STYLE_GENERAL, str(name_indices[i])))

        # 판매가 (필수) — inline 숫자.
        cells.append((_COL_PRICE, None, _STYLE_GENERAL, str(_DUMMY_PRICE)))

        # 재고수량 (필수) — inline 숫자.
        cells.append((_COL_STOCK, None, _STYLE_GENERAL, str(_DUMMY_STOCK)))

        # 대표이미지 (필수) — sharedStrings 참조.
        cells.append((_COL_MAIN_IMAGE, "s", _STYLE_IMAGE, str(image_idx)))

        # 상세설명 (필수) — sharedStrings 참조.
        cells.append((_COL_DETAIL, "s", _STYLE_TEXT, str(detail_idx)))

        # 원산지코드 (필수) — sharedStrings 참조 (텍스트 서식).
        cells.append((_COL_ORIGIN_CODE, "s", _STYLE_ORIGIN, str(origin_idx)))

        # 배송비 템플릿코드 (있을 때만) — inline 숫자.
        if i < len(shipping_codes) and shipping_codes[i]:
            cells.append((_COL_SHIPPING_TEMPLATE, None, _STYLE_GENERAL, shipping_codes[i]))

        # 고시 템플릿코드 (있을 때만) — inline 숫자.
        if i < len(notice_codes) and notice_codes[i]:
            cells.append((_COL_NOTICE_TEMPLATE, None, _STYLE_GENERAL, notice_codes[i]))

        # AS 템플릿코드 (있을 때만) — inline 숫자.
        if i < len(as_codes) and as_codes[i]:
            cells.append((_COL_AS_TEMPLATE, None, _STYLE_GENERAL, as_codes[i]))

        rows_xml.append(_build_row_xml(row_num, cells))

    # </sheetData> 앞에 행들 삽입.
    injection = "".join(rows_xml)
    new_sheet_xml = sheet_xml.replace("</sheetData>", injection + "</sheetData>")
    all_files["xl/worksheets/sheet1.xml"] = new_sheet_xml.encode("utf-8")

    # 4. 새 ZIP 쓰기.
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dst_path, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, data in all_files.items():
            zout.writestr(name, data)

    # 5. 깨짐 검증 — 생성 파일을 다시 파싱해 주입 행이 읽히는지.
    _verify_injection(dst_path, expected_rows=row_count)

    return str(dst_path)


def _serialize_shared_strings_full(strings: list[str], count: int) -> bytes:
    """전체 문자열 리스트로 sharedStrings.xml 을 빌드."""
    si_parts: list[str] = []
    for s in strings:
        esc = html.escape(s, quote=False)
        si_parts.append(f'<si><t xml:space="preserve">{esc}</t></si>')
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<sst xmlns="{_XML_NS_URI}" count="{count}" uniqueCount="{len(strings)}">'
        + "".join(si_parts)
        + "</sst>"
    )
    return xml.encode("utf-8")


def _verify_injection(xlsx_path: str | Path, *, expected_rows: int) -> None:
    """생성된 xlsx 를 zipfile 으로 다시 파싱해 주입 행이 읽히는지 검증.

    Raises:
        ValueError: 깨진 파일이거나 주입 행이 읽히지 않을 때.
    """
    path = Path(xlsx_path)
    try:
        with zipfile.ZipFile(path, "r") as z:
            sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
            # sharedStrings 도 읽을 수 있는지.
            z.read("xl/sharedStrings.xml").decode("utf-8")
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError) as exc:
        raise ValueError(f"생성된 xlsx 가 깨졌습니다: {type(exc).__name__}") from exc

    root = ET.fromstring(sheet)
    sd = root.find(f"{_NS_MAIN}sheetData")
    if sd is None:
        raise ValueError("sheetData 엘리먼트를 찾을 수 없습니다 (깨진 파일).")
    rows = sd.findall(f"{_NS_MAIN}row")
    # 행 7 이후가 있는지.
    data_rows = [r for r in rows if int(r.get("r", "0")) >= 7]
    if len(data_rows) < expected_rows:
        raise ValueError(
            f"주입된 행 수가 예상과 다릅니다: expected={expected_rows}, " f"found={len(data_rows)}"
        )
    # 각 행에 최소 하나의 셀이 있는지.
    for r in data_rows:
        cells = r.findall(f"{_NS_MAIN}c")
        if not cells:
            raise ValueError(f"행 {r.get('r')} 에 셀이 없습니다 (깨진 주입).")


# ---------------------------------------------------------------------------
# 대표이미지 조달 (외부 서비스 의존 제거).
#
# 우선순위:
#   1. 사용자가 직접 대표이미지 URL 을 지정 → 그 값을 그대로 쓴다 (우리 기본값이
#      덮지 않는다).
#   2. 캐시된 CDN 주소가 있으면 재사용 (네트워크 호출 0회).
#   3. 패키지 자산 PNG(data/dummy_main_image.png) 를 업로드 → CDN 주소 획득 → 캐시.
#
# 업로드는 기존 경로(naver_client.upload_images)를 재사용한다. 새로 만들지 않는다.
# 업로드 실패 시 ``ImageResolveError`` 를 일으켜 호출자(generate_dummy_excel)가
# 엑셀 생성을 거부하게 한다 — 깨진 주소가 박힌 엑셀을 만들지 않는다 (조용한 통과
# 금지).
#
# 캐시 위치: ``common.STATE_DIR / _DUMMY_IMAGE_CACHE_NAME``. 비밀값이 아니므로
# 별도 파일로 둔다(config.json 이나 토큰과 섞이지 않는다).
# ---------------------------------------------------------------------------
class ImageResolveError(RuntimeError):
    """대표이미지 조달 실패 — 업로드 실패·자산 부재 등."""


def _dummy_image_cache_path() -> Path:
    """캐시 파일 경로 (STATE_DIR 아래 별도 파일)."""
    return Path(common.STATE_DIR) / _DUMMY_IMAGE_CACHE_NAME


def _read_cached_image_url() -> str:
    """캐시된 대표이미지 CDN URL 을 읽는다. 없으면 빈 문자열.

    비밀값이 아니므로 일반 파일로 취급한다.
    """
    cache = _dummy_image_cache_path()
    try:
        import json

        data = json.loads(cache.read_text(encoding="utf-8"))
        url = str(data.get("url") or "").strip()
        return url
    except Exception:
        return ""


def _write_cached_image_url(url: str) -> None:
    """업로드로 얻은 CDN URL 을 캐시 파일에 저장한다 (비밀값 아님)."""
    if not url:
        return
    cache = _dummy_image_cache_path()
    try:
        import json

        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(cache.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"url": url}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, cache)
    except OSError:
        # 캐시 쓰기 실패는 치명적이지 않다 — 다음 생성 때 업로드를 다시 시도한다.
        # 단, 호출자에게 알리지 않고 조용히 넘기면 안 되므로 로그는 남기지 않되
        # 캐시 없음 경로가 그대로 동작하게 둔다.
        pass


def _resolve_dummy_image_url(
    *,
    user_image_url: str = "",
    upload_fn: Any = None,
) -> str:
    """대표이미지 URL 을 조달한다.

    우선순위:
      1. ``user_image_url`` 이 비어있지 않으면 그 값 (우리 기본값이 덮지 않는다).
      2. 캐시된 CDN URL.
      3. 패키지 자산 PNG 업로드 → 새 CDN URL → 캐시.

    Args:
        user_image_url: 사용자가 직접 지정한 대표이미지 주소 (선택).
        upload_fn: 패키지 자산 PNG 경로 리스트 → CDN URL 리스트 변환 함수.
            기본값은 ``naver_client.upload_images``. 테스트 주입 가능.

    Returns:
        조달된 대표이미지 URL 문자열.

    Raises:
        ImageResolveError: 업로드 실패 또는 패키지 자산 부재.
    """
    # 1. 사용자 지정값 우선.
    user_url = (user_image_url or "").strip()
    if user_url:
        return user_url

    # 2. 캐시 적중 시 네트워크 호출 0회.
    cached = _read_cached_image_url()
    if cached:
        return cached

    # 3. 패키지 자산 PNG 업로드.
    asset_path = common.package_data_path(_DUMMY_IMAGE_ASSET)
    if not asset_path.exists():
        raise ImageResolveError(
            f"대표이미지 패키지 자산이 없습니다: {asset_path} "
            "(설치본이 깨졌거나 wheel 에서 누락됨). "
            "사용자 대표이미지 주소를 직접 지정하세요."
        )

    if upload_fn is None:
        # 지연 import — naver_client 는 requests/bcrypt 에 의존하므로 모듈 로딩
        # 시점이 아닌 실제 업로드가 필요할 때만 불러온다. 테스트가 mock 주입 없이
        # generate_dummy_excel 을 호출해도 모듈 자체는 import 된다.
        from . import naver_client

        upload_fn = naver_client.upload_images

    try:
        urls = upload_fn([str(asset_path)])
    except Exception as exc:
        # 네이버 실호출 에러를 그대로 노출하면 인증 정보가 새어나갈 수 있다.
        # 사유(예외 타입)는 남기고 메시지는 sanitize 한다.
        reason = common.sanitize_error(exc)
        raise ImageResolveError(
            f"대표이미지 업로드 실패 — {reason}. "
            "깨진 이미지 주소가 박힌 엑셀을 만들지 않기 위해 생성을 거부합니다. "
            "사용자 대표이미지 주소를 직접 지정하거나 인증·네트워크를 확인하세요."
        ) from exc

    if not urls or not str(urls[0] or "").strip():
        raise ImageResolveError(
            "대표이미지 업로드가 빈 결과를 반환했습니다. "
            "깨진 이미지 주소가 박힌 엑셀을 만들지 않기 위해 생성을 거부합니다."
        )
    cdn_url = str(urls[0]).strip()
    _write_cached_image_url(cdn_url)
    return cdn_url


# ---------------------------------------------------------------------------
# 공개 API: 더미 엑셀 생성 (폼 서버 없이 직접 호출용).
# ---------------------------------------------------------------------------
def generate_dummy_excel(
    *,
    src_xlsx: str | Path,
    dst_xlsx: str | Path,
    notice_codes_raw: str,
    shipping_codes_raw: str,
    as_codes_raw: str,
    origin_code: str,
    category_code: str = "",
    main_image_url: str = "",
    upload_fn: Any = None,
) -> Outcome:
    """더미 대량등록 엑셀을 생성한다.

    셋 다(고시/배송/AS) 비어 있으면 생성하지 않고 사유를 알린다.
    원산지코드가 없으면 생성을 거부한다 (규제값 — 예시값·기본값 창작 금지).
    이상 코드 줄은 어느 줄이 왜 탈락했는지 명시한다 (조용한 드롭 금지).

    대표이미지 URL 조달은 ``_resolve_dummy_image_url`` 에 위임한다. 업로드 실패 시
    ``ImageResolveError`` 를 잡아 엑셀 생성을 거부한다 (깨진 주소가 박힌 엑셀을
    만들지 않는다 — 조용한 통과 금지).

    Args:
        src_xlsx: 원본 A1 엑셀 경로.
        dst_xlsx: 출력 엑셀 경로.
        notice_codes_raw: 고시 템플릿코드 원시 입력 (줄바꿈/쉼표 구분).
        shipping_codes_raw: 배송비 템플릿코드 원시 입력.
        as_codes_raw: AS 템플릿코드 원시 입력.
        origin_code: 원산지코드 (사용자 실값).
        category_code: 카테고리코드 (선택).
        main_image_url: 사용자가 직접 지정한 대표이미지 주소 (선택).
            비어 있으면 패키지 자산 PNG 를 업로드해 CDN 주소를 얻는다
            (캐시 적중 시 네트워크 호출 0회).
        upload_fn: 테스트 주입용 업로드 함수. 기본값은
            ``naver_client.upload_images``. ``main_image_url`` 이나 캐시가
            있으면 호출되지 않는다.

    Returns:
        ``Outcome`` — 생성 결과.
    """
    notice_codes, notice_rejected = parse_codes(notice_codes_raw)
    shipping_codes, shipping_rejected = parse_codes(shipping_codes_raw)
    as_codes, as_rejected = parse_codes(as_codes_raw)

    all_rejected = notice_rejected + shipping_rejected + as_rejected

    # 셋 다 비어 있으면 생성하지 않는다.
    if not notice_codes and not shipping_codes and not as_codes:
        reason = "고시·배송·AS 템플릿코드가 모두 비어 있습니다."
        if all_rejected:
            reason += " 탈락한 코드: " + "; ".join(all_rejected)
        return Outcome(generated=False, reason=reason, rejected_lines=all_rejected)

    # 원산지코드 필수 (규제값 — 창작 금지).
    origin = (origin_code or "").strip()
    if not origin:
        return Outcome(
            generated=False,
            reason="원산지코드는 필수입니다. 규제값이므로 기본값·예시값을 창작할 수 없습니다.",
            origin_missing=True,
            rejected_lines=all_rejected,
        )
    # 원산지코드도 숫자 검증.
    if not _CODE_PATTERN.match(origin):
        return Outcome(
            generated=False,
            reason=f"원산지코드가 숫자가 아닙니다: '{origin}'",
            origin_missing=True,
            rejected_lines=all_rejected + [f"'{origin}' — 원산지코드가 숫자가 아님"],
        )

    # 카테고리코드 검증 (선택 — 있으면 숫자여야).
    cat = (category_code or "").strip()
    if cat and not _CODE_PATTERN.match(cat):
        return Outcome(
            generated=False,
            reason=f"카테고리코드가 숫자가 아닙니다: '{cat}'",
            rejected_lines=all_rejected + [f"'{cat}' — 카테고리코드가 숫자가 아님"],
        )

    # 대표이미지 URL 조달 (외부 서비스 의존 제거).
    # 업로드 실패 시 ImageResolveError 를 잡아 엑셀 생성을 거부한다.
    try:
        image_url = _resolve_dummy_image_url(
            user_image_url=main_image_url,
            upload_fn=upload_fn,
        )
    except ImageResolveError as exc:
        return Outcome(
            generated=False,
            reason=str(exc),
            rejected_lines=all_rejected,
        )

    # 엑셀 생성.
    try:
        output = _inject_dummy_rows(
            src_xlsx,
            dst_xlsx,
            notice_codes=notice_codes,
            shipping_codes=shipping_codes,
            as_codes=as_codes,
            origin_code=origin,
            image_url=image_url,
            category_code=cat,
        )
    except (ValueError, OSError, zipfile.BadZipFile) as exc:
        type_name = type(exc).__name__
        return Outcome(
            generated=False,
            reason=f"{type_name}: 엑셀 생성 실패 — {exc}",
            rejected_lines=all_rejected,
        )

    row_count = max(len(notice_codes), len(shipping_codes), len(as_codes), 1)
    return Outcome(
        generated=True,
        output_path=output,
        row_count=row_count,
        rejected_lines=all_rejected,
    )


# ---------------------------------------------------------------------------
# 폼 HTML 생성.
#
# **방어 계약**:
#   - ``<script>`` 0개 (순수 HTML 폼 POST).
#   - 비밀값 미출력 (결과 페이지에 코드 값 자체를 노출하지 않는다).
#   - 규제값 예시값 창작 금지 (원산지코드 placeholder 에 형식만 표시).
#   - 가등록 고지 문구 (D50 보강).
# ---------------------------------------------------------------------------
_FORM_CSS = """
body{margin:0;padding:24px;background:#f5f5f5;font-family:-apple-system,
  BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  color:#222;font-size:14px;line-height:1.6}
.form-wrap{max-width:720px;margin:0 auto;background:#fff;padding:32px;
  border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,0.08)}
.form-header{border-bottom:2px solid #333;padding-bottom:16px;margin-bottom:24px}
.form-title{font-size:22px;font-weight:700;margin:0 0 8px}
.form-subtitle{color:#555;font-size:13px;line-height:1.7}
.form-section{margin:28px 0}
.form-section h2{font-size:16px;font-weight:600;margin:0 0 12px;
  padding-bottom:6px;border-bottom:1px solid #e0e0e0}
.form-section-desc{color:#555;font-size:13px;margin:0 0 16px;line-height:1.7}
.field{margin:16px 0}
.field-label{display:block;font-weight:600;font-size:13px;margin-bottom:4px}
.field-label .req{color:#a50e0e}
.field-label .opt{color:#555;font-weight:400}
.field-input{width:100%;box-sizing:border-box;padding:8px 10px;font-size:14px;
  border:1px solid #ccc;border-radius:4px;font-family:inherit}
textarea.field-input{min-height:80px;resize:vertical}
.field-input:focus{outline:none;border-color:#1a73e8;
  box-shadow:0 0 0 2px rgba(26,115,232,0.15)}
.field-guide{font-size:12px;color:#555;margin-top:4px;line-height:1.6}
.form-note{margin-top:24px;padding:14px;background:#eef6ff;border-radius:6px;
  color:#1a4d8f;font-size:12px;line-height:1.7}
.form-note strong{color:#0b3d7a}
.form-warn{margin-top:16px;padding:14px;background:#fff8e1;border:1px solid #ffe082;
  border-radius:6px;color:#7a5a00;font-size:12px;line-height:1.7}
.form-warn strong{color:#a87900}
.submit-bar{margin-top:28px;padding-top:20px;border-top:1px solid #e0e0e0}
.submit-btn{background:#137333;color:#fff;border:0;border-radius:6px;
  padding:12px 28px;font-size:15px;font-weight:600;cursor:pointer}
.submit-btn:hover{background:#0f5c2b}
"""


def _field_label(name: str, label: str, required: bool) -> str:
    """필드 라벨 HTML. required 면 [필수], 아니면 [선택]."""
    tag = '<span class="req">[필수]</span>' if required else '<span class="opt">[선택]</span>'
    return (
        f'<label class="field-label" for="f-{html.escape(name)}">{html.escape(label)} {tag}</label>'
    )


def render_template_migration_form_html(
    *,
    token: str,
    port: int,
    src_xlsx_path: str = "",
) -> str:
    """템플릿 이관 더미 엑셀 생성 폼 HTML 문자열을 만든다.

    **<script> 0개**: 순수 HTML 폼 POST 만으로 성립한다 (config_form_server 와 동일).
    **비밀값 미출력**: 폼은 빈 칸으로 시작한다.
    **규제값 예시값 금지**: 원산지코드 placeholder 에는 형식만 표시한다.
    **가등록 고지**: D50 보강 문구를 폼 상단에 둔다.

    Args:
        token: 일회용 폼 토큰 (서버가 검증). ``<input type="hidden">`` 으로 싣는다.
        port: 로컬 폼 서버의 포트.
        src_xlsx_path: 원본 A1 엑셀 경로 (안내용 표시).

    Returns:
        완전한 HTML 문서 문자열. 외부 CSS/JS/폰트 참조 없는 인라인 HTML.
    """
    safe_token = html.escape(str(token), quote=True)
    action = f"http://127.0.0.1:{int(port)}/"

    parts = [
        "<!DOCTYPE html>",
        '<html lang="ko">',
        "<head>",
        '<meta charset="utf-8" />',
        '<meta name="viewport" content="width=device-width, initial-scale=1" />',
        "<title>템플릿 이관 — 더미 엑셀 생성</title>",
        "<style>",
        _FORM_CSS,
        "</style>",
        "</head>",
        "<body>",
        '<div class="form-wrap">',
    ]

    # 헤더 + 가등록 고지 문구 (D50 보강).
    parts.append('<div class="form-header">')
    parts.append('<h1 class="form-title">템플릿 이관 — 더미 대량등록 엑셀</h1>')
    parts.append(
        '<p class="form-subtitle">고시·배송·AS 템플릿코드를 넣으면 더미 상품이 '
        "담긴 대량등록 엑셀을 만들어 줍니다. 판매자센터에 업로드하면 템플릿이 "
        "펼쳐진 더미 상품이 가등록됩니다.</p>"
    )
    parts.append("</div>")  # form-header

    # 가등록 고지 (D50 보강).
    parts.append('<div class="form-warn">')
    parts.append(
        "<strong>가등록 고지:</strong> 이 엑셀을 업로드하면 더미 상품 N개가 "
        "<strong>잠시 실제로 등록</strong>됩니다. 등록 직후 자동으로 "
        "<strong>판매중지</strong> 처리하고, 템플릿 값을 읽은 뒤 "
        "<strong>삭제</strong>합니다. 그 사이 짧은 시간 동안 더미 상품이 "
        "<strong>노출될 수 있습니다</strong>. 완료 후 삭제 결과를 표시합니다."
    )
    parts.append("</div>")

    # 숨겨진 토큰.
    parts.append(f'<form id="template-form" method="POST" action="{action}">')
    parts.append(f'<input type="hidden" name="token" value="{safe_token}" />')

    # 원본 엑셀 경로 (안내용).
    if src_xlsx_path:
        parts.append(
            f'<input type="hidden" name="src_xlsx" value="{html.escape(src_xlsx_path, quote=True)}" />'
        )

    # ------------------------------------------------------------------ #
    # 입력 필드.
    # ------------------------------------------------------------------ #
    parts.append('<div class="form-section">')
    parts.append("<h2>템플릿코드</h2>")
    parts.append(
        '<p class="form-section-desc">각 코드는 숫자만 허용됩니다. '
        "여러 개는 줄바꿈·쉼표·공백으로 구분합니다. "
        "숫자가 아닌 값은 어느 줄이 왜 탈락했는지 생성 결과에 표시됩니다 "
        "(조용한 드롭 없음).</p>"
    )

    # 고시 템플릿코드.
    parts.append('<div class="field">')
    parts.append(_field_label("notice_codes", "고시 템플릿코드", required=False))
    parts.append(
        '<textarea id="f-notice_codes" name="notice_codes" '
        'class="field-input" placeholder="[선택] 숫자, 여러 줄 가능"></textarea>'
    )
    parts.append(
        '<div class="field-guide">판매자센터 [상품관리] &gt; [템플릿관리] &gt; '
        "[상품정보제공고시 템플릿관리] 에서 확인할 수 있습니다.</div>"
    )
    parts.append("</div>")

    # 배송비 템플릿코드.
    parts.append('<div class="field">')
    parts.append(_field_label("shipping_codes", "배송비 템플릿코드", required=False))
    parts.append(
        '<textarea id="f-shipping_codes" name="shipping_codes" '
        'class="field-input" placeholder="[선택] 숫자, 여러 줄 가능"></textarea>'
    )
    parts.append(
        '<div class="field-guide">판매자센터 [상품관리] &gt; [템플릿관리] &gt; '
        "[배송비 템플릿관리] 에서 확인할 수 있습니다.</div>"
    )
    parts.append("</div>")

    # AS 템플릿코드.
    parts.append('<div class="field">')
    parts.append(_field_label("as_codes", "A/S 템플릿코드", required=False))
    parts.append(
        '<textarea id="f-as_codes" name="as_codes" '
        'class="field-input" placeholder="[선택] 숫자, 여러 줄 가능"></textarea>'
    )
    parts.append(
        '<div class="field-guide">판매자센터 [상품관리] &gt; [템플릿관리] &gt; '
        "[A/S 템플릿관리] 에서 확인할 수 있습니다.</div>"
    )
    parts.append("</div>")

    parts.append("</div>")  # form-section

    # 규제값 섹션.
    parts.append('<div class="form-section">')
    parts.append("<h2>규제값 (사용자 실값)</h2>")
    parts.append(
        '<p class="form-section-desc">규제 신고값은 사용자가 직접 입력해야 합니다. '
        "기본값이나 예시값을 창작하지 않습니다.</p>"
    )

    # 원산지코드 (필수 — 규제값).
    parts.append('<div class="field">')
    parts.append(_field_label("origin_code", "원산지코드", required=True))
    parts.append(
        '<input type="text" id="f-origin_code" name="origin_code" '
        'class="field-input" autocomplete="off" '
        'placeholder="[필수] 숫자 (원산지 찾기 팝업에서 확인)" />'
    )
    parts.append(
        '<div class="field-guide">원산지 찾기 팝업에서 확인할 수 있습니다. '
        "<strong>규제값이므로 본인 스토어의 실제 값을 입력하세요</strong> — "
        "기본값·예시값을 창작하지 않습니다.</div>"
    )
    parts.append("</div>")

    # 카테고리코드 (선택).
    parts.append('<div class="field">')
    parts.append(_field_label("category_code", "카테고리코드", required=False))
    parts.append(
        '<input type="text" id="f-category_code" name="category_code" '
        'class="field-input" autocomplete="off" '
        'placeholder="[선택] 숫자 (카테고리 찾기 팝업에서 확인)" />'
    )
    parts.append(
        '<div class="field-guide">1행 파일럿을 권장합니다 — 카테고리↔고시템플릿 '
        "궁합이 확인되지 않았으므로, 처음에는 1행짜리로 시험해 보세요.</div>"
    )
    parts.append("</div>")

    parts.append("</div>")  # form-section

    # 대표이미지 섹션 (선택).
    parts.append('<div class="form-section">')
    parts.append("<h2>대표이미지 (선택)</h2>")
    parts.append(
        '<p class="form-section-desc">비워두면 패키지에 포함된 더미 이미지를 '
        "업로드해 자동으로 채웁니다. 이미 사용할 이미지 주소가 있거나 업로드가 "
        "막힌 경우에 직접 지정하세요.</p>"
    )

    # 대표이미지 URL (선택).
    parts.append('<div class="field">')
    parts.append(_field_label("main_image_url", "대표이미지 주소", required=False))
    parts.append(
        '<input type="text" id="f-main_image_url" name="main_image_url" '
        'class="field-input" autocomplete="off" '
        'placeholder="[선택] https://example.com/image.png" />'
    )
    parts.append(
        '<div class="field-guide">비워두면 더미 이미지를 자동 업로드합니다. '
        "업로드 실패 시 엑셀 생성이 거부되며 사유가 표시됩니다 — "
        "이 경우 직접 주소를 지정하세요.</div>"
    )
    parts.append("</div>")  # field

    parts.append("</div>")  # form-section

    # 안내문.
    parts.append('<div class="form-note">')
    parts.append(
        "<strong>생성:</strong> [엑셀 생성]을 누르면 로컬 폼 서버가 더미 엑셀을 "
        "만들어 결과 페이지에 경로를 표시합니다. <strong>1행 파일럿 권고</strong>: "
        "카테고리↔고시템플릿 궁합이 미확인이므로 처음에는 고시 1개만 넣어 "
        "1행짜리로 시험하세요. 생성 후 판매자센터에 업로드하세요."
    )
    parts.append("</div>")

    # 저장 버튼.
    parts.append('<div class="submit-bar">')
    parts.append('<button type="submit" class="submit-btn">엑셀 생성</button>')
    parts.append("</div>")

    parts.append("</form>")
    parts.append("</div>")  # form-wrap
    parts.append("</body>")
    parts.append("</html>")
    return "\n".join(parts)


def write_template_migration_form_html(
    html_path: str | Path,
    *,
    token: str,
    port: int,
    src_xlsx_path: str = "",
) -> Path:
    """템플릿 이관 폼 HTML 을 디스크에 쓰고 경로를 반환한다."""
    path = Path(html_path)
    doc = render_template_migration_form_html(
        token=token,
        port=port,
        src_xlsx_path=src_xlsx_path,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 결과 페이지 HTML.
# ---------------------------------------------------------------------------
_RESULT_CSS = """
body{margin:0;padding:32px;background:#f5f5f5;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
  "Helvetica Neue",Arial,sans-serif;color:#222}
.wrap{max-width:640px;margin:0 auto;background:#fff;
  padding:32px;border-radius:8px;
  box-shadow:0 1px 4px rgba(0,0,0,0.08)}
.banner{padding:16px 18px;border-radius:8px;font-size:18px;
  font-weight:700;margin-bottom:16px}
.banner.ok{background:#e6f4ea;color:#137333;border:2px solid #137333}
.banner.err{background:#fce8e6;color:#a50e0e;border:2px solid #a50e0e}
.detail{color:#444;line-height:1.6;font-size:14px;word-break:break-word}
.note{margin-top:20px;padding:12px;background:#eef6ff;border-radius:6px;
  color:#1a4d8f;font-size:12px;line-height:1.6}
.note strong{color:#0b3d7a}
.reject-list{margin:12px 0;padding:12px;background:#fff8e1;border-radius:6px;
  font-size:13px;line-height:1.7}
.reject-list ul{margin:4px 0 0 0;padding-left:20px}
"""


def _result_page(
    *,
    ok: bool,
    status_text: str,
    detail: str,
    rejected_lines: list[str] | None = None,
) -> str:
    """결과 HTML 페이지를 조합. detail 은 이미 html.escape 된 문자열."""
    title = "엑셀 생성 완료" if ok else "엑셀 생성 거부"
    banner_cls = "ok" if ok else "err"

    reject_block = ""
    if rejected_lines:
        lis = "".join(f"<li>{html.escape(r)}</li>" for r in rejected_lines)
        reject_block = (
            '<div class="reject-list"><strong>탈락한 코드:</strong><ul>' + lis + "</ul></div>"
        )

    return (
        "<!DOCTYPE html>\n"
        '<html lang="ko"><head><meta charset="utf-8" />'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />'
        f"<title>{html.escape(title)}</title>"
        "<style>" + _RESULT_CSS + "</style></head><body>"
        '<div class="wrap">'
        f'<div class="banner {banner_cls}">{html.escape(status_text)}</div>'
        f"{reject_block}"
        f'<div class="detail">{detail}</div>'
        '<div class="note">이 페이지는 로컬 폼 서버의 처리 결과입니다. '
        "코드 값 자체는 표시되지 않습니다 (비밀값 비노출).</div>"
        "</div></body></html>"
    )


# ---------------------------------------------------------------------------
# HTTP 핸들러 — config_form_server 와 동일한 구조.
# ---------------------------------------------------------------------------
class _TemplateFormHandler(http.server.BaseHTTPRequestHandler):
    """템플릿 이관 폼 1건의 처리를 받는 HTTP 핸들러.

    ``application/x-www-form-urlencoded`` 폼 본문만 받는다.
    CORS 헤더는 절대 내보내지 않는다. 로그에 토큰·코드값이 찍히지 않도록
    ``log_message`` 를 덮어쓴다.
    """

    server_version = "clossify-template-form"
    sys_version = ""

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path not in ("/", "/generate"):
            self._reject_html(404, "not_found", "알 수 없는 경로입니다.")
            return
        self._handle_generate()

    def do_GET(self) -> None:
        self._reject_html(405, "method_not_allowed", "GET 은 지원하지 않습니다.")

    def do_OPTIONS(self) -> None:
        self._reject_html(405, "method_not_allowed", "CORS preflight 는 지원하지 않습니다.")

    def _handle_generate(self) -> None:
        srv = self.server.template_form_state  # type: ignore[attr-defined]
        if srv is None:
            self._reject_html(500, "no_state", "서버 상태를 사용할 수 없습니다.")
            return

        # 1. 만료 검사.
        if srv.is_expired():
            self._respond_html(
                410,
                _result_page(
                    ok=False,
                    status_text="폼 대기 시간이 만료되었습니다.",
                    detail=html.escape("10분이 경과했습니다. 새 폼을 받으세요."),
                ),
            )
            srv.shutdown_from_request()
            return

        # 2. Origin/Referer 검사.
        if not approval_server.origin_referer_ok(self.headers):
            self._respond_html(
                403,
                _result_page(
                    ok=False,
                    status_text="허용되지 않은 Origin/Referer 입니다.",
                    detail=html.escape("file:// 이외의 출처에서 온 요청은 거부됩니다."),
                ),
            )
            return

        # 3. 본문 읽기.
        length = self._content_length()
        if length is None:
            self._reject_html(400, "bad_request", "본문이 필요합니다.")
            return
        if length > _MAX_BODY_BYTES:
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
                raw.decode("utf-8"),
                keep_blank_values=True,
                strict_parsing=False,
            )
        except (UnicodeDecodeError, ValueError):
            self._reject_html(400, "bad_form", "폼 본문이 올바르지 않습니다.")
            return

        # 폼 값을 dict 로.
        form_values: dict[str, str] = {}
        for key, value in pairs:
            k = str(key or "").strip()
            if not k:
                continue
            if k == "token":
                continue
            form_values[k] = str(value)

        # 5. 토큰 검증.
        presented = self._extract_token(pairs)
        if not presented:
            self._respond_html(
                401,
                _result_page(
                    ok=False,
                    status_text="폼 토큰이 필요합니다.",
                    detail=html.escape("토큰이 누락되었습니다."),
                ),
            )
            return
        if srv.is_consumed():
            self._respond_html(
                410,
                _result_page(
                    ok=False,
                    status_text="이미 사용된 토큰입니다.",
                    detail=html.escape("이 폼은 이미 제출되었습니다."),
                ),
            )
            return
        if not approval_server.tokens_match(srv.token, presented):
            self._respond_html(
                403,
                _result_page(
                    ok=False,
                    status_text="폼 토큰이 일치하지 않습니다.",
                    detail=html.escape("토큰이 올바르지 않습니다."),
                ),
            )
            return

        # 6. 더미 엑셀 생성.
        notice_raw = form_values.get("notice_codes", "")
        shipping_raw = form_values.get("shipping_codes", "")
        as_raw = form_values.get("as_codes", "")
        origin = form_values.get("origin_code", "")
        category = form_values.get("category_code", "")
        main_image = form_values.get("main_image_url", "")
        src_xlsx = form_values.get("src_xlsx", "") or srv.src_xlsx_path

        # 출력 경로 — 타임스탬프 기반.
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dst_dir = Path(srv.output_dir) if srv.output_dir else Path.cwd() / ".local"
        dst_xlsx = dst_dir / f"template_migration_dummy_{ts}.xlsx"

        outcome = generate_dummy_excel(
            src_xlsx=src_xlsx,
            dst_xlsx=dst_xlsx,
            notice_codes_raw=notice_raw,
            shipping_codes_raw=shipping_raw,
            as_codes_raw=as_raw,
            origin_code=origin,
            category_code=category,
            main_image_url=main_image,
        )

        # 7. 결과 기록하고 토큰 폐기.
        srv.consume(outcome)

        # 8. 결과 페이지 응답.
        if outcome.generated:
            detail_parts = [
                f"생성 파일: {html.escape(outcome.output_path)}",
                f"더미 행 수: {outcome.row_count}개",
            ]
            if outcome.rejected_lines:
                detail_parts.append(f"탈락한 코드 {len(outcome.rejected_lines)}개 (위 목록 참조).")
            detail_parts.append(
                "판매자센터에 이 엑셀을 업로드하세요. "
                "<strong>1행 파일럿 권고</strong>: 카테고리↔고시템플릿 궁합이 "
                "미확인이므로 처음에는 1행짜리로 시험하세요."
            )
            detail = "<br>".join(detail_parts)
            self._respond_html(
                200,
                _result_page(
                    ok=True,
                    status_text="더미 엑셀이 생성되었습니다.",
                    detail=detail,
                    rejected_lines=outcome.rejected_lines,
                ),
            )
        else:
            self._respond_html(
                200,
                _result_page(
                    ok=False,
                    status_text="엑셀 생성이 거부되었습니다.",
                    detail=html.escape(outcome.reason),
                    rejected_lines=outcome.rejected_lines,
                ),
            )

        # 9. 서버 종료 예약.
        srv.shutdown_from_request()

    # ------------------------------------------------------------------ #
    # 응답 헬퍼들. 모두 Access-Control-Allow-Origin 을 내보내지 않는다.
    # ------------------------------------------------------------------ #
    def _reject_html(self, status: int, code: str, detail: str) -> None:
        page = _result_page(
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

    def _extract_token(self, pairs: list[tuple[str, str]]) -> str:
        h = self.headers.get("X-Template-Form-Token")
        if h and h.strip():
            return h.strip()
        for key, value in pairs:
            if str(key or "").strip() == "token":
                v = str(value or "").strip()
                if v:
                    return v
        return ""

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
# TemplateFormServer — config_form_server 와 동일한 구조/수명주기.
# ---------------------------------------------------------------------------
class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    template_form_state: TemplateFormServer | None = None


class TemplateFormServer:
    """템플릿 이관 폼 1건의 처리를 대기하는 로컬 서버.

    사용 흐름:
        srv = TemplateFormServer(
            src_xlsx_path="...", output_dir="...", token=...)
        port = srv.start()
        srv.wait(timeout=...)
        outcome = srv.outcome
    """

    def __init__(
        self,
        *,
        src_xlsx_path: str,
        output_dir: str = "",
        token: str,
        ttl_seconds: int = TTL_SECONDS,
        bind_host: str = "127.0.0.1",
    ) -> None:
        if not src_xlsx_path:
            raise ValueError("src_xlsx_path 가 필요합니다.")
        if not token:
            raise ValueError("token 이 필요합니다.")
        if bind_host not in ("127.0.0.1", "localhost"):
            raise ValueError(f"bind_host 는 127.0.0.1 이어야 합니다 (got {bind_host!r}).")
        self.src_xlsx_path = str(src_xlsx_path)
        self.output_dir = str(output_dir) if output_dir else ""
        self.token = str(token)
        self.ttl_seconds = int(ttl_seconds)
        self.bind_host = "127.0.0.1"
        self._born_at = time.monotonic()
        self._http: _ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._outcome: Outcome | None = None
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
    def outcome(self) -> Outcome | None:
        with self._lock:
            return self._outcome

    def start(self) -> int:
        if self._http is not None:
            raise RuntimeError("TemplateFormServer 는 한 번만 시작할 수 있습니다.")
        httpd = _ThreadingHTTPServer(("127.0.0.1", 0), _TemplateFormHandler)
        httpd.template_form_state = self
        self._http = httpd
        self._port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        self._thread = t
        return self._port

    def wait(self, timeout: float | None = None) -> Outcome:
        if self._http is None:
            raise RuntimeError("start() 를 먼저 호출해야 합니다.")
        deadline = self._born_at + (self.ttl_seconds if timeout is None else float(timeout))
        while True:
            with self._lock:
                if self._outcome is not None:
                    outcome = self._outcome
                    break
            if time.monotonic() >= deadline:
                outcome = Outcome(generated=False, reason="timeout")
                break
            time.sleep(0.05)
        self._shutdown()
        return outcome

    def consume(self, outcome: Outcome) -> None:
        with self._lock:
            if self._consumed:
                return
            self._consumed = True
            self._outcome = outcome

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


def actual_bound_host(server: TemplateFormServer) -> str:
    """서버가 실제로 바인드한 호스트를 반환한다 (방어 1 검증용)."""
    httpd = server._http
    if httpd is None:
        return ""
    return str(httpd.server_address[0])


__all__ = [
    "ImageResolveError",
    "Outcome",
    "TemplateFormServer",
    "_load_shared_strings",
    "actual_bound_host",
    "generate_dummy_excel",
    "parse_codes",
    "render_template_migration_form_html",
    "write_template_migration_form_html",
    "TTL_SECONDS",
]
