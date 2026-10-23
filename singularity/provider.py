"""The ``singularity`` terminal backend provider: classification, health checks and environment creation."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from agent.terminal_env_provider import TerminalEnvironmentProvider

logger = logging.getLogger(__name__)

_INSTALL_URL = "https://apptainer.org/docs/admin/main/installation.html"


def find_singularity_executable() -> Optional[str]:
    """``apptainer`` or ``singularity`` on PATH (apptainer preferred), else None."""
    return next((exe for exe in ("apptainer", "singularity") if shutil.which(exe)), None)


def _terminal_config() -> dict:
    from hermes_cli.config import load_config_readonly
    terminal = load_config_readonly().get("terminal")
    return terminal if isinstance(terminal, dict) else {}


def _default_image() -> str:
    # Core's default sandbox image, as a docker:// reference Apptainer builds a SIF from.
    from hermes_cli.config_defaults import DEFAULT_SANDBOX_IMAGE
    return f"docker://{DEFAULT_SANDBOX_IMAGE}"


def configured_image() -> str:
    """``terminal.singularity_image`` from config.yaml, else ``TERMINAL_SINGULARITY_IMAGE`` (env or the
    profile's ``.env``), else Hermes' default sandbox image. config.yaml wins over the env var, as it did
    while core bridged the key."""
    value = _terminal_config().get("singularity_image")
    if value is None or not str(value).strip():
        from hermes_cli.config import get_env_value
        value = get_env_value("TERMINAL_SINGULARITY_IMAGE")
    value = str(value or "").strip()
    return value or _default_image()


def as_apptainer_ref(image: str) -> str:
    """A bare registry reference (``python:3.11``, ``nousresearch/hermes-sandbox:desktop``) becomes
    ``docker://<ref>``; anything with a scheme (``docker://``, ``library://``, ``oras://``), a ``.sif``
    file or an existing path (sandbox directory) is used as given."""
    if "://" in image or image.endswith(".sif") or Path(image).exists():
        return image
    return f"docker://{image}"


def resolve_image(image: Optional[str]) -> str:
    """The image for a new instance: the per-task override Hermes passes (batch runs register
    ``singularity_image``), else :func:`configured_image`."""
    return as_apptainer_ref((image or "").strip() or configured_image())


class SingularityProvider(TerminalEnvironmentProvider):
    """Singularity/Apptainer: a rootless, HPC-friendly container on this machine."""

    # Commands run in the container's own filesystem, not the host's: host OS/home/cwd hints are
    # suppressed, host-looking cwds are sanitized, and the disposable sandbox skips approval prompts.
    is_remote = True
    is_container = True
    supports_streams = True

    @property
    def name(self) -> str:
        return "singularity"

    @property
    def display_name(self) -> str:
        return "Singularity/Apptainer"

    @property
    def description(self) -> str:
        return "HPC-friendly rootless container (Apptainer or Singularity)."

    @property
    def env_description(self) -> str:
        return "a Singularity container (Linux)"

    def is_available(self) -> bool:
        return find_singularity_executable() is not None

    def check_requirements(self, config: dict[str, Any]) -> bool:
        exe = find_singularity_executable()
        if not exe:
            logger.error("Singularity backend selected but neither 'apptainer' nor 'singularity' is on PATH. "
                         "Install Apptainer: %s", _INSTALL_URL)
            return False
        try:
            probe = subprocess.run([exe, "--version"], capture_output=True, timeout=5,
                                   stdin=subprocess.DEVNULL, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.error("Apptainer/Singularity is installed but `%s --version` failed (%s), "
                         "so the install looks broken", exe, exc)
            return False
        if probe.returncode != 0:
            logger.error("Apptainer/Singularity is installed but `%s --version` exited with code %s, "
                         "so the install looks broken", exe, probe.returncode)
            return False
        return True

    def probe(self) -> tuple[str, str]:
        if self.is_available():
            return ("ready", "")
        return ("needs_setup", "Neither singularity nor apptainer found on PATH.")

    def setup_instructions(self) -> list[str]:
        exe = find_singularity_executable()
        lines = [f"Found {exe}." if exe else f"Singularity/Apptainer not found in PATH! Install: {_INSTALL_URL}"]
        lines.append(f"Image: {configured_image()} (change it with "
                     "`hermes config set terminal.singularity_image <image>`).")
        return lines

    def doctor_checks(self) -> list[tuple[bool, str, str]]:
        exe = find_singularity_executable()
        return [(bool(exe), "Singularity/Apptainer",
                 f"({exe} found)" if exe else f"(not found on PATH; install: {_INSTALL_URL})")]

    def create_environment(self, *, cwd: str, timeout: int, task_id: str = "default",
                           image: Optional[str] = None, container_config: Optional[dict[str, Any]] = None,
                           **kwargs: Any):
        from .singularity_env import SingularityEnvironment, check_disk_usage_warning
        cc = container_config or {}
        check_disk_usage_warning()
        return SingularityEnvironment(
            image=resolve_image(image), cwd=cwd, timeout=timeout,
            cpu=cc.get("container_cpu", 1), memory=cc.get("container_memory", 5120),
            disk=cc.get("container_disk", 51200), persistent_filesystem=cc.get("container_persistent", True),
            task_id=task_id)
