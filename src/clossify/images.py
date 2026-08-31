# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""이미지 입력 정규화 + 업로드 가드 정본화.

이 모듈은 이미지 입력(로컬 파일 경로 + 외부 URL)의 단일 정규화 진입점을
제공한다. 기존 ``mcp_server.upload_images`` 의 인라인 검사를 대체하는
**정본 로컬 이미지 가드**(``validate_local_image``)와 SSRF 공격면을 차단하는
**외부 URL 페처**(``fetch_external_image``), 그리고 이 둘을 묶어 호출자에게
구조화된 결과를 돌려주는 ``attach_images`` 진입점으로 구성된다.

의존 방향(DAG): ``images`` 는 ``common``·``naver_client`` 만 import 한다.
``mcp_server`` 는 ``images`` 를 import 해도 된다(최상위 어댑터).

보안 원칙:
  - fail-closed: 검증을 통과하지 못한 항목은 거부되며, 호출자는 ``rejected`` 가
    비어있지 않으면 진행해서는 안 된다 (``attach_images`` docstring 참조).
  - SSRF 기본 OFF: 외부 URL 은 환경변수 ``CLOSSIFY_IMAGE_FETCH_ALLOW_HOSTS``
    로 명시적으로 허용된 호스트만 opt-in. 미설정이면 모든 외부 URL 을 거부.
  - 매직바이트 기반 콘텐츠 검증: 확장자 위장 차단.
  - 심링크·디렉터리 거부 + 업로드 루트 컨테인먼트.
  - DNS 리바인딩/TOCTOU 방지: 해석한 IP 를 고정해 사용한다(주석 한계 참조).
"""

from __future__ import annotations

import ipaddress
import os
import socket
import stat as _statmod
import tempfile
import time as _time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests

from . import naver_client

# --------------------------------------------------------------------------- #
# 상수 — 확장자 화이트리스트, 매직바이트, 크기 상한, 타임아웃/리다이렉트
# --------------------------------------------------------------------------- #
# 네이버 이미지서버 호환 확장자. mcp_server 의 기존 상수과 동일한 의미.
ALLOWED_IMAGE_EXTS: frozenset[str] = frozenset({".jpg", ".jpeg", ".png", ".webp"})

# 단일 이미지 파일 크기 상한 (10MB). mcp_server._MAX_IMAGE_BYTES 와 동일.
MAX_IMAGE_BYTES: int = 10 * 1024 * 1024

# 매직바이트 사전 — 확장자 위장 차단용. 키는 확장자.
# JPEG: FF D8 FF
# PNG : 89 50 4E 47 0D 0A 1A 0A  (8바이트 시그니처)
# WEBP: "RIFF" .... "WEBP"  (12바이트, 0..3=RIFF, 8..11=WEBP)
_MAGIC_BYTES: dict[str, bytes] = {
    ".jpg": b"\xff\xd8\xff",
    ".jpeg": b"\xff\xd8\xff",
    ".png": b"\x89PNG\r\n\x1a\n",
    ".webp": b"RIFF",
}
_WEBP_TRAIL_OFFSET = 8
_WEBP_TRAIL = b"WEBP"

# 외부 URL 페치 방어 설정.
_FETCH_CONNECT_TIMEOUT = 5.0  # connect 타임아웃(초)
_FETCH_READ_TIMEOUT = 15.0  # read 타임아웃(초)
_FETCH_WALL_BUDGET = 30.0  # 전체 벽시계 예산(초)
_FETCH_MAX_HOPS = 4  # 리다이렉트 홉 상한
_FETCH_MAX_BYTES = MAX_IMAGE_BYTES  # 외부 이미지 크기 상한은 로컬과 동일

# 네이버 CDN 호스트 접미사 — 이미 업로드된 URL 은 재업로드하지 않고 통과시킨다.
# 상품 이미지 서버는 shop-phinf.pstatic.net 계열을 사용한다.
_NAVER_CDN_HOST_SUFFIXES: tuple[str, ...] = (".pstatic.net",)


# --------------------------------------------------------------------------- #
# 로컬 이미지 가드 — 정본
# --------------------------------------------------------------------------- #
def _resolve_upload_root() -> str:
    """업로드 루트 결정. ``CLOSSIFY_UPLOAD_ROOT`` 우선, 없으면 cwd.

    mcp_server._resolve_upload_root 와 동일한 규칙을 images 모듈에도 둔다.
    mcp_server 가 이미 자체 규칙을 가지고 있으므로 여기서는 독립 복사본을 둬
    의존 방향(상위 모듈 import 금지)을 지킨다.
    """
    env_root = os.environ.get("CLOSSIFY_UPLOAD_ROOT")
    if env_root and env_root.strip():
        return os.path.normpath(os.path.expandvars(os.path.expanduser(env_root.strip())))
    return os.getcwd()


def _resolve_upload_path(raw_path: str) -> str:
    """상대경로를 ``CLOSSIFY_UPLOAD_ROOT`` 기준 절대경로로 정규화."""
    if os.path.isabs(raw_path):
        return os.path.normpath(raw_path)
    return os.path.normpath(os.path.join(_resolve_upload_root(), raw_path))


def _matches_any_image_magic(head: bytes) -> bool:
    """바이트 헤더가 JPEG/PNG/WEBP 중 하나와 일치하면 True."""
    if head.startswith(_MAGIC_BYTES[".jpg"]):
        return True
    if head.startswith(_MAGIC_BYTES[".png"]):
        return True
    if (
        head.startswith(_MAGIC_BYTES[".webp"])
        and len(head) >= 12
        and head[_WEBP_TRAIL_OFFSET : _WEBP_TRAIL_OFFSET + 4] == _WEBP_TRAIL
    ):
        return True
    return False


def _check_magic_bytes(path: str, ext: str) -> str:
    """파일 헤더가 확장자와 일치하는지 검사. 빈 문자열이면 OK, 사유면 거부."""
    expected = _MAGIC_BYTES.get(ext)
    if expected is None:
        # 화이트리스트 확장자가 아니면 여기까지 오지 않지만 방어적으로.
        return f"매직바이트 검증을 지원하지 않는 확장자: {ext}"
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError as exc:
        return f"파일 헤더 읽기 실패: {exc}"
    if not head.startswith(expected):
        return f"확장자({ext})와 파일 내용(매직바이트)이 일치하지 않습니다 — 위장 의심"
    # WEBP 는 추가로 8..11 위치에 "WEBP" 가 있어야 한다.
    if ext == ".webp":
        if (
            len(head) < _WEBP_TRAIL_OFFSET + 4
            or head[_WEBP_TRAIL_OFFSET : _WEBP_TRAIL_OFFSET + 4] != _WEBP_TRAIL
        ):
            return "WEBP 매직바이트(RIFF....WEBP) 불일치 — 위장 의심"
    return ""


def _check_containment(abs_path: str) -> tuple[bool, str]:
    """업로드 루트 컨테인먼트 검사.

    Returns:
        ``(applies, reason_or_empty)`` — ``applies`` 는 루트가 설정돼 검사가
        적용됐는지. ``reason`` 이 비어있지 않으면 거부 사유.
    """
    env_root = os.environ.get("CLOSSIFY_UPLOAD_ROOT")
    if not env_root or not env_root.strip():
        # 루트 미설정 — 컨테인먼트 검사를 적용하지 않는다 (정책상 통과).
        return False, ""
    root = Path(env_root.strip()).expanduser().resolve()
    try:
        resolved = Path(abs_path).resolve()
    except OSError as exc:
        return True, f"경로 resolve 실패(컨테인먼트 검사 불가): {exc}"
    try:
        resolved.relative_to(root)
    except ValueError:
        return True, (f"업로드 루트({root}) 밖의 절대경로는 허용되지 않습니다: {resolved}")
    return True, ""


def validate_local_image(
    path: str,
    *,
    max_bytes: int | None = None,
    require_image_ext: bool = True,
) -> dict[str, Any]:
    """로컬 이미지 파일 단일 경로를 정본 검증한다.

    검사 항목(전부 통과해야 OK):
      1. 경로가 문자열이고 비어있지 않음.
      2. 심링크 거부(``os.lstat`` 기반).
      3. 존재 + 일반 파일(디렉터리/특수파일 거부).
      4. 확장자 화이트리스트(``require_image_ext=True`` 일 때만).
      5. 파일 크기 상한.
      6. 매직바이트 일치 — 확장자가 화이트리스트에 있으면 그 확장자 기준,
         아니면 JPEG/PNG/WEBP 중 하나와 일치하면 통과.
      7. 업로드 루트 컨테인먼트(``CLOSSIFY_UPLOAD_ROOT`` 설정 시).

    Args:
        path: 검증할 이미지 경로(절대 또는 ``CLOSSIFY_UPLOAD_ROOT`` 기준 상대).
        max_bytes: 크기 상한 오버라이드. ``None`` 이면 ``MAX_IMAGE_BYTES`` 사용.
            호출자가 자신만의 상한을 이미 정의해 둔 경우(예: mcp_server 의
            기존 ``_MAX_IMAGE_BYTES``) 그 값을 그대로 존중한다.
        require_image_ext: 확장자 화이트리스트 검사를 적용할지. 기본 True.
            외부 URL 에서 fetch 한 임시파일(의미없는 .img 확장자)을 검증할
            때는 False 로 설정해 매직바이트 기반 검증만 적용한다.

    Returns:
        ``{"ok": bool, "path": str, "errors": [str, ...], "contained": bool}``
        - ``ok``: 모든 검사 통과.
        - ``path``: 검증한 정규화 절대경로.
        - ``errors``: 거부 사유 문자열 리스트. ``ok=False`` 면 최소 1개.
        - ``contained``: 컨테인먼트 검사가 적용됐는지 여부(루트 설정 시 True).
    """
    cap = MAX_IMAGE_BYTES if max_bytes is None else int(max_bytes)
    result: dict[str, Any] = {
        "ok": False,
        "path": "",
        "errors": [],
        "contained": False,
    }
    if not isinstance(path, str) or not path.strip():
        result["errors"].append("이미지 경로는 비어있지 않은 문자열이어야 합니다.")
        return result

    abs_path = _resolve_upload_path(path)
    result["path"] = abs_path

    # 심링크 거부 — lstat 로 판별(링크 자체를 거부).
    try:
        lst = os.lstat(abs_path)
    except OSError as exc:
        result["errors"].append(f"존재하지 않는 파일(또는 접근 불가): {exc}")
        return result
    if _statmod.S_ISLNK(lst.st_mode):
        result["errors"].append("심볼릭 링크는 보안상 허용되지 않습니다.")
        return result

    # 일반 파일 여부(디렉터리/특수파일 거부). stat 으로 실제 타겟 검사.
    try:
        st = os.stat(abs_path, follow_symlinks=False)
    except OSError as exc:
        result["errors"].append(f"stat 실패: {exc}")
        return result
    if not _statmod.S_ISREG(st.st_mode):
        result["errors"].append("일반 파일이 아닙니다(디렉터리 또는 특수 파일).")
        return result

    # 컨테인먼트 검사.
    contained, contain_reason = _check_containment(abs_path)
    result["contained"] = contained
    if contain_reason:
        result["errors"].append(contain_reason)

    # 확장자 화이트리스트(요청 시에만).
    ext = os.path.splitext(abs_path)[1].lower()
    if require_image_ext and ext not in ALLOWED_IMAGE_EXTS:
        result["errors"].append(
            f"허용되지 않은 확장자: {ext!r} (허용: {sorted(ALLOWED_IMAGE_EXTS)})"
        )

    # 크기 상한.
    try:
        size = os.path.getsize(abs_path)
    except OSError as exc:
        result["errors"].append(f"파일 크기 조회 실패: {exc}")
        size = -1
    if size > cap:
        result["errors"].append(
            f"파일 크기 초과({size} > {cap}, 최대 " f"{cap // (1024 * 1024)}MB)"
        )

    # 매직바이트. 확장자가 화이트리스트에 있으면 그 확장자 기준, 아니면
    # JPEG/PNG/WEBP 중 어느 것과 일치하는지 확인(임시파일 등에 적용).
    if ext in ALLOWED_IMAGE_EXTS:
        magic_reason = _check_magic_bytes(abs_path, ext)
        if magic_reason:
            result["errors"].append(magic_reason)
    else:
        try:
            with open(abs_path, "rb") as fh:
                head = fh.read(16)
            if not _matches_any_image_magic(head):
                result["errors"].append(
                    "매직바이트 불일치 — JPEG/PNG/WEBP 어떤 것과도 일치하지 않음"
                )
        except OSError as exc:
            result["errors"].append(f"파일 헤더 읽기 실패: {exc}")

    result["ok"] = not result["errors"]
    return result


# --------------------------------------------------------------------------- #
# 외부 URL 가드 — 기본 OFF + 호스트 허용목록 opt-in + SSRF 방어
# --------------------------------------------------------------------------- #
def _allowed_hosts() -> tuple[tuple[str, ...], bool]:
    """``CLOSSIFY_IMAGE_FETCH_ALLOW_HOSTS`` 를 파싱해 (hosts, enabled) 반환.

    Returns:
        ``(hosts, enabled)`` — ``enabled=False`` 면 외부 URL fetch 가 전부 거부.
    """
    raw = os.environ.get("CLOSSIFY_IMAGE_FETCH_ALLOW_HOSTS", "")
    if not raw or not raw.strip():
        return (), False
    hosts = tuple(h.strip().lower() for h in raw.split(",") if h.strip())
    return hosts, bool(hosts)


def _is_private_or_special(ip: ipaddress._BaseAddress) -> str:
    """IP 가 사설/루프백/링크로컬/예약/멀티캐스트/미할당 이면 사유를, 아니면 빈 문자열.

    IPv4-mapped IPv6 는 언랩해 재검사한다(``ipv4_mapped`` 속성 사용).
    """
    if isinstance(ip, ipaddress.IPv6Address):
        v4 = ip.ipv4_mapped
        if v4 is not None:
            return _is_private_or_special(v4)
    if ip.is_private:
        return f"사설 주소({ip})"
    if ip.is_loopback:
        return f"루프백({ip})"
    if ip.is_link_local:
        return f"링크 로컬({ip})"
    if ip.is_reserved:
        return f"예약 주소({ip})"
    if ip.is_multicast:
        return f"멀티캐스트({ip})"
    if ip.is_unspecified:
        return f"미할당/0.0.0.0({ip})"
    return ""


def _resolve_host_ips(host: str, resolver: Any = None) -> tuple[list[str], str]:
    """호스트명을 IP 리스트로 해석.

    ``resolver`` 는 테스트 주입용 — 기본값은 ``socket.getaddrinfo``.

    Returns:
        ``(ips, error)`` — ``ips`` 는 정규화된 IP 문자열 리스트.
        ``error`` 가 비어있지 않으면 해석 실패.
    """
    func = resolver if resolver is not None else socket.getaddrinfo
    try:
        infos = func(host, None)
    except (socket.gaierror, OSError, UnicodeError) as exc:
        return [], f"DNS 해석 실패({host}): {exc}"
    ips: list[str] = []
    seen: set[str] = set()
    for row in infos:
        # getaddrinfo 튜플: (family, type, proto, canonname, sockaddr)
        if not isinstance(row, tuple) or len(row) < 5:
            continue
        sockaddr = row[4]
        if not sockaddr:
            continue
        ip_str = sockaddr[0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        key = str(ip_obj)
        if key not in seen:
            seen.add(key)
            ips.append(key)
    if not ips:
        return [], f"DNS 해석 결과 없음({host})"
    return ips, ""


def _validate_resolved_ips(ips: list[str]) -> str:
    """해석된 IP 리스트 각각을 검사. 하나라도 사설/특수면 사유 반환(fail-closed)."""
    for ip_str in ips:
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError as exc:
            return f"IP 파싱 불가({ip_str}): {exc}"
        reason = _is_private_or_special(ip_obj)
        if reason:
            return reason
    return ""


def _validate_url_target(
    url: str,
    allowed_hosts: tuple[str, ...],
    resolver: Any = None,
) -> tuple[list[str], str]:
    """단일 URL 의 스킴/호스트/IP 를 검증.

    Returns:
        ``(ips, reason)`` — ``reason`` 이 빈 문자열이면 OK. ``ips`` 는 검증된
        IP 리스트(후속 접속에 고정해 사용).
    """
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return [], f"허용되지 않은 스킴: {scheme!r} (http/https 만 허용)"
    host = (parsed.hostname or "").lower()
    if not host:
        return [], "URL 호스트가 비어있습니다"
    if host not in allowed_hosts:
        return [], (
            f"호스트가 허용목록에 없음: {host!r} " f"(허용: {list(allowed_hosts) or '없음'})"
        )
    ips, dns_err = _resolve_host_ips(host, resolver=resolver)
    if dns_err:
        return [], dns_err
    ip_reason = _validate_resolved_ips(ips)
    if ip_reason:
        return [], f"해석 IP 가 내부/예약 대역: {ip_reason}"
    return ips, ""


def _rewrite_url_with_ip(url: str, pinned_ip: str) -> tuple[str, str]:
    """URL 의 호스트를 IP 로 치환. ``(rewritten_url, original_host)`` 반환.

    구현 한계(주석 명시):
      Python ``requests`` 는 TLS SNI 를 공개 API 로 제어하지 않는다. 본 함수는
      URL 호스트를 검증된 IP 로 치환해 리졸버 재호출 창을 줄이고 ``Host`` 헤더를
      원 호스트명으로 보존한다. 단 SNI 는 치환된 URL 을 따르므로 HTTPS 의 경우
      인증서 검증이 실패할 수 있다 — 이 경우 호출자가 ``verify=True`` 로 인증서를
      고집하면 접속이 거부된다(안전 실패). HTTP 에는 이 제약이 없다. 완전한
      SNI 고정은 운영체제 수준 소켓 옵션 또는 ``urllib3`` HTTPConnection 강제
      바인딩이 필요해 표준 라이브러리 범위를 벗어난다(주석으로 한계 명시).
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    netloc = pinned_ip
    if parsed.port:
        netloc = f"{pinned_ip}:{parsed.port}"
    rewritten = urlunparse(
        (
            parsed.scheme,
            netloc,
            parsed.path or "/",
            parsed.params,
            parsed.query,
            "",
        )
    )
    return rewritten, host


def fetch_external_image(
    url: str,
    *,
    allowed_hosts: tuple[str, ...] | None = None,
    resolver: Any = None,
    session: Any = None,
    max_hops: int = _FETCH_MAX_HOPS,
) -> dict[str, Any]:
    """외부 URL 이미지를 안전하게 fetch 한다.

    외부 URL 은 제품 전제상 "사용자 본인의 FTP/CDN" 이다. 임의 URL 을 받는
    범용 페처는 SSRF 공격면만 키운다. 따라서:

      - ``allowed_hosts`` 가 비어있으면 즉시 거부(환경변수에서 읽은 결과).
      - 허용목록에 있어도 스킴/IP/리다이렉트 를 매 홉마다 재검증.
      - TOCTOU/DNS 리바인딩 방지를 위해 해석한 IP 를 고정해 접속(한계는 주석).
      - 수신 바이트는 매직바이트 검증 후에만 임시 파일로 저장.

    Args:
        url: fetch 대상 외부 URL.
        allowed_hosts: 허용 호스트 목록. ``None`` 이면 환경변수에서 읽는다.
        resolver: ``socket.getaddrinfo`` 대체(테스트 주입용).
        session: ``requests.Session`` 대체(테스트 주입용).
        max_hops: 리다이렉트 홉 수 상한.

    Returns:
        ``{"ok": bool, "url": str, "temp_path": str | None, "reason": str,
        "hops": int}`` — ``ok=True`` 면 ``temp_path`` 가 가리키는 임시 파일을
        호출자가 사용 후 반드시 삭제해야 한다.
    """
    if allowed_hosts is None:
        allowed_hosts, _enabled = _allowed_hosts()
    result: dict[str, Any] = {
        "ok": False,
        "url": url,
        "temp_path": None,
        "reason": "",
        "hops": 0,
    }
    if not allowed_hosts:
        result["reason"] = (
            "외부 URL fetch 가 비활성화 — 환경변수 "
            "CLOSSIFY_IMAGE_FETCH_ALLOW_HOSTS 에 호스트를 등록하세요"
        )
        return result
    if not isinstance(url, str) or not url.strip():
        result["reason"] = "URL 이 비어있지 않은 문자열이 아님"
        return result

    own_session = session is None
    if own_session:
        session = requests.Session()

    current_url = url
    visited: set[str] = set()
    deadline = _time.monotonic() + _FETCH_WALL_BUDGET
    try:
        for hop in range(1, max_hops + 1):
            result["hops"] = hop
            if _time.monotonic() > deadline:
                result["reason"] = "전체 벽시계 예산 초과"
                return result
            if current_url in visited:
                result["reason"] = "리다이렉트 루프 감지"
                return result
            visited.add(current_url)

            ips, target_err = _validate_url_target(current_url, allowed_hosts, resolver=resolver)
            if target_err:
                result["reason"] = f"hop {hop} 검증 실패: {target_err}"
                return result

            pinned_ip = ips[0] if ips else ""
            if not pinned_ip:
                result["reason"] = f"hop {hop} 검증된 IP 가 없음"
                return result
            rewritten, host = _rewrite_url_with_ip(current_url, pinned_ip)
            try:
                resp = session.get(
                    rewritten,
                    headers={"Host": host},
                    stream=True,
                    timeout=(_FETCH_CONNECT_TIMEOUT, _FETCH_READ_TIMEOUT),
                    allow_redirects=False,
                    verify=True,
                )
            except requests.RequestException as exc:
                result["reason"] = f"hop {hop} 요청 실패: {exc}"
                return result

            # 수동 리다이렉트 루프 — 홉마다 스킴·호스트 허용목록·IP 재검증.
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location") or ""
                resp.close()
                if not location:
                    result["reason"] = f"hop {hop} 리다이렉트 Location 없음"
                    return result
                if not urlparse(location).scheme:
                    parsed_curr = urlparse(current_url)
                    location = urlunparse(
                        (
                            parsed_curr.scheme,
                            parsed_curr.netloc,
                            location,
                            "",
                            "",
                            "",
                        )
                    )
                current_url = location
                continue

            if not (200 <= resp.status_code < 300):
                resp.close()
                result["reason"] = f"hop {hop} HTTP 상태 {resp.status_code}"
                return result

            # 2xx — stream + iter_content 누적 크기 컷 수신.
            chunks: list[bytes] = []
            total = 0
            over = False
            try:
                for chunk in resp.iter_content(chunk_size=8192):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > _FETCH_MAX_BYTES:
                        over = True
                        break
                    chunks.append(chunk)
            except requests.RequestException as exc:
                resp.close()
                result["reason"] = f"hop {hop} 수신 오류: {exc}"
                return result
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
            if over:
                result["reason"] = f"hop {hop} 수신 크기 초과({total} > {_FETCH_MAX_BYTES})"
                return result
            body = b"".join(chunks)
            if not _matches_any_image_magic(body):
                result["reason"] = f"hop {hop} 매직바이트 불일치(이미지가 아님)"
                return result

            # 매직바이트 통과 — 임시 파일로 저장(업로드 루트/cwd 아님).
            fd, temp_path = tempfile.mkstemp(prefix="clossify_fetch_", suffix=".img")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(body)
            except OSError as exc:
                result["reason"] = f"임시 파일 저장 실패: {exc}"
                if temp_path and os.path.exists(temp_path):
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
                return result
            result["ok"] = True
            result["temp_path"] = temp_path
            return result

        result["reason"] = f"리다이렉트 홉 수 상한({max_hops}) 초과"
        return result
    finally:
        if own_session:
            try:
                session.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# 정규화 진입점 — attach_images
# --------------------------------------------------------------------------- #
def _is_naver_cdn_url(url: str) -> bool:
    """``url`` 이 네이버 CDN 호스트인지 판별 — 이미 업로드된 URL 이면 통과."""
    if not isinstance(url, str):
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    return any(host.endswith(suf) for suf in _NAVER_CDN_HOST_SUFFIXES)


def _classify_source(src: str) -> str:
    """소스 문자열을 분류: ``"cdn"``, ``"url"`` 또는 ``"local"``."""
    if not isinstance(src, str) or not src.strip():
        return "local"
    parsed = urlparse(src)
    scheme = (parsed.scheme or "").lower()
    if scheme in ("http", "https"):
        if _is_naver_cdn_url(src):
            return "cdn"
        return "url"
    return "local"


def attach_images(
    sources: list[str],
    *,
    upload_fn: Any = None,
    fetch_fn: Any = None,
    resolver: Any = None,
) -> dict[str, Any]:
    """이미지 소스(로컬 경로 + 외부 URL + 네이버 CDN URL) 를 정규화한다.

    각 소스를 분류해 처리한다:

      - **이미 네이버 CDN URL** → 그대로 ``urls`` 에 포함(재업로드 금지).
      - **로컬 경로** → ``validate_local_image`` 로 검증 후 ``upload_fn`` 업로드.
      - **외부 URL** → ``fetch_external_image`` 로 SSRF 방어 fetch 후 업로드.

    **순서 보존**: ``urls`` 는 입력 순서를 유지한다. 단, **하나라도 거부되면**
    ``urls`` 가 부분 반환되더라도 호출자는 진행해서는 안 된다 — ``rejected`` 가
    비어있지 않으면 **fail-closed 원칙** 으로 전체 작업을 중단해야 한다.
    1번 이미지가 거부됐을 때 2번이 조용히 대표 이미지가 되는 것을 막기 위함이다.

    Args:
        sources: 이미지 소스 문자열 리스트. 입력 순서가 곧 출력 순서.
        upload_fn: 로컬 파일 경로 리스트 → CDN URL 리스트 로 변환하는 함수.
            기본값은 ``naver_client.upload_images``. 테스트 주입 가능.
        fetch_fn: 외부 URL → 임시 파일 경로 로 fetch 하는 함수. 기본값은
            ``fetch_external_image``. 테스트 주입 가능.
        resolver: ``socket.getaddrinfo`` 대체 함수(테스트용).

    Returns:
        ``{"urls": [...], "rejected": [{"index": int, "source": str,
        "reason": str}], "notes": [...]}`` — ``urls`` 는 입력 순서 유지.
    """
    if upload_fn is None:
        upload_fn = naver_client.upload_images
    if fetch_fn is None:
        fetch_fn = fetch_external_image

    result: dict[str, Any] = {
        "urls": [],
        "rejected": [],
        "notes": [],
    }
    if not isinstance(sources, list):
        result["rejected"].append(
            {
                "index": -1,
                "source": str(sources),
                "reason": "sources 는 리스트여야 합니다",
            }
        )
        return result

    # ``urls`` 는 (원래 인덱스, URL) 튜플로 모았다가 마지막에 정렬+언랩.
    url_pairs: list[tuple[int, str]] = []
    pending_local: list[tuple[int, str]] = []  # (원래 인덱스, 경로)
    fetch_cleanup: list[str] = []

    try:
        for idx, src in enumerate(sources):
            kind = _classify_source(src)
            if kind == "cdn":
                # 이미 네이버 CDN URL — 재업로드 금지, 그대로 통과.
                url_pairs.append((idx, src))
                continue
            if kind == "local":
                v = validate_local_image(src)
                if not v["ok"]:
                    result["rejected"].append(
                        {
                            "index": idx,
                            "source": src,
                            "reason": "; ".join(v["errors"]),
                        }
                    )
                    continue
                pending_local.append((idx, v["path"]))
                continue
            # kind == "url"
            fetch_result = fetch_fn(src, resolver=resolver)
            if not fetch_result["ok"]:
                result["rejected"].append(
                    {
                        "index": idx,
                        "source": src,
                        "reason": str(fetch_result.get("reason") or "fetch 실패"),
                    }
                )
                continue
            tmp = fetch_result.get("temp_path")
            if not tmp:
                result["rejected"].append(
                    {
                        "index": idx,
                        "source": src,
                        "reason": "fetch 결과에 임시 경로가 없음",
                    }
                )
                continue
            fetch_cleanup.append(tmp)
            # 임시 파일을 정본 가드로 재검증(매직바이트·크기 등 동일 기준).
            # 단, 임시파일은 .img 확장자를 가지므로 확장자 검사는 제외하고
            # 매직바이트 기반 검증만 적용한다(이미 fetch 단계에서 magic 검증됨).
            v = validate_local_image(tmp, require_image_ext=False)
            if not v["ok"]:
                result["rejected"].append(
                    {
                        "index": idx,
                        "source": src,
                        "reason": "fetch 임시파일 검증 실패: " + "; ".join(v["errors"]),
                    }
                )
                continue
            pending_local.append((idx, tmp))

        # 로컬/페치 경로를 일괄 업로드 — 인덱스 기반 재결합으로 순서 보존.
        if pending_local:
            paths_in_order = [p for _idx, p in pending_local]
            idxs_in_order = [idx for idx, _p in pending_local]
            try:
                uploaded_urls = upload_fn(paths_in_order)
            except Exception as exc:
                # 업로드 자체가 실패하면 모든 pending_local 항목을 거부.
                msg = f"upload 실패: {exc}"
                for idx, p in pending_local:
                    result["rejected"].append(
                        {
                            "index": idx,
                            "source": p,
                            "reason": msg,
                        }
                    )
                uploaded_urls = None
            if uploaded_urls is not None:
                if len(uploaded_urls) != len(pending_local):
                    result["notes"].append(
                        f"upload URL 개수 불일치 (입력 {len(pending_local)} vs "
                        f"반환 {len(uploaded_urls)}) — 일부 항목 누락 가능"
                    )
                for i, idx in enumerate(idxs_in_order):
                    if i < len(uploaded_urls):
                        url_pairs.append((idx, uploaded_urls[i]))
                    else:
                        result["rejected"].append(
                            {
                                "index": idx,
                                "source": paths_in_order[i],
                                "reason": "upload 반환 URL 부재",
                            }
                        )

        # 입력 순서로 정렬 후 언랩.
        url_pairs.sort(key=lambda pair: pair[0])
        result["urls"] = [u for _idx, u in url_pairs]
        result["rejected"].sort(key=lambda r: r["index"])

        if result["rejected"]:
            result["notes"].append(
                "거부된 항목이 있습니다 — 호출자는 fail-closed 원칙으로 "
                "진행을 멈춰야 합니다 (대표 이미지 승격 방지)."
            )
        return result
    finally:
        # 임시 파일은 항상 정리(업로드 루트/cwd 가 아닌 tempfile 위치).
        for tmp in fetch_cleanup:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except OSError:
                pass
