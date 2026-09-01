# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""store_url_slug 선택화 계약을 고정하는 시험 (슬러그 선택화).

근거(실측 2026-08-31): 슬러그는 naver_client.py·register.py·preview.py 어디에서도
쓰이지 않는다(grep 0건). 인증 서명은 client_id+client_secret 만 사용. 따라서
슬러그 부재가 어떤 기능도 막지 않아야 한다.

워크오더 인수조건 대응:
  (a) 2키(client_id·client_secret) 설정 → ``ok=True``, ``missing=[]``,
      ``optional_absent=["store_url_slug"]``.
  (b) 3키 설정 → 기존과 동일(회귀): ``ok=True``, ``present.store_url_slug=True``,
      ``optional_absent=[]``.
  (c) 슬러그 없는 상태로 ``register_product`` 경로가 **슬러그 때문에** 죽지 않는다
      (COMMERCE_DRY_RUN=1, 네이버 호출은 mock — 다른 사유의 실패는 무방).
  (d) 설정 폼 HTML 이 슬러그 칸에 ``[선택]``, 자격증명 2칸에 ``[필수]`` 를 표시.

네트워크 금지 — 네이버 API 호출 경로는 mock. 사용자 설정 파일(.local/config.json)
읽기·쓰기 금지 — 임시 설정만 쓴다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import config_form_server, mcp_server, naver_client

# 의류 카테고리 (KC 불필요, WEAR 고시 타입) — test_n21a 와 동일 값.
_CLOTHING_CATEGORY = "50021299"


def _write_config(tmp_path: Path, naver: dict) -> Path:
    """임시 설정 파일을 쓰고 경로를 반환 (.local/config.json 을 건드리지 않는다).

    config.example.json 은 자리표시자 원본 보존용으로 참조만 하고, 여기서는
    인수조건에 맞는 naver 섹션을 담은 완전히 별도의 임시 파일을 만든다.
    """
    cfg = {
        "naver": naver,
        "smartstore_notice_defaults": {
            "origin_area_code": "04",
            "origin_content": "국산",
        },
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


@pytest.fixture
def two_key_env(tmp_path: Path, monkeypatch) -> Path:
    """인수조건 (a): 슬러그 없는 2키 설정."""
    path = _write_config(
        tmp_path,
        {"client_id": "real-id-123", "client_secret": "real-secret-456"},
    )
    monkeypatch.setenv("CLOSSIFY_CONFIG", str(path))
    return path


@pytest.fixture
def three_key_env(tmp_path: Path, monkeypatch) -> Path:
    """인수조건 (b): 슬러그 있는 3키 설정 (회귀 확인용)."""
    path = _write_config(
        tmp_path,
        {
            "client_id": "real-id-123",
            "client_secret": "real-secret-456",
            "store_url_slug": "my-store",
        },
    )
    monkeypatch.setenv("CLOSSIFY_CONFIG", str(path))
    return path


# =========================================================================== #
# (a) 2키 설정 → ok=True
# =========================================================================== #
class TestTwoKeyConfigIsComplete:
    def test_ok_true_with_missing_slug(self, two_key_env: Path) -> None:
        """슬러그가 없어도 ok=True 다 (워크오더 Goal)."""
        result = mcp_server.check_config()
        assert result.get("ok") is True, f"슬러그 부재인데 ok=False: {result}"

    def test_missing_empty(self, two_key_env: Path) -> None:
        """missing 은 비어 있다 — 슬러그는 missing 에 들어가지 않는다."""
        result = mcp_server.check_config()
        assert result.get("missing") == [], f"missing 에 슬러그가 잡힘: {result.get('missing')}"

    def test_placeholders_empty(self, two_key_env: Path) -> None:
        """placeholders 도 비어 있다."""
        result = mcp_server.check_config()
        assert result.get("placeholders") == []

    def test_optional_absent_names_slug(self, two_key_env: Path) -> None:
        """부재는 optional_absent 힌트로만 드러난다 (ok 를 깎지 않는다)."""
        result = mcp_server.check_config()
        assert result.get("optional_absent") == [
            "store_url_slug"
        ], f"optional_absent 불일치: {result.get('optional_absent')}"

    def test_present_map(self, two_key_env: Path) -> None:
        """present: 필수 2키 True, 슬러그 False."""
        result = mcp_server.check_config()
        present = result.get("present") or {}
        assert present.get("client_id") is True
        assert present.get("client_secret") is True
        assert present.get("store_url_slug") is False


# =========================================================================== #
# (b) 3키 설정 → 기존 동작 회귀 없음
# =========================================================================== #
class TestThreeKeyConfigRegression:
    def test_ok_true_slug_present(self, three_key_env: Path) -> None:
        """3키 설정은 기존처럼 ok=True."""
        result = mcp_server.check_config()
        assert result.get("ok") is True

    def test_slug_present_true_and_optional_absent_empty(self, three_key_env: Path) -> None:
        """값이 있는 슬러그는 present 에 보존되고 optional_absent 는 비어 있다."""
        result = mcp_server.check_config()
        present = result.get("present") or {}
        assert present.get("store_url_slug") is True, f"슬러그 값이 무시됨: {present}"
        assert result.get("optional_absent") == []
        assert result.get("missing") == []
        assert result.get("placeholders") == []


# =========================================================================== #
# (c) register_product 경로 — 슬러그 부재가 사유가 되지 않는다
# =========================================================================== #
class TestRegisterProductIgnoresSlugAbsence:
    def test_register_not_blocked_by_slug(self, two_key_env: Path, monkeypatch) -> None:
        """슬러그 없이 register_product 를 돌려도 '슬러그 부재' 가 원인이면 안 된다.

        COMMERCE_DRY_RUN=1 + 네이버 호출 mock. 다른 사유(원산지 등)의 실패는
        무방하다 — 반드시 슬러그/store_url_slug 가 사유로 지목되지 않는지만 본다.
        예외로 죽는 것도 슬러그 기원이면 안 된다.
        """
        monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
        naver_calls: list = []
        with mock.patch.object(
            naver_client,
            "register_product",
            side_effect=lambda *a, **kw: naver_calls.append((a, kw)) or (200, {}),
        ):
            result = mcp_server.register_product(
                name="테스트니트",
                price=30000,
                image_urls=["http://cdn/x.png"],
                category_id=_CLOTHING_CATEGORY,
                detail_html="<html><body>상세</body></html>",
                preview_confirmed=True,
            )

        # 응답이 dict 로 살아 돌아와야 한다 (슬러그 부재로 예외 사망 금지).
        assert isinstance(result, dict), f"register_product 가 dict 가 아님: {type(result)}"
        blob = json.dumps(result, ensure_ascii=False)
        # 슬러그가 거부 사유으로 지목되면 실패.
        assert "store_url_slug" not in blob, f"슬러그 부재가 사유로 지목됨: {blob}"
        assert "슬러그" not in blob, f"슬러그 부재가 사유로 지목됨(한글): {blob}"
        assert "slug" not in blob.lower(), f"slug 가 사유로 지목됨: {blob}"


# =========================================================================== #
# (d) 폼 — 슬러그 칸 [선택], 자격증명 2칸 [필수]
# =========================================================================== #
class TestFormMarksSlugOptional:
    def test_form_slug_field_optional(self) -> None:
        """슬러그 칸은 [선택] 표시, 칸 자체는 유지된다."""
        doc = config_form_server.render_config_form_html(token="t", port=1)
        # 칸 유지 — 삭제되면 안 된다.
        assert 'name="store_url_slug"' in doc, "슬러그 입력 칸이 사라졌다 (칸 유지 계약 위반)"
        # 라벨은 [선택].
        assert 'for="f-store_url_slug"' in doc
        slug_label = doc.split('for="f-store_url_slug"', 1)[1][:200]
        assert "[선택]" in slug_label, f"슬러그 라벨에 [선택] 없음: {slug_label!r}"

    def test_form_credential_fields_required(self) -> None:
        """자격증명 2칸은 여전히 [필수]."""
        doc = config_form_server.render_config_form_html(token="t", port=1)
        for field in ("client_id", "client_secret"):
            chunk = doc.split(f'for="f-{field}"', 1)[1][:200]
            assert "[필수]" in chunk, f"{field} 라벨에 [필수] 없음: {chunk!r}"


# =========================================================================== #
# 안전장치 — 이 시험 파일 자체가 사용자 설정을 읽지 않는지
# =========================================================================== #
def test_no_local_config_read(two_key_env: Path) -> None:
    """모든 check_config 호출이 임시 설정을 본다 (monkeypatch 환경 정상 작동 확인).

    .local/config.json 을 읽었다면 ok/missing 이 기계마다 달라진다. 여기서는
    임시 2키 설정의 결정적 결과가 나오는지로 확인한다.
    """
    result = mcp_server.check_config()
    assert result.get("ok") is True
    assert result.get("optional_absent") == ["store_url_slug"]
