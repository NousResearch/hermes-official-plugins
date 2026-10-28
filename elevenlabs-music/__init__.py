"""hermes-elevenlabs-music: ElevenLabs Music as Hermes tools.

Wiring goes only through the public plugin API: ``ctx.register_tool`` for the tools, ``ctx.get_config``
for the user's settings (read on every call, never cached at registration), ``ctx.session_surface`` /
``ctx.session_cwd`` per call where the host has them (feature-detected: older hosts lack them), and
``ctx.register_skill`` for the bundled music skill. Every audio result is a plain ``MEDIA:`` line; the
Desktop half (``src/``) draws it as a music card where Desktop supports that.
"""

from __future__ import annotations

from pathlib import Path

from . import music_tools as _tools
from . import schemas as _schemas
from .elevenlabs_api import API_KEY_ENV, DEFAULTS, check_available

TOOLSET = "elevenlabs_music"
SKILL = "elevenlabs-music"
# Shown in <available_skills>; the 60-character limit applies (same text as the SKILL.md frontmatter).
SKILL_DESCRIPTION = "Write, edit, extend and split songs with ElevenLabs Music."

# (schema, handler, takes local file paths)
_TOOLS = (
    (_schemas.COMPOSE, _tools.compose, False, "🎵"),
    (_schemas.COMPOSE_PLAN, _tools.compose_plan, False, "🎼"),
    (_schemas.DRAFT_PLAN, _tools.draft_plan, False, "📝"),
    (_schemas.UPLOAD, _tools.upload, True, "📤"),
    (_schemas.STEMS, _tools.stems, True, "🎚️"),
    (_schemas.FROM_VIDEO, _tools.from_video, True, "🎬"),
    (_schemas.FINETUNES, _tools.finetunes, False, "🎛️"),
    (_schemas.JOB, _tools.job, False, "⏳"),
)


def register(ctx) -> None:
    def setting(key: str):
        return ctx.get_config(key, default=DEFAULTS.get(key))

    surface_of = getattr(ctx, "session_surface", None)
    cwd_of = getattr(ctx, "session_cwd", None)

    def surface() -> str | None:
        # None on hosts before 0.22.3 (surface unknown): the tools then wait inline, the safe default.
        try:
            return surface_of() if callable(surface_of) else None
        except Exception:
            return None

    def cwd() -> str:
        try:
            return (cwd_of() if callable(cwd_of) else "") or ""
        except Exception:
            return ""

    def bind(handler, takes_files: bool):
        def run(args, session_id="", **_):
            extra = {"cwd": cwd()} if takes_files else {}
            return handler(args, setting, surface=surface(), session_id=session_id or "", **extra)
        return run

    for schema, handler, takes_files, emoji in _TOOLS:
        ctx.register_tool(name=schema["name"], toolset=TOOLSET, schema=schema, handler=bind(handler, takes_files),
                          check_fn=check_available, requires_env=[API_KEY_ENV], emoji=emoji)
    ctx.register_skill(
        name=SKILL, path=Path(__file__).resolve().parent / "skills" / SKILL / "SKILL.md",
        description=SKILL_DESCRIPTION,
    )


__all__ = ["register"]
