"""검사기 — 시험 mock 의 dict 리터럴 키와 접근식 키 중 정본에 없는 camelCase 필드명을 찾아 경고한다.

사용법:
    python scripts/check_mock_fields.py

- tests/ 아래 test_*.py 를 ast 로 파싱한다.
- dict 리터럴의 키인 문자열 중 camelCase 전체일치인 것만 수집한다 (생성 키).
- ``obj.get("필드명")`` 의 첫 인자와 ``obj["필드명"]`` 의 문자열 첨자도 수집한다 (접근 키).
- 어휘(script/api_field_vocab.json)에도 없고 허용목록(mock_field_allowlist.json)에도 없으면 발견으로 본다.
- 발견 항목에 ``생성`` / ``접근`` 표기를 붙인다.
- 종료 코드: 발견이 있어도 0 (경고 전용). 검사 자체가 성립하지 않으면 2.
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

# Windows 리다이렉트(> file) 환경에서 stdout/stderr 이 cp949 로 고정되어
# 한글이 깨지는 것을 방지한다. UTF-8 로 재구성하되, 구형 환경에서
# reconfigure 가 없거나 실패하면 조용히 무시한다(원래 동작 보존).
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8")
        except Exception:
            pass

CAMEL_RE = re.compile(r"^[a-z][a-zA-Z0-9]*[A-Z][a-zA-Z0-9]*$")
WORD_SPLIT_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")


def _load_vocab(vocab_path: Path) -> set[str]:
    """api_field_vocab.json 에서 names 목록을 읽는다."""
    with open(vocab_path, encoding="utf-8") as fh:
        data = json.load(fh)
    return set(data.get("names", []))


def _load_allowlist(allowlist_path: Path) -> dict[str, str]:
    """mock_field_allowlist.json 을 읽는다. 사유가 빈 문자열이면 오류로 보고한다."""
    with open(allowlist_path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data


class DictKeyCollector(ast.NodeVisitor):
    """ast 에서 dict 리터럴 키(생성)와 접근식 키(접근)를 수집한다.

    수집 결과는 ``found`` 에 ``(lineno, key_name, origin)`` 튜플로 담긴다.
    ``origin`` 은 ``"생성"`` (dict 리터럴 키) 또는 ``"접근"`` (.get / 대괄호 접근).
    """

    def __init__(self) -> None:
        self.found: list[tuple[int, str, str]] = []

    def visit_Dict(self, node: ast.Dict) -> None:
        for key in node.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                if CAMEL_RE.fullmatch(key.value):
                    self.found.append((node.lineno, key.value, "생성"))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # obj.get("string") 패턴 감지
        if isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            if node.args and isinstance(node.args[0], ast.Constant):
                val = node.args[0].value
                if isinstance(val, str) and CAMEL_RE.fullmatch(val):
                    self.found.append((node.lineno, val, "접근"))
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        # obj["string"] 패턴 감지
        sl = node.slice
        if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
            if CAMEL_RE.fullmatch(sl.value):
                self.found.append((node.lineno, sl.value, "접근"))
        self.generic_visit(node)


def _collect_mock_fields(
    tests_dir: Path,
) -> dict[str, list[tuple[str, int, str]]]:
    """tests/ 아래 test_*.py 에서 dict 키 camelCase 토큰을 모은다.

    반환: {필드명: [(파일경로, 줄, origin), ...]}
    ``origin`` 은 ``"생성"`` (dict 리터럴 키) 또는 ``"접근"`` (.get / 대괄호 접근).
    """
    fields: dict[str, list[tuple[str, int, str]]] = defaultdict(list)

    test_files = sorted(tests_dir.glob("test_*.py"))
    for test_file in test_files:
        try:
            with open(test_file, encoding="utf-8") as fh:
                source = fh.read()
        except OSError:
            continue
        try:
            tree = ast.parse(source, filename=str(test_file))
        except SyntaxError:
            continue
        collector = DictKeyCollector()
        collector.visit(tree)
        for lineno, key_name, origin in collector.found:
            fields[key_name].append((str(test_file), lineno, origin))

    return fields


def _word_set(name: str) -> frozenset[str]:
    """이름을 단어로 쪼개고, 소문자화하고, 각 단어의 끝 `s` 를 하나 제거한 집합을 만든다.

    쪼개기 규칙: `[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+`
    단어가 빈 문자열이 되면 제거하지 않는다.
    """
    words: set[str] = set()
    for m in WORD_SPLIT_RE.findall(name):
        w = m.lower()
        if len(w) > 1 and w.endswith("s"):
            w = w[:-1]
        words.add(w)
    return frozenset(words)


def _find_candidates(name: str, vocab: set[str]) -> list[tuple[str, str]]:
    """가까운 정본 후보를 (후보, 근거) 쌍으로 찾는다.

    순서: ①대소문자만 다른 것(대소문자만 다름) ②단어집합 포함(단어집합 포함) ③후보 없음
    """
    # ① 대소문자 무시 정확일치
    name_lower = name.lower()
    for v in vocab:
        if v.lower() == name_lower and v != name:
            return [(v, "대소문자만 다름")]

    # ② 단어집합 포함: 어느 한쪽이 다른 쪽의 부분집합
    name_words = _word_set(name)
    if name_words:
        scored: list[tuple[int, str]] = []
        for v in vocab:
            v_words = _word_set(v)
            if not v_words:
                continue
            if name_words <= v_words or v_words <= name_words:
                symdiff_size = len(name_words ^ v_words)
                scored.append((symdiff_size, v))
        if scored:
            scored.sort(key=lambda pair: (pair[0], pair[1]))
            return [(v, "단어집합 포함") for _, v in scored[:2]]

    # ③ 후보 없음
    return []


def _format_report(
    discoveries: list[tuple[str, list[tuple[str, int, str]], list[tuple[str, str]]]],
    allowlist_errors: list[str],
) -> str:
    """사람이 읽는 표를 만든다.

    각 발견 위치에 ``생성`` / ``접근`` origin 표기를 붙인다.
    """
    lines: list[str] = []

    if allowlist_errors:
        lines.append("=== 허용목록 오류 (사유 없는 등록) ===")
        for name in allowlist_errors:
            lines.append(
                f"  ❌ '{name}' — 사유가 빈 문자열입니다. 사유를 채우거나 항목을 삭제하라."
            )
        lines.append("")

    if not discoveries:
        lines.append("발견: 0건 — 시험 mock 의 dict 키 중 정본에 없는 camelCase 필드명이 없습니다.")
        return "\n".join(lines)

    lines.append(f"=== 발견: {len(discoveries)}건 ===")
    lines.append("")

    for name, locations, candidates in discoveries:
        lines.append(f"  [{name}]")
        lines.append(f"    나온 곳: {len(locations)}곳")
        for fpath, lineno, origin in locations[:3]:
            short = fpath.replace("\\", "/")
            lines.append(f"      - {short}:{lineno} ({origin})")
        if len(locations) > 3:
            lines.append(f"      - ... 외 {len(locations) - 3}곳")
        if candidates:
            parts = [f"{cand} ({basis})" for cand, basis in candidates]
            cand_str = " | ".join(parts)
            lines.append(f"    가까운 정본 후보: {cand_str}")
        else:
            lines.append("    가까운 정본 후보: 후보 없음")
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    scripts_dir = Path(__file__).resolve().parent
    repo_root = scripts_dir.parent
    tests_dir = repo_root / "tests"

    vocab_path = scripts_dir / "api_field_vocab.json"
    allowlist_path = scripts_dir / "mock_field_allowlist.json"

    # 검사 자체가 성립하는지 확인
    if not vocab_path.exists():
        print(f"오류: 어휘 파일이 없습니다: {vocab_path}", file=sys.stderr)
        return 2
    if not allowlist_path.exists():
        print(f"오류: 허용목록 파일이 없습니다: {allowlist_path}", file=sys.stderr)
        return 2
    if not tests_dir.is_dir():
        print(f"오류: tests 디렉토리가 없습니다: {tests_dir}", file=sys.stderr)
        return 2

    vocab = _load_vocab(vocab_path)
    if not vocab:
        print("오류: 어휘가 비어 있습니다.", file=sys.stderr)
        return 2

    allowlist = _load_allowlist(allowlist_path)

    # 사유 없는 등록 검사
    allowlist_errors: list[str] = []
    for k, v in allowlist.items():
        if v == "":
            allowlist_errors.append(k)

    # mock 필드 수집
    mock_fields = _collect_mock_fields(tests_dir)

    # 발견 필터링: 어휘에도 없고 허용목록에도 없으면 발견
    discoveries: list[tuple[str, list[tuple[str, int, str]], list[tuple[str, str]]]] = []
    for name, locations in sorted(mock_fields.items()):
        if name in vocab:
            continue
        if name in allowlist:
            continue
        candidates = _find_candidates(name, vocab)
        discoveries.append((name, locations, candidates))

    report = _format_report(discoveries, allowlist_errors)

    print(report)

    # GITHUB_STEP_SUMMARY 에 덧붙이기
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        try:
            with open(step_summary, "a", encoding="utf-8") as fh:
                fh.write("## Mock Field Check\n\n```\n")
                fh.write(report)
                fh.write("\n```\n")
        except OSError:
            pass

    # 종료 코드: 발견이 있어도 0 (경고 전용)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
