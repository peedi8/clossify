# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""고시 필드명 → 한국어 라벨/사유 매핑(아래층 모듈).

``data/notice_types.json`` 의 필드명은 camelCase 영어라서 사용자가 바로 이해하기
어렵다. 이 매핑은 거부 응답의 needs_user 항목에 사람이 읽을 수 있는 안내를
제공하기 위해 사용한다. 정책: "필드명만 던지지 말고 사람이 이해할 라벨·
사유를 함께 준다."

**계층 (N77 해소):** 본 모듈은 ``common`` 바로 위층의 아래층 모듈이다.
``requirements`` · ``mcp_server`` 모두 본 모듈에서 라벨을 읽는다.
``mcp_server._notice_field_label`` 은 본 모듈의 함수를 재노출하는 별칭으로
하위 호환을 유지한다.

라벨 데이터는 data/notice_field_labels.json 에서 읽는다(단일 진실 공급원).
이전에는 ``mcp_server`` 에 dict 로 하드코딩되어 있었으나, 문서와 코드가
갈라지는 문제로 데이터 파일로 분리했다. 라벨을 새로 창작하지 않는다 — 출처 기반
수집만 data/notice_field_labels.json 의 labels 에 추가한다.
패키지 데이터 경로는 importlib.resources 기반으로 해결 (install-paths 재배치).
이 라벨 파일은 genuinely optional 이다 — 파일이 없거나 깨지면 호출자가
필드명 그대로를 라벨로 쓴다(아래 _load_notice_field_labels 의 폴백 참고).

의존 방향: ``common`` → 본 모듈. 본 모듈은 위 모듈(``mcp_server`` 등)을
import 하지 않는다.
"""

from __future__ import annotations

import json
import sys

from . import common

_NOTICE_LABELS_PATH = common.package_data_path("notice_field_labels.json")

# 1회 캐시 — 매 호출마다 디스크를 읽지 않는다. None 은 "로드 시도 전",
# dict 는 "로드 결과(성공 시 항목, 실패 시 빈 dict → 폴백)" 을 뜻한다.
_notice_labels_cache: dict[str, tuple[str, str]] | None = None
# 타입별 오버라이드 캵: {TYPE: {field: label}}. labels_by_type 과 동일 모양.
# formats_by_type 패턴과 동일한 구조 문제(같은 필드가 타입마다 다른 이름을 가짐).
_notice_labels_by_type_cache: dict[str, dict[str, str]] | None = None


def _load_notice_field_labels() -> dict[str, tuple[str, str]]:
    """data/notice_field_labels.json 의 필드 단독 라벨을 1회 로드해 캐싱한다.

    파일이 없거나 깨졌을 때: 조용히 죽지 않고 기존 폴백(필드명 그대로 +
    "이 카테고리 고시 필수 항목입니다")으로 떨어지되, 그 사실이 stderr 에
    드러난다(조용한 축소 금지). 호출자에게는 빈 dict 를 반환해 _notice_field_label
    이 필드명 폴백을 적용하도록 한다. 예외를 밖으로 던지지 않는다 —
    needs_user 조립 경로가 컴플라이언스 게이트 안에서 호출되므로, 예외 전파는
    등록 차단(fail-closed 게이트의 오작동)으로 이어진다.

    타입별 라벨(labels_by_type)은 본 함수에서 로드하지 않는다.
    ``_load_notice_field_labels_by_type`` 이 별도로 담당한다. 필드 단독 층과
    타입별 층을 한 번에 로드하면 어느 쪽 결함이 다른 쪽 폴백까지 끌어내릴 수
    있다 — 독립 로드/독립 캐싱한다.
    """
    global _notice_labels_cache
    if _notice_labels_cache is not None:
        return _notice_labels_cache
    loaded: dict[str, tuple[str, str]] = {}
    try:
        with open(_NOTICE_LABELS_PATH, encoding="utf-8") as f:
            doc = json.load(f)
        labels = doc.get("labels") if isinstance(doc, dict) else None
        if isinstance(labels, dict):
            for name, entry in labels.items():
                if not isinstance(entry, dict):
                    continue
                label = entry.get("label")
                hint = entry.get("hint")
                if isinstance(label, str) and isinstance(hint, str):
                    loaded[name] = (label, hint)
    except (OSError, ValueError) as exc:
        # 폴백으로 떨어지되 사실을 stderr 에 남긴다 (조용한 축소 금지).
        # 빈 dict 를 캐싱해 이후 호출이 디스크를 반복해 읽지 않게 한다.
        print(
            f"[clossify] notice_field_labels.json 로드 실패 — 필드명 폴백 적용: {exc}",
            file=sys.stderr,
        )
    _notice_labels_cache = loaded
    return loaded


def _load_notice_field_labels_by_type() -> dict[str, dict[str, str]]:
    """data/notice_field_labels.json 의 labels_by_type 을 1회 로드해 캐싱한다.

    반환 형태: ``{TYPE: {field: label}}``.
    파일이 없거나 labels_by_type 키가 없으면 빈 dict 를 캐싱한다(조용한 폴백).
    이 층이 비어도 _notice_field_label 은 필드 단독 라벨로 정상 동작한다.
    """
    global _notice_labels_by_type_cache
    if _notice_labels_by_type_cache is not None:
        return _notice_labels_by_type_cache
    loaded: dict[str, dict[str, str]] = {}
    try:
        with open(_NOTICE_LABELS_PATH, encoding="utf-8") as f:
            doc = json.load(f)
        by_type = doc.get("labels_by_type") if isinstance(doc, dict) else None
        if isinstance(by_type, dict):
            for type_name, field_map in by_type.items():
                if not isinstance(field_map, dict):
                    continue
                clean: dict[str, str] = {}
                for fname, text in field_map.items():
                    if isinstance(text, str):
                        clean[fname] = text
                if clean:
                    loaded[str(type_name)] = clean
    except (OSError, ValueError) as exc:
        print(
            f"[clossify] notice_field_labels.json labels_by_type 로드 실패 — "
            f"타입별 라벨 없이 진행: {exc}",
            file=sys.stderr,
        )
    _notice_labels_by_type_cache = loaded
    return loaded


def _notice_field_label(field: str, notice_type: str | None = None) -> tuple[str, str]:
    """고시 필드명에 대한 (라벨, 사유) 반환. 매핑 없으면 필드명 그대로.

    ``notice_type`` 이 주어지면 타입별 오버라이드 층(labels_by_type)을 먼저
    조회한다. 같은 필드가 타입마다 다른 한국어 라벨을 가질 수 있다(예:
    COSMETIC.expirationDate = "사용기한 또는 개봉 후 사용기간",
    BIOCIDAL.expirationDate = "유통기한"). 타입별 라벨이 있으면 그것이
    우선한다. 없으면 기존 폴백 순서(필드 단독 라벨 → 필드명 그대로).

    반환 형태 ``(라벨, 사유)`` 는 호출자가 의존하므로 변경하지 않는다.
    타입별 라벨이 사용된 경우에도 사유는 필드 단독 hint 를 유지한다 —
    hint 는 "이 카테고리 고시 필수 항목입니다" 와 같이 필드 성질을 설명하며
    타입이 바뀌어도 의미가 동일하다.
    """
    # 타입별 오버라이드 층을 먼저 본다.
    if notice_type:
        by_type = _load_notice_field_labels_by_type()
        type_map = by_type.get(notice_type)
        if isinstance(type_map, dict):
            type_label = type_map.get(field)
            if isinstance(type_label, str) and type_label:
                # 사유는 필드 단독 hint 를 우선하고, 없으면 기본 사유.
                labels = _load_notice_field_labels()
                _label_only, hint = labels.get(field, (field, ""))
                if not hint:
                    hint = "이 카테고리 고시 필수 항목입니다"
                return (type_label, hint)
    labels = _load_notice_field_labels()
    return labels.get(field, (field, "이 카테고리 고시 필수 항목입니다"))


__all__ = [
    "_load_notice_field_labels",
    "_load_notice_field_labels_by_type",
    "_notice_field_label",
]
