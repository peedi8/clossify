"""정본 fieldType 기반 날짜형-직접입력 택일 게이트 회귀.

모든 검사는 저장된 정본 데이터와 단위 게이트만 사용한다. 네트워크 API 호출은 없다.
"""

from __future__ import annotations

from unittest import mock

from clossify import mcp_server, naver_client, qa_agents


def _filled_body_except(notice_type: str, excluded: set[str]) -> dict[str, str]:
    """해당 타입의 필수 필드를 유효한 비공백 값으로 채우되 지정 필드는 비운다."""
    spec = qa_agents._notice_type_spec(notice_type)
    assert spec is not None
    return {field: "실측값" for field in spec["fields"] if field not in excluded}


def test_all_date_text_pairs_are_derived_from_type_metadata():
    """정본의 22개 타입/28쌍이 수동 목록 없이 field_meta에서 도출된다."""
    pairs = [
        (spec["type"], pair)
        for spec in qa_agents._load_notice_types()
        for pair in qa_agents._notice_date_text_pairs(spec["type"])
    ]

    assert len({notice_type for notice_type, _ in pairs}) == 22
    assert len(pairs) == 28
    for notice_type, pair in pairs:
        spec = qa_agents._notice_type_spec(notice_type)
        assert spec is not None
        meta = spec["field_meta"]
        assert pair["text_field"] == pair["date_field"] + "Text"
        assert meta[pair["date_field"]]["fieldType"] in {"YearMonth", "LocalDate"}
        assert meta[pair["text_field"]]["fieldType"] == "String"


def test_wear_pack_date_or_text_satisfies_pair_and_neither_reports_once():
    """WEAR packDate/packDateText는 어느 한쪽으로 충족, 둘 다 없으면 한 번만 누락."""
    fields = qa_agents._notice_type_spec("WEAR")["fields"]
    pair = {"packDate", "packDateText"}

    text_only = _filled_body_except("WEAR", {"packDate"})
    assert qa_agents._notice_field_missing_with_relations(text_only, fields, "WEAR") == []

    date_only = _filled_body_except("WEAR", {"packDateText"})
    assert qa_agents._notice_field_missing_with_relations(date_only, fields, "WEAR") == []

    neither = _filled_body_except("WEAR", pair)
    assert qa_agents._notice_field_missing_with_relations(neither, fields, "WEAR") == ["packDate"]


def test_food_expiration_date_or_text_satisfies_same_data_rule():
    """FOOD expirationDate/expirationDateText도 동일한 정본 기반 규칙을 쓴다."""
    fields = qa_agents._notice_type_spec("FOOD")["fields"]

    text_only = _filled_body_except("FOOD", {"expirationDate"})
    assert qa_agents._notice_field_missing_with_relations(text_only, fields, "FOOD") == []

    date_only = _filled_body_except("FOOD", {"expirationDateText"})
    assert qa_agents._notice_field_missing_with_relations(date_only, fields, "FOOD") == []


def test_text_deferral_satisfies_pair_but_date_deferral_does_not():
    """날짜형은 미루지 못하지만 같은 쌍의 Text는 미루기로 충족할 수 있다."""
    fields = ["packDate", "packDateText"]
    assert qa_agents._field_missing_with_deferred({}, fields, ["packDateText"], "WEAR") == []
    assert qa_agents._field_missing_with_deferred({}, fields, ["packDate"], "WEAR") == ["packDate"]


def test_needs_user_reports_one_pair_with_both_answer_methods():
    """둘 다 없으면 needs_user는 한 건이며 연월/직접입력 방법을 모두 안내한다."""
    body = _filled_body_except("WEAR", {"packDate", "packDateText"})
    payload = {
        "originProduct": {
            "detailAttribute": {
                "productInfoProvidedNotice": {
                    "productInfoProvidedNoticeType": "WEAR",
                    "wear": body,
                }
            }
        }
    }
    with (
        mock.patch.object(mcp_server, "_category_path_for", return_value="의류/여성의류"),
        mock.patch.object(qa_agents, "_infer_notice_type", return_value="WEAR"),
        mock.patch.object(
            naver_client,
            "_notice_config",
            return_value={
                "origin_area_code": "04",
                "origin_content": "중국",
                "as_tel": "070-1234-5678",
            },
        ),
        mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
    ):
        gate = mcp_server._run_compliance_gate("테스트 WEAR", "50021299", payload)

    pair_requests = [item for item in gate["needs_user"] if item["field"] == "packDate"]
    assert len(pair_requests) == 1
    answer_shape = pair_requests[0]["answer_shape"]
    for expected in ("packDate", "packDateText", "정확한 연월(yyyy-MM)", "직접 입력"):
        assert expected in answer_shape
