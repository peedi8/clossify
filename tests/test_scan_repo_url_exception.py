# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""스캐너 local_word 정확일치 URL 예외 — 가드 생존 증명 테스트.

티켓 수용 조건:

  - 예외는 **URL 전체 문자열이 정확히 일치할 때만** 성립한다.
    같은 금지단어가 그 URL 이 아닌 형태(다른 경로, 혹은 예외 경로 안에서도
    URL 밖 문맥)로 등장하면 **여전히 위반으로 잡혀야 한다.**
  - 예외 적용 경로는 ``src/clossify/ui/setup.html`` 하나로 한정된다.

이 테스트 소스에는 금지단어도 예외 URL 리터럴도 두지 않는다 — 실제 로컬
목록(``.secrets/banned_words.local.txt``) 과 스캐너 상수에서 런타임에 추출해
임시 저장소 트리를 만들어 실제 scan_patterns 를 실행한다.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

# 프로젝트 루트/scripts 를 path 에 추가.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_PROJECT_ROOT), str(_PROJECT_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture
def scanner():
    """scan_repo 모듈을 fresh import 한다 (모듈 전역 상태 격리)."""
    sys.modules.pop("scan_repo", None)
    mod = importlib.import_module("scan_repo")
    importlib.reload(mod)
    return mod


def _banned_word_in_url(scanner) -> str:
    """예외 URL 에 포함된 실제 로컬 금지단어 하나를 런타임에 추출한다."""
    url = scanner.ALLOWED_LOCAL_EXACT_SUBSTRINGS[0][1]
    words, _rx = scanner.load_local_words()
    for w in words:
        if w and w in url:
            return w
    pytest.skip("예외 URL 에 포함된 로컬 금지단어가 없음 — 가드 검증 불가")


def _make_repo(scanner, tmp_path, setup_body: str, extra: str | None):
    """임시 저장소 트리를 만들고 스캐너 전역을 가리킨다. setup rel 경로 반환."""
    setup = tmp_path / "src" / "clossify" / "ui" / "setup.html"
    setup.parent.mkdir(parents=True)
    setup.write_text(setup_body, encoding="utf-8")
    if extra is not None:
        other = tmp_path / "src" / "other.txt"
        other.write_text(extra, encoding="utf-8")
    monkey_targets = {
        "_REPO_ROOT": str(tmp_path),
        "SCAN_PATHS": ["src"],
        "ALLOWED_MASKING_PAIRS": [],
    }
    return setup, monkey_targets


class TestExactUrlException:
    def test_exact_url_in_setup_html_not_flagged(self, scanner, tmp_path, monkeypatch):
        """정확히 일치하는 URL 의 setup.html 등장 → local_word 위반 아님."""
        url = scanner.ALLOWED_LOCAL_EXACT_SUBSTRINGS[0][1]
        word = _banned_word_in_url(scanner)
        local_path = tmp_path / "local.txt"
        local_path.write_text(word + "\n", encoding="utf-8")
        monkeypatch.setattr(scanner, "_LOCAL_LIST_PATH", str(local_path))

        setup, targets = _make_repo(scanner, tmp_path, f'<a href="{url}">g</a>\n', None)
        for k, v in targets.items():
            monkeypatch.setattr(scanner, k, v)

        v = scanner.scan_patterns(local_rx=scanner.load_local_words()[1])
        assert not any("local_word" in line for line in v), f"예외 URL 이 잡힘: {v}"

    def test_same_word_other_path_still_flagged(self, scanner, tmp_path, monkeypatch):
        """같은 금지단어가 다른 파일에 URL 아닌 형태로 → 여전히 위반."""
        url = scanner.ALLOWED_LOCAL_EXACT_SUBSTRINGS[0][1]
        word = _banned_word_in_url(scanner)
        local_path = tmp_path / "local.txt"
        local_path.write_text(word + "\n", encoding="utf-8")
        monkeypatch.setattr(scanner, "_LOCAL_LIST_PATH", str(local_path))

        # URL 아닌 문맥(평범한 문장) 에 금지단어 — 다른 경로의 파일.
        _setup, targets = _make_repo(
            scanner,
            tmp_path,
            f'<a href="{url}">g</a>\n',
            f"주석: 여기 {word} 언급\n",
        )
        for k, v in targets.items():
            monkeypatch.setattr(scanner, k, v)

        v = scanner.scan_patterns(local_rx=scanner.load_local_words()[1])
        hits = [line for line in v if "local_word" in line and "other.txt" in line]
        assert hits, f"다른 경로의 금지단어가 안 잡힘: {v}"

    def test_same_word_outside_url_in_setup_html_still_flagged(
        self, scanner, tmp_path, monkeypatch
    ):
        """예외 경로 setup.html 안에서도 URL 밖 문맥의 등장 → 여전히 위반."""
        url = scanner.ALLOWED_LOCAL_EXACT_SUBSTRINGS[0][1]
        word = _banned_word_in_url(scanner)
        local_path = tmp_path / "local.txt"
        local_path.write_text(word + "\n", encoding="utf-8")
        monkeypatch.setattr(scanner, "_LOCAL_LIST_PATH", str(local_path))

        # 예외 URL 과 **같은 줄** 에, URL 밖 문맥으로 한 번 더 등장.
        body = f'<a href="{url}">g</a> 메모: {word}\n'
        _setup, targets = _make_repo(scanner, tmp_path, body, None)
        for k, v in targets.items():
            monkeypatch.setattr(scanner, k, v)

        v = scanner.scan_patterns(local_rx=scanner.load_local_words()[1])
        hits = [line for line in v if "local_word" in line and "setup.html" in line]
        assert hits, f"URL 밖 문맥의 금지단어가 안 잡힘: {v}"

    def test_mutated_url_still_flagged(self, scanner, tmp_path, monkeypatch):
        """URL 이 변형된 형태(중간 삽입) → 정확일치 아니므로 위반."""
        url = scanner.ALLOWED_LOCAL_EXACT_SUBSTRINGS[0][1]
        word = _banned_word_in_url(scanner)
        local_path = tmp_path / "local.txt"
        local_path.write_text(word + "\n", encoding="utf-8")
        monkeypatch.setattr(scanner, "_LOCAL_LIST_PATH", str(local_path))

        # 중간에 문자 하나 삽입 — 정확일치 URL 을 포함하지 않는 변형형.
        marker = url.find(".com/") + len(".com/")
        mutated = url[:marker] + "x" + url[marker:]
        assert mutated != url and url not in mutated
        body = f'<a href="{mutated}">g</a>\n'
        _setup, targets = _make_repo(scanner, tmp_path, body, None)
        for k, v in targets.items():
            monkeypatch.setattr(scanner, k, v)

        v = scanner.scan_patterns(local_rx=scanner.load_local_words()[1])
        hits = [line for line in v if "local_word" in line]
        assert hits, f"변형 URL 이 예외로 통과함: {v}"

    def test_exception_scoped_to_single_path(self, scanner):
        """예외 정의가 (경로, URL) 쌍 리스트이고 경로는 setup.html 하나뿐."""
        entries = scanner.ALLOWED_LOCAL_EXACT_SUBSTRINGS
        assert isinstance(entries, list)
        paths = {p for p, _s in entries}
        assert paths == {"src/clossify/ui/setup.html"}, f"예외 경로 한정 위반: {paths}"
