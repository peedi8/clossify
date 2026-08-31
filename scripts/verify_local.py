"""로컬 검증 스크립트 — CI 가 실행하는 검사를 같은 순서로 그대로 실행한다.

이 목록은 ``.github/workflows/ci.yml`` 과 한 쌍이며, 워크플로를 바꾸면 여기도
함께 바꾼다. 두 곳이 어긋나면 "로컬은 통과, CI 는 실패"가 재발한다(과거 패스
세 건이 모두 이 어긋남이 원인이었다).

실행 순서(CI 의 ruff·pytest·scan-repo 잡과 동일):
  1. ruff==0.6.9 버전 확인(다르면 경고, 계속 진행)
  2. ``ruff check .``        — 린트
  3. ``ruff format --check .``— 포맷 검사(적용 아님)
  4. ``pip install -e ".[dev]"`` — 개발 의존성으로 패키지 설치
  5. ``pytest -q``           — 테스트 전건
  6. ``python scripts/scan_repo.py`` — 금칙어·한자·커밋메시지 자체 검사

하나라도 실패하면 exit 1. 각 단계의 명령과 exit 코드를 출력한다.

사용법:
    python scripts/verify_local.py

커밋 전 이것만 돌리면 CI 와 같은 검사를 한다.
"""

from __future__ import annotations

import os
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

# CI 가 고정하는 ruff 버전. ``.github/workflows/ci.yml`` 의
# ``pip install ruff==0.6.9`` 와 한 쌍. 워크플로를 바꾸면 여기도 바꾼다.
_EXPECTED_RUFF_VERSION = "0.6.9"

# CI 가 실행하는 검사 순서. 워크플로의 ruff·pytest·scan-repo 잡 단계를
# 그대로 옮겼다. 워크플로를 바꾸면 이 목록도 함께 바꾼다.
_STEPS: list[tuple[str, list[str]]] = [
    ("ruff check .", ["python", "-m", "ruff", "check", "."]),
    ("ruff format --check .", ["python", "-m", "ruff", "format", "--check", "."]),
    ('pip install -e ".[dev]"', ["python", "-m", "pip", "install", "-e", ".[dev]"]),
    ("pytest -q", ["python", "-m", "pytest", "-q"]),
    ("python scripts/scan_repo.py", ["python", "scripts/scan_repo.py"]),
]


def _run(cmd: list[str]) -> int:
    """명령을 저장소 루트에서 실행하고 exit 코드를 반환."""
    print(f"\n[verify_local] $ {' '.join(cmd)}", flush=True)
    print("-" * 60, flush=True)
    r = subprocess.run(cmd, cwd=_REPO_ROOT)
    print("-" * 60, flush=True)
    print(f"[verify_local] exit={r.returncode}", flush=True)
    return r.returncode


def _check_ruff_version() -> None:
    """설치된 ruff 버전이 CI 와 다르면 경고를 출력한다.

    버전이 다르면 셀렉터 해석 차이로 "로컬은 통과, CI 는 실패"가 발생할 수
    있다(과거 ruff 셀렉터 파싱 실패 사례). 경고만 출력하고 진행은 막지 않는다 — 사용자가
    의도적으로 다른 버전을 검사할 수도 있기 때문이다.
    """
    try:
        r = subprocess.run(
            ["python", "-m", "ruff", "--version"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        print(
            f"[verify_local] 경고: ruff 버전을 확인하지 못했습니다. "
            f"CI 는 ruff=={_EXPECTED_RUFF_VERSION} 을 사용합니다.",
            flush=True,
        )
        return
    if r.returncode != 0:
        print(
            f"[verify_local] 경고: ruff 가 설치되어 있지 않을 수 있습니다. "
            f"CI 는 ruff=={_EXPECTED_RUFF_VERSION} 을 사용합니다.",
            flush=True,
        )
        return
    installed = r.stdout.strip()
    # 출력형 예: "ruff 0.6.9" → 버전 부분만 추출.
    parts = installed.split()
    version = parts[-1] if parts else installed
    if version != _EXPECTED_RUFF_VERSION:
        print(
            f"[verify_local] 경고: 설치된 ruff 버전이 {version} 입니다. "
            f"CI 는 ruff=={_EXPECTED_RUFF_VERSION} 으로 고정되어 있어 "
            f"버전 차이로 인해 '로컬은 통과, CI 는 실패'가 발생할 수 있습니다. "
            f"권장: pip install ruff=={_EXPECTED_RUFF_VERSION}",
            flush=True,
        )
    else:
        print(f"[verify_local] ruff 버전 확인: {version} (CI 와 일치)", flush=True)


def main() -> int:
    print("=" * 60, flush=True)
    print("[verify_local] 로컬 검증 시작", flush=True)
    print(
        "[verify_local] 이 스크립트는 .github/workflows/ci.yml 의 "
        "ruff·pytest·scan-repo 잡과 같은 검사를 같은 순서로 실행한다.",
        flush=True,
    )
    print("=" * 60, flush=True)

    _check_ruff_version()

    results: list[tuple[str, int]] = []
    for label, cmd in _STEPS:
        code = _run(cmd)
        results.append((label, code))
        if code != 0:
            print(
                f"\n[verify_local] 실패: '{label}' exit={code} — " f"이후 단계는 건너뛴다.",
                flush=True,
            )
            break

    print("\n" + "=" * 60, flush=True)
    print("[verify_local] 요약:", flush=True)
    all_pass = True
    for label, code in results:
        status = "PASS" if code == 0 else "FAIL"
        print(f"  {status}  exit={code}  {label}", flush=True)
        if code != 0:
            all_pass = False

    # 전체 단계를 실행하지 않았다면 미실행 단계도 표시.
    for label, _ in _STEPS[len(results) :]:
        print(f"  SKIP  (앞 단계 실패)  {label}", flush=True)
        all_pass = False

    print("=" * 60, flush=True)
    if all_pass:
        print("[verify_local] 전 단계 PASS — CI 와 동일한 검사를 통과했다.", flush=True)
        return 0
    print("[verify_local] FAIL — 위 단계 중 하나 이상이 실패했다.", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
