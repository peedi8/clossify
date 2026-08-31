# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""로컬 승인 다리(localhost approval bridge).

본 모듈은 **로컬 포트 하나**를 열어 ``file://`` 미리보기 HTML 의 [승인] 버튼이
실제로 동작하게 만드는 얇은 다리다. 포트를 여는 것 자체가 위험하므로, 본
모듈의 *전부* 가 방어다. 버튼이 동작하게 하는 것은 쉬운 부분이고, 포트를 안전하게
지키는 것이 어려운 부분이다.

위협 모델 (설계의 중심)
-----------------------
로컬 포트를 열면 **같은 컴퓨터의 아무 웹페이지나** 그 포트를 찌를 수 있다.
사용자가 다른 탭에서 악성 사이트를 열어두면 그 사이트의 스크립트가 우리
승인 엔드포인트를 호출해 **판매자 스토어에 상품을 올릴 수 있다.**

이것이 왜 심각한지: 이 도구는 네이버 커머스 API 로 상품을 *실제로* 등록한다.
승인 엔드포인트가 뚫리면 악성 페이지가 사용자 모르게 상품을 올릴 수 있다.

필수 방어 10가지 (하나라도 못 지키면 포트를 열지 않는다)
-------------------------------------------------------
1. **바인딩**: ``127.0.0.1`` 에만 바인드. ``0.0.0.0`` 금지.
2. **일회용 토큰**: 승인 대기마다 새로 생성 (``secrets.token_urlsafe``,
   32바이트 이상). 토큰은 미리보기 파일 URL 에 담기고, 요청 시 **헤더 또는
   본문** 으로 제시해야 한다. 토큰 비교는 ``secrets.compare_digest``
   (타이밍 공격 방지).
3. **1회 소진**: 승인 1건 성공 시 토큰 즉시 폐기. 재사용 시도는 거부.
4. **수명 제한**: 대기 시작 후 **10분** 경과 시 자동 만료·서버 종료.
5. **Origin/Referer 검사**: 브라우저가 보낸 ``Origin``/``Referer`` 가 있으면
   ``null`` 또는 ``file://`` 만 허용. 다른 사이트에서 온 요청은 거부.
6. **CORS 금지**: ``Access-Control-Allow-Origin`` 헤더를 **절대 내보내지
   않는다**. 내보내면 악성 사이트가 응답을 읽을 수 있다.
7. **범위 제한**: 이 서버가 처리하는 것은 **해당 product_key 1건의 승인·수정
   반영** 뿐. 다른 상품·다른 동작 불가.
8. **기본 OFF**: ``enable_local_approval`` (기본 ``false``). 켜야 동작한다.
9. **수명주기**: 요청 처리 후 또는 만료 시 **반드시 서버 종료**. 좀비 포트 금지.
10. **로그에 토큰 금지.**

구현 메모
---------
- 표준 라이브러리만 쓴다(``http.server``/``socketserver``). 새 의존성 없음.
- 네이버 API 호출은 본 모듈이 하지 않는다 — 승인 *신호* 를 받아 ``Outcome``
  로 돌려줄 뿐, 실제 등록은 호출자(``mcp_server.register_product``)가 한다.
  이렇게 하면 승인 서버가 네이버 자격증명을 모르고, 호출자의 기존 게이트
  (컴플라이언스·QA) 가 그대로 통과하는 경로를 유지한다.

의존 방향: ``naver_client.config_path`` (설정 경로만) → ``approval_server``.
본 모듈은 ``mcp_server`` 에 의해 호출된다.
"""

from __future__ import annotations

import html
import http.client
import http.server
import json
import secrets
import socketserver
import threading
import time
import urllib.parse
from typing import Any

# ---------------------------------------------------------------------------
# 설정값(상수). 10분 수명 — 티켓 요구사항.
# ---------------------------------------------------------------------------
TTL_SECONDS = 10 * 60  # 10분. 만료 후 서버는 종료된다.
_TOKEN_NBYTES = 43  # secrets.token_urlsafe(43) -> 약 57자. 32바이트(256비트) 초과.

# POST 본문 최대 크기. 승인 페이로드는 작다(수정 필드 몇 개). 대용량 본문으로
# 인한 메모리 압밍/지연 차단.
_MAX_BODY_BYTES = 64 * 1024


# ---------------------------------------------------------------------------
# 결과 타입. 서버가 처리한 승인 결과를 호출자에게 돌려주는 형태.
# ---------------------------------------------------------------------------
class Outcome:
    """승인 대기 결과. 성공이면 decisions 가 있고, 실패면 reason 가 있다."""

    def __init__(
        self,
        *,
        approved: bool,
        reason: str = "",
        decisions: dict[str, Any] | None = None,
    ) -> None:
        self.approved = bool(approved)
        self.reason = str(reason)
        # decisions: {"edits": {field: value, ...}} 형태. [수정 후 승인] 시 채워짐.
        self.decisions = dict(decisions) if isinstance(decisions, dict) else {}


# ---------------------------------------------------------------------------
# 토큰 비교 — 항상 secrets.compare_digest.
#
# ``==`` 는 첫 번째로 다른 바이트에서 반환되므로 타이밍이 토큰의 접두사를
# 누출한다. ``compare_digest`` 는 길이가 같으면 일정 시간에 비교한다(길이가
# 다르면 일찍 반환하되, 그것은 토큰 누출이 아니다 — 존재 여부는 어차피
# 응답 차이로 드러난다).
# ---------------------------------------------------------------------------
def tokens_match(expected: str, presented: str) -> bool:
    """두 토큰이 같은지 일정 시간에 비교한다.

    ``str`` 을 UTF-8 바이트로 인코딩한 뒤 ``compare_digest`` 에 넘긴다.
    둘 중 하나라도 빈 문자열이면 ``False`` (조용한 통과 금지).
    """
    if not expected or not presented:
        return False
    a = expected.encode("utf-8")
    b = presented.encode("utf-8")
    return secrets.compare_digest(a, b)


def new_token() -> str:
    """일회용 승인 토큰을 생성한다 (32바이트 이상의 엔트로피)."""
    return secrets.token_urlsafe(_TOKEN_NBYTES)


# ---------------------------------------------------------------------------
# Origin/Referer 검사.
#
# 브라우저가 보낸 Origin/Referer 가 있으면 ``null`` 또는 ``file://`` 만 허용한다.
# ``file://`` 로 연 미리보기 페이지의 fetch 는 Origin: null 을 보내거나
# (Chromium 계열), Origin 헤더를 아예 보내지 않거나(Firefox), ``file://``
# Referer 를 보낸다. ``https://evil.example`` 같은 Origin 이 오면 거부한다.
#
# 중요: 헤더가 *없는* 경우는 허용한다 — 브라우저가 fetch 의 Origin 을 생략하는
# 정당한 경우가 있고, 본 서버의 1차 방어는 토큰이지 Origin 이다. Origin 검사는
# 토큰이 유출되더라도 다른 사이트에서 직접 호출을 막는 추가 층이다.
# ---------------------------------------------------------------------------
def _origin_ok(header_value: str) -> bool:
    """단일 Origin/Referer 헤더값이 허용되는지."""
    v = (header_value or "").strip()
    if v == "":
        return True  # 헤더 없음 — 허용(1차 방어는 토큰).
    if v.lower() == "null":
        return True  # file:// 페이지의 fetch 가 보내는 Origin.
    if v.lower().startswith("file://"):
        return True  # file:// Referer.
    return False  # 그 외(https://, http://) — 다른 사이트에서 온 요청.


def origin_referer_ok(headers: http.client.HTTPMessage) -> bool:
    """요청의 Origin/Referer 가 모두 허용되는지.

    **과거**: ``headers.get("Origin")`` 을 써서 **첫 번째 값만** 검사했다.
    중복 Origin 헤더(``Origin: null`` + ``Origin: https://evil``)가 오면 첫 번째
    값만 보고 통과시켰다. 본 함수는 ``get_all`` 로 **모든 값**을 검사한다 —
    하나라도 허용 목록 밖이면 거부. ``Referer`` 도 같은 방식.
    """
    # get_all 이 지원되지 않는 환경을 대비해 안전하게 폴백. http.client.HTTPMessage
    # (email.message.Message 서브클래스) 는 Python 3.x 전체에서 get_all 을 지원한다.
    get_all = getattr(headers, "get_all", None)
    if callable(get_all):
        origins = [v for v in get_all("Origin") or [] if v is not None]
        referers = [v for v in get_all("Referer") or [] if v is not None]
    else:  # 폴백 — get 이 첫 값만 반환하지만 거라도 검사한다.
        origins = [headers.get("Origin")] if headers.get("Origin") is not None else []
        referers = [headers.get("Referer")] if headers.get("Referer") is not None else []
    for v in origins:
        if not _origin_ok(v):
            return False
    for v in referers:
        if not _origin_ok(v):
            return False
    return True


# ---------------------------------------------------------------------------
# 폼 본문 파싱 헬퍼.
#
# 브라우저 폼 POST 의 ``application/x-www-form-urlencoded`` 본문을 핸들러가
# 다루기 쉬운 ``dict`` 로 정규화한다. 토큰과 product_key 는 단일 값이고,
# 수정 필드는 ``edits[<field>]`` 키 컨벤션으로 여러 개가 올 수 있다.
#
# 설계:
#   - ``token`` / ``product_key`` 는 첫 번째 값.
#   - ``edits[<field>]`` 폼 키는 ``body["edits"][<field>]`` 로 펼친다.
#     같은 field 가 여러 번 오면 마지막 값(폼의 일반적 동작).
#   - ``edits`` 자체에는 ``dict`` 만 넣는다(핸들러가 ``isinstance(dict)`` 로 검사).
# ---------------------------------------------------------------------------
_FORM_EDITS_PREFIX = "edits["


def _form_pairs_to_body(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    """``parse_qsl`` 결과 리스트를 핸들러 본문 dict 로 정규화.

    - ``token=...``        → ``body["token"]``
    - ``product_key=...``  → ``body["product_key"]``
    - ``edits[a]=1``       → ``body["edits"]["a"] = "1"``
    - 그 외 키(예: 숨겨진 보조 필드)는 무시하지 않고 body 최상위로 올린다
      (악의적 임의 키가 들어와도 토큰/product_key/edits 외에는 승인 본문에
      영향을 주지 않으므로 안전).
    """
    body: dict[str, Any] = {}
    edits: dict[str, str] = {}
    seen_multi: set[str] = set()
    for key, value in pairs:
        k = str(key or "")
        if not k:
            continue
        if k.startswith(_FORM_EDITS_PREFIX) and k.endswith("]"):
            field = k[len(_FORM_EDITS_PREFIX) : -1]
            if field:
                edits[field] = value
                continue
        if k in ("token", "product_key"):
            # 단일 값 필드. 첫 번째를 쓴다(폼의 일반적 규약).
            if k not in seen_multi:
                body[k] = value
                seen_multi.add(k)
            continue
        # 그 외 키도 body 에 올리되, 중복 시 마지막 값.
        body[k] = value
    if edits:
        body["edits"] = edits
    return body


def _approval_result_page(*, ok: bool, status_text: str, detail: str) -> str:
    """승인 처리 결과를 사람이 읽을 수 있는 HTML 페이지로 조립.

    ``ok=True`` 면 접수 성공, ``ok=False`` 면 거부/오류. detail 은 이미
    ``html.escape`` 된 문자열이어야 한다(호출자 책임). 외부 CSS/JS/폰트 참조
    없는 인라인 HTML 이다(미리보기 페이지와 같은 규율).
    """
    title = "승인 접수 완료" if ok else "승인 거부"
    banner_cls = "ok" if ok else "err"
    return (
        "<!DOCTYPE html>\n"
        '<html lang="ko"><head><meta charset="utf-8" />'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />'
        f"<title>{html.escape(title)}</title>"
        "<style>"
        "body{margin:0;padding:32px;background:#f5f5f5;"
        'font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,'
        '"Helvetica Neue",Arial,sans-serif;color:#222}'
        ".wrap{max-width:640px;margin:0 auto;background:#fff;"
        "padding:32px;border-radius:8px;"
        "box-shadow:0 1px 4px rgba(0,0,0,0.08)}"
        ".banner{padding:16px 18px;border-radius:8px;font-size:18px;"
        "font-weight:700;margin-bottom:16px}"
        ".banner.ok{background:#e6f4ea;color:#137333;border:2px solid #137333}"
        ".banner.err{background:#fce8e6;color:#a50e0e;border:2px solid #a50e0e}"
        ".detail{color:#444;line-height:1.6;font-size:14px;word-break:break-word}"
        ".note{margin-top:20px;padding:12px;background:#eef6ff;border-radius:6px;"
        "color:#1a4d8f;font-size:12px;line-height:1.6}"
        "</style></head><body>"
        '<div class="wrap">'
        f'<div class="banner {banner_cls}">{html.escape(status_text)}</div>'
        f'<div class="detail">{detail}</div>'
        '<div class="note">이 페이지는 로컬 승인 다리의 처리 결과입니다. '
        "승인 결과에 따라 채팅에서 최종 등록 결과를 확인하세요.</div>"
        "</div></body></html>"
    )


# ---------------------------------------------------------------------------
# 핵심: ApprovalHandler. http.server.BaseHTTPRequestHandler 서브클래스.
#
# 설계상 결정:
#   - 핸들러는 인스턴스별로 만들어지므로, 서버 상태(토큰·만료·결과)는
#     ``server`` 속성(아래 ApprovalServer 인스턴스) 에서 읽는다.
#   - POST / 만 지원한다(범위 제한 — 다른 경로/메서드는 404/405).
#   - 처리 후 ``server.consume()`` 으로 결과를 기록하고 서버를 종료한다.
#   - **절대** ``Access-Control-Allow-Origin`` 헤더를 내보내지 않는다.
# ---------------------------------------------------------------------------
class _ApprovalHandler(http.server.BaseHTTPRequestHandler):
    """단일 product_key 승인을 받는 HTTP 핸들러.

    두 가지 본문 형식을 받는다:

    - ``application/json`` (레거시/소켓 테스트 경로). JSON 응답. 기존 동작 유지.
    - ``application/x-www-form-urlencoded`` (브라우저 폼 POST 경로). **사람이 읽을
      HTML 결과 페이지**로 응답. 이 경로가 실제 브라우저에서 [승인] 버튼이
      작동하게 만드는 경로다 — 폼 POST 는 CORS 프리플라이트를 유발하지 않으므로.

    **CORS 헤더는 어느 경로에서도 절대 내보내지 않는다** (방어 6). 로그에 토큰이
    찍히지 않도록 ``log_message`` 를 덮어쓴다 (방어 10).
    """

    # ``server_version``/``sys_version`` 노출을 최소화 — 서버 지문을 줄인다.
    server_version = "clossify-approval"
    sys_version = ""

    # ------------------------------------------------------------------ #
    # 라우팅. POST / 만 허용. 다른 경로·메서드는 거부 (범위 제한, 방어 7).
    # ------------------------------------------------------------------ #
    def do_POST(self) -> None:  # http.server API 대문자 규약.
        # 경로가 정확히 "/" 또는 "/approve" 인 경우만 처리. 쿼리스트링은 허용
        # (토큰이 URL 에 있을 수 있으나, 본 검증은 헤더/본문으로 한다).
        path = self.path.split("?", 1)[0]
        if path not in ("/", "/approve"):
            self._reject(404, "not_found", "알 수 없는 경로입니다.")
            return
        self._handle_approval()

    def do_GET(self) -> None:
        # GET 은 허용하지 않는다. 승인은 부작용(등록)을 일으키므로 POST 만.
        self._reject(405, "method_not_allowed", "GET 은 지원하지 않습니다.")

    def do_OPTIONS(self) -> None:
        # CORS preflight (OPTIONS) 에도 절대 Access-Control-Allow-Origin 을
        # 내보내지 않는다. preflight 를 처리해주면 악성 페이지가 실제 요청을
        # 보내기 쉬워지지만, 허용 헤더를 주면 응답을 읽을 수 있게 된다.
        self._reject(
            405,
            "method_not_allowed",
            "CORS preflight 는 지원하지 않습니다.",
        )

    # ------------------------------------------------------------------ #
    # 승인 처리 본체.
    # ------------------------------------------------------------------ #
    def _handle_approval(self) -> None:
        # self.server 는 _ThreadingHTTPServer 인스턴스다. ApprovalServer 는
        # 그 ``approval_state`` 속성으로 참조된다 (start() 에서 설정).
        srv = self.server.approval_state  # type: ignore[attr-defined]
        if srv is None:  # 설정 누락 — 서버 상태를 알 수 없으면 거부.
            self._reject(500, "no_state", "서버 상태를 사용할 수 없습니다.")
            return

        # 1. 만료 검사 (방어 4). 만료된 서버는 더 이상 승인을 받지 않는다.
        if srv.is_expired():
            self._respond_pre_body(410, "expired", "승인 대기 시간이 만료되었습니다.")
            srv.shutdown_from_request()
            return

        # 2. Origin/Referer 검사 (방어 5).
        if not origin_referer_ok(self.headers):
            self._respond_pre_body(403, "bad_origin", "허용되지 않은 Origin/Referer 입니다.")
            return

        # 3. 본문 읽기 (크기 제한).
        length = self._content_length()
        if length is None:
            self._reject(400, "bad_request", "본문이 필요합니다.")
            return
        if length > _MAX_BODY_BYTES:
            self._reject(413, "too_large", "요청 본문이 너무 큽니다.")
            return
        raw = self.rfile.read(length)

        # 4. Content-Type 으로 본문 파싱 분기.
        #    - application/json                          -> 기존 JSON 경로.
        #    - application/x-www-form-urlencoded         -> 폼 POST (브라우저 경로).
        #    - 그 외                                      -> 기존처럼 거부.
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        wants_html = False
        body: dict[str, Any]
        if ctype == "application/json":
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                self._reject(400, "bad_json", "JSON 본문이 필요합니다.")
                return
            if not isinstance(parsed, dict):
                self._reject(400, "bad_json", "본문은 JSON 객체여야 합니다.")
                return
            body = parsed
        elif ctype == "application/x-www-form-urlencoded":
            # 폼 POST 경로. 브라우저 폼 본문을 dict 로 파싱하고 HTML 로 응답한다.
            # 이 경로는 CORS preflight 를 유발하지 않아 실제 브라우저에서 작동한다.
            wants_html = True
            try:
                form_pairs = urllib.parse.parse_qsl(
                    raw.decode("utf-8"),
                    keep_blank_values=True,
                    strict_parsing=False,
                )
            except (UnicodeDecodeError, ValueError):
                self._respond_error(400, "bad_form", "폼 본문이 올바르지 않습니다.")
                return
            body = _form_pairs_to_body(form_pairs)
        else:
            # 알 수 없는 Content-Type. 기존 호환을 위해 본문이 비어있으면 JSON 폼백
            # 을 시도하지 않고 거부한다 (방어 — 모호한 입력 허용 금지).
            self._reject(415, "unsupported_media_type", "지원하지 않는 Content-Type 입니다.")
            return

        # 5. 토큰 검증 (방어 2, 3).
        #    토큰은 헤더 또는 본문 어느 쪽이든 제시될 수 있다.
        presented = self._extract_token(body)
        if not presented:
            self._respond_error_tpl(wants_html, 401, "no_token", "승인 토큰이 필요합니다.")
            return
        # 토큰이 이미 소진되었는지 먼저 검사 (방어 3: 1회 소진). 이 검사가
        # 토큰 비교보다 먼저여야, 같은 토큰으로 재시도하는 경로를 막는다.
        if srv.is_consumed():
            self._respond_error_tpl(wants_html, 410, "already_used", "이미 사용된 토큰입니다.")
            return
        # secrets.compare_digest 로 일정 시간 비교 (방어 2).
        if not tokens_match(srv.token, presented):
            self._respond_error_tpl(wants_html, 403, "bad_token", "승인 토큰이 일치하지 않습니다.")
            return

        # 6. product_key 일치 검사 (방어 7: 범위 제한). 본문의 product_key 는
        #    **필수**며 서버가 대기 중인 product_key 와 정확히 같아야 한다.
        body_pkey = str(body.get("product_key") or "").strip()
        if not body_pkey:
            self._respond_error_tpl(
                wants_html, 400, "missing_product_key", "product_key 는 필수입니다."
            )
            return
        if body_pkey != srv.product_key:
            self._respond_error_tpl(
                wants_html,
                403,
                "wrong_product",
                "다른 상품의 승인은 처리할 수 없습니다.",
            )
            return

        # 7. 승인 확정. 결과를 서버에 기록하고 토큰을 폐기한다 (방어 3).
        edits = body.get("edits")
        srv.consume(
            Outcome(
                approved=True,
                decisions={"edits": dict(edits)} if isinstance(edits, dict) else {},
            )
        )

        # 8. 응답. CORS 헤더 절대 없음 (방어 6).
        if wants_html:
            self._respond_html_ok(srv.product_key)
        else:
            self._respond(200, {"ok": True, "approved": True})

        # 9. 서버 종료 예약 (방어 9). 처리 후 좀비 포트 금지.
        srv.shutdown_from_request()

    # ------------------------------------------------------------------ #
    # 응답 헬퍼들. 모두 ``Access-Control-Allow-Origin`` 을 내보내지 않는다.
    # ------------------------------------------------------------------ #
    def _respond(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 의도적으로 Access-Control-Allow-Origin 은 보내지 않는다 (방어 6).
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _reject(self, status: int, code: str, detail: str) -> None:
        self._respond(status, {"ok": False, "approved": False, "code": code, "detail": detail})

    # ------------------------------------------------------------------ #
    # 폼 POST 경로용 응답 헬퍼.
    #
    # 폼 전송은 브라우저를 결과 페이지로 **이동**시킨다. 따라서 응답은 사람이
    # 읽을 수 있는 HTML 이어야 하며, 승인 접수·거부 사유 등 **실제 상태를 그대로**
    # 보여준다. 이 구조 자체가 "아무것도 안 갔는데 갔다고 말하는" 거짓 성공을
    # 불가능하게 만든다 — 사용자는 서버가 반환한 사실을 직접 본다.
    #
    # CORS 헤더는 이 경로에서도 절대 내보내지 않는다 (방어 6).
    # ------------------------------------------------------------------ #
    def _is_form_request(self) -> bool:
        """요청의 Content-Type 이 폼 인코딩인지 (브라우저 폼 POST 경로 판별)."""
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        return ctype == "application/x-www-form-urlencoded"

    def _respond_pre_body(self, status: int, code: str, detail: str) -> None:
        """본문을 파싱하기 전의 거부(만료·Origin 등).

        Content-Type 으로 어느 경로인지 판별할 수 있으므로, 폼 경로면 HTML,
        JSON/그 외 경로면 JSON 응답을 보낸다. 기존 JSON 호출자의 회귀를 막는다.
        """
        if self._is_form_request():
            self._respond_html_error(status, code, detail)
        else:
            self._reject(status, code, detail)

    def _respond_error_tpl(self, wants_html: bool, status: int, code: str, detail: str) -> None:
        """본문을 파싱한 뒤의 거부. 폼 경로면 HTML, JSON 경로면 JSON."""
        if wants_html:
            self._respond_html_error(status, code, detail)
        else:
            self._reject(status, code, detail)

    def _respond_html_ok(self, product_key: str) -> None:
        """승인 접수 성공 HTML 페이지. 사용자가 직접 눈으로 확인한다."""
        page = _approval_result_page(
            ok=True,
            status_text="승인이 접수되었습니다.",
            detail=f"product_key: {html.escape(product_key)} 의 승인이 처리되었습니다. "
            "등록 결과는 채팅에서 확인하세요.",
        )
        self._write_html(200, page)

    def _respond_html_error(self, status: int, code: str, detail: str) -> None:
        """거부/오류 HTML 페이지. 사유를 사람이 읽을 수 있게 표시한다."""
        page = _approval_result_page(
            ok=False,
            status_text=f"거부됨 (HTTP {status}, {html.escape(code)})",
            detail=html.escape(detail),
        )
        self._write_html(status, page)

    def _write_html(self, status: int, page: str) -> None:
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
    # 토큰 추출. 헤더 우선, 없으면 본문.
    # ------------------------------------------------------------------ #
    def _extract_token(self, body: dict[str, Any]) -> str:
        # 헤더: X-Approval-Token (권장), Authorization: Bearer <token> (편의).
        h = self.headers.get("X-Approval-Token")
        if h and h.strip():
            return h.strip()
        auth = self.headers.get("Authorization")
        if auth and auth.strip().lower().startswith("bearer "):
            cand = auth.strip()[7:].strip()
            if cand:
                return cand
        # 본문: {"token": "..."}
        body_tok = body.get("token")
        if isinstance(body_tok, str) and body_tok.strip():
            return body_tok.strip()
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
    # 로그 — 토큰이 찍히지 않도록 기본 출력은 순간다 (방어 10).
    # http.server.BaseHTTPRequestHandler.log_message 는 요청 줄을 찍는데,
    # URL 에 토큰이 있을 수 있으므로 출력을 무음화한다.
    # ------------------------------------------------------------------ #
    def log_message(self, format: str, *args: Any) -> None:
        # 의도적으로 아무것도 출력하지 않는다 — 토큰이 로그에 노출되지 않게.
        return


# ---------------------------------------------------------------------------
# ApprovalServer — 단일 product_key 승인을 대기하는 TCP/HTTP 서버.
#
# ``socketserver.TCPServer`` 대신 ``http.server.HTTPServer`` 의 서브클래스를
# 쓴다. ``HTTPServer`` 는 내부적으로 ``socketserver.TCPServer`` 이다.
#
# 바인딩: ``127.0.0.1`` 에만 바인드 (방어 1). ``0.0.0.0`` 을 쓰면 다른 컴퓨터가
# 접근할 수 있어 절대 금지.
# ---------------------------------------------------------------------------
class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """요청 처리 중 shutdown 이 교착하지 않도록 스레드 기반으로.

    ``approval_state`` 속성은 ``ApprovalServer`` 인스턴스를 참조한다 —
    핸들러는 ``self.server.approval_state`` 로 접근한다(BaseHTTPRequestHandler
    의 ``self.server`` 는 HTTPServer 인스턴스이기 때문).
    """

    daemon_threads = True
    allow_reuse_address = True
    approval_state: ApprovalServer | None = None


class ApprovalServer:
    """단일 product_key 의 승인을 대기하는 로컬 서버.

    사용 흐름:
        srv = ApprovalServer(product_key=..., token=..., on_outcome=cb)
        port = srv.start()        # 127.0.0.1:<port> 바인드. 포트 확정.
        srv.wait(timeout=...)     # 승인/만료/거부 시까지 대기.
        outcome = srv.outcome     # 결과.

    본 객체는 한 번만 쓴다 — 같은 객체로 두 번 ``start`` 하지 않는다.
    """

    def __init__(
        self,
        *,
        product_key: str,
        token: str,
        ttl_seconds: int = TTL_SECONDS,
        bind_host: str = "127.0.0.1",
    ) -> None:
        if not product_key:
            raise ValueError("product_key 가 필요합니다.")
        if not token:
            raise ValueError("token 이 필요합니다.")
        if bind_host not in ("127.0.0.1", "localhost"):
            # 방어 1: 바인딩 호스트가 127.0.0.1 이 아니면 거부. 생성 단계에서
            # 막지 않으면 의도치 않게 외부 인터페이스가 노출된다.
            raise ValueError(f"bind_host 는 127.0.0.1 이어야 합니다 (got {bind_host!r}).")
        self.product_key = str(product_key)
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
        반환 후에는 미리보기 파일이 그 포트를 가리키도록 갱신할 수 있다.
        """
        if self._http is not None:
            raise RuntimeError("ApprovalServer 는 한 번만 시작할 수 있습니다.")
        # port=0 으로 OS 자동 할당. host="127.0.0.1" 강제 (방어 1).
        httpd = _ThreadingHTTPServer(("127.0.0.1", 0), _ApprovalHandler)
        # 핸들러가 ApprovalServer 상태(토큰·만료·결과) 에 접근할 수 있도록
        # 참조를 걸어둔다. 핸들러의 self.server 는 _ThreadingHTTPServer 이므로,
        # ApprovalServer 인스턴스는 approval_state 속성으로 노출한다.
        httpd.approval_state = self
        self._http = httpd
        self._port = httpd.server_address[1]
        # 백그라운드 serve.
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        self._thread = t
        return self._port

    def wait(self, timeout: float | None = None) -> Outcome:
        """승인/만료/거부 결과가 확정될 때까지 대기하고 ``Outcome`` 을 반환.

        timeout 이 None 이면 TTL 만큼 대기한다. 결과가 확정되면 서버를 종료한다
        (방어 9). 만료 시에도 서버를 종료하고 ``Outcome(approved=False)`` 반환.
        """
        if self._http is None:
            raise RuntimeError("start() 를 먼저 호출해야 합니다.")
        deadline = self._born_at + (self.ttl_seconds if timeout is None else float(timeout))

        # 결과가 들어오거나 만료될 때까지 잠자며 폴링. ThreadingMixIn 이 요청
        # 처리를 다른 스레드에서 하므로 이 폴링이 serve 를 막지 않는다.
        while True:
            with self._lock:
                if self._outcome is not None:
                    outcome = self._outcome
                    break
            if time.monotonic() >= deadline:
                outcome = Outcome(approved=False, reason="timeout")
                break
            time.sleep(0.05)

        # 결과 확정(성공/거부/만료) 후 서버를 반드시 종료한다 (방어 9).
        self._shutdown()
        return outcome

    def consume(self, outcome: Outcome) -> None:
        """승인 결과를 기록하고 토큰을 폐기한다 (방어 3: 1회 소진)."""
        with self._lock:
            if self._consumed:
                # 이미 소진됨 — 이 경로는 호출자가 이미 결과를 가지고 있다는
                # 뜻이므로 무시한다. 핸들러는 is_consumed() 로 미리 막는다.
                return
            self._consumed = True
            self._outcome = outcome

    def shutdown_from_request(self) -> None:
        """요청 처리 스레드가 결과를 확정한 뒤 serve 루프를 끝내도록 요청."""
        # serve_forever 가 다른 스레드에서 돌고 있으므로 shutdown 은 별도
        # 스레드에서 부른다 — 동일 스레드에서 부르면 교착한다.
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
        """명시적 종료. ``wait`` 을 호출하지 않은 경로(예: 포트만 열어본 경우)
        를 위한 안전망. 좀비 포트가 남지 않게 한다 (방어 9)."""
        self._shutdown()


# ---------------------------------------------------------------------------
# 외부 진입 헬퍼. mcp_server.register_product 가 "승인 대기 모드" 로 호출된다.
# ---------------------------------------------------------------------------
def await_approval(
    *,
    product_key: str,
    token: str,
    ttl_seconds: int = TTL_SECONDS,
) -> tuple[ApprovalServer, Outcome]:
    """승인 서버를 띄워 대기하고 ``(server, outcome)`` 을 반환.

    본 함수는 포트를 실제로 열고 *실제 소켓* 으로 승인을 기다린다. 결과를
    받으면 서버를 종료한다(좀비 없음). 호출자는 ``outcome.approved`` 로
    승인 여부를, ``outcome.decisions`` 로 수정 필드를 얻는다.
    """
    srv = ApprovalServer(product_key=product_key, token=token, ttl_seconds=ttl_seconds)
    srv.start()
    outcome = srv.wait()
    return srv, outcome


# ---------------------------------------------------------------------------
# 바인딩 검증 헬퍼 (테스트가 "실제 127.0.0.1 바인드" 를 증명하기 위해 쓴다).
# ---------------------------------------------------------------------------
def actual_bound_host(server: ApprovalServer) -> str:
    """서버가 실제로 바인드한 호스트를 반환한다 (방어 1 검증용).

    테스트가 이 값을 읽어 ``0.0.0.0`` 이 아님을 증명한다.
    """
    httpd = server._http  # 테스트 검증용 내부 접근.
    if httpd is None:
        return ""
    return str(httpd.server_address[0])


__all__ = [
    "ApprovalServer",
    "Outcome",
    "actual_bound_host",
    "await_approval",
    "new_token",
    "tokens_match",
    "TTL_SECONDS",
]
