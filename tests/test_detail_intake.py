# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""외부 상세페이지 인수 파이프(intake_detail_html) 검증 — 상세 인수 파이프.

실물 표본(585KB DOCTYPE 문서 + base64 이미지 11장)을 테스트 픽스처로
축소 재현한다(원본 복사 금지 — 구조만 재현). **네이버 업로드는 전부
모킹**한다(g): 실호출 0회를 증명한다.

  (a) 표본형 입력 → 조각화 + img src 전부 모킹 CDN URL 로 재작성, 순서 보존.
  (b) detail_html 에 DOCTYPE/html/head/body/script 태그 0.
  (c) script/iframe 포함 입력 → 제거 + removed 카운트 정확.
  (d) 대표이미지 후보 = 첫 이미지.
  (e) 잘못된 경로/비 HTML/초과 크기 → 명확한 error.
  (f) 도구 수 13 (기존 가드 파일들이 담당 — 여기선 도구 함수 동작만).
  (g) 실제 네이버 업로드 호출 0회(전부 모킹) — 증명 포함.
"""

from __future__ import annotations

import base64
import sys
import tempfile
from pathlib import Path

import pytest

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import detail_intake, mcp_server, naver_client

# 매직바이트만 유효한 최소 PNG — 업로드 가드는 매직바이트를 검사한다.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _fake_png(payload: bytes) -> bytes:
    """매직바이트 + 구별 페이로드를 가진 가짜 PNG 바이트."""
    return _PNG_MAGIC + payload


def _data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def _mock_upload_factory(counter: list):
    """네이버 업로드 모킹 — 호출 수를 기록하고 순서 있는 모킹 CDN URL 반환."""

    def _upload(paths):
        counter.append(list(paths))
        return [f"https://shop-phinf.pstatic.net/mock/{i + 1}.png" for i, _ in enumerate(paths)]

    return _upload


@pytest.fixture(autouse=True)
def _no_upload_root(monkeypatch):
    """컨테인먼트 검사가 픽스처 경로를 거부하지 않게 환경변수를 격리."""
    monkeypatch.delenv("CLOSSIFY_UPLOAD_ROOT", raising=False)


def _sample_doc(n_images: int = 3, *, with_evil: bool = False) -> str:
    """표본형 축소 재현 — DOCTYPE + style 블록 + base64 img (+유해 요소)."""
    imgs = "\n".join(
        f'<img src="{_data_uri(_fake_png(bytes([65 + i]) * (i + 1)))}" alt="img{i + 1}" />'
        for i in range(n_images)
    )
    evil = ""
    if with_evil:
        evil = (
            "<script>alert('x')</script>\n"
            '<iframe src="https://evil.example.com/frame"></iframe>\n'
            '<link rel="stylesheet" href="https://evil.example.com/x.css" />\n'
            '<img src="https://evil.example.com/ext.jpg" alt="외부" />\n'
        )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="ko">\n<head>\n<meta charset="utf-8" />\n'
        "<title>표본</title>\n"
        "<style>.a{color:#222}</style>\n"
        "<style>.b{margin:0}</style>\n"
        "</head>\n<body>\n"
        '<div class="detail">\n'
        f"{imgs}\n{evil}"
        "<p>소개 문구</p>\n"
        "</div>\n</body>\n</html>\n"
    )


def _write_sample(tmp_path: Path, doc: str) -> str:
    p = tmp_path / "sample.html"
    # newline="\n" — 플랫폼 개행 번역으로 bytes_before 검증이 흔들리지 않게.
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(doc)
    return str(p)


# =========================================================================== #
# (a) 표본형 입력 — 조각화 + CDN 재작성 + 순서 보존.
# =========================================================================== #


class TestSampleIntake:
    def test_a_ok_and_urls_rewritten_in_order(self, tmp_path):
        """(a) ok=true, img src 전부 모킹 CDN URL, 순서 보존, data: 잔존 0."""
        counter: list = []
        path = _write_sample(tmp_path, _sample_doc(3))
        result = detail_intake.intake_detail_html(path, upload_fn=_mock_upload_factory(counter))

        assert result["ok"] is True, result["error"]
        assert result["image_urls"] == [
            "https://shop-phinf.pstatic.net/mock/1.png",
            "https://shop-phinf.pstatic.net/mock/2.png",
            "https://shop-phinf.pstatic.net/mock/3.png",
        ]
        html = result["detail_html"]
        assert "data:" not in html
        # 순서 보존 — 상세는 순서가 내용이다.
        assert html.index("mock/1.png") < html.index("mock/2.png") < html.index("mock/3.png")
        # 업로드는 1회 배치 호출, 경로 3개.
        assert len(counter) == 1 and len(counter[0]) == 3

    def test_a_style_blocks_kept_inline_and_bytes_shrunk(self, tmp_path):
        """(a) style 은 인라인으로 살리고 base64 제거로 크기가 줄어든다."""
        path = _write_sample(tmp_path, _sample_doc(2))
        result = detail_intake.intake_detail_html(path, upload_fn=_mock_upload_factory([]))
        html = result["detail_html"]
        assert "<style>.a{color:#222}</style>" in html
        assert "<style>.b{margin:0}</style>" in html
        assert result["bytes_before"] == len(_sample_doc(2).encode("utf-8"))
        assert result["bytes_after"] == len(html.encode("utf-8"))
        assert result["bytes_after"] < result["bytes_before"]

    def test_a_temp_files_cleaned_up(self, tmp_path):
        """(a) 임시 파일 정리 — tempdir 에 clossify_intake_ 잔존 0."""
        before = set(Path(tempfile.gettempdir()).glob("clossify_intake_*"))
        path = _write_sample(tmp_path, _sample_doc(2))
        detail_intake.intake_detail_html(path, upload_fn=_mock_upload_factory([]))
        after = set(Path(tempfile.gettempdir()).glob("clossify_intake_*"))
        assert after == before, f"임시 파일 잔존: {after - before}"


# =========================================================================== #
# (b) 조각화 — 금지 태그 0.
# =========================================================================== #


class TestFragmentClean:
    def test_b_no_document_or_script_tags(self, tmp_path):
        """(b) DOCTYPE/html/head/body/script 태그가 detail_html 에 0개."""
        path = _write_sample(tmp_path, _sample_doc(2, with_evil=True))
        result = detail_intake.intake_detail_html(path, upload_fn=_mock_upload_factory([]))
        assert result["ok"] is True
        html = result["detail_html"].lower()
        for tag in ("<!doctype", "<html", "<head", "<body", "<script", "<iframe"):
            assert tag not in html, f"금지 태그 잔존: {tag}"


# =========================================================================== #
# (c) 제거 카운트.
# =========================================================================== #


class TestRemovedCounts:
    def test_c_removed_counts_exact(self, tmp_path):
        """(c) script 1·iframe 1·외부(link+외부 img) 2 제거 및 카운트."""
        path = _write_sample(tmp_path, _sample_doc(1, with_evil=True))
        result = detail_intake.intake_detail_html(path, upload_fn=_mock_upload_factory([]))
        assert result["ok"] is True
        assert result["removed"] == {"scripts": 1, "iframes": 1, "external_refs": 2}
        assert "evil.example.com" not in result["detail_html"]

    def test_c_clean_input_removed_zero(self, tmp_path):
        """(c) 유해 요소 없는 입력은 removed 전부 0."""
        path = _write_sample(tmp_path, _sample_doc(1))
        result = detail_intake.intake_detail_html(path, upload_fn=_mock_upload_factory([]))
        assert result["removed"] == {"scripts": 0, "iframes": 0, "external_refs": 0}


# =========================================================================== #
# (d) 대표이미지 후보.
# =========================================================================== #


class TestRepresentative:
    def test_d_representative_is_first_image(self, tmp_path):
        """(d) representative_candidate == image_urls[0]."""
        path = _write_sample(tmp_path, _sample_doc(3))
        result = detail_intake.intake_detail_html(path, upload_fn=_mock_upload_factory([]))
        assert result["representative_candidate"] == result["image_urls"][0]
        assert result["representative_candidate"] == "https://shop-phinf.pstatic.net/mock/1.png"


# =========================================================================== #
# (e) 명확한 오류 — 조용한 실패 금지.
# =========================================================================== #


class TestErrors:
    def test_e_nonexistent_path(self, tmp_path):
        result = detail_intake.intake_detail_html(str(tmp_path / "nope.html"))
        assert result["ok"] is False
        assert result["error"] and "존재하지 않는" in result["error"]

    def test_e_relative_path_rejected(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_sample(tmp_path, _sample_doc(1))
        result = detail_intake.intake_detail_html("sample.html")
        assert result["ok"] is False
        assert result["error"] and "절대경로" in result["error"]

    def test_e_non_html_rejected(self, tmp_path):
        p = tmp_path / "plain.html"
        p.write_text("그냥 텍스트입니다", encoding="utf-8")
        result = detail_intake.intake_detail_html(str(p))
        assert result["ok"] is False
        assert result["error"] and "HTML 이 아닙니다" in result["error"]

    def test_e_oversize_rejected(self, tmp_path):
        path = _write_sample(tmp_path, _sample_doc(1))
        result = detail_intake.intake_detail_html(path, max_bytes=8)
        assert result["ok"] is False
        assert result["error"] and "크기 초과" in result["error"]

    def test_e_bad_magic_image_fail_closed(self, tmp_path):
        """위장 data: 이미지 → 전체 거부(fail-closed), 부분 URL 0."""
        doc = (
            "<!DOCTYPE html><html><body>"
            f'<img src="{_data_uri(_fake_png(b"ok"))}" />'
            '<img src="data:image/png;base64,'
            + base64.b64encode(b"GIF89a-not-png").decode("ascii")
            + '" /></body></html>'
        )
        path = _write_sample(tmp_path, doc)
        result = detail_intake.intake_detail_html(path, upload_fn=_mock_upload_factory([]))
        assert result["ok"] is False
        assert result["image_urls"] == []
        assert result["error"] and "매직바이트" in result["error"]


# =========================================================================== #
# (f)/(g) MCP 도구 배선 — 업로드는 전부 모킹, 실호출 0회 증명.
# =========================================================================== #


class TestMcpToolWiring:
    def test_g_tool_uses_mocked_naver_upload_zero_real_calls(self, tmp_path, monkeypatch):
        """(g) 도구 함수가 naver_client.upload_images 경로를 탄다 — 모킹 교체로
        실호출 0회를 증명한다(교체 안 되면 실경로가 그대로 불릴 위험)."""
        calls: list = []

        def _fake_upload(paths):
            calls.append(list(paths))
            return [f"https://shop-phinf.pstatic.net/mock/{i + 1}.png" for i, _ in enumerate(paths)]

        # 실경로를 모킹으로 교체 — 이 테스트 안에서 네이버 실호출은 불가능하다.
        monkeypatch.setattr(naver_client, "upload_images", _fake_upload)
        path = _write_sample(tmp_path, _sample_doc(2))
        result = mcp_server.intake_detail_html(path)

        assert result["ok"] is True, result["error"]
        assert len(calls) == 1 and len(calls[0]) == 2  # 모킹 호출 1회 = 실호출 0회.
        assert result["image_urls"][0].endswith("/mock/1.png")

    def test_f_intake_tool_registered(self):
        """(f) intake_detail_html 이 도구 목록에 있다(총 13개는 기존 가드 담당)."""
        import asyncio

        raw = mcp_server.mcp.list_tools()
        tools = asyncio.run(raw) if asyncio.iscoroutine(raw) else raw
        names = [str(getattr(t, "name", t)) for t in tools]
        assert "intake_detail_html" in names

    def test_f_tool_error_passthrough(self, tmp_path):
        """(f) 도구도 조용한 실패 없이 error 를 그대로 드러낸다."""
        result = mcp_server.intake_detail_html(str(tmp_path / "nope.html"))
        assert result["ok"] is False
        assert result["error"]
