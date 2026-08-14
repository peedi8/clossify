# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""수확 데이터 정합 — 2026-08-04 야간 실호출로 확정된 계약 수확 검증.

본 테스트 파일은 살아있는 네이버 커머스 API 의 400 응답 원문으로 실증된
2026-08-04 야간 수확분이 데이터 파일에 정확히 반영되었는지 검증한다.
핵심 계약 — **목록 밖 유추 추가 절대 금지**. 본 테스트는 실증분만 데이터에
있는지 확인한다.

검증 항목:
  (a) 수확된 XOR 관계가 데이터에 실재하고, 대표 타입에서 게이트가 XOR 을
      적용한다 (하나만=통과, 둘 다=위반, 둘 다 없음=미제공).
  (b) geneticallyModified=False 가 제공으로 판정된다 (boolean).
  (c) IMAGE_APPLIANCES 필수 필드 목록에 additionalCost 가 있다.
  (d) 데이터 파일 스키마 정합 — 관계·타입에 등장하는 필드가 notice_types
      의 어느 타입 fields 에 실재한다 (고아 필드 없음).

COMMERCE_DRY_RUN 을 끈 상태에서 단위 판정 함수로 검증한다. HTTP 호출은
없다(단위 판정 함수만 사용).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import naver_client, qa_agents

# --------------------------------------------------------------------------- #
# 수확 목록 — 2026-08-04 야간 실호출로 확정된 XOR 관계 (19개 타입, 22쌍).
# KITCHEN_UTENSILS/ETC 는 기존 기록분이므로 여기서는 수확분만 나열한다.
# 본 목록은 테스트 고정값이다 — 데이터 파일과 비교하는 기준.
# --------------------------------------------------------------------------- #

# 각 타입별로 확정된 XOR 쌍의 리스트.
_HARVESTED_XOR: dict[str, list[tuple[str, str]]] = {
    "COSMETIC": [("expirationDate", "expirationDateText")],
    "HOME_APPLIANCES": [("releaseDate", "releaseDateText")],
    "FOOD": [
        ("packDate", "packDateText"),
        ("consumptionDate", "consumptionDateText"),
    ],
    "SEASON_APPLIANCES": [("releaseDate", "releaseDateText")],
    "OFFICE_APPLIANCES": [("releaseDate", "releaseDateText")],
    "CELLPHONE": [("releaseDate", "releaseDateText")],
    "OPTICS_APPLIANCES": [("releaseDate", "releaseDateText")],
    "BOOKS": [("publishDate", "publishDateText")],
    "KIDS": [("releaseDate", "releaseDateText")],
    "BIOCHEMISTRY": [
        ("expirationDate", "expirationDateText"),
        ("packDate", "packDateText"),
    ],
    "MICROELECTRONICS": [("releaseDate", "releaseDateText")],
    "NAVIGATION": [("releaseDate", "releaseDateText")],
    "CAR_ARTICLES": [("releaseDate", "releaseDateText")],
    "MEDICAL_APPLIANCES": [("releaseDate", "releaseDateText")],
    "GENERAL_FOOD": [
        ("packDate", "packDateText"),
        ("consumptionDate", "consumptionDateText"),
    ],
    "DIET_FOOD": [("consumptionDate", "consumptionDateText")],
    "MUSICAL_INSTRUMENT": [("releaseDate", "releaseDateText")],
    "SPORTS_EQUIPMENT": [("releaseDate", "releaseDateText")],
    "IMAGE_APPLIANCES": [("releaseDate", "releaseDateText")],
}

# 수확된 boolean 필드 — 2026-08-04 야간.
_HARVESTED_BOOLEAN_FIELDS = {"geneticallyModified", "importDeclarationCheck"}

# 수확된 date 필드 (형식이 확인된 것만).
_HARVESTED_DATE_FIELDS = {"packDate", "consumptionDate"}


def _flatten_pairs(pairs: list[tuple[str, str]]) -> set[tuple[str, str]]:
    return set(pairs)


def _xor_groups_as_set(notice_type: str) -> set[tuple[str, str]]:
    """qa_agents._notice_xor_groups 결과를 frozenset-of-tuples 로 반환."""
    groups = qa_agents._notice_xor_groups(notice_type)
    return {tuple(sorted(g)) for g in groups}


# =========================================================================== #
# (a) 수확된 XOR 관계가 데이터에 실재 + 게이트 적용.
# =========================================================================== #


class TestHarvestedXorRelations:
    """(a) 수확된 19개 타입/22쌍 의 XOR 관계가 데이터에 실재하며,
    대표 타입에서 게이트가 XOR 을 올바르게 적용한다."""

    def test_a_all_harvested_types_present_in_relations(self):
        """수확된 19개 타입이 전부 notice_field_relations.json 에 있다."""
        relations = qa_agents._load_notice_field_relations()
        missing_types = [t for t in _HARVESTED_XOR if t not in relations]
        assert not missing_types, f"수확된 타입 중 relations 데이터에 없는 것: {missing_types}"

    def test_a_all_harvested_pairs_present(self):
        """각 타입별 수확된 XOR 쌍이 전부 데이터에 있다."""
        for notice_type, pairs in _HARVESTED_XOR.items():
            actual = _xor_groups_as_set(notice_type)
            for left, right in pairs:
                key = tuple(sorted((left, right)))
                assert key in actual, (
                    f"{notice_type}: XOR 쌍 ({left}, {right}) 이 데이터에 없음. "
                    f"실제 그룹: {actual}"
                )

    @pytest.mark.parametrize(
        "notice_type,left,right",
        [
            ("COSMETIC", "expirationDate", "expirationDateText"),
            ("FOOD", "packDate", "packDateText"),
            ("BOOKS", "publishDate", "publishDateText"),
        ],
    )
    def test_a_gate_one_only_passes(self, notice_type, left, right):
        """대표 3개 타입 — 하나만 채우면 누락 0건 (통과 판정)."""
        fields = [left, right]
        # left 만 채운 경우.
        body = {left: "2026-08-04"}
        missing = qa_agents._notice_field_missing_with_relations(
            body, fields, notice_type=notice_type
        )
        assert missing == [], f"{notice_type}: {left} 만 있는데 누락 보고됨: {missing}"
        # right 만 채운 경우.
        body = {right: "2026년 8월"}
        missing = qa_agents._notice_field_missing_with_relations(
            body, fields, notice_type=notice_type
        )
        assert missing == [], f"{notice_type}: {right} 만 있는데 누락 보고됨: {missing}"

    @pytest.mark.parametrize(
        "notice_type,left,right",
        [
            ("COSMETIC", "expirationDate", "expirationDateText"),
            ("FOOD", "packDate", "packDateText"),
            ("BOOKS", "publishDate", "publishDateText"),
        ],
    )
    def test_a_gate_both_filled_is_xor_violation(self, notice_type, left, right):
        """대표 3개 타입 — 둘 다 채우면 XOR 위반."""
        body = {left: "2026-08-04", right: "2026년 8월"}
        violations = qa_agents._notice_field_xor_violations(body, notice_type)
        assert (
            len(violations) == 1
        ), f"{notice_type}: 둘 다 채웠을 때 위반이 1건이어야 함: {len(violations)}"
        detail = str(violations[0].get("detail") or "")
        assert (
            "둘 중 하나만" in detail
        ), f"{notice_type}: 위반 사유에 '둘 중 하나만' 없음: {detail!r}"

    @pytest.mark.parametrize(
        "notice_type,left,right",
        [
            ("COSMETIC", "expirationDate", "expirationDateText"),
            ("FOOD", "packDate", "packDateText"),
            ("BOOKS", "publishDate", "publishDateText"),
        ],
    )
    def test_a_gate_neither_is_missing(self, notice_type, left, right):
        """대표 3개 타입 — 둘 다 없으면 미제공 (누락 보고).

        XOR 그룹 전체가 충족되지 않았으므로 첫 멤버가 누락으로 보고된다
        (중복 방지 — 기존 계약).
        """
        fields = [left, right]
        body: dict = {}
        missing = qa_agents._notice_field_missing_with_relations(
            body, fields, notice_type=notice_type
        )
        assert len(missing) == 1, f"{notice_type}: 둘 다 비었을 때 누락이 1건이어야 함: {missing}"


# =========================================================================== #
# (b) geneticallyModified=False 가 제공으로 판정.
# =========================================================================== #


class TestHarvestedBooleanFields:
    """(b) 수확된 boolean 필드 판정 — False 도 제공."""

    def test_b_genetically_modified_type_is_boolean(self):
        """geneticallyModified 의 타입이 boolean 이다."""
        assert qa_agents._notice_field_type("geneticallyModified") == "boolean"

    def test_b_import_declaration_check_type_is_boolean(self):
        """importDeclarationCheck 의 타입이 boolean 이다."""
        assert qa_agents._notice_field_type("importDeclarationCheck") == "boolean"

    def test_b_false_is_provided(self):
        """geneticallyModified=False 는 제공으로 판정된다."""
        fields = ["geneticallyModified"]
        # False → 제공됨.
        assert qa_agents._notice_field_missing({"geneticallyModified": False}, fields) == []
        # True → 제공됨.
        assert qa_agents._notice_field_missing({"geneticallyModified": True}, fields) == []
        # None → 미제공.
        assert qa_agents._notice_field_missing({"geneticallyModified": None}, fields) == [
            "geneticallyModified"
        ]
        # 키 부재 → 미제공.
        assert qa_agents._notice_field_missing({}, fields) == ["geneticallyModified"]

    def test_b_string_refused_in_payload_validation(self):
        """boolean 필드에 문자열을 주면 _validate_notice_field_type 이 거부."""
        with pytest.raises(ValueError, match="예/아니오"):
            naver_client._validate_notice_field_type("geneticallyModified", "아니오")
        with pytest.raises(ValueError, match="예/아니오"):
            naver_client._validate_notice_field_type("importDeclarationCheck", "true")

    def test_b_bool_accepted_in_payload_validation(self):
        """Python bool 은 검증을 통과한다."""
        assert naver_client._validate_notice_field_type("geneticallyModified", False) is False
        assert naver_client._validate_notice_field_type("importDeclarationCheck", True) is True


# =========================================================================== #
# (c) IMAGE_APPLIANCES 필드 목록에 additionalCost 가 있다.
# =========================================================================== #


class TestImageAppliancesAdditionalCost:
    """(c) IMAGE_APPLIANCES 필수 필드에 additionalCost 가 있다 (NotNull 실증)."""

    def test_c_additional_cost_in_image_appliances_fields(self):
        """IMAGE_APPLIANCES 의 fields 배열에 additionalCost 가 있다."""
        spec = naver_client._notice_type_spec("IMAGE_APPLIANCES")
        assert spec is not None, "IMAGE_APPLIANCES 스펙이 없음"
        fields = spec.get("fields") or []
        assert (
            "additionalCost" in fields
        ), f"IMAGE_APPLIANCES 필드에 additionalCost 가 없음: {fields}"


# =========================================================================== #
# (d) 데이터 파일 스키마 정합 — 고아 필드 없음.
# =========================================================================== #


class TestSchemaConsistency:
    """(d) 관계·타입 데이터에 등장하는 필드가 notice_types 의 어느 타입
    fields 에 실재한다 (고아 필드 없음)."""

    @staticmethod
    def _all_notice_fields() -> set[str]:
        """모든 타입의 필수·조건부 실재 필드를 합친 집합.

        조건부 필드는 필수 ``fields`` 에는 없지만 정본 메타 ``field_meta`` 에
        보존되므로, 고아 필드 검증에서는 둘을 함께 본다.
        """
        all_fields: set[str] = set()
        for entry in naver_client._load_notice_type_specs():
            for f in entry.get("fields") or []:
                all_fields.add(f)
            field_meta = entry.get("field_meta") or {}
            if isinstance(field_meta, dict):
                all_fields.update(field_meta)
        return all_fields

    def test_d_relation_fields_exist_in_some_notice_type(self):
        """relations 데이터에 등장하는 모든 필드가 notice_types 어딘가에 있다."""
        relations = qa_agents._load_notice_field_relations()
        all_fields = self._all_notice_fields()
        orphans: list[str] = []
        for notice_type, groups in relations.items():
            xor_pairs = (groups or {}).get("xor", []) if isinstance(groups, dict) else []
            for pair in xor_pairs:
                for field in pair:
                    if field not in all_fields:
                        orphans.append(f"{notice_type}.{field}")
        assert (
            not orphans
        ), f"relations 에 등장하지만 notice_types 어디에도 없는 필드 (고아): {orphans}"

    def test_d_type_data_fields_exist_in_some_notice_type(self):
        """notice_field_types.json 의 필드가 notice_types 어딘가에 있다.

        2026-08-10 개정: D64 전수 수록 이후 API 정본(118 고유 필드명)에
        등장하지만 notice_types 의 36종 어디에도 속하지 않는 필드가 소수
        있다 (예: refurb, numberLimit, maintenance). 이들은 API 가 특정
        고시 타입에서만 요구하는 필드로, notice_types 의 36종이 API 전체
        가 아닌 검증된 부분집합이기 때문이다. 본 테스트는 이들을 허용한다.
        """
        types = qa_agents._load_notice_field_types()
        all_fields = self._all_notice_fields()
        orphans = [f for f in types if f not in all_fields]
        # API 정본에서만 등장하는 필드(notice_types 36종에 없음)는 허용.
        # 이들은 API 가 특정 타입에서만 요구하는 필드다.
        _API_ONLY_FIELDS = {"refurb", "numberLimit", "maintenance"}
        real_orphans = [f for f in orphans if f not in _API_ONLY_FIELDS]
        assert (
            not real_orphans
        ), f"notice_field_types 에 있지만 notice_types 어디에도 없는 필드: {real_orphans}"

    def test_d_harvested_fields_are_in_type_data(self):
        """수확된 boolean/date 계열 필드가 notice_field_types.json 에 기록되어 있다.

        API 정답표(D64 실측) 기반으로 date 가 세분화되었다:
        packDate·consumptionDate → local_date (LocalDate, yyyy-MM-dd).
        """
        types = qa_agents._load_notice_field_types()
        for field in _HARVESTED_BOOLEAN_FIELDS:
            assert field in types, f"수확된 boolean 필드 {field} 이 notice_field_types 에 없음"
            assert (
                types[field]["type"] == "boolean"
            ), f"{field} 의 타입이 boolean 이 아님: {types[field].get('type')!r}"
        for field in _HARVESTED_DATE_FIELDS:
            assert field in types, f"수확된 date 필드 {field} 이 notice_field_types 에 없음"
            assert types[field]["type"] in (
                "date",
                "local_date",
            ), f"{field} 의 타입이 date/local_date 가 아님: {types[field].get('type')!r}"

    def test_d_nutrition_facts_is_string_in_type_data(self):
        """nutritionFacts 는 string 타입으로 수록되어 있다.

        2026-08-10 개정: D64 전수 수록 이후 nutritionFacts 는 API 정본에서
        String 으로 선언된 것이 확인되어 데이터에 포함되었다. 과거에는
        문자열 실증만 있어 타입 파일에서 빼두었으나, 이제 API 가 String 으로
        선언한 것이 정본이므로 수록한다. 타입은 string 이어야 한다 (미루기 가능).
        """
        types = qa_agents._load_notice_field_types()
        assert "nutritionFacts" in types, (
            "nutritionFacts 가 타입 파일에 없습니다 — API 정본에서 String 으로 "
            "선언되었으므로 수록되어야 합니다."
        )
        assert (
            types["nutritionFacts"]["type"] == "string"
        ), f"nutritionFacts 의 타입이 string 이 아님: {types['nutritionFacts'].get('type')!r}"


# =========================================================================== #
# (e) date 필드 형식 — 2026-08-04 date-parse probe 수확.
#
# live API date-parse probe (2026-08-04) 로 타입별 date 필드 형식이 확정되었다.
# 본 섹션은 확정된 형식이 formats_by_type 에 정확히 들어있는지, 그리고 probe
# 하지 않은 필드/타입에는 형식이 없는지(목록 밖 유추 금지) 검증한다.
# =========================================================================== #

# releaseDate yyyy-MM — probe 로 확정된 14개 타입 (KITCHEN_UTENSILS, IMAGE_APPLIANCES
# 는 기존 기록분이고 여기서는 수확분만 나열).
_HARVESTED_RELEASE_DATE_YYYY_MM: set[str] = {
    "CAR_ARTICLES",
    "CELLPHONE",
    "HOME_APPLIANCES",
    "KIDS",
    "MEDICAL_APPLIANCES",
    "MICROELECTRONICS",
    "MUSICAL_INSTRUMENT",
    "NAVIGATION",
    "OFFICE_APPLIANCES",
    "OPTICS_APPLIANCES",
    "SEASON_APPLIANCES",
    "SPORTS_EQUIPMENT",
}

# expirationDate yyyy-MM — probe 로 확정된 2개 타입.
_HARVESTED_EXPIRATION_DATE_YYYY_MM: set[str] = {
    "COSMETIC",
    "BIOCHEMISTRY",
}

# publishDate yyyy-MM-dd — probe 로 확정된 1개 타입.
_HARVESTED_PUBLISH_DATE_YYYY_MM_DD: set[str] = {
    "BOOKS",
}


class TestHarvestedDateFormats:
    """(e) date-parse probe 로 확정된 타입별 date 형식이 데이터에 정확히 있다."""

    def test_e_release_date_formats_present(self):
        """releaseDate 의 formats_by_type 에 수확분 yyyy-MM 타입이 전부 있다."""
        types = qa_agents._load_notice_field_types()
        entry = types.get("releaseDate")
        assert entry is not None, "releaseDate 가 타입 데이터에 없음"
        fbt = entry.get("formats_by_type")
        assert isinstance(fbt, dict), f"releaseDate formats_by_type 이 없음: {entry!r}"
        for type_name in _HARVESTED_RELEASE_DATE_YYYY_MM:
            assert (
                fbt.get(type_name) == "yyyy-MM"
            ), f"releaseDate.{type_name} 형식이 yyyy-MM 이 아님: {fbt.get(type_name)!r}"
        # 기존 기록분도 보존되어야 한다.
        assert fbt.get("KITCHEN_UTENSILS") == "yyyy-MM"
        assert fbt.get("IMAGE_APPLIANCES") == "yyyy-MM"

    def test_e_expiration_date_formats_present(self):
        """expirationDate 가 타입 데이터에 있고 formats_by_type 이 정확하다.

        API 정답표(D64 실측) 기반으로 expirationDate 타입은 local_date(LocalDate) 이다.
        mixed_types 가 [LocalDate, YearMonth] 이므로 year_month 도 허용한다.
        """
        types = qa_agents._load_notice_field_types()
        entry = types.get("expirationDate")
        assert entry is not None, "expirationDate 가 타입 데이터에 없음"
        assert entry["type"] in (
            "date",
            "year_month",
            "local_date",
        ), f"expirationDate 타입이 date/year_month/local_date 가 아님: {entry.get('type')!r}"
        fbt = entry.get("formats_by_type")
        assert isinstance(fbt, dict), f"expirationDate formats_by_type 이 없음: {entry!r}"
        for type_name in _HARVESTED_EXPIRATION_DATE_YYYY_MM:
            assert (
                fbt.get(type_name) == "yyyy-MM"
            ), f"expirationDate.{type_name} 형식이 yyyy-MM 이 아님: {fbt.get(type_name)!r}"

    def test_e_publish_date_format_present(self):
        """publishDate 가 타입 데이터에 있고 BOOKS 형식이 yyyy-MM-dd 이다.

        API 정답표(D64 실측) 기반으로 publishDate 타입은 local_date(LocalDate) 이다.
        """
        types = qa_agents._load_notice_field_types()
        entry = types.get("publishDate")
        assert entry is not None, "publishDate 가 타입 데이터에 없음"
        assert entry["type"] in (
            "date",
            "local_date",
        ), f"publishDate 타입이 date/local_date 가 아님: {entry.get('type')!r}"
        fbt = entry.get("formats_by_type")
        assert isinstance(fbt, dict), f"publishDate formats_by_type 이 없음: {entry!r}"
        for type_name in _HARVESTED_PUBLISH_DATE_YYYY_MM_DD:
            assert (
                fbt.get(type_name) == "yyyy-MM-dd"
            ), f"publishDate.{type_name} 형식이 yyyy-MM-dd 가 아님: {fbt.get(type_name)!r}"

    def test_e_biochemistry_pack_date_not_probed(self):
        """BIOCHEMISTRY.packDate 는 probe 되지 않았으므로 형식이 없다.

        핵심 계약: probe 하지 않은 필드/타입 조합에 형식을 유추 기록하지 않는다.
        packDate 자체는 FOOD/GENERAL_FOOD/DIET_FOOD 에서 형식이 확인되었지만,
        BIOCHEMISTRY 의 packDate 는 별도 probe 없이 yyyy-MM-dd 라고 단정하면
        오신고가 된다.
        """
        types = qa_agents._load_notice_field_types()
        entry = types.get("packDate")
        assert entry is not None
        fbt = entry.get("formats_by_type") or {}
        assert (
            "BIOCHEMISTRY" not in fbt
        ), f"BIOCHEMISTRY.packDate 는 probe 되지 않았는데 형식이 기록됨: {fbt.get('BIOCHEMISTRY')!r}"

    def test_e_no_format_for_unlisted_types(self):
        """releaseDate 의 formats_by_type 에 목록 밖 타입이 없다.

        핵심 계약: probe 하지 않은 타입에 형식을 유추하지 않는다.
        예: FOOD 는 releaseDate 를 쓰지 않거나 형식이 확인되지 않았으므로
        releaseDate.formats_by_type 에 들어가면 안 된다.
        """
        types = qa_agents._load_notice_field_types()
        entry = types.get("releaseDate")
        assert entry is not None
        fbt = entry.get("formats_by_type") or {}
        # 전체가 수확분 + 기존 기록분(KITCHEN_UTENSILS, IMAGE_APPLIANCES) 이어야 한다.
        expected = _HARVESTED_RELEASE_DATE_YYYY_MM | {"KITCHEN_UTENSILS", "IMAGE_APPLIANCES"}
        extra = set(fbt.keys()) - expected
        assert (
            not extra
        ), f"releaseDate formats_by_type 에 probe 하지 않은 타입이 있음 (유추 금지 위반): {extra}"
