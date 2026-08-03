"""카테고리 메타데이터 수집 스크립트 (T-110).

네이버 커머스 API 에서 전체 카테고리 목록을 조회한 뒤, 리프(``last=true``)
카테고리 전체에 대해 상세 조회하여 ``exceptionalCategories`` 를 모은다.
결과를 ``data/category_meta.json`` 과 ``data/certification_types.json`` 으로
떨군다.

설계 요점 (티켓 요구사항):
  - 인증은 ``clossify.naver_client`` 의 ``get_token``/``_h`` 를 재사용.
    자체 인증 구현 금지.
  - 읽기 전용(GET) 호출만 사용. 상품 등록/수정/삭제 등 쓰기 호출 금지.
  - 동시성 4 이하, 요청 간 최소 간격 유지. 429/5xx 는 지수백오프로
    최대 3회 재시도. 실패한 카테고리는 목록에 남겨 보고(조용한 누락 금지).
  - 재개 가능: ``data/.category_meta.progress.json`` 진행상황 파일에 이미
    받은 카테고리 ID 를 기록하고, 재실행 시 건너뛴다. 전량 재요청 금지.
  - ``certificationInfos`` 는 모든 카테고리에서 동일하므로 1회만 저장한다
    (카테고리별 반복 저장 금지 — 용량 낭비).
  - 수집 데이터에는 계정 식별자·스토어명·토큰이 섞이지 않는다
    (카테고리 메타는 공개 정보).

사용법:
    python scripts/fetch_category_meta.py

환경변수:
    CLOSSIFY_CONFIG  — 설정 파일 경로 (기본: .local/config.json)
"""
from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

# 스크립트 위치 기준으로 src/ 를 import 경로에 추가.
# (설치 환경이 아닌 저장소 체크아웃에서 직접 실행하기 위함)
_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_SRC_PATH = os.path.join(_REPO_ROOT, "src")
if _SRC_PATH not in sys.path:
    sys.path.insert(0, _SRC_PATH)

from clossify import naver_client as nc

DATA_DIR = os.path.join(_REPO_ROOT, "data")
META_PATH = os.path.join(DATA_DIR, "category_meta.json")
CERT_PATH = os.path.join(DATA_DIR, "certification_types.json")
PROGRESS_PATH = os.path.join(DATA_DIR, ".category_meta.progress.json")

# 레이트리밋 배려: 동시성 4, 스레드 간 최소 간격(초).
MAX_WORKERS = 4
MIN_INTERVAL = 0.05  # 초 — 동시성 4 이하에서 서버에 부담 주지 않는 값.
# 429/5xx 재시도: 최대 3회, 지수백오프.
MAX_RETRIES = 3
RETRY_STATUS = {429, 500, 502, 503, 504}
# 진행 로그 주기.
LOG_EVERY = 200

# 카테고리 메타에 노출할 키 (공개 정보만). 내부 식별자/토큰/스토어명은 없음.
CATEGORY_KEYS = ("id", "name", "wholeCategoryName", "last", "exceptionalCategories")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetch_all_categories(tk: str) -> list[dict]:
    """``GET /external/v1/categories`` — 전체 카테고리 목록."""
    r = requests.get(
        nc.BASE + "/external/v1/categories",
        headers=nc._h(tk, json_ct=False),
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise RuntimeError(
            f"categories list expected JSON array, got {type(data).__name__}"
        )
    return data


def _fetch_detail(cid: str, tk: str) -> dict:
    """``GET /external/v1/categories/{id}`` — 1개 카테고리 상세.

    429/5xx 는 지수백오프(1s, 2s, 4s)로 최대 ``MAX_RETRIES`` 회 재시도.
    그 외 4xx 는 즉시 에러로 간주(재시도해도 의미 없음).
    """
    url = nc.BASE + f"/external/v1/categories/{cid}"
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = requests.get(url, headers=nc._h(tk, json_ct=False), timeout=30)
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
                continue
            raise
        if r.status_code in RETRY_STATUS and attempt < MAX_RETRIES:
            # 429 인 경우 Retry-After 헤더가 있으면 존중.
            retry_after = r.headers.get("Retry-After")
            wait = _retry_after_seconds(retry_after)
            if wait is None:
                wait = float(2 ** attempt)
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    # 재시도 전부 소진 — 마지막 예외/상태를 올림.
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"detail fetch exhausted retries for {cid}")


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(int(value))
    except (TypeError, ValueError):
        return None


def _load_progress() -> dict:
    if not os.path.exists(PROGRESS_PATH):
        return {"done": {}}
    try:
        with open(PROGRESS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("done"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"done": {}}


def _save_progress(progress: dict) -> None:
    tmp = PROGRESS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False)
    os.replace(tmp, PROGRESS_PATH)


def _work(cid: str, tk: str) -> tuple[str, dict | None, Exception | None]:
    """스레드 작업 단위. (cid, detail, exc) 반환."""
    try:
        detail = _fetch_detail(cid, tk)
        # 최소 간격 — 스레드별로 직렬화는 아니지만 전체 RPS 를 낮춤.
        if MIN_INTERVAL > 0:
            time.sleep(MIN_INTERVAL)
        return cid, detail, None
    except Exception as exc:  # 개별 실패는 모인다 — 조용한 누락 금지.
        return cid, None, exc


def _certification_infos_first_seen(detail: dict | None) -> list | None:
    if not isinstance(detail, dict):
        return None
    ci = detail.get("certificationInfos")
    return ci if isinstance(ci, list) else None


def collect() -> dict:
    """전체 리프 카테고리 상세를 수집하여 파일로 떨군다.

    Returns:
        결과 요약 dict (``collected``, ``failed``, ``kc_required_count`` 등).
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    tk = nc.get_token()

    print("[fetch_category_meta] 전체 카테고리 목록 조회 중...", flush=True)
    all_cats = _fetch_all_categories(tk)
    total = len(all_cats)
    leaves = [c for c in all_cats if c.get("last") is True]
    print(
        f"[fetch_category_meta] 전체 {total}개, 리프 {len(leaves)}개",
        flush=True,
    )

    # 인증 헤더 재사용 — 토큰은 데이터에 기록하지 않음.
    progress = _load_progress()
    done = progress.get("done", {})
    if not isinstance(done, dict):
        done = {}
        progress["done"] = done

    pending = [c for c in leaves if str(c.get("id")) not in done]
    skipped = len(leaves) - len(pending)
    if skipped:
        print(
            f"[fetch_category_meta] 재개: 이미 수집된 {skipped}개 건너뜀, "
            f"남은 {len(pending)}개 조회 시작",
            flush=True,
        )

    failed: list[dict] = []
    certification_master: list | None = None
    completed = skipped

    # 동시성 제한 — MAX_WORKERS (4) 이하.
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_work, str(c["id"]), tk): c for c in pending}
        for fut in as_completed(futures):
            cid, detail, exc = fut.result()
            if exc is not None or detail is None:
                failed.append({
                    "id": cid,
                    "error": _redact_error(exc),
                })
                continue
            # certificationInfos 최초 1회만 캡처 (모든 카테고리 동일).
            if certification_master is None:
                certification_master = _certification_infos_first_seen(detail)
            # exceptionalCategories 만 진행상황에 저장 (용량 절약).
            exc_cats = detail.get("exceptionalCategories")
            done[cid] = exc_cats if isinstance(exc_cats, list) else []
            completed += 1
            if completed % LOG_EVERY == 0:
                print(
                    f"[fetch_category_meta] 진행: {completed}/{len(leaves)} "
                    f"(실패 {len(failed)})",
                    flush=True,
                )
                # 주기적으로 진행상황 디스크에 flush — 중단 후 재개 대비.
                _save_progress(progress)

    # 최종 진행상황 저장.
    _save_progress(progress)

    # category_meta.json 구성.
    categories_out: list[dict] = []
    kc_required_count = 0
    for c in leaves:
        cid = str(c.get("id"))
        exc_cats = done.get(cid)
        if exc_cats is None:
            # 이번 실행에서 실패한 카테고리 — 보고를 위해 최소한의 정보는 남김.
            exc_cats = []
        if "KC_CERTIFICATION" in exc_cats:
            kc_required_count += 1
        categories_out.append({
            "id": cid,
            "name": c.get("name"),
            "wholeCategoryName": c.get("wholeCategoryName"),
            "last": bool(c.get("last")),
            "exceptionalCategories": list(exc_cats),
        })

    meta_doc = {
        "generated_at": _utc_now_iso(),
        "source": "commerce categories API",
        "count": len(categories_out),
        "categories": categories_out,
    }
    with open(META_PATH, "w", encoding="utf-8") as f:
        json.dump(meta_doc, f, ensure_ascii=False)

    # certification_types.json — 마스터 1회 저장. 구조 그대로.
    if certification_master is None:
        # 드문 케이스: 단 한 건도 성공하지 못함. 빈 배열로라도 남겨 명확히.
        certification_master = []
        print(
            "[fetch_category_meta] 경고: certificationInfos 를 한 건도"
            " 수집하지 못했습니다 — 모든 상세 조회가 실패했을 가능성.",
            flush=True,
        )
    with open(CERT_PATH, "w", encoding="utf-8") as f:
        json.dump(certification_master, f, ensure_ascii=False)

    print(
        f"[fetch_category_meta] 완료: 수집 {len(categories_out)}, "
        f"KC 필요 {kc_required_count}, 실패 {len(failed)}",
        flush=True,
    )
    if failed:
        # 실패 목록은 stdout 요약 + 별도 파일로도 남김 (조용한 누락 금지).
        fail_path = os.path.join(DATA_DIR, ".category_meta.failed.json")
        with open(fail_path, "w", encoding="utf-8") as f:
            json.dump(failed, f, ensure_ascii=False, indent=2)
        print(
            f"[fetch_category_meta] 실패 목록: {fail_path}",
            flush=True,
        )

    return {
        "collected": len(categories_out),
        "failed": failed,
        "kc_required_count": kc_required_count,
        "certification_count": len(certification_master),
        "meta_path": META_PATH,
        "cert_path": CERT_PATH,
    }


def _redact_error(exc: Exception | None) -> str:
    """에러 메시지에서 토큰/계정 식별자가 노출되지 않게 다듬는다.

    Authorization 헤더값이나 config 경로가 예외 메시지에 섞이는 것을 막음.
    """
    if exc is None:
        return "unknown"
    msg = str(exc)
    # Bearer 토큰 패턴이 들어있으면 마스킹.
    import re
    msg = re.sub(r"(Bearer\s+)[A-Za-z0-9._\-]+", r"\1<redacted>", msg)
    return msg[:200]


if __name__ == "__main__":
    collect()
