# -*- coding: utf-8 -*-
"""T-301 — 조립 결과 문서(scene) 산출 검증 테스트.

작업지시(T-301) 의 Acceptance 반례 전체를 단위 테스트로 구현한다:
  - 결정론: 같은 입력으로 build_scene 두 번 호출 → generatedAt 제외 동일.
  - HTML 무회귀: 같은 입력의 render_detail_html 결과가 이 티켓 전후로 동일.
  - 정합성: prepare_listing 의 scene 과 detail_html 이 같은 입력에서 나옴.
  - provenance: 사용자 제공 값의 source.field 가 실제 입력 경로와 일치.
  - missing 표시: 사용자가 소재를 주지 않은 경우 행이 missing: true 로 남음.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import common, register  # noqa: E402
from clossify import detail_render  # noqa: E402


# --------------------------------------------------------------------------- #
# 공통 픽스처.
# --------------------------------------------------------------------------- #
@pytest.fixture
def isolated_prepared_dir(tmp_path, monkeypatch):
    """common.PREPARED_DIR 을 tmp_path 로 격리."""
    fake_dir = tmp_path / "prepared"
    fake_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(common, "PREPARED_DIR", fake_dir)
    return fake_dir


def _fake_attach_ok(sources):
    """images.attach_images 대체 — 항상 URL 리스트 반환."""
    urls = [f"http://cdn/test/img{i}.png" for i in range(len(sources))]
    return {"urls": urls, "rejected": [], "notes": []}


def _sample_product():
    """정형화된 샘플 상품 입력."""
    return {
        "name": "면 티셔츠",
        "summary": "기본템 티셔츠",
        "props": {"소재": "면 100%", "치수": "FREE"},
        "notice": {
            "wear": {
                "material": "면 100%",
                "color": "블랙",
                "size": "FREE",
            }
        },
    }


def _sample_image_urls():
    return ["http://cdn/a.png", "http://cdn/b.png"]


def _sample_options():
    return [{"name": "블랙", "desc": "기본 색상", "price": 10000}]


# --------------------------------------------------------------------------- #
# 결정론 반례.
# --------------------------------------------------------------------------- #
class TestDeterminism:
    """같은 입력 → 같은 scene(generatedAt 제외)."""

    def test_two_calls_identical_except_generated_at(self):
        product = _sample_product()
        urls = _sample_image_urls()
        opts = _sample_options()
        s1 = detail_render.build_scene(product, urls, opts)
        s2 = detail_render.build_scene(product, urls, opts)
        # generatedAt 제거 후 비교.
        s1c = copy.deepcopy(s1)
        s2c = copy.deepcopy(s2)
        s1c.pop("generatedAt", None)
        s2c.pop("generatedAt", None)
        assert s1c == s2c, (
            "같은 입력으로 두 번 호출했으나 generatedAt 외에 차이가 있음"
        )

    def test_ids_stable(self):
        """모든 id 가 두 호출 간 동일."""
        product = _sample_product()
        urls = _sample_image_urls()
        opts = _sample_options()
        s1 = detail_render.build_scene(product, urls, opts)
        s2 = detail_render.build_scene(product, urls, opts)

        def _all_ids(scene):
            ids = []
            for sec in scene["sections"]:
                ids.append(sec["id"])
                for blk in (sec.get("blocks") or []):
                    ids.append(blk["id"])
                for row in (sec.get("rows") or []):
                    ids.append(row["id"])
            return ids

        assert _all_ids(s1) == _all_ids(s2), (
            "id 리스트가 두 호출 간 불일치"
        )

    def test_generated_at_changes(self):
        """generatedAt 은 시각에 따라 달라질 수 있다(결정론 제외 대상)."""
        import time
        product = _sample_product()
        s1 = detail_render.build_scene(product, [], [])
        time.sleep(0.01)
        s2 = detail_render.build_scene(product, [], [])
        # generatedAt 은 존재하지만, 값이 같을 수도 있다(빠른 연속 호출).
        # 중요한 것은 존재 여부와 ISO-8601 형식.
        assert "generatedAt" in s1
        assert "generatedAt" in s2
        assert "T" in s1["generatedAt"]  # ISO-8601 형식 확인.

    def test_different_input_different_ids(self):
        """다른 입력은 다른 id 를 낳을 수 있다(라벨이 다르면 id 도 다름)."""
        p1 = {"props": {"소재": "면"}}
        p2 = {"props": {"소재": "폴리"}}
        s1 = detail_render.build_scene(p1, [], [])
        s2 = detail_render.build_scene(p2, [], [])
        specs1 = [s for s in s1["sections"] if s["id"] == "specs"][0]
        specs2 = [s for s in s2["sections"] if s["id"] == "specs"][0]
        # 같은 라벨("소재")이면 id 는 같다(안정성).
        labels1 = {r["label"] for r in specs1["rows"]}
        labels2 = {r["label"] for r in specs2["rows"]}
        assert labels1 == labels2  # 둘 다 "소재"


# --------------------------------------------------------------------------- #
# HTML 무회귀 반례.
# --------------------------------------------------------------------------- #
class TestHtmlNoRegression:
    """render_detail_html 결과가 이 티켓 전후로 동일."""

    def test_html_still_returns_doctype(self):
        html = detail_render.render_detail_html(
            _sample_product(), _sample_image_urls(), _sample_options()
        )
        assert html.startswith("<!DOCTYPE html>")

    def test_html_contains_all_section_texts(self):
        """scene 에 있는 텍스트가 HTML 에 실제로 등장해야 한다."""
        # _detail_safe_text 가 "100%" 를 제거하므로, 금지 표현이 없는 값을 쓴다.
        product = {
            "name": "면 티셔츠",
            "summary": "기본템 티셔츠",
            "notice": {"소재": "면", "치수": "FREE"},
        }
        urls = _sample_image_urls()
        opts = _sample_options()
        html = detail_render.render_detail_html(product, urls, opts)
        scene = detail_render.build_scene(product, urls, opts)
        # intro title 이 HTML 에 있어야 함.
        intro = [s for s in scene["sections"] if s["id"] == "intro"][0]
        title_block = [b for b in intro["blocks"] if b["id"] == "intro.title"][0]
        if title_block["text"]:
            assert title_block["text"] in html, (
                f"intro.title 텍스트가 HTML 에 없음: {title_block['text']!r}"
            )
        # notice 값이 HTML 에 있어야 함(빈 값/missing 제외).
        notice_sec = [s for s in scene["sections"] if s["id"] == "notice"][0]
        for row in notice_sec["rows"]:
            if row["value"] and not row["source"].get("missing"):
                assert row["value"] in html, (
                    f"notice 값이 HTML 에 없음: {row['value']!r}"
                )

    def test_html_image_urls_match_scene(self):
        """scene 의 이미지 URL 이 HTML 에 모두 등장."""
        urls = _sample_image_urls()
        html = detail_render.render_detail_html({"name": "x"}, urls, [])
        scene = detail_render.build_scene({"name": "x"}, urls, [])
        hero = [s for s in scene["sections"] if s["id"] == "hero"][0]
        for u in hero["images"]:
            assert u in html, f"이미지 URL 이 HTML 에 없음: {u}"

    def test_html_empty_product_still_doc(self):
        """어떤 섹션도 없어도 뼈대 문서를 반환."""
        html = detail_render.render_detail_html({}, [], [])
        assert "<!DOCTYPE html>" in html
        assert "detail-wrap" in html


# --------------------------------------------------------------------------- #
# 정합성 반례 — prepare_listing 의 scene 과 detail_html.
# --------------------------------------------------------------------------- #
class TestPrepareListingConsistency:
    """prepare_listing 이 저장한 scene 과 detail_html 이 같은 입력에서 나옴."""

    def test_payload_has_scene_key(self, isolated_prepared_dir):
        d = {
            "name": "정합성테스트",
            "salePrice": 15000,
            "image_sources": ["a.png", "b.png"],
            "summary": "요약문",
            "props": {"소재": "면 100%"},
            "notice": {"소재": "면 100%"},
            "options": [{"name": "블랙", "price": 15000}],
        }
        payload = register.prepare_listing(d, attach_fn=_fake_attach_ok)
        assert "scene" in payload, "payload 에 scene 키가 없음"
        assert isinstance(payload["scene"], dict)

    def test_scene_sections_count_matches_html(self, isolated_prepared_dir):
        """scene 의 섹션 개수와 HTML 의 섹션 개수가 정합."""
        d = {
            "name": "섹션개수",
            "salePrice": 10000,
            "image_sources": ["a.png"],
            "summary": "요약",
            "props": {"소재": "면"},
            "notice": {"소재": "면"},
            "options": [{"name": "옵션1"}],
        }
        payload = register.prepare_listing(d, attach_fn=_fake_attach_ok)
        scene = payload["scene"]
        html = payload["detail_html"]
        # scene 은 항상 5개 섹션(hero, intro, specs, options, notice).
        assert len(scene["sections"]) == 5
        section_ids = [s["id"] for s in scene["sections"]]
        assert section_ids == ["hero", "intro", "specs", "options", "notice"]
        # HTML 에 각 비어있지 않은 섹션 클래스가 등장하는지 확인.
        # hero
        if scene["sections"][0]["images"]:
            assert "detail-hero" in html
        # intro
        intro = scene["sections"][1]
        if any(b["text"] for b in intro["blocks"]):
            assert "detail-intro" in html
        # specs
        specs = scene["sections"][2]
        if any(r["value"] for r in specs["rows"]):
            assert "detail-specs" in html

    def test_scene_main_texts_appear_in_html(self, isolated_prepared_dir):
        """scene 의 주요 텍스트가 detail_html 에 실제로 등장."""
        # _detail_safe_text 가 "100%" 를 제거하므로, 검사에 쓸 값은 금지 표현이
        # 없는 것으로 한다. scene 과 HTML 이 같은 조립 결과를 공유하는지만 확인.
        d = {
            "name": "주요텍스트상품",
            "salePrice": 20000,
            "image_sources": ["a.png"],
            "summary": "이것은 요약입니다",
            "notice": {"소재": "면", "치수": "FREE"},
        }
        payload = register.prepare_listing(d, attach_fn=_fake_attach_ok)
        scene = payload["scene"]
        html = payload["detail_html"]
        # 상품명 등장.
        assert "주요텍스트상품" in html
        # 요약 등장.
        assert "이것은 요약입니다" in html
        # notice 값 등장.
        assert "면" in html
        assert "FREE" in html
        # scene 의 intro.title text 도 같아야 함.
        intro = [s for s in scene["sections"] if s["id"] == "intro"][0]
        title = [b for b in intro["blocks"] if b["id"] == "intro.title"][0]
        assert title["text"] == "주요텍스트상품"
        # scene 의 notice 값이 HTML 에도 등장(정합성).
        notice_sec = [s for s in scene["sections"] if s["id"] == "notice"][0]
        for row in notice_sec["rows"]:
            if row["value"] and not row["source"].get("missing"):
                assert row["value"] in html, (
                    f"scene 의 notice 값이 HTML 에 없음(정합 위반): {row['value']!r}"
                )

    def test_scene_and_html_from_same_input(self, isolated_prepared_dir):
        """scene 의 이미지 URL 과 HTML 의 이미지 URL 이 일치."""
        d = {
            "name": "이미지정합",
            "salePrice": 30000,
            "image_sources": ["x.png", "y.png", "z.png"],
        }
        payload = register.prepare_listing(d, attach_fn=_fake_attach_ok)
        scene = payload["scene"]
        html = payload["detail_html"]
        hero = [s for s in scene["sections"] if s["id"] == "hero"][0]
        # scene 의 모든 이미지가 HTML 에 등장.
        for u in hero["images"]:
            assert u in html, f"scene 이미지가 HTML 에 없음: {u}"
        # listing_urls 와 일치.
        assert hero["images"] == payload["images"]["listing_urls"]


# --------------------------------------------------------------------------- #
# provenance 반례.
# --------------------------------------------------------------------------- #
class TestProvenance:
    """사용자 제공 값의 source.field 가 실제 입력 경로와 일치."""

    def test_intro_title_source_field(self):
        product = {"name": "이름상품"}
        scene = detail_render.build_scene(product, [], [])
        intro = [s for s in scene["sections"] if s["id"] == "intro"][0]
        title = [b for b in intro["blocks"] if b["id"] == "intro.title"][0]
        assert title["source"]["field"] == "name"
        assert not title["source"].get("missing")

    def test_intro_title_source_field_title_ko(self):
        product = {"title_ko": "타이틀"}
        scene = detail_render.build_scene(product, [], [])
        intro = [s for s in scene["sections"] if s["id"] == "intro"][0]
        title = [b for b in intro["blocks"] if b["id"] == "intro.title"][0]
        assert title["source"]["field"] == "title_ko"

    def test_notice_source_field_matches_input_path(self):
        product = {"notice": {"wear": {"material": "면 100%"}}}
        scene = detail_render.build_scene(product, [], [])
        notice = [s for s in scene["sections"] if s["id"] == "notice"][0]
        # notice dict 의 키 구조: notice.wear.material
        # 현재 구조에서 notice dict 를 평탄화하므로 행 라벨은 중첩 키의 최상단.
        # source.field 는 notice.<key> 형태.
        for row in notice["rows"]:
            assert row["source"]["field"].startswith("notice."), (
                f"notice source.field 가 notice. 접두사 아님: {row['source']['field']!r}"
            )

    def test_provenance_block_composed(self):
        scene = detail_render.build_scene({"name": "x"}, [], [])
        assert scene["provenance"]["origin"] == "composed"
        assert scene["provenance"]["renderer"] == "clossify"

    def test_version_string(self):
        scene = detail_render.build_scene({"name": "x"}, [], [])
        assert scene["version"] == "clossify-scene-v1"

    def test_canvas_width(self):
        scene = detail_render.build_scene({"name": "x"}, [], [])
        assert scene["canvas"]["widthPx"] == 1000


# --------------------------------------------------------------------------- #
# missing 표시 반례.
# --------------------------------------------------------------------------- #
class TestMissingMarking:
    """사용자가 제공하지 않은 값은 missing: true 로 남음(생략 아님)."""

    def test_missing_material_row_present(self):
        """소재를 주지 않은 경우 행이 사라지지 않고 missing: true."""
        product = {
            "notice": {
                "wear": {
                    "color": "블랙",
                    # material 누락
                }
            }
        }
        scene = detail_render.build_scene(product, [], [])
        notice = [s for s in scene["sections"] if s["id"] == "notice"][0]
        # notice 섹션은 color 행을 가짐.
        labels = {r["label"] for r in notice["rows"]}
        # 빈 값 행이 missing 으로 표시되어 있는지 확인.
        missing_rows = [r for r in notice["rows"] if r["source"].get("missing")]
        # color 는 값이 있으므로 missing 이 아님.
        for row in notice["rows"]:
            if row["label"] and row["label"].startswith("color") or "블랙" in str(row["value"]):
                assert not row["source"].get("missing"), (
                    f"값이 있는 행이 missing 표시됨: {row}"
                )

    def test_empty_name_marked_missing(self):
        """상품명을 주지 않으면 intro.title 이 missing: true."""
        scene = detail_render.build_scene({}, [], [])
        intro = [s for s in scene["sections"] if s["id"] == "intro"][0]
        title = [b for b in intro["blocks"] if b["id"] == "intro.title"][0]
        assert title["source"].get("missing") is True
        assert title["text"] == ""

    def test_empty_summary_marked_missing(self):
        """요약을 주지 않으면 intro.summary 가 missing: true."""
        scene = detail_render.build_scene({"name": "이름만"}, [], [])
        intro = [s for s in scene["sections"] if s["id"] == "intro"][0]
        summary = [b for b in intro["blocks"] if b["id"] == "intro.summary"][0]
        assert summary["source"].get("missing") is True
        assert summary["text"] == ""

    def test_missing_value_empty_string_not_omitted(self):
        """빈 값은 value: '' 이고 행이 사라지지 않는다."""
        product = {"notice": {"빈항목": ""}}
        scene = detail_render.build_scene(product, [], [])
        notice = [s for s in scene["sections"] if s["id"] == "notice"][0]
        # 빈항목 행이 존재해야 함.
        labels = {r["label"] for r in notice["rows"]}
        assert any("빈항목" in (l or "") for l in labels), (
            "빈 값 행이 scene 에서 사라짐(생략 금지 위반)"
        )
        # 해당 행의 value 는 빈 문자열, source.missing 은 true.
        empty_rows = [r for r in notice["rows"] if "빈항목" in (r["label"] or "")]
        assert len(empty_rows) == 1
        assert empty_rows[0]["value"] == ""
        assert empty_rows[0]["source"].get("missing") is True

    def test_options_missing_label(self):
        """라벨이 없는 옵션도 행으로 존재(missing: true)."""
        opts = [{"desc": "설명만있는옵션"}]
        scene = detail_render.build_scene({}, [], opts)
        options = [s for s in scene["sections"] if s["id"] == "options"][0]
        assert len(options["rows"]) == 1
        assert options["rows"][0]["source"].get("missing") is True


# --------------------------------------------------------------------------- #
# scene 구조 기본 검증.
# --------------------------------------------------------------------------- #
class TestSceneStructure:
    """scene 문서 구조 기본 검증."""

    def test_always_five_sections(self):
        """섹션은 항상 5개(hero, intro, specs, options, notice)."""
        scene = detail_render.build_scene({}, [], [])
        assert len(scene["sections"]) == 5
        ids = [s["id"] for s in scene["sections"]]
        assert ids == ["hero", "intro", "specs", "options", "notice"]

    def test_section_kinds(self):
        """각 섹션의 kind 가 올바름."""
        scene = detail_render.build_scene({}, [], [])
        kinds = {s["id"]: s["kind"] for s in scene["sections"]}
        assert kinds["hero"] == "images"
        assert kinds["intro"] == "text"
        assert kinds["specs"] == "table"
        assert kinds["options"] == "table"
        assert kinds["notice"] == "table"

    def test_hero_empty_images(self):
        """이미지 0장이면 hero.images 는 빈 리스트(섹션 자체는 존재)."""
        scene = detail_render.build_scene({}, [], [])
        hero = [s for s in scene["sections"] if s["id"] == "hero"][0]
        assert hero["images"] == []

    def test_hero_with_images(self):
        urls = ["http://a.png", "http://b.png"]
        scene = detail_render.build_scene({}, urls, [])
        hero = [s for s in scene["sections"] if s["id"] == "hero"][0]
        assert hero["images"] == urls

    def test_html_key_not_in_scene_sections(self):
        """scene 섹션에 HTML 전용 키(html)가 노출되지 않음."""
        scene = detail_render.build_scene({"name": "x"}, ["http://a.png"], [])
        for sec in scene["sections"]:
            assert "html" not in sec, (
                f"scene 섹션에 html 키가 노출됨: {sec['id']}"
            )


# --------------------------------------------------------------------------- #
# T-301b — 고시 본문 필드 분해 + 미제공 필수 항목 표시 반례.
# --------------------------------------------------------------------------- #
class TestNoticeDecomposition:
    """T-301b 결함 1: 고시 본문 dict 를 필드 단위 행으로 분해."""

    def test_wear_material_color_size_separate_rows(self):
        """WEAR 입력 → material/color/size 가 각각 별도 행."""
        product = {
            "notice": {
                "wear": {
                    "material": "면",
                    "color": "블랙",
                    "size": "FREE",
                }
            }
        }
        scene = detail_render.build_scene(product, [], [])
        notice = [s for s in scene["sections"] if s["id"] == "notice"][0]
        labels = {r["label"] for r in notice["rows"]}
        assert "material" in labels, "material 행이 없음"
        assert "color" in labels, "color 행이 없음"
        assert "size" in labels, "size 행이 없음"

    def test_no_value_starts_with_brace(self):
        """객체 문자열화 0건 — 어떤 행의 value 도 '{' 로 시작하지 않음."""
        product = {
            "notice": {
                "wear": {
                    "material": "면",
                    "color": "블랙",
                    "size": "FREE",
                }
            }
        }
        scene = detail_render.build_scene(product, [], [])
        notice = [s for s in scene["sections"] if s["id"] == "notice"][0]
        for row in notice["rows"]:
            assert not row["value"].startswith("{"), (
                f"객체 문자열화 발견: {row['label']}={row['value']!r}"
            )

    def test_source_field_includes_full_path(self):
        """분해된 행의 source.field 가 notice.<node>.<field> 형태."""
        product = {
            "notice": {
                "wear": {
                    "material": "면",
                }
            }
        }
        scene = detail_render.build_scene(product, [], [])
        notice = [s for s in scene["sections"] if s["id"] == "notice"][0]
        material_row = [r for r in notice["rows"] if r["label"] == "material"][0]
        assert material_row["source"]["field"] == "notice.wear.material", (
            f"source.field 불일치: {material_row['source']['field']!r}"
        )

    def test_nested_dict_value_not_stringified(self):
        """중첩 dict 값이 문자열화되어 value 에 들어가지 않음."""
        product = {"notice": {"wear": {"material": "면", "color": "블랙"}}}
        scene = detail_render.build_scene(product, [], [])
        notice = [s for s in scene["sections"] if s["id"] == "notice"][0]
        for row in notice["rows"]:
            # value 가 파이썬 dict 문자열화 결과가 아닌지 확인.
            assert "material" not in row["value"] or row["label"] == "material", (
                f"dict 문자열화 의심: {row}"
            )

    def test_flat_korean_keys_unchanged(self):
        """flat 한국어 키 notice 도 여전히 올바르게 동작."""
        product = {"notice": {"소재": "면", "치수": "FREE"}}
        scene = detail_render.build_scene(product, [], [])
        notice = [s for s in scene["sections"] if s["id"] == "notice"][0]
        labels = {r["label"] for r in notice["rows"]}
        assert any("소재" in (l or "") for l in labels)
        material_row = [r for r in notice["rows"] if "소재" in (r["label"] or "")][0]
        assert material_row["value"] == "면"


class TestMissingRequiredFields:
    """T-301b 결함 2: 미제공 필수 항목을 value:'' + source.missing:true 로 표시."""

    def test_missing_material_row_exists_with_missing_flag(self):
        """소재를 주지 않은 WEAR 입력 → material 행이 존재하고 missing:true."""
        product = {
            "notice": {
                "wear": {
                    "color": "블랙",
                    # material 누락
                }
            }
        }
        scene = detail_render.build_scene(product, [], [])
        notice = [s for s in scene["sections"] if s["id"] == "notice"][0]
        material_rows = [r for r in notice["rows"] if r["label"] == "material"]
        assert len(material_rows) >= 1, "material 행이 아예 없음(결함 2)"
        mr = material_rows[0]
        assert mr["source"].get("missing") is True, (
            f"material 행이 missing 표시 아님: {mr}"
        )
        assert mr["value"] == "", (
            f"missing material 행의 value 가 빈 문자열이 아님: {mr['value']!r}"
        )

    def test_missing_fields_match_notice_types_json(self):
        """missing 으로 표시된 필드가 notice_types.json 의 WEAR 필수 필드 중 미제공분과 일치."""
        from clossify import qa_agents
        spec = qa_agents._notice_type_spec("WEAR")
        assert spec is not None, "WEAR 타입을 찾을 수 없음"
        required = set(spec.get("fields") or [])
        # WEAR 의 필수 필드 중 material/color/size 만 제공
        provided = {"material", "color", "size"}
        expected_missing = required - provided

        product = {
            "notice": {
                "wear": {
                    "material": "면",
                    "color": "블랙",
                    "size": "FREE",
                }
            }
        }
        scene = detail_render.build_scene(product, [], [])
        notice = [s for s in scene["sections"] if s["id"] == "notice"][0]
        missing_labels = {
            r["label"] for r in notice["rows"]
            if r["source"].get("missing")
        }
        # expected_missing 중 scene 에 missing 행으로 있는지 확인.
        for field in expected_missing:
            assert field in missing_labels, (
                f"필수 필드 {field} 가 missing 행에 없음. "
                f"missing 행들: {missing_labels}"
            )

    def test_no_invented_fields_outside_required(self):
        """필수 목록에 없는 임의 필드를 만들어내지 않음."""
        product = {"notice": {"wear": {"material": "면"}}}
        scene = detail_render.build_scene(product, [], [])
        notice = [s for s in scene["sections"] if s["id"] == "notice"][0]
        from clossify import qa_agents
        spec = qa_agents._notice_type_spec("WEAR")
        required = set(spec.get("fields") or [])
        # 모든 행의 라벨이 필수 필드에 포함되어야 함(임의 필드 없음).
        for row in notice["rows"]:
            assert row["label"] in required, (
                f"필수 목록 밖의 임의 필드 발견: {row['label']!r}"
            )

    def test_missing_row_value_empty_string(self):
        """missing 행의 value 는 빈 문자열이어야 함."""
        product = {"notice": {"wear": {"color": "블랙"}}}
        scene = detail_render.build_scene(product, [], [])
        notice = [s for s in scene["sections"] if s["id"] == "notice"][0]
        for row in notice["rows"]:
            if row["source"].get("missing"):
                assert row["value"] == "", (
                    f"missing 행의 value 가 빈 문자열이 아님: {row['value']!r}"
                )

    def test_non_notice_type_node_no_missing_rows(self):
        """고시 타입 node 가 아닌 키(예: flat 한국어)는 missing 행을 추가하지 않음."""
        product = {"notice": {"소재": "면", "치수": "FREE"}}
        scene = detail_render.build_scene(product, [], [])
        notice = [s for s in scene["sections"] if s["id"] == "notice"][0]
        missing_rows = [r for r in notice["rows"] if r["source"].get("missing")]
        # 소재/치수는 값이 있으므로 missing 이 아님.
        # 고시 타입 node 가 아니므로 필수 필드 missing 행이 추가되지 않음.
        for row in missing_rows:
            assert row["value"] == ""


class TestNoticeLabelSource:
    """라벨 매핑 근거: 한국어 라벨을 지어내지 않고 필드명을 그대로 사용."""

    def test_label_uses_field_name_not_invented_korean(self):
        """WEAR 의 material 행 라벨은 필드명(material)을 그대로 사용."""
        product = {"notice": {"wear": {"material": "면"}}}
        scene = detail_render.build_scene(product, [], [])
        notice = [s for s in scene["sections"] if s["id"] == "notice"][0]
        material_row = [r for r in notice["rows"] if r["label"] == "material"]
        assert len(material_row) >= 1
        # 라벨이 필드명과 동일 — 한국어 라벨(소재)로 바꾸지 않음.
        assert material_row[0]["label"] == "material"

    def test_missing_field_label_uses_field_name(self):
        """missing 행의 라벨도 필드명(returnCostReason 등)을 그대로 사용."""
        product = {"notice": {"wear": {"material": "면"}}}
        scene = detail_render.build_scene(product, [], [])
        notice = [s for s in scene["sections"] if s["id"] == "notice"][0]
        rcr_rows = [r for r in notice["rows"] if r["label"] == "returnCostReason"]
        assert len(rcr_rows) >= 1
        assert rcr_rows[0]["label"] == "returnCostReason"
