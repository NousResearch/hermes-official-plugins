"""``_place``: a place against max_sessions, or the longest-idle kept process's place (no Claude Code needed)."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import directsdk


class Kept:
    """A kept process as ``_place`` sees it: when it began to wait for its user, and its owner's way to end it."""

    def __init__(self, since, ends):
        self.idle_since, self.calls = since, []

        def end(native, idle=False):
            self.calls.append(idle)
            if ends:
                directsdk._live.discard(native)
            return ends
        self.end = lambda: end


def test_the_longest_idle_gives_its_place_and_one_that_cannot_is_left_as_it_is(monkeypatch):
    """The oldest is no longer its owner's idle session (a step took it): it is skipped and keeps its idle stamp; the
    next oldest gives its place. Nothing waits for a user: no place."""
    stale, older, newer, parked = Kept(1.0, False), Kept(2.0, True), Kept(3.0, True), Kept(None, True)
    monkeypatch.setattr(directsdk, '_live', {stale, older, newer, parked})
    reservation = directsdk._place(4)
    assert isinstance(reservation, directsdk._Reservation) and reservation in directsdk._live
    assert (stale.calls, older.calls, newer.calls, parked.calls) == ([True], [True], [], [])
    assert stale.idle_since == 1.0 and older not in directsdk._live
    monkeypatch.setattr(directsdk, '_live', {stale, parked, directsdk._Reservation()})
    assert directsdk._place(3) is None and parked.calls == []
