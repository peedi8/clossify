"""상품 필드와 중복되는 판매자 태그의 사전 검사 계약."""

from clossify import qa_agents, register


def _no_recommend(_keyword):
    return 200, []


def _all_unrestricted(tags):
    return 200, [{"tag": tag, "restricted": False} for tag in tags]


def test_name_duplicate_is_removed_and_reported():
    result = qa_agents.filter_duplicate_field_tags(["니트", "보온"], name="여성 니트 풀오버")

    assert result == {
        "tags": ["보온"],
        "removed": [{"tag": "니트", "reason": "name"}],
    }


def test_brand_duplicate_is_removed_and_reported():
    result = qa_agents.filter_duplicate_field_tags(["Nike", "러닝"], brand="NIKE")

    assert result["tags"] == ["러닝"]
    assert result["removed"] == [{"tag": "Nike", "reason": "brand"}]


def test_category_duplicate_is_removed_and_reported():
    result = qa_agents.filter_duplicate_field_tags(
        ["풀오버", "겨울"], category_name="패션의류 > 여성의류 > 풀오버"
    )

    assert result["tags"] == ["겨울"]
    assert result["removed"] == [{"tag": "풀오버", "reason": "category"}]


def test_unmatched_tags_and_partial_match_control_group_are_preserved():
    tags = ["니트류", "니트풀오버", "보온", "겨울", "레이어드"]

    result = qa_agents.filter_duplicate_field_tags(tags, name="여성 니트 풀오버")

    assert result == {"tags": tags, "removed": []}


def test_empty_and_none_tags_are_safe():
    assert qa_agents.filter_duplicate_field_tags(None, name="니트") == {"tags": [], "removed": []}
    assert qa_agents.filter_duplicate_field_tags([], name="니트") == {"tags": [], "removed": []}


def test_case_and_whitespace_policy_is_stable():
    result = qa_agents.filter_duplicate_field_tags(
        ["  nike air  ", "러닝"], name="NIKE   AIR 운동화"
    )

    assert result["tags"] == ["러닝"]
    assert result["removed"] == [{"tag": "nike air", "reason": "name"}]


def test_existing_tag_route_filters_before_restricted_lookup_and_exposes_meta():
    checked_pools = []

    def restricted(tags):
        checked_pools.append(list(tags))
        return _all_unrestricted(tags)

    result = register._resolve_tags(
        "여성 니트 풀오버",
        ["니트", "보온"],
        brand="WarmCo",
        category_name="패션의류 > 여성의류 > 풀오버",
        recommend_fn=_no_recommend,
        restricted_fn=restricted,
    )

    assert result["final_tags"] == ["보온"]
    assert result["field_duplicates"] == [{"tag": "니트", "reason": "name"}]
    assert checked_pools == [["보온"]]
