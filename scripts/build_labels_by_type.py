# SPDX-License-Identifier: MIT
"""Build data/notice_field_labels.json with type-override layer.

One-shot builder for the §1 data-entry task. Reads the existing flat labels,
the harvest_labels_by_type.json (585 entries), and emits the merged file:
  - keep field-only entries (39 short labels incl. common-5)
  - add labels_by_type top-level key following formats_by_type pattern
  - new (doc) labels win over existing field-only labels; common-5 short
    labels stay at field-only key, full doc text goes to type layer

Idempotent: same inputs -> same output.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# ops directory lives outside the repo; resolve via env to keep the source
# scanner happy (no embedded absolute paths).
_OPS_ENV = os.environ.get("CLOSSIFY_OPS_DIR")
if not _OPS_ENV:
    raise SystemExit(
        "CLOSSIFY_OPS_DIR env var must point at the ops directory "
        "(sibling of the repo, holds harvest_*.json)."
    )
OPS = Path(_OPS_ENV)

EXISTING = REPO / "data" / "notice_field_labels.json"
HARVEST = OPS / "harvest_labels_by_type.json"
OUT = REPO / "data" / "notice_field_labels.json"

# Common-5 fields whose short field-only label is the needs_user display name.
# Full doc phrasing goes to the type-override layer only.
COMMON5 = {
    "returnCostReason",
    "noRefundReason",
    "qualityAssuranceStandard",
    "compensationProcedure",
    "troubleShootingContents",
}

SOURCE_URL = (
    "https://apicenter.commerce.naver.com/docs/commerce-api/current/" "create-product-product"
)


def main() -> None:
    existing = json.loads(EXISTING.read_text(encoding="utf-8"))
    harvest = json.loads(HARVEST.read_text(encoding="utf-8"))

    raw_harvest_labels: dict[str, str] = harvest["labels"]

    # Group harvest entries by (type, field) and also by field for cross-check.
    by_type: dict[str, dict[str, str]] = defaultdict(dict)
    for compound, label in raw_harvest_labels.items():
        if "." not in compound:
            raise ValueError(f"harvest key without '.': {compound!r}")
        type_name, field = compound.split(".", 1)
        by_type[type_name][field] = label

    # Build labels_by_type: {TYPE: {field: label}} mirroring formats_by_type.
    labels_by_type: dict[str, dict[str, str]] = {}
    for type_name in sorted(by_type):
        labels_by_type[type_name] = dict(sorted(by_type[type_name].items()))

    # The field-only layer: keep existing 39 entries verbatim.
    # Work order §1: "기존 39건과 새 라벨이 충돌하면 새(문서) 라벨이 이긴다 —
    # 단 기존 공통 5필드의 짧은 라벨은 필드 단독 키로 유지하고
    # 문서 전체 문구는 타입별 층에."
    #
    # For NON-common-5 fields where existing label and doc label disagree,
    # the doc label wins for the field-only key. We compute agreement across
    # all types that carry that field: if every type agrees on one text, the
    # field-only layer takes that text; if types disagree, the field-only
    # label is left as the existing short label (fallback for unknown type)
    # and the divergence is resolved at the type layer.
    fields_in_harvest: dict[str, set[str]] = defaultdict(set)
    for type_name, field_map in by_type.items():
        for field, text in field_map.items():
            fields_in_harvest[field].add(text)

    labels_out: dict[str, dict[str, str]] = {}
    for field, entry in existing["labels"].items():
        if field in COMMON5:
            # Short label stays verbatim for needs_user display.
            labels_out[field] = entry
            continue
        texts = fields_in_harvest.get(field)
        if texts and len(texts) == 1:
            doc_text = next(iter(texts))
            # Doc wins. Preserve hint if existing label matches doc; otherwise
            # rewrite label to doc text and keep a generic hint.
            if entry["label"] == doc_text:
                labels_out[field] = entry
            else:
                labels_out[field] = {
                    "label": doc_text,
                    "hint": entry["hint"],
                }
        else:
            # No harvest data, or genuinely divergent across types: keep as-is.
            labels_out[field] = entry

    out = {
        "generated_at": "2026-08-05T00:00:00Z",
        "source": SOURCE_URL,
        "provenance": (
            "필드 단독 라벨(39건)은 기존 큐레이션. "
            "labels_by_type 은 공식 문서 스키마에서 기계 추출한 "
            "585건의 (타입.필드) → 한국어 라벨 (사용자 저장본에서 추출, 2026-08-05). "
            "같은 필드가 타입마다 다른 이름을 가질 때(35개 필드) 타입별 층이 우선한다."
        ),
        "note": existing["note"],
        "labels": labels_out,
        "labels_by_type": labels_by_type,
    }

    OUT.write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    n_field_only = len(labels_out)
    n_by_type = sum(len(m) for m in labels_by_type.values())
    print(f"field-only entries: {n_field_only}")
    print(f"labels_by_type entries: {n_by_type}")
    print(f"types covered: {len(labels_by_type)}")


if __name__ == "__main__":
    main()
