"""singularity — Hermes terminal backend that runs commands in a Singularity/Apptainer container.

Official catalog plugin (formerly built into Hermes core as ``tools/environments/singularity.py``).
Select it with ``terminal.backend: singularity``; the image comes from ``terminal.singularity_image``
(or ``TERMINAL_SINGULARITY_IMAGE``) and the limits from ``terminal.container_*``, exactly as before.
See README.md.
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path

from .provider import SingularityProvider

logger = logging.getLogger(__name__)


def _core_ships_singularity() -> bool:
    """True on a Hermes core that still has its built-in Singularity backend.

    That core reserves the ``singularity`` name (a registration would be refused) and serves the
    backend itself, so the plugin stays inactive there. Looks only next to core's own environment
    base class, never at an editable install of another checkout."""
    from tools.environments.base import BaseEnvironment
    return (Path(inspect.getfile(BaseEnvironment)).parent / "singularity.py").is_file()


def register(ctx) -> None:
    if _core_ships_singularity():
        logger.info("singularity: this Hermes core still ships the Singularity backend built in; "
                    "the plugin stays inactive until core drops it")
        return
    ctx.register_terminal_environment_provider(SingularityProvider())
