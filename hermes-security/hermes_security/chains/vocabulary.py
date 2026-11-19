"""Versioned closed vocabulary, mirrored from docs/CONTRACT.md."""
VOCABULARY_VERSION = "hermes-security/capabilities-1"
PRECONDITIONS = frozenset({"network-access", "authenticated-session", "role", "tenant-membership",
    "user-interaction", "config-enabled", "component-reachable", "credential", "file-write", "internal-network"})
EFFECTS = frozenset({"data-read", "data-write", "credential-acquire", "identity-change", "privilege-gain",
    "internal-network-reach", "file-write", "code-exec", "integrity-impact", "availability-impact"})
EFFECT_SATISFIES = {
    "credential-acquire": frozenset({"credential", "authenticated-session"}),
    "identity-change": frozenset({"authenticated-session", "role"}),
    "privilege-gain": frozenset({"role", "authenticated-session"}),
    "internal-network-reach": frozenset({"internal-network", "network-access", "component-reachable"}),
    "file-write": frozenset({"file-write", "config-enabled"}),
    "code-exec": frozenset({"file-write", "credential", "internal-network", "component-reachable"}),
    "data-read": frozenset({"credential"}),  # additionally requires literal yieldsCredential=True
}
ACTORS = frozenset({"anonymous", "authenticated", "same-tenant", "cross-tenant", "admin",
                    "local-user", "internal-network", "any"})
TENANTS = frozenset({"same", "cross", "any"})
SEVERITIES = ("critical", "high", "medium", "low", "informational")
PARTICIPATING_STATES = frozenset({"candidate", "source_supported", "runtime_confirmed"})
# Actor transitions are never inferred from a kind alone. Each requires an explicit
# yieldsActor equal to the required actor, either on the capability or its detail object.
ACTOR_YIELDS = {
    "credential-acquire": ACTORS - {"anonymous", "any", "internal-network"},
    "identity-change": ACTORS - {"any", "internal-network"},
    "privilege-gain": ACTORS - {"anonymous", "any", "internal-network"},
}
