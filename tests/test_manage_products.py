# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""N27+N64 manage_products 도구 테스트.

모든 테스트는 실제 네이버 API 를 호출하지 않는다 — monkeypatch/mock 으로
naver_client 함수를 대체한다. 검증 항목:

  (a) list: 상품 목록 + HTML 패널 생성; HTML 은 정적(``<script>``·``<button>``·
      ``<input>``·``contenteditable`` 0개).
  (b) inspections: 0건/N건/실패 3상태가 패널과 반환에서 **구분**된다.
  (c) suspend/resume: confirm 없으면 API 호출 0회(dry-run); confirm=True 면 1회.
  (d) 존재하지 않는 action: 거부 + 유효 목록 안내.
  (e) 도구 8개 등록 유지, 기존 7개 시그니처 불변 (test_compliance_gate_bypass
      에서 담당 — 본 파일에서는 manage_products 시그니처만 검증).
  (f) 페이지 경계 / 빈 스토어가 조용히 죽지 않는다.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from unittest import mock

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import common, mcp_server, naver_client

# --------------------------------------------------------------------------- #
# 테스트용 응답 픽스처.
# --------------------------------------------------------------------------- #

# search_products 200 응답 (상품 2건).
_SEARCH_BODY_2_PRODUCTS = {
    "contents": [
        {
            "originProductNo": "100001",
            "channelProducts": [
                {
                    "channelProductNo": "200001",
                    "name": "테스트 니트",
                    "salePrice": 29000,
                    "statusType": "SALE",
                }
            ],
            "originProduct": {
                "stockQuantity": 50,
            },
        },
        {
            "originProductNo": "100002",
            "channelProducts": [
                {
                    "channelProductNo": "200002",
                    "name": "테스트 가디건",
                    "salePrice": 45000,
                    "statusType": "SUSPENSION",
                }
            ],
            "originProduct": {
                "stockQuantity": 0,
            },
        },
    ]
}

# search_products 200 응답 (0건 — 빈 스토어).
_SEARCH_BODY_EMPTY = {"contents": []}

# 검수 200 응답 (0건).
_INSPECTION_BODY_ZERO = {
    "page": 1,
    "size": 100,
    "totalElements": 0,
    "totalPages": 0,
    "first": True,
    "last": True,
}

# 검수 200 응답 (3건 — 항목 배열 키는 "contents" 로 가정).
_INSPECTION_BODY_3 = {
    "page": 1,
    "size": 100,
    "totalElements": 3,
    "totalPages": 1,
    "first": True,
    "last": True,
    "contents": [
        {"channelProductNo": "200001", "inspectionType": "MODIFY_REQUEST"},
        {"channelProductNo": "200002", "inspectionType": "MODIFY_REQUEST"},
        {"channelProductNo": "200003", "inspectionType": "MODIFY_REQUEST"},
    ],
}

# get_product 200 응답 (suspend/resume 용) — **실측 모양**.
# 실제 get_product 응답 최상위는 ['originProduct', 'smartstoreChannelProduct']
# 이고, smartstoreChannelProduct(단수 dict) 의 키는
# channelProductDisplayStatusType · naverShoppingRegistration ·
# storeKeepExclusiveProduct 이다. **channelProductNo 가 응답에 존재하지
# 않는다** — channelProducts/smartstoreChannelProducts (복수 배열) 도 없다.
_GET_PRODUCT_BODY = {
    "originProduct": {
        "originProductNo": "100001",
        "statusType": "SALE",
    },
    "smartstoreChannelProduct": {
        "channelProductDisplayStatusType": "SALE",
        "naverShoppingRegistration": {},
        "storeKeepExclusiveProduct": {},
    },
}


def _mock_search_products_factory(status_code: int = 200, body=None):
    """naver_client.search_products 를 mock 하는 factory."""
    if body is None:
        body = _SEARCH_BODY_2_PRODUCTS

    def _mock(*args, **kwargs):
        return (status_code, body)

    return _mock


def _mock_inspections_factory(status_code: int = 200, body=None):
    """naver_client.fetch_product_inspections 를 mock 하는 factory."""
    if body is None:
        body = _INSPECTION_BODY_ZERO

    def _mock(*args, **kwargs):
        return (status_code, body)

    return _mock


# --------------------------------------------------------------------------- #
# (a) list: 상품 목록 + HTML 패널 생성; HTML 정적 (script/button/input 0개).
# --------------------------------------------------------------------------- #
class TestListAction:
    """action='list' 검증: 목록 반환 + 정적 HTML 패널."""

    def test_list_returns_products_and_panel(self, tmp_path, monkeypatch):
        """list 가 상품 목록과 패널 경로를 반환하는가."""
        monkeypatch.setattr(common, "STATE_DIR", tmp_path)
        with mock.patch.object(
            naver_client,
            "search_products",
            side_effect=_mock_search_products_factory(),
        ):
            with mock.patch.object(
                naver_client,
                "fetch_product_inspections",
                side_effect=_mock_inspections_factory(),
            ):
                result = mcp_server.manage_products(action="list")

        assert result["ok"] is True
        assert result["action"] == "list"
        assert isinstance(result["products"], list)
        assert len(result["products"]) == 2
        # 상품 요약 검증.
        p0 = result["products"][0]
        assert p0["origin_product_no"] == "100001"
        assert p0["channel_product_no"] == "200001"
        assert p0["name"] == "테스트 니트"
        assert p0["price"] == 29000
        assert p0["stock"] == 50
        assert p0["status"] == "SALE"
        # 패널 경로.
        assert isinstance(result.get("panel_path"), str)
        assert os.path.isfile(result["panel_path"])

    def test_panel_html_has_zero_script_button_input_contenteditable(self, tmp_path, monkeypatch):
        """패널 HTML 에 script/button/input/contenteditable 이 0개인가 (D39)."""
        monkeypatch.setattr(common, "STATE_DIR", tmp_path)
        with mock.patch.object(
            naver_client,
            "search_products",
            side_effect=_mock_search_products_factory(),
        ):
            with mock.patch.object(
                naver_client,
                "fetch_product_inspections",
                side_effect=_mock_inspections_factory(),
            ):
                result = mcp_server.manage_products(action="list")

        panel_path = result["panel_path"]
        html = Path(panel_path).read_text(encoding="utf-8")
        # <script> 0개.
        assert (
            re.search(r"<script[\s>]", html, re.IGNORECASE) is None
        ), "패널 HTML 에 <script> 가 있다"
        # <button> 0개.
        assert (
            re.search(r"<button[\s>]", html, re.IGNORECASE) is None
        ), "패널 HTML 에 <button> 가 있다"
        # <input> 0개.
        assert (
            re.search(r"<input[\s>]", html, re.IGNORECASE) is None
        ), "패널 HTML 에 <input> 가 있다"
        # contenteditable 0개.
        assert "contenteditable" not in html.lower(), "패널 HTML 에 contenteditable 이 있다"

    def test_panel_html_distinguishes_status_by_text(self, tmp_path, monkeypatch):
        """상태(판매중/중지)가 텍스트로 구분되는가 (색상만이 아님)."""
        monkeypatch.setattr(common, "STATE_DIR", tmp_path)
        with mock.patch.object(
            naver_client,
            "search_products",
            side_effect=_mock_search_products_factory(),
        ):
            with mock.patch.object(
                naver_client,
                "fetch_product_inspections",
                side_effect=_mock_inspections_factory(),
            ):
                result = mcp_server.manage_products(action="list")

        html = Path(result["panel_path"]).read_text(encoding="utf-8")
        # SALE → "판매중", SUSPENSION → "중지" 텍스트가 있어야 한다.
        assert "판매중" in html, "판매중 상태가 텍스트로 표시되지 않음"
        assert "중지" in html, "중지 상태가 텍스트로 표시되지 않음"

    def test_no_secret_in_panel_or_result(self, tmp_path, monkeypatch):
        """패널과 반환에 비밀값(토큰 등)이 없는가."""
        monkeypatch.setattr(common, "STATE_DIR", tmp_path)
        with mock.patch.object(
            naver_client,
            "search_products",
            side_effect=_mock_search_products_factory(),
        ):
            with mock.patch.object(
                naver_client,
                "fetch_product_inspections",
                side_effect=_mock_inspections_factory(),
            ):
                result = mcp_server.manage_products(action="list")

        import json

        html = Path(result["panel_path"]).read_text(encoding="utf-8")
        serialized = json.dumps(result, ensure_ascii=False, default=str)
        # 비밀값 후보.
        for secret in ("access_token", "client_secret", "Bearer ", "password"):
            assert secret not in html, f"패널에 비밀값 후보 '{secret}' 있음"
            assert secret not in serialized, f"반환에 비밀값 후보 '{secret}' 있음"


# --------------------------------------------------------------------------- #
# (b) inspections: 0건 / N건 / 실패 3상태 구분.
# --------------------------------------------------------------------------- #
class TestInspectionsThreeStates:
    """검수 3상태(0건/N건/실패)가 패널과 반환에서 구분되는가."""

    def test_inspections_zero_items(self, tmp_path, monkeypatch):
        """검수 0건 → ok=True, total=0, 패널에 '지적 없음' 표시."""
        monkeypatch.setattr(common, "STATE_DIR", tmp_path)
        with mock.patch.object(
            naver_client,
            "search_products",
            side_effect=_mock_search_products_factory(),
        ):
            with mock.patch.object(
                naver_client,
                "fetch_product_inspections",
                side_effect=_mock_inspections_factory(body=_INSPECTION_BODY_ZERO),
            ):
                result = mcp_server.manage_products(action="list")

        assert result["ok"] is True
        inspections = result["inspections"]
        assert inspections["ok"] is True
        assert inspections["total"] == 0
        assert inspections["items"] == []
        # 패널에 "지적 없음" 표시.
        html = Path(result["panel_path"]).read_text(encoding="utf-8")
        assert "지적 없음" in html or "0건" in html

    def test_inspections_n_items(self, tmp_path, monkeypatch):
        """검수 N건 → ok=True, total>0, 패널에 '수정요청 N건' 눈에 띄게."""
        monkeypatch.setattr(common, "STATE_DIR", tmp_path)
        with mock.patch.object(
            naver_client,
            "search_products",
            side_effect=_mock_search_products_factory(),
        ):
            with mock.patch.object(
                naver_client,
                "fetch_product_inspections",
                side_effect=_mock_inspections_factory(body=_INSPECTION_BODY_3),
            ):
                result = mcp_server.manage_products(action="list")

        inspections = result["inspections"]
        assert inspections["ok"] is True
        assert inspections["total"] == 3
        assert len(inspections["items"]) == 3
        # 패널에 "수정요청 3건" 배너.
        html = Path(result["panel_path"]).read_text(encoding="utf-8")
        assert "수정요청" in html
        assert "3" in html

    def test_inspections_failure_shows_reason(self, tmp_path, monkeypatch):
        """검수 실패 → ok=False, 패널에 실패 사유 + 목록은 나온다 (fail-open)."""
        monkeypatch.setattr(common, "STATE_DIR", tmp_path)

        def _failing_inspections(*args, **kwargs):
            raise ConnectionError("검수 서버 응답 없음")

        with mock.patch.object(
            naver_client,
            "search_products",
            side_effect=_mock_search_products_factory(),
        ):
            with mock.patch.object(
                naver_client,
                "fetch_product_inspections",
                side_effect=_failing_inspections,
            ):
                result = mcp_server.manage_products(action="list")

        # 목록은 나온다 (fail-open).
        assert result["ok"] is True
        assert len(result["products"]) == 2
        # 검수는 실패.
        inspections = result["inspections"]
        assert inspections["ok"] is False
        assert inspections["total"] == -1
        assert inspections["reason"] is not None
        # 패널에 실패 사유.
        html = Path(result["panel_path"]).read_text(encoding="utf-8")
        assert "검수 확인 실패" in html

    def test_inspections_three_states_distinguishable(self, tmp_path, monkeypatch):
        """3상태(0건/N건/실패)가 패널에서 서로 다르게 표시되는가."""
        monkeypatch.setattr(common, "STATE_DIR", tmp_path)
        panels = {}

        # 0건.
        with mock.patch.object(
            naver_client, "search_products", side_effect=_mock_search_products_factory()
        ):
            with mock.patch.object(
                naver_client,
                "fetch_product_inspections",
                side_effect=_mock_inspections_factory(body=_INSPECTION_BODY_ZERO),
            ):
                r0 = mcp_server.manage_products(action="list")
                panels["zero"] = Path(r0["panel_path"]).read_text(encoding="utf-8")

        # N건.
        with mock.patch.object(
            naver_client, "search_products", side_effect=_mock_search_products_factory()
        ):
            with mock.patch.object(
                naver_client,
                "fetch_product_inspections",
                side_effect=_mock_inspections_factory(body=_INSPECTION_BODY_3),
            ):
                rn = mcp_server.manage_products(action="list")
                panels["n"] = Path(rn["panel_path"]).read_text(encoding="utf-8")

        # 실패.
        def _fail(*a, **kw):
            raise ConnectionError("fail")

        with mock.patch.object(
            naver_client, "search_products", side_effect=_mock_search_products_factory()
        ):
            with mock.patch.object(naver_client, "fetch_product_inspections", side_effect=_fail):
                rf = mcp_server.manage_products(action="list")
                panels["fail"] = Path(rf["panel_path"]).read_text(encoding="utf-8")

        # 0건: "지적 없음" 이 있고 "수정요청" 이 없다.
        assert "지적 없음" in panels["zero"]
        assert "수정요청" not in panels["zero"]
        # N건: "수정요청" 이 있다.
        assert "수정요청" in panels["n"]
        # 실패: "검수 확인 실패" 가 있다.
        assert "검수 확인 실패" in panels["fail"]
        # 3개 패널이 서로 다른 텍스트를 가진다.
        assert panels["zero"] != panels["n"]
        assert panels["zero"] != panels["fail"]
        assert panels["n"] != panels["fail"]

    def test_inspections_action_alone(self, tmp_path, monkeypatch):
        """action='inspections' 단독 호출도 동작하는가."""
        monkeypatch.setattr(common, "STATE_DIR", tmp_path)
        with mock.patch.object(
            naver_client,
            "fetch_product_inspections",
            side_effect=_mock_inspections_factory(body=_INSPECTION_BODY_3),
        ):
            result = mcp_server.manage_products(action="inspections")

        assert result["ok"] is True
        assert result["action"] == "inspections"
        assert result["total"] == 3
        assert len(result["items"]) == 3


# --------------------------------------------------------------------------- #
# (c) suspend/resume: dry-run(0회) vs confirm=True(1회).
#
# **실측 모양 mock**:
#   - get_product mock 은 실응답 모양({originProduct, smartstoreChannelProduct})
#     이고 channelProductNo 가 **없다**. 예전 "칩한 mock"(channelProducts 복수
#     배열에 channelProductNo 포함) 은 사용 금지 — 코드에 맞춘 mock 이다.
#   - search mock 은 contents[].channelProducts[] 모양 — channel 번호의
#     실측상 유일한 공급원.
# --------------------------------------------------------------------------- #
class TestSuspendResumeConfirmGate:
    """confirm 게이트: 없으면 0회, True 면 1회."""

    def test_suspend_without_confirm_zero_api_calls(self, tmp_path, monkeypatch):
        """suspend + confirm=False → update_product 호출 0회."""
        monkeypatch.setattr(common, "STATE_DIR", tmp_path)
        update_calls: list = []
        get_calls: list = []
        search_calls: list = []

        def _update_recorder(*args, **kwargs):
            update_calls.append({"args": args, "kwargs": kwargs})
            return (200, {})

        def _get_recorder(*args, **kwargs):
            get_calls.append({"args": args, "kwargs": kwargs})
            return (200, _GET_PRODUCT_BODY)

        def _search_recorder(*args, **kwargs):
            search_calls.append({"args": args, "kwargs": kwargs})
            return (200, _SEARCH_BODY_2_PRODUCTS)

        with mock.patch.object(naver_client, "get_product", side_effect=_get_recorder):
            with mock.patch.object(naver_client, "search_products", side_effect=_search_recorder):
                with mock.patch.object(
                    naver_client, "update_product", side_effect=_update_recorder
                ):
                    result = mcp_server.manage_products(
                        action="suspend", origin_product_no="100001"
                    )

        assert result["ok"] is False
        assert result["dry_run"] is True
        # API 호출 0회.
        assert len(update_calls) == 0, f"dry-run 에서 update 호출됨: {update_calls}"
        assert len(get_calls) == 0, f"dry-run 에서 get_product 호출됨: {get_calls}"
        assert len(search_calls) == 0, f"dry-run 에서 search 호출됨: {search_calls}"

    def test_resume_without_confirm_zero_api_calls(self, tmp_path, monkeypatch):
        """resume + confirm=False → update_product 호출 0회."""
        monkeypatch.setattr(common, "STATE_DIR", tmp_path)
        update_calls: list = []
        get_calls: list = []
        search_calls: list = []

        def _update_recorder(*args, **kwargs):
            update_calls.append({"args": args, "kwargs": kwargs})
            return (200, {})

        def _get_recorder(*args, **kwargs):
            get_calls.append({"args": args, "kwargs": kwargs})
            return (200, _GET_PRODUCT_BODY)

        def _search_recorder(*args, **kwargs):
            search_calls.append({"args": args, "kwargs": kwargs})
            return (200, _SEARCH_BODY_2_PRODUCTS)

        with mock.patch.object(naver_client, "get_product", side_effect=_get_recorder):
            with mock.patch.object(naver_client, "search_products", side_effect=_search_recorder):
                with mock.patch.object(
                    naver_client, "update_product", side_effect=_update_recorder
                ):
                    result = mcp_server.manage_products(action="resume", origin_product_no="100001")

        assert result["ok"] is False
        assert result["dry_run"] is True
        assert len(update_calls) == 0
        assert len(get_calls) == 0
        assert len(search_calls) == 0

    def test_suspend_with_confirm_one_call_real_shape_mock(self, tmp_path, monkeypatch):
        """(a) suspend + confirm=True → 실측 모양 mock 에서 update 1회 + before/after.

        get_product mock 은 channelProductNo 가 없는 실응답 모양.
        channel 번호는 search_products mock (contents[].channelProducts[]) 에서
        해석된다. update_product 는 1회만 호출된다.
        """
        monkeypatch.setattr(common, "STATE_DIR", tmp_path)
        update_calls: list = []

        def _update_recorder(channel_no, payload, *args, **kwargs):
            update_calls.append({"channel_no": channel_no, "payload": payload})
            return (200, {})

        # get_product — 실측 모양 (channelProductNo 없음).
        with mock.patch.object(naver_client, "get_product", return_value=(200, _GET_PRODUCT_BODY)):
            # search_products — 실측 모양 (contents[].channelProducts[] 에 channelProductNo).
            with mock.patch.object(
                naver_client,
                "search_products",
                side_effect=_mock_search_products_factory(),
            ):
                with mock.patch.object(
                    naver_client, "update_product", side_effect=_update_recorder
                ):
                    result = mcp_server.manage_products(
                        action="suspend", origin_product_no="100001", confirm=True
                    )

        assert result["ok"] is True
        assert result["dry_run"] is False
        assert len(update_calls) == 1
        assert update_calls[0]["channel_no"] == "200001"
        assert update_calls[0]["payload"] == {"channelProductDisplayStatusType": "SUSPENSION"}
        # before/after 상태.
        assert result["before"]["statusType"] == "SALE"
        assert result["after"]["statusType"] == "SUSPENSION"

    def test_resume_with_confirm_one_call_real_shape_mock(self, tmp_path, monkeypatch):
        """(b) resume + confirm=True → 실측 모양 mock 에서 update 1회 + before/after."""
        monkeypatch.setattr(common, "STATE_DIR", tmp_path)
        update_calls: list = []

        def _update_recorder(channel_no, payload, *args, **kwargs):
            update_calls.append({"channel_no": channel_no, "payload": payload})
            return (200, {})

        with mock.patch.object(naver_client, "get_product", return_value=(200, _GET_PRODUCT_BODY)):
            with mock.patch.object(
                naver_client,
                "search_products",
                side_effect=_mock_search_products_factory(),
            ):
                with mock.patch.object(
                    naver_client, "update_product", side_effect=_update_recorder
                ):
                    result = mcp_server.manage_products(
                        action="resume", origin_product_no="100001", confirm=True
                    )

        assert result["ok"] is True
        assert len(update_calls) == 1
        assert update_calls[0]["channel_no"] == "200001"
        assert update_calls[0]["payload"] == {"channelProductDisplayStatusType": "SALE"}
        assert result["after"]["statusType"] == "SALE"

    def test_suspend_channel_not_found_clear_reason_zero_updates(self, tmp_path, monkeypatch):
        """(c) 검색에서 못 찾음 → 명확한 사유 + update 호출 0회.

        상한(빈 검색 응답) 도달로 originProductNo 일치 항목을 못 찾으면
        ok=False, 명확한 사유, update_product 호출 0회.
        """
        monkeypatch.setattr(common, "STATE_DIR", tmp_path)
        update_calls: list = []

        def _update_recorder(*args, **kwargs):
            update_calls.append({"args": args, "kwargs": kwargs})
            return (200, {})

        with mock.patch.object(naver_client, "get_product", return_value=(200, _GET_PRODUCT_BODY)):
            # 검색은 빈 스토어 — originProductNo 일치 항목 없음.
            with mock.patch.object(
                naver_client,
                "search_products",
                side_effect=_mock_search_products_factory(body=_SEARCH_BODY_EMPTY),
            ):
                with mock.patch.object(
                    naver_client, "update_product", side_effect=_update_recorder
                ):
                    result = mcp_server.manage_products(
                        action="suspend", origin_product_no="999999", confirm=True
                    )

        assert result["ok"] is False
        assert len(update_calls) == 0
        # 명확한 사유 — 조용한 실패가 아니다.
        err = result.get("error") or ""
        assert "channel_product_no" in err or "search_products" in err, f"사유 불명확: {err}"
        assert (
            "999999" in err or "일치" in err or "listing 없음" in err
        ), f"대상 번호 또는 일치 언급 없음: {err}"

    def test_resume_channel_not_found_clear_reason_zero_updates(self, tmp_path, monkeypatch):
        """(c) resume 도 검색에서 못 찾으면 명확한 사유 + update 호출 0회."""
        monkeypatch.setattr(common, "STATE_DIR", tmp_path)
        update_calls: list = []

        def _update_recorder(*args, **kwargs):
            update_calls.append({"args": args, "kwargs": kwargs})
            return (200, {})

        with mock.patch.object(naver_client, "get_product", return_value=(200, _GET_PRODUCT_BODY)):
            with mock.patch.object(
                naver_client,
                "search_products",
                side_effect=_mock_search_products_factory(body=_SEARCH_BODY_EMPTY),
            ):
                with mock.patch.object(
                    naver_client, "update_product", side_effect=_update_recorder
                ):
                    result = mcp_server.manage_products(
                        action="resume", origin_product_no="999999", confirm=True
                    )

        assert result["ok"] is False
        assert len(update_calls) == 0
        err = result.get("error") or ""
        assert "channel_product_no" in err or "search_products" in err


# --------------------------------------------------------------------------- #
# (d) 존재하지 않는 action: 거부 + 유효 목록 안내.
# --------------------------------------------------------------------------- #
class TestUnknownActionRejected:
    """알 수 없는 action 을 거부하고 유효 목록을 안내하는가."""

    def test_unknown_action_rejected(self):
        result = mcp_server.manage_products(action="delete_all")
        assert result["ok"] is False
        assert result["action"] == "delete_all"
        err = result.get("error") or ""
        # 유효 action 목록이 안내에 포함되어야 한다.
        assert "list" in err
        assert "suspend" in err
        assert "resume" in err
        assert "inspections" in err

    def test_empty_action_rejected(self):
        result = mcp_server.manage_products(action="")
        assert result["ok"] is False
        assert "list" in (result.get("error") or "")

    def test_action_case_insensitive(self, tmp_path, monkeypatch):
        """action 이 대소문자 구분 없이 동작하는가."""
        monkeypatch.setattr(common, "STATE_DIR", tmp_path)
        with mock.patch.object(
            naver_client, "search_products", side_effect=_mock_search_products_factory()
        ):
            with mock.patch.object(
                naver_client,
                "fetch_product_inspections",
                side_effect=_mock_inspections_factory(),
            ):
                result = mcp_server.manage_products(action="LIST")
        assert result["ok"] is True
        assert result["action"] == "list"


# --------------------------------------------------------------------------- #
# (e) manage_products 시그니처.
# --------------------------------------------------------------------------- #
class TestManageProductsSignature:
    """manage_products 의 파라미터가 계약과 일치하는가."""

    def test_signature(self):
        import inspect

        sig = inspect.signature(mcp_server.manage_products)
        params = list(sig.parameters.keys())
        assert params == [
            "action",
            "origin_product_no",
            "page",
            "size",
            "confirm",
        ], f"시그니처 불일치: {params}"
        # 기본값 검증.
        assert sig.parameters["origin_product_no"].default == ""
        assert sig.parameters["page"].default == 1
        assert sig.parameters["size"].default == 50
        assert sig.parameters["confirm"].default is False


# --------------------------------------------------------------------------- #
# (f) 페이지 경계 / 빈 스토어.
# --------------------------------------------------------------------------- #
class TestPageBoundsAndEmptyStore:
    """페이지 경계값과 빈 스토어가 조용히 죽지 않는가."""

    def test_empty_store_returns_empty_products(self, tmp_path, monkeypatch):
        """빈 스토어(0건) → ok=True, products=[] (조용히 죽지 않음)."""
        monkeypatch.setattr(common, "STATE_DIR", tmp_path)
        with mock.patch.object(
            naver_client,
            "search_products",
            side_effect=_mock_search_products_factory(body=_SEARCH_BODY_EMPTY),
        ):
            with mock.patch.object(
                naver_client,
                "fetch_product_inspections",
                side_effect=_mock_inspections_factory(),
            ):
                result = mcp_server.manage_products(action="list")

        assert result["ok"] is True
        assert result["products"] == []
        assert result["total_returned"] == 0
        # 패널은 생성된다.
        assert os.path.isfile(result["panel_path"])

    def test_invalid_page_falls_back_to_1(self, tmp_path, monkeypatch):
        """잘못된 page(0/음수) → 1로 정규화."""
        monkeypatch.setattr(common, "STATE_DIR", tmp_path)
        captured = {}

        def _search_recorder(*args, **kwargs):
            captured["page"] = kwargs.get("page") or (args[0] if args else None)
            return (200, _SEARCH_BODY_EMPTY)

        with mock.patch.object(naver_client, "search_products", side_effect=_search_recorder):
            with mock.patch.object(
                naver_client,
                "fetch_product_inspections",
                side_effect=_mock_inspections_factory(),
            ):
                result = mcp_server.manage_products(action="list", page=0, size=10)

        assert result["ok"] is True
        assert result["page"] == 1

    def test_invalid_size_falls_back_to_50(self, tmp_path, monkeypatch):
        """잘못된 size(0/음수) → 50으로 정규화."""
        monkeypatch.setattr(common, "STATE_DIR", tmp_path)

        with mock.patch.object(
            naver_client, "search_products", side_effect=_mock_search_products_factory()
        ):
            with mock.patch.object(
                naver_client,
                "fetch_product_inspections",
                side_effect=_mock_inspections_factory(),
            ):
                result = mcp_server.manage_products(action="list", page=1, size=0)

        assert result["ok"] is True
        assert result["size"] == 50

    def test_non_numeric_page_does_not_crash(self, tmp_path, monkeypatch):
        """문자열 page 가 와도 죽지 않고 기본값으로."""
        monkeypatch.setattr(common, "STATE_DIR", tmp_path)
        with mock.patch.object(
            naver_client, "search_products", side_effect=_mock_search_products_factory()
        ):
            with mock.patch.object(
                naver_client,
                "fetch_product_inspections",
                side_effect=_mock_inspections_factory(),
            ):
                result = mcp_server.manage_products(action="list", page="abc")  # type: ignore[arg-type]

        assert result["ok"] is True
        assert result["page"] == 1

    def test_suspend_non_numeric_origin_no_rejected(self):
        """suspend 에 숫자가 아닌 origin_product_no → API 호출 없이 거부."""
        result = mcp_server.manage_products(action="suspend", origin_product_no="abc", confirm=True)
        assert result["ok"] is False
        assert "숫자" in (result.get("error") or "")
