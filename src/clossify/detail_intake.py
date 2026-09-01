# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""외부 상세페이지 인수 — 완성 HTML → 등록용 조각 + CDN 이미지 URL 변환.

**외부 호출 고지**: ``intake_detail_html`` 은 추출한 base64 이미지를 네이버
CDN 에 업로드하기 위해 **네이버 API 를 실호출**한다
(``images.attach_images`` → ``naver_client.upload_images`` 경로).
테스트는 반드시 업로드를 모킹해야 한다.

입력 전제(상세 인수 파이프 실물 표본): 외부 상세페이지 생성 트랙의 산출물은 완성
웹페이지다 — DOCTYPE 완전 문서 · ``<style>`` 블록 · 이미지 전부 ``data:``
base64 내장 · script 0. 네이버 상세(detailContent)는 이 형태를 받지 못한다:
이미지는 네이버 CDN URL 이어야 하고 문서 전체가 아니라 본문 조각이어야 한다.

본 모듈은 그 변환을 담당한다:

  1. ``data:`` base64 이미지를 문서 순서대로 추출해 임시 파일로 저장.
  2. 기존 검증 경로(``images.validate_local_image`` — 확장자·매직바이트·
     크기 상한)를 **우회 없이** 통과시켜 ``images.attach_images`` 로 업로드.
  3. ``src`` 를 CDN URL 로 재작성(순서 보존 — 상세는 순서가 내용이다).
  4. DOCTYPE/head/body 래퍼 제거·script/iframe/외부 리소스 제거 후
     ``register_product(detail_html=...)`` 에 바로 넣을 수 있는 조각 반환.

보안 원칙:
  - 임시 파일은 ``tempfile`` 위치에만 두고 항상 정리(업로드 루트 오염 금지).
  - 경로 검증은 기존 이미지 경로 가드(``images.validate_local_image``)와 같은
    보수성 — 심링크 거부·일반 파일·업로드 루트 컨테인먼트·크기 상한.
    단 HTML 파일이므로 확장자 화이트리스트/매직바이트는 적용하지 않는다.
  - 제거한 script/iframe/외부 리소스는 조용히 사라지지 않고 ``removed``
    카운트로 반환에 드러난다.

의존 방향(DAG): ``images``·``naver_client`` (기존) → ``detail_intake``
(본 모듈). ``mcp_server`` 가 본 모듈을 import 한다(최상위 어댑터).
"""

from __future__ import annotations

import base64
import binascii
import os
import re
import stat as _statmod
import tempfile
from typing import Any
from urllib.parse import urlparse

from . import images

# 입력 HTML 파일 크기 상한 (20MB). 표본(585KB)의 30배 이상 여유.
MAX_INTAKE_BYTES: int = 20 * 1024 * 1024

# 임시 파일 젔두 — 정리 검증(tempdir 오염 0)을 위한 전용 젔두.
_TEMP_PREFIX = "clossify_intake_"

# ``src="data:image/png;base64,...."`` — MIME 는 업로드 가드가 허용하는
# 세 종류로 제한한다(jpg/jpeg 는 동일 확장자군). 문서 순서대로 정합.
_DATA_URI_RE = re.compile(
    r"""(?ix)\bsrc\s*=\s*(?P<q>["']?)data:image/(?P<mime>png|jpe?g|webp);base64,(?P<b64>[A-Za-z0-9+/=\s]+)(?P=q)"""
)

_MIME_TO_EXT: dict[str, str] = {
    "png": ".png",
    "jpeg": ".jpeg",
    "jpg": ".jpg",
    "webp": ".webp",
}

# --- 조각화 정규식들 (전부 대소문자 무시·개행 포함) ---
_DOCTYPE_RE = re.compile(r"(?is)<!DOCTYPE[^>]*>")
_HTML_TAG_RE = re.compile(r"(?is)</?html\b[^>]*>")
_BODY_TAG_RE = re.compile(r"(?is)</?body\b[^>]*>")
_HEAD_BLOCK_RE = re.compile(r"(?is)<head\b[^>]*>.*?</head\s*>")
_HEAD_BARE_RE = re.compile(r"(?is)<head\b[^>]*>|</head\s*>")
_BODY_BLOCK_RE = re.compile(r"(?is)<body\b[^>]*>(?P<inner>.*?)</body\s*>")
_STYLE_BLOCK_RE = re.compile(r"(?is)<style\b[^>]*>.*?</style\s*>")
_SCRIPT_BLOCK_RE = re.compile(r"(?is)<script\b[^>]*>.*?</script\s*>")
_SCRIPT_BARE_RE = re.compile(r"(?is)<script\b[^>]*/?>|</script\s*>")
_IFRAME_BLOCK_RE = re.compile(r"(?is)<iframe\b[^>]*>.*?</iframe\s*>")
_IFRAME_BARE_RE = re.compile(r"(?is)<iframe\b[^>]*/?>|</iframe\s*>")
_LINK_TAG_RE = re.compile(r"(?is)<link\b[^>]*>")
_IMG_TAG_RE = re.compile(r"(?is)<img\b[^>]*/?>")
_IMG_SRC_RE = re.compile(r"""(?is)\bsrc\s*=\s*(?P<q>["']?)(?P<url>[^\s"'>]+)(?P=q)""")
# 인라인 이벤트 핸들러(onclick=...) — 인라인 스크립트이므로 script 로 분류해 제거.
_ON_ATTR_RE = re.compile(r"""(?is)\son\w+\s*=\s*(?P<q>["']).*?(?P=q)""")
_SRC_HREF_ATTR_RE = re.compile(
    r"""(?is)\s(?P<attr>src|href)\s*=\s*(?P<q>["'])(?P<url>[^"']*)(?P=q)"""
)


def _blank_result() -> dict[str, Any]:
    """계약 형태의 빈 결과(실패 시에도 형태는 동일하게)."""
    return {
        "ok": False,
        "detail_html": "",
        "image_urls": [],
        "representative_candidate": "",
        "removed": {"scripts": 0, "iframes": 0, "external_refs": 0},
        "bytes_before": 0,
        "bytes_after": 0,
        "error": None,
    }


def _fail(error: str, *, base: dict[str, Any] | None = None) -> dict[str, Any]:
    """명확한 error 와 함께 실패 반환 — 조용한 실패 금지."""
    out = base if base is not None else _blank_result()
    out["ok"] = False
    out["error"] = error
    return out


def _validate_intake_path(path: str, *, max_bytes: int) -> tuple[str, str]:
    """HTML 파일 경로를 정본 이미지 가드와 같은 보수성으로 검증.

    검사 항목(이미지 가드 ``images.validate_local_image`` 와 같은 보수성):
      1. 비어있지 않은 문자열.
      2. 절대경로만 허용(상대경로는 업로드 루트 해석 모호성으로 거부).
      3. 심링크 거부(``os.lstat``).
      4. 존재 + 일반 파일.
      5. 업로드 루트 컨테인먼트(``CLOSSIFY_UPLOAD_ROOT`` 설정 시) —
         images 모듈의 동일 검사를 재사용한다(같은 패키지 내부 정본 공유).
      6. 크기 상한.

    Returns:
        ``(abs_path, reason)`` — ``reason`` 이 빈 문자열이면 OK.
    """
    if not isinstance(path, str) or not path.strip():
        return "", "html_path 는 비어있지 않은 문자열이어야 합니다."
    if not os.path.isabs(path):
        return "", f"html_path 는 절대경로만 허용됩니다: {path!r}"
    abs_path = os.path.normpath(path)
    try:
        lst = os.lstat(abs_path)
    except OSError as exc:
        return "", f"존재하지 않는 파일(또는 접근 불가): {exc}"
    if _statmod.S_ISLNK(lst.st_mode):
        return "", "심볼릭 링크는 보안상 허용되지 않습니다."
    try:
        st = os.stat(abs_path, follow_symlinks=False)
    except OSError as exc:
        return "", f"stat 실패: {exc}"
    if not _statmod.S_ISREG(st.st_mode):
        return "", "일반 파일이 아닙니다(디렉터리 또는 특수 파일)."
    # 컨테인먼트 — 이미지 경로 가드의 정본 검사를 그대로 쓴다(동일 보수성).
    _applies, contain_reason = images._check_containment(abs_path)
    if contain_reason:
        return "", contain_reason
    try:
        size = os.path.getsize(abs_path)
    except OSError as exc:
        return "", f"파일 크기 조회 실패: {exc}"
    if size > max_bytes:
        return "", (
            f"입력 HTML 크기 초과({size} > {max_bytes}, " f"최대 {max_bytes // (1024 * 1024)}MB)"
        )
    return abs_path, ""


def _is_external_url(url: str) -> bool:
    """http(s) URL 중 네이버 CDN(.pstatic.net) 이 아닌 외부 리소스인지."""
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    return not any(host.endswith(suf) for suf in images._NAVER_CDN_HOST_SUFFIXES)


def _extract_and_upload(text: str, *, upload_fn: Any) -> tuple[list[str], str, list[str]]:
    """``data:`` base64 이미지를 순서대로 추출·임시 저장·업로드.

    Returns:
        ``(cdn_urls, error, temp_paths)`` — ``error`` 가 빈 문자열이면 OK.
        ``temp_paths`` 는 성공 여부와 무관하게 호출자가 정리해야 한다.
    """
    temp_paths: list[str] = []
    matches = list(_DATA_URI_RE.finditer(text))
    if not matches:
        return [], "", temp_paths
    for m in matches:
        mime = m.group("mime").lower()
        ext = _MIME_TO_EXT[mime]
        try:
            raw = base64.b64decode("".join(m.group("b64").split()), validate=True)
        except (binascii.Error, ValueError) as exc:
            return [], f"base64 디코딩 실패(data:image/{mime}): {exc}", temp_paths
        if not images._matches_any_image_magic(raw[:16]):
            return (
                [],
                (f"data:image/{mime} 매직바이트 불일치 — 이미지가 아니거나 위장 의심"),
                temp_paths,
            )
        fd, tmp = tempfile.mkstemp(prefix=_TEMP_PREFIX, suffix=ext)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
        except OSError as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return [], f"임시 파일 저장 실패: {exc}", temp_paths
        temp_paths.append(tmp)

    # 정본 업로드 경로 — 확장자·매직바이트·크기 상한 검증을 우회 없이 통과.
    # attach_images 는 순서 보존 + 거부 시 fail-closed(rejected 반환).
    attach = images.attach_images(list(temp_paths), upload_fn=upload_fn)
    if attach["rejected"]:
        first = attach["rejected"][0]
        return (
            [],
            (
                f"이미지 업로드 거부(#{first.get('index')}): {first.get('reason')} "
                "— fail-closed 원칙으로 전체를 중단한다"
            ),
            temp_paths,
        )
    return list(attach["urls"]), "", temp_paths


def _fragmentize(text: str) -> tuple[str, dict[str, int]]:
    """문서 조각화 — 래퍼 제거·유해 요소 제거. ``(fragment, removed)`` 반환.

    보수성 기준은 ``detail_render`` 와 같다: 조각은 순수 마크업 +
    인라인 ``<style>`` 블록만 남는다(detail_render 가 옵션 그리드용
    인라인 ``<style>`` 을 본문에 싣는 것과 같은 허용 범위).
    """
    removed = {"scripts": 0, "iframes": 0, "external_refs": 0}

    # 1) 스타일 블록을 먼저 캡처(래퍼 제거 전 문서 전체에서).
    styles = [m.group(0) for m in _STYLE_BLOCK_RE.finditer(text)]

    # 2) script / iframe / 인라인 핸들러 제거(조각화 전에, head 안 포함 전부).
    text, n_script = _SCRIPT_BLOCK_RE.subn("", text)
    removed["scripts"] += n_script
    text, n_bare = _SCRIPT_BARE_RE.subn("", text)
    removed["scripts"] += n_bare
    text, n_iframe = _IFRAME_BLOCK_RE.subn("", text)
    removed["iframes"] += n_iframe
    text, n_bare_if = _IFRAME_BARE_RE.subn("", text)
    removed["iframes"] += n_bare_if
    text, n_on = _ON_ATTR_RE.subn("", text)
    removed["scripts"] += n_on  # 인라인 이벤트 핸들러 = 인라인 스크립트.

    # 3) 외부 리소스: <link> 태그 제거.
    text, n_link = _LINK_TAG_RE.subn("", text)
    removed["external_refs"] += n_link

    # 4) 외부 src 를 가진 <img> 태그 제거(빈 src img 는 무의미·정보 누출).
    kept_imgs: list[str] = []

    def _img_gate(m: re.Match[str]) -> str:
        tag = m.group(0)
        src = _IMG_SRC_RE.search(tag)
        if src and _is_external_url(src.group("url")):
            removed["external_refs"] += 1
            return ""
        kept_imgs.append(tag)
        return tag

    text = _IMG_TAG_RE.sub(_img_gate, text)

    # 5) 남은 외부 src/href 속성 제거(속성 단위).
    def _attr_gate(m: re.Match[str]) -> str:
        if _is_external_url(m.group("url")):
            removed["external_refs"] += 1
            return ""
        return m.group(0)

    text = _SRC_HREF_ATTR_RE.sub(_attr_gate, text)

    # 6) 래퍼 제거 — 본문 조각만 남긴다.
    text = _DOCTYPE_RE.sub("", text)
    body_m = _BODY_BLOCK_RE.search(text)
    if body_m:
        # head 는 body 밖이므로 inner 만 취하면 자연히 버려진다. head 안의
        # 스타일은 1) 에서 이미 캡싱했으므로 손실이 없다.
        text = body_m.group("inner")
    else:
        text = _HEAD_BLOCK_RE.sub("", text)
        text = _HEAD_BARE_RE.sub("", text)
        text = _BODY_TAG_RE.sub("", text)
    text = _HTML_TAG_RE.sub("", text)

    fragment = "\n".join(styles + [text.strip()]).strip()
    return fragment, removed


def intake_detail_html(
    html_path: str, *, upload_fn: Any = None, max_bytes: int | None = None
) -> dict[str, Any]:
    """완성 상세페이지 HTML 파일을 등록용 조각 + 이미지 URL 목록으로 변환한다.

    **외부 호출 고지**: 본 함수는 추출한 이미지 업로드를 위해 **네이버 API 를
    실호출**한다(``images.attach_images`` → ``naver_client.upload_images``).

    처리 순서:
      1. 경로 검증(절대경로·심링크 거부·일반 파일·컨테인먼트·크기 상한).
      2. ``data:`` base64 이미지 추출 → 임시 파일 → 정본 가드 통과 업로드.
      3. ``src`` 를 CDN URL 로 재작성(문서 순서 보존).
      4. DOCTYPE/head/body 래퍼 제거·script/iframe/외부 리소스 제거.

    Args:
        html_path: 입력 HTML 파일 절대경로.
        upload_fn: 업로드 함수(테스트 주입용). 기본값은 네이버 실경로.
        max_bytes: 입력 파일 크기 상한 오버라이드(테스트용).

    Returns:
        ``{"ok": bool, "detail_html": str, "image_urls": [...],
        "representative_candidate": str, "removed": {"scripts": n,
        "iframes": n, "external_refs": n}, "bytes_before": n,
        "bytes_after": n, "error": str | None}`` — ``image_urls[0]`` 이
        대표이미지 후보. ``detail_html`` 은 ``register_product`` 의
        ``detail_html`` 인자로 바로 쓸 수 있는 본문 조각이다.
    """
    result = _blank_result()
    cap = MAX_INTAKE_BYTES if max_bytes is None else int(max_bytes)

    abs_path, reason = _validate_intake_path(html_path, max_bytes=cap)
    if reason:
        return _fail(reason, base=result)

    try:
        with open(abs_path, "rb") as fh:
            raw_bytes = fh.read()
    except OSError as exc:
        return _fail(f"파일 읽기 실패: {exc}", base=result)
    result["bytes_before"] = len(raw_bytes)

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        return _fail(f"UTF-8 디코딩 실패(HTML 은 UTF-8 이어야 함): {exc}", base=result)
    if "<" not in text:
        return _fail("입력이 HTML 이 아닙니다(여는 태그 문자 없음).", base=result)

    temp_paths: list[str] = []
    try:
        urls, up_err, temp_paths = _extract_and_upload(text, upload_fn=upload_fn)
        if up_err:
            return _fail(up_err, base=result)
        result["image_urls"] = urls
        result["representative_candidate"] = urls[0] if urls else ""

        # src 재작성 — data: URI 를 문서 순서대로 CDN URL 로.
        if urls:
            it = iter(urls)

            def _swap(m: re.Match[str]) -> str:
                url = next(it)
                safe = url.replace('"', "%22").replace("'", "%27")
                return f'src="{safe}"'

            text = _DATA_URI_RE.sub(_swap, text)

        fragment, removed = _fragmentize(text)
        result["detail_html"] = fragment
        result["removed"] = removed
        result["bytes_after"] = len(fragment.encode("utf-8"))
        result["ok"] = True
        return result
    finally:
        # 임시 파일은 항상 정리 — 업로드 루트/cwd 가 아닌 tempfile 위치.
        for tmp in temp_paths:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except OSError:
                pass


__all__ = ["MAX_INTAKE_BYTES", "intake_detail_html"]
