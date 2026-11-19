# Cloud and infrastructure review

## Trace
- Inventory deployment manifests, service exposure, network boundaries, identities, secrets, storage, and administrative endpoints across development and production overlays.
- Follow workload identities and metadata access into cloud permissions; trace ingress headers, egress, and service-to-service trust.

## Sinks and controls
- Least-privilege IAM, private storage, secret references, restricted security groups, non-root containers, scoped mounts, read-only filesystems, resource quotas, and transport verification.

## Counterevidence
- Cite effective overlay values, explicit network policies, external identity gates, and scoped service roles. Do not equate an example configuration with a deployed exposure.

## A10 exceptional conditions
- Check secret-fetch failure, missing environment settings, health-check degradation, autoscaling exhaustion, restart loops, and failed policy initialization. Defaults must not open ingress, bypass auth, or mount sensitive host paths.
