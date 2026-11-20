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


def _event(utilization, overage_status="allowed", using=False):
    """The secret-free summary the client builds from native's rate_limit_event."""
    from directsdk import _rate_limit_summary
    return _rate_limit_summary({"rate_limit_info": {
        "rateLimitType": "five_hour", "overageStatus": overage_status, "isUsingOverage": using, "resetsAt": 4102444800,
        "unifiedWindows": {"five_hour": {"utilization": utilization}, "seven_day": {"utilization": 0.2}}}})


def test_a_running_process_stops_at_the_reserve_from_the_turns_own_event():
    """After a turn reports 98% the next request is refused without a new probe; below the reserve it is sent."""
    import pytest
    values = {"extra_usage": "stop", "extra_usage_reserve_percent": "97"}
    no_probe = lambda: pytest.fail("a fresh event answer must not probe")
    eu.reset()
    eu.observe("k5", _event(0.90), setting(values))
    eu.admit("k5", setting(values), no_probe)
    eu.observe("k5", _event(0.98), setting(values))
    with pytest.raises(eu.ClaudeExtraUsageBlocked, match="97% extra_usage_reserve_percent"):
        eu.admit("k5", setting(values), no_probe)


def test_the_event_reserve_needs_permitted_overage_and_a_refusing_mode():
    import pytest
    no_probe = lambda: pytest.fail("a fresh event answer must not probe")
    eu.reset()
    # Overage not permitted on this account: nothing past the limit can be billed.
    stop = {"extra_usage": "stop", "extra_usage_reserve_percent": "97"}
    eu.observe("k6", _event(0.99, overage_status="rejected"), setting(stop))
    eu.admit("k6", setting(stop), no_probe)
    # notify never refuses, and an unset reserve keeps the old wall at 100%.
    eu.observe("k7", _event(0.99), setting({"extra_usage": "notify", "extra_usage_reserve_percent": "97"}))
    eu.observe("k8", _event(0.99), setting({"extra_usage": "stop"}))
    eu.admit("k8", setting({"extra_usage": "stop"}), no_probe)
