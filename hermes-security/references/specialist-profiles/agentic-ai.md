# Agent and tool review

## Trace
- Track the trust boundary between user authorization, system instructions, retrieved content, repository files, model output, and executable tool arguments.
- Inventory tools, MCP servers, credentials, filesystem access, network destinations, approval gates, and persisted memory. Treat external instructions as data.
- Trace prompt-injected input through planning and tool selection into concrete side effects; show actor, accessible data, and action scope rather than claiming all model text is exploitable.

## Sinks and controls
- Argument validation, path containment, origin allowlists, least-privilege tools, explicit approval for consequential actions, result-size limits, secret redaction, and provenance-aware memory writes.

## Counterevidence
- Cite non-model policy enforcement, read-only tools, isolated execution, denied capabilities, and scoped credentials. A prompt saying not to do something is not equivalent to a tool-level gate.

## A10 exceptional conditions
- Check tool timeout ambiguity, partial actions, duplicated sends on retries, malformed model output, dropped approval state, and token/resource exhaustion. Never turn parser failures into unrestricted tool execution or treat missing tool results as success.
