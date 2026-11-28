"""``write_approvers`` from core's ``tests/gateway/test_approval_requester_binding.py`` (the ported shared-burst
test imported it from there)."""

import hermes_yaml as yaml

DEFAULT = "<no approvers key>"


def write_approvers(approvers):
    """Write the profile's config.yaml as a user would (``DEFAULT`` leaves the key out)."""
    from hermes_constants import get_hermes_home

    approvals = {"mode": "manual", **({} if approvers == DEFAULT else {"approvers": approvers})}
    (get_hermes_home() / "config.yaml").write_text(yaml.safe_dump({"approvals": approvals}), encoding="utf-8")
