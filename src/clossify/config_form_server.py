# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""최초 설정 폼 서버(localhost setup form bridge).

본 모듈은 **첫 사용자가 브라우저 폼 한 장을 채우고 [저장]을 눌러 설정을 기록**하는
경로의 로컬 서버다. ``approval_server`` 는 "해당 product_key 승인 1건만" 으로 범위가
잠겨 있고 거기에 설정 쓰기를 얹으면 그 보안 계약이 깨진다. 본 모듈은 **설정 쓰기**
범위를 위해 새로 만들어졌으며, **방어는 approval_server 와 동등한 수준**을 상속한다.

방어 10가지 — approval_server 와 동일한 위협 모델
--------------------------------------------------
로컬 포트를 열면 **같은 컴퓨터의 아무 웹페이지나** 그 포트를 찌를 수 있다. 사용자가
다른 탭에서 악성 사이트를 열어두면 그 사이트의 스크립트가 우리 설정 엔드포인트를
호출해 **판매자의 스토어 자격증명(API 키)을 덮어쓸 수 있다.** 공격자가 제어하는
값으로 client_id/client_secret 을 바꾸면, 이후 모든 등록 요청이 공격자 계정으로
향하게 된다. 따라서 본 모듈의 *전부* 가 방어다.

1. **바인딩**: ``127.0.0.1`` 에만 바인드. ``0.0.0.0`` 금지 (approval_server 와 동일).
2. **일회용 토큰**: 설정 폼마다 새로 생성 (``secrets.token_urlsafe``, 32바이트 이상).
   비교는 ``secrets.compare_digest`` (타이밍 공격 방지). approval_server 의
   ``new_token``/``tokens_match`` 를 그대로 재사용 — 단일 진실 공급원.
3. **1회 소진**: 설정 저장 1건 성공 시 토큰 즉시 폐기. 재사용 시도는 거부.
4. **수명 제한**: 대기 시작 후 **10분** 경과 시 자동 만료·서버 종료 (동일 상수).
5. **Origin/Referer 전값 검사**: ``null``/``file://`` 만 허용. 다른 사이트에서 온
   요청은 거부. approval_server 의 ``origin_referer_ok`` 를 재사용.
6. **CORS 금지**: ``Access-Control-Allow-Origin`` 헤더를 **절대 내보내지 않는다.**
7. **범위 제한**: 이 서버가 처리하는 것은 **설정 폼 1건의 저장** 뿐. 다른 동작 불가.
8. **기본 OFF**: ``enable_local_approval`` 가 아닌 **별도 설정**
   ``enable_config_form`` (기본 ``false``) 로 켜고 끈다. 켜야 동작한다.
9. **수명주기**: 요청 처리 후 또는 만료 시 **반드시 서버 종료**. 좀비 포트 금지.
10. **로그에 토큰 금지** + **로그에 설정값(비밀) 금지.**

추가 방어(설정 쓰기 특유):
- **부분 저장**: 빈 칸은 기존 값을 **지우지 않는다**(덮어쓰기 아님). 채운 것만 반영.
- **쓰기 전 백업**: 기존 설정 파일이 있으면 백업본을 남긴 뒤 쓴다.
- **비밀값 비노출**: 결과 페이지·로그에 값 자체를 출력하지 않는다(설정됨/미설정만).
- **폼 인코딩 본문만**: 커스텀 헤더 금지 — 프리플라이트 회피(approval_server 와 동일).

의존 방향: ``approval_server`` (방어 공통) → ``config_form_server``.
본 모듈은 ``preview`` (폼 HTML 생성) 및 ``mcp_server.check_config`` 에 의해 호출된다.
"""

from __future__ import annotations

import datetime
import html
import http.server
import json
import shutil
import socketserver
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

from . import approval_server

# ---------------------------------------------------------------------------
# 설정값(상수). approval_server 와 동일한 10분 수명.
# ---------------------------------------------------------------------------
TTL_SECONDS = approval_server.TTL_SECONDS  # 10분. 만료 후 서버는 종료된다.

# POST 본문 최대 크기. 양식1 은 키 3개 + 토큰. 충분한 여유치.
_MAX_BODY_BYTES = 128 * 1024

# 백업 파일 접미사. 타임스탬프와 함께 저장.
_BACKUP_SUFFIX = ".bak"


# ---------------------------------------------------------------------------
# 결과 타입. 서버가 처리한 설정 저장 결과를 호출자에게 돌려주는 형태.
# ---------------------------------------------------------------------------
class Outcome:
    """설정 폼 처리 결과. 성공이면 saved_keys 가 있고, 실패면 reason 가 있다.

    **비밀값 비노출 계약**: ``saved_keys``/``skipped_keys``/``unchanged_keys``
    는 *키 이름* 만 담는다. *값* 은 절대 담지 않는다 — 결과 객체가 로그/반환에
    흘러들어가도 비밀이 노출되지 않게.
    """

    def __init__(
        self,
        *,
        saved: bool,
        reason: str = "",
        saved_keys: list[str] | None = None,
        skipped_keys: list[str] | None = None,
        unchanged_keys: list[str] | None = None,
        backup_path: str = "",
    ) -> None:
        self.saved = bool(saved)
        self.reason = str(reason)
        # 키 이름만. 값은 절대 담지 않는다.
        self.saved_keys = list(saved_keys) if isinstance(saved_keys, list) else []
        self.skipped_keys = list(skipped_keys) if isinstance(skipped_keys, list) else []
        self.unchanged_keys = list(unchanged_keys) if isinstance(unchanged_keys, list) else []
        self.backup_path = str(backup_path)


# ---------------------------------------------------------------------------
# 허용된 폼 필드 — 화이트리스트. 이 목록에 없는 키는 무시된다(임의 키 쓰기 방지).
#
# **필드를 창작하지 않는다.** 아래 키들은 실제 config 스키마(config.example.json)
# 및 naver_client._notice_defaults / mcp_server._POLICY_CONFIG_KEYS 에서 읽은
# 실제 필드들이다. 새 필드를 추가하려면 먼저 그 소스들이 인식하는지 확인해야 한다.
#
# **양식1 = "한 번만 넣으면 다시 넣을 필요 없는 것" = 계정 자격증명 3개.**
# 직전 산출은 고시 35/35 공통 5필드 + AS 연락처 2필드(=10필드)를
# 양식1 에 올렸다. 재검토 결과:
#   - 고시 5필드: 네이버가 법정 표준 문구 기본 제공 + [상품상세 참조]/[직접입력]
#     선택지를 주며 대량등록 양식에서도 비필수. 검색 1위 상품 2건 모두 상세참조로
#     미룬다. → 판매자가 법조문 5문단을 타이핑할 이유가 없다. 상세참조 기본(D35,
#     별건 티켓) 또는 기존 상품에서 추출(별건 티켓)로 해결된다.
#   - AS 2필드: 실측 결과 afterServiceDirector 22/35 타입, customerServicePhoneNumber
#     13/35 타입으로 공통이 아니며(XOR 관계), detailAttribute.afterServiceInfo
#     별도 노드에도 존재해 상품군 문맥이 필요 → 양식2(상품 등록 시점) 관할.
#
# 따라서 양식1 은 **정확히 3개**만 남긴다. 고시·AS 는 상품 등록 단계에서
# 상품에 맞는 항목만 요청한다 (양식2). 저장 로직의 화이트리스트도 이 3개로
# 좁힌다 — 폼에서만 감추고 서버가 계속 쓰면 의미 없다.
#
# 각 항목: (폼 필드명, config 내 키 경로 튜플, 민감여부).
#   - 민감 필드(client_secret)는 결과 페이지에서 설정됨/미설정만 표시하고 값을
#     출력하지 않는다.
# ---------------------------------------------------------------------------
# 양식1 전체 허용 필드 = 계정 자격증명 3종 (naver 섹션).
_ALLOWED_FIELDS: tuple[tuple[str, tuple[str, ...], bool], ...] = (
    ("client_id", ("naver", "client_id"), False),
    ("client_secret", ("naver", "client_secret"), True),
    ("store_url_slug", ("naver", "store_url_slug"), False),
)

# 폼 필드명 → (config 경로, 민감여부) 조회 인덱스.
_FIELD_INDEX: dict[str, tuple[tuple[str, ...], bool]] = {
    name: (path, sensitive) for name, path, sensitive in _ALLOWED_FIELDS
}

# config 경로 문자열("section.key") → 폼 필드명 역조회(결과 페이지용).
_PATH_TO_FIELD: dict[str, str] = {".".join(path): name for name, path, _ in _ALLOWED_FIELDS}


def allowed_field_names() -> tuple[str, ...]:
    """폼이 허용하는 필드명 목록(테스트·렌더러용)."""
    return tuple(name for name, _, _ in _ALLOWED_FIELDS)


# ---------------------------------------------------------------------------
# 설정 파일 쓰기 헬퍼.
#
# **부분 저장**: 빈 칸은 기존 값을 지우지 않는다. 폼에서 빈 문자열로 온 필드는
# 무시된다(채운 것만 반영). 이것은 "사용자가 일부만 채우고 저장"을 허용하기 위함이다.
#
# **쓰기 전 백업**: 기존 파일이 있으면 ``.local/config.json.bak.<timestamp>`` 로
# 복사한 뒤 쓴다. 원본이 손상되면 백업에서 되돌릴 수 있다.
#
# **임의 키 쓰기 방지**: 화이트리스트(_ALLOWED_FIELDS)에 없는 키는 무시된다.
# 공격자가 폼에 임의 키를 끼워넣어도 config 의 다른 섹션에 쓰이지 않는다.
# ---------------------------------------------------------------------------
def _set_path(cfg: dict[str, Any], path: tuple[str, ...], value: Any) -> bool:
    """config dict 에 다단계 경로로 값을 설정. 경로의 중간 dict 가 없으면 만든다.

    Returns:
        ``True`` = 값이 설정됨. ``False`` = 값이 비어(빈 문자열/None) 설정하지 않음.
    """
    text = str(value).strip() if value is not None else ""
    if not text:
        return False  # 빈 값은 부분 저장 계약에 따라 무시(기존 값을 지우지 않음).
    cur: Any = cfg
    for key in path[:-1]:
        if not isinstance(cur, dict):
            return False
        child = cur.get(key)
        if not isinstance(child, dict):
            child = {}
            cur[key] = child
        cur = child
    if not isinstance(cur, dict):
        return False
    cur[path[-1]] = text
    return True


def _backup_config(config_path: Path) -> str:
    """기존 설정 파일을 백업. 백업 경로를 반환(백업 안 했으면 빈 문자열).

    기존 파일이 있으면 ``<path>.bak.<UTC타임스탬프>`` 로 복사한다.
    """
    if not config_path.is_file():
        return ""
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = config_path.with_suffix(config_path.suffix + f"{_BACKUP_SUFFIX}.{ts}")
    try:
        shutil.copy2(config_path, backup)
        return str(backup)
    except OSError:
        # 백업 실패는 치명적이지 않다 — 쓰기는 계속 진행. 단, 호출자에게 알린다.
        return ""


def write_config_values(
    config_path: str | Path,
    form_values: dict[str, str],
) -> tuple[list[str], list[str], list[str], str]:
    """폼에서 받은 값을 설정 파일에 부분 저장한다.

    **부분 저장 계약**: ``form_values`` 에서 빈 문자열(또는 공백만)인 필드는
    기존 config 값을 **지우지 않는다**. 채운 필드만 반영한다.

    Args:
        config_path: 설정 파일 경로.
        form_values: ``{폼필드명: 값}``. 화이트리스트에 없는 키는 무시된다.

    Returns:
        ``(saved_keys, skipped_keys, unchanged_keys, backup_path)``
        - ``saved_keys``: 새로 설정된 필드명 목록(키 이름만, 값 없음).
        - ``skipped_keys``: 화이트리스트 밖이라 무시된 폼 키 목록.
        - ``unchanged_keys``: 빈 값이 와서 기존 값을 유지한 필드명 목록.
        - ``backup_path``: 백업 파일 경로(백업 안 했으면 빈 문자열).

    Raises:
        OSError: 파일 읽기/쓰기 실패.
        ValueError: JSON 파싱 실패.
    """
    path = Path(config_path)
    # 기존 config 로드 (없으면 빈 구조에서 시작).
    cfg: dict[str, Any] = {}
    if path.is_file():
        with open(path, encoding="utf-8-sig") as f:
            loaded = json.load(f)
            if isinstance(loaded, dict):
                cfg = loaded

    backup_path = _backup_config(path)

    saved_keys: list[str] = []
    skipped_keys: list[str] = []
    unchanged_keys: list[str] = []

    # 화이트리스트 필드만 처리.
    for field_name, value in form_values.items():
        if field_name not in _FIELD_INDEX:
            skipped_keys.append(str(field_name))
            continue
        config_path_tuple, _sensitive = _FIELD_INDEX[field_name]
        set_ok = _set_path(cfg, config_path_tuple, value)
        if set_ok:
            saved_keys.append(field_name)
        else:
            unchanged_keys.append(field_name)

    # 디렉터리 보장.
    path.parent.mkdir(parents=True, exist_ok=True)
    # 쓰기 — UTF-8, 들여쓰기.
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")

    return saved_keys, skipped_keys, unchanged_keys, backup_path


# ---------------------------------------------------------------------------
# 결과 페이지 CSS (삼중 따옴표 — CSS 안의 큰따옴표가 파이썬 문자열 리터럴을
# 깨뜨리지 않게).
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
.blocks{margin:16px 0}
.block{padding:12px;margin:8px 0;border-radius:6px}
.block-label{font-weight:600;font-size:13px;margin-bottom:6px}
.block ul{margin:4px 0 0 0;padding-left:20px}
.block li{font-size:13px;line-height:1.7}
.block.saved{background:#e6f4ea}
.block.unchanged{background:#fff8e1}
.block.skipped{background:#fce8e6}
.detail{color:#444;line-height:1.6;font-size:14px;word-break:break-word}
.note{margin-top:20px;padding:12px;background:#eef6ff;border-radius:6px;
  color:#1a4d8f;font-size:12px;line-height:1.6}
"""


# ---------------------------------------------------------------------------
# 결과 페이지 HTML (서버 렌더).
#
# **비밀값 비노출**: 이 페이지에는 값 자체가 절대 나오지 않는다. 어떤 키가
# 저장됐고/건너뛰어졌고/변경 없음 인지만 표시한다. client_secret 도 마찬가지.
# ---------------------------------------------------------------------------
def _result_page(
    *,
    ok: bool,
    status_text: str,
    saved_keys: list[str],
    skipped_keys: list[str],
    unchanged_keys: list[str],
    detail: str,
) -> str:
    """설정 저장 결과 HTML 페이지를 조합.

    ``detail`` 은 이미 ``html.escape`` 된 문자열이어야 한다(호출자 책임).
    외부 CSS/JS/폰트 참조 없는 인라인 HTML (approval_server 와 동일 규율).
    """
    title = "설정 저장 완료" if ok else "설정 저장 거부"
    banner_cls = "ok" if ok else "err"

    def _list_block(label: str, items: list[str], cls: str) -> str:
        if not items:
            return ""
        lis = "".join(f"<li>{html.escape(name)}</li>" for name in items)
        return (
            f'<div class="block {cls}"><div class="block-label">{html.escape(label)} '
            f"({len(items)})</div><ul>{lis}</ul></div>"
        )

    blocks = (
        _list_block("저장된 항목", saved_keys, "saved")
        + _list_block("변경 없음 (빈 값)", unchanged_keys, "unchanged")
        + _list_block("무시된 항목 (허용되지 않은 키)", skipped_keys, "skipped")
    )

    return (
        "<!DOCTYPE html>\n"
        '<html lang="ko"><head><meta charset="utf-8" />'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />'
        f"<title>{html.escape(title)}</title>"
        "<style>" + _RESULT_CSS + "</style></head><body>"
        '<div class="wrap">'
        f'<div class="banner {banner_cls}">{html.escape(status_text)}</div>'
        f'<div class="blocks">{blocks}</div>'
        f'<div class="detail">{detail}</div>'
        '<div class="note">이 페이지는 로컬 설정 폼 서버의 처리 결과입니다. '
        "값 자체는 표시되지 않습니다 (설정됨/미설정만). "
        "설정이 완료되었는지 확인하려면 채팅에서 check_config 를 다시 호출하세요.</div>"
        "</div></body></html>"
    )


# ---------------------------------------------------------------------------
# 핵심: _ConfigFormHandler. http.server.BaseHTTPRequestHandler 서브클래스.
#
# approval_server._ApprovalHandler 와 동일한 구조:
#   - 핸들러는 인스턴스별로 만들어지므로 서버 상태는 ``self.server.config_form_state``
#     속성에서 읽는다.
#   - POST / 만 지원 (범위 제한).
#   - **폼 인코딩 본문만** (application/x-www-form-urlencoded). 커스텀 헤더 금지 —
#     프리플라이트 회피(approval_server 폼 경로와 동일).
#   - 처리 후 ``server.consume()`` 으로 결과를 기록하고 서버를 종료한다.
#   - **절대** ``Access-Control-Allow-Origin`` 헤더를 내보내지 않는다.
# ---------------------------------------------------------------------------
class _ConfigFormHandler(http.server.BaseHTTPRequestHandler):
    """설정 폼 1건의 저장을 받는 HTTP 핸들러.

    ``application/x-www-form-urlencoded`` 폼 본문만 받는다. JSON 경로가 없다
    (approval_server 와 달리 레거시 JSON 호출자가 없다). 폼 POST 만으로
    CORS 프리플라이트를 유발하지 않는다.

    **CORS 헤더는 절대 내보내지 않는다** (방어 6). 로그에 토큰·설정값이 찍히지
    않도록 ``log_message`` 를 덮어쓴다 (방어 10).
    """

    server_version = "clossify-config-form"
    sys_version = ""

    def do_POST(self) -> None:  # http.server API 대문자 규약.
        path = self.path.split("?", 1)[0]
        if path not in ("/", "/save"):
            self._reject_html(404, "not_found", "알 수 없는 경로입니다.")
            return
        self._handle_save()

    def do_GET(self) -> None:
        # GET 은 허용하지 않는다. 설정 저장은 부작용(파일 쓰기)을 일으키므로 POST 만.
        self._reject_html(405, "method_not_allowed", "GET 은 지원하지 않습니다.")

    def do_OPTIONS(self) -> None:
        # CORS preflight (OPTIONS) 에도 절대 Access-Control-Allow-Origin 을
        # 내보내지 않는다 (approval_server 와 동일).
        self._reject_html(405, "method_not_allowed", "CORS preflight 는 지원하지 않습니다.")

    # ------------------------------------------------------------------ #
    # 설정 저장 처리 본체.
    # ------------------------------------------------------------------ #
    def _handle_save(self) -> None:
        srv = self.server.config_form_state  # type: ignore[attr-defined]
        if srv is None:
            self._reject_html(500, "no_state", "서버 상태를 사용할 수 없습니다.")
            return

        # 1. 만료 검사 (방어 4).
        if srv.is_expired():
            self._respond_html(
                410,
                _result_page(
                    ok=False,
                    status_text="설정 폼 대기 시간이 만료되었습니다.",
                    saved_keys=[],
                    skipped_keys=[],
                    unchanged_keys=[],
                    detail=html.escape(
                        "10분이 경과했습니다. 채팅에서 check_config 를 다시 호출해 폼을 새로 받으세요."
                    ),
                ),
            )
            srv.shutdown_from_request()
            return

        # 2. Origin/Referer 전값 검사 (방어 5) — approval_server 의 함수 재사용.
        if not approval_server.origin_referer_ok(self.headers):
            self._respond_html(
                403,
                _result_page(
                    ok=False,
                    status_text="허용되지 않은 Origin/Referer 입니다.",
                    saved_keys=[],
                    skipped_keys=[],
                    unchanged_keys=[],
                    detail=html.escape("file:// 이외의 출처에서 온 요청은 거부됩니다."),
                ),
            )
            return

        # 3. 본문 읽기 (크기 제한).
        length = self._content_length()
        if length is None:
            self._reject_html(400, "bad_request", "본문이 필요합니다.")
            return
        if length > _MAX_BODY_BYTES:
            self._reject_html(413, "too_large", "요청 본문이 너무 큽니다.")
            return
        raw = self.rfile.read(length)

        # 4. 폼 본문 파싱. application/x-www-form-urlencoded 만 허용 (프리플라이트 회피).
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

        # 폼 키를 dict 로. 같은 키가 여러 번 오면 마지막 값(폼의 일반적 동작).
        form_values: dict[str, str] = {}
        for key, value in pairs:
            k = str(key or "").strip()
            if not k:
                continue
            # token 키는 폼 값이 아니라 토큰 검증용 — form_values 에 넣지 않는다.
            if k == "token":
                continue
            form_values[k] = str(value)

        # 5. 토큰 검증 (방어 2, 3). approval_server 의 tokens_match 재사용.
        presented = self._extract_token(pairs)
        if not presented:
            self._respond_html(
                401,
                _result_page(
                    ok=False,
                    status_text="설정 폼 토큰이 필요합니다.",
                    saved_keys=[],
                    skipped_keys=[],
                    unchanged_keys=[],
                    detail=html.escape("토큰이 누락되었습니다."),
                ),
            )
            return
        # 토큰이 이미 소진되었는지 먼저 검사 (방어 3: 1회 소진).
        if srv.is_consumed():
            self._respond_html(
                410,
                _result_page(
                    ok=False,
                    status_text="이미 사용된 토큰입니다.",
                    saved_keys=[],
                    skipped_keys=[],
                    unchanged_keys=[],
                    detail=html.escape("이 폼은 이미 제출되었습니다. 새 폼을 받으세요."),
                ),
            )
            return
        # secrets.compare_digest 로 일정 시간 비교 (방어 2).
        if not approval_server.tokens_match(srv.token, presented):
            self._respond_html(
                403,
                _result_page(
                    ok=False,
                    status_text="설정 폼 토큰이 일치하지 않습니다.",
                    saved_keys=[],
                    skipped_keys=[],
                    unchanged_keys=[],
                    detail=html.escape("토큰이 올바르지 않습니다."),
                ),
            )
            return

        # 6. 설정 파일에 부분 저장. 화이트리스트 외 키는 무시된다.
        try:
            saved_keys, skipped_keys, unchanged_keys, backup_path = write_config_values(
                srv.config_path, form_values
            )
        except (OSError, ValueError) as exc:
            # 쓰기 실패 — 값은 로그에 남기지 않고 타입+메시지만.
            type_name = type(exc).__name__
            srv.consume(
                Outcome(
                    saved=False,
                    reason=f"{type_name}: 저장 실패",
                )
            )
            self._respond_html(
                500,
                _result_page(
                    ok=False,
                    status_text="설정 파일 저장에 실패했습니다.",
                    saved_keys=[],
                    skipped_keys=[],
                    unchanged_keys=[],
                    detail=html.escape(
                        f"{type_name}: 설정 파일을 쓸 수 없습니다. 파일 권한을 확인하세요."
                    ),
                ),
            )
            srv.shutdown_from_request()
            return

        # 7. 성공. 결과 기록하고 토큰 폐기 (방어 3).
        srv.consume(
            Outcome(
                saved=True,
                saved_keys=saved_keys,
                skipped_keys=skipped_keys,
                unchanged_keys=unchanged_keys,
                backup_path=backup_path,
            )
        )

        # 8. 결과 페이지 응답. CORS 헤더 절대 없음 (방어 6).
        detail_parts: list[str] = []
        if backup_path:
            detail_parts.append(f"기존 설정 백업: {backup_path}")
        detail = " ".join(detail_parts) or "저장되었습니다."
        self._respond_html(
            200,
            _result_page(
                ok=True,
                status_text="설정이 저장되었습니다.",
                saved_keys=saved_keys,
                skipped_keys=skipped_keys,
                unchanged_keys=unchanged_keys,
                detail=html.escape(detail),
            ),
        )

        # 9. 서버 종료 예약 (방어 9). 처리 후 좀비 포트 금지.
        srv.shutdown_from_request()

    # ------------------------------------------------------------------ #
    # 응답 헬퍼들. 모두 ``Access-Control-Allow-Origin`` 을 내보내지 않는다.
    # ------------------------------------------------------------------ #
    def _reject_html(self, status: int, code: str, detail: str) -> None:
        """단순 거부 HTML 응답."""
        page = _result_page(
            ok=False,
            status_text=f"거부됨 (HTTP {status}, {html.escape(code)})",
            saved_keys=[],
            skipped_keys=[],
            unchanged_keys=[],
            detail=html.escape(detail),
        )
        self._respond_html(status, page)

    def _respond_html(self, status: int, page: str) -> None:
        body = page.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 의도적으로 Access-Control-Allow-Origin 은 보내지 않는다 (방어 6).
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ------------------------------------------------------------------ #
    # 토큰 추출. 헤더 우선(approval_server 규약 호환), 없으면 폼 본문 hidden 필드.
    # 폼 POST 는 커스텀 헤더를 쓸 수 없으므로, 실제로는 폼 본문 경로가 쓰인다.
    # ------------------------------------------------------------------ #
    def _extract_token(self, pairs: list[tuple[str, str]]) -> str:
        # 헤더: X-Config-Form-Token (테스트/편의용).
        h = self.headers.get("X-Config-Form-Token")
        if h and h.strip():
            return h.strip()
        # 폼 본문: token=<...> hidden 필드.
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

    # ------------------------------------------------------------------ #
    # 로그 — 토큰·설정값이 찍히지 않도록 기본 출력은 음소거 (방어 10).
    # ------------------------------------------------------------------ #
    def log_message(self, format: str, *args: Any) -> None:
        # 의도적으로 아무것도 출력하지 않는다 — 토큰/비밀이 로그에 노출되지 않게.
        return


# ---------------------------------------------------------------------------
# ConfigFormServer — 설정 폼 1건의 저장을 대기하는 TCP/HTTP 서버.
#
# approval_server.ApprovalServer 와 동일한 구조/수명주기. 방어는 동등하다.
# ---------------------------------------------------------------------------
class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """요청 처리 중 shutdown 이 교착하지 않도록 스레드 기반으로 (approval_server 와 동일)."""

    daemon_threads = True
    allow_reuse_address = True
    config_form_state: ConfigFormServer | None = None


class ConfigFormServer:
    """설정 폼 1건의 저장을 대기하는 로컬 서버.

    사용 흐름:
        srv = ConfigFormServer(config_path=..., token=...)
        port = srv.start()        # 127.0.0.1:<port> 바인드. 포트 확정.
        srv.wait(timeout=...)     # 저장/만료/거부 시까지 대기.
        outcome = srv.outcome     # 결과.

    본 객체는 한 번만 쓴다 — 같은 객체로 두 번 ``start`` 하지 않는다.
    """

    def __init__(
        self,
        *,
        config_path: str,
        token: str,
        ttl_seconds: int = TTL_SECONDS,
        bind_host: str = "127.0.0.1",
    ) -> None:
        if not config_path:
            raise ValueError("config_path 가 필요합니다.")
        if not token:
            raise ValueError("token 이 필요합니다.")
        if bind_host not in ("127.0.0.1", "localhost"):
            # 방어 1: 바인딩 호스트가 127.0.0.1 이 아니면 거부 (approval_server 와 동일).
            raise ValueError(f"bind_host 는 127.0.0.1 이어야 합니다 (got {bind_host!r}).")
        self.config_path = str(config_path)
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

    # ------------------------------------------------------------------ #
    # 수명·소진 상태.
    # ------------------------------------------------------------------ #
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

    # ------------------------------------------------------------------ #
    # 시작·대기·종료.
    # ------------------------------------------------------------------ #
    def start(self) -> int:
        """``127.0.0.1:<자동 할당 포트>`` 에 바인드하고 백그라운드 serve 를 시작.

        포트는 OS 가 자동 할당(포트 0 지정)하며, 할당된 실제 포트를 반환한다.
        """
        if self._http is not None:
            raise RuntimeError("ConfigFormServer 는 한 번만 시작할 수 있습니다.")
        # port=0 으로 OS 자동 할당. host="127.0.0.1" 강제 (방어 1).
        httpd = _ThreadingHTTPServer(("127.0.0.1", 0), _ConfigFormHandler)
        httpd.config_form_state = self
        self._http = httpd
        self._port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        self._thread = t
        return self._port

    def wait(self, timeout: float | None = None) -> Outcome:
        """저장/만료/거부 결과가 확정될 때까지 대기하고 ``Outcome`` 을 반환.

        timeout 이 None 이면 TTL 만큼 대기한다. 결과가 확정되면 서버를 종료한다
        (방어 9). 만료 시에도 서버를 종료하고 ``Outcome(saved=False)`` 반환.
        """
        if self._http is None:
            raise RuntimeError("start() 를 먼저 호출해야 합니다.")
        deadline = self._born_at + (self.ttl_seconds if timeout is None else float(timeout))

        while True:
            with self._lock:
                if self._outcome is not None:
                    outcome = self._outcome
                    break
            if time.monotonic() >= deadline:
                outcome = Outcome(saved=False, reason="timeout")
                break
            time.sleep(0.05)

        # 결과 확정(성공/거부/만료) 후 서버를 반드시 종료한다 (방어 9).
        self._shutdown()
        return outcome

    def consume(self, outcome: Outcome) -> None:
        """저장 결과를 기록하고 토큰을 폐기한다 (방어 3: 1회 소진)."""
        with self._lock:
            if self._consumed:
                return
            self._consumed = True
            self._outcome = outcome

    def shutdown_from_request(self) -> None:
        """요청 처리 스레드가 결과를 확정한 뒤 serve 루프를 끝내도록 요청."""
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
        """명시적 종료 (wait 을 호출하지 않은 경로용 안전망)."""
        self._shutdown()


# ---------------------------------------------------------------------------
# 바인딩 검증 헬퍼 (테스트가 "실제 127.0.0.1 바인드" 를 증명하기 위해 쓴다).
# ---------------------------------------------------------------------------
def actual_bound_host(server: ConfigFormServer) -> str:
    """서버가 실제로 바인드한 호스트를 반환한다 (방어 1 검증용)."""
    httpd = server._http
    if httpd is None:
        return ""
    return str(httpd.server_address[0])


# ---------------------------------------------------------------------------
# 폼 HTML 생성 (브라우저에서 채우는 입력 폼).
#
# **하드 제약 — 예시값 금지**:
#   규제 신고값(원산지·AS 전화·반품비 사유·품질보증기준 등)에 예시값을 달면
#   모델/사용자가 그대로 복사해 넣는다. 양식은 묻기 좋게 만드는 것이지 채우기
#   좋게가 아니다. 따라서 규제 항목의 ``placeholder`` 에는 값의 *형식* 이나
#   *필수 여부* 만 표시하고, 구체적 예시값은 절대 넣지 않는다.
#
#   나쁨: placeholder="예: 국산 - 경북 영천"
#   좋음: placeholder="[필수] 국가 또는 국내 지역명"
#
# **비밀값 비노출**:
#   ``client_secret`` 은 ``type="password"`` 이며, 기존 값을 다시 화면에 뿌리지
#   않는다(설정됨/미설정만 표시). 모든 입력 필드는 빈 칸으로 시작 — 사용자가
#   채운 것만 저장된다(부분 저장 계약).
#
# **폼 POST 구조** (approval_server 미리보기 승인 바와 동일한 패턴):
#   - ``<form method="POST" action="http://127.0.0.1:<port>/">``
#   - ``enctype`` 생략 = ``application/x-www-form-urlencoded`` (CORS 프리플라이트 회피)
#   - 커스텀 헤더 일절 없음 — 토큰은 ``<input type="hidden" name="token">`` 으로.
#   - 전송 자체는 JS 없이 성립 (순수 ``<button type="submit">``).
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
.field-input:focus{outline:none;border-color:#1a73e8;
  box-shadow:0 0 0 2px rgba(26,115,232,0.15)}
.field-status{font-size:12px;margin-top:4px}
.field-status.set{color:#137333}
.field-status.unset{color:#a50e0e}
.field-guide{font-size:12px;color:#555;margin-top:4px;line-height:1.6}
.field-guide code{background:#f0f0f0;padding:1px 4px;border-radius:3px;
  font-size:11px}
.field-todo{font-size:12px;color:#a50e0e;background:#fff8e1;padding:4px 8px;
  border-radius:3px;display:inline-block;margin-top:4px}
.form-note{margin-top:24px;padding:14px;background:#eef6ff;border-radius:6px;
  color:#1a4d8f;font-size:12px;line-height:1.7}
.form-note strong{color:#0b3d7a}
.guide-toggle{margin:8px 0 4px;font-size:12px}
.guide-toggle summary{cursor:pointer;color:#1a4d8f;padding:4px 0;
  list-style:none;display:inline-block}
.guide-toggle summary::-webkit-details-marker{display:none}
.guide-toggle[open] summary{color:#0b3d7a}
.guide-panel{margin-top:8px;padding:12px;background:#f8f9fb;border:1px solid #e3e6ea;
  border-radius:6px;font-size:12px;line-height:1.7;color:#333}
.guide-panel .guide-row{margin:4px 0}
.guide-panel .guide-row strong{color:#0b3d7a}
.guide-panel .guide-trap{background:#fff8e1;padding:8px 10px;border-radius:4px;
  margin-top:6px;color:#7a5a00}
.guide-panel .guide-trap strong{color:#a87900}
.guide-panel .guide-domain{color:#137333}
.guide-panel code{background:#eef0f3;padding:1px 4px;border-radius:3px;font-size:11px}
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


def _status_badge(is_set: bool) -> str:
    """기존 값 설정 상태 배지(값 자체는 표시하지 않는다)."""
    if is_set:
        return '<span class="field-status set">(설정됨 — 빈 칸으로 두면 기존 값 유지)</span>'
    return '<span class="field-status unset">(미설정)</span>'


# ---------------------------------------------------------------------------
# 양식1 은 이제 키 3개뿐이다 — (B)(C) 영역 필드 스펙은 제거됐다.
# 고시 5필드·AS 연락처 2필드는 상품 등록 단계(양식2)에서 상품에 맞는 항목만
# 요청한다. 여기서 받지 않는다.
# ---------------------------------------------------------------------------


def render_config_form_html(
    *,
    token: str,
    port: int,
    config_set_status: dict[str, bool] | None = None,
) -> str:
    """최초 설정 폼 HTML 문자열을 만든다.

    **양식1 = "한 번만 넣으면 다시 넣을 필요 없는 것" = 계정 자격증명 3개.**
      (A) client_id / client_secret(password) / store_url_slug.

    고시 5필드·AS 연락처 2필드는 키 3개를 넣는 *이* 화면에서 받지 않는다.
    고시는 네이버가 법정 표준 문구 기본 제공 + [상품상세 참조] 선택지가 있고,
    AS 연락처는 상품군마다 달라 상품 문맥이 필요하다. 둘 다 상품 등록 단계
    (양식2)에서 상품에 맞는 항목만 요청한다.

    상품마다 달라지는 값(원산지·제조사·수입사·모델명·KC·검색광고·이미지 키)은
    **어디에도 등장하지 않는다** — 폼 HTML 본문·hidden 필드·data 속성·JS 에서
    모두 빠진다. 저장 로직도 이 3개만 허용한다(_ALLOWED_FIELDS 화이트리스트).

    Args:
        token: 일회용 폼 토큰(서버가 검증). ``<input type="hidden">`` 으로 싣는다.
        port: 로컬 설정 폼 서버의 포트. 폼 ``action`` URL 에 들어간다.
        config_set_status: 각 필드의 현재 설정 여부(값 아님). ``{필드명: bool}``.
            ``True`` 면 "(설정됨)" 배지를, ``False`` 면 "(미설정)" 배지를 표시.
            값 자체는 절대 표시하지 않는다(비밀값 비노출 계약).

    Returns:
        완전한 HTML 문서 문자열. 외부 CSS/JS/폰트 참조 없는 인라인 HTML.

    **예시값 금지 계약**: 규제 신고값의 안내문에는 값의 형식/필수여부만 표시하고,
    구체적 예시값은 절대 넣지 않는다.

    **JS 없는 정적 렌더**: 양식1 은 순수 HTML 폼 POST 만으로 성립한다.
    ``<button type="submit">`` 외에 JavaScript 를 전혀 쓰지 않는다(우측구이 정적
    렌더 대응).
    """
    status = config_set_status or {}
    safe_token = html.escape(str(token), quote=True)
    action = f"http://127.0.0.1:{int(port)}/"

    parts = [
        "<!DOCTYPE html>",
        '<html lang="ko">',
        "<head>",
        '<meta charset="utf-8" />',
        '<meta name="viewport" content="width=device-width, initial-scale=1" />',
        "<title>클로시파이 최초 설정</title>",
        "<style>",
        _FORM_CSS,
        "</style>",
        "</head>",
        "<body>",
        '<div class="form-wrap">',
    ]

    # 헤더. "부분 저장이 가능함" 을 화면에 알린다(계약 5). 3칸짜리 화면임이 드러나게.
    parts.append('<div class="form-header">')
    parts.append('<h1 class="form-title">클로시파이 최초 설정</h1>')
    parts.append(
        '<p class="form-subtitle">첫 사용자를 위한 설정 폼입니다 — 항목 3개로 끝납니다. '
        "필요한 항목만 채우고 [저장]을 누르세요. "
        "<strong>다 채우지 않아도 저장할 수 있습니다</strong> — 빈 칸은 기존 값을 "
        "지우지 않습니다. 10분 안에 저장하지 않으면 폼이 만료됩니다.</p>"
    )
    parts.append("</div>")  # form-header

    # 숨겨진 토큰. 폼 POST 로 서버에 전달(커스텀 헤더 없이).
    parts.append(f'<form id="config-form" method="POST" action="{action}">')
    parts.append(f'<input type="hidden" name="token" value="{safe_token}" />')

    # ------------------------------------------------------------------ #
    # (A) 계정 자격증명 — 키 3종. 양식1 의 전부.
    #
    # 안내 문구 계약(이슈: "어디서 무엇을 받는지" 가 드러나야):
    #   - 발급처는 "네이버 커머스API 센터" 이다. 검색·지도용 "네이버 개발자센터"
    #     와 다른 곳이므로 명시적으로 구분한다.
    #   - client_id/client_secret = 커머스API 센터 [애플리케이션 > 내스토어 애플리케이션]
    #     에서 등록하면 발급되는 애플리케이션 ID·시크릿.
    #   - store_url_slug = 스토어 주소 smartstore.naver.com/<이 부분>. 발급물이 아님.
    #   - 사전 조건(통합매니저 권한) 과 함정(API 호출 IP 등록) 은 접기 패널에
    #     한 덩어리로 둔다 — 본문 3칸 구조를 가리지 않게.
    #   - 링크는 도메인까지만(apicenter.commerce.naver.com). 하위 경로는 확인되지
    #     않았으므로 지어내지 않는다. 대신 메뉴명으로 안내한다.
    # ------------------------------------------------------------------ #
    parts.append('<div class="form-section">')
    parts.append("<h2>네이버 커머스 API 자격증명</h2>")
    parts.append(
        '<p class="form-section-desc">스마트스토어 상품 등록에 필요한 값 3개입니다. '
        "발급처는 <strong>네이버 커머스API 센터</strong>입니다 "
        '(검색·지도용인 "네이버 개발자센터"와 다른 곳입니다). '
        '각 항목 아래 "어디서 가져오나?" 을 누르면 출처가 열립니다.</p>'
    )

    # client_id.
    parts.append('<div class="field">')
    parts.append(_field_label("client_id", "Client ID", required=True))
    parts.append(
        '<input type="text" id="f-client_id" name="client_id" '
        'class="field-input" autocomplete="off" />'
    )
    parts.append(_status_badge(bool(status.get("client_id"))))
    parts.append(
        '<div class="field-guide">커머스API 센터에서 발급받는 '
        "<code>애플리케이션 ID</code> 입니다.</div>"
    )
    parts.append(
        '<details class="guide-toggle"><summary>어디서 가져오나?</summary>'
        '<div class="guide-panel">'
        '<div class="guide-row"><strong>발급처:</strong> '
        '<span class="guide-domain">apicenter.commerce.naver.com</span> '
        "(네이버 커머스API 센터). 메뉴 <strong>[애플리케이션] &gt; "
        "[내스토어 애플리케이션]</strong> 에서 애플리케이션을 등록하면 "
        "<strong>애플리케이션 ID</strong>가 발급됩니다 — 이 값이 Client ID 입니다."
        "</div>"
        '<div class="guide-trap"><strong>주의 — 다른 곳 아님:</strong> '
        '검색·지도용 "네이버 개발자센터"의 Client ID 와 이름이 같지만 '
        "<strong>전혀 다른 센터</strong>입니다. 커머스API 센터에서 받아야 합니다."
        "</div>"
        "</div></details>"
    )
    parts.append("</div>")

    # client_secret (password).
    parts.append('<div class="field">')
    parts.append(_field_label("client_secret", "Client Secret", required=True))
    parts.append(
        '<input type="password" id="f-client_secret" name="client_secret" '
        'class="field-input" autocomplete="new-password" />'
    )
    parts.append(_status_badge(bool(status.get("client_secret"))))
    parts.append(
        '<div class="field-guide">같은 애플리케이션의 <code>시크릿</code> 입니다. '
        "비밀값이므로 화면에 다시 표시되지 않습니다. 입력해도 저장 전까지는 "
        "어디에도 기록되지 않습니다.</div>"
    )
    parts.append(
        '<details class="guide-toggle"><summary>어디서 가져오나?</summary>'
        '<div class="guide-panel">'
        '<div class="guide-row"><strong>발급처:</strong> Client ID 와 같은 화면 — '
        "커머스API 센터 <strong>[애플리케이션] &gt; [내스토어 애플리케이션]</strong>. "
        "애플리케이션을 등록하면 <strong>애플리케이션 ID</strong>와 함께 "
        "<strong>시크릿</strong>이 발급됩니다 — 이 값이 Client Secret 입니다."
        "</div>"
        "</div></details>"
    )
    parts.append("</div>")

    # store_url_slug.
    parts.append('<div class="field">')
    parts.append(_field_label("store_url_slug", "스토어 URL 슬러그", required=True))
    parts.append(
        '<input type="text" id="f-store_url_slug" name="store_url_slug" '
        'class="field-input" autocomplete="off" />'
    )
    parts.append(_status_badge(bool(status.get("store_url_slug"))))
    parts.append(
        '<div class="field-guide">스토어 주소 <code>smartstore.naver.com/&lt;이 부분&gt;</code> '
        "의 그 부분입니다. <strong>발급받는 값이 아닙니다</strong> — 스토어 주소의 일부입니다."
        "</div>"
    )
    parts.append(
        '<details class="guide-toggle"><summary>어디서 가져오나?</summary>'
        '<div class="guide-panel">'
        '<div class="guide-row"><strong>확인처:</strong> 본인 스토어의 주소 '
        "<code>https://smartstore.naver.com/<strong>이_부분</strong></code>. "
        "이 값을 발급받는 게 아니라 스토어 주소에서 읽습니다."
        "</div>"
        '<div class="guide-trap"><strong>혼동 주의:</strong> '
        "스토어 관리자 주소(<code>sell.smartstore.naver.com/.../#/my/store/slug</code>) "
        "에도 slug 가 보이지만, 실제 스토어 주소의 그것과 같은지 반드시 "
        "스토어 주소로 확인하세요."
        "</div>"
        "</div></details>"
    )
    parts.append("</div>")

    # 사전 조건·함정 접기 패널 — 3칸 본문을 가리지 않게 한 덩어리로 접어둔다.
    parts.append(
        '<details class="guide-toggle"><summary>'
        "키 발급 전/후에 꼭 봐야 할 사전 조건과 함정"
        "</summary>"
        '<div class="guide-panel">'
        '<div class="guide-row"><strong>사전 조건 — 통합매니저 권한:</strong> '
        "키를 발급·조회하려면 해당 스토어의 <strong>통합매니저 권한</strong>이 "
        "필요합니다. 권한이 없으면 커머스API 센터에서 애플리케이션을 만들거나 "
        "조회할 수 없습니다."
        "</div>"
        '<div class="guide-row"><strong>등록 시 — API 호출 IP:</strong> '
        "애플리케이션을 만들 때 <strong>API 호출 IP 입력</strong> 항목이 있습니다. "
        "이 IP 가 아니면 키가 있어도 호출이 막힙니다 — 원인이 화면에 명확히 "
        "드러나지 않는 경우가 많으므로 발급 단계에서 반드시 챙기세요."
        "</div>"
        '<div class="guide-row"><strong>스토어당 1개:</strong> '
        "스토어 하나당 만들 수 있는 애플리케이션은 <strong>최대 1개</strong>입니다. "
        "이미 만들어둔 애플리케이션이 있으면 새로 만들지 말고 그 값을 쓰세요."
        "</div>"
        '<div class="guide-trap"><strong>다시 한 번 — 다른 센터 아님:</strong> '
        '검색/지도 API 용 "네이버 개발자센터"가 아닙니다. '
        '<span class="guide-domain">apicenter.commerce.naver.com</span> '
        "(네이버 커머스API 센터) 입니다."
        "</div>"
        "</div></details>"
    )

    parts.append("</div>")  # form-section (A)

    # 안내문 — 부분 저장이 가능함을 화면에 알린다(계약 5). 고시·AS 를 여기서 안
    # 받는 이유를 한 줄로 안내한다. 절차 안내 수준이며 규제 약속 문구는 창작하지 않는다.
    parts.append('<div class="form-note">')
    parts.append(
        "<strong>저장:</strong> [저장]을 누르면 로컬 폼 서버가 설정 파일에 기록합니다. "
        "쓰기 전 기존 파일을 자동으로 백업합니다. "
        "<strong>다 채우지 않아도 저장할 수 있습니다</strong> — 빈 칸은 기존 값을 지우지 않습니다. "
        "<strong>비밀값(client_secret)은 결과 페이지에 다시 표시되지 않습니다.</strong>"
    )
    parts.append(
        " 상품정보제공고시 항목과 A/S 정보는 이 폼에 없습니다 — "
        "상품 등록 단계에서 해당 상품에 맞는 항목만 그때 요청합니다."
    )
    parts.append("</div>")

    # 저장 버튼. 순수 type="submit" — JS 없이 폼이 전송된다.
    parts.append('<div class="submit-bar">')
    parts.append('<button type="submit" class="submit-btn">저장</button>')
    parts.append("</div>")

    parts.append("</form>")

    parts.append("</div>")  # form-wrap
    parts.append("</body>")
    parts.append("</html>")
    return "\n".join(parts)


def write_config_form_html(
    html_path: str | Path,
    *,
    token: str,
    port: int,
    config_set_status: dict[str, bool] | None = None,
) -> Path:
    """설정 폼 HTML 을 디스크에 쓰고 경로를 반환한다.

    ``check_config`` 가 설정이 비어 있을 때 이 함수로 폼 파일을 생성하고 경로를 반환한다.
    """
    path = Path(html_path)
    doc = render_config_form_html(token=token, port=port, config_set_status=config_set_status)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc, encoding="utf-8")
    return path


__all__ = [
    "ConfigFormServer",
    "Outcome",
    "actual_bound_host",
    "allowed_field_names",
    "render_config_form_html",
    "write_config_form_html",
    "write_config_values",
    "TTL_SECONDS",
]
