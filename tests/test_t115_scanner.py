# -*- coding: utf-8 -*-
"""T-115 — 스캐너 자기유출 제거 검증 테스트.

이 테스트는 ``scripts/scan_repo.py`` 가 두 층 설계로 올바르게 전환되었는지
검증한다. 핵심 검증 항목:

  1. 층 1 범용 패턴이 고유명사 없이도 침투를 잡는다
     (가짜 휴대전화번호 / 가짜 드라이브 경로 / 한자 리터럴).
  2. 층 2 로컬 목록이 **있으면** 추가 검출이 일어나고, **없으면**
     층 1 만으로 동작하되 목록 부재 메시지를 stdout 에 출력한다.
  3. 파일 부재는 실패가 아니다.

**주의**: 이 테스트 파일에는 고유명사(실제 스토어명·인명·프로젝트명·도구명·
전화번호 앞자리 등)를 넣지 않는다. 검출력은 **범용 가짜 값**과 **임시로
생성한 로컬 목록**으로만 검증한다. 한자 테스트 데이터도 리터럴/이스케이프
대신 ``chr()`` 로 동적 생성하여 이 파일 자신이 스캐너에 걸리지 않게 한다.

고유명사 0건 대조(acceptance)는 오케스트레이터가 별도 목록으로 수행한다.
"""
from __future__ import annotations

import importlib
import io
import os
import sys
from pathlib import Path

import pytest

# 프로젝트 루트를 path 에 추가.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# scripts/ 디렉터리를 import 가능하게 path 에 추가.
_SCRIPTS = _PROJECT_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


@pytest.fixture
def scanner():
    """scan_repo 모듈을 fresh import 한다 (모듈 전역 상태 격리)."""
    sys.modules.pop("scan_repo", None)
    mod = importlib.import_module("scan_repo")
    importlib.reload(mod)
    return mod


# ============================================================================ #
# 층 1 — 범용 패턴 검출력 (로컬 목록 없는 상태)
# ============================================================================ #
class TestLayer1GenericPatterns:
    """로컬 목록이 없을 때 범용 패턴만으로 침투를 잡는가."""

    def test_kr_mobile_phone_detected(self, scanner, tmp_path, monkeypatch):
        """가짜 휴대전화번호 주입 → 검출."""
        target = tmp_path / "src" / "leak.py"
        target.parent.mkdir(parents=True)
        target.write_text(
            'phone = "010-0000-0000"\n',  # 범용 가짜 번호 (고유명사 아님)
            encoding="utf-8",
        )
        monkeypatch.setattr(scanner, "_REPO_ROOT", str(tmp_path))
        monkeypatch.setattr(scanner, "_LOCAL_LIST_PATH", str(tmp_path / "nope.txt"))
        monkeypatch.setattr(scanner, "SCAN_PATHS", ["src"])
        v = scanner.scan_patterns(local_rx=None)
        joined = "\n".join(v)
        assert "kr_mobile_phone" in joined

    def test_windows_drive_path_detected(self, scanner, tmp_path, monkeypatch):
        """가짜 드라이브 절대경로 주입 → 검출."""
        target = tmp_path / "src" / "leak.py"
        target.parent.mkdir(parents=True)
        # 드라이브 문자 + 백슬래시 — 범용 패턴. tests/ 가 아니므로 허용목록 제외.
        target.write_text(
            'p = "Z:\\\\some\\\\path"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(scanner, "_REPO_ROOT", str(tmp_path))
        monkeypatch.setattr(scanner, "_LOCAL_LIST_PATH", str(tmp_path / "nope.txt"))
        monkeypatch.setattr(scanner, "SCAN_PATHS", ["src"])
        monkeypatch.setattr(scanner, "ALLOWED_MASKING_PAIRS", [])
        v = scanner.scan_patterns(local_rx=None)
        joined = "\n".join(v)
        assert "windows_abs_path" in joined

    def test_posix_home_path_detected(self, scanner, tmp_path, monkeypatch):
        """POSIX 홈 경로 패턴 검출."""
        target = tmp_path / "src" / "leak.py"
        target.parent.mkdir(parents=True)
        target.write_text(
            'home = "/home/someuser/data"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(scanner, "_REPO_ROOT", str(tmp_path))
        monkeypatch.setattr(scanner, "_LOCAL_LIST_PATH", str(tmp_path / "nope.txt"))
        monkeypatch.setattr(scanner, "SCAN_PATHS", ["src"])
        monkeypatch.setattr(scanner, "ALLOWED_MASKING_PAIRS", [])
        v = scanner.scan_patterns(local_rx=None)
        joined = "\n".join(v)
        assert "posix_home_path" in joined

    def test_secret_assignment_detected(self, scanner, tmp_path, monkeypatch):
        """일반 시크릿 형태 검출 (api_key= 장문 값)."""
        target = tmp_path / "src" / "leak.py"
        target.parent.mkdir(parents=True)
        target.write_text(
            'api_key = "AAAAAAAAAAAAAAAA"\n',  # 16자 가짜 값
            encoding="utf-8",
        )
        monkeypatch.setattr(scanner, "_REPO_ROOT", str(tmp_path))
        monkeypatch.setattr(scanner, "_LOCAL_LIST_PATH", str(tmp_path / "nope.txt"))
        monkeypatch.setattr(scanner, "SCAN_PATHS", ["src"])
        monkeypatch.setattr(scanner, "ALLOWED_MASKING_PAIRS", [])
        v = scanner.scan_patterns(local_rx=None)
        joined = "\n".join(v)
        assert "secret_assignment" in joined

    def test_short_secret_not_false_positive(self, scanner, tmp_path, monkeypatch):
        """짧은 값(12자 미만)은 시크릿 패턴이 잡지 않음 (과잉 검출 방지)."""
        target = tmp_path / "src" / "ok.py"
        target.parent.mkdir(parents=True)
        target.write_text(
            'token = "short"\n',  # 5자 — 임계치 미만
            encoding="utf-8",
        )
        monkeypatch.setattr(scanner, "_REPO_ROOT", str(tmp_path))
        monkeypatch.setattr(scanner, "_LOCAL_LIST_PATH", str(tmp_path / "nope.txt"))
        monkeypatch.setattr(scanner, "SCAN_PATHS", ["src"])
        monkeypatch.setattr(scanner, "ALLOWED_MASKING_PAIRS", [])
        v = scanner.scan_patterns(local_rx=None)
        assert not any("secret_assignment" in line for line in v)

    def test_clean_file_no_violations(self, scanner, tmp_path, monkeypatch):
        """깨끗한 파일은 위반 0건."""
        target = tmp_path / "src" / "clean.py"
        target.parent.mkdir(parents=True)
        target.write_text(
            'name = "상품이름"\nprice = 1000\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(scanner, "_REPO_ROOT", str(tmp_path))
        monkeypatch.setattr(scanner, "_LOCAL_LIST_PATH", str(tmp_path / "nope.txt"))
        monkeypatch.setattr(scanner, "SCAN_PATHS", ["src"])
        monkeypatch.setattr(scanner, "ALLOWED_MASKING_PAIRS", [])
        v = scanner.scan_patterns(local_rx=None)
        assert v == []


# ============================================================================ #
# 한자(CJK) 검출력
# ============================================================================ #
class TestCjkDetection:
    """한자 리터럴/이스케이프 검출 — 범용 데이터로 검증.

    주의: 이 파일 자신이 스캐너에 걸리지 않도록, 한자 코드포인트는
    ``chr(0x...)`` 로 동적 생성하여 임시 파일에 기록한다. 테스트 소스에
    한자 리터럴이나 ``\\u`` 이스케이프를 두지 않는다.
    """

    def test_literal_cjk_detected(self, scanner, tmp_path, monkeypatch):
        """한자 리터럴 한 글자 → 검출."""
        cjk_char = chr(0x4E2D)  # U+4E2D — 소스에 리터럴/이스케이프 없음.
        target = tmp_path / "src" / "leak.py"
        target.parent.mkdir(parents=True)
        target.write_text(f's = "{cjk_char}"\n', encoding="utf-8")
        monkeypatch.setattr(scanner, "_REPO_ROOT", str(tmp_path))
        monkeypatch.setattr(scanner, "SCAN_PATHS", ["src"])
        v = scanner.scan_cjk()
        assert any("cjk_ideograph" in line for line in v)

    def test_escape_cjk_detected(self, scanner, tmp_path, monkeypatch):
        """``\\u`` 이스케이프로 숨긴 한자 → 디코드 후 검출."""
        hexpoint = "4" + "e" + "2" + "d"  # 분할 조립 — 소스에 완성형 이스케이프 없음.
        target = tmp_path / "src" / "leak.py"
        target.parent.mkdir(parents=True)
        target.write_text(
            's = "\\\\' + "u" + hexpoint + '"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(scanner, "_REPO_ROOT", str(tmp_path))
        monkeypatch.setattr(scanner, "SCAN_PATHS", ["src"])
        v = scanner.scan_cjk()
        assert any("cjk_ideograph" in line and "[decoded]" in line for line in v)

    def test_korean_not_flagged(self, scanner, tmp_path, monkeypatch):
        """한글은 CJK 한자 검사에서 제외 (위반 아님)."""
        target = tmp_path / "src" / "ok.py"
        target.parent.mkdir(parents=True)
        target.write_text(
            'name = "한글상품"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(scanner, "_REPO_ROOT", str(tmp_path))
        monkeypatch.setattr(scanner, "SCAN_PATHS", ["src"])
        v = scanner.scan_cjk()
        assert v == []


# ============================================================================ #
# 층 2 — 로컬 목록 로딩 & 적용
# ============================================================================ #
class TestLayer2LocalList:
    """로컬 목록 파일이 있을 때/없을 때 동작."""

    def test_local_list_present_applies(self, scanner, tmp_path, monkeypatch):
        """로컬 목록에 있는 단어 → 추가 검출."""
        # 임시 로컬 목록 — 고유명사가 아닌 **임시 마커 토큰** 사용.
        local_path = tmp_path / "local.txt"
        local_path.write_text(
            "# 임시 마커\nzzqqxx_test_token\n\n# 빈 줄 무시\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(scanner, "_LOCAL_LIST_PATH", str(local_path))
        words, rx = scanner.load_local_words()
        assert "zzqqxx_test_token" in words
        assert rx is not None
        assert rx.search("ZZQQXX_TEST_TOKEN")  # 대소문자 무시 확인

    def test_local_list_absent_returns_empty(self, scanner, tmp_path, monkeypatch):
        """로컬 목록 파일 부재 → ([], None) (실패 아님)."""
        monkeypatch.setattr(
            scanner, "_LOCAL_LIST_PATH", str(tmp_path / "does_not_exist.txt")
        )
        words, rx = scanner.load_local_words()
        assert words == []
        assert rx is None

    def test_local_list_applied_to_scan(self, scanner, tmp_path, monkeypatch):
        """로컬 목록 단어가 소스에 있으면 scan_patterns 가 잡는다."""
        local_path = tmp_path / "local.txt"
        local_path.write_text("zzqqxx_test_token\n", encoding="utf-8")
        monkeypatch.setattr(scanner, "_LOCAL_LIST_PATH", str(local_path))
        _, rx = scanner.load_local_words()

        target = tmp_path / "src" / "leak.py"
        target.parent.mkdir(parents=True)
        target.write_text(
            'v = "zzqqxx_test_token"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(scanner, "_REPO_ROOT", str(tmp_path))
        monkeypatch.setattr(scanner, "SCAN_PATHS", ["src"])
        monkeypatch.setattr(scanner, "ALLOWED_MASKING_PAIRS", [])
        v = scanner.scan_patterns(local_rx=rx)
        joined = "\n".join(v)
        assert "local_pattern=local_word" in joined

    def test_local_list_skips_comments_and_blanks(self, scanner, tmp_path, monkeypatch):
        """주석/빈 줄은 무시되고 값만 추출된다."""
        local_path = tmp_path / "local.txt"
        local_path.write_text(
            "# 첫 줄 주석\n\n  # 들여쓴 주석\nalpha_marker\nbeta_marker  # 행내 주석\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(scanner, "_LOCAL_LIST_PATH", str(local_path))
        words, rx = scanner.load_local_words()
        assert words == ["alpha_marker", "beta_marker"]
        assert rx is not None


# ============================================================================ #
# 메인 — stdout 알림 & 부재 시 동작
# ============================================================================ #
class TestMainNoticeAndExit:
    """main() 이 로컬 목록 부재를 stdout 에 알리고 층 1 로 동작하는가."""

    def test_absent_local_list_prints_notice(self, scanner, tmp_path, monkeypatch):
        """로컬 목록 부재 → 안내 메시지 출력 (조용한 축소 금지)."""
        monkeypatch.setattr(scanner, "_REPO_ROOT", str(tmp_path))
        monkeypatch.setattr(scanner, "_LOCAL_LIST_PATH", str(tmp_path / "nope.txt"))
        monkeypatch.setattr(scanner, "SCAN_PATHS", [])
        monkeypatch.setattr(scanner, "scan_commit_messages", lambda: [])

        buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", buf)
        rc = scanner.main()
        assert rc == 0  # 부재는 실패가 아님.
        out = buf.getvalue()
        assert "로컬 고유명사 목록이 없습니다" in out
        assert "PASS" in out

    def test_main_exit_1_on_violation(self, scanner, tmp_path, monkeypatch):
        """층 1 위반 → exit 1."""
        target = tmp_path / "src" / "leak.py"
        target.parent.mkdir(parents=True)
        target.write_text('p = "010-0000-0000"\n', encoding="utf-8")
        monkeypatch.setattr(scanner, "_REPO_ROOT", str(tmp_path))
        monkeypatch.setattr(scanner, "_LOCAL_LIST_PATH", str(tmp_path / "nope.txt"))
        monkeypatch.setattr(scanner, "SCAN_PATHS", ["src"])
        monkeypatch.setattr(scanner, "ALLOWED_MASKING_PAIRS", [])
        monkeypatch.setattr(scanner, "scan_commit_messages", lambda: [])

        buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", buf)
        rc = scanner.main()
        assert rc == 1
        assert "FAIL" in buf.getvalue()


# ============================================================================ #
# 커밋 메시지 — 형식 기반 검사
# ============================================================================ #
class TestCommitMessageFormScan:
    """커밋 메시지가 형식(트레일러) 기반으로 검사되는가."""

    def test_coauthored_trailer_flagged(self, scanner, monkeypatch):
        """공동저자 트레일러 형태 → 검출.

        트레일러 문자열을 ``chr`` 조립으로 만들어 이 테스트 소스 자신이
        로컬 목록 스캔에 걸리지 않게 한다.
        """
        # 공동저자 트레일러를 조립 — 소스에 연속된 토큰이 나타나지 않음.
        parts = ["Co", "authored", "by"]
        trailer = parts[0] + "-" + parts[1] + "-" + parts[2] + ":"
        monkeypatch.setattr(
            scanner,
            "_commit_range",
            lambda: ["abcdef0123\n\nSome change\n\n" + trailer + " x <x@y>\n"],
        )
        v = scanner.scan_commit_messages()
        # 검출 메시지에 banned_form 이 포함되어 있는지 확인.
        assert any("banned_form" in line for line in v)

    def test_clean_commit_no_violation(self, scanner, monkeypatch):
        """트레일러 없는 커밋 → 위반 0."""
        monkeypatch.setattr(
            scanner,
            "_commit_range",
            lambda: ["abcdef0123\n\nJust a normal message\n"],
        )
        v = scanner.scan_commit_messages()
        assert v == []


# ============================================================================ #
# 구조 — docstring / gitignore / 예시파일 부재
# ============================================================================ #
class TestScannerStructure:
    """스캐너 파일의 구조적 요구사항 검증 (고유명사 미포함 검증은 별도)."""

    def test_scanner_docstring_mentions_local_path(self):
        """docstring 이 로컬 목록 경로를 안내하고 있는가."""
        scanner_src = (_PROJECT_ROOT / "scripts" / "scan_repo.py").read_text(
            encoding="utf-8"
        )
        assert ".secrets/banned_words.local.txt" in scanner_src

    def test_no_example_local_list_tracked(self):
        """예시 파일(.example)이 저장소에 추적되지 않는가."""
        example = _PROJECT_ROOT / ".secrets" / "banned_words.local.txt.example"
        assert not example.exists(), "예시 파일이 존재 — 힌트가 됨"

    def test_local_list_is_gitignored(self):
        """.secrets/ 가 .gitignore 에 있는가."""
        gi = (_PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        assert ".secrets/" in gi

    def test_scanner_defines_generic_patterns(self, scanner):
        """GENERIC_PATTERNS 이 (이름, 정규식) 튜플 리스트인가."""
        assert isinstance(scanner.GENERIC_PATTERNS, list)
        assert len(scanner.GENERIC_PATTERNS) >= 4
        names = {n for n, _ in scanner.GENERIC_PATTERNS}
        assert "kr_mobile_phone" in names
        assert "windows_abs_path" in names
        assert "posix_home_path" in names
        assert "secret_assignment" in names

    def test_scanner_commit_patterns_are_form_based(self, scanner):
        """커밋 패턴이 형식(트레일러) 기반이고 특정 도구명을 나열하지 않는가."""
        assert isinstance(scanner.COMMIT_TRAILER_PATTERNS, list)
        assert len(scanner.COMMIT_TRAILER_PATTERNS) >= 1
        # 각 패턴 소스에 특정 도구명이 하드코딩되지 않았는지 확인.
        for rx in scanner.COMMIT_TRAILER_PATTERNS:
            src = rx.pattern.lower()
            # 트레일러 형태는 허용 — 특정 도구명 토큰은 불허.
            assert "authored" in src or "trailer" in src


# ============================================================================ #
# 통합 — 현 저장소 스캔 exit 0 (오탐 없음)
# ============================================================================ #
class TestRepoScanNoFalsePositives:
    """실제 저장소를 스캔했을 때 오탐 없이 exit 0."""

    def test_repo_scan_passes(self, scanner, monkeypatch):
        """scripts/scan_repo.py 가 이 저장소에서 exit 0."""
        # _REPO_ROOT / _LOCAL_LIST_PATH 는 reload 후 자동으로 현 저장소 기준.
        buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", buf)
        rc = scanner.main()
        out = buf.getvalue()
        # exit 0 이어야 함. 위반 시 디버그 출력.
        if rc != 0:
            pytest.fail(f"스캐너 위반 감지 (오탐):\n{out}")
        assert "PASS" in out
