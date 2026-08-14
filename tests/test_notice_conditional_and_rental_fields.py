# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""Conditional notice fields and data-derived rental inheritance."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import qa_agents

_FIXTURE_PATH = _PROJECT_ROOT / "tests" / "fixtures" / "products_for_provided_notice.json"
_NOTICE_TYPES_PATH = _PROJECT_ROOT / "src" / "clossify" / "data" / "notice_types.json"
_HARVEST_PATH = _PROJECT_ROOT / "scripts" / "fetch_origin_and_notice_types.py"


def _load_harvester():
    spec = importlib.util.spec_from_file_location("notice_harvester_for_test", _HARVEST_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _entry(document: dict, notice_type: str) -> dict:
    for entry in document["verified"]:
        if entry["type"] == notice_type:
            return entry
    raise AssertionError(f"missing notice type: {notice_type}")


def _raw_entry(response: list[dict], notice_type: str) -> dict:
    for entry in response:
        if entry["productInfoProvidedNoticeType"] == notice_type:
            return entry
    raise AssertionError(f"missing raw notice type: {notice_type}")


def _notice_required_violations(notice_type: str, body: dict) -> list[dict]:
    node = qa_agents._notice_type_spec(notice_type)["node"]
    result = qa_agents._compliance_code_check(
        "notice-regression-test",
        {
            "category_id": None,
            "origin_content": "Korea",
            "notice": {
                "productInfoProvidedNoticeType": notice_type,
                node: body,
            },
        },
    )
    return [
        violation for violation in result["violations"] if violation.get("rule") == "고시 필수필드"
    ]


def test_conditional_field_descriptions_are_metadata_only_and_narrowly_scoped():
    harvester = _load_harvester()
    response = _load_json(_FIXTURE_PATH)
    document = harvester.build_notice_types_document(response, _load_json(_NOTICE_TYPES_PATH))

    conditional: list[tuple[str, str, str]] = []
    for raw_type in response:
        notice_type = raw_type["productInfoProvidedNoticeType"]
        spec = _entry(document, notice_type)
        for raw_field in raw_type["productInfoProvidedNoticeContents"]:
            field_name = raw_field["fieldName"]
            if harvester._is_conditional_notice_field(raw_field):
                conditional.append((notice_type, field_name, raw_field["fieldAddDescription"]))
                assert field_name not in spec["fields"]
                assert (
                    spec["field_meta"][field_name]["fieldAddDescription"]
                    == raw_field["fieldAddDescription"]
                )
            else:
                assert field_name in spec["fields"]

    conditional_keys = {(notice_type, field_name) for notice_type, field_name, _ in conditional}
    expected_named_conditionals = {
        ("FURNITURE", "refurb"),
        ("KIDS", "numberLimit"),
        ("SHOES", "height"),
        ("MEDICAL_APPLIANCES", "licenceNo"),
    }
    expected_regulated_fields = {
        (raw_type["productInfoProvidedNoticeType"], raw_field["fieldName"])
        for raw_type in response
        for raw_field in raw_type["productInfoProvidedNoticeContents"]
        if raw_field["fieldName"] in {"certificationType", "energyEfficiencyRating"}
        and "에 한함" in str(raw_field["fieldAddDescription"] or "")
    }
    misclassified_writing_instructions = {
        ("WEAR", "material"),
        ("KIDS", "material"),
        ("KIDS", "weight"),
        ("FURNITURE", "manufacturer"),
        ("FURNITURE", "producer"),
        ("SHOES", "material"),
        ("JEWELLERY", "producer"),
    }

    assert len(conditional) == 35
    assert expected_named_conditionals <= conditional_keys
    assert expected_regulated_fields <= conditional_keys
    assert not (misclassified_writing_instructions & conditional_keys)
    for notice_type, field_name in misclassified_writing_instructions:
        assert field_name in _entry(document, notice_type)["fields"]


def test_biocidal_expiration_date_text_uses_existing_data_derived_xor_handling():
    spec = qa_agents._notice_type_spec("BIOCIDAL")
    assert spec is not None
    assert {"expirationDate", "expirationDateText"} <= set(spec["fields"])
    assert any(
        set(group) == {"expirationDate", "expirationDateText"}
        for group in qa_agents._notice_xor_groups("BIOCIDAL")
    )

    body = {field: f"substantive value for {field}" for field in spec["fields"]}
    date_only = dict(body)
    date_only.pop("expirationDateText")
    assert _notice_required_violations("BIOCIDAL", date_only) == []
    assert qa_agents._notice_field_xor_violations(date_only, "BIOCIDAL") == []

    text_only = dict(body)
    text_only.pop("expirationDate")
    assert _notice_required_violations("BIOCIDAL", text_only) == []
    assert qa_agents._notice_field_xor_violations(text_only, "BIOCIDAL") == []

    violations = qa_agents._notice_field_xor_violations(body, "BIOCIDAL")
    assert len(violations) == 1
    assert set(violations[0]["group"]) == {"expirationDate", "expirationDateText"}


def test_rental_base_is_derived_from_the_unique_rental_etc_type():
    harvester = _load_harvester()
    response = _load_json(_FIXTURE_PATH)
    document = harvester.build_notice_types_document(response, _load_json(_NOTICE_TYPES_PATH))

    rental_types = [
        item for item in response if item["productInfoProvidedNoticeType"].startswith("RENTAL_")
    ]
    assert [item["productInfoProvidedNoticeType"] for item in rental_types] == [
        "RENTAL_ETC",
        "RENTAL_HA",
    ]
    base = harvester._rental_base_type(response)
    assert base is not None
    assert base["productInfoProvidedNoticeType"] == "RENTAL_ETC"

    base_required, base_meta = harvester._notice_field_parts(
        "RENTAL_ETC", base["productInfoProvidedNoticeContents"]
    )
    ha_raw = _raw_entry(response, "RENTAL_HA")
    ha_required, ha_meta = harvester._notice_field_parts(
        "RENTAL_HA", ha_raw["productInfoProvidedNoticeContents"]
    )
    ha_spec = _entry(document, "RENTAL_HA")

    assert ha_spec["fields"] == harvester.COMMON_NOTICE_FIELDS + base_required + ha_required
    assert ha_spec["field_meta"] == {**base_meta, **ha_meta}
    assert "ownershipTransferCondition" not in ha_spec["fields"]
    assert "ownershipTransferCondition" in ha_spec["field_meta"]


def test_rental_inheritance_refuses_ambiguous_base_data():
    harvester = _load_harvester()
    response = _load_json(_FIXTURE_PATH)
    ambiguous = copy.deepcopy(_raw_entry(response, "RENTAL_ETC"))
    ambiguous["productInfoProvidedNoticeType"] = "RENTAL_OTHER_ETC"
    response.append(ambiguous)

    with pytest.raises(ValueError, match="렌탈 공통 기반"):
        harvester._rental_base_type(response)


def test_gate_accepts_plain_furniture_but_requires_rental_base_fields():
    furniture_spec = qa_agents._notice_type_spec("FURNITURE")
    furniture_body = {field: f"substantive value for {field}" for field in furniture_spec["fields"]}
    assert "refurb" not in furniture_body
    assert _notice_required_violations("FURNITURE", furniture_body) == []

    furniture_body["refurb"] = "display item: minor scratch on tabletop"
    assert _notice_required_violations("FURNITURE", furniture_body) == []

    rental_spec = qa_agents._notice_type_spec("RENTAL_HA")
    rental_body = {field: f"substantive value for {field}" for field in rental_spec["fields"]}
    assert _notice_required_violations("RENTAL_HA", rental_body) == []

    rental_body.pop("refundPolicyForCancel")
    violations = _notice_required_violations("RENTAL_HA", rental_body)
    assert len(violations) == 1
    assert "refundPolicyForCancel" in violations[0]["detail"]
