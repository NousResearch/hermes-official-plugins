"""Tests that memory-wiki plugin_api._clamp rejects booleans and non-numeric inputs safely."""
import pytest


@pytest.mark.parametrize("bad_value", [True, False, None, "not-a-number", float("nan"), float("inf")])
def test_clamp_falls_back_to_low_on_invalid_inputs(plugin_api, bad_value):
    assert plugin_api._clamp(bad_value, 1, 200) == 1


def test_clamp_bounds_valid_integers(plugin_api):
    assert plugin_api._clamp(-5, 1, 200) == 1
    assert plugin_api._clamp(50, 1, 200) == 50
    assert plugin_api._clamp(999, 1, 200) == 200
