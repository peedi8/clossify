# -*- coding: utf-8 -*-
"""T-206 — 자체 저장소 검사 스크립트.

오케스트레이터가 임시 스크립트로 수행하던 검사를 저장소에 고정된 스크립트로
승격한다. 누구든 ``python scripts/scan_repo.py`` 한 번이면 동일한 검사를
재현할 수 있다.

수행 항목 (위반 시 즉시 exit 1):
  1. 디코드 인지 금칙어 스캔
     - 소스를 읽고 ``\\xNN`` · ``\\uNNNN`` 이스케이프를 **디코드한 뒤** 검사.
       인코딩 우회(escape-obfuscation)를 차단하기 위함.
     - 금칙어 목록은 ``BANNED_WORDS`` 에 하드코딩.
     - ``ALLOWED_MASKING_PAIRS`` 로 tests/ 의 마스킹 검증용 가짜 키 문자열을
       (파일, 문자열) 쌍으로 허용.
  2. 한자(CJK 통합한자) 0건 확인 — 한글은 검사 대상 아님.
  3. 커밋 메시지 스캔 — ``git log origin/main..HEAD`` (원격 없으면 최근 20커밋)
     에서 claude/anthropic/co-authored 문자열이 보이면 실패.

각 위반은 ``파일:라인: 무엇이 걸렸는지`` 형태로 stdout 에 출력한다.

사용법:
    python scripts/scan_repo.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

# Windows cp949 콘솔에서 UTF-8 출력이 깨지는 것을 방지.
# (한글 메시지 + em-dash 등이 ASCII 폴백 없이 출력되도록.)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except (AttributeError, OSError):
    pass  # 구형 Python 또는 리다이렉트된 스트림 — 폴백 불필요.

# ──────────────────────────────────────────────────────────────────────
# 저장소 루트 절대경로 (상수 — 다른 섹션보다 먼저 정의).
# ──────────────────────────────────────────────────────────────────────
_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))

# ──────────────────────────────────────────────────────────────────────
# 설정 — 금칙어 / 허용목록
# ──────────────────────────────────────────────────────────────────────

# 검사 대상 디렉터리/파일 (상대경로는 저장소 루트 기준).
SCAN_PATHS = [
    "src",
    "agents",
    "data",
    "scripts",
    "tests",
    "pyproject.toml",
    "config.example.json",
]

# 금칙어 — 소문자 비교.
#  - loah / onebound / taobao / tmall : 이전 소싱 레인 흔적
#  - 010-4400 : 내부 연락처 패턴
#  - peedi / DesignTasteLab / 3rdhand / thirdhand : 내부 식별자
#  - claude / anthropic / co-authored / codex / gpt-5 : LLM 도구 흔적
#  - api-gw : 내부 게이트웨이 식별자
#  - H:\    : 로컬 절대경로 누출 (Windows 드라이브 문자)
BANNED_WORDS = [
    "loah",
    "010-4400",
    "onebound",
    "taobao",
    "tmall",
    "peedi",
    "designtastelab",
    "claude",
    "anthropic",
    "co-authored",
    "codex",
    "gpt-5",
    "3rdhand",
    "thirdhand",
    "api-gw",
    # Windows 절대경로 누출 — "H:\" 접두사. Python 문자열 "h:\\" 는
    # 3문자(h,:,\)이며 re.escape 가 정규식용으로 추가 이스케이프한다.
    "h:\\",
]

# tests/ 의 마스킹 검증용 가짜 키 문자열 허용목록 — (파일경로 regex, 문자열).
# 여기에 등록된 (파일, 문자열) 쌍은 금칙어 스캔에서 제외된다.
# tests/ 전체를 무조건 통과시키지 않고, 파일+문자열 쌍으로 명시한다.
#
# ⚠️ 자기 참조 예외: 이 스캐너 자신(scripts/scan_repo.py)은 금칙어 리스트를
# 정의해야 하므로 자기 자신을 스캔하면 정의부가 위반으로 잡힌다. 이것은
# 스캐너의 본질적 속성이지 우회가 아니다 — 아래 루프가 BANNED_WORDS 의
# 각 토큰에 대해 (이 파일, 토큰) 쌍을 자동 등록한다. 토큰이 추가되면
# 예외도 자동으로 따라간다.
_SELF_PATH = os.path.relpath(__file__, _REPO_ROOT).replace(os.sep, "/")
ALLOWED_MASKING_PAIRS: list[tuple[str, str]] = [
    # tests/ 의 마스킹 검증용 가짜 키/경로 문자열 — 파일+문자열 쌍으로 명시.
    # (T-106 sanitization 테스트가 Windows H:\ 경로 마스킹을 검증한다.
    #  이 경로가 실제 유출이 아니라 테스트 픽스처임을 명시.)
    (r"tests/test_t105_fixes\.py$", "h:\\"),
]
# 자기 자신의 금칙어 정의부 허용 등록.
for _w in BANNED_WORDS:
    # "h:\" 토큰은 매칭 시 역슬래시가 정규식 메타이므로, 비교는 소문자화된
    # 원본 토큰으로 한다. _is_allowed 가 needle.lower() 와 비교하므로 그대로.
    ALLOWED_MASKING_PAIRS.append((_SELF_PATH, _w))
del _w, _SELF_PATH

# CJK 통합한자 코드포인트 범위 — 한글은 제외.
CJK_RANGES = [
    (0x4E00, 0x9FFF),    # CJK Unified Ideographs
    (0x3400, 0x4DBF),    # CJK Unified Ideographs Extension A
    (0xF900, 0xFAFF),    # CJK Compatibility Ideographs
]

# 커밋 메시지에서 금지되는 문자열 (소문자 비교).
BANNED_COMMIT_TOKENS = ["claude", "anthropic", "co-authored"]

# .git/ 등 무시할 디렉터리/확장자.
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache",
             "node_modules", ".local", ".secrets", "artifacts", "runs",
             "reports", "logs", "preview", "cases", ".mypy_cache"}
SKIP_EXTS = {".pyc", ".pyo", ".png", ".jpg", ".jpeg", ".gif", ".ico",
             ".woff", ".woff2", ".zip", ".gz", ".tar", ".7z", ".pdf"}

# Windows 절대경로 검사 제외 디렉터리/패턴.
# (H:\ 패턴 자체는 BANNED_WORDS 의 "h:\" 토큰이 처리한다.)

# ──────────────────────────────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────────────────────────────


def _repo(path: str) -> str:
    """저장소 루트 기준 절대경로로 변환."""
    return os.path.normpath(os.path.join(_REPO_ROOT, path))


def _iter_files(paths: list[str]):
    """검사 대상 파일(절대경로)을 순회한다. 스킵 디렉터리/확장자는 건너뜀."""
    for p in paths:
        ap = _repo(p)
        if os.path.isdir(ap):
            for root, dirs, files in os.walk(ap):
                # 스킵 디렉터리 prune — os.walk 수정.
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
    """``\\xNN`` / ``\\uNNNN`` / ``\\UNNNNNNNN`` 이스케이프를 디코딩한다.

    인코딩 우회(escape-obfuscation) 차단이 목적. 원본과 디코딩 결과를
    비교해 디코딩이 의미 있으면 디코딩된 텍스트를 반환한다.
    """
    # 1) \xNN → 바이트 → latin-1 디코딩 (UTF-8 시퀀스도 자연스럽게 복원).
    def _hex_repl(m: re.Match) -> str:
        try:
            return bytes([int(m.group(1), 16)]).decode("utf-8", "replace")
        except (ValueError, UnicodeDecodeError):
            return m.group(0)

    # 2) \uNNNN → 해당 코드포인트.
    def _uni_repl(m: re.Match) -> str:
        try:
            return chr(int(m.group(1), 16))
        except (ValueError, OverflowError):
            return m.group(0)

    # \xNN — hex 2자리.
    text = re.sub(r"\\x([0-9A-Fa-f]{2})", _hex_repl, text)
    # \uNNNN — 4자리.
    text = re.sub(r"\\u([0-9A-Fa-f]{4})", _uni_repl, text)
    # \UNNNNNNNN — 8자리.
    text = re.sub(r"\\U([0-9A-Fa-f]{8})", _uni_repl, text)
    # 8진법 이스케이프 \NNN (3자리) — 드물지만 회피에 쓰일 수 있음.
    def _oct_repl(m: re.Match) -> str:
        try:
            return bytes([int(m.group(1), 8)]).decode("utf-8", "replace")
        except (ValueError, UnicodeDecodeError):
            return m.group(0)
    text = re.sub(r"\\([0-3][0-7]{2})", _oct_repl, text)
    return text


def _is_allowed(path: str, needle: str) -> bool:
    """(파일경로, 문자열) 쌍이 허용목록에 있으면 True."""
    rel = os.path.relpath(path, _REPO_ROOT).replace(os.sep, "/")
    norm = needle.lower()
    for pat, s in ALLOWED_MASKING_PAIRS:
        if re.search(pat, rel) and s.lower() == norm:
            return True
    return False


def _banned_regex(word: str) -> re.Pattern:
    """금칙어 → 대소문자 무시 정규식. 역슬래시는 이스케이프."""
    return re.compile(re.escape(word), re.IGNORECASE)


# ──────────────────────────────────────────────────────────────────────
# 검사 1 — 디코드 인지 금칙어 스캔
# ──────────────────────────────────────────────────────────────────────

def scan_banned_words() -> list[str]:
    """각 파일을 원문/디코드본 양쪽에서 금칙어 검사.

    Returns:
        위반 메시지 리스트 (``파일:라인: word=...``).
    """
    violations: list[str] = []
    compiled = [(_banned_regex(w), w) for w in BANNED_WORDS]

    for path in _iter_files(SCAN_PATHS):
        raw = _read_text(path)
        if not raw:
            continue
        rel = os.path.relpath(path, _REPO_ROOT).replace(os.sep, "/")

        # 원문 라인별 검사.
        for lineno, line in enumerate(raw.splitlines(), start=1):
            for rx, word in compiled:
                for m in rx.finditer(line):
                    if _is_allowed(path, word):
                        continue
                    violations.append(
                        f"{rel}:{lineno}: banned_word={word!r} "
                        f"(raw match: {m.group(0)!r})"
                    )
        # 디코드 후 재검사 — 이스케이프로 숨긴 금칙어를 잡는다.
        decoded = _decode_escapes(raw)
        if decoded == raw:
            continue  # 디코딩 결과가 동일 → 이스케이프 없었음.
        for lineno, line in enumerate(decoded.splitlines(), start=1):
            for rx, word in compiled:
                for m in rx.finditer(line):
                    if _is_allowed(path, word):
                        continue
                    violations.append(
                        f"{rel}:{lineno}: banned_word={word!r} "
                        f"(decoded match: {m.group(0)!r})"
                    )
    return violations


# ──────────────────────────────────────────────────────────────────────
# 검사 2 — 한자(CJK 통합한자) 스캔
# ──────────────────────────────────────────────────────────────────────

# 정규식 문자 클래스 범위 표기(예: ``[\u4e00-\u9fff]``) 안의 이스케이프는
# 한자 *데이터*가 아니라 코드의 일부다. 디코드 후 검사 시 이 범위 표기
# 안에서 디코드된 한자는 오탐이다.
#
# 판정 근거: ``[...]`` 안의 ``X-Y`` 표기는 문자 *범위 리터럴*이며, 그 안의
# 유니코드 이스케이프(예: ``\u`` + 4자리 hex)는 "이 코드포인트에서 저 코
# 드포인트까지"라는 메타데이터를 인코딩할 뿐 실제 한자 텍스트가 아니다.
# 따라서 이 위치에서 디코드된 한자는 위반에서 제외한다.
_RE_CHARCLASS_RANGE = re.compile(
    r"\[[^\[\]\n]*\]",  # 한 줄 내의 대괄호 그룹 (정규식 문자 클래스는 중첩 불가)
)
# 문자 클래스 내에서 ``A-B`` 범위 정의를 찾는다.
# 디코드 *후* 라인을 검사하므로 양끝점은 이미 실제 문자(한자일 수 있음)이고,
# 중간에 ``-`` 만 있으면 된다. ``\-`` 이스케이프된 하이픈은 제외.
# 범위의 양끝점은 ``]`` 나 whitespace 가 아닌 임의의 한 글자.
_RE_RANGE_INSIDE_CLASS = re.compile(
    r"(?<!\\)"  # 끝점이 이스케이프된 `-`가 아니도록
    r"(\S)"     # 범위의 시작점
    r"\s*-\s*"  # 범위 연산자
    r"(\S)"     # 범위의 끝점
)


def _is_in_charclass_range(line: str, pos: int) -> bool:
    """``pos`` 위치의 문자가 정규식 문자 클래스 ``[...]`` 범위 표기 안인지.

    디코드 후 검사 시 ``[\u4e00-\u9fff]`` 같은 정규식 리터럴에서 디코드된
    한자가 오탐으로 잡히는 것을 막기 위함.
    """
    for cc in _RE_CHARCLASS_RANGE.finditer(line):
        if not (cc.start() <= pos < cc.end()):
            continue
        # 문자 클래스 안 — 그 안에 ``A-B`` 범위 표기가 있으면 예외.
        # 단, 끝점이 ``]`` 이면 안 된다 (닫는 괄호).
        inner = cc.group(0)
        if _RE_RANGE_INSIDE_CLASS.search(inner):
            return True
    return False


def scan_cjk() -> list[str]:
    """CJK 통합한자가 한 글자라도 있으면 위반.

    금칙어 스캔과 동일하게 ``\\xNN`` / ``\\uNNNN`` 이스케이프를 **디코드한
    뒤** 검사한다. 디코드 로직은 ``_decode_escapes`` 를 재사용(중복 구현 금지).

    예외: 정규식 문자 클래스 범위 표기(예: ``[\u4e00-\u9fff]``) 안에서
    디코드된 한자는 *코드*이지 데이터가 아니므로 위반에서 제외한다.
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
                            f"{rel}:{lineno}:{col}: cjk_ideograph "
                            f"U+{cp:04X} ({ch!r})"
                        )
                        break  # 같은 글자를 두 번 보고하지 않음.

        # (B) 디코드 후 재검사 — 이스케이프로 숨긴 한자를 잡는다.
        # 금칙어 스캔과 동일한 패턴: _decode_escapes 재사용.
        decoded = _decode_escapes(raw)
        if decoded == raw:
            continue  # 이스케이프 없었음.
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
                # 오탐 방지: 정규식 문자 클래스 범위 표기 안이면 제외.
                # 판정 근거: 대괄호 안의 범위 리터럴은 코드 메타데이터이지
                # 한자 데이터가 아니다.
                if _is_in_charclass_range(line, col - 1):
                    continue
                violations.append(
                    f"{rel}:{lineno}:{col}: cjk_ideograph "
                    f"U+{cp:04X} ({ch!r}) [decoded]"
                )
    return violations


# ──────────────────────────────────────────────────────────────────────
# 검사 3 — 커밋 메시지 스캔
# ──────────────────────────────────────────────────────────────────────

def _commit_range() -> list[str]:
    """``git log origin/main..HEAD`` 범위의 커밋 SHA 리스트.

    origin/main 이 없거나 git 명령 실패 시 최근 20커밋으로 폴백.
    """
    # Windows 한글 환경(cp949) 에서 UTF-8 커밋 메시지가 깨지는 것을 막기 위해
    # 바이트로 받아 UTF-8 로 명시 디코딩한다.
    def _run(args: list[str]) -> bytes | None:
        try:
            r = subprocess.run(
                ["git", *args],
                cwd=_REPO_ROOT, capture_output=True,
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

    # origin/main 범위 우선.
    if _run(["rev-parse", "--verify", "origin/main"]) is not None:
        out = _decode(_run(["log", "--format=%H%n%B%x00", "origin/main..HEAD"]))
        if out.strip():
            return out.split("\x00")
    # 폴백: 최근 20커밋.
    out = _decode(_run(["log", "-n", "20", "--format=%H%n%B%x00"]))
    if out.strip():
        return out.split("\x00")
    return []


def scan_commit_messages() -> list[str]:
    """커밋 메시지에서 claude/anthropic/co-authored 토큰 검출."""
    violations: list[str] = []
    blocks = _commit_range()
    for block in blocks:
        if not block.strip():
            continue
        lines = block.splitlines()
        if not lines:
            continue
        sha = lines[0].strip()
        body = "\n".join(lines[1:]).lower()
        for tok in BANNED_COMMIT_TOKENS:
            if tok in body:
                violations.append(
                    f"commit {sha[:10]}: banned_token={tok!r} "
                    f"in commit message"
                )
    return violations


# ──────────────────────────────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────────────────────────────

def main() -> int:
    print("[scan_repo] 검사 시작...", flush=True)
    all_violations: list[str] = []

    print("[scan_repo] (1/3) 디코드 인지 금칙어 스캔", flush=True)
    v1 = scan_banned_words()
    all_violations.extend(v1)

    print("[scan_repo] (2/3) 한자(CJK) 스캔", flush=True)
    v2 = scan_cjk()
    all_violations.extend(v2)

    print("[scan_repo] (3/3) 커밋 메시지 스캔", flush=True)
    v3 = scan_commit_messages()
    all_violations.extend(v3)

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
