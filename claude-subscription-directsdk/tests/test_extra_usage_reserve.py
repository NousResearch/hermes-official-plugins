"""#398: an opt-in reserve stops before the first billed overage request."""

import extra_usage as eu


def setting(values):
    return lambda key, default="": values.get(key, default)


def limits_at(percent):
    return {"extra_usage": {"is_enabled": True},
            "limits": [{"kind": "weekly_all", "percent": percent, "resets_at": "2026-11-20T00:00:00Z"}]}


def test_default_threshold_is_100():
    assert eu.reserve_percent(setting({})) == 100.0
    assert eu.reserve_percent(setting({"extra_usage_reserve_percent": "abc"})) == 100.0
    assert eu.reserve_percent(setting({"extra_usage_reserve_percent": "0"})) == 100.0
    assert eu.reserve_percent(setting({"extra_usage_reserve_percent": "150"})) == 100.0


def test_reserve_percent_parsed():
    assert eu.reserve_percent(setting({"extra_usage_reserve_percent": "97"})) == 97.0


def test_below_reserve_not_using():
    eu.reset()
    using, _ = eu.observe_usage("k1", limits_at(90), threshold=97.0)
    assert using is False


def test_at_reserve_is_using():
    eu.reset()
    using, resets = eu.observe_usage("k2", limits_at(97), threshold=97.0)
    assert using is True
    assert resets is not None


def test_default_100_only_at_full():
    eu.reset()
    using, _ = eu.observe_usage("k3", limits_at(97))
    assert using is False
    eu.reset()
    using, _ = eu.observe_usage("k4", limits_at(100))
    assert using is True
