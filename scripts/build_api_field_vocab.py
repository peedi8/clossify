"""어휘 생성기 — 커머스API 문서 스크랩 + src/clossify/data/*.json 에서 camelCase·PascalCase 토큰을 모은다.

사용법:
    python scripts/build_api_field_vocab.py <문서_디렉토리>

산출: scripts/api_field_vocab.json
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
from pathlib import Path

CAMEL_RE = re.compile(r"^[a-z][a-zA-Z0-9]*[A-Z][a-zA-Z0-9]*$")
PASCAL_RE = re.compile(r"^[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+$")


def _matches_any(name: str) -> bool:
    """camelCase 또는 PascalCase 규칙에 해당하는지 판별한다."""
    return bool(CAMEL_RE.fullmatch(name) or PASCAL_RE.fullmatch(name))


def _extract_camel_tokens(text: str) -> set[str]:
    """텍스트에서 camelCase·PascalCase 전체일치 토큰을 모은다."""
    tokens: set[str] = set()
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9]*", text):
        if _matches_any(raw):
            tokens.add(raw)
    return tokens


def _extract_keys_from_json(obj: object) -> set[str]:
    """JSON 객체의 모든 키를 재귀적으로 모은다."""
    keys: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and _matches_any(k):
                keys.add(k)
            keys |= _extract_keys_from_json(v)
    elif isinstance(obj, list):
        for item in obj:
            keys |= _extract_keys_from_json(item)
    return keys


def build_vocab(docs_dir: str) -> list[str]:
    """문서 디렉토리와 데이터 JSON 에서 camelCase 토큰을 모은다."""
    tokens: set[str] = set()

    # 1. 문서 디렉토리의 *.txt 전부
    txt_files = sorted(glob.glob(os.path.join(docs_dir, "*.txt")))
    for txt_path in txt_files:
        try:
            with open(txt_path, encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError):
            # 읽을 수 없는 파일은 건너뛴다
            continue
        tokens |= _extract_camel_tokens(text)

    # 2. src/clossify/data/*.json 의 키
    repo_root = Path(__file__).resolve().parent.parent
    json_dir = repo_root / "src" / "clossify" / "data"
    for json_path in sorted(json_dir.glob("*.json")):
        try:
            with open(json_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        tokens |= _extract_keys_from_json(data)

    return sorted(tokens)


def main() -> int:
    if len(sys.argv) < 2:
        print("사용법: python scripts/build_api_field_vocab.py <문서_디렉토리>", file=sys.stderr)
        return 1

    docs_dir = sys.argv[1]
    if not os.path.isdir(docs_dir):
        print(f"오류: 디렉토리가 없습니다: {docs_dir}", file=sys.stderr)
        return 1

    names = build_vocab(docs_dir)

    output = {
        "_생성": "scripts/build_api_field_vocab.py 로 재생성한다. 손으로 고치지 마라.",
        "_출처": "커머스API 문서 스크랩 + src/clossify/data/*.json",
        "_주의": "문서 스크랩은 불완전하다. 여기 없다고 존재하지 않는 필드인 것은 아니다.",
        "names": names,
    }

    out_path = Path(__file__).resolve().parent / "api_field_vocab.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(output, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    print(f"생성: {out_path} ({len(names)} 토큰)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
