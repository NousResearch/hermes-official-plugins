"""Pinned native routes; a loopback gateway needs explicit long-context selection."""
CONTEXT_WINDOWS = {
    'claude-sonnet-5-5': 1_000_000,
    'claude-sonnet-5': 1_000_000,
    'claude-haiku-5-5': 1_000_000,
    'claude-haiku-4-5-20251001': 200_000,
    'claude-opus-5-5': 1_000_000,
    'claude-opus-5': 1_000_000,
    'claude-opus-4-8': 1_000_000,
    'claude-fable-5-1': 1_000_000,
}
# Families that 400 on ``thinking: {"type": "disabled"}`` (the same contract Hermes core keeps
# in agent/anthropic_adapter.py). A caller's disable is omitted for them, which beats a dead
# request. That does not turn thinking down: a reasoning-off request on these routes carries no
# `thinking` and no effort (neither `output_config.effort` nor `--effort`), so the model thinks
# at native's default effort. Opus 5.5 joined Fable: the docs say thinking cannot be turned off
# there and the API answers the disable with the same 400 (#22). Plain Opus 5 still accepts it,
# so the entry is the full `claude-opus-5-5` prefix. Sonnet 5.5 rejects the disable too (its lowest
# setting is `between_tools`); plain Sonnet 5 still accepts it. The docs say thinking cannot be
# turned off on Haiku 5.5 either; Haiku 4.5 still accepts the disable.
MANDATORY_THINKING = ('claude-fable', 'claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5')
ALIASES = {
    'sonnet': 'claude-sonnet-5-5',
    'haiku': 'claude-haiku-5-5',
    'claude-haiku-4-5': 'claude-haiku-4-5-20251001',
    'opus': 'claude-opus-5-5',
    'fable': 'claude-fable-5-1',
}
# Haiku 4.5 rejects `thinking: {'type': 'adaptive'}` with a 400 upstream; its effort signal
# still applies. Unknown routes keep adaptive so future models are not silently downgraded.
NO_ADAPTIVE_THINKING = frozenset({'claude-haiku-4-5-20251001'})


def _resolve_route(model):
    """Resolve a route spelling (full, short, dotted, aliased) to a pinned canonical id or itself."""
    if not isinstance(model, str):
        return model
    base = model.removesuffix('[1m]')
    if base in ALIASES:
        return ALIASES[base]
    if base in CONTEXT_WINDOWS:
        return base
    candidate = base
    if not candidate.startswith('claude-'):
        candidate = 'claude-' + candidate
    candidate = candidate.replace('.', '-')
    if candidate in ALIASES:
        return ALIASES[candidate]
    if candidate in CONTEXT_WINDOWS:
        return candidate
    return base


def native_model(model):
    if not isinstance(model, str):
        return model
    base = model.removesuffix('[1m]')
    canonical = _resolve_route(base)
    window = CONTEXT_WINDOWS.get(canonical)
    long_context = model.endswith('[1m]')
    if window == 1_000_000:
        return canonical + '[1m]'
    if window == 200_000:
        if long_context:
            raise ValueError('Haiku 4.5 does not support a 1M context window')
        return canonical
    return model


def accepts_thinking_disable(model):
    # Same tolerance as supports_adaptive_thinking: an absent model still reaches `model is required`.
    base = model.removesuffix('[1m]') if isinstance(model, str) else ''
    canonical = _resolve_route(base)
    return not canonical.startswith(MANDATORY_THINKING)


def supports_adaptive_thinking(model):
    # Body assembly precedes model validation, so an absent or invalid route answers True
    # and still reaches the existing `model is required` error.
    base = model.removesuffix('[1m]') if isinstance(model, str) else ''
    canonical = _resolve_route(base)
    return canonical not in NO_ADAPTIVE_THINKING


MODEL_METADATA = {
    native_model(model): {'canonical_model': model, 'context_window': window}
    for model, window in CONTEXT_WINDOWS.items()
}


def _session_ids(canonical):
    """Every id a session may carry for a pinned route: core looks capabilities up by exact id."""
    aliases = [alias for alias, target in ALIASES.items() if target == canonical]
    ids = {canonical, native_model(canonical), *aliases}
    if CONTEXT_WINDOWS[canonical] == 1_000_000:
        ids.update(alias + '[1m]' for alias in aliases)
    return ids


# Declared with the window: a declared model without one falls to core's 200K unknown-model
# default in the capability lookup (dashboard model info). Unpinned ids keep the catalog path.
MODEL_CAPABILITIES = {
    model_id: {'supports_vision': True, 'context_window': window}
    for canonical, window in CONTEXT_WINDOWS.items()
    for model_id in _session_ids(canonical)
}
