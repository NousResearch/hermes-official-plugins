<!-- # Modified from Apache-2.0 source skills/attack-path-analysis/references/severity-policy.md; skills/attack-path-analysis/SKILL.md; references/core-scan.md (see NOTICE). -->
# Severity from reachable impact

Rate only after source/control/sink/boundary and counterevidence are established. Keep the following fields separate rather than hiding uncertainty inside one label:

- **Reachability:** who controls which input, through which actual entry point, to which operation.
- **Impact:** the new read/write/identity/privilege/execution/availability capability and the affected asset or tenant.
- **Likelihood:** realistic trigger complexity, reliability, privileges, interaction, and effective mitigations.
- **Prerequisites:** supported configuration, version, deployment, feature flags, network position, and credentials. Distinguish established facts from assumptions.
- **Confidence:** quality of evidence for those claims, not the severity itself.

## Calibration

Use `critical` only for a clear, immediately actionable path to severe compromise with realistic in-scope reachability and low-friction exploitation. Examples may include broad identity/control-plane takeover, severe cross-tenant compromise, or attacker-triggerable code execution with supported consequences; a weakness name alone never earns this level.

Use `high` for major supported impact with realistic high likelihood and no long speculative chain. Bound the affected users/assets and explain the exact privilege delta. Same-tenant or internal paths can matter, but cannot silently be described as unauthenticated internet compromise.

As a conservative default, high impact with medium or unknown likelihood is `medium`; high impact with low likelihood is `low`. Medium or unknown impact reaches `medium` only with high likelihood, otherwise `low`; low impact stays `low`. Product-specific policy may refine calibration only with explicit evidence and stated rationale. Use `informational` for actionable observations without established security impact, not to pad vulnerability counts. Reject defeated claims; do not disguise false positives as low severity.

Self-only behavior or an action requiring authority the attacker already owns is not a new boundary crossing. Trusted-operator misconfiguration, missing headers, version banners, dependency presence, and unusual error behavior alone do not establish severe impact. Conversely, lack of runtime reproduction does not refute an exact source-supported path. Missing deployment facts limit confidence and conditional impact; do not invent either safety or exposure.

For SSRF, show destination control and the actual internal capability reachable; an intended webhook does not automatically defeat risk or prove metadata access. For browser attacks, check origin, credential attachment, parsing, and controls rather than inferring from an HTTP method. For secrets, source access is a prerequisite; do not invent validity or privileges or use credentials to test them.

State what specific additional evidence would raise or lower the rating. Keep root cause, attack steps, validation, and severity explanation distinct.

## Chains

Chain severity is never summed. Preserve primitive findings. An eligible effect must satisfy the next precondition in the same scan/snapshot with compatible actor, tenant, and deployment. Every edge needs resolving evidence references, assumptions, and counterevidence. Do not invent credentials, routes, trust, or network reach.

Base severity on final reachable impact and all prerequisites. A speculative edge makes the chain conditional (`candidate_chain`); a defeated edge belongs in `broken_chain`, with the defeating control retained. Source-supported edges do not imply runtime-confirmed execution.

The deterministic engine owns severity bounds: normally no higher than the maximum justified primitive; an established final code-execution, privilege-gain, or cross-tenant credential-acquisition effect may justify at most one band above that maximum, and only without speculative edges. Explain that actual effect rather than arithmetic. Use `security_scan_record_chains` to propose the graph, then explain existing chain IDs only; prose cannot create an edge or override eligibility.
