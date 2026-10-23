"""browser_import_cookies: Netscape cookies.txt into the session's Camofox user."""

from __future__ import annotations

import json

import pytest

from camofox_plugin import camofox_tools as t


class TestNetscapeParser:
    def test_seven_field_line(self):
        assert t._netscape_parse(".example.com\tTRUE\t/\tFALSE\t1735689600\tsession\tabc123") == [{
            "name": "session", "value": "abc123", "domain": ".example.com", "path": "/",
            "expires": 1735689600, "httpOnly": False, "secure": False}]

    def test_httponly_prefix(self):
        (cookie,) = t._netscape_parse("#HttpOnly_.example.com\tTRUE\t/\tTRUE\t0\tauth\ttoken")
        assert cookie["httpOnly"] is True and cookie["secure"] is True and cookie["domain"] == ".example.com"

    def test_comments_blank_lines_bom_crlf_and_malformed_lines(self):
        text = ("\ufeff# Netscape HTTP Cookie File\r\n\r\nnot\tenough\tfields\r\n"
                ".a.com\tTRUE\t/\tFALSE\t0\tn1\tv1\r\n.b.com\tTRUE\t/\tFALSE\tbad\tn2\tv2\r\n")
        assert [c["domain"] for c in t._netscape_parse(text)] == [".a.com"]

    def test_value_keeps_embedded_tabs(self):
        (cookie,) = t._netscape_parse(".example.com\tTRUE\t/\tFALSE\t0\ttoken\taaa\tbbb\tccc")
        assert cookie["value"] == "aaa\tbbb\tccc"


@pytest.fixture
def cookies_dir(tmp_path, write_config):
    path = tmp_path / "cookies"
    path.mkdir()
    write_config(f"browser:\n  camofox:\n    cookies_dir: {path}\n")
    return path


class TestReadCookieFile:
    def test_reads_inside_the_configured_dir(self, cookies_dir):
        (cookies_dir / "site.txt").write_text(".example.com\tTRUE\t/\tFALSE\t0\tfoo\tbar\n")
        assert t._read_cookie_file("site.txt")[0]["name"] == "foo"

    def test_traversal_and_absolute_paths_are_refused(self, cookies_dir):
        (cookies_dir.parent / "secret.txt").write_text(".evil\tTRUE\t/\tFALSE\t0\tleaked\tvalue\n")
        for bad in ("../secret.txt", str(cookies_dir.parent / "secret.txt")):
            with pytest.raises(ValueError, match="relative path"):
                t._read_cookie_file(bad)

    def test_the_dir_itself_is_not_a_cookie_file(self, cookies_dir, camofox, monkeypatch):
        monkeypatch.setenv("CAMOFOX_API_KEY", "k")
        (cookies_dir / "sub").mkdir()
        for path in ("", "sub"):
            result = json.loads(t.browser_import_cookies(path, task_id="t1"))
            assert result["success"] is False and "not found" in result["error"]

    def test_oversized_file_is_refused(self, cookies_dir):
        (cookies_dir / "big.txt").write_text("x" * (6 * 1024 * 1024))
        with pytest.raises(ValueError, match="too large"):
            t._read_cookie_file("big.txt")

    def test_domain_suffix_filter(self, cookies_dir):
        (cookies_dir / "mixed.txt").write_text(
            ".linkedin.com\tTRUE\t/\tFALSE\t0\tli_at\ttokA\n.google.com\tTRUE\t/\tFALSE\t0\tSID\ttokB\n"
            "www.linkedin.com\tTRUE\t/\tFALSE\t0\tJSESSIONID\ttokC\n")
        assert {c["name"] for c in t._read_cookie_file("mixed.txt", ".linkedin.com")} == {"li_at", "JSESSIONID"}

    def test_default_dir_is_under_home(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows expanduser ignores HOME
        assert t._cookies_dir() == str(tmp_path / ".camofox" / "cookies")


class TestImport:
    def test_needs_the_api_key(self, camofox, cookies_dir):
        (cookies_dir / "x.txt").write_text(".example.com\tTRUE\t/\tFALSE\t0\tfoo\tbar\n")
        result = json.loads(t.browser_import_cookies("x.txt", task_id="t1"))
        assert result["success"] is False and "CAMOFOX_API_KEY" in result["error"]
        assert camofox.requests == []

    def test_posts_sanitized_cookies_with_the_bearer_token(self, camofox, cookies_dir, monkeypatch):
        monkeypatch.setenv("CAMOFOX_API_KEY", "k-secret-123")
        (cookies_dir / "x.txt").write_text(".example.com\tTRUE\t/\tFALSE\t0\tfoo\tbar\n")
        result = json.loads(t.browser_import_cookies("x.txt", task_id="t1"))
        assert result["success"] is True and result["imported"] == 1
        (call,) = camofox.calls("POST", "/cookies")
        assert call["path"] == f"/sessions/{result['user_id']}/cookies"
        assert call["headers"]["Authorization"] == "Bearer k-secret-123"
        assert call["body"]["cookies"][0] == {"name": "foo", "value": "bar", "domain": ".example.com", "path": "/",
                                              "expires": 0, "httpOnly": False, "secure": False}

    def test_server_refusal_is_surfaced(self, camofox, cookies_dir, monkeypatch):
        monkeypatch.setenv("CAMOFOX_API_KEY", "wrong")
        (cookies_dir / "x.txt").write_text(".example.com\tTRUE\t/\tFALSE\t0\tfoo\tbar\n")
        user = t.client.get_session("t1")["user_id"]
        camofox.overrides[("POST", f"/sessions/{user}/cookies")] = (403, {"error": "Forbidden"})
        result = json.loads(t.browser_import_cookies("x.txt", task_id="t1"))
        assert result["success"] is False and "HTTP 403" in result["error"] and "Forbidden" in result["error"]

    def test_more_than_500_cookies_is_refused_client_side(self, camofox, cookies_dir, monkeypatch):
        monkeypatch.setenv("CAMOFOX_API_KEY", "k")
        (cookies_dir / "many.txt").write_text(
            "".join(f".example.com\tTRUE\t/\tFALSE\t0\tc{i}\tv\n" for i in range(501)))
        assert "Too many cookies" in json.loads(t.browser_import_cookies("many.txt", task_id="t1"))["error"]
        assert camofox.calls("POST", "/cookies") == []
