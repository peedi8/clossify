# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""빈 설정 첫 실행 경로를 고정하는 시험 (백로그 N21a).

가장 많은 사용자가 겪을 경로인 **"config.example.json 그대로(자리표시자) 첫 실행"**
상태의 동작을 고정한다. 개발자 로컬 ``.local/config.json`` 을 읽으면 기계마다
결과가 달라지는 사고를 막기 위해 **반드시 몽키패치** 한다 — 임시 디렉터리에
``config.example.json`` 을 복사해 ``CLOSSIFY_CONFIG`` 환경변수로 가리킨다.

세 범위:
  ① ``check_config`` 가 정확히 말한다 — 자리표시자/부족항목 정확한 카운트,
     값 미노출(자리표시자 문자열 자체도 응답에 없다), 설정 파일 부재 시 예외 없이 보고.
  ② ``prepare_listing``/``register_product`` 가 자리표시자 설정 상태에서
     fail-closed 로 막힌다 — 조용히 진행되지 않는다.
  ③ ``registration_agent.md`` 의 자격증명 안내 절과 ``check_config`` 응답의
     부족 항목 이름이 서로 가리키는 대상이 같은지 대조.

네트워크 금지 — 토큰 발급·API 호출 경로는 mock. 자격증명 파일을 읽지 않는다.
소스 수정 없이 시험만 추가한다.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from unittest import mock

import pytest

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import mcp_server, naver_client

# config.example.json 의 자리표시자 상수 — 시험 본문에서 값을 직접 쓰지 않고
# 이 상수로만 가리킨다(시험 본문에 자리표시자 문자열이 등장하면 "값이 응답에
# 노출되는가" 검증의 위양성이 될 수 있다).
_PLACEHOLDER_CLIENT_ID = "REPLACE_WITH_NAVER_CLIENT_ID"
_PLACEHOLDER_CLIENT_SECRET = "REPLACE_WITH_NAVER_CLIENT_SECRET"
_PLACEHOLDER_ORIGIN_AREA = "REPLACE_WITH_ORIGIN_AREA_CODE"
_PLACEHOLDER_ORIGIN_CONTENT = "REPLACE_WITH_ORIGIN_CONTENT"
_PLACEHOLDER_AS_TEL = "REPLACE_WITH_REAL_AS_TEL"

# 의류 카테고리 (KC 불필요, WEAR 고시 타입).
_CLOTHING_CATEGORY = "50021299"


@pytest.fixture
def example_config_path(tmp_path: Path) -> Path:
    """config.example.json 을 임시 디렉터리에 복사하고 그 경로를 반환.

    이 픽스처가 반환하는 경로를 CLOSSIFY_CONFIG 로 가리키면 check_config 등이
    **자리표시자만 있는 깨끗한 첫 설치 상태** 를 본다. 개발자 로컬 .local/config.json
    을 결코 읽지 않는다(몽키패치).
    """
    src = _PROJECT_ROOT / "config.example.json"
    dst = tmp_path / "config.json"
    shutil.copyfile(src, dst)
    return dst


@pytest.fixture
def example_config_env(example_config_path: Path, monkeypatch) -> Path:
    """CLOSSIFY_CONFIG 환경변수로 example config 을 가리킨다.

    check_config 는 naver_client.config_path() → resolve_config_path() 로
    경로를 정하는데, 이 함수는 CLOSSIFY_CONFIG 환경변수를 최우선으로 읽는다.
    환경변수를 임시 디렉터리의 복사본으로 설정하면 개발자 로컬 파일은
    전혀 관여하지 않는다(사고 방지).
    """
    monkeypatch.setenv("CLOSSIFY_CONFIG", str(example_config_path))
    return example_config_path


# =========================================================================== #
# ① check_config 가 정확히 말한다
# =========================================================================== #
class TestCheckConfigSpeaksExactly:
    """자리표시자 설정 상태에서 check_config 응답의 정확성을 고정한다."""

    def test_placeholders_count_exact(self, example_config_env: Path) -> None:
        """자리표시자로 남아있는 필수 naver 키는 정확히 2개 (FIX-slug-optional).

        store_url_slug 는 선택 키가 되었다 — 자리표시자({STORE_SLUG})여도
        ``placeholders``/``missing`` 에 들어가지 않고 ``optional_absent`` 로만
        드러난다. 필수 자리표시자는 client_id, client_secret 2개뿐이다.
        """
        result = mcp_server.check_config()

        assert isinstance(result.get("placeholders"), list)
        # 자리표시자로 잡힌 필수 naver 키는 정확히 2개.
        placeholders = result["placeholders"]
        assert len(placeholders) == 2, f"placeholders 개수가 2가 아님: {placeholders}"
        assert sorted(placeholders) == sorted(
            ["client_id", "client_secret"]
        ), f"placeholders 키 이름 불일치: {placeholders}"
        # 선택 슬러그는 placeholders/missing 어디에도 없다.
        assert (
            "store_url_slug" not in placeholders
        ), f"선택 슬러그가 placeholders 에 잡힘: {placeholders}"
        assert (
            result.get("missing") == []
        ), f"키가 존재하는데 missing 에 잡혔다: {result.get('missing')}"
        # 대신 optional_absent 에 드러난다 (ok 를 깎지 않는 힌트).
        assert result.get("optional_absent") == [
            "store_url_slug"
        ], f"optional_absent 불일치: {result.get('optional_absent')}"

    def test_ok_false_when_placeholders(self, example_config_env: Path) -> None:
        """자리표시자가 하나라도 있으면 ok=False."""
        result = mcp_server.check_config()
        assert result.get("ok") is False, "자리표시자가 있는데 ok=True 는 결함"

    def test_present_map_all_false(self, example_config_env: Path) -> None:
        """present 맵의 값이 전부 False 다 (자리표시자이므로)."""
        result = mcp_server.check_config()
        present = result.get("present")
        assert isinstance(present, dict)
        for key in ("client_id", "client_secret", "store_url_slug"):
            assert present.get(key) is False, f"자리표시자인데 present[{key!r}]==True: {present}"

    def test_no_placeholder_string_in_response(self, example_config_env: Path) -> None:
        """자리표시자 문자열 자체도 응답 어디에도 실리지 않는다.

        워크오더 ①: "자리표시자 문자열 자체도 응답에 실리면 안 된다.
        실린다면 그건 결함이니 정지·보고." — 결함이면 본 시험이 빨간불로 잡는다.
        """
        result = mcp_server.check_config()
        serialized = json.dumps(result, ensure_ascii=False)
        # 자리표시자 토큰 전체가 한 번도 등장하면 안 된다.
        for token in (
            _PLACEHOLDER_CLIENT_ID,
            _PLACEHOLDER_CLIENT_SECRET,
            _PLACEHOLDER_ORIGIN_AREA,
            _PLACEHOLDER_ORIGIN_CONTENT,
            _PLACEHOLDER_AS_TEL,
        ):
            assert (
                token not in serialized
            ), f"자리표시자 문자열이 check_config 응답에 노출됨: {token!r}"
        # "REPLACE_WITH_" 접두 자체도 노출되면 안 된다 (값 미노출 계약).
        assert (
            "REPLACE_WITH_" not in serialized
        ), "REPLACE_WITH_ 토큰이 응답에 노출됨 (값 미노출 계약 위반)"

    def test_no_config_file_reports_cleanly(self, tmp_path: Path, monkeypatch) -> None:
        """설정 파일이 아예 없을 때 예외로 죽지 않고 그 사실을 말한다.

        워크오더 ①: "설정 파일이 아예 없을 때 도 예외로 죽지 않고 그 사실을 말한다."
        """
        # 존재하지 않는 경로 지정.
        ghost = tmp_path / "does_not_exist.json"
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(ghost))

        # 예외로 죽지 않고 dict 를 반환해야 한다.
        result = mcp_server.check_config()
        assert isinstance(result, dict)
        assert result.get("ok") is False
        # 파일 부재 사실을 명시적으로 말해야 한다.
        error = result.get("error")
        assert isinstance(error, str) and len(error) > 0
        assert "config" in error, f"파일 부재 사유가 부실하다: {error!r}"

    def test_origin_configured_false_with_example(self, example_config_env: Path) -> None:
        """example config 의 원산지는 자리표시자 → origin_configured=False."""
        result = mcp_server.check_config()
        assert result.get("origin_configured") is False

    def test_as_tel_configured_false_with_example(self, example_config_env: Path) -> None:
        """example config 의 AS 전화번호는 자리표시자 → as_tel_configured=False."""
        result = mcp_server.check_config()
        assert result.get("as_tel_configured") is False


# =========================================================================== #
# ② prepare_listing / register_product 가 fail-closed 로 막힌다
# =========================================================================== #
class TestPrepareRegisterFailClosed:
    """자리표시자 설정 상태에서 prepare_listing·register_product 가 막히는가.

    핵심: 자리표시자가 규제값(원산지·AS·고시필드)으로 전송되는 경로가 없어야 한다.
    PR #26 5라운드에서 원산지 자리표시자 전송을 잡은 회귀를 본 시험도 잡는다.
    """

    def test_register_product_blocks_with_placeholder_origin(
        self, example_config_env: Path
    ) -> None:
        """자리표시자 원산지 설정으로 register_product 가 막힌다 (fail-closed).

        naver_client.register_product(네이버 호출) 자체는 mock 으로 가로채고,
        컴플라이언스 게이트가 자리표시자 원산지를 잡아 거부하는지 확인한다.
        네이버 API 는 0회 호출되어야 한다.
        """
        naver_calls: list = []
        with mock.patch.object(
            naver_client,
            "register_product",
            side_effect=lambda *a, **kw: naver_calls.append((a, kw)) or (200, {}),
        ):
            result = mcp_server.register_product(
                name="테스트니트",
                price=30000,
                image_urls=["http://cdn/x.png"],
                category_id=_CLOTHING_CATEGORY,
                detail_html="<html><body>상세</body></html>",
                preview_confirmed=True,
            )

        # 등록이 거부되어야 한다.
        assert result["ok"] is False, f"자리표시자 원산지인데 등록이 통과함: {result}"
        # 네이버 API 가 호출되지 않아야 한다.
        assert len(naver_calls) == 0, f"자리표시자 원산지로 네이버 API 호출 발생: {naver_calls}"
        # 거부 사유가 명시적으로 있어야 한다 (조용히 진행 금지).
        message = result.get("message") or result.get("error") or ""
        assert (
            isinstance(message, str) and len(message) > 0
        ), "거부 사유가 비어있다 (조용히 막힘 — 규율 위반)"

    def test_register_product_blocked_response_names_origin(self, example_config_env: Path) -> None:
        """거부 응답이 '무엇이 없어서 막혔는지' 를 말한다 (원산지/KC/고시필드)."""
        with mock.patch.object(naver_client, "register_product", return_value=(200, {})):
            result = mcp_server.register_product(
                name="테스트니트",
                price=30000,
                image_urls=["http://cdn/x.png"],
                category_id=_CLOTHING_CATEGORY,
                detail_html="<html><body>상세</body></html>",
                preview_confirmed=True,
            )
        # 컴플라이언스 위반 메시지에 사유가 드러나야 한다.
        # 의류 카테고리 + 자리표시자 원산지 → 최소 원산지 위반이 잡혀야 한다.
        text = json.dumps(result, ensure_ascii=False)
        # "원산지" 또는 "origin" 키워드가 사유에 들어있어야 한다.
        assert ("원산지" in text) or (
            "origin" in text.lower()
        ), f"거부 사유에 원산지/origin 이 없다: {text}"

    def test_register_product_no_placeholder_in_payload(self, example_config_env: Path) -> None:
        """자리표시자 문자열이 네이버로 전송될 페이로드에 등장하지 않는다.

        PR #26 5라운드 회귀: 원산지 자리표시자 전송을 막는 회귀를 본 시험도 잡는다.
        네이버 호출이 일어나지 않더라도, 혹시 모를 경로를 위해 호출 레코더를 붙여
        자리표시자 문자열이 한 번도 인자로 넘어가지 않았는지 검증한다.
        """
        calls: list = []
        with mock.patch.object(
            naver_client,
            "register_product",
            side_effect=lambda *a, **kw: calls.append({"args": a, "kwargs": kw}) or (200, {}),
        ):
            mcp_server.register_product(
                name="테스트니트",
                price=30000,
                image_urls=["http://cdn/x.png"],
                category_id=_CLOTHING_CATEGORY,
                detail_html="<html><body>상세</body></html>",
                preview_confirmed=True,
            )
        # 자리표시자 토큰이 한 번도 호출 인자에 등장하지 않아야 한다.
        for call in calls:
            blob = json.dumps(call, ensure_ascii=False, default=str)
            assert (
                _PLACEHOLDER_ORIGIN_AREA not in blob
            ), f"원산지 자리표시자가 네이버 호출에 실림: {blob}"
            assert (
                _PLACEHOLDER_ORIGIN_CONTENT not in blob
            ), f"원산지 자리표시자(content)가 네이버 호출에 실림: {blob}"

    def test_prepare_listing_blocks_on_invalid_input(self, example_config_env: Path) -> None:
        """prepare_listing 은 상품 입력 검증에서 명확한 사유와 함께 막힌다.

        자리표시자 설정과 무관하게, 빈 상품 입력/이미지 누락 등에는 ValueError 를
        감싸 명확한 error/needs_user 로 응답한다 (조용히 진행 금지).
        """
        # 상품명·가격·이미지 모두 빈 입력 — ValueError 경로.
        result = mcp_server.prepare_listing(product={})
        assert result.get("ok") is False
        # 사유가 명시적으로 있어야 한다.
        err = result.get("error") or ""
        assert (
            isinstance(err, str) and len(err) > 0
        ), f"prepare_listing 거부 사유가 비어있다: {result}"

    def test_prepare_listing_needs_user_includes_missing(self, example_config_env: Path) -> None:
        """prepare_listing 의 needs_user 가 빠진 항목을 말한다."""
        result = mcp_server.prepare_listing(product={})
        needs_user = result.get("needs_user") or []
        assert isinstance(needs_user, list)
        assert len(needs_user) > 0, f"빈 입력인데 needs_user 가 비었다 (조용한 진행): {result}"


# =========================================================================== #
# ③ 안내가 프롬프트와 어긋나지 않는다
# =========================================================================== #
class TestPromptGuidanceAgreesWithResponse:
    """registration_agent.md 의 자격증명 안내와 check_config 응답의 키 이름 대조.

    프롬프트가 ``client_id``/``client_secret`` 을 말하면 응답의 키도 그것이어야 한다.
    어긋나면 사용자가 프롬프트대로 파일을 채워도 check_config 가 "미설정" 이라
    거짓 말을 하게 된다.
    """

    @pytest.fixture
    def prompt_text(self) -> str:
        """registration_agent.md 의 자격증명 발급·연결 안내 절 텍스트."""
        path = _PROJECT_ROOT / "src" / "clossify" / "agents" / "registration_agent.md"
        return path.read_text(encoding="utf-8")

    def test_prompt_names_client_id(self, prompt_text: str) -> None:
        """프롬프트가 client_id 를 안내한다."""
        assert "client_id" in prompt_text, "registration_agent.md 가 client_id 를 안내하지 않는다"

    def test_prompt_names_client_secret(self, prompt_text: str) -> None:
        """프롬프트가 client_secret 을 안내한다."""
        assert (
            "client_secret" in prompt_text
        ), "registration_agent.md 가 client_secret 을 안내하지 않는다"

    def test_response_keys_match_prompt(self, prompt_text: str, example_config_env: Path) -> None:
        """check_config 응답의 placeholders/present 키가 프롬프트가 말한 이름과 같다.

        워크오더 ③ 핵심: "프롬프트가 client_id/client_secret 을 말하면 응답의 키도
        그것이어야 한다." config.example.json 의 키 이름과 check_config 가 보고하는
        키 이름이 일치해야 한다.
        """
        result = mcp_server.check_config()
        # placeholders 에 잡힌 키 이름.
        reported_keys = set(result.get("placeholders") or []) | set(
            (result.get("present") or {}).keys()
        )
        # 프롬프트가 안내하는 자격증명 키가 응답에도 같은 이름으로 있어야 한다.
        for prompt_key in ("client_id", "client_secret"):
            assert prompt_key in reported_keys, (
                f"프롬프트는 {prompt_key!r} 를 안내하지만 check_config 응답 키에 없다: "
                f"{reported_keys}"
            )

    def test_config_example_keys_match_response(self, example_config_env: Path) -> None:
        """config.example.json 의 naver 섹션 키와 check_config 보고 키가 같다.

        사용자가 config.example.json 을 그대로 복사해 values 만 채우면
        check_config 가 ok=True 라고 말하는 자리가 같아야 한다 — 키 이름이
        어긋나면 "채웠는데도 미설정" 이라는 거짓 진단이 나온다.
        """
        # example config 의 naver 키 읽기.
        cfg = json.loads(example_config_env.read_text(encoding="utf-8"))
        naver_keys = set((cfg.get("naver") or {}).keys()) - {"_comment", "type"}
        # check_config 응답의 present 키.
        result = mcp_server.check_config()
        reported = set((result.get("present") or {}).keys())
        # 자리표시자로 잡힌 키도 같은 이름이어야 한다.
        reported |= set(result.get("placeholders") or [])
        # 세 필수 키가 example config 에도, 응답에도 같은 이름으로 있어야.
        for key in ("client_id", "client_secret", "store_url_slug"):
            assert (
                key in naver_keys
            ), f"config.example.json 의 naver 섹션에 {key!r} 이 없다: {naver_keys}"
            assert key in reported, f"check_config 응답에 {key!r} 이 없다: {reported}"
