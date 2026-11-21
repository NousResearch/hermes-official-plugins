"""A template literal that concatenates a sensitive name must not slip past the denylist."""
from camofox_plugin.camofox_policy import _risky_eval_reason


def test_template_literal_concatenation_is_decoded():
    assert _risky_eval_reason('document[`coo${""}kie`]') == "document.cookie"


def test_plain_string_concatenation_still_caught():
    assert _risky_eval_reason('document["coo" + "kie"]') == "document.cookie"


def test_unrelated_expression_is_allowed():
    assert _risky_eval_reason("document.title") is None
