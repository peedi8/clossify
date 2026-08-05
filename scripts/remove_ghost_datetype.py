# SPDX-License-Identifier: MIT
"""Remove 4 ghost DateType fields from notice_types.json (§3 of work order).

publishDateType, releaseDateType, packDateType, expirationDateType do not
exist in the official Naver Commerce API schema. Live-probe confirms: a
"test" string sent on these fields was silently ignored (BOOKS registered
HTTP 200 with the ghost value present). Their presence in required lists
makes the compliance gate demand non-existent values from sellers.

For each affected type, this script removes the ghost field from `fields`
and adds a `field_notes` entry recording the removal reasoning.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PATH = REPO / "data" / "notice_types.json"

GHOSTS = {
    "publishDateType": (
        "공식 문서 스키마에 존재하지 않는 필드. "
        "프로브에서 임의 문자열을 넣어 전송해도 API 가 오류를 내지 않았고 "
        "BOOKS 타입은 그 상태로 HTTP 200 등록까지 완료됨 — 스키마에 없어 "
        "무시된 것으로 실증됨. 2026-08-05 제거. "
        "필수 목록에 남기면 게이트가 존재하지 않는 값을 요구하게 된다."
    ),
    "releaseDateType": (
        "공식 문서 스키마에 존재하지 않는 필드. "
        "프로브에서 임의 문자열을 넣어 전송해도 API 가 이를 무시했다 "
        "(BOOKS 등록 200 과 동일한 패턴 — 스키마 밖 필드는 조용히 누락). "
        "2026-08-05 제거. releaseDate/releaseDateText XOR 쌍은 유지."
    ),
    "packDateType": (
        "공식 문서 스키마에 존재하지 않는 필드. "
        "프로브에서 임의 문자열을 넣어도 API 가 무시함을 실증. "
        "2026-08-05 제거. packDate/packDateText XOR 쌍은 유지."
    ),
    "expirationDateType": (
        "공식 문서 스키마에 존재하지 않는 필드. "
        "프로브에서 임의 문자열을 넣어도 API 가 무시함을 실증. "
        "2026-08-05 제거. expirationDate/expirationDateText XOR 쌍은 유지."
    ),
}


def main() -> None:
    doc = json.loads(PATH.read_text(encoding="utf-8"))

    removed: list[tuple[str, str]] = []
    for entry in doc["verified"]:
        if "fields" not in entry:
            continue
        type_name = entry["type"]
        before = list(entry["fields"])
        after = [f for f in before if f not in GHOSTS]
        removed_here = [f for f in before if f not in after]
        if not removed_here:
            continue
        entry["fields"] = after
        notes = entry.setdefault("field_notes", {})
        for ghost in removed_here:
            notes[ghost] = (
                GHOSTS[ghost] + f" (이 타입 필드 목록에서 제거됨 — {len(before)}→{len(after)}건)"
            )
            removed.append((type_name, ghost))

    PATH.write_text(
        json.dumps(doc, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )

    print(f"removed {len(removed)} ghost field occurrences:")
    for t, f in removed:
        print(f"  {t}.{f}")


if __name__ == "__main__":
    main()
