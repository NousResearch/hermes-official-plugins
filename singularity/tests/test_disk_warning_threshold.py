"""Tests that TERMINAL_DISK_WARNING_GB rejects non-finite and non-positive values."""
import pytest

from singularity_plugin import singularity_env


@pytest.mark.parametrize("bad_value", ["nan", "inf", "-inf", "0", "-10", "not-a-number"])
def test_disk_warning_threshold_falls_back_on_invalid_values(monkeypatch, bad_value):
    monkeypatch.setenv("TERMINAL_DISK_WARNING_GB", bad_value)
    assert singularity_env._disk_warning_threshold_gb() == 500.0


def test_disk_warning_threshold_accepts_positive_finite_value(monkeypatch):
    monkeypatch.setenv("TERMINAL_DISK_WARNING_GB", "125.5")
    assert singularity_env._disk_warning_threshold_gb() == 125.5
