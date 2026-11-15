"""Native's rate_limit_event is the subscription-window signal (#43): the receive
loop used to drop it. It is now summarized secret-free for native_admission."""

from directsdk import _rate_limit_summary


def test_subscription_windows_are_not_paid_use():
    for window in ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet",
                   "seven_day_overage_included"):
        summary = _rate_limit_summary(
            {"type": "rate_limit_event",
             "rate_limit_info": {"rateLimitType": window, "overageStatus": "allowed"}})
        assert summary == {"window": window, "overage_status": "allowed", "using_overage": False}


def test_paid_use_is_flagged_only_when_actually_in_use():
    assert _rate_limit_summary(
        {"rate_limit_info": {"rateLimitType": "five_hour", "overageStatus": "allowed",
                             "isUsingOverage": True}})["using_overage"] is True
    assert _rate_limit_summary(
        {"rate_limit_info": {"rateLimitType": "overage"}})["using_overage"] is True
    # "allowed" alone means permitted, not in use.
    assert _rate_limit_summary(
        {"rate_limit_info": {"rateLimitType": "five_hour",
                             "overageStatus": "allowed"}})["using_overage"] is False


def test_missing_or_malformed_event_summarizes_to_none():
    assert _rate_limit_summary({"type": "rate_limit_event"}) is None
    assert _rate_limit_summary({"rate_limit_info": "nope"}) is None
    assert _rate_limit_summary("nope") is None
