"""#477: a shared X post must reach the agent as a URL, not disappear as bare text."""
from xchat_plugin.crypto import message_text


def _shared(text="thoughts?"):
    return {"type": "Message", "content": {
        "text": text,
        "attachments": [{"post": {
            "attachment_id": "a1", "post_url": "https://x.com/user/status/12345", "rest_id": "12345"}}]}}


def test_shared_post_url_is_appended_to_the_text():
    assert "https://x.com/user/status/12345" in message_text(_shared())


def test_typed_comment_is_kept():
    out = message_text(_shared())
    assert "thoughts?" in out


def test_empty_comment_still_yields_the_url():
    assert message_text(_shared("")) == "https://x.com/user/status/12345"


def test_plain_text_message_is_unchanged():
    event = {"type": "Message", "content": {"text": "hello"}}
    assert message_text(event) == "hello"


def test_media_only_attachment_is_not_treated_as_a_post():
    event = {"type": "Message", "content": {"attachments": [{"media_hash_key": "abc"}]}}
    assert message_text(event) is None
