# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""Shared constants and JSON IO helpers.

Ported from the original sourcing pipeline as the DAG root module.
Symbols whose source values contain forbidden tokens (e.g. the as_tel
literal) are resolved at runtime from config and raise ``ValueError``
when the config key is absent (fail-closed). No ``NotImplementedError``
stubs live in this module.
"""

from __future__ import annotations

import json
import os
import re
from importlib.resources import files as _ir_files
from pathlib import Path

# ---------------------------------------------------------------------------
# Path resolution (install-paths 재배치).
#
# Two distinct categories of files exist and must not be conflated:
#
#   (1) Package data — read-only assets shipped inside the wheel
#       (``data/*.json``, ``agents/*.md``). These are resolved through
#       ``importlib.resources`` so the same code works in the source tree,
#       an editable install, and a wheel install. No ``__file__``-based
#       "repo root" estimation is used.
#
#   (2) User-space configuration and working state — files the operator
#       owns and mutates (``config.json``, ``prepared/``, logs, caches).
#       These live under ``CLOSSIFY_STATE_DIR`` if set, otherwise
#       ``<cwd>/.local/``. ``CLOSSIFY_CONFIG`` overrides the config file
#       location specifically (see ``naver_client.resolve_config_path``).
# ---------------------------------------------------------------------------
_PKG_DIR: Path = Path(str(_ir_files("clossify")))
DATA_DIR: Path = _PKG_DIR / "data"


def package_data_path(filename: str) -> Path:
    """Return the path to a packaged data file under ``data/``.

    Uses ``importlib.resources`` so the path resolves correctly in the
    source tree, in editable installs, and in wheel installs. Callers
    that read this path should still handle the file-not-found case by
    raising a clear error rather than silently returning empty data.
    """
    return DATA_DIR / str(filename)


def _state_dir() -> Path:
    """Return the working-state directory root.

    Honours ``CLOSSIFY_STATE_DIR`` (absolute path override). Defaults to
    ``<cwd>/.local`` so each working directory has its own state, which
    matches how operators actually run the server (one store per folder).
    """
    override = os.environ.get("CLOSSIFY_STATE_DIR")
    if override and override.strip():
        return Path(override.strip()).expanduser().resolve()
    return Path.cwd() / ".local"


# Package data locations (read-only).
AGENTS_DIR: Path = _PKG_DIR / "agents"

# User-space state locations (read/write, operator-owned).
STATE_DIR: Path = _state_dir()
# Back-compat alias: legacy code referred to the project-root local dir.
# LOCAL_DIR is the modern name for the same directory. Do not introduce new
# uses of LOCAL_DIR — prefer STATE_DIR.
LOCAL_DIR: Path = STATE_DIR
AGENT_LOG_PATH: Path = STATE_DIR / "agent_log.json"
AGENT_LOG_LIMIT: int = 200
CATEGORY_TRACE_LOG_PATH: Path = STATE_DIR / "category_trace.jsonl"
TRAIN_LOG_PATH: Path = STATE_DIR / "train_log.jsonl"
PREPARED_DIR: Path = STATE_DIR / "prepared"
PREPARED_PAYLOAD_VERSION: int = 1
LLM_TMP_DIR: Path = STATE_DIR / "llm_tmp"
KW_CACHE_PATH: Path = STATE_DIR / "kw_cache.json"
POSTTEAM_RATES_PATH: Path = STATE_DIR / "postteam_rates.json"

SEO_MIN_SEARCH_VOLUME: int = 10
VISION_QA_MAX_SIDE: int = 1568
NAVER_OPTION_PRICE_DELTA_LIMIT_KRW: int = 500000


def cfg():
    """Return the loaded config dict.

    Delegates to :mod:`clossify.naver_client` to keep a single source of
    truth for the config path resolution.
    """
    from . import naver_client as nc

    return nc.load_config()


# ---------------------------------------------------------------------------
# Config section accessor. Used by live accessors below
# (DEFAULT_AS_TEL). The ported layer that read "upstream" and "llm"
# sections was removed — those lanes are not part of this
# product (text inference is owned by the MCP client).
# ---------------------------------------------------------------------------


def _cfg_section(name: str) -> dict:
    """Return ``cfg()[name]`` if it is a dict, else ``{}``."""
    try:
        section = cfg().get(name)
    except Exception:
        return {}
    return section if isinstance(section, dict) else {}


def DEFAULT_AS_TEL() -> str:
    """Default AS telephone literal.

    Resolved from ``cfg()["brand"]["as_tel"]``; raises ``ValueError`` when
    absent (the literal phone number is a forbidden token).
    """
    tel = str(_cfg_section("brand").get("as_tel") or "").strip()
    if not tel:
        raise ValueError("brand.as_tel is not configured")
    return tel


# ---------------------------------------------------------------------------
# JSON IO helpers. Pure stdlib, no forbidden content.
# ---------------------------------------------------------------------------


def _read_json_file(path, default):
    """Read JSON from ``path``; return ``default`` on any failure."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def _write_json_file(path, data):
    """Atomically write ``data`` as UTF-8 JSON to ``path``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Config coercion helpers.
# ---------------------------------------------------------------------------


def _bool_config(value, default=False):
    """Coerce ``value`` to bool using the sourcing.py truthy/falsy tables."""
    if value is None:
        return default
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("1", "true", "yes", "y", "on"):
            return True
        if s in ("0", "false", "no", "n", "off", "none"):
            return False
        return default
    return bool(value)


def _int_config(value, default):
    """Coerce ``value`` to int; return ``default`` on failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default=0.0):
    """Coerce ``value`` to float; return ``default`` on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# llm_hint descriptor contract.
#
# The MCP host (the external LLM client driving this MCP server) performs
# all real LLM work. Functions that used to call an LLM CLI now return an
# ``llm_hint`` descriptor dict instead. The host inspects the dict, executes
# the LLM call with its own configured provider/model, and passes the
# parsed result back to the caller (typically via a second MCP tool call).
#
# Descriptor shape:
#     {"needs_llm": True,
#      "task": "<short task id>",
#      "input": {...},             # structured arguments for the host LLM
#      "instruction": "<prompt>"}  # prompt body (from packaged agents/*.md)
# ---------------------------------------------------------------------------


class LLMGenerateError(RuntimeError):
    """Raised when an llm_hint descriptor cannot be constructed.

    This is **not** a provider failure — the host LLM never ran. It
    indicates a programming error (bad input shape, missing required
    field) in the caller.
    """


def _llm_hint(task, *, input, instruction):
    """Build a canonical ``llm_hint`` descriptor for the MCP host.

    Args:
        task: short, stable task id (e.g. ``"translate_title"``,
            ``"naming_agent"``). The MCP host keys on this.
        input: structured arguments the host LLM consumes. Must be a
            JSON-serialisable dict.
        instruction: the prompt body, typically sourced from a packaged
            ``agents/*.md`` asset.

    Returns:
        ``{"needs_llm": True, "task": task, "input": input,
        "instruction": instruction}``.

    Raises:
        LLMGenerateError: when ``task``/``instruction`` are empty or
            ``input`` is not a dict.
    """
    if not str(task or "").strip():
        raise LLMGenerateError("llm_hint.task must be a non-empty string")
    if not isinstance(input, dict):
        raise LLMGenerateError("llm_hint.input must be a dict")
    if not str(instruction or "").strip():
        raise LLMGenerateError("llm_hint.instruction must be non-empty")
    return {
        "needs_llm": True,
        "task": str(task),
        "input": input,
        "instruction": str(instruction),
    }


def _resolve_instruction(*candidates):
    """Return the first non-empty instruction string from ``candidates``.

    Each candidate may be a string, a callable returning a string, or
    ``None``. Callables are invoked lazily so an asset read only happens
    when needed.
    """
    for candidate in candidates:
        if candidate is None:
            continue
        if callable(candidate):
            try:
                candidate = candidate()
            except Exception:
                continue
        text = str(candidate or "").strip()
        if text:
            return text
    return ""


# ---------------------------------------------------------------------------
# Secret/text sanitization — single source of truth.
#
# 이 모듈(``common``)은 DAG 의 루트로, 모든 모듈이 import 할 수 있다.
# 따라서 민감 정보 정화 규칙을 **여기에 단일 위치**로 둔다. 과거에는
# ``mcp_server._sanitize_text``/``_SENSITIVE_PATTERNS`` 가 유일했으나,
# ``image_gen`` 등 ``mcp_server`` 를 import 할 수 없는 모듈에서도 같은
# 규칙이 필요해지면서 규칙이 두 벌로 갈라질 위험이 있었다. 이제
# ``common`` 이 단일 진실 공급원이고, ``mcp_server`` 는 이곳에서 재노출
# 해 기존 호출부를 유지한다.
#
# 정화 정책(불변):
#   - **값만 가린다.** 사유(예외 타입·HTTP 상태·오류 코드)는 남긴다.
#   - **설정된 키 값만 가리는 것으로는 부족하다** — 사용자가 방금 오타
#     낸 키 값도 오류 응답 본문에 실려 올 수 있으므로, 키 *형태* 를
#     패턴으로 가린다(``sk-...``·긴 base64/hex·``Bearer ...``).
#   - **조용한 실패로 바꾸지 않는다.** 정화 후에도 "무엇이 잘못됐는지" 는
#     반환·로그에 보이게 둔다.
# ---------------------------------------------------------------------------

# traceback/에러 메시지에서 제거해야 할 민감 패턴.
SENSITIVE_PATTERNS: list[re.Pattern[str]] = [
    # OpenAI 스타일 키 (sk- 접두사). 응답 본문에 실린 "잘못된 키" 도 가림.
    re.compile(r"(sk-[A-Za-z0-9_\-]{8,})", re.IGNORECASE),
    # Google API 키 스타일 (AIza 접두사).
    re.compile(r"(AIza[A-Za-z0-9_\-]{8,})", re.IGNORECASE),
    # Bearer 토큰.
    re.compile(r"(Bearer\s+[A-Za-z0-9_\-\.]+)", re.IGNORECASE),
    # key=value 형태의 시크릿 (api_key=..., client_secret: ..., token=..., 등)
    # 콜론(:) 또는 등호(=) 구분자 모두 매칭. 값 부분은 4자까지만 노출(표식용).
    re.compile(
        r"((?:api[_\-]?key|client[_\-]?secret|access[_\-]?token|auth[_\-]?token|"
        r"secret[_\-]?key|password|passwd|pwd|credential|private[_\-]?key|"
        r"token|secret|apikey)"
        r"\s*[:=]\s*)([^\s\"'<>,;]{5,})",
        re.IGNORECASE,
    ),
    # 긴 base64/hex 시크릿 (32자 이상, base64 알파벳). 이미지 데이터가 아닌
    # "키처럼 생긴" 긴 토큰을 가린다. data: URL (이미지) 은 슬래시/쉼표를
    # 포함하므로 이 패턴에 안 걸린다.
    re.compile(r"(?<![A-Za-z0-9+/])([A-Za-z0-9+/]{40,}={0,2})(?![A-Za-z0-9+/])"),
    # 긴 hex 토큰 (32자 이상 16진수). OAuth client_secret 등.
    re.compile(r"\b([0-9a-fA-F]{32,})\b"),
    # Windows 파일시스템 경로 전체 (드라이브 문자 포함).
    # 파이썬 예외 메시지는 경로를 repr 형태(역슬래시 이중)로 담아 내보내므로,
    # 단일 구분자와 이중 구분자 모두 매칭해야 한다. 아래 정규식의
    # `[\\/][\\/]?` 부분이 1~2개의 연속된 역슬래시/슬래시를 커버한다.
    re.compile(
        r"([A-Za-z]:[\\/][\\/]?"
        r"(?:Users|home|private|secret|config|\.local|Desktop|Documents)[\\/][\\/]?)"
        r"[^\"'<>\s]+",
        re.IGNORECASE,
    ),
    # POSIX 시스템/사용자 디렉토리 경로. 슬래시 단일/이중 모두 커버.
    re.compile(
        r"(/[/]?(?:home|Users|etc|var|root|tmp|opt|srv|private|secret)/[^\"'<>\s]+)",
        re.IGNORECASE,
    ),
    # traceback 헤더 및 File 프레임.
    re.compile(r"Traceback\s*\(most\s+recent\s+call\s+last\)", re.IGNORECASE),
    re.compile(r'(File\s+"[^"]+",\s*line\s+\d+[^\n]*)', re.IGNORECASE),
]


def sanitize_text(text: str) -> str:
    """traceback/메시지에서 민감 정보(시크릿, 사용자 경로 등)를 마스킹한다.

    단일 진실 공급원: 모든 모듈(``mcp_server``·``image_gen``·기타)은 이
    함수를 쓴다. 규칙이 두 벌로 갈라지지 않게 한다.

    정책:
      - **값만 가린다.** 사유(예외 타입·HTTP 상태·오류 코드)는 남긴다.
      - 조용한 실패로 바꾸지 않는다 — 호출자가 사유를 결과에 담아야 한다.
    """
    if not isinstance(text, str):
        text = str(text)
    for pat in SENSITIVE_PATTERNS:
        if pat.groups >= 2:
            # key=value 패턴: 키 이름은 유지, 값만 [REDACTED].
            text = pat.sub(lambda m: m.group(1) + "[REDACTED]", text)
        else:
            text = pat.sub("[REDACTED]", text)
    return text


def sanitize_error(exc: BaseException) -> str:
    """예외 객체로부터 타입+메시지를 추출해 정화된 문자열을 반환한다.

    traceback 전체를 반환/로그에 노출하면 민감한 경로/키가 노출될 수
    있으므로 예외 타입명과 메시지만 간결하게. 사유(예외 타입명)는 남긴다.
    """
    type_name = type(exc).__name__
    msg = sanitize_text(str(exc))
    return f"{type_name}: {msg}"


def sanitize_provider_response(text: str) -> str:
    """제공자(OpenAI/Gemini 등) 응답 본문에서 키 값을 가린다.

    OpenAI 는 **잘못된 API 키를 오류 메시지에 담아 돌려주는** 사례가
    있어, 그 본문을 가공 없이 반환·로그에 싣는 것은 키 유출 경로가
    된다. 본 함수는 :func:`sanitize_text` 와 같은 규칙을 적용해
    키 *형태* 를 가리되, 오류 사유(HTTP 상태·메시지 골격)는 남긴다.

    설정된 키 값만 지우는 것으로는 부족하다 — 사용자가 방금 오타 낸
    키 값도 응답에 실려 올 수 있다. 따라서 **키처럼 생긴 문자열 패턴**
    을 가린다.
    """
    return sanitize_text(text)
