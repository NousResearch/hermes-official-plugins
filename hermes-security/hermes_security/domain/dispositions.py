"""Contract enums; evidence state is not a severity or triage decision."""
EVIDENCE_STATES = ("candidate", "source_supported", "runtime_confirmed", "rejected", "inconclusive")
EVIDENCE_LABELS = {"candidate": "Needs review", "source_supported": "Supported by code", "runtime_confirmed": "Confirmed by test", "rejected": "Not an issue", "inconclusive": "Couldn't confirm"}
CHAIN_STATES = ("candidate_chain", "source_supported_chain", "runtime_confirmed_chain", "broken_chain")
SEVERITIES = ("critical", "high", "medium", "low", "informational")
CONFIDENCE = ("high", "medium", "low")
SCAN_MODES = ("standard", "deep", "diff", "validate")
SAFETY_LEVELS = ("static", "local-safe", "active-authorized")
SCAN_STATUSES = ("created", "running", "awaiting_analysis", "finalizing", "completed", "partial", "canceled", "interrupted", "failed")
COVERAGE_STATES = ("reviewed", "not_applicable", "deferred", "unsupported", "unknown", "failed")
VALIDATION_STATUS = ("NOT_RUN", "passed", "failed", "inconclusive")
TRIAGE_STATES = ("open", "closed", "accepted_risk", "false_positive")

def severity_rank(level):
    """Higher numbers mean higher severity (informational=0)."""
    return len(SEVERITIES) - 1 - SEVERITIES.index(level)

def max_severity(levels):
    return max(levels, key=severity_rank, default="informational")

def bump_severity(level, bands=1):
    if isinstance(bands, bool) or not isinstance(bands, int) or bands < 0:
        raise ValueError("bands must be a nonnegative integer")
    return SEVERITIES[max(0, SEVERITIES.index(level) - bands)]

def human_label(state):
    return EVIDENCE_LABELS.get(state, {"NOT_RUN": "NOT RUN", "candidate_chain": "Needs review", "source_supported_chain": "Supported by code", "runtime_confirmed_chain": "Confirmed by test", "broken_chain": "Broken chain"}.get(state, state.replace("_", " ").capitalize()))
