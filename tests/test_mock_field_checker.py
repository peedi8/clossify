"""mock 필드 검사기가 생성뿐 아니라 접근식 오탈자도 경고하는지 검증한다."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_mock_fields.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_mock_fields_under_test", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_collector_finds_creation_get_and_subscript_origins():
    checker = _load_checker()
    tree = ast.parse(
        """
payload = {"createdField": 1, "snake_case": 2}
value = payload.get("readField")
other = payload["indexedField"]
ignored = payload.get(variable_name)
"""
    )
    collector = checker.DictKeyCollector()
    collector.visit(tree)
    assert collector.found == [
        (2, "createdField", "생성"),
        (3, "readField", "접근"),
        (4, "indexedField", "접근"),
    ]


def test_collect_mock_fields_preserves_access_origin(tmp_path):
    checker = _load_checker()
    (tmp_path / "test_sample.py").write_text(
        'payload = {"madeField": 1}\n' 'payload.get("getField")\n' 'payload["subscriptField"]\n',
        encoding="utf-8",
    )
    found = checker._collect_mock_fields(tmp_path)
    origins = {key: locations[0][2] for key, locations in found.items()}
    assert sorted(origins.items()) == [
        ("getField", "접근"),
        ("madeField", "생성"),
        ("subscriptField", "접근"),
    ]


def test_report_labels_creation_and_access():
    checker = _load_checker()
    report = checker._format_report(
        [
            (
                "wrongField",
                [("tests/test_x.py", 3, "생성"), ("tests/test_x.py", 7, "접근")],
                [],
            )
        ],
        [],
    )
    assert "(생성)" in report
    assert "(접근)" in report
    assert "wrongField" in report


def test_real_repository_scan_is_warning_only():
    checker = _load_checker()
    assert checker.main() == 0
