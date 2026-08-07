# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""이미지 생성 모듈 — 단계 분기로 유령 키 해소.

본 모듈은 ``config.image_providers`` 섹션을 **실제로 읽는** 유일한 모듈이다.
``config.example.json`` 에 키가 있었으나 ``src/`` 어디서도 읽지 않아 "유령 키"
가 되었던 것을 해소한다.

단계 분기(계약 — ``register.prepare_listing`` 의 분기와 합의):
  ① 원본 사진이 있는가? → 원본 0장이면 ``images.attach_images`` 게이트가
     ``ValueError`` 로 차단한다. 생성이 이 자리를 대체하지 못한다(불변).
  ② 원본으로 필요 컷 수(needed_cuts) 를 채웠는가? → ``images_ready`` 가
     판정. 채웠으면 생성 경로 미진입(생성 0). 부족하면 ③ 으로.
  ③ 생성을 원하는가(generate_images)? 아니면 진행(생성 0). 예면 ④.
  ④ 생성 API 키가 설정돼 있는가?
       있음 → 자기 키로 **부족분(shortfall)만큼만** 생성 (무료).
       없음 → 안내만(키 발급 절차 + 추후 유료 대행 한 줄). 결제·계정·서버 일절 구현 금지.

안전 규율 (불변):
  - 원본 이미지 0장이면 생성으로 대체할 수 없다 (실재 상품 게이트 유지).
  - 생성 결과는 원본을 대체하지 않는다 (대표이미지 규약·순서 보존).
  - 키가 없으면 생성 함수는 호출되지 않아야 한다. 호출됐다면 명확한 사유로
    거부한다 (조용한 실패 금지).
  - 네트워크 호출은 **이 모듈 안에서만** 일어난다. 다른 모듈은 이 모듈만 부른다.
  - 제공자별 호출은 하나의 인터페이스(``generate``) 뒤로 격리한다.

비용 규율 (``agents/IMAGE_GENERATION_PRICE_POLICY.md`` 준수):
  반환에 ``needed_cuts``·``api_call_count``·``output_canvas_count``·
  ``output_layout``·``panel_count_used``·``estimated_cost_usd`` 를 실어
  몇 호출 했고 예상 비용이 얼마인지 드러낸다.

의존 방향: ``common``·``naver_client`` 만 import 가능. ``image_gen`` 은
``images``/``mcp_server``/``register`` 가 import 해도 된다. **새 의존성 금지** —
HTTP 호출은 이미 프로젝트 의존성인 ``requests`` 만 사용한다 (제공자 SDK 설치 금지).

보안 규율 (추가 — 제공자 응답 본문 정화):
  - OpenAI 는 **잘못된 API 키를 오류 메시지에 담아 돌려주는** 사례가 있다.
    우리가 그 응답 본문을 가공 없이 반환·로그에 싣는 것은 키 유출 경로가
    된다. 제공자 응답 본문을 ``common.sanitize_provider_response`` 로
    정화한 뒤 사유에 싣는다.
  - 정화 규칙은 ``common.sanitize_text`` 의 단일 진실 공급원을 따른다
    (``mcp_server._sanitize_text`` 와 같은 규칙 — 규칙이 두 벌로 갈라지지
    않게 ``common`` 에 둔다). ``image_gen`` 은 ``mcp_server`` 를 import 할
    수 없으므로 ``common`` 에서 가져온다.
"""

from __future__ import annotations

import base64 as _base64
import binascii
import os
import tempfile
from typing import Any

import requests

from . import common, naver_client
from . import images as _images_mod

# --------------------------------------------------------------------------- #
# 상수 — config 스키마 키, 플레이스홀더 토큰, 단가표.
# --------------------------------------------------------------------------- #
# config.image_providers 의 실제 스키마 (config.example.json 에서 실측):
#   {
#     "_comment": str,
#     "openai": {"api_key": str, "model": str},
#     "gemini": {"api_key": str, "model": str}
#   }
# 제공자 객체는 항상 {"api_key": str, "model": str} 형태다. 다른 형태는 거부가
# 아니라 "미설정" 으로 다룬다(알 수 없음을 알 수 없음으로 두지 않되, 호환을
# 깨지도 않는다 — 섹션 자체가 빠진 것과 동일 취급).
_PROVIDER_NAMES: tuple[str, ...] = ("openai", "gemini")

# 플레이스홀더 토큰 — mcp_server._PLACEHOLDER_TOKENS 와 의미가 같다. 본 모듈은
# mcp_server 를 import 하지 않으므로(의존 방향: mcp_server -> image_gen 가능,
# image_gen -> mcp_server 불가) 동일 토큰을 독자적으로 둔다. mcp_server 의
# 토큰 집합이 바뀌면 여기도 같이 바꿔야 한다(단일 진실 공급원 한계 — 주석 명시).
_PLACEHOLDER_TOKENS: tuple[str, ...] = (
    "REPLACE_WITH_",
    "{STORE_SLUG}",
    "{STORE_NAME}",
)

# 단가표(USD 추정) — agents/IMAGE_GENERATION_PRICE_POLICY.md 의 current rate
# table 에서 기본 라우팅에 쓰이는 단일 출력 단가만 발췌. 가격 표본이지 청구서가
# 아니다. 정책 변경 시 이 표와 정책 문서를 같이 바꿔야 한다.
# 키: (provider, model 소문자). 값: 단일 캔버스 당 추정 USD.
_DEFAULT_RATE_TABLE: dict[tuple[str, str], float] = {
    ("openai", "gpt-image-2"): 0.006,  # 1024x1024 low single output
    ("gemini", "gemini-2.5-flash-image"): 0.039,
    ("gemini", "gemini-3.1-flash-image"): 0.101,
    ("gemini", "gemini-3-pro-image"): 0.134,
}
# 모델명이 단가표에 없을 때 쓰는 보수적 기본 단가(정책 문서의 가장 비싼
# 단일-출력 라인 근사). 과소 추정으로 "싼 줄 알고 많이 호출" 하는 것을 막는다.
_FALLBACK_RATE_USD: float = 0.134

# 생성된 이미지 한 장의 바이트 크기 상한. ``images.MAX_IMAGE_BYTES`` 와 같은
# 의미(10MB)지만, 본 모듈은 업로드 게이트의 상수를 존중하되 **방어적으로**
# 디코드 직후에도 한 번 더 자른다 — 거대한 응답이 메모리를 삼키지 않게.
# 상한을 ``images.MAX_IMAGE_BYTES`` 에서 읽어 단일 진실 공급원을 따른다.
_MAX_GENERATED_IMAGE_BYTES: int = _images_mod.MAX_IMAGE_BYTES

# 매직바이트 → 임시 파일 확장자 매핑. ``images.validate_local_image`` 가
# 화이트리스트 확장자(``.jpg/.jpeg/.png/.webp``) 를 요구하므로, 임시 파일에
# 그 의미없는 ``.img`` 확장자를 쓰면 확장자 게이트에서 거부된다. 매직바이트를
# 읽어 실제 포맷에 맞는 확장자를 붙인다 — 위장이 아니라 *내용 기반* 명명이다.
_MAGIC_TO_EXT: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    # WEBP 는 RIFF....WEBP (12바이트) — 헤더 4바이트만 보고 WEBPtrail 검사는
    # _matches_any_image_magic 가 이미 한다. 여기서는 확장자만 매칭.
    (b"RIFF", ".webp"),
)


def _ext_for_magic(head: bytes) -> str:
    """바이트 헤더에서 확장자를 고른다. 모르면 ``.img`` (이후 게이트가 거부)."""
    for magic, ext in _MAGIC_TO_EXT:
        if head.startswith(magic):
            # WEBP 추가 검증: 8..11 위치에 "WEBP" 필요. RIFF 로 시작하는 다른
            # 포맷(WAV 등) 이 webp 확장자를 달지 않게.
            if ext == ".webp":
                if len(head) >= 12 and head[8:12] == b"WEBP":
                    return ".webp"
                continue
            return ext
    return ".img"


# --------------------------------------------------------------------------- #
# Config 판정 헬퍼 — 단일 진실 공급원.
# --------------------------------------------------------------------------- #
def _is_placeholder_value(value: Any) -> bool:
    """값이 플레이스홀더(``REPLACE_WITH_...`` 등) 인지 판별.

    mcp_server._is_placeholder 와 동일한 토큰 기반 판정이다. 본 모듈은
    mcp_server 를 import 하지 않으므로 동일 판정을 독자적으로 둔다.
    """
    if not isinstance(value, str):
        # 비문자열(None/숫자/bool)은 빈 값이 아닌 이상 플레이스홀더가 아니다.
        return value is None
    text = value.strip()
    if not text:
        return True
    return any(token in text for token in _PLACEHOLDER_TOKENS)


def _load_image_providers(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """``config.image_providers`` 섹션을 dict 로 반환.

    Args:
        config: 명시적으로 넘긴 config dict. ``None`` 이면
            :func:`naver_client.load_config` 로 읽는다. 테스트 주입 가능.

    Returns:
        ``image_providers`` dict. 섹션이 없거나 dict 가 아니면 빈 dict.
        로드 실패(파일 부재·JSON 파싱 실패)도 빈 dict 로 떨어진다
        (생성 불가 상태로 취급).
    """
    if config is not None:
        if not isinstance(config, dict):
            return {}
        section = config.get("image_providers")
        return section if isinstance(section, dict) else {}
    try:
        cfg = naver_client.load_config()
    except Exception:
        return {}
    section = cfg.get("image_providers") if isinstance(cfg, dict) else None
    return section if isinstance(section, dict) else {}


def _provider_config(providers: dict[str, Any], name: str) -> tuple[dict[str, Any] | None, str]:
    """단일 제공자 설정을 검증해 반환.

    Returns:
        ``(provider_cfg, reason)``:
          - ``provider_cfg`` 가 ``None`` 이 아니면 유효(키+모델 갖춤).
          - ``None`` 이면 ``reason`` 에 왜 미설정인지 사유를 담는다.
    """
    if not isinstance(providers, dict):
        return None, "image_providers 섹션이 객체가 아닙니다"
    raw = providers.get(name)
    if not isinstance(raw, dict):
        return None, f"image_providers.{name} 섹션이 없거나 객체가 아닙니다"
    api_key = raw.get("api_key")
    if _is_placeholder_value(api_key):
        return None, f"image_providers.{name}.api_key 가 플레이스홀더/빈 값입니다"
    model = raw.get("model")
    if _is_placeholder_value(model) or not str(model or "").strip():
        return None, f"image_providers.{name}.model 이 플레이스홀더/빈 값입니다"
    return {"api_key": str(api_key), "model": str(model)}, ""


# --------------------------------------------------------------------------- #
# 상태 판정 함수 — 결정론적.
# --------------------------------------------------------------------------- #
def images_ready(image_sources: Any, needed_cuts: int = 1) -> bool:
    """필요한 컷 수만큼 원본 이미지가 준비됐는지 판정.

    "준비됐다" = 유효한(비어있지 않은 문자열) 소스 개수가 ``needed_cuts``
    이상이다. ``needed_cuts`` 기본값이 1 이므로 인자 없이 부르던 기존
    호출부의 거동("하나라도 있으면 True") 을 그대로 보존한다(호환 회귀).

    의미 (``register.prepare_listing`` 단계 분기와의 합의):
      - 원본 사진 0장은 본 함수 판정 *이전* 에 ``images.attach_images``
        게이트가 이미 차단한다(원본 게이트 불변). 따라서 본 함수가
        반환하는 값의 역할은 "원본이 있느냐" 가 아니라 **"필요 컷 수를
        원본으로 채웠느냐"** 다 — 채우지 못했으면 그 부족분(shortfall) 만큼
        생성이 메운다. 원본 자리를 생성으로 대체하지는 않는다.

    본 함수는 **판정만** 한다. 이미지의 유효성(존재·매직바이트·확장자)은
    :mod:`images` 모듈의 게이트가 담당한다 — 여기서 검사하지 않는다.
    """
    if not isinstance(image_sources, list):
        return False
    try:
        needed = int(needed_cuts)
    except (TypeError, ValueError):
        needed = 1
    if needed <= 0:
        # 0 컷 이하가 "필요" 라면 원본 유무와 무관하게 항상 준비된 것.
        return True
    valid_count = sum(1 for s in image_sources if isinstance(s, str) and s.strip())
    return valid_count >= needed


def generation_available(config: dict[str, Any] | None = None) -> bool:
    """이미지 생성 키가 설정돼 있는지 판정.

    ``image_providers`` 의 openai/gemini 중 **하나라도** 유효한(api_key + model
    모두 비어있지 않고 플레이스홀더가 아닌) 제공자가 있으면 ``True``.

    플레이스홀더 값(``REPLACE_WITH_...``)은 미설정으로 취급한다.
    """
    providers = _load_image_providers(config)
    if not isinstance(providers, dict) or not providers:
        return False
    for name in _PROVIDER_NAMES:
        cfg, _reason = _provider_config(providers, name)
        if cfg is not None:
            return True
    return False


def pick_provider(config: dict[str, Any] | None = None) -> tuple[str | None, str]:
    """설정된 제공자 중 하나를 선택해 반환.

    정책(``IMAGE_GENERATION_PRICE_POLICY.md`` 의 "Default price-first route"):
      - OpenAI(gpt-image-2 단일 출력) 를 기본 라인으로 선호.
      - OpenAI 가 설정돼 있지 않으면 Gemini.
      - 둘 다 없으면 ``(None, reason)``.

    Returns:
        ``(provider_name, "")`` 또는 ``(None, reason)``.
    """
    providers = _load_image_providers(config)
    # OpenAI 우선.
    oai, oai_reason = _provider_config(providers, "openai")
    if oai is not None:
        return "openai", ""
    gem, gem_reason = _provider_config(providers, "gemini")
    if gem is not None:
        return "gemini", ""
    if oai_reason or gem_reason:
        return (
            None,
            f"설정된 이미지 생성 제공자가 없습니다. openai={oai_reason!r} gemini={gem_reason!r}",
        )
    return None, "image_providers 섹션에 유효한 제공자가 없습니다"


# --------------------------------------------------------------------------- #
# 안내 문구 — 키 없을 때.
# --------------------------------------------------------------------------- #
def key_missing_guidance() -> str:
    """생성 API 키가 없을 때 사용자에게 보여줄 안내 문구를 반환.

    계약:
      - 확인되지 않은 URL 을 만들어 넣지 않는다. 발급처는 제공자 이름과
        "API 키 발급" 수준으로만 안내한다.
      - "추후 유료 대행 예정" 은 한 줄 언급까지만. 가격·조건·시점은 쓰지 않는다.
    """
    return (
        "이미지 생성 API 키가 설정되지 않았습니다. 생성하려면 제공자 API 키를 "
        "발급받아 .local/config.json 의 image_providers.<openai|gemini>.api_key "
        "항목에 넣으세요 (OpenAI API 키 발급 / Google AI Studio Gemini API 키 발급). "
        "키를 넣고 다시 prepare_listing 을 호출하면 생성 경로가 동작합니다. "
        "추후 유료 대행 예정."
    )


# --------------------------------------------------------------------------- #
# 비용 추정.
# --------------------------------------------------------------------------- #
def _estimate_cost_usd(provider: str, model: str, api_call_count: int) -> float:
    """단가표에서 단일 호출 당 단가를 찾아 총 추정 비용(USD) 을 반환.

    정책 문서의 단가표에 없는 모델명이면 보수적 기본 단가를 쓴다(과소 추정 방지).
    """
    if api_call_count <= 0:
        return 0.0
    key = (str(provider or "").lower(), str(model or "").lower())
    rate = _DEFAULT_RATE_TABLE.get(key, _FALLBACK_RATE_USD)
    return round(rate * api_call_count, 4)


# --------------------------------------------------------------------------- #
# 생성 진입점 — 통합 인터페이스.
# --------------------------------------------------------------------------- #
def generate(
    prompt: str,
    *,
    needed_cuts: int = 1,
    provider: str | None = None,
    config: dict[str, Any] | None = None,
    session: Any = None,
    upload_fn: Any = None,
    fetch_fn: Any = None,
) -> dict[str, Any]:
    """이미지 생성 통합 진입점.

    제공자별 HTTP 호출은 모두 이 함수 안에서 일어난다. 다른 모듈은 이 함수만
    부른다. 제공자가 늘거나 우리 서버 대행이 붙어도 **호출부가 안 바뀌게**.

    정책 (``IMAGE_GENERATION_PRICE_POLICY.md`` 의 Default price-first route):
      - ``needed_cuts == 0`` → 생성 0.
      - ``needed_cuts >= 1`` → ``needed_cuts`` 회 단일 출력 호출.
      - 기본 제공자 선택: OpenAI 우선, 없으면 Gemini. 둘 다 없으면 거부.

    Args:
        prompt: 생성 프롬프트(비어있으면 거부).
        needed_cuts: 필요한 최종 컷 수. 양의 정수.
        provider: 강제 제공자 이름(``"openai"``/``"gemini"``). ``None`` 이면
            :func:`pick_provider` 로 자동 선택.
        config: 명시 config(테스트 주입용). ``None`` 이면 파일에서 읽는다.
        session: ``requests.Session`` 대체(테스트 주입용).
        upload_fn: ``images.attach_images`` 의 ``upload_fn`` 으로 다시 넘길 함수
            (로컬 파일 경로 리스트 → CDN URL 리스트). ``None`` 이면
            ``naver_client.upload_images`` 기본값.
        fetch_fn: ``images.attach_images`` 의 ``fetch_fn`` 으로 다시 넘길 함수
            (외부 URL → 임시 파일). ``None`` 이면 ``images.fetch_external_image``.

    Returns:
        결과 dict::

            {
              "ok": bool,
              "provider": str,             # 실제 사용한 제공자
              "model": str,                # 사용한 모델명
              "needed_cuts": int,
              "api_call_count": int,       # 실제 유료 API 호출 수
              "output_canvas_count": int,  # 반환된 캔버스(전체 이미지) 수
              "output_layout": str,        # "single" | ...
              "panel_count_used": int,     # 최종 사용 가능 컷 수
              "estimated_cost_usd": float, # 추정 비용
              "image_urls": [str, ...],    # ★ 네이버 CDN URL 만 담긴다
                                              (base64/data: URL/로컬경로 금지).
                                              생성 이미지도 사용자 사진과 같은
                                              길(바이트 → 임시 파일 → 업로드 →
                                              CDN URL) 을 탄다.
              "error": str | None,         # 실패 사유 (ok=False 일 때)
            }

    계약:
      - **키가 없으면 이 함수는 호출되지 않아야 한다.** 호출됐다면 명확한 사유로
        거부한다(조용한 실패 금지). 본 함수는 그래도 한 번 더 ``generation_available``
        로 확인한다.
      - **네트워크 호출은 이 모듈 안에서만.** ``session`` 주입이 없으면
        ``requests`` 기본 세션을 쓴다. OpenAI 가 ``url`` 을 주면 그 URL 의
        이미지 바이트를 **받아오는** 데에도 같은 ``session`` 을 쓴다(별도 세션
        X). 업로드(네이버 이미지서버)는 ``images.attach_images`` →
        ``naver_client.upload_images`` 경로를 타며, 이 경로는 실호출 테스트에서
        ``upload_fn`` 주입으로 0회로 검증된다.
      - **네이버·OpenAI·Gemini 실호출 금지(테스트).** 모두 ``session``/``upload_fn``
        mock 으로 호출 횟수를 센다. 단 mock 은 **제공자가 실제로 주는 응답 형태**
        (b64_json / url / inlineData) 를 따라야 한다 — 과거 결함의 원인이
        "mock 이 너무 착했다" 는 점을 잊지 말 것.
      - **``image_urls`` 에는 CDN URL 만 담는다.** base64 문자열이나 ``data:`` URL
        이 섞이면 상세 HTML 의 ``<img src>`` 깨짐으로 이어진다. 업로드 실패 시
        해당 컷은 결과에 **들어가지 않는다** — 깨진 이미지가 상품에 들어가는
        것보다 생성이 실패한 게 낫다(조용한 통과 금지).
    """
    result: dict[str, Any] = {
        "ok": False,
        "provider": None,
        "model": None,
        "needed_cuts": int(needed_cuts or 0),
        "api_call_count": 0,
        "output_canvas_count": 0,
        "output_layout": "single",
        "panel_count_used": 0,
        "estimated_cost_usd": 0.0,
        "image_urls": [],
        "error": None,
    }

    # --- 입력 검증 ---
    if not isinstance(prompt, str) or not prompt.strip():
        result["error"] = "generate: prompt 가 비어있지 않은 문자열이어야 합니다."
        return result
    try:
        cuts = int(needed_cuts)
    except (TypeError, ValueError):
        result["error"] = f"generate: needed_cuts 가 정수가 아닙니다: {needed_cuts!r}"
        return result
    if cuts <= 0:
        # 필요 컷 0 — 생성 자체가 의미 없다. ok=True, 호출 0.
        result["ok"] = True
        result["needed_cuts"] = 0
        result["error"] = None
        return result
    result["needed_cuts"] = cuts

    # --- 키 게이트: 키 없으면 명확한 사유로 거부 (조용한 실패 금지) ---
    if not generation_available(config):
        result["error"] = key_missing_guidance()
        return result

    # --- 제공자 선택 ---
    providers_section = _load_image_providers(config)
    if provider is None:
        provider, p_reason = pick_provider(config)
        if provider is None:
            result["error"] = (
                f"generate: 사용 가능한 제공자가 없습니다 — {p_reason}. "
                f"{key_missing_guidance()}"
            )
            return result
    if provider not in _PROVIDER_NAMES:
        result["error"] = (
            f"generate: 알 수 없는 제공자 {provider!r} (허용: {list(_PROVIDER_NAMES)})"
        )
        return result
    prov_cfg, prov_reason = _provider_config(providers_section, provider)
    if prov_cfg is None:
        result["error"] = (
            f"generate: 제공자 {provider!r} 설정이 유효하지 않습니다 — {prov_reason}. "
            f"{key_missing_guidance()}"
        )
        return result

    model = prov_cfg["model"]
    api_key = prov_cfg["api_key"]
    result["provider"] = provider
    result["model"] = model

    # --- 제공자별 HTTP 호출 (이 모듈 안에 격리) ---
    # ★ 제공자는 **raw bytes** 만 반환한다. URL/base64 여부는 이 모듈의
    # ``_normalize_to_bytes`` 가 통일적으로 처리한다 — 과거 결함의 핵심은
    # ``url`` 과 ``b64_json`` 을 같은 것으로 취급해 base64 덩어리가 그대로
    # 흘러간 것이었다. 이제 바이트까지 환원한 뒤 임시 파일로 저장하고
    # ``images.attach_images`` 로 **사용자 사진과 같은 길** 을 탄다.
    own_session = session is None
    if own_session:
        session = requests.Session()
    try:
        try:
            if provider == "openai":
                image_bytes_list, call_reason = _call_openai(session, api_key, model, prompt, cuts)
            elif provider == "gemini":
                image_bytes_list, call_reason = _call_gemini(session, api_key, model, prompt, cuts)
            else:  # 방어 — 위에서 걸렀으나 도달 가능성 유지
                result["error"] = f"generate: 지원하지 않는 제공자: {provider!r}"
                return result
        except requests.RequestException as exc:
            # 네트워크 예외 — 조용히 통과하지 않고 사유 반환. 예외 텍스트에
            # 응답 본문이 섞일 수 있으므로 정화한다(예: HTTPError 가 응답을 담음).
            result["error"] = (
                f"generate: {provider} 호출 중 네트워크 오류: {common.sanitize_error(exc)}"
            )
            return result
        except Exception as exc:  # 방어 — 사유를 반환에 담는다 (조용한 통과 금지).
            result["error"] = f"generate: {provider} 호출 중 예외: {common.sanitize_error(exc)}"
            return result
    finally:
        if own_session:
            try:
                session.close()
            except Exception:
                pass

    if not image_bytes_list:
        result["error"] = (
            f"generate: {provider} 호출이 이미지를 반환하지 않았습니다 — {call_reason}"
        )
        return result

    api_call_count = cuts  # 단일 출력 정책: needed_cuts == api_call_count

    # --- bytes → 임시 파일 → CDN URL (사용자 사진과 같은 길 재사용) ---
    # ★ ``images.attach_images`` 가 로컬 경로 → 네이버 CDN URL 로 만드는
    # 단일 진실 공급원이다. 업로드 로직을 새로 쓰지 않는다. 생성 이미지의
    # 임시 파일을 ``attach_images`` 에 넘기면 그것이 동일한 검증(매직바이트·
    # 크기 상한·컨테인먼트) 과 동일한 업로드 경로를 통과한다. 순서 보존은
    # ``attach_images`` 가 입력 순서를 유지하는 계약에 맡긴다.
    cdn_urls, upload_reason = _generated_bytes_to_cdn_urls(
        image_bytes_list, upload_fn=upload_fn, fetch_fn=fetch_fn
    )
    if not cdn_urls:
        # 업로드가 한 건도 안 됐다 — 깨진 이미지가 상품에 들어가는 것보다
        # 생성이 실패한 게 낫다. ok=False 로 사유를 명확히 전달(조용한 통과 금지).
        result["api_call_count"] = api_call_count
        result["output_canvas_count"] = len(image_bytes_list)
        result["estimated_cost_usd"] = _estimate_cost_usd(provider, model, api_call_count)
        result["error"] = (
            f"generate: 생성된 이미지를 CDN 에 업로드하지 못했습니다 — {upload_reason}. "
            "깨진 이미지가 상품에 들어가는 것을 막기 위해 생성분을 결과에 넣지 않습니다."
        )
        return result

    result["ok"] = True
    result["api_call_count"] = api_call_count
    result["output_canvas_count"] = len(image_bytes_list)
    result["panel_count_used"] = len(cdn_urls)
    result["estimated_cost_usd"] = _estimate_cost_usd(provider, model, api_call_count)
    result["image_urls"] = list(cdn_urls)
    result["error"] = None
    return result


# --------------------------------------------------------------------------- #
# 제공자 응답 → raw bytes 통일 헬퍼.
#
# ★ 과거 결함의 뿌리: ``_call_openai`` 가 ``url`` 과 ``b64_json`` 을 같은
# 문자열로 취급해 ``image_urls`` 에 그대로 담았다. ``_call_gemini`` 는 항상
# base64 만 주기 때문에 이 경로에서는 URL 이 나올 수가 없었다 — 즉 실호출하면
# 반드시 깨졌다. mock 이 URL 을 지어줘서 이음매가 한 번도 안 밟혔다.
#
# 이제 제공자 응답은 모두 raw ``bytes`` 로 환원한다:
#   - OpenAI ``url``   → ``session.get(url)`` 로 바이트 수신.
#   - OpenAI ``b64_json`` → ``base64.b64decode``.
#   - Gemini ``inlineData.data`` → ``base64.b64decode`` (항상 base64).
# 바이트는 매직바이트 검증 + 크기 상한을 거쳐 임시 파일로 저장되고,
# ``_generated_bytes_to_cdn_urls`` 가 ``images.attach_images`` 에 넘긴다.
# --------------------------------------------------------------------------- #
def _decode_b64_to_bytes(value: str, *, source: str) -> tuple[bytes, str]:
    """base64 문자열을 raw bytes 로 디코드.

    Args:
        value: base64 로 인코딩된 문자열.
        source: 오류 메시지에 실을 출처 키명(``"b64_json"``/``"inlineData.data"``).

    Returns:
        ``(decoded_bytes, reason)`` — ``reason`` 이 빈 문자열이면 OK.
        디코드 실패·크기 초과면 ``(b"", reason)``.
    """
    if not isinstance(value, str) or not value.strip():
        return b"", f"{source} 값이 비어있습니다"
    raw = value.strip()
    # ``data:image/png;base64,....`` 형태의 data URL 도 허용 — 과거 gemini 경로가
    # 이 접두사를 붙여 담았다. 호환을 위해 접두사는 잘라낸다.
    if raw.startswith("data:") and ";base64," in raw:
        raw = raw.split(";base64,", 1)[1]
    try:
        decoded = _base64.b64decode(raw, validate=False)
    except (binascii.Error, ValueError) as exc:
        return b"", f"{source} base64 디코드 실패: {exc}"
    if not decoded:
        return b"", f"{source} 디코드 결과가 빈 바이트"
    if len(decoded) > _MAX_GENERATED_IMAGE_BYTES:
        return b"", (
            f"{source} 디코드 결과 크기 초과({len(decoded)} > " f"{_MAX_GENERATED_IMAGE_BYTES})"
        )
    return decoded, ""


def _fetch_url_bytes(session: Any, url: str) -> tuple[bytes, str]:
    """OpenAI ``url`` 을 받아 raw bytes 로 수신.

    SSRF 차단은 ``images.fetch_external_image`` 가 담당하지만, OpenAI 응답의
    ``url`` 은 제공자가 준 값이므로 여기서는 같은 ``session`` 으로 받아온다.
    허용 호스트 게이트는 적용하지 않는다(제공자가 준 URL 이 곧 그 제공자의
    CDN 이기 때문). 대신 크기 상한과 매직바이트는 이 모듈에서 1차 방어로 검사.

    Returns:
        ``(bytes, reason)`` — ``reason`` 이 빈 문자열이면 OK.
    """
    if not isinstance(url, str) or not url.strip():
        return b"", "url 값이 비어있습니다"
    try:
        resp = session.get(url, timeout=60)
    except requests.RequestException as exc:
        return b"", f"url 이미지 수신 실패: {common.sanitize_error(exc)}"
    status = getattr(resp, "status_code", 0)
    if not (200 <= status < 300):
        return b"", f"url 이미지 수신 HTTP {status}"
    content = getattr(resp, "content", b"")
    if isinstance(content, str):
        content = content.encode("utf-8", "replace")
    if not isinstance(content, bytes | bytearray):
        return b"", f"url 이미지 수신 — content 타입이 bytes 가 아님: {type(content).__name__}"
    if not content:
        return b"", "url 이미지 수신 — 빈 본문"
    if len(content) > _MAX_GENERATED_IMAGE_BYTES:
        return b"", (f"url 이미지 수신 크기 초과({len(content)} > {_MAX_GENERATED_IMAGE_BYTES})")
    return bytes(content), ""


def _normalize_to_bytes(session: Any, item: dict[str, Any]) -> tuple[bytes, str]:
    """OpenAI 응답 항목 하나를 raw bytes 로 통일.

    ``url`` 이 오면 그 URL 을 받아 바이트로. ``b64_json`` 이 오면 디코드해
    바이트로. 둘 다 같은 ``bytes`` 타입으로 환원된다 — 이 함수가 바로 과거
    결함(둘을 같은 문자열로 취급) 의 뿌리를 자르는 지점이다.

    Returns:
        ``(bytes, reason)`` — ``reason`` 이 빈 문자열이면 OK.
    """
    if not isinstance(item, dict):
        return b"", "응답 항목이 dict 가 아님"
    url_val = item.get("url")
    b64_val = item.get("b64_json")
    # url 이 오면 그 URL 을 받아 바이트로 (우선).
    if isinstance(url_val, str) and url_val.strip():
        return _fetch_url_bytes(session, url_val)
    # b64_json 이 오면 디코드해 바이트로.
    if isinstance(b64_val, str) and b64_val.strip():
        return _decode_b64_to_bytes(b64_val, source="b64_json")
    return b"", "응답 항목에 url/b64_json 이 없습니다"


# --------------------------------------------------------------------------- #
# bytes → 임시 파일 → CDN URL (사용자 사진과 같은 길 재사용).
# --------------------------------------------------------------------------- #
def _generated_bytes_to_cdn_urls(
    image_bytes_list: list[bytes],
    *,
    upload_fn: Any = None,
    fetch_fn: Any = None,
) -> tuple[list[str], str]:
    """생성된 이미지 바이트 리스트를 네이버 CDN URL 리스트로 변환.

    ★ 업로드 로직을 새로 쓰지 않는다 — ``images.attach_images`` 를 부른다.
    사용자 사진(로컬 경로) 이 지나는 **동일한 검증·동일한 업로드 경로** 를
    생성 이미지도 지난다.

    흐름:
      1. 각 바이트를 매직바이트로 1차 검증(이미지가 아닌 응답 거부).
      2. 바이트를 임시 파일로 저장(``tempfile.mkstemp``).
      3. 임시 파일 경로 리스트를 ``images.attach_images`` 에 넘긴다.
         ``attach_images`` 는 자체적으로 매직바이트·크기·컨테인먼트 검증과
         ``upload_fn``(기본 ``naver_client.upload_images``) 경로를 수행한다.
      4. ``rejected`` 가 비어있지 않으면 fail-closed — 부분 성공이라도 그
         사실을 사유에 담는다(조용한 통과 금지).
      5. 임시 파일은 ``try/finally`` 로 항상 정리.

    Returns:
        ``(cdn_urls, reason)`` — ``reason`` 이 빈 문자열이면 OK. ``cdn_urls``
        가 비어있으면 업로드가 한 건도 안 된 것이다.
    """
    if not image_bytes_list:
        return [], "업로드할 생성 이미지가 없습니다"

    # 1. 매직바이트 1차 검증 — images._matches_any_image_magic 재사용.
    temp_paths: list[str] = []
    rejected_items: list[str] = []
    for idx, raw in enumerate(image_bytes_list):
        if not isinstance(raw, bytes | bytearray) or not raw:
            rejected_items.append(f"[{idx}] 빈 바이트/비바이트 타입")
            continue
        if not _images_mod._matches_any_image_magic(bytes(raw[:16])):
            rejected_items.append(f"[{idx}] 매직바이트 불일치 — 이미지가 아님")
            continue
        if len(raw) > _MAX_GENERATED_IMAGE_BYTES:
            rejected_items.append(f"[{idx}] 크기 초과({len(raw)} > {_MAX_GENERATED_IMAGE_BYTES})")
            continue
        # 2. 임시 파일로 저장. ★ 매직바이트 기반 확장자 — images.validate_local_image
        # 가 ``ALLOWED_IMAGE_EXTS`` (.jpg/.jpeg/.png/.webp) 화이트리스트를 검사하므로,
        # 임의의 ``.img`` 확장자는 거부된다. 매직바이트에서 실제 포맷을 읽어 붙인다.
        ext = _ext_for_magic(bytes(raw[:16]))
        fd, tmp_path = tempfile.mkstemp(prefix="clossify_gen_", suffix=ext)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(bytes(raw))
        except OSError as exc:
            # 임시 파일 생성 실패 — 정리 후 거부 사유 누적.
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass
            rejected_items.append(f"[{idx}] 임시 파일 저장 실패: {exc}")
            continue
        temp_paths.append(tmp_path)

    try:
        if not temp_paths:
            return [], "유효한 생성 이미지 바이트가 없습니다 — " + "; ".join(rejected_items)

        # 3. attach_images 에 임시 파일 경로 리스트 전달.
        #    ``require_image_ext=False`` 경로(임시 파일의 .img 확장자)를 타게
        #    하기 위해 ``validate_local_image`` 가 확장자 검사를 건너뛰도록
        #    ``attach_images`` 자체는 그대로 부른다 — .img 확장자는
        #    ``validate_local_image`` 가 매직바이트 기반으로 통과시킨다.
        attach_result = _images_mod.attach_images(
            list(temp_paths), upload_fn=upload_fn, fetch_fn=fetch_fn
        )
        cdn_urls = list(attach_result.get("urls") or [])
        rejected = list(attach_result.get("rejected") or [])
        if rejected:
            # 거부 항목이 있으면 이유를 합친다. 단 CDn_urls 가 부분이라도 있으면
            # 그것을 반환하되 사유를 함께 남긴다(fail-closed 는 호출자가 판정).
            rej_summary = "; ".join(f"[{r.get('index')}] {r.get('reason')}" for r in rejected[:5])
            if not cdn_urls:
                return [], f"attach_images 전체 거부 — {rej_summary}"
            return cdn_urls, f"attach_images 부분 거부 있음 — {rej_summary}"
        if len(cdn_urls) != len(temp_paths):
            return cdn_urls, (
                f"attach_images URL 개수 불일치 (입력 {len(temp_paths)} vs "
                f"반환 {len(cdn_urls)})"
            )
        if rejected_items:
            # 매직바이트/크기 단계에서 거부된 항목이 있었다면 그것도 사유에 담는다.
            return cdn_urls, "일부 생성 이미지 사전 검증 거부 — " + "; ".join(rejected_items)
        return cdn_urls, ""
    except Exception as exc:
        # attach_images 자체가 예외를 던지면(예: upload_fn 장애) 조용한 통과 금지.
        return [], f"attach_images 예외: {common.sanitize_error(exc)}"
    finally:
        # 5. 임시 파일은 항상 정리(작업 성공 여부와 무관).
        for tmp in temp_paths:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# 제공자 어댑터 — HTTP 호출 (이 모듈 안에만 존재).
#
# ★ 이 어댑터들은 이제 **raw bytes** 만 반환한다. URL/base64 여부는
# ``_normalize_to_bytes`` 가 통일 처리한다. base64 덩어리가 그대로 흐르던
# 과거 결함의 뿌리를 자른다.
# --------------------------------------------------------------------------- #
def _call_openai(
    session: Any, api_key: str, model: str, prompt: str, cuts: int
) -> tuple[list[bytes], str]:
    """OpenAI 이미지 생성 API 호출 → raw bytes 리스트.

    Returns:
        ``(image_bytes_list, reason)`` — ``image_bytes_list`` 가 비어있으면
        ``reason`` 에 사유. 각 항목은 ``bytes`` (URL 을 받아 바이트로, 또는
        b64_json 을 디코드해 바이트로). 이 함수는 **문자열 URL/base64 를
        그대로 반환하지 않는다** — 과거 결함의 핵심이 바로 그것이었다.
    """
    # OpenAI Images API (단일 출력 n=1 을 cuts 회). 정책상 단일 출력 선호.
    url = "https://api.openai.com/v1/images/generations"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    image_bytes_list: list[bytes] = []
    for _ in range(cuts):
        body = {"model": model, "prompt": prompt, "n": 1, "size": "1024x1024"}
        resp = session.post(url, headers=headers, json=body, timeout=60)
        # 상태 코드는 호출부에서 RequestException 로 번역되지 않으므로 여기서 검사.
        status = getattr(resp, "status_code", 0)
        if not (200 <= status < 300):
            text = ""
            try:
                text = resp.text if hasattr(resp, "text") else str(resp.content)
            except Exception:
                pass
            # 제공자 응답 본문을 가공 없이 싣지 않는다 — OpenAI 는 잘못된 키를
            # 오류 메시지에 담아 돌려주는 사례가 있어 키 유출 경로가 된다.
            # 사유(HTTP 상태·메시지 골격)는 남기고, 값만 정화한다.
            return [], f"HTTP {status}: {common.sanitize_provider_response(text[:200])}"
        data = {}
        try:
            data = resp.json() if hasattr(resp, "json") else {}
        except Exception as exc:
            return [], f"응답 JSON 파싱 실패: {exc}"
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list) or not items:
            return [], "응답에 data 배열이 없습니다"
        first = items[0] if isinstance(items[0], dict) else {}
        # ★ url 과 b64_json 을 같은 문자열로 취급하지 않는다.
        # 둘 다 raw bytes 로 환원한다 (_normalize_to_bytes).
        img_bytes, norm_reason = _normalize_to_bytes(session, first)
        if norm_reason or not img_bytes:
            return [], norm_reason or "url/b64_json 을 바이트로 환원하지 못했습니다"
        image_bytes_list.append(img_bytes)
    return image_bytes_list, ""


def _call_gemini(
    session: Any, api_key: str, model: str, prompt: str, cuts: int
) -> tuple[list[bytes], str]:
    """Google Gemini 이미지 생성 API 호출 → raw bytes 리스트.

    Gemini 는 쿼리 파라미터로 키를 받는다. 단일 출력 정책: cuts 회 호출.
    Gemini 응답의 ``candidates[0].content.parts[*].inlineData.data`` 는
    **항상 base64** 다 — 즉 URL 은 나올 수가 없다. 과거 결함의 "제미나이는
    더 확정적" 지적이 바로 이것이다.

    Returns:
        ``(image_bytes_list, reason)`` — 각 항목은 ``bytes`` (base64 디코드 결과).
    """
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    image_bytes_list: list[bytes] = []
    for _ in range(cuts):
        params = {"key": api_key}
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]},
        }
        resp = session.post(endpoint, params=params, json=body, timeout=60)
        status = getattr(resp, "status_code", 0)
        if not (200 <= status < 300):
            text = ""
            try:
                text = resp.text if hasattr(resp, "text") else str(resp.content)
            except Exception:
                pass
            # 제공자 응답 본문 정화 — 키 유출 경로 차단. 사유는 남긴다.
            return [], f"HTTP {status}: {common.sanitize_provider_response(text[:200])}"
        data = {}
        try:
            data = resp.json() if hasattr(resp, "json") else {}
        except Exception as exc:
            return [], f"응답 JSON 파싱 실패: {exc}"
        # Gemini 응답: candidates[0].content.parts[*].inlineData.data (base64)
        candidates = data.get("candidates") if isinstance(data, dict) else None
        if not isinstance(candidates, list) or not candidates:
            return [], "응답에 candidates 배열이 없습니다"
        first_cand = candidates[0] if isinstance(candidates[0], dict) else {}
        content = first_cand.get("content") if isinstance(first_cand, dict) else None
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list) or not parts:
            return [], "응답에 content.parts 가 없습니다"
        img_data = ""
        for part in parts:
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline, dict) and str(inline.get("data") or "").strip():
                img_data = str(inline["data"])
                break
        if not img_data:
            return [], "응답 parts 에 inlineData.data(base64) 가 없습니다"
        # ★ base64 디코드 → raw bytes. data: URL 접두사는 _decode_b64_to_bytes
        # 가 잘라낸다(과거 ``f"data:image/png;base64,{img_data}"`` 형태 호환).
        img_bytes, decode_reason = _decode_b64_to_bytes(img_data, source="inlineData.data")
        if decode_reason or not img_bytes:
            return [], decode_reason or "inlineData.data 디코드 결과가 빈 바이트"
        image_bytes_list.append(img_bytes)
    return image_bytes_list, ""


__all__ = [
    "generate",
    "generation_available",
    "images_ready",
    "key_missing_guidance",
    "pick_provider",
]
