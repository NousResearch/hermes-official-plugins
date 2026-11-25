"""A vendor-prefixed model id (``anthropic/claude-sonnet-5-5``, #421) reaches a kept process as the bare id: its
requests are the bytes the plain id sends per-call."""
import pytest

from test_directsdk_session import CLAUDE, client, converse, home, session_pids, upstream  # noqa: F401

pytestmark = pytest.mark.skipif(not CLAUDE, reason='needs the Claude Code CLI')


def test_a_kept_process_on_a_vendor_prefixed_id_sends_the_plain_ids_bytes(home, upstream, monkeypatch):
    plain = client(home, upstream, monkeypatch, session=False)
    try:
        converse(plain)
    finally:
        plain.close()
    expected, upstream.bodies = list(upstream.bodies), []
    kept, pids = client(home, upstream, monkeypatch, session=True), []
    create = kept.create
    kept.create = lambda **kw: create(**dict(kw, model='anthropic/' + kw['model']))
    try:
        converse(kept, on_step=lambda: pids.append(session_pids(kept)))
    finally:
        kept.close()
    assert len(set().union(*pids)) == 1
    assert len(expected) == 12 and upstream.bodies == expected
