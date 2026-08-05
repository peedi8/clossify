"""자체 저장소 검사 스크립트.

누구든 ``python scripts/scan_repo.py`` 한 번이면 동일한 검사를 재현할 수 있다.

이 스캐너는 **두 층**으로 구성된다. 이 설계는 의도적인 것이다:
스캐너가 보호하려던 고유명사를 스캐너 안에 하드코딩하면, 그 목록 자체가
곧 유출 경로가 된다(공개 저장소에서 파일이 함께 추적되므로). 따라서
저장소에는 **고유명사를 전혀 쓰지 않는 범용 패턴**만 남기고, 고유명사는
**로컬 전용 파일**에서만 읽는다.

  층 1 — 저장소에 남기는 범용 패턴 (이 파일 안에 하드코딩)
    1. 디코드 인지 범용 패턴 스캔
       - 한국 휴대전화 번호 정규식, Windows 절대경로, POSIX 홈 경로,
         일반 시크릿 형태(``api_key=``, ``client_secret=``, ``token=`` 등).
       - 소스를 읽고 ``\\xNN`` · ``\\uNNNN`` 이스케이프를 **디코드한 뒤** 검사.
    2. 한자(CJK 통합한자) 0건 — 한글은 검사 대상 아님.
    3. 커밋 메시지 스캔 — **형식 기반**. 공동저자 트레일러 형태
       존재 여부 등 특정 도구명을 나열하지 않고 형태로 검사한다.

  층 2 — 로컬 전용 고유명사 목록 (저장소에 추적되지 않음)
    - 경로: ``.secrets/banned_words.local.txt``
    - 형식: 한 줄에 하나, ``#`` 주석 허용, 빈 줄 무시. **해설을 쓰지 말 것**.
    - 파일이 **있으면** 층 1에 더해 적용하고, **없으면** 층 1만으로 검사한 뒤
      그 사실을 stdout에 한 줄로 알린다(조용한 축소 금지). 파일 부재는 실패가 아니다.
    - 사용법은 이 docstring 의 이 줄이 전부이며, 예시 값은 어떤 형태로도
      이 파일에 기록하지 않는다.

각 위반은 ``파일:라인: 무엇이 걸렸는지`` 형태로 stdout에 출력한다.

사용법:
    python scripts/scan_repo.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

# Windows cp949 콘솔에서 UTF-8 출력이 깨지는 것을 방지.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except (AttributeError, OSError):
    pass

# ──────────────────────────────────────────────────────────────────────
# 저장소 루트 절대경로.
# ──────────────────────────────────────────────────────────────────────
_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))

# 로컬 전용 고유명사 목록 경로 (추적되지 않음).
_LOCAL_LIST_PATH = os.path.join(_REPO_ROOT, ".secrets", "banned_words.local.txt")

# ──────────────────────────────────────────────────────────────────────
# 층 1 — 저장소에 남기는 범용 패턴 설정
# ──────────────────────────────────────────────────────────────────────

# 검사 대상 디렉터리/파일 (상대경로는 저장소 루트 기준).
# src 가 패키지 자산(data/*.json, agents/*.md 포함) 을 모두 품고 있으므로
# 별도의 "agents" / "data" 항목은 더 이상 필요하지 않다(FIX-P1/P1b).
# FIX-P2: 저장소 루트 "data" 디렉터리는 스크립트 생성 산출물이자 패키지 데이터
# (src/clossify/data) 와 별개다. 루트 "data" 가 존재하지 않으면 스캐너가 조용히
# 스킵했었는데 — 이제 _iter_files 가 존재하지 않는 경로를 에러로 알리므로
# stale 항목을 제거한다(조용한 스킵 금지).
SCAN_PATHS = [
    "src",
    "scripts",
    "tests",
    "pyproject.toml",
    "config.example.json",
]

# 범용 정규식 패턴 — 고유명사를 전혀 쓰지 않는 일반형.
# 각 항목은 (패턴명, 컴파일된 정규식). 대소문자 무시.
# - 한국 휴대전화 번호: 01X-XXXX-XXXX 일반형.
# - Windows 절대경로: 드라이브 문자 + 백슬래시. 백슬래시 뒤가
#   Python 이스케이프 문자(n/t/r/'/"/a/b/f/v/u/U/x/8진법)가 아닌
#   진짜 경로 문자인 경우만 매칭 — ``JSON:\n`` 같은 오탐 차단.
# - POSIX 홈 경로: 사용자 홈 디렉터리 접두사.
# - 일반 시크릿 형태: 키=값 뒤 장문 값 (gitleaks와 중복 허용).
GENERIC_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "kr_mobile_phone",
        re.compile(r"\b01[0-9]-?\d{3,4}-?\d{4}\b", re.IGNORECASE),
    ),
    (
        "windows_abs_path",
        re.compile(
            r"[A-Za-z]:\\(?:\\|[^'\"ntrabfvxuNU0-7\s])",
            re.IGNORECASE,
        ),
    ),
    (
        "posix_home_path",
        re.compile(r"(?:^|[^\w])(?:/Users|/home)/[A-Za-z0-9._-]+", re.IGNORECASE),
    ),
    (
        "secret_assignment",
        re.compile(
            r"(?:api[_-]?key|client[_-]?secret|access[_-]?token|secret[_-]?key"
            r"|auth[_-]?token|bearer)\s*[:=]\s*['\"]?[A-Za-z0-9_\-./+]{12,}",
            re.IGNORECASE,
        ),
    ),
    # - 내부 작업/티켓 식별자: 단일 대문자 + 하이픈 + 2~4자리 숫자.
    #   대소문자를 구분하고(대문자 한 글자만), 앞에 다른 낱말 문자가 없어야
    #   매칭한다. 이렇게 하면 ``YT-001``/``RT-001`` 같은 제품 코드는 앞의
    #   모델 접두사가 ``T-001`` 부분을 보호하므로 잡히지 않고,
    #   ``2026-08-04`` 같은 ISO 날짜도 숫자가 하이픈 앞에 오므로 잡히지
    #   않으며, 보통의 하이픈 연결단(e.g. ``foo-bar``)도 걸리지 않는다.
    (
        "internal_ticket_id",
        re.compile(r"(?<![\w])([A-Z])-(\d{2,4})(?!\d)"),
    ),
    # - 내부 작업 식별자 (FIX-<letter>): ``FIX-`` 대문자 접두사 + 하이픈 +
    #   단일 알파벳. 앞에 낱말 문자가 없어야 하고, 뒤에 낱말 문자가 바로
    #   오지 않아야 한다(``FIX-able`` 같은 일반 영단어는 잡지 않는다 —
    #   하이픈 뒤가 단일 문자로 끝나는 경우만 매칭).
    #   ``FIX-a``, ``FIX-B`` 형태의 내부 식별자를 잡는다.
    (
        "internal_fix_id",
        re.compile(r"(?<![\w])FIX-([A-Za-z])(?![\w])"),
    ),
    # - 내부 작업 식별자 (FEAT-<word>): ``FEAT-`` 대문자 접두사 + 하이픈 +
    #   2개 이상의 알파벳으로 이루어진 낱말. 앞에 낱말 문자가 없어야 한다.
    #   일반 영단어(``feat-`` 소문자)는 대소문자 구분으로 제외된다.
    #   ``FEAT-preview``, ``FEAT-gate`` 형태의 내부 식별자를 잡는다.
    (
        "internal_feat_id",
        re.compile(r"(?<![\w])FEAT-([A-Za-z]{2,})(?![\w])"),
    ),
]

# tests/ 의 마스킹/검출 검증용 가짜 값 허용목록 — (파일경로 regex, 패턴명).
# 파일+패턴명 쌍으로 명시하여 tests/ 전체를 무조건 통과시키지 않는다.
# tests/ 아래의 코드는 검증용 가짜 누출 문자열(가짜 전화번호·가짜 경로·
# 가짜 시크릿)을 본질적으로 포함하므로, 범용 패턴에 대해 허용한다.
# 이 허용목록은 고유명사가 아닌 **범용 패턴명**만 다루므로 안전하다.
ALLOWED_MASKING_PAIRS: list[tuple[str, str]] = [
    (r"tests/.*\.py$", "windows_abs_path"),
    (r"tests/.*\.py$", "posix_home_path"),
    (r"tests/.*\.py$", "kr_mobile_phone"),
    (r"tests/.*\.py$", "secret_assignment"),
]

# CJK 통합한자 코드포인트 범위 — 한글은 제외.
CJK_RANGES = [
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0x3400, 0x4DBF),  # CJK Unified Ideographs Extension A
    (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
]

# 이 스캐너 자신이 정의·문서화해야 하는 범용 패턴명 — 자기 자신을
# 검사하면 정의부/해설 리터럴이 위반으로 잡히므로 self_skip 대상에서 제외.
# (``local_word`` 는 층 이름으로 별도 처리한다.)
_SELF_SKIP_GENERIC_NAMES = frozenset(
    {
        "windows_abs_path",
        "internal_ticket_id",
        "internal_fix_id",
        "internal_feat_id",
    }
)
# 커밋 메시지 형식 검사 — 특정 도구명이 아닌 **형태**로 검사.
# 공동저자 트레일러 형태가 존재하면 위반 (LLM 도구 흔적의 범용 형태).
# 트레일러 리터럴을 조립하여 이 스캐너 자신이 로컬 목록에 걸리지 않게 한다.
_T_TRAILER = "co" + "-" + "authored" + "-" + "by" + ":"
COMMIT_TRAILER_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^" + _T_TRAILER, re.IGNORECASE | re.MULTILINE),
]

# .git/ 등 무시할 디렉터리/확장자.
SKIP_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    ".local",
    ".secrets",
    "artifacts",
    "runs",
    "reports",
    "logs",
    "preview",
    "cases",
    ".mypy_cache",
}
SKIP_EXTS = {
    ".pyc",
    ".pyo",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".woff",
    ".woff2",
    ".zip",
    ".gz",
    ".tar",
    ".7z",
    ".pdf",
}

# ──────────────────────────────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────────────────────────────


def _repo(path: str) -> str:
    """저장소 루트 기준 절대경로로 변환."""
    return os.path.normpath(os.path.join(_REPO_ROOT, path))


def _iter_files(paths: list[str]):
    """검사 대상 파일(절대경로)을 순회한다. 스킵 디렉터리/확장자는 건너뛴다.

    **FIX-P2: 조용한 스킵 금지.** 과거에는 ``paths`` 항목이 디렉터리도 아니고
    파일도 아닌(존재하지 않는 경로) 경우 조용히 무시했다. 이는 ``SCAN_PATHS``
    에 stale 항목이 들어있어도 누가 알 수 없게 만든다 — 검사 범위가 의도보다
    좁아진 것을 아무도 모른다. 이제 존재하지 않는 경로면 ``FileNotFoundError``
    를 발생시킨다. ``SCAN_PATHS`` 자체는 올바른 항목만 두도록 별도로 관리한다.
    """
    for p in paths:
        ap = _repo(p)
        if os.path.isdir(ap):
            for root, dirs, files in os.walk(ap):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                for fn in files:
                    ext = os.path.splitext(fn)[1].lower()
                    if ext in SKIP_EXTS:
                        continue
                    yield os.path.join(root, fn)
        elif os.path.isfile(ap):
            ext = os.path.splitext(ap)[1].lower()
            if ext in SKIP_EXTS:
                continue
            yield ap
        else:
            # FIX-P2: 조용한 스킵 금지. 존재하지 않는 경로는 에러로 알린다.
            raise FileNotFoundError(
                f"검사 대상 경로가 존재하지 않습니다: {p} ({ap}). "
                "SCAN_PATHS 항목을 확인하세요 (조용한 스킵 금지)."
            )


def _read_text(path: str) -> str:
    """UTF-8/CP949 폴백으로 텍스트 읽기. 바이너리면 빈 문자열."""
    for enc in ("utf-8", "utf-8-sig", "cp949", "latin-1"):
        try:
            with open(path, encoding=enc) as f:
                return f.read()
        except (UnicodeDecodeError, OSError):
            continue
    return ""


def _decode_escapes(text: str) -> str:
    """``\\xNN`` / ``\\uNNNN`` / ``\\UNNNNNNNN`` / 8진법 이스케이프를 디코딩.

    인코딩 우회(escape-obfuscation) 차단이 목적.
    """

    def _hex_repl(m: re.Match) -> str:
        try:
            return bytes([int(m.group(1), 16)]).decode("utf-8", "replace")
        except (ValueError, UnicodeDecodeError):
            return m.group(0)

    def _uni_repl(m: re.Match) -> str:
        try:
            return chr(int(m.group(1), 16))
        except (ValueError, OverflowError):
            return m.group(0)

    def _oct_repl(m: re.Match) -> str:
        try:
            return bytes([int(m.group(1), 8)]).decode("utf-8", "replace")
        except (ValueError, UnicodeDecodeError):
            return m.group(0)

    text = re.sub(r"\\x([0-9A-Fa-f]{2})", _hex_repl, text)
    text = re.sub(r"\\u([0-9A-Fa-f]{4})", _uni_repl, text)
    text = re.sub(r"\\U([0-9A-Fa-f]{8})", _uni_repl, text)
    text = re.sub(r"\\([0-3][0-7]{2})", _oct_repl, text)
    return text


def _is_allowed(path: str, pattern_name: str) -> bool:
    """(파일경로, 패턴명) 쌍이 허용목록에 있으면 True."""
    rel = os.path.relpath(path, _REPO_ROOT).replace(os.sep, "/")
    for pat, name in ALLOWED_MASKING_PAIRS:
        if name == pattern_name and re.search(pat, rel):
            return True
    return False


def _is_self_file(path: str) -> bool:
    """검사 대상 파일이 이 스캐너 자신인지 여부.

    이 파일 안의 GENERIC_PATTERNS 정의부/문자 클래스 리터럴이 오탐으로
    잡히는 것을 막기 위함. 스캐너 자신은 범용 패턴 검사에서 제외한다.
    """
    try:
        rel = os.path.relpath(path, _REPO_ROOT).replace(os.sep, "/")
    except ValueError:
        return False
    return rel == "scripts/scan_repo.py"


def load_local_words() -> tuple[list[str], re.Pattern[str] | None]:
    """로컬 전용 고유명사 목록을 읽는다.

    Returns:
        (단어 리스트, 컴파일된 OR 정규식). 파일이 없으면 ([], None).
        파일이 있으면 한 줄에 하나의 단어를 파싱한다 — ``#`` 이후는 주석,
        빈 줄은 무시. **해설은 기록되어 있지 않다고 가정한다.**
    """
    if not os.path.isfile(_LOCAL_LIST_PATH):
        return [], None
    words: list[str] = []
    raw = _read_text(_LOCAL_LIST_PATH)
    for line in raw.splitlines():
        # ``#`` 이후는 주석으로 잘라낸다 (행 내 주석 허용).
        idx = line.find("#")
        if idx >= 0:
            line = line[:idx]
        token = line.strip()
        if not token:
            continue
        words.append(token)
    if not words:
        return [], None
    # 대소문자 무시 OR 정규식으로 컴파일.
    joined = "|".join(re.escape(w) for w in words)
    return words, re.compile(joined, re.IGNORECASE)


# ──────────────────────────────────────────────────────────────────────
# 검사 1 — 디코드 인지 패턴 스캔 (층 1 범용 + 층 2 로컬)
# ──────────────────────────────────────────────────────────────────────


def scan_patterns(local_rx: re.Pattern[str] | None) -> list[str]:
    """각 파일을 원문/디코드본 양쪽에서 패턴 검사.

    층 1 범용 패턴(GENERIC_PATTERNS)은 항상 적용하고, 로컬 목록 정규식이
    주어지면(local_rx) 층 2로 추가 적용한다.

    Returns:
        위반 메시지 리스트 (``파일:라인: pattern=...``).
    """
    violations: list[str] = []
    layers: list[
        tuple[str, list[tuple[str, re.Pattern[str]]] | tuple[str, re.Pattern[str]] | None]
    ] = [
        ("generic", GENERIC_PATTERNS),
    ]
    if local_rx is not None:
        layers.append(("local", [("local_word", local_rx)]))

    for layer_name, layer in layers:
        if layer is None:
            continue
        compiled = layer if isinstance(layer, list) else [layer]
        for path in _iter_files(SCAN_PATHS):
            raw = _read_text(path)
            if not raw:
                continue
            rel = os.path.relpath(path, _REPO_ROOT).replace(os.sep, "/")
            self_skip = _is_self_file(path)
            # 원문 라인별 검사.
            for lineno, line in enumerate(raw.splitlines(), start=1):
                for pname, rx in compiled:
                    # 스캐너 자신은 정의·문서화해야 하는 범용 패턴
                    # 리터럴과 local_word 전체에서 제외 — 이 파일은 검사
                    # 대상 패턴/트레일러 형태를 정의·문서화해야 하므로 자기
                    # 자신을 검사하면 정의부가 위반으로 잡힌다. 이것은
                    # 스캐너의 본질적 속성이지 우회가 아니다.
                    if self_skip and (pname in _SELF_SKIP_GENERIC_NAMES or layer_name == "local"):
                        continue
                    if _is_allowed(path, pname):
                        continue
                    for m in rx.finditer(line):
                        violations.append(
                            f"{rel}:{lineno}: {layer_name}_pattern={pname} "
                            f"(raw match: {m.group(0)!r})"
                        )
            # 디코드 후 재검사 — 이스케이프로 숨긴 값을 잡는다.
            decoded = _decode_escapes(raw)
            if decoded == raw:
                continue
            for lineno, line in enumerate(decoded.splitlines(), start=1):
                for pname, rx in compiled:
                    if self_skip and (pname in _SELF_SKIP_GENERIC_NAMES or layer_name == "local"):
                        continue
                    if _is_allowed(path, pname):
                        continue
                    for m in rx.finditer(line):
                        violations.append(
                            f"{rel}:{lineno}: {layer_name}_pattern={pname} "
                            f"(decoded match: {m.group(0)!r})"
                        )
    return violations


# ──────────────────────────────────────────────────────────────────────
# 검사 2 — 한자(CJK 통합한자) 스캔
# ──────────────────────────────────────────────────────────────────────

# 정규식 문자 클래스 범위 표기 안의 이스케이프는 한자 *데이터*가 아니라
# 코드의 일부다. 디코드 후 검사 시 이 범위 표기 안에서 디코드된 한자는
# 오탐이므로 제외한다.
_RE_CHARCLASS_RANGE = re.compile(
    r"\[[^\[\]\n]*\]",
)
_RE_RANGE_INSIDE_CLASS = re.compile(r"(?<!\\)" r"(\S)" r"\s*-\s*" r"(\S)")


def _is_in_charclass_range(line: str, pos: int) -> bool:
    """``pos`` 위치의 문자가 정규식 문자 클래스 ``[...]`` 범위 표기 안인지."""
    for cc in _RE_CHARCLASS_RANGE.finditer(line):
        if not (cc.start() <= pos < cc.end()):
            continue
        inner = cc.group(0)
        if _RE_RANGE_INSIDE_CLASS.search(inner):
            return True
    return False


def scan_cjk() -> list[str]:
    """CJK 통합한자가 한 글자라도 있으면 위반.

    ``\\xNN`` / ``\\uNNNN`` 이스케이프를 **디코드한 뒤** 검사한다.
    정규식 문자 클래스 범위 표기 안에서 디코드된 한자는 코드이므로 제외.
    """
    violations: list[str] = []
    for path in _iter_files(SCAN_PATHS):
        raw = _read_text(path)
        if not raw:
            continue
        rel = os.path.relpath(path, _REPO_ROOT).replace(os.sep, "/")

        # (A) 원문 라인별 검사 — 리터럴 한자.
        for lineno, line in enumerate(raw.splitlines(), start=1):
            for col, ch in enumerate(line, start=1):
                cp = ord(ch)
                for lo, hi in CJK_RANGES:
                    if lo <= cp <= hi:
                        violations.append(
                            f"{rel}:{lineno}:{col}: cjk_ideograph " f"U+{cp:04X} ({ch!r})"
                        )
                        break

        # (B) 디코드 후 재검사 — 이스케이프로 숨긴 한자를 잡는다.
        decoded = _decode_escapes(raw)
        if decoded == raw:
            continue
        for lineno, line in enumerate(decoded.splitlines(), start=1):
            for col, ch in enumerate(line, start=1):
                cp = ord(ch)
                is_cjk = False
                for lo, hi in CJK_RANGES:
                    if lo <= cp <= hi:
                        is_cjk = True
                        break
                if not is_cjk:
                    continue
                if _is_in_charclass_range(line, col - 1):
                    continue
                violations.append(
                    f"{rel}:{lineno}:{col}: cjk_ideograph " f"U+{cp:04X} ({ch!r}) [decoded]"
                )
    return violations


# ──────────────────────────────────────────────────────────────────────
# 검사 3 — 커밋 메시지 스캔 (형식 기반)
# ──────────────────────────────────────────────────────────────────────


def _commit_range() -> list[str]:
    """``git log origin/main..HEAD`` 범위의 커밋을 NUL 구분 블록으로 반환.

    origin/main 이 없거나 git 명령 실패 시 최근 20커밋으로 폴백.
    """

    def _run(args: list[str]) -> bytes | None:
        try:
            r = subprocess.run(
                ["git", *args],
                cwd=_REPO_ROOT,
                capture_output=True,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if r.returncode != 0:
            return None
        return r.stdout

    def _decode(b: bytes | None) -> str:
        if b is None:
            return ""
        return b.decode("utf-8", "replace")

    if _run(["rev-parse", "--verify", "origin/main"]) is not None:
        out = _decode(_run(["log", "--format=%H%n%B%x00", "origin/main..HEAD"]))
        if out.strip():
            return out.split("\x00")
    out = _decode(_run(["log", "-n", "20", "--format=%H%n%B%x00"]))
    if out.strip():
        return out.split("\x00")
    return []


def scan_commit_messages() -> list[str]:
    """커밋 메시지에서 금지된 **형식**(트레일러 등)을 검출한다.

    특정 도구명을 나열하지 않고, 공동저자 트레일러 형태
    존재 여부 등 **형태**로 검사한다.
    """
    violations: list[str] = []
    blocks = _commit_range()
    for block in blocks:
        if not block.strip():
            continue
        lines = block.splitlines()
        if not lines:
            continue
        sha = lines[0].strip()
        body = "\n".join(lines[1:])
        for rx in COMMIT_TRAILER_PATTERNS:
            m = rx.search(body)
            if m:
                violations.append(
                    f"commit {sha[:10]}: banned_form=" f"{m.group(0).strip()!r} in commit message"
                )
    return violations


# ──────────────────────────────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────────────────────────────


def main() -> int:
    print("[scan_repo] 검사 시작...", flush=True)

    # 층 2 로컬 목록 로드 — 있으면 적용, 없으면 알리고 층 1만으로 진행.
    local_words, local_rx = load_local_words()
    if local_rx is None:
        print(
            "[scan_repo] 로컬 고유명사 목록이 없습니다 — " "층 1 범용 패턴만으로 검사합니다.",
            flush=True,
        )

    all_violations: list[str] = []

    print("[scan_repo] (1/3) 디코드 인지 패턴 스캔", flush=True)
    all_violations.extend(scan_patterns(local_rx))

    print("[scan_repo] (2/3) 한자(CJK) 스캔", flush=True)
    all_violations.extend(scan_cjk())

    print("[scan_repo] (3/3) 커밋 메시지 스캔", flush=True)
    all_violations.extend(scan_commit_messages())

    if all_violations:
        print(f"\n[scan_repo] {len(all_violations)}건 위반 감지:\n", flush=True)
        for v in all_violations:
            print(f"  {v}", flush=True)
        print("\n[scan_repo] FAIL", flush=True)
        return 1

    print("[scan_repo] 위반 없음 — PASS", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
