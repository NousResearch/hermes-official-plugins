"""#453: doctor says what a command cannot see, without claiming the list is complete."""
from bubblewrap_plugin import BubblewrapProvider


def test_doctor_names_the_hidden_categories():
    rows = BubblewrapProvider().doctor_checks()
    hidden = [detail for _ok, name, detail in rows if name == "hidden from commands"]
    assert len(hidden) == 1
    text = hidden[0].lower()
    assert "credential stores" in text
    assert "agent sockets" in text
    assert "hermes_home" in text
    assert "not an exhaustive list" in text
