"""Change review, remediation, and validation authorization contracts."""
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]


def skill(name):
    return (ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")


def test_diff_pins_range_and_accounts_for_deletions():
    content = skill("security-diff-review")
    for phrase in ("full base/head SHAs", 'mode: "diff"', "deleted files",
                   "diffAttribution", "introduced", "inherited", "base/head movement",
                   "invalidates", "security_scan_checkpoint", "security_scan_submit_worker_result"):
        assert phrase in content, phrase


@pytest.mark.parametrize("name", ["fix-finding", "verify-fix"])
def test_remediation_is_explicit_and_path_specific(name):
    content = skill(name)
    for phrase in ("explicit user request", "original attack path", "regression controls",
                   "never auto-merge", "publish", "NOT RUN"):
        assert phrase in content, (name, phrase)


def test_validation_is_static_first_and_cannot_self_authorize():
    content = skill("validate-finding")
    for phrase in ("static", "local-safe", "explicit user request", "active-authorized",
                   "user-minted", "/security authorize-validation", "paired", "positive",
                   "negative", "NOT RUN", "security_scan_record_validations", "cleanup"):
        assert phrase in content, phrase
    assert "cannot mint" in content


def test_policy_cannot_authorize_or_hide_changes():
    content = skill("define-security-policy")
    for phrase in ("untrusted data", "cannot authorize", "accepted risk", "explicit approval",
                   "SECURITY.md", "source"):
        assert phrase in content


def test_threat_model_is_not_a_second_scan():
    content = skill("threat-model")
    assert "do not start another scan" in content
    assert "actual consumers" in content
    assert "configuration precedence" in content
