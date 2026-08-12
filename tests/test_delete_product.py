# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""``delete_product`` MCP 도구 검증.

파괴 동작(되돌릴 수 없는 단건 삭제) 이므로 안전장치를 엄격하게 검증한다:

  (a) ``confirm=True`` 로 확정된 삭제는 네이버 DELETE 엔드포인트를 *정확히 1회*
      호출하고, 성공을 보고한다.
  (b) ``confirm`` 이 명시적으로 ``True`` 가 아니면 네이버 API 호출이 0회,
      거부 사유에 "permanent"(되돌릴 수 없음) 가 명시된다.
  (c) 빈 값/비숫자 상품번호는 네이버 API 호출 0회 로 거부된다.
  (d) 비 2xx 응답은 조용히 삼키지 않고 실패(``ok=False``) 로 보고한다.
  (e) 삭제 성공 후 같은 ``origin_product_no`` 의 로컬 등록 기록
      (``registration_record.json``) 이 지워진다. 기록이 애초에 없어도 오류가
      아니다(``registration_record_removed=False``, ``ok=True``).

``COMMERCE_DRY_RUN`` 은 끈 상태로, 실제 네이버 HTTP 호출은 mock 으로 차단하고
호출 횟수를 센다.
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

from clossify import common, mcp_server, naver_client
from clossify import register as register_mod

# ============================================================================
# 공통 픽스처·헬퍼.
# ============================================================================


@pytest.fixture
def isolated_prepared_dir(tmp_path, monkeypatch):
    """``common.PREPARED_DIR`` 을 tmp_path 로 격리.

    ``register_mod._prepared_dir()`` 는 호출 시점에 ``Path(common.PREPARED_DIR)``
    을 읽으므로, monkeypatch 가 테스트 동안 유효하다.
    """
    fake_dir = tmp_path / "prepared"
    fake_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(common, "PREPARED_DIR", fake_dir)
    return fake_dir


def _write_record_for_origin(
    prepared_dir: Path,
    *,
    product_key: str,
    origin_product_no: str,
    channel_product_no: str = "CH-DEL-1",
) -> Path:
    """주어진 origin_product_no 로 로컬 등록 기록 파일을 만든다.

    삭제 테스트 (e) 에서 기록 삭제를 검증하려면 기록 파일이 디스크에 있어야
    한다. ``register_mod.write_registration_record`` 과 동일한 디렉터리 규약을
    따른다(``prepared/<key>/registration_record.json``).
    """
    record = {
        "product_key": product_key,
        "origin_product_no": origin_product_no,
        "channel_product_no": channel_product_no,
        "name": "삭제테스트상품",
        "salePrice": 10000,
        "categoryId": "99999999",
        "requested_status": "SALE",
        "applied_status": "SALE",
        "registered_at": "2026-08-05T00:00:00+00:00",
    }
    record_path = register_mod._registration_record_path(product_key)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    common._write_json_file(record_path, record)
    return record_path


# ============================================================================
# (a) 확정된 삭제는 정확히 1회 의 DELETE 호출, 성공 보고.
# ============================================================================
class TestConfirmedDeleteIssuesSingleCall:
    def test_one_delete_call_and_success_reported(self, isolated_prepared_dir, monkeypatch):
        """``confirm=True`` → delete_origin_product 1회 호출, ``ok=True``."""
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
        delete_calls: list[str] = []

        def _fake_delete(origin_product_no, tk=None):
            delete_calls.append(origin_product_no)
            return 200, {"data": True}

        monkeypatch.setattr(naver_client, "delete_origin_product", _fake_delete)

        result = mcp_server.delete_product("12345", confirm=True)

        assert (
            len(delete_calls) == 1
        ), f"확정된 삭제는 DELETE 를 정확히 1회 호출해야 한다: {len(delete_calls)}회"
        assert delete_calls[0] == "12345"
        assert result["ok"] is True
        assert result["status_code"] == 200
        assert result["origin_product_no"] == "12345"


# ============================================================================
# (b) 미확정 삭제는 네이버 API 호출 0회, 거부 사유에 "permanent".
# ============================================================================
class TestUnconfirmedDeleteMakesNoCall:
    def test_no_api_call_and_permanent_in_reason(self, isolated_prepared_dir, monkeypatch):
        """``confirm`` 누락/False → API 호출 0회, 에러에 "permanent" 명시."""
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
        delete_calls: list[str] = []

        def _fake_delete(origin_product_no, tk=None):
            delete_calls.append(origin_product_no)
            return 200, {"data": True}

        monkeypatch.setattr(naver_client, "delete_origin_product", _fake_delete)

        # 1) confirm 생략(기본값 False).
        result_default = mcp_server.delete_product("12345")
        assert result_default["ok"] is False
        assert result_default["status_code"] is None
        assert "permanent" in result_default["error"], (
            "거부 사유는 삭제가 되돌릴 수 없음(permanent) 을 명시해야 한다 — "
            "모델이 의도를 추론해 삭제하는 것을 막기 위함이다."
        )

        # 2) confirm=False 명시.
        result_false = mcp_server.delete_product("12345", confirm=False)
        assert result_false["ok"] is False
        assert "permanent" in result_false["error"]

        # 3) truthy 값(비어있지 않은 문자열 등)은 ``is True`` 검사로 차단.
        result_truthy = mcp_server.delete_product("12345", confirm="yes")
        assert result_truthy["ok"] is False, (
            "confirm 은 ``is True`` 로 검사해야 한다 — truthy 값(문자열 등)이 "
            "우연히 승인되면 안 된다."
        )

        # API 는 한 건도 호출되지 않는다.
        assert (
            len(delete_calls) == 0
        ), f"미확정 삭제는 네이버 API 호출이 0회여야 한다: {len(delete_calls)}회"


# ============================================================================
# (c) 빈 값/비숫자 상품번호는 네이버 API 호출 0회.
# ============================================================================
class TestInvalidNumberMakesNoCall:
    @pytest.mark.parametrize(
        "bad_no",
        [
            "",
            "   ",
            None,
            "abc",
            "12a45",
            "  ",
            "12.3",
            "#12345",
            "----",
            "origin-123",
        ],
    )
    def test_non_numeric_rejected_without_api_call(
        self, isolated_prepared_dir, monkeypatch, bad_no
    ):
        """빈 값/비숫자 상품번호는 입력 검증 단계에서 거부(네이버 호출 0회)."""
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
        delete_calls: list[str] = []

        def _fake_delete(origin_product_no, tk=None):
            delete_calls.append(origin_product_no)
            return 200, {"data": True}

        monkeypatch.setattr(naver_client, "delete_origin_product", _fake_delete)

        result = mcp_server.delete_product(bad_no, confirm=True)
        assert result["ok"] is False
        assert result["status_code"] is None
        assert len(delete_calls) == 0, (
            f"비숫자 상품번호 {bad_no!r} 는 네이버 API 호출 0회 로 거부되어야 한다: "
            f"{len(delete_calls)}회"
        )


# ============================================================================
# (d) 비 2xx 응답은 조용히 삼키지 않고 실패로 보고.
# ============================================================================
class TestNon2xxReportedAsFailure:
    @pytest.mark.parametrize(
        "status, body",
        [
            (404, {"code": "NOT_FOUND", "message": "product not found"}),
            (400, {"code": "BAD_REQUEST", "message": "invalid id"}),
            (500, "internal server error"),
            (403, {"code": "FORBIDDEN"}),
        ],
    )
    def test_failure_not_swallowed(self, isolated_prepared_dir, monkeypatch, status, body):
        """비 2xx 응답 → ``ok=False``, status_code·error 가 채워진다."""
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)

        def _fake_delete(origin_product_no, tk=None):
            return status, body

        monkeypatch.setattr(naver_client, "delete_origin_product", _fake_delete)

        result = mcp_server.delete_product("12345", confirm=True)
        assert result["ok"] is False, f"비 2xx({status}) 응답을 조용히 성공으로 삼키면 안 된다."
        assert result["status_code"] == status
        assert result["error"] is not None
        # 에러 메시지가 상태 코드를 언급한다 — 조용한 삼킴 방지.
        assert (
            str(status) in result["error"]
        ), "에러 메시지는 HTTP 상태 코드를 명시해야 한다 (조용한 삼킴 금지)."


# ============================================================================
# (e) 로컬 등록 기록 정리: 성공 후 기록 파일이 지워진다. 부재는 오류 아님.
# ============================================================================
class TestRegistrationRecordRemoved:
    def test_record_file_removed_after_successful_delete(self, isolated_prepared_dir, monkeypatch):
        """삭제 성공 → ``registration_record.json`` 이 디스크에서 사라진다."""
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)

        def _fake_delete(origin_product_no, tk=None):
            return 200, {"data": True}

        monkeypatch.setattr(naver_client, "delete_origin_product", _fake_delete)

        # 기록 파일을 디스크에 만든다.
        origin_no = "987654"
        pkey = "testkeydel0001"
        record_path = _write_record_for_origin(
            isolated_prepared_dir,
            product_key=pkey,
            origin_product_no=origin_no,
        )
        assert record_path.exists(), "전제조건: 기록 파일이 있어야 한다"

        result = mcp_server.delete_product(origin_no, confirm=True)

        assert result["ok"] is True, "삭제 자체는 성공해야 한다(로컬 기록 정리 성공 여부와 무관)."
        assert (
            result["registration_record_removed"] is True
        ), "삭제 성공 후 로컬 등록 기록이 지워졌으면 removed=True 여야 한다."
        assert not record_path.exists(), (
            "성공한 삭제 후에는 기록 파일이 디스크에서 사라져야 한다 — "
            "저장된 기록이 삭제된 listing 보다 오래 남으면 안 된다."
        )

    def test_absent_record_is_not_an_error(self, isolated_prepared_dir, monkeypatch):
        """기록이 애초에 없어도 삭제 성공은 그대로, removed=False (오류 아님)."""
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)

        def _fake_delete(origin_product_no, tk=None):
            return 200, {"data": True}

        monkeypatch.setattr(naver_client, "delete_origin_product", _fake_delete)

        # 기록 파일을 만들지 않은 상태에서 삭제.
        origin_no = "555000"
        result = mcp_server.delete_product(origin_no, confirm=True)

        assert result["ok"] is True, "로컬 기록이 없어도 네이버 삭제 성공이면 ok=True 여야 한다."
        assert (
            result["registration_record_removed"] is False
        ), "기록이 애초에 없으면 removed=False (False 는 오류가 아니다)."
        # error 는 None 이어야 한다 — 부재가 오류가 아니므로.
        assert result["error"] is None


# ============================================================================
# (f) WO PR #27 6라운드 ② — DELETE 재시도 후 404 멱등 성공.
#
# ``delete_origin_product`` 는 ``allow_retry=True`` 로 401+GW.AUTHN 시 1회
# 재시도한다. 첫 DELETE 가 서버에서는 성공했는데 게이트웨이 응답만 401 로
# 왔을 수 있다. 재시도는 "이미 없음"(404) 을 받고, ``_api_request`` 는 그 두
# 번째 응답을 돌려준다. 원격은 지워졌는데 "삭제 실패"로 보고하면 로컬·원격
# 상태가 어긋난다 — 재시도 후 404 는 멱등 성공으로 인정한다.
# ============================================================================
class TestDeleteRetryIdempotency:
    """재시도 후 404 = 멱등 성공, 재시도 없는 404 = 여전히 실패."""

    def test_404_after_retry_is_success(self, isolated_prepared_dir, monkeypatch):
        """재시도 후 404 → ``ok=True`` (첫 DELETE 성공, 재시도가 404 수신).

        시나리오:
          1. ``delete_product(confirm=True)`` 호출.
          2. ``delete_origin_product`` 모크: ``_AUTHN_RETRY_EVENTS`` 에 1행
             추가(재시도 발생 시뮬레이션) 후 404 반환.
          3. ``delete_product`` 는 재시도 후 404 를 멱등 성공으로 인정해야 한다.
        """
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
        # 테스트 간 격리 — 재시도 이벤트 버퍼 비움.
        naver_client._AUTHN_RETRY_EVENTS.clear()

        def _fake_delete_with_retry(origin_product_no, tk=None):
            # 재시도가 일어났음을 시뮬레이션 — 1행 추가.
            naver_client._AUTHN_RETRY_EVENTS.append(
                {"url": f"delete/{origin_product_no}", "retried": True}
            )
            # 재시도 결과로 404 수신 (첫 DELETE 가 성공했으므로 이미 없음).
            return 404, {"code": "NOT_FOUND", "message": "product not found"}

        monkeypatch.setattr(naver_client, "delete_origin_product", _fake_delete_with_retry)

        result = mcp_server.delete_product("12345", confirm=True)

        assert result["ok"] is True, (
            "재시도 후 404 는 멱등 성공이어야 한다 — 첫 DELETE 가 서버에서 "
            "성공했고 재시도가 '이미 없음'을 받은 경우다. ok=False 로 보고하면 "
            "로컬·원격 상태가 어긋난다(원격은 지워졌는데 로컬은 실패로 기록)."
        )
        assert result["status_code"] == 404, (
            "status_code 는 실제 응답(404) 을 그대로 보고한다 — 멱등 성공이지만 "
            "원격 응답이 404 였음을 투명하게 남긴다."
        )

    def test_404_without_retry_is_still_failure(self, isolated_prepared_dir, monkeypatch):
        """재시도 없는 404 → ``ok=False`` (원래 없던 상품을 지우려 한 경우).

        ``_AUTHN_RETRY_EVENTS`` 에 변화가 없으면 재시도가 일어나지 않은 것이다.
        이때 404 는 "애초에 존재하지 않았음" 이므로 여전히 실패로 보고한다.
        """
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
        naver_client._AUTHN_RETRY_EVENTS.clear()

        def _fake_delete_no_retry(origin_product_no, tk=None):
            # 재시도 이벤트 추가 없이 404 반환.
            return 404, {"code": "NOT_FOUND", "message": "product not found"}

        monkeypatch.setattr(naver_client, "delete_origin_product", _fake_delete_no_retry)

        result = mcp_server.delete_product("12345", confirm=True)

        assert result["ok"] is False, (
            "재시도 없는 404 는 여전히 실패다 — '원래 없던 상품'과 '방금 지운 "
            "상품'을 구별할 수 없으므로, 재시도가 있을 때만 멱등 성공으로 인정한다."
        )

    def test_500_after_retry_is_still_failure(self, isolated_prepared_dir, monkeypatch):
        """재시도 후 500 → ``ok=False`` (404 가 아닌 비 2xx 는 멱등 성공 아님).

        재시도가 일어나도 500 은 서버 오류 — 삭제 성공으로 간주할 수 없다.
        """
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
        naver_client._AUTHN_RETRY_EVENTS.clear()

        def _fake_delete_500_with_retry(origin_product_no, tk=None):
            naver_client._AUTHN_RETRY_EVENTS.append(
                {"url": f"delete/{origin_product_no}", "retried": True}
            )
            return 500, {"code": "INTERNAL", "message": "server error"}

        monkeypatch.setattr(naver_client, "delete_origin_product", _fake_delete_500_with_retry)

        result = mcp_server.delete_product("12345", confirm=True)

        assert (
            result["ok"] is False
        ), "재시도 후 500 은 여전히 실패다 — 멱등 성공 인정은 404 에 한한다."


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
