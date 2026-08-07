# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
"""이미지 생성 단계 분기 검증 테스트.

본 테스트는 ``image_gen`` 모듈이 ``config.image_providers`` 를 실제로 읽어
단계 분기(①②③④) 를 수행하는지, 그리고 **``register.prepare_listing`` 의
생성 분기가 도달 가능한 분기**인지 검증한다.

과거 결함 (죽은 코드): ``prepare_listing`` 의 생성 분기 조건이
``want_generation and not user_has_images`` 였는데, 이 조건이 참이 되려면
``image_sources`` 가 비어있거나 전부 공백이어야 했고 — 그런 입력은
상위 ``attach_images`` 게이트가 이미 ``ValueError`` 로 차단하고 있었다.
즉 생성 분기로 진입하는 입력 경로가 존재하지 않았다. ``image_gen.generate``
만 직접 부르는 테스트는 이 결함을 잡지 못했다.

이 결함의 수정(**부족분 기반 생성**) 을 모든 케이스에서
★ **``register.prepare_listing`` 진입점** 으로 검증한다.

케이스 매항 (과업 계약 (a)-(g), 전부 ``prepare_listing`` 경유):
  (a) 원본 1장 + ``needed_cuts=3`` + ``generate_images=True`` → 생성 호출 **1회**,
      생성 요청 컷 수 = **2**(부족분만), 결과 URL 이 원본 **뒤에** 붙는다.
  (b) 원본 3장 + ``needed_cuts=3`` + ``generate_images=True`` → 생성 호출 **0회**.
  (c) 원본 1장 + ``needed_cuts=3`` + ``generate_images`` 없음/False → 생성 호출 **0회**.
  (d) 원본 0장 → **여전히 ValueError 차단**, 생성 호출 **0회** (안전 불변식).
  (e) 부족분 있음 + 키 미설정 → 생성 실패가 **조용히 넘어가지 않고** ``needs_user``
      에 사유가 실린다.
  (f) 생성 성공 후 **원본 URL 이 앞쪽에 그대로**, 개수 = 원본 + 생성분.
  (g) ``images_ready(sources)`` 를 인자 없이 부르면 기존 거동과 같다 (호환 회귀).

부수 검증:
  - 키 없음/플레이스홀더 감지 (``generate`` 의 fail-closed 거부).
  - 단가표/비용 메타 (``IMAGE_GENERATION_PRICE_POLICY.md`` 단위 규약).
  - ``check_config`` 호환성.

모든 테스트는 네트워크를 호출하지 않는다 — ``session``/``generate_fn``/``attach_fn``
주입으로 호출 수를 센다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import common, image_gen, mcp_server, naver_client, register


# --------------------------------------------------------------------------- #
# 공통 픽스처/헬퍼.
# --------------------------------------------------------------------------- #
@pytest.fixture
def isolated_prepared_dir(tmp_path, monkeypatch):
    """common.PREPARED_DIR 을 tmp_path 로 격리."""
    fake_dir = tmp_path / "prepared"
    fake_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(common, "PREPARED_DIR", fake_dir)
    return fake_dir


def _valid_openai_config() -> dict:
    """유효한 OpenAI 키가 채워진 config dict."""
    return {
        "image_providers": {
            "openai": {"api_key": "sk-test-key", "model": "gpt-image-2"},
        }
    }


def _valid_gemini_config() -> dict:
    """유효한 Gemini 키가 채워진 config dict."""
    return {
        "image_providers": {
            "gemini": {"api_key": "AIza-test-key", "model": "gemini-3.1-flash-image"},
        }
    }


def _placeholder_config() -> dict:
    """플레이스홀더 키만 있는 config dict (config.example.json 형태)."""
    return {
        "image_providers": {
            "openai": {"api_key": "REPLACE_WITH_OPENAI_API_KEY", "model": "gpt-image-2"},
            "gemini": {"api_key": "REPLACE_WITH_GEMINI_API_KEY", "model": "gemini-3.1-flash-image"},
        }
    }


def _empty_config() -> dict:
    """image_providers 섹션 자체가 없는 config."""
    return {}


class _FakeResponse:
    """requests.Response 를 흉내내는 최소한의 fake."""

    def __init__(self, status_code: int = 200, json_data: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text or json.dumps(self._json)

    def json(self) -> dict:
        return self._json


def _ok_openai_response() -> _FakeResponse:
    """OpenAI Images API 정상 응답 흉내."""
    return _FakeResponse(
        200,
        {"data": [{"url": "https://cdn.test/generated/openai-1.png"}]},
    )


def _ok_gemini_response() -> _FakeResponse:
    """Gemini generateContent 정상 응답 흉내 (base64 인라인)."""
    return _FakeResponse(
        200,
        {
            "candidates": [
                {"content": {"parts": [{"inlineData": {"data": "ZmFrZS1pbWFnZS1ieXRlcw=="}}]}}
            ]
        },
    )


class _RecordingSession:
    """post 호출을 기록하고 미리 준비한 응답을 반환하는 fake session."""

    def __init__(self, response: _FakeResponse):
        self._response = response
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self._response

    def close(self):
        pass


def _fake_attach_honest(sources):
    """``images.attach_images`` 대체 — ★ 정직한 fake.

    핵심: 빈 문자열/공백 문자열은 **진짜 빈 소스** 로 다룬다. 실제
    ``images.attach_images`` 처럼 — 비어있는 소스는 URL 로 승격하지 않는다.
    과거 테스트 결함의 원인이 이 부정직한 fake 였다: ``[""]`` 을
    ``["http://cdn/0.png"]`` 로 바꿔버리면 상위 게이트가 통과하면서
    ``images_ready`` 는 False 인 모순이 생긴다. 본 fake 는 비어있는 소스를
    빈 URL 로 두고 rejected 로 분류해 상위 게이트가 일관되게 차단하게 한다.

    유효한(비어있지 않은) 소스는 CDN URL 로 정규화한다 (attach_images 규약).
    """
    urls: list[str] = []
    rejected: list[dict] = []
    for i, s in enumerate(sources):
        if not isinstance(s, str) or not s.strip():
            rejected.append({"index": i, "source": s, "reason": "빈 문자열/공백 소스 (fake)"})
            continue
        urls.append(f"http://cdn/test/img{i}.png")
    return {"urls": urls, "rejected": rejected, "notes": []}


def _fake_attach_lenient(sources):
    """``images.attach_images`` 대체 — 모든 소스를 URL 로 승격.

    ⚠️ 본 fake 는 **유효한 소스에만** 쓴다 (부족분 시나리오에서 원본 N 장을
    자연스럽게 통과시키기 위함). 빈 문자열 소스와 함께 쓰면 상위 게이트와
    ``valid_original_count`` 계산이 어긋나므로 — 본 파일에서는 빈 문자열
    시나리오에 이 fake 를 쓰지 않는다(과거 결함 재현 방지).
    """
    urls = [f"http://cdn/test/img{i}.png" for i in range(len(sources))]
    return {"urls": urls, "rejected": [], "notes": []}


def _ok_generate(prompt, *, needed_cuts=1, **kwargs):
    """성공하는 fake generate — 호출 인자를 단언하기 위해 외부에서 랩핑 가능.

    본 헬퍼는 단일 기본형만 반환한다. 호출부에서 필요 시 호출 기록을 위해
    자체 클로저를 만든다.
    """
    return {
        "ok": True,
        "provider": "openai",
        "model": "gpt-image-2",
        "needed_cuts": needed_cuts,
        "api_call_count": needed_cuts,
        "output_canvas_count": needed_cuts,
        "output_layout": "single",
        "panel_count_used": needed_cuts,
        "estimated_cost_usd": 0.006 * needed_cuts,
        "image_urls": [f"http://gen/cut{i}.png" for i in range(needed_cuts)],
        "error": None,
    }


# --------------------------------------------------------------------------- #
# (a) 부족분만큼만 생성 — 원본 1장 + needed_cuts=3 → 생성 1회, 컷 수=2.
# --------------------------------------------------------------------------- #
class TestCaseAShortfallOnlyGeneration:
    """(a) 원본이 필요 컷 수를 채우지 못하면 **부족분만큼만** 생성한다.

    과거 결함 회귀: 과거에는 이 시나리오 자체가 도달 불가였다. 이제는
    원본 1장이 통과한 *뒤* 필요 컷 수(3) 에 미달하면 ③④ 로 진입해
    부족분(3-1=2) 만큼만 생성을 부른다. 전체 3장을 다시 만들지 않는다
    (돈 누수 방지). 생성 결과는 원본 뒤에 붙는다 (순서 보존).
    """

    def test_prepare_listing_calls_generate_once_with_shortfall_only(
        self, isolated_prepared_dir, monkeypatch
    ):
        generate_calls: list[dict] = []

        def fake_generate(prompt, *, needed_cuts=1, **kwargs):
            generate_calls.append({"prompt": prompt, "needed_cuts": needed_cuts})
            return _ok_generate(prompt, needed_cuts=needed_cuts)

        monkeypatch.setattr(naver_client, "load_config", lambda: _valid_openai_config())

        d = {
            "name": "부족분상품",
            "salePrice": 20000,
            "image_sources": ["http://user/original1.jpg"],  # 원본 1장
            "generate_images": True,
            "needed_cuts": 3,  # 3장 필요 → 부족분 2
        }
        payload = register.prepare_listing(
            d, attach_fn=_fake_attach_lenient, generate_fn=fake_generate
        )

        # (1) 생성은 정확히 1회 호출.
        assert (
            len(generate_calls) == 1
        ), f"생성이 1회가 아님: {len(generate_calls)}회 (부족분 있을 때 1회여야)"
        # (2) ★ 생성 요청 컷 수는 부족분(2) 이어야 — 전체(3) 가 아니라.
        assert generate_calls[0]["needed_cuts"] == 2, (
            f"생성 컷 수가 부족분(2)이 아님: {generate_calls[0]['needed_cuts']} "
            "(전체 needed_cuts 를 다시 만들면 돈 누수)"
        )
        # (3) image_generation 메타에 부족분 추적 필드가 있다.
        meta = payload.get("image_generation") or {}
        assert meta.get("requested_needed_cuts") == 3, "요청 needed_cuts 누락/불일치"
        assert meta.get("original_count") == 1, "원본 카운트 불일치"
        assert meta.get("shortfall") == 2, "부족분 불일치"

    def test_generated_urls_appended_after_originals(self, isolated_prepared_dir, monkeypatch):
        """생성 URL 은 원본 *뒤에* 붙는다 (대표이미지 규약·순서 보존)."""
        monkeypatch.setattr(naver_client, "load_config", lambda: _valid_openai_config())

        d = {
            "name": "순서보존상품",
            "salePrice": 20000,
            "image_sources": ["http://user/only-original.jpg"],
            "generate_images": True,
            "needed_cuts": 3,
        }
        payload = register.prepare_listing(
            d, attach_fn=_fake_attach_lenient, generate_fn=_ok_generate
        )
        urls = payload["images"]["listing_urls"]
        # 원본 1장(attach 가 만든 URL) + 생성 2장(부족분).
        assert len(urls) == 3, f"총 URL 개수가 원본+생성분(3)이 아님: {len(urls)}"
        # 원본(attach URL) 이 앞에 와야 — 생성 URL(http://gen/...) 이 앞이면 안 됨.
        attach_urls = [u for u in urls if "cdn/test" in u]
        gen_urls = [u for u in urls if "gen/" in u]
        assert len(attach_urls) == 1, f"원본 URL 개수 불일치: {attach_urls}"
        assert len(gen_urls) == 2, f"생성 URL 개수 불일치: {gen_urls}"
        assert urls.index(attach_urls[0]) < urls.index(
            gen_urls[0]
        ), f"생성 URL 이 원본보다 앞에 있음 (순서 위반): {urls}"


# --------------------------------------------------------------------------- #
# (b) 원본이 필요 컷 수를 채움 — 생성 0회.
# --------------------------------------------------------------------------- #
class TestCaseBOriginalsSufficientSkipsGeneration:
    """(b) 원본이 needed_cuts 를 채우면 생성 경로에 진입하지 않는다."""

    def test_prepare_listing_skips_generation_when_originals_meet_needed_cuts(
        self, isolated_prepared_dir, monkeypatch
    ):
        generate_calls: list[dict] = []

        def fake_generate(prompt, *, needed_cuts=1, **kwargs):
            generate_calls.append({"prompt": prompt, "needed_cuts": needed_cuts})
            return _ok_generate(prompt, needed_cuts=needed_cuts)

        monkeypatch.setattr(naver_client, "load_config", lambda: _valid_openai_config())

        d = {
            "name": "충분상품",
            "salePrice": 20000,
            "image_sources": [
                "http://user/a.jpg",
                "http://user/b.jpg",
                "http://user/c.jpg",
            ],  # 원본 3장
            "generate_images": True,
            "needed_cuts": 3,  # 3장 필요 → 부족분 0
        }
        payload = register.prepare_listing(
            d, attach_fn=_fake_attach_lenient, generate_fn=fake_generate
        )
        assert (
            len(generate_calls) == 0
        ), f"원본이 needed_cuts(3) 를 채웠는데 생성 호출됨: {len(generate_calls)}회"
        # image_generation 메타가 싣히지 않아야 (생성 시도 자체가 없었으므로).
        assert (
            "image_generation" not in payload
        ), "생성 시도가 없었는데 image_generation 메타가 payload 에 있음"
        # listing_urls 는 원본 3장만.
        urls = payload["images"]["listing_urls"]
        assert len(urls) == 3, f"생성 안 했는데 URL 개수가 3이 아님: {len(urls)}"


# --------------------------------------------------------------------------- #
# (c) generate_images 미설정/False — 생성 0회.
# --------------------------------------------------------------------------- #
class TestCaseCNoGenerateFlagSkipsGeneration:
    """(c) generate_images 가 없거나 False 면 부족분이 있어도 생성 0회."""

    def test_prepare_listing_no_generate_flag(self, isolated_prepared_dir, monkeypatch):
        generate_calls: list[dict] = []

        def fake_generate(prompt, *, needed_cuts=1, **kwargs):
            generate_calls.append({"prompt": prompt, "needed_cuts": needed_cuts})
            return _ok_generate(prompt, needed_cuts=needed_cuts)

        monkeypatch.setattr(naver_client, "load_config", lambda: _valid_openai_config())

        d = {
            "name": "생성미원청",
            "salePrice": 20000,
            "image_sources": ["http://user/a.jpg"],  # 원본 1장
            # generate_images 없음.
            "needed_cuts": 3,  # 부족분 2 가 있지만 생성을 원하지 않음.
        }
        payload = register.prepare_listing(
            d, attach_fn=_fake_attach_lenient, generate_fn=fake_generate
        )
        assert (
            len(generate_calls) == 0
        ), f"generate_images 미설정인데 생성 호출됨: {len(generate_calls)}회"
        assert "image_generation" not in payload

    def test_prepare_listing_generate_false(self, isolated_prepared_dir, monkeypatch):
        generate_calls: list[dict] = []

        def fake_generate(prompt, *, needed_cuts=1, **kwargs):
            generate_calls.append({"prompt": prompt, "needed_cuts": needed_cuts})
            return _ok_generate(prompt, needed_cuts=needed_cuts)

        monkeypatch.setattr(naver_client, "load_config", lambda: _valid_openai_config())

        d = {
            "name": "생성명시거부",
            "salePrice": 20000,
            "image_sources": ["http://user/a.jpg"],
            "generate_images": False,  # 명시 False.
            "needed_cuts": 3,
        }
        payload = register.prepare_listing(
            d, attach_fn=_fake_attach_lenient, generate_fn=fake_generate
        )
        assert len(generate_calls) == 0, "generate_images=False 인데 생성 호출됨"
        assert "image_generation" not in payload


# --------------------------------------------------------------------------- #
# (d) 원본 0장 — 여전히 ValueError 차단, 생성 0회 (안전 불변식).
# --------------------------------------------------------------------------- #
class TestCaseDOriginalGatePreserved:
    """(d) 원본 0장이면 생성으로 대체할 수 없다.

    안전 불변식: ``prepare_listing`` 은 상위 ``attach_images`` 게이트가
    원본을 검증한 *뒤* 단계 분기를 수행한다. 원본 0장이면 게이트가 먼저
    거부한다 — 생성이 이 자리를 우회할 수 없다. 이것이 과거 죽은 코드
    결함의 핵심이었고 (생성 분기가 게이트와 겹쳐 도달 불가), 이제 생성은
    **부족한 추가 컷** 만 메운다(원본 자리 아님).
    """

    def test_prepare_listing_blocks_empty_image_sources(self, isolated_prepared_dir, monkeypatch):
        monkeypatch.setattr(naver_client, "load_config", lambda: _valid_openai_config())
        generate_calls: list = []

        def fake_generate(*a, **kw):
            generate_calls.append(1)
            return _ok_generate(*a, **kw)

        d = {
            "name": "원본없음1",
            "salePrice": 10000,
            "image_sources": [],  # 빈 리스트 — 상위 게이트에서 ValueError.
            "generate_images": True,
        }
        with pytest.raises(ValueError, match="image_sources"):
            register.prepare_listing(d, attach_fn=_fake_attach_honest, generate_fn=fake_generate)
        assert len(generate_calls) == 0, "원본 0장인데 generate 가 호출됨 (게이트 우회)"

    def test_prepare_listing_blocks_blank_only_sources(self, isolated_prepared_dir, monkeypatch):
        """image_sources 가 빈 문자열/공백으로만 채워진 경우 — 게이트 차단."""
        monkeypatch.setattr(naver_client, "load_config", lambda: _valid_openai_config())
        generate_calls: list = []

        def fake_generate(*a, **kw):
            generate_calls.append(1)
            return _ok_generate(*a, **kw)

        d = {
            "name": "원본없음2",
            "salePrice": 10000,
            "image_sources": ["", "   ", ""],  # 전부 공백 → 정직한 fake 가 rejected.
            "generate_images": True,
        }
        # 정직한 fake 는 빈 소스를 rejected 로 분류 → 상위 게이트 ValueError.
        with pytest.raises(ValueError):
            register.prepare_listing(d, attach_fn=_fake_attach_honest, generate_fn=fake_generate)
        assert len(generate_calls) == 0, "공백 소스만 있는데 generate 가 호출됨"

    def test_prepare_listing_blocks_zero_listing_urls_after_attach(
        self, isolated_prepared_dir, monkeypatch
    ):
        """attach 결과 listing_urls 가 0장이면 ValueError (생성 경로 진입 전)."""
        monkeypatch.setattr(naver_client, "load_config", lambda: _valid_openai_config())
        generate_calls: list = []

        def fake_generate(*a, **kw):
            generate_calls.append(1)
            return _ok_generate(*a, **kw)

        def fake_attach_zero_urls(sources):
            return {"urls": [], "rejected": [], "notes": []}

        d = {
            "name": "정규화후0장",
            "salePrice": 10000,
            "image_sources": ["only-source"],
            "generate_images": True,
        }
        with pytest.raises(ValueError, match="0장"):
            register.prepare_listing(d, attach_fn=fake_attach_zero_urls, generate_fn=fake_generate)
        assert len(generate_calls) == 0


# --------------------------------------------------------------------------- #
# (e) 부족분 있음 + 키 미설정 — needs_user 에 사유.
# --------------------------------------------------------------------------- #
class TestCaseEKeyMissingReportsToUser:
    """(e) 부족분이 있는데 키가 없으면 생성이 **조용히 넘어가지 않는다**.

    fail-closed 원칙: 생성을 원했는데 못 한 사실을 사용자가 알아야 한다.
    ``image_gen.generate`` 가 반환하는 ``error`` 를 ``prepare_listing`` 은
    ``needs_user`` 의 ``field=image_generation`` 항목으로 싣는다.
    """

    def test_prepare_listing_reports_key_missing_in_needs_user(
        self, isolated_prepared_dir, monkeypatch
    ):
        # 실제 image_gen.generate 를 타게 둔다 — config 를 비워 키 없음을 재현.
        monkeypatch.setattr(naver_client, "load_config", lambda: _empty_config())

        d = {
            "name": "키없음상품",
            "salePrice": 20000,
            "image_sources": ["http://user/a.jpg"],  # 원본 1장
            "generate_images": True,
            "needed_cuts": 3,  # 부족분 2 → 생성 시도 → 키 없음 → 거부.
        }
        payload = register.prepare_listing(
            d,
            attach_fn=_fake_attach_lenient,
            generate_fn=None,  # 실제 image_gen.generate 사용.
        )
        needs_user = payload.get("needs_user") or []
        gen_hints = [
            nu
            for nu in needs_user
            if isinstance(nu, dict) and nu.get("field") == "image_generation"
        ]
        assert (
            len(gen_hints) >= 1
        ), f"키 미설정인데 needs_user 에 생성 안내가 없음 (조용한 실패): {needs_user}"
        # 안내 문구에 사유(키 발급/설정) 가 포함돼야.
        why = str(gen_hints[0].get("why") or "")
        assert why, "needs_user 안내에 사유가 없음 (빈 문자열)"
        assert (
            "키" in why or "api_key" in why.lower()
        ), f"안내가 키 발급/설정을 언급하지 않음: {why!r}"
        # image_generation 메타도 실패로 기록돼야.
        meta = payload.get("image_generation") or {}
        assert meta.get("ok") is False, "키 없는데 image_generation.ok=True"
        # 부족분 추적 필드도 그대로.
        assert meta.get("shortfall") == 2, "부족분 추적 필드 누락"

    def test_prepare_listing_reports_generation_failure_in_needs_user(
        self, isolated_prepared_dir, monkeypatch
    ):
        """generate_fn 이 실패(ok=False) 를 반환해도 needs_user 에 싣는다."""
        monkeypatch.setattr(naver_client, "load_config", lambda: _valid_openai_config())

        def fake_generate_fails(prompt, *, needed_cuts=1, **kwargs):
            return {
                "ok": False,
                "error": "테스트용 생성 실패",
                "image_urls": [],
            }

        d = {
            "name": "생성실패상품",
            "salePrice": 15000,
            "image_sources": ["http://user/a.jpg"],  # 원본 1장 → 부족분 있음.
            "generate_images": True,
            "needed_cuts": 2,
        }
        payload = register.prepare_listing(
            d, attach_fn=_fake_attach_lenient, generate_fn=fake_generate_fails
        )
        needs_user = payload.get("needs_user") or []
        gen_hints = [
            nu
            for nu in needs_user
            if isinstance(nu, dict) and nu.get("field") == "image_generation"
        ]
        assert len(gen_hints) >= 1, f"생성 실패인데 needs_user 안내 없음: {needs_user}"
        assert gen_hints[0].get("why"), "needs_user 안내에 사유가 없음"


# --------------------------------------------------------------------------- #
# (f) 생성 성공 후 원본 URL 이 앞쪽에 그대로, 개수 = 원본 + 생성분.
# --------------------------------------------------------------------------- #
class TestCaseFOriginalsPreservedFront:
    """(f) 생성 성공 시 원본 listing_urls 가 앞쪽에 그대로, 총 개수 = 원본+생성."""

    def test_original_urls_preserved_and_generated_appended(
        self, isolated_prepared_dir, monkeypatch
    ):
        generate_calls: list[dict] = []

        def fake_generate(prompt, *, needed_cuts=1, **kwargs):
            generate_calls.append({"needed_cuts": needed_cuts})
            return _ok_generate(prompt, needed_cuts=needed_cuts)

        monkeypatch.setattr(naver_client, "load_config", lambda: _valid_openai_config())

        # 원본 2장 + needed_cuts=5 → 부족분 3.
        d = {
            "name": "원본보존상품",
            "salePrice": 30000,
            "image_sources": ["http://user/a.jpg", "http://user/b.jpg"],
            "generate_images": True,
            "needed_cuts": 5,
        }
        payload = register.prepare_listing(
            d, attach_fn=_fake_attach_lenient, generate_fn=fake_generate
        )

        # 생성은 부족분(3) 만큼만 1회 호출.
        assert len(generate_calls) == 1
        assert generate_calls[0]["needed_cuts"] == 3, "부족분(3)이 아님"

        urls = payload["images"]["listing_urls"]
        # 총 5개 (원본 2 + 생성 3).
        assert len(urls) == 5, f"URL 개수가 원본+생성분(5)이 아님: {len(urls)}"
        # 앞 2개는 원본(attach URL), 뒤 3개는 생성.
        for i in range(2):
            assert "cdn/test" in urls[i], f"원본 자리({i})에 생성 URL 이 있음 (순서 위반): {urls}"
        for i in range(2, 5):
            assert "gen/" in urls[i], f"생성 자리({i})에 원본 URL 이 있음 (순서 위반): {urls}"

        # meta 도 총 개수와 일치.
        meta = payload["image_generation"]
        assert meta["ok"] is True
        assert meta["original_count"] == 2
        assert meta["shortfall"] == 3
        assert meta["requested_needed_cuts"] == 5

    def test_detail_urls_match_listing_urls_after_generation(
        self, isolated_prepared_dir, monkeypatch
    ):
        """detail_urls 도 listing_urls 와 같은 순서/개수 (불일치 금지)."""
        monkeypatch.setattr(naver_client, "load_config", lambda: _valid_openai_config())

        d = {
            "name": "상세일치상품",
            "salePrice": 30000,
            "image_sources": ["http://user/a.jpg"],
            "generate_images": True,
            "needed_cuts": 2,
        }
        payload = register.prepare_listing(
            d, attach_fn=_fake_attach_lenient, generate_fn=_ok_generate
        )
        assert (
            payload["images"]["listing_urls"] == payload["images"]["detail_urls"]
        ), "listing_urls 와 detail_urls 가 불일치"


# --------------------------------------------------------------------------- #
# (g) images_ready(sources) 를 인자 없이 부르면 기존 거동 (호환 회귀).
# --------------------------------------------------------------------------- #
class TestCaseGImagesReadyBackwardCompat:
    """(g) ``images_ready(sources)`` 를 ``needed_cuts`` 없이 부르면 기존 거동.

    기존 거동: "유효한 소스가 하나라도 있으면 True".
    신규 거동(needed_cuts 명시): "유효 소스 개수 >= needed_cuts".
    기본값 ``needed_cuts=1`` 은 기존 거동과 동일하다 — 기존 호출부 회귀 없음.
    """

    def test_default_needed_cuts_one_preserves_old_behavior(self):
        # 기존 호출부: 인자 없이 images_ready(sources).
        assert image_gen.images_ready(["http://cdn/a.png"]) is True
        assert image_gen.images_ready(["local/path.jpg"]) is True
        assert image_gen.images_ready([]) is False
        assert image_gen.images_ready(None) is False
        assert image_gen.images_ready(["", "  "]) is False
        assert image_gen.images_ready("not-a-list") is False

    def test_explicit_needed_cuts_changes_semantics(self):
        """명시 needed_cuts 로 새 의미(개수 기반) 가 동작한다."""
        # 1장 있는데 3장 필요 → False (부족).
        assert image_gen.images_ready(["http://cdn/a.png"], needed_cuts=3) is False
        # 3장 있는데 3장 필요 → True (충족).
        assert (
            image_gen.images_ready(
                ["http://cdn/a.png", "http://cdn/b.png", "http://cdn/c.png"],
                needed_cuts=3,
            )
            is True
        )
        # 2장 있는데 1장 필요 → True (과잉 OK).
        assert (
            image_gen.images_ready(["http://cdn/a.png", "http://cdn/b.png"], needed_cuts=1) is True
        )

    def test_needed_cuts_non_positive_returns_true(self):
        """needed_cuts 가 0 이하면 항상 True (필요 컷이 없으니 준비된 것)."""
        assert image_gen.images_ready([], needed_cuts=0) is True
        assert image_gen.images_ready([], needed_cuts=-1) is True

    def test_needed_cuts_garbage_falls_back_to_one(self):
        """needed_cuts 가 정수가 아니면 1로 폴백 (기존 거동 보존)."""
        assert image_gen.images_ready(["x"], needed_cuts="garbage") is True  # type: ignore[arg-type]
        assert image_gen.images_ready([], needed_cuts="garbage") is False  # type: ignore[arg-type]

    def test_prepare_listing_default_needed_cuts_is_one(self, isolated_prepared_dir, monkeypatch):
        """``needed_cuts`` 를 명시하지 않으면 1이어서 원본 1장이면 생성 미진입."""
        generate_calls: list = []

        def fake_generate(*a, **kw):
            generate_calls.append(1)
            return _ok_generate(*a, **kw)

        monkeypatch.setattr(naver_client, "load_config", lambda: _valid_openai_config())

        d = {
            "name": "기본컷수",
            "salePrice": 20000,
            "image_sources": ["http://user/a.jpg"],  # 원본 1장.
            "generate_images": True,
            # needed_cuts 생략 → 기본 1 → 부족분 0 → 생성 0.
        }
        payload = register.prepare_listing(
            d, attach_fn=_fake_attach_lenient, generate_fn=fake_generate
        )
        assert (
            len(generate_calls) == 0
        ), "needed_cuts 생략 시 기본 1인데 원본 1장으로 생성이 호출됨 (회귀)"
        assert "image_generation" not in payload


# --------------------------------------------------------------------------- #
# 부수 — image_gen.generate 직접 단위 테스트 (단가표/비용 메타/플레이스홀더).
# --------------------------------------------------------------------------- #
class TestGenerateUnitCostsAndPlaceholder:
    """``image_gen.generate`` 단위 — 단가표 기반 비용, 플레이스홀더 감지, 오류 번역."""

    def test_openai_generates_and_reports_cost(self):
        session = _RecordingSession(_ok_openai_response())
        result = image_gen.generate(
            "상세 컷",
            needed_cuts=2,
            config=_valid_openai_config(),
            session=session,
        )
        assert result["ok"] is True, f"생성 실패: {result.get('error')}"
        assert result["provider"] == "openai"
        assert result["model"] == "gpt-image-2"
        # 단일 출력 정책: needed_cuts == api_call_count.
        assert result["needed_cuts"] == 2
        assert result["api_call_count"] == 2
        assert result["output_canvas_count"] == 2
        assert result["panel_count_used"] == 2
        # gpt-image-2 = $0.006/캔버스 * 2 = $0.012 (단가표).
        assert result["estimated_cost_usd"] == round(0.006 * 2, 4)
        assert len(session.calls) == 2

    def test_gemini_generates_and_reports_cost(self):
        session = _RecordingSession(_ok_gemini_response())
        result = image_gen.generate(
            "상세 컷",
            needed_cuts=1,
            config=_valid_gemini_config(),
            session=session,
        )
        assert result["ok"] is True
        assert result["provider"] == "gemini"
        assert result["model"] == "gemini-3.1-flash-image"
        assert result["api_call_count"] == 1
        # gemini-3.1-flash-image = $0.101/캔버스 (단가표).
        assert result["estimated_cost_usd"] == 0.101

    def test_generate_returns_error_when_no_key(self):
        result = image_gen.generate("프롬프트", needed_cuts=1, config=_empty_config())
        assert result["ok"] is False, "키 없는데 ok=True (조용한 통과)"
        assert isinstance(result["error"], str) and result["error"]
        assert (
            "api_key" in result["error"].lower() or "키" in result["error"]
        ), f"안내가 키 발급/설정을 언급하지 않음: {result['error']!r}"

    def test_generate_returns_error_when_placeholder_key(self):
        result = image_gen.generate("프롬프트", needed_cuts=1, config=_placeholder_config())
        assert result["ok"] is False
        assert isinstance(result["error"], str) and result["error"]

    def test_generate_zero_calls_when_no_key(self):
        session = _RecordingSession(_ok_openai_response())
        image_gen.generate("프롬프트", needed_cuts=1, config=_empty_config(), session=session)
        assert len(session.calls) == 0, "키 없는데 네트워크 호출 발생"

    def test_generate_empty_response_returns_error(self):
        empty_response = _FakeResponse(200, {"data": []})
        session = _RecordingSession(empty_response)
        result = image_gen.generate(
            "프롬프트", needed_cuts=1, config=_valid_openai_config(), session=session
        )
        assert result["ok"] is False
        assert isinstance(result["error"], str) and result["error"]
        assert result["api_call_count"] == 0
        assert result["estimated_cost_usd"] == 0.0

    def test_generate_http_error_returns_error(self):
        err_response = _FakeResponse(401, {"error": "invalid_api_key"}, "Unauthorized")
        session = _RecordingSession(err_response)
        result = image_gen.generate(
            "프롬프트", needed_cuts=1, config=_valid_openai_config(), session=session
        )
        assert result["ok"] is False
        assert "401" in result["error"] or "Unauthorized" in result["error"]


class TestPickProvider:
    """제공자 선택 정책 (OpenAI 우선 → Gemini 폴백)."""

    def test_pick_provider_prefers_openai(self):
        provider, reason = image_gen.pick_provider(_valid_openai_config())
        assert provider == "openai"
        assert reason == ""

    def test_pick_provider_falls_back_to_gemini(self):
        provider, reason = image_gen.pick_provider(_valid_gemini_config())
        assert provider == "gemini"
        assert reason == ""

    def test_pick_provider_none_when_no_valid(self):
        provider, reason = image_gen.pick_provider(_placeholder_config())
        assert provider is None
        assert reason


class TestPlaceholderDetection:
    """플레이스홀더 값 감지 (``REPLACE_WITH_...``, 빈 값, 중괄호 템플릿)."""

    def test_generation_available_false_for_placeholder(self):
        assert image_gen.generation_available(_placeholder_config()) is False

    def test_generation_available_true_for_real_key(self):
        assert image_gen.generation_available(_valid_openai_config()) is True

    def test_is_placeholder_value_detects_replace_token(self):
        assert image_gen._is_placeholder_value("REPLACE_WITH_OPENAI_API_KEY") is True
        assert image_gen._is_placeholder_value("sk-real-key") is False
        assert image_gen._is_placeholder_value("") is True
        assert image_gen._is_placeholder_value("   ") is True
        assert image_gen._is_placeholder_value(None) is True
        assert image_gen._is_placeholder_value("{STORE_SLUG}") is True

    def test_empty_image_providers_section_is_unset(self):
        assert image_gen.generation_available({"image_providers": {}}) is False

    def test_missing_image_providers_section_is_unset(self):
        assert image_gen.generation_available({}) is False


# --------------------------------------------------------------------------- #
# 부수 — check_config 호환성 (image_generation_configured 키 추가, 기존 보존).
# --------------------------------------------------------------------------- #
class TestCheckConfigImageGenerationCompat:
    """``check_config`` 가 image_generation_configured 를 추가하되 기존 키 보존."""

    def test_check_config_has_image_generation_configured(self, tmp_path):
        cfg = {
            "naver": {
                "client_id": "id",
                "client_secret": "secret",
                "type": "SELF",
                "store_url_slug": "slug",
            },
            "smartstore_notice_defaults": {
                "origin_area_code": "01",
                "origin_content": "한국",
                "as_tel": "070-0000-0000",
            },
            "image_providers": {
                "openai": {"api_key": "sk-real", "model": "gpt-image-2"},
            },
        }
        path = tmp_path / "config.json"
        path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        with mock.patch.object(naver_client, "config_path", return_value=str(path)):
            result = mcp_server.check_config()
        assert "image_generation_configured" in result
        assert result["image_generation_configured"] is True

    def test_check_config_image_generation_unset_reports_hint(self, tmp_path):
        cfg = {
            "naver": {
                "client_id": "id",
                "client_secret": "secret",
                "type": "SELF",
                "store_url_slug": "slug",
            },
            "smartstore_notice_defaults": {
                "origin_area_code": "01",
                "origin_content": "한국",
                "as_tel": "070-0000-0000",
            },
        }
        path = tmp_path / "config.json"
        path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        with mock.patch.object(naver_client, "config_path", return_value=str(path)):
            result = mcp_server.check_config()
        assert result.get("image_generation_configured") is False
        assert "image_generation_hint" in result
        assert isinstance(result["image_generation_hint"], str)
        assert result["image_generation_hint"]

    def test_check_config_preserves_existing_keys(self, tmp_path):
        cfg = {
            "naver": {
                "client_id": "id",
                "client_secret": "secret",
                "type": "SELF",
                "store_url_slug": "slug",
            },
            "smartstore_notice_defaults": {
                "origin_area_code": "01",
                "origin_content": "한국",
                "as_tel": "070-0000-0000",
            },
        }
        path = tmp_path / "config.json"
        path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        with mock.patch.object(naver_client, "config_path", return_value=str(path)):
            result = mcp_server.check_config()
        for key in ("ok", "present", "missing", "as_tel_configured"):
            assert key in result, f"기존 키 {key!r} 가 사라짐 (회귀)"

    def test_check_config_placeholder_treated_as_unset(self, tmp_path):
        cfg = {
            "naver": {
                "client_id": "id",
                "client_secret": "secret",
                "type": "SELF",
                "store_url_slug": "slug",
            },
            "smartstore_notice_defaults": {
                "origin_area_code": "01",
                "origin_content": "한국",
                "as_tel": "070-0000-0000",
            },
            "image_providers": _placeholder_config()["image_providers"],
        }
        path = tmp_path / "config.json"
        path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        with mock.patch.object(naver_client, "config_path", return_value=str(path)):
            result = mcp_server.check_config()
        assert result["image_generation_configured"] is False
