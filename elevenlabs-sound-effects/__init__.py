"""hermes-elevenlabs-sound-effects — ElevenLabs sound effect generation as a Hermes tool.

Wiring goes only through the public plugin API: ``ctx.register_tool`` for the tool and
``ctx.get_config`` for the user's settings (read on every call, never cached at registration), and
``ctx.register_skill`` for the bundled sound-design skill. The tool always returns a plain ``MEDIA:``
line; the Desktop half (``src/``) draws it as a sound card where Desktop supports that.
"""

from __future__ import annotations

from pathlib import Path

from . import sound_effects as _sfx

TOOLSET = "elevenlabs_sound_effects"
SKILL = "elevenlabs-sound-design"
# Shown in <available_skills>; the 60-character limit applies (same text as the SKILL.md frontmatter).
SKILL_DESCRIPTION = "Write ElevenLabs sound effect prompts, loops and packs."


def register(ctx) -> None:
    def setting(key: str):
        return ctx.get_config(key, default=_sfx.DEFAULTS.get(key))

    ctx.register_tool(
        name="elevenlabs_sound_effect", toolset=TOOLSET, schema=_sfx.SCHEMA,
        handler=lambda args, session_id="", **_: _sfx.generate(args, setting, session_id=session_id or ""),
        check_fn=_sfx.check_available, requires_env=[_sfx.API_KEY_ENV], emoji="🔊",
    )
    ctx.register_skill(
        name=SKILL, path=Path(__file__).resolve().parent / "skills" / SKILL / "SKILL.md",
        description=SKILL_DESCRIPTION,
    )


__all__ = ["register"]
