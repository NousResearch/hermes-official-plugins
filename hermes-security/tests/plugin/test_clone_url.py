"""#452: plain http repository URLs are not cloned."""
import pytest

from hermes_security.target.clone import CloneError, parse_url, clone_or_update


def test_http_url_is_refused():
    with pytest.raises(CloneError, match="https:// or ssh"):
        parse_url("http://github.com/owner/repo")


def test_https_and_ssh_still_parse():
    url, host, segments = parse_url("https://github.com/owner/repo")
    assert (url, host, segments) == ("https://github.com/owner/repo", "github.com", ["owner", "repo"])
    _, host, segments = parse_url("git@github.com:owner/repo.git")
    assert (host, segments) == ("github.com", ["owner", "repo"])


def test_shorthand_stays_https():
    url, _, _ = parse_url("owner/repo")
    assert url == "https://github.com/owner/repo"


def test_existing_http_origin_is_not_pulled(tmp_path, monkeypatch):
    dest = tmp_path / "repos" / "github.com" / "owner" / "repo"
    (dest / ".git").mkdir(parents=True)

    def _git(args, cwd=None, timeout=None):
        if args[-1] == "remote.origin.url":
            return 0, "http://github.com/owner/repo\n", ""
        raise AssertionError("must not pull an http origin")

    monkeypatch.setattr("hermes_security.target.clone._git", _git)
    with pytest.raises(CloneError, match="plain http"):
        clone_or_update("https://github.com/owner/repo", tmp_path)
