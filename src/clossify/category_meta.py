# -*- coding: utf-8 -*-
"""카테고리 메타데이터 조회 헬퍼 (T-110).

``data/category_meta.json`` (및 ``data/certification_types.json``) 을 로드하여
카테고리별 요구사항 판정을 제공한다. ``qa_agents`` 등 후속 단계가 이 모듈로
카테고리 메타를 조회한다.

설계 원칙:
  - **조용한 빈 결과 금지.** 데이터 파일이 부재하면 명확한 에러를 발생시킨다.
  - **알 수 없는 카테고리 ID.** 기본적으로 ``KeyError`` 를 발생시킨다
    (``raise_if_unknown=False`` 인 경우에만 ``None``/기본값 반환 — 문서화됨).
  - 데이터는 캐싱하여 반복 호출 비용을 줄인다.

데이터 파일 위치는 환경변수 ``CLOSSIFY_DATA_DIR`` 로 재정의 가능하며,
기본값은 저장소 루트의 ``data/`` 디렉터리다.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any

# 데이터 디렉터리 기본값: 저장소 루트(본 모듈 기준 상위 2단계) 의 data/.
_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
_DEFAULT_DATA_DIR = os.path.join(_REPO_ROOT, "data")

META_FILENAME = "category_meta.json"
CERT_FILENAME = "certification_types.json"

_KC_FLAG = "KC_CERTIFICATION"

# 모듈 수준 캐시 — 한 프로세스 내에서 반복 로드 비용을 줄인다.
_cache: dict[str, Any] = {}
_cache_lock = threading.Lock()


def _data_dir() -> str:
    custom = os.environ.get("CLOSSIFY_DATA_DIR")
    if custom and custom.strip():
        return os.path.normpath(custom.strip())
    return _DEFAULT_DATA_DIR


def _meta_path() -> str:
    return os.path.join(_data_dir(), META_FILENAME)


def _cert_path() -> str:
    return os.path.join(_data_dir(), CERT_FILENAME)


class CategoryMetaUnavailableError(RuntimeError):
    """카테고리 메타데이터 파일이 존재하지 않거나 읽을 수 없을 때 발생.

    조용한 빈 결과 대신 명확한 에러로 알리기 위한 예외.
    """


class UnknownCategoryError(KeyError):
    """알 수 없는 카테고리 ID 를 조회했을 때 발생.

    ``raise_if_unknown=False`` 인 경우에는 발생하지 않고 ``None``/기본값을
    반환한다.
    """


def load_category_meta(force: bool = False) -> dict:
    """``data/category_meta.json`` 을 로드하여 반환한다.

    한 프로세스 내에서는 캐싱되어 반복 호출 시 디스크 I/O 가 발생하지 않는다.

    Args:
        force: ``True`` 면 캐시를 무시하고 다시 읽는다.

    Returns:
        메타데이터 dict. 최상위 키: ``generated_at``, ``source``, ``count``,
        ``categories`` (리스트).

    Raises:
        CategoryMetaUnavailableError: 파일이 부재하거나 JSON 이 깨진 경우.
    """
    with _cache_lock:
        if not force and "meta" in _cache:
            return _cache["meta"]
        path = _meta_path()
        if not os.path.exists(path):
            raise CategoryMetaUnavailableError(
                f"카테고리 메타데이터 파일이 없습니다: {path}. "
                f"스크립트(scripts/fetch_category_meta.py)를 먼저 실행하세요."
            )
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError) as exc:
            raise CategoryMetaUnavailableError(
                f"카테고리 메타데이터 파일을 읽을 수 없습니다: {path} ({exc})"
            ) from exc
        if not isinstance(doc, dict) or not isinstance(doc.get("categories"), list):
            raise CategoryMetaUnavailableError(
                f"카테고리 메타데이터 구조가 올바르지 않습니다: {path}"
            )
        _cache["meta"] = doc
        # 인덱스도 함께 갱신.
        _cache["index"] = _build_index(doc["categories"])
        return doc


def load_certification_types(force: bool = False) -> list:
    """``data/certification_types.json`` (마스터 인증 57종) 을 로드.

    Args:
        force: ``True`` 면 캐시를 무시하고 다시 읽는다.

    Returns:
        인증 타입 dict 의 리스트 (API 가 반환한 구조 그대로).

    Raises:
        CategoryMetaUnavailableError: 파일이 부재하거나 JSON 이 깨진 경우.
    """
    with _cache_lock:
        if not force and "cert" in _cache:
            return _cache["cert"]
        path = _cert_path()
        if not os.path.exists(path):
            raise CategoryMetaUnavailableError(
                f"인증 타입 파일이 없습니다: {path}. "
                f"스크립트(scripts/fetch_category_meta.py)를 먼저 실행하세요."
            )
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError) as exc:
            raise CategoryMetaUnavailableError(
                f"인증 타입 파일을 읽을 수 없습니다: {path} ({exc})"
            ) from exc
        if not isinstance(doc, list):
            raise CategoryMetaUnavailableError(
                f"인증 타입 파일 구조가 올바르지 않습니다: {path}"
            )
        _cache["cert"] = doc
        return doc


def _build_index(categories: list[dict]) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for c in categories:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id"))
        if cid:
            index[cid] = c
    return index


def _category_index() -> dict[str, dict]:
    if "index" not in _cache:
        load_category_meta()
    return _cache["index"]


def _lookup(category_id: int | str, raise_if_unknown: bool) -> dict | None:
    cid = str(category_id)
    index = _category_index()
    cat = index.get(cid)
    if cat is None:
        if raise_if_unknown:
            raise UnknownCategoryError(
                f"알 수 없는 카테고리 ID: {cid} (메타데이터에 존재하지 않음)"
            )
        return None
    return cat


def requires_kc(category_id: int | str, raise_if_unknown: bool = True) -> bool:
    """해당 카테고리가 KC 인증 관련 처리를 필요로 하는지 여부.

    카테고리의 ``exceptionalCategories`` 에 ``KC_CERTIFICATION`` 이 포함되어
    있으면 ``True``.

    Args:
        category_id: 카테고리 ID.
        raise_if_unknown: ``True`` (기본) 이면 알 수 없는 ID 에서
            ``UnknownCategoryError`` 발생. ``False`` 면 ``False`` 반환.

    Returns:
        KC 인증 필요 여부.

    Raises:
        CategoryMetaUnavailableError: 데이터 파일 부재.
        UnknownCategoryError: 알 수 없는 ID (``raise_if_unknown=True`` 일 때).
    """
    cat = _lookup(category_id, raise_if_unknown)
    if cat is None:
        return False
    flags = cat.get("exceptionalCategories")
    if not isinstance(flags, list):
        return False
    return _KC_FLAG in flags


def exceptional_flags(category_id: int | str, raise_if_unknown: bool = True) -> list[str]:
    """카테고리의 ``exceptionalCategories`` 목록을 반환.

    Args:
        category_id: 카테고리 ID.
        raise_if_unknown: ``True`` (기본) 이면 알 수 없는 ID 에서
            ``UnknownCategoryError`` 발생. ``False`` 면 빈 리스트 반환.

    Returns:
        예외 카테고리 플래그 문자열 리스트. 없으면 빈 리스트.

    Raises:
        CategoryMetaUnavailableError: 데이터 파일 부재.
        UnknownCategoryError: 알 수 없는 ID (``raise_if_unknown=True`` 일 때).
    """
    cat = _lookup(category_id, raise_if_unknown)
    if cat is None:
        return []
    flags = cat.get("exceptionalCategories")
    if isinstance(flags, list):
        return [str(f) for f in flags]
    return []


def category_path(category_id: int | str, raise_if_unknown: bool = True) -> str:
    """카테고리의 전체 경로(``wholeCategoryName``)를 반환.

    Args:
        category_id: 카테고리 ID.
        raise_if_unknown: ``True`` (기본) 이면 알 수 없는 ID 에서
            ``UnknownCategoryError`` 발생. ``False`` 면 빈 문자열 반환.

    Returns:
        카테고리 경로 문자열 (예: ``"패션의류>여성의류>티셔츠>반팔티셔츠"``).

    Raises:
        CategoryMetaUnavailableError: 데이터 파일 부재.
        UnknownCategoryError: 알 수 없는 ID (``raise_if_unknown=True`` 일 때).
    """
    cat = _lookup(category_id, raise_if_unknown)
    if cat is None:
        return ""
    path = cat.get("wholeCategoryName")
    return str(path) if path is not None else ""
