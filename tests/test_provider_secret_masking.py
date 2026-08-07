# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""제공자 오류 응답 비밀값 마스킹 검증.

하드 리뷰 과업 (a)-(c):
  (a) 제공자가 키를 오류 본문에 실어 돌려줘도 **반환/로그에 키가 없다.**
      canary 문자열로 단언.
  (b) 오류 사유·HTTP 상태는 여전히 보인다 (조용한 실패 아님).
  (c) 설정에 없는(오타낸) 키 형태도 패턴으로 가린다.

과거 결함: ``image_gen`` 의 ``_call_openai``/``_call_gemini`` 이 제공자 응답
본문을 ``text[:200]`` 그대로 오류 사유에 실었다. OpenAI 는 **잘못된 API 키
자체를 오류 메시지에 담아 돌려주는** 사례가 있어, 그 본문이 반환값·로그에
흐르면 키 유출 경로가 된다.

단일 진실 공급원: ``common.sanitize_text`` / ``common.sanitize_provider_response``.
``mcp_server._sanitize_text`` 는 호환 별칭으로 같은 규칙을 쓴다 — 규칙이 두
벌로 갈라지지 않는다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import common, image_gen, mcp_server


# --------------------------------------------------------------------------- #
# Fake HTTP 응답 — 제공자가 키를 오류 본문에 실어 돌려주는 사례를 흉내.
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text

    def json(self):
        return json.loads(self.text)


class _RecordingSession:
    """``requests.Session`` 의 최소 fake — ``post`` 만 지원."""

    def __init__(self, response: _FakeResponse):
        self._response = response
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self._response

    def close(self):
        pass


# OpenAI 가 **잘못된 키 자체를 오류 본문에 담아** 돌려주는 사례.
# 본문에 canary 접두사 ``sk-LEAK-CANARY`` 를 넣어 단언에 쓴다.
_OPENAI_LEAK_BODY = (
    '{"error": {"message": "Incorrect API key provided: sk-LEAK-CANARY-aB1cD2eF3gH4iJ5kL6mN7oP8qR. '
    'You can find your API key at https://platform.openai.com.", "type": "invalid_request_error", '
    '"code": "invalid_api_key"}}'
)

# Gemini 스타일 — API 키가 URL 에 노출된채 400 응답.
_GEMINI_LEAK_BODY = (
    '{"error": {"code": 400, "message": "API key not valid. '
    'Please pass a valid API key. key=AIzaLEAK-CANARY-aB1cD2eF3gH4iJ5kL6mN7", '
    '"status": "INVALID_ARGUMENT"}}'
)

# Bearer 토큰이 본문에 노출된 사례.
_BEARER_LEAK_BODY = "Unauthorized: Bearer LEAK-CANARY-aB1cD2eF3gH4iJ5kL6mN7oP8qR"

# 설정에 없는(오타낸) 키가 본문에 노출된 사례 — 패턴으로 가려야 한다.
_TYPO_KEY_BODY = (
    '{"error": {"message": "Invalid key sk-TYPOCANARY000000000000000000000000000AAA", '
    '"code": "invalid_api_key"}}'
)


def _valid_openai_config() -> dict:
    return {
        "image_providers": {
            "openai": {"api_key": "sk-test-key", "model": "gpt-image-2"},
        }
    }


def _valid_gemini_config() -> dict:
    return {
        "image_providers": {
            "gemini": {"api_key": "AIza-test-key", "model": "gemini-3.1-flash-image"},
        }
    }


# =========================================================================== #
# (a) 제공자가 오류 본문에 키를 실어 보내도 반환에 키가 없다 (canary 단언).
# =========================================================================== #
class TestProviderKeyNotInResult:
    """제공자 오류 응답에 키가 포함돼 있어도 generate() 반환에 키가 없다."""

    def test_openai_leaked_key_not_in_error(self):
        """OpenAI 응답이 잘못된 키를 본문에 담아도 error 에 canary 가 없다."""
        # 응답 본문에 canary 접두사가 있다.
        assert "sk-LEAK-CANARY" in _OPENAI_LEAK_BODY
        session = _RecordingSession(_FakeResponse(401, _OPENAI_LEAK_BODY))
        result = image_gen.generate(
            "프롬프트",
            needed_cuts=1,
            config=_valid_openai_config(),
            session=session,
        )
        assert result["ok"] is False
        # 반환 error 에 canary 가 없다 (마스킹됨).
        assert "LEAK-CANARY" not in str(result.get("error") or "")
        # 마스킹 표식이 있다.
        assert "[REDACTED]" in str(result.get("error") or "")

    def test_gemini_leaked_key_not_in_error(self):
        """Gemini 응답에 키 형태가 노출돼도 반환 error 에 canary 가 없다."""
        assert "AIzaLEAK-CANARY" in _GEMINI_LEAK_BODY
        session = _RecordingSession(_FakeResponse(400, _GEMINI_LEAK_BODY))
        result = image_gen.generate(
            "프롬프트",
            needed_cuts=1,
            config=_valid_gemini_config(),
            session=session,
        )
        assert result["ok"] is False
        assert "LEAK-CANARY" not in str(result.get("error") or "")

    def test_bearer_token_not_in_error(self):
        """Bearer 토큰이 본문에 있어도 마스킹된다."""
        assert "LEAK-CANARY" in _BEARER_LEAK_BODY
        session = _RecordingSession(_FakeResponse(401, _BEARER_LEAK_BODY))
        result = image_gen.generate(
            "프롬프트",
            needed_cuts=1,
            config=_valid_openai_config(),
            session=session,
        )
        assert result["ok"] is False
        assert "LEAK-CANARY" not in str(result.get("error") or "")

    def test_full_result_flat_no_canary(self):
        """결과 전체를 덤프해도 canary 가 없다 (로그 안전성)."""
        session = _RecordingSession(_FakeResponse(401, _OPENAI_LEAK_BODY))
        result = image_gen.generate(
            "프롬프트",
            needed_cuts=1,
            config=_valid_openai_config(),
            session=session,
        )
        flat = json.dumps(result, ensure_ascii=False, default=str)
        assert "LEAK-CANARY" not in flat


# =========================================================================== #
# (b) 오류 사유·HTTP 상태는 여전히 보인다 (조용한 실패 아님).
# =========================================================================== #
class TestErrorReasonStillVisible:
    """키 값만 가리고 사유(HTTP 상태·메시지 골격)는 남긴다."""

    def test_http_status_visible_in_error(self):
        """401 상태 코드가 error 에 보인다."""
        session = _RecordingSession(_FakeResponse(401, _OPENAI_LEAK_BODY))
        result = image_gen.generate(
            "프롬프트",
            needed_cuts=1,
            config=_valid_openai_config(),
            session=session,
        )
        assert result["ok"] is False
        err = str(result.get("error") or "")
        assert "401" in err, f"HTTP 상태가 error 에 없음 (조용한 실패): {err!r}"

    def test_error_reason_skeleton_visible(self):
        """에러 타입(invalid_request_error) 골격은 보인다."""
        session = _RecordingSession(_FakeResponse(401, _OPENAI_LEAK_BODY))
        result = image_gen.generate(
            "프롬프트",
            needed_cuts=1,
            config=_valid_openai_config(),
            session=session,
        )
        err = str(result.get("error") or "")
        # 사유 골격 — invalid_request_error 같은 타입명은 남는다.
        assert (
            "invalid_request_error" in err or "HTTP 401" in err
        ), f"사유 골격이 사라짐 (조용한 실패): {err!r}"

    def test_gemini_http_status_visible(self):
        session = _RecordingSession(_FakeResponse(400, _GEMINI_LEAK_BODY))
        result = image_gen.generate(
            "프롬프트",
            needed_cuts=1,
            config=_valid_gemini_config(),
            session=session,
        )
        assert result["ok"] is False
        err = str(result.get("error") or "")
        assert "400" in err, f"Gemini HTTP 상태가 error 에 없음: {err!r}"


# =========================================================================== #
# (c) 설정에 없는(오타낸) 키 형태도 패턴으로 가린다.
# =========================================================================== #
class TestUnknownKeyFormatMasked:
    """설정 키 값이 아닌, "키 형태" 자체를 패턴으로 가린다.

    사용자가 방금 오타 낸 키(sk-TYPOCANARY...)도 제공자 오류 본문에 실려
    올 수 있으므로, 설정된 키 값과 비교하는 방식만으로는 부족하다.
    """

    def test_typo_key_pattern_masked(self):
        """설정에 없는(오타낸) ``sk-...`` 형태도 마스킹된다."""
        # 본문에 canary 가 있다.
        assert "sk-TYPOCANARY" in _TYPO_KEY_BODY
        # 설정된 키는 ``sk-test-key`` — 본문의 키와 다르다(오타 시나리오).
        session = _RecordingSession(_FakeResponse(401, _TYPO_KEY_BODY))
        result = image_gen.generate(
            "프롬프트",
            needed_cuts=1,
            config=_valid_openai_config(),
            session=session,
        )
        assert result["ok"] is False
        err = str(result.get("error") or "")
        # 오타 키가 error 에 없다 (패턴 매칭으로 가림).
        assert "TYPOCANARY" not in err

    def test_common_sanitize_masks_sk_pattern(self):
        """common.sanitize_text 가 ``sk-`` 패턴을 가린다."""
        text = "error: key sk-ABCD1234abcd5678 not valid"
        masked = common.sanitize_text(text)
        assert "sk-ABCD1234abcd5678" not in masked
        assert "[REDACTED]" in masked

    def test_common_sanitize_masks_bearer(self):
        masked = common.sanitize_text("Bearer ABCDEFGHIJKLMNOPQRSTUVWXYZ1234")
        assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ1234" not in masked

    def test_common_sanitize_masks_key_value_pair(self):
        text = 'api_key: "sk-ABCD1234abcd5678efgh"'
        masked = common.sanitize_text(text)
        assert "sk-ABCD1234abcd5678efgh" not in masked


# =========================================================================== #
# 단일 진실 공급원 — mcp_server 별칭이 common 과 같은 규칙을 쓴다.
# =========================================================================== #
class TestSanitizationSingleSourceOfTruth:
    """mcp_server._sanitize_text / _sanitize_error 가 common 규칙을 따른다."""

    def test_mcp_sanitize_uses_common_rules(self):
        text = "key sk-ABCD1234abcd5678 leaked"
        via_mcp = mcp_server._sanitize_text(text)
        via_common = common.sanitize_text(text)
        assert via_mcp == via_common
        assert "sk-ABCD1234abcd5678" not in via_mcp

    def test_mcp_sanitize_error_uses_common(self):
        try:
            raise ValueError("failed with key sk-ABCD1234abcd5678")
        except ValueError as exc:
            via_mcp = mcp_server._sanitize_error(exc)
            via_common = common.sanitize_error(exc)
        assert via_mcp == via_common
        assert "sk-ABCD1234abcd5678" not in via_mcp
        # 예외 타입명(ValueError)은 남는다.
        assert "ValueError" in via_mcp

    def test_sensitive_patterns_list_shared(self):
        """mcp_server.SENSITIVE_PATTERNS 가 common.SENSITIVE_PATTERNS 같다."""
        assert mcp_server._SENSITIVE_PATTERNS is common.SENSITIVE_PATTERNS

    def test_common_sanitize_provider_response_is_alias(self):
        text = "invalid key sk-ABCD1234abcd5678"
        assert common.sanitize_provider_response(text) == common.sanitize_text(text)


# =========================================================================== #
# 예외 메시지 경로 정화 (이중 역슬래시 repr 형태 포함).
#
# 과거 결함: ``sanitize_error``/``sanitize_text`` 는 단일 역슬래시 Windows
# 경로(`C:\Users\..`)만 매칭했다. 하지만 파이썬 예외 메시지는 경로를 repr
# 형태(역슬래시 이중, 예: 'C:\\Users\\..')로 담아 내보내므로, 실제로 가장
# 자주 만나는 형태를 정확히 놓치고 있었다 — 즉 "동작한다"는 착시.
# =========================================================================== #
class TestExceptionPathRedaction:
    """예외 메시지 안의 절대경로도 가린다 (사유는 남긴다).

    (a) 이중 역슬래시 Windows 경로가 가려진다.
    (b) 단일 역슬래시 Windows 경로가 여전히 가려진다 (회귀).
    (c) 실제 FileNotFoundError(존재하지 않는 절대경로 열기) 를 정화하면
        경로가 안 보이고 예외 타입은 보인다.
    (d) POSIX 절대경로(/home/..., /Users/...) 도 가려진다.
    (e) https:// URL 은 가려지지 않는다 (과탐 방지 — URL 은 진단에 필요).
    (f) 일반 한국어 문장이 훼손되지 않는다.
    """

    def test_a_double_backslash_windows_path_redacted(self):
        """(a) 이중 역슬래시 Windows 경로(repr 형태) 가 가려진다.

        주의: 파이썬 소스에서 ``"C:\\\\Users"`` 는 실제로 ``C:\\Users`` (역슬래시
        2개) 로 인코딩된다. 이것이 ``str(FileNotFoundError)`` 가 내보내는
        형태와 동일하다 — 결함 있던 단일-역슬래시 패턴은 이 형태를 놓쳤다.
        ``"C:\\Users"`` (역슬래시 1개) 로 테스트하면 결함을 잡아내지 못한다.
        """
        # 실제 문자열에 역슬래시가 2개씩 들어간 형태 (repr 형태).
        text = "file at C:\\\\Users\\\\speedy\\\\secret.png"
        # 전제 검증: 실제로 역슬래시가 6개(구분자 3개 x 2) 있어야 한다.
        assert text.count("\\") == 6, f"테스트 픽스처가 잘못됨: {text!r}"
        masked = common.sanitize_text(text)
        assert "speedy" not in masked, f"사용자명이 새어나감: {masked!r}"
        assert "secret.png" not in masked
        assert "[REDACTED]" in masked

    def test_a_double_backslash_in_quoted_repr_form(self):
        """(a) repr 인용 형태('C:\\\\Users\\\\...')도 가려진다.

        실제 FileNotFoundError str() 은 인용부호로 경로를 감싸고 역슬래시를
        이중으로 인코딩한다. 이 형태가 가장 자주 노출되는 실전 형태다.
        """
        # str(e) 형태: 'C:\\\\Users\\\\alice\\\\no.png' (역슬래시 6개).
        text = "[Errno 2] No such file or directory: 'C:\\\\Users\\\\alice\\\\no.png'"
        assert text.count("\\") == 6
        masked = common.sanitize_text(text)
        assert "alice" not in masked
        assert "no.png" not in masked
        assert "[REDACTED]" in masked
        # 사유 골격은 남는다.
        assert "Errno 2" in masked or "No such file" in masked

    def test_b_single_backslash_windows_path_still_redacted(self):
        """(b) 단일 역슬래시 Windows 경로도 가려진다 (회귀 방지)."""
        text = "file at C:\\Users\\alice\\x.png"
        masked = common.sanitize_text(text)
        assert "alice" not in masked
        assert "[REDACTED]" in masked

    def test_b_forward_slash_windows_path_still_redacted(self):
        """(b) 슬래시 형태 Windows 경로(C:/Users/..)도 가려진다 (회귀)."""
        text = "file at C:/Users/alice/x.png"
        masked = common.sanitize_text(text)
        assert "alice" not in masked
        assert "[REDACTED]" in masked

    def test_c_real_filenotfounderror_redacted(self, tmp_path):
        """(c) 실제 FileNotFoundError 를 정화하면 경로가 안 보이고 타입은 보인다.

        실제 예외를 일으켜 검증한다 — 문자열 단위 테스트만으로는 이 결함을
        놓쳤다 (이것이 이 티켓의 핵심 교훈).
        """
        # 존재하지 않는 절대경로 — tmp_path 하위라도 예외 메시지에 절대경로가
        # repr 형태로 실린다. 사용자명을 흉내 내기 위해 alice 하위를 쓴다.
        nonexistent = tmp_path / "Users-alice-standin" / "nope_probe.png"
        try:
            open(str(nonexistent))
            raise AssertionError("예외가 발생해야 함")
        except FileNotFoundError as exc:
            sanitized = common.sanitize_error(exc)

        # 예외 타입명은 보인다.
        assert "FileNotFoundError" in sanitized
        # 사유 골격은 보인다.
        assert "No such file" in sanitized or "Errno" in sanitized
        # 경로 값(파일명 일부)은 가려진다.
        assert "nope_probe.png" not in sanitized
        assert "[REDACTED]" in sanitized

    def test_d_posix_home_path_redacted(self):
        """(d) POSIX /home/... 절대경로가 가려진다."""
        text = "error in /home/alice/secret.png"
        masked = common.sanitize_text(text)
        assert "alice" not in masked
        assert "[REDACTED]" in masked

    def test_d_posix_users_path_redacted(self):
        """(d) POSIX /Users/... 절대경로가 가려진다 (macOS)."""
        text = "error in /Users/bob/secret.png"
        masked = common.sanitize_text(text)
        assert "bob" not in masked
        assert "[REDACTED]" in masked

    def test_d_posix_etc_path_redacted(self):
        """(d) POSIX /etc/... 경로도 가려진다 (회귀)."""
        text = "error in /etc/passwd"
        masked = common.sanitize_text(text)
        assert "/etc/passwd" not in masked
        assert "[REDACTED]" in masked

    def test_e_https_url_not_redacted(self):
        """(e) https:// URL 은 가려지지 않는다 (과탐 방지).

        URL 은 진단에 필요하고 비밀이 아니다 — 문장 전체를 가리면 안 된다.
        """
        text = "see https://platform.openai.com/docs for help"
        masked = common.sanitize_text(text)
        assert "https://platform.openai.com/docs" in masked
        assert "[REDACTED]" not in masked

    def test_e_http_url_not_redacted(self):
        """(e) http:// URL 도 가려지지 않는다."""
        text = "fetch http://example.com/api failed"
        masked = common.sanitize_text(text)
        assert "http://example.com/api" in masked

    def test_f_korean_sentence_unchanged(self):
        """(f) 일반 한국어 문장이 훼손되지 않는다."""
        text = "상품 등록에 실패했습니다. 카테고리를 확인하세요."
        masked = common.sanitize_text(text)
        assert masked == text

    def test_f_mixed_korean_and_path(self):
        """(f) 한국어 사유와 경로가 섞여 있으면 사유는 남고 경로만 가려진다."""
        text = "이미지 열기 실패: C:\\Users\\alice\\img.png 를 찾을 수 없습니다"
        masked = common.sanitize_text(text)
        assert "이미지 열기 실패" in masked
        assert "찾을 수 없습니다" in masked
        assert "alice" not in masked
        assert "[REDACTED]" in masked
