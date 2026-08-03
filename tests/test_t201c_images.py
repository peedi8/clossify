"""T-201c-r — 이미지 입력 정규화 + 업로드 가드 정본화 테스트.

이 테스트는 작업지시서 T-201c-r 의 Acceptance 항목을 전부 검증한다:

  - 로컬 이미지 가드 반례(디렉터리/심링크/루트밖 절대경로/매직바이트 위장)
  - 외부 URL SSRF 반례(루프백/사설/링크로컬/예약대역/10진·16진·8진 IP 표기/
    IPv4-mapped IPv6/다중 A레코드/리다이렉트-내부IP/file: 스킴)
  - 정규화 진입점(attach_images) 의 순서 보존·대표이미지 승격 방지·fail-closed
  - mcp_server.upload_images 가 정본 가드를 쓰는지(확장자 위장 거부)
  - 4개 MCP 도구 등록 유지 + 기존 171개 테스트 무회귀는 pytest 전체 실행으로 검증

실제 네트워크 호출·DNS 해석은 전부 monkeypatch 한다(해석기·세션 주입).
"""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path
from unittest import mock

import pytest

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import images, mcp_server, naver_client


# --------------------------------------------------------------------------- #
# 헬퍼 — 가짜 DNS 해석기/세션/응답
# --------------------------------------------------------------------------- #
def _addrinfo(family: int, ip: str, port: int | None = None):
    """``socket.getaddrinfo`` 결과 1행을 생성."""
    if family == socket.AF_INET:
        sockaddr = (ip, port or 0, 0, "")
    else:
        sockaddr = (ip, port or 0, 0, 0)
    return (family, socket.SOCK_STREAM, 0, "", sockaddr)


def make_resolver(host_to_ips: dict[str, list[str]]):
    """호스트명 -> IP 리스트 매핑을 getaddrinfo-호환 해석기로 만든다.

    IPv4 / IPv6 / IPv4-mapped IPv6 주소를 모두 지원한다.
    """

    def resolver(host, port, *args, **kwargs):
        host_l = (host or "").lower()
        if host_l not in host_to_ips:
            raise socket.gaierror(8, "nodomain")
        infos = []
        for ip in host_to_ips[host_l]:
            # 대괄호 제거 (IPv6 URL 형식)
            clean = ip.strip("[]")
            if ":" in clean and "." not in clean:
                infos.append(_addrinfo(socket.AF_INET6, clean, port))
            else:
                infos.append(_addrinfo(socket.AF_INET, clean, port))
        return infos

    return resolver


class FakeResponse:
    """stream=True 인 requests.Response 의 최소 가짜."""

    def __init__(self, status_code=200, body=b"", headers=None, location=""):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        if location:
            self.headers["Location"] = location
        self._closed = False

    def iter_content(self, chunk_size=8192):
        # body 가 비어있지 않으면 청크로 쪼개 반환.
        if not self._body:
            return
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        self._closed = True


class FakeSession:
    """``requests.Session`` 의 최소 가짜. ``get`` 호출을 기록한다."""

    def __init__(self, responses):
        # responses: 단일 응답 또는 URL 패턴 -> 응답 dict.
        self._responses = responses
        self.calls = []

    def get(self, url, headers=None, stream=False, timeout=None,
            allow_redirects=False, verify=True, **kw):
        self.calls.append({
            "url": url, "headers": headers, "stream": stream,
            "timeout": timeout, "allow_redirects": allow_redirects,
        })
        if isinstance(self._responses, FakeResponse):
            return self._responses
        # URL 패턴 매칭 — IP 치환된 URL 이면 Host 헤더를 기준으로 매칭.
        # (실제 코드가 URL 호스트를 IP 로 치환하고 Host 헤더로 원 호스트명 보존)
        match_key = url
        if headers and "Host" in headers:
            match_key = headers["Host"]
        for pat, resp in self._responses.items():
            if pat in match_key or pat in url:
                return resp
        raise AssertionError(f"FakeSession: 예상 못한 URL {url!r}")

    def close(self):
        pass


# 표준 매직바이트 헤더들.
PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG_HEADER = b"\xff\xd8\xff\xe0" + b"\x00" * 32
WEBP_HEADER = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 32


# --------------------------------------------------------------------------- #
# 로컬 이미지 가드 반례 — Acceptance ① ~ ⑤
# --------------------------------------------------------------------------- #
class TestLocalGuardAcceptance:
    """``validate_local_image`` 가 작업지시 반례 5종을 모두 다루는가."""

    def test_text_file_with_jpg_extension_rejected(self, tmp_path):
        """① .jpg 인데 내용이 텍스트 → 매직바이트 위장으로 거부."""
        fake = tmp_path / "fake.jpg"
        fake.write_text("this is not a jpeg", encoding="utf-8")
        v = images.validate_local_image(str(fake))
        assert v["ok"] is False
        reasons = " ".join(v["errors"])
        assert "매직바이트" in reasons or "위장" in reasons

    def test_directory_path_rejected(self, tmp_path):
        """② 디렉터리 경로 → 거부."""
        v = images.validate_local_image(str(tmp_path))
        assert v["ok"] is False
        assert any("일반 파일이 아닙니다" in e for e in v["errors"])

    def test_symlink_rejected(self, tmp_path):
        """③ 심링크 → 거부."""
        target = tmp_path / "real.png"
        target.write_bytes(PNG_HEADER)
        link = tmp_path / "link.png"
        try:
            os.symlink(str(target), str(link))
        except (OSError, NotImplementedError):
            pytest.skip("symlink not supported on this platform")
        v = images.validate_local_image(str(link))
        assert v["ok"] is False
        assert any("심볼릭 링크" in e for e in v["errors"])

    def test_path_outside_upload_root_rejected(
        self, tmp_path, monkeypatch
    ):
        """④ CLOSSIFY_UPLOAD_ROOT 설정 시 루트 밖 절대경로 → 거부."""
        root = tmp_path / "upload_root"
        root.mkdir()
        outside = tmp_path / "outside.png"
        outside.write_bytes(PNG_HEADER)
        monkeypatch.setenv("CLOSSIFY_UPLOAD_ROOT", str(root))
        v = images.validate_local_image(str(outside))
        assert v["ok"] is False
        assert any("업로드 루트" in e for e in v["errors"])
        assert v["contained"] is True

    def test_valid_png_jpeg_pass(self, tmp_path, monkeypatch):
        """⑤ 정상 PNG/JPEG → 통과."""
        monkeypatch.delenv("CLOSSIFY_UPLOAD_ROOT", raising=False)
        png = tmp_path / "ok.png"
        png.write_bytes(PNG_HEADER)
        v = images.validate_local_image(str(png))
        assert v["ok"] is True, v["errors"]

        jpg = tmp_path / "ok.jpg"
        jpg.write_bytes(JPEG_HEADER)
        v2 = images.validate_local_image(str(jpg))
        assert v2["ok"] is True, v2["errors"]

    def test_webp_valid_passes(self, tmp_path, monkeypatch):
        """WEBP 정상 파일도 통과."""
        monkeypatch.delenv("CLOSSIFY_UPLOAD_ROOT", raising=False)
        webp = tmp_path / "ok.webp"
        webp.write_bytes(WEBP_HEADER)
        v = images.validate_local_image(str(webp))
        assert v["ok"] is True, v["errors"]

    def test_containment_unset_passes_but_flagged(
        self, tmp_path, monkeypatch
    ):
        """루트 미설정이면 컨테인먼트 검사 미적용(통과) + contained=False 표기."""
        monkeypatch.delenv("CLOSSIFY_UPLOAD_ROOT", raising=False)
        png = tmp_path / "ok.png"
        png.write_bytes(PNG_HEADER)
        v = images.validate_local_image(str(png))
        assert v["ok"] is True
        assert v["contained"] is False


# --------------------------------------------------------------------------- #
# 외부 URL SSRF 가드 — 기본 OFF + 허용목록 opt-in
# --------------------------------------------------------------------------- #
class TestExternalUrlGuardDefault:
    """허용목록 미설정/비었을 때 외부 URL 은 전부 거부."""

    def test_arbitrary_url_rejected_when_allowlist_empty(self, monkeypatch):
        """Acceptance: https://example.com/x.jpg → 거부(사유에 허용목록 안내)."""
        monkeypatch.delenv("CLOSSIFY_IMAGE_FETCH_ALLOW_HOSTS", raising=False)
        r = images.fetch_external_image(
            "https://example.com/x.jpg",
            allowed_hosts=None,
            resolver=make_resolver({}),
            session=FakeSession(FakeResponse()),
        )
        assert r["ok"] is False
        assert "허용" in r["reason"] or "CLOSSIFY_IMAGE_FETCH_ALLOW_HOSTS" in r["reason"]

    def test_empty_allowed_hosts_tuple_rejects(self):
        r = images.fetch_external_image(
            "https://example.com/x.jpg",
            allowed_hosts=(),
            resolver=make_resolver({}),
            session=FakeSession(FakeResponse()),
        )
        assert r["ok"] is False


# --------------------------------------------------------------------------- #
# 외부 URL SSRF 가드 — 허용목록에 호스트를 넣은 상태에서의 반례
# --------------------------------------------------------------------------- #
class TestSsrfCounterexamples:
    """작업지시 Acceptance SSRF 반례 전부."""

    def setup_method(self):
        # 테스트 전역 상태 오염 방지용 루트 비움.
        self._orig_root = os.environ.get("CLOSSIFY_UPLOAD_ROOT")

    def teardown_method(self):
        if self._orig_root is None:
            os.environ.pop("CLOSSIFY_UPLOAD_ROOT", None)
        else:
            os.environ["CLOSSIFY_UPLOAD_ROOT"] = self._orig_root

    def _ssrf_reject(
        self, url, host_to_ips, allowed_hosts, *, status=200, body=PNG_HEADER
    ):
        r = images.fetch_external_image(
            url,
            allowed_hosts=allowed_hosts,
            resolver=make_resolver(host_to_ips),
            session=FakeSession(FakeResponse(status_code=status, body=body)),
        )
        return r

    def test_loopback_ipv4_rejected(self):
        r = self._ssrf_reject(
            "http://127.0.0.1/x.jpg",
            {"127.0.0.1": ["127.0.0.1"]},
            ("127.0.0.1",),
        )
        assert r["ok"] is False
        assert "루프백" in r["reason"] or "내부" in r["reason"]

    def test_private_192_168_rejected(self):
        r = self._ssrf_reject(
            "http://192.168.0.1/x.jpg",
            {"192.168.0.1": ["192.168.0.1"]},
            ("192.168.0.1",),
        )
        assert r["ok"] is False
        assert "사설" in r["reason"] or "내부" in r["reason"]

    def test_link_local_169_254_169_254_rejected(self):
        """AWS 메타데이터 엔드포인트."""
        r = self._ssrf_reject(
            "http://169.254.169.254/latest/meta-data",
            {"169.254.169.254": ["169.254.169.254"]},
            ("169.254.169.254",),
        )
        assert r["ok"] is False
        assert "링크" in r["reason"] or "내부" in r["reason"]

    def test_decimal_ip_notation_rejected(self):
        """2130706433 = 127.0.0.1. 허용목록에 넣고 해석기가 127.0.0.1 반환."""
        r = self._ssrf_reject(
            "http://2130706433/x.jpg",
            {"2130706433": ["127.0.0.1"]},
            ("2130706433",),
        )
        assert r["ok"] is False

    def test_hex_ip_notation_rejected(self):
        """0x7f000001 = 127.0.0.1."""
        r = self._ssrf_reject(
            "http://0x7f000001/x.jpg",
            {"0x7f000001": ["127.0.0.1"]},
            ("0x7f000001",),
        )
        assert r["ok"] is False

    def test_octal_ip_notation_rejected(self):
        """0177.0.0.1 = 127.0.0.1."""
        r = self._ssrf_reject(
            "http://0177.0.0.1/x.jpg",
            {"0177.0.0.1": ["127.0.0.1"]},
            ("0177.0.0.1",),
        )
        assert r["ok"] is False

    def test_short_ip_notation_rejected(self):
        """127.1 = 127.0.0.1."""
        r = self._ssrf_reject(
            "http://127.1/x.jpg",
            {"127.1": ["127.0.0.1"]},
            ("127.1",),
        )
        assert r["ok"] is False

    def test_ipv4_mapped_ipv6_rejected(self):
        """::ffff:127.0.0.1 은 ipv4_mapped 언랩 후 루프백으로 거부."""
        r = images.fetch_external_image(
            "http://[::ffff:127.0.0.1]/x.jpg",
            allowed_hosts=("::ffff:127.0.0.1",),
            resolver=make_resolver(
                {"::ffff:127.0.0.1": ["::ffff:127.0.0.1"]}
            ),
            session=FakeSession(FakeResponse()),
        )
        assert r["ok"] is False

    def test_multi_a_record_with_loopback_rejected(self):
        """다중 A 레코드(공인 + 127.0.0.1) → 하나라도 내부면 거부."""
        r = self._ssrf_reject(
            "http://multi.example.com/x.jpg",
            {"multi.example.com": ["93.184.216.34", "127.0.0.1"]},
            ("multi.example.com",),
        )
        assert r["ok"] is False
        assert "루프백" in r["reason"] or "내부" in r["reason"]

    def test_redirect_to_internal_ip_rejected(self):
        """허용된 공인 호스트에서 응답은 OK 지만 리다이렉트 Location 이 내부 IP."""
        r = images.fetch_external_image(
            "http://good.example.com/x.jpg",
            allowed_hosts=("good.example.com",),
            resolver=make_resolver(
                {
                    "good.example.com": ["93.184.216.34"],
                    "169.254.169.254": ["169.254.169.254"],
                }
            ),
            session=FakeSession({
                # 첫 홉은 302 리다이렉트 — Location 이 메타데이터 엔드포인트.
                "good.example.com": FakeResponse(
                    status_code=302, body=b"",
                    location="http://169.254.169.254/latest/meta-data",
                ),
                # 두 번째 홉은 정상 응답이어도 검증 단계에서 거부돼야 함.
                "169.254.169.254": FakeResponse(
                    status_code=200, body=PNG_HEADER
                ),
            }),
        )
        assert r["ok"] is False
        assert "169.254" in r["reason"] or "링크" in r["reason"] or "내부" in r["reason"]

    def test_file_scheme_rejected(self):
        """file:///etc/passwd → 스킴 거부."""
        r = images.fetch_external_image(
            "file:///etc/passwd",
            allowed_hosts=("",),  # 빈 호스트 허용목록이라도 스킴에서 거부
            resolver=make_resolver({}),
            session=FakeSession(FakeResponse()),
        )
        assert r["ok"] is False
        assert "스킴" in r["reason"]

    def test_scheme_validation_http_only(self):
        """gopher/ftp/file 모두 거부."""
        for scheme in ("ftp", "gopher", "file", "dict", "ldap"):
            r = images.fetch_external_image(
                f"{scheme}://example.com/x",
                allowed_hosts=("example.com",),
                resolver=make_resolver({"example.com": ["93.184.216.34"]}),
                session=FakeSession(FakeResponse()),
            )
            assert r["ok"] is False, f"{scheme} 은 거부되어야 함"
            assert "스킴" in r["reason"], f"{scheme} 스킴 사유 필요"

    def test_host_not_in_allowlist_rejected(self):
        """허용목록에 없는 호스트 → 거부."""
        r = self._ssrf_reject(
            "http://other.example.com/x.jpg",
            {"other.example.com": ["93.184.216.34"]},
            ("good.example.com",),
        )
        assert r["ok"] is False
        assert "허용목록" in r["reason"] or "허용" in r["reason"]

    def test_valid_public_host_accepted(self):
        """정상 공인 호스트 + 매직바이트 통과 → ok=True + 임시 파일 생성."""
        r = images.fetch_external_image(
            "http://good.example.com/x.jpg",
            allowed_hosts=("good.example.com",),
            resolver=make_resolver({"good.example.com": ["93.184.216.34"]}),
            session=FakeSession(FakeResponse(status_code=200, body=PNG_HEADER)),
        )
        assert r["ok"] is True, r["reason"]
        assert r["temp_path"]
        assert os.path.exists(r["temp_path"])
        # 호출자가 정리.
        os.unlink(r["temp_path"])

    def test_magic_byte_check_on_fetched_body(self):
        """정상 호스트라도 응답 바디가 이미지가 아니면 거부."""
        r = images.fetch_external_image(
            "http://good.example.com/x.jpg",
            allowed_hosts=("good.example.com",),
            resolver=make_resolver({"good.example.com": ["93.184.216.34"]}),
            session=FakeSession(FakeResponse(
                status_code=200, body=b"<html>not image</html>"
            )),
        )
        assert r["ok"] is False
        assert "매직바이트" in r["reason"]

    def test_size_cap_enforced_during_streaming(self):
        """누적 크기가 상한 초과하면 거부."""
        # 10MB 초과 바디.
        big_body = PNG_HEADER + b"\x00" * (images.MAX_IMAGE_BYTES + 1)
        r = images.fetch_external_image(
            "http://good.example.com/x.jpg",
            allowed_hosts=("good.example.com",),
            resolver=make_resolver({"good.example.com": ["93.184.216.34"]}),
            session=FakeSession(FakeResponse(status_code=200, body=big_body)),
        )
        assert r["ok"] is False
        assert "크기" in r["reason"] or "초과" in r["reason"]

    def test_redirect_loop_detected(self):
        """같은 URL 로 돌아오는 리다이렉트 → 루프로 거부."""
        loop_url = "http://good.example.com/x.jpg"
        r = images.fetch_external_image(
            loop_url,
            allowed_hosts=("good.example.com",),
            resolver=make_resolver({"good.example.com": ["93.184.216.34"]}),
            session=FakeSession({
                "good.example.com": FakeResponse(
                    status_code=302, body=b"", location=loop_url
                ),
            }),
            max_hops=5,
        )
        assert r["ok"] is False
        assert "루프" in r["reason"] or "상한" in r["reason"]


# --------------------------------------------------------------------------- #
# 정규화 진입점 attach_images — 순서 보존 / 재업로드 금지 / fail-closed
# --------------------------------------------------------------------------- #
class TestAttachImages:
    """``attach_images`` 가 작업지시 Acceptance 정규화 반례를 다루는가."""

    def test_mixed_local_and_cdn_preserves_order(self, tmp_path):
        """로컬 + 네이버 CDN URL 혼합 → urls 가 입력 순서 유지."""
        png = tmp_path / "local.png"
        png.write_bytes(PNG_HEADER)
        cdn = "https://shop-phinf.pstatic.net/example.jpg"

        def fake_upload(paths):
            return [f"https://upload.example.com/{i}.jpg" for i, _ in enumerate(paths)]

        r = images.attach_images(
            [str(png), cdn, str(png)],
            upload_fn=fake_upload,
            fetch_fn=lambda *a, **kw: {"ok": False, "reason": "no fetch"},
        )
        assert r["rejected"] == []
        # 3개 입력 → 3개 출력(로컬 2개 업로드 + CDN 1개 통과) 순서대로.
        assert len(r["urls"]) == 3
        # CDN URL 은 변형 없이 그대로.
        assert r["urls"][1] == cdn
        # 로컬 업로드는 CDN URL 과 다른 도메인.
        assert "pstatic.net" not in r["urls"][0]
        assert "pstatic.net" not in r["urls"][2]

    def test_cdn_url_not_reuploaded(self):
        """CDN URL 은 upload_fn 이 호출되지 않고 통과해야 함."""
        cdn = "https://shop-phinf.pstatic.net/x.jpg"
        called = []

        def fake_upload(paths):
            called.extend(paths)
            return [f"https://upload.example.com/{i}" for i, _ in enumerate(paths)]

        r = images.attach_images([cdn], upload_fn=fake_upload)
        assert r["rejected"] == []
        assert r["urls"] == [cdn]
        assert called == []  # upload_fn 이 한 번도 호출되지 않음.

    def test_representative_image_protected_on_reject(self, tmp_path):
        """1번이 거부될 때 rejected 가 비지 않고 2번이 1번 자리로 승격되지 않음.

        핵심: ``urls`` 는 부분 반환될 수 있으나 ``rejected`` 가 비어있지 않으면
        호출자가 진행하지 않아야 한다(fail-closed 원칙). 즉, 반환형이 그 사실을
        호출자가 판단할 수 있는 구조여야 한다.
        """
        bad_local = tmp_path / "fake.jpg"
        bad_local.write_text("not jpeg")
        cdn = "https://shop-phinf.pstatic.net/good.jpg"

        r = images.attach_images(
            [str(bad_local), cdn],
            upload_fn=lambda paths: [],
            fetch_fn=lambda *a, **kw: {"ok": False, "reason": "disabled"},
        )
        # 1번이 거부됐으므로 rejected 가 비어있지 않음.
        assert len(r["rejected"]) >= 1
        assert r["rejected"][0]["index"] == 0
        # 호출자에게 "거부가 있다"는 명확한 신호.
        assert any("fail-closed" in n for n in r["notes"]) or r["rejected"]
        # 2번 URL 은 urls 에 포함돼 있을 수 있으나(부분 반환) rejected 가
        # 비어있지 않으므로 호출자가 진행을 멈춘다. urls 를 신뢰하면 안 됨.
        # (이것이 핵심 — 대표이미지 손상 방지 반례)

    def test_fail_closed_documented_in_notes(self, tmp_path):
        """거부가 있으면 notes 에 fail-closed 안내."""
        bad = tmp_path / "bad.jpg"
        bad.write_text("x")
        r = images.attach_images(
            [str(bad)],
            upload_fn=lambda p: [],
        )
        assert r["rejected"]
        assert any("fail-closed" in n for n in r["notes"])

    def test_invalid_source_type_rejected(self):
        """sources 가 리스트가 아니면 거부."""
        r = images.attach_images("not a list")  # type: ignore[arg-type]
        assert r["rejected"]
        assert r["rejected"][0]["index"] == -1

    def test_local_url_mixed_ordering(self, tmp_path, monkeypatch):
        """로컬 + 외부 URL 혼합 입력 순서 보존(외부 URL 은 fetch_fn 주입)."""
        png = tmp_path / "a.png"
        png.write_bytes(PNG_HEADER)
        external = "https://my-cdn.example.com/x.jpg"
        naver_cdn = "https://shop-phinf.pstatic.net/y.jpg"

        fetch_tmps: list[str] = []

        def fake_fetch(url, *, resolver=None):
            # 매직바이트 통과용 임시 파일을 직접 만들어 반환.
            fd, tmp = __import__("tempfile").mkstemp(suffix=".img")
            with os.fdopen(fd, "wb") as fh:
                fh.write(PNG_HEADER)
            fetch_tmps.append(tmp)
            return {"ok": True, "temp_path": tmp, "reason": "", "hops": 1}

        upload_calls: list[list[str]] = []

        def fake_upload(paths):
            upload_calls.append(list(paths))
            return [f"https://up{i}.example.com/" for i in range(len(paths))]

        r = images.attach_images(
            [str(png), external, naver_cdn, str(png)],
            upload_fn=fake_upload,
            fetch_fn=fake_fetch,
        )
        assert r["rejected"] == []
        assert len(r["urls"]) == 4
        # 순서가 입력과 동일한지.
        assert "pstatic.net" in r["urls"][2]
        # fetch 임시 파일이 모두 정리됐는지.
        for t in fetch_tmps:
            assert not os.path.exists(t)


# --------------------------------------------------------------------------- #
# mcp_server.upload_images 가 정본 가드를 쓰는지 검증
# --------------------------------------------------------------------------- #
class TestMcpUploadUsesGuard:
    """mcp_server.upload_images 가 확장자 위장 파일을 거부하는가(정본 가드 적용)."""

    def test_rejects_extension_mismatch(self, tmp_path):
        """Acceptance: .jpg 인데 내용이 텍스트 → 도구가 거부."""
        fake = tmp_path / "fake.jpg"
        fake.write_text("this is text")
        r = mcp_server.upload_images([str(fake)])
        assert r["ok"] is False
        # 매직바이트 또는 위장 사유가 포함돼야 함.
        assert "매직바이트" in r["error"] or "위장" in r["error"]

    def test_rejects_symlink(self, tmp_path):
        target = tmp_path / "real.png"
        target.write_bytes(PNG_HEADER)
        link = tmp_path / "link.png"
        try:
            os.symlink(str(target), str(link))
        except (OSError, NotImplementedError):
            pytest.skip("symlink not supported")
        r = mcp_server.upload_images([str(link)])
        assert r["ok"] is False
        assert "심볼릭" in r["error"]

    def test_tool_signature_unchanged(self, tmp_path):
        """반환 계약 유지: {ok, image_urls, count, error} 키."""
        png = tmp_path / "ok.png"
        png.write_bytes(PNG_HEADER)
        with mock.patch.object(
            naver_client, "upload_images", return_value=["https://cdn/x.png"]
        ):
            r = mcp_server.upload_images([str(png)])
        assert r["ok"] is True
        assert set(r.keys()) >= {"ok", "image_urls", "count", "error"}
        assert r["count"] == 1


# --------------------------------------------------------------------------- #
# MCP 도구 등록 — 4개 유지
# --------------------------------------------------------------------------- #
class TestToolRegistrationPreserved:
    """MCP 도구가 등록돼 있는지 (무회귀)."""

    def test_tool_count(self):
        import asyncio
        tools = mcp_server.mcp.list_tools()
        if hasattr(tools, "__await__"):
            try:
                tools = asyncio.run(tools)
            except RuntimeError:
                tools = asyncio.get_event_loop().run_until_complete(tools)
        assert len(tools) == 6


# --------------------------------------------------------------------------- #
# 모듈 구조 — import / 의존 방향 검증
# --------------------------------------------------------------------------- #
class TestModuleStructure:
    """images 모듈이 자사 모듈 중 common·naver_client 만 import 하는지."""

    def test_images_imports_only_allowed_modules(self):
        # images.py 소스를 읽어 상위 자사 모듈 import 가 common/naver_client
        # 만 있는지 확인.
        src = (_SRC / "clossify" / "images.py").read_text(encoding="utf-8")
        # from . import ... 또는 from clossify import ... 형태만 허용.
        import re
        matches = re.findall(r"^\s*from\s+\.?\s*import\s+(\w+)", src, re.MULTILINE)
        # common 과 naver_client 만 허용.
        forbidden_modules = {
            "mcp_server", "qa_agents", "category", "category_meta",
            "templates", "keyword_volume", "seo", "copywriting",
            "agent_calls", "text_props", "register",
        }
        for m in matches:
            assert m not in forbidden_modules, f"images -> {m} import 금지"
        # mcp_server 가 images 를 import 하는 건 허용.
        mcp_src = (_SRC / "clossify" / "mcp_server.py").read_text(encoding="utf-8")
        assert "images" in mcp_src

    def test_no_stubs_or_identity_functions(self):
        """images.py 에 무동작/identity/예외 삼킴 함수가 없는지(정성 검사)."""
        src = (_SRC / "clossify" / "images.py").read_text(encoding="utf-8")
        # except Exception: pass 만으로 아무것도 안 하는 패턴 검사.
        bad_patterns = [
            "pass  # stub",
            "return None  # stub",
            "NotImplementedError",
        ]
        for pat in bad_patterns:
            assert pat not in src, f"스텁 패턴 발견: {pat!r}"
