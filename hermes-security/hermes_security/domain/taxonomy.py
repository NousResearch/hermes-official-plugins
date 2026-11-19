"""Capability vocabulary. data-read additionally requires yieldsCredential=true."""
from .dispositions import SEVERITIES, CONFIDENCE, severity_rank, max_severity, bump_severity
OWASP_2025 = tuple(f"A{i:02d}:2025" for i in range(1, 11))
PRECONDITION_KINDS = ("network-access", "authenticated-session", "role", "tenant-membership", "user-interaction", "config-enabled", "component-reachable", "credential", "file-write", "internal-network")
EFFECT_KINDS = ("data-read", "data-write", "credential-acquire", "identity-change", "privilege-gain", "internal-network-reach", "file-write", "code-exec", "integrity-impact", "availability-impact")
ACTORS = ("anonymous", "authenticated", "same-tenant", "cross-tenant", "admin", "local-user", "internal-network", "any")
TENANTS = ("same", "cross", "any")
EFFECT_SATISFIES = {
 "credential-acquire": {"credential", "authenticated-session"},
 "identity-change": {"authenticated-session", "role"},
 "privilege-gain": {"role", "authenticated-session"},
 "internal-network-reach": {"internal-network", "network-access", "component-reachable"},
 "file-write": {"file-write", "config-enabled"},
 "code-exec": {"file-write", "credential", "internal-network", "component-reachable"},
 "data-read": {"credential"},
}
