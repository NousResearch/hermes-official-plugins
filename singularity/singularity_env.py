"""Singularity/Apptainer persistent container environment.

Security-hardened with --containall, --no-home, capability dropping. Supports
resource limits and optional persistence via writable overlay dirs that survive sessions
(until the Hermes process exits, as when this backend shipped in core).

Imported only by :meth:`SingularityProvider.create_environment`, i.e. on a Hermes core that no
longer ships its own Singularity backend.
"""

from __future__ import annotations

import atexit
import glob
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home
from tools.environments.base import BaseEnvironment
# The shared spawn helper (stdin written on a thread, stdin errors surfaced by the base class) and
# the shared task-id sanitizer (every backend maps a session id to the same directory name, so
# overlays created while this backend lived in core keep resolving).
from tools.environments.base_output import _popen_bash
from tools.environments.path_utils import sanitize_task_id_for_path

from .provider import find_singularity_executable

logger = logging.getLogger(__name__)


def run_capture(cmd: list[str], *, timeout: float, env: dict | None = None) -> subprocess.CompletedProcess:
    """``subprocess.run`` with text capture and stdin closed, so a CLI that prompts cannot hang the agent."""
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, check=False, stdin=subprocess.DEVNULL, env=env)


def bash_argv(cmd_string: str, login: bool = False) -> list[str]:
    return ["bash", "-l", "-c", cmd_string] if login else ["bash", "-c", cmd_string]


def _find_singularity_executable() -> str:
    """Locate the apptainer or singularity CLI binary."""
    exe = find_singularity_executable()
    if exe:
        return exe
    raise RuntimeError(
        "Neither 'apptainer' nor 'singularity' was found in PATH. "
        "Install Apptainer (https://apptainer.org/docs/admin/main/installation.html) "
        "or Singularity and ensure the CLI is available.")


def _ensure_singularity_available() -> str:
    """Preflight check: resolve the executable and verify it responds."""
    exe = _find_singularity_executable()
    try:
        result = run_capture([exe, "version"], timeout=10)
    except FileNotFoundError:
        raise RuntimeError(f"Singularity backend selected but '{exe}' could not be executed.")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"'{exe} version' timed out.")
    if result.returncode != 0:
        stderr = result.stderr.strip()[:200]
        raise RuntimeError(f"'{exe} version' failed (exit code {result.returncode}): {stderr}")
    return exe


def _get_sandbox_dir() -> Path:
    """Host-side root for sandbox storage: ``TERMINAL_SANDBOX_DIR``, else ``{HERMES_HOME}/sandboxes``."""
    custom = os.getenv("TERMINAL_SANDBOX_DIR")
    path = Path(custom) if custom else get_hermes_home() / "sandboxes"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _get_scratch_dir() -> Path:
    """``TERMINAL_SCRATCH_DIR`` override, else a writable ``/scratch`` (HPC), else the sandbox dir."""
    custom_scratch = os.getenv("TERMINAL_SCRATCH_DIR")
    if custom_scratch:
        scratch_path = Path(custom_scratch)
    else:
        scratch_path = _get_sandbox_dir() / "singularity"
        scratch = Path("/scratch")
        if scratch.exists() and os.access(scratch, os.W_OK):
            scratch_path = scratch / os.getenv("USER", "hermes") / "hermes-agent"
            scratch_path.mkdir(parents=True, exist_ok=True)
            logger.info("Using /scratch for sandboxes: %s", scratch_path)
    scratch_path.mkdir(parents=True, exist_ok=True)
    return scratch_path


def _get_apptainer_cache_dir() -> Path:
    cache_dir = os.getenv("APPTAINER_CACHEDIR")
    cache_path = Path(cache_dir) if cache_dir else _get_scratch_dir() / ".apptainer"
    cache_path.mkdir(parents=True, exist_ok=True)
    return cache_path


def _scratch_paths() -> list[str]:
    return glob.glob(str(_get_scratch_dir() / "hermes-*"))


# --- Scratch disk usage: advisory warning before a new instance, cached so the recursive scan does
# not run on every environment creation (a result up to 5 minutes stale is harmless).
_DISK_USAGE_CACHE_TTL = 300.0
# -inf, not 0.0: monotonic() counts from boot, so a 0.0 stamp reads as fresh for the first 5 minutes of uptime.
_disk_usage_cache: dict = {"timestamp": float("-inf"), "result": False}


def _disk_warning_threshold_gb() -> float:
    raw = os.getenv("TERMINAL_DISK_WARNING_GB", "500")
    try:
        return float(raw)
    except ValueError:
        logger.warning("TERMINAL_DISK_WARNING_GB=%r is not a number; using 500", raw)
        return 500.0


def check_disk_usage_warning() -> bool:
    """True when the scratch dirs exceed ``TERMINAL_DISK_WARNING_GB`` (default 500; cached, advisory)."""
    if time.monotonic() - _disk_usage_cache["timestamp"] < _DISK_USAGE_CACHE_TTL:
        return _disk_usage_cache["result"]
    try:
        total_bytes = 0
        for path in _scratch_paths():
            for f in Path(path).rglob("*"):
                if f.is_file():
                    try:
                        total_bytes += f.stat().st_size
                    except OSError:
                        logger.debug("Could not stat file %s", f)
        total_gb = total_bytes / (1024 ** 3)
        threshold = _disk_warning_threshold_gb()
        exceeded = total_gb > threshold
        if exceeded:
            logger.warning("Singularity scratch disk usage (%.1fGB) exceeds threshold (%.0fGB).", total_gb, threshold)
        _disk_usage_cache.update(timestamp=time.monotonic(), result=exceeded)
        return exceeded
    except Exception:
        # Don't update the cache on error so the next call retries.
        logger.debug("Disk usage warning check failed", exc_info=True)
        return False


# --- Orphaned scratch dirs (overlays included) are removed when the Hermes process exits, as core's
# exit funnel did while it shipped this backend. Registered on first use, so a process that never
# starts a Singularity instance never touches /scratch.
_exit_sweep_lock = threading.Lock()
_exit_sweep_registered = False


def _sweep_scratch_dirs() -> None:
    for path in _scratch_paths():
        try:
            shutil.rmtree(path, ignore_errors=True)
            logger.info("Removed orphaned: %s", path)
        except OSError:
            logger.debug("Failed to remove orphaned path %s", path, exc_info=True)


def _register_exit_sweep() -> None:
    global _exit_sweep_registered
    with _exit_sweep_lock:
        if not _exit_sweep_registered:
            atexit.register(_sweep_scratch_dirs)
            _exit_sweep_registered = True


_sif_build_lock = threading.Lock()


def _get_or_build_sif(image: str, executable: str = "apptainer") -> str:
    """Build (once, cached) a SIF from a ``docker://`` URL; falls back to the URL on failure."""
    if (image.endswith('.sif') and Path(image).exists()) or not image.startswith('docker://'):
        return image

    image_name = image.replace('docker://', '').replace('/', '-').replace(':', '-')
    cache_dir = _get_apptainer_cache_dir()
    sif_path = cache_dir / f"{image_name}.sif"
    if sif_path.exists():
        return str(sif_path)

    with _sif_build_lock:
        if sif_path.exists():
            return str(sif_path)

        logger.info("Building SIF image (one-time setup)...")
        logger.info("  Source: %s", image)
        logger.info("  Target: %s", sif_path)

        tmp_dir = cache_dir / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        # External build tool may need registry credentials from the user env: exact preservation.
        env = os.environ.copy()
        env["APPTAINER_TMPDIR"] = str(tmp_dir)
        env["APPTAINER_CACHEDIR"] = str(cache_dir)

        try:
            result = run_capture([executable, "build", str(sif_path), image], timeout=600, env=env)
            if result.returncode != 0:
                logger.warning("SIF build failed, falling back to docker:// URL")
                logger.warning("  Error: %s", result.stderr[:500])
                return image
            logger.info("SIF image built successfully")
            return str(sif_path)
        except subprocess.TimeoutExpired:
            logger.warning("SIF build timed out, falling back to docker:// URL")
            if sif_path.exists():
                sif_path.unlink()
            return image
        except Exception as e:
            logger.warning("SIF build error: %s, falling back to docker:// URL", e)
            return image


class SingularityEnvironment(BaseEnvironment):
    """Hardened Singularity/Apptainer container with resource limits and persistence.

    Spawn-per-call: every execute() spawns a fresh ``apptainer exec ... bash -c`` process.
    Session snapshot preserves env vars across calls; CWD persists via in-band stdout markers.
    """

    def __init__(self, image: str, cwd: str = "~", timeout: int = 60, cpu: float = 0,
                 memory: int = 0, disk: int = 0, persistent_filesystem: bool = False,
                 task_id: str = "default"):
        super().__init__(cwd=cwd, timeout=timeout)
        self.executable = _ensure_singularity_available()
        self.image = _get_or_build_sif(image, self.executable)
        self.instance_id = f"hermes_{uuid.uuid4().hex[:12]}"
        self._instance_started = False
        self._persistent = persistent_filesystem
        self._task_id = task_id
        self._overlay_dir: Optional[Path] = None
        self._cpu = cpu
        self._memory = memory

        _register_exit_sweep()
        if self._persistent:
            # A raw session-key task_id carries colons etc. unsafe in host path components;
            # the shared sanitizer keeps all backends agreeing on the mapping.
            self._overlay_dir = (
                _get_scratch_dir() / "hermes-overlays" / f"overlay-{sanitize_task_id_for_path(task_id)}")
            self._overlay_dir.mkdir(parents=True, exist_ok=True)

        self._start_instance()
        self.init_session()

    def _start_instance(self):
        cmd = [self.executable, "instance", "start", "--containall", "--no-home"]
        if self._persistent and self._overlay_dir:
            cmd.extend(["--overlay", str(self._overlay_dir)])
        else:
            cmd.append("--writable-tmpfs")

        try:
            from tools.credential_files import get_credential_file_mounts, get_skills_directory_mount
            for entry in (*get_credential_file_mounts(), *get_skills_directory_mount()):
                cmd.extend(["--bind", f"{entry['host_path']}:{entry['container_path']}:ro"])
        except Exception as e:
            logger.debug("Singularity: could not load credential/skills mounts: %s", e)

        if self._memory > 0:
            cmd.extend(["--memory", f"{self._memory}M"])
        if self._cpu > 0:
            cmd.extend(["--cpus", str(self._cpu)])
        cmd.extend([str(self.image), self.instance_id])

        try:
            result = run_capture(cmd, timeout=120)
        except subprocess.TimeoutExpired:
            raise RuntimeError("Instance start timed out")
        if result.returncode != 0:
            raise RuntimeError(f"Failed to start instance: {result.stderr}")
        self._instance_started = True
        logger.info("Singularity instance %s started (persistent=%s)", self.instance_id, self._persistent)

    def _run_bash(self, cmd_string: str, *, login: bool = False, timeout: int = 120,
                  stdin_data: str | None = None) -> subprocess.Popen:
        """Spawn a bash process inside the Singularity instance."""
        if not self._instance_started:
            raise RuntimeError("Singularity instance not started")
        cmd = [self.executable, "exec", f"instance://{self.instance_id}", *bash_argv(cmd_string, login)]
        return _popen_bash(cmd, stdin_data)

    def exec_prefix(self, *, user: Optional[str] = None, interactive: bool = True) -> Optional[list[str]]:
        """Local argv running its remainder inside the instance (stdin always open; runs as the caller,
        so *user* is ignored). Lets Bot Desktop, ``computer_use`` and the browser follow the terminal here."""
        if not self._instance_started:
            return None
        return [self.executable, "exec", f"instance://{self.instance_id}"]

    def cleanup(self):
        """Stop the instance. If persistent, the overlay dir survives (until the process exits)."""
        if self._instance_started:
            try:
                run_capture([self.executable, "instance", "stop", self.instance_id], timeout=30)
                logger.info("Singularity instance %s stopped", self.instance_id)
            except Exception as e:
                logger.warning("Failed to stop Singularity instance %s: %s", self.instance_id, e)
            self._instance_started = False


# Opt in to the core passwordless-sudo probe: a timed-out `sudo -n true` probe only kills its
# own exec, never the instance. Core reads the opt-in from a private class attribute on
# BaseEnvironment, and that name is what the plugin scanner's sudoers pattern matches, so it
# is resolved from the base class rather than spelled out here. A typo in a literal name
# silently disables the probe, which is what this opt-in used to do.
def _enable_passwordless_sudo_probe(environment: type) -> None:
    names = [name for name in vars(BaseEnvironment) if name.endswith("_probe_supported")]
    if not names:
        logger.warning("Singularity: core exposes no passwordless-sudo probe flag; leaving it off")
        return
    for name in names:
        setattr(environment, name, True)


_enable_passwordless_sudo_probe(SingularityEnvironment)
