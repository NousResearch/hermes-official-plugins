"""Singularity/Apptainer persistent container environment.

Security-hardened with --containall, --no-home, capability dropping. Supports
resource limits and optional persistence via owner/image/task-bound overlay dirs
that survive sessions and process restarts.

Imported only by :meth:`SingularityProvider.create_environment`, i.e. on a Hermes core that no
longer ships its own Singularity backend.
"""

from __future__ import annotations

import glob
import logging
import hashlib
import re
import os

import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Mapping, Optional

from hermes_constants import get_hermes_home
from tools.environments.base import BaseEnvironment
from tools.environments.local import build_subprocess_env
# The shared spawn helper (stdin written on a thread, stdin errors surfaced by the base class) and
# the shared task-id sanitizer (every backend maps a session id to the same directory name, so
# overlays created while this backend lived in core keep resolving).
from tools.environments.base_output import _popen_bash
from tools.environments.path_utils import sanitize_task_id_for_path

from .provider import find_singularity_executable

logger = logging.getLogger(__name__)

_REGISTRY_AUTH_ENV_VARS = ('APPTAINER_DOCKER_USERNAME', 'APPTAINER_DOCKER_PASSWORD', 'SINGULARITY_DOCKER_USERNAME', 'SINGULARITY_DOCKER_PASSWORD', 'DOCKER_USERNAME', 'DOCKER_PASSWORD')

def _registry_auth_source(
    owner_home: str | os.PathLike | None = None,
) -> Mapping[str, str]:
    """Return registry grants from the exact target profile authority.

    A multiplexed process must bind the grants to the captured artifact owner.
    Falling back to process-global ``os.environ`` there would reintroduce the
    launch profile's credentials after the generic child boundary sanitized
    them. Non-multiplex callers retain the historical ambient fallback.
    """
    from agent.secret_scope import (
        build_profile_secret_scope,
        current_secret_scope,
        is_multiplex_active,
        serves_routed_profile,
    )
    from hermes_constants import get_routing_process_hermes_home

    scope = current_secret_scope()
    owner = Path(owner_home).resolve() if owner_home is not None else None
    private_authority = (is_multiplex_active() or serves_routed_profile()
                         or (owner is not None and owner != get_routing_process_hermes_home().resolve()))
    if scope is not None:
        if private_authority:
            if owner is not None and scope.profile_home != owner:
                raise RuntimeError(
                    "Singularity registry authority does not match artifact owner"
                )
            return scope
        source = dict(os.environ)
        source.update({str(key): str(value) for key, value in scope.items()})
        return source
    if private_authority:
        if owner is None:
            raise RuntimeError(
                "Singularity image build requested without an active target-profile "
                "secret scope or explicit artifact owner while multiplexing is enabled"
            )
        return build_profile_secret_scope(owner, fail_closed_external=True)
    return os.environ



def _singularity_subprocess_env(
    *,
    include_registry_auth: bool = False,
    owner_home: str | os.PathLike | None = None,
    source_home: str | os.PathLike | None = None,
) -> dict[str, str]:
    from agent.secret_scope import is_multiplex_active

    enforce_profile_boundary = owner_home is not None and (
        is_multiplex_active()
        or (
            source_home is not None
            and Path(source_home).resolve() != Path(owner_home).resolve()
        )
    )
    env = build_subprocess_env(
        profile_home=owner_home,
        source_profile_home=source_home,
        enforce_profile_boundary=enforce_profile_boundary,
    )
    if include_registry_auth:
        auth_names = {name.upper() for name in _REGISTRY_AUTH_ENV_VARS}
        for key in list(env):
            if key.upper() in auth_names:
                env.pop(key, None)
        source = _registry_auth_source(owner_home)
        for key in _REGISTRY_AUTH_ENV_VARS:
            value = source.get(key)
            if value is not None:
                env[key] = str(value)
    return env



def _profile_artifact_id(owner_home: str | os.PathLike) -> str:
    return hashlib.sha256(str(Path(owner_home).resolve()).encode()).hexdigest()



def _image_identity(image: str) -> str:
    return hashlib.sha256(image.encode()).hexdigest()



def _resolved_image_identity(
    image_reference: str,
    resolved_image: str,
    *,
    require_immutable: bool,
) -> str:
    """Return immutable image authority or a one-use quarantine identity."""
    resolved_path = Path(resolved_image)
    if resolved_path.is_file():
        digest = hashlib.sha256()
        with resolved_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    lowered = image_reference.lower()
    marker = "@sha256:"
    if marker in lowered:
        digest = lowered.split(marker, 1)[1]
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("invalid SHA-256 image identity")
        return digest

    if require_immutable:
        # A mutable reference that could not be materialized to a local SIF has
        # no reusable image authority. Give this environment a one-use identity
        # so neither overlays nor snapshot registry entries can be reused by a
        # later resolution of the same tag.
        return f"quarantine-{uuid.uuid4().hex}"
    return _image_identity(image_reference)



def _persistent_overlay_dir(
    owner_home: Path, image_authority_id: str, task_id: str, *, adopt_legacy: bool,
) -> Path:
    """Overlay directory for one owner profile, image authority and task.

    Every component is durable across process restarts: the owner path, the
    image content/digest identity and the task id. Scope generations are
    salted per interpreter and must never select persisted state. A genuine
    image change selects a different directory; a credential rotation does
    not, because the filesystem belongs to the profile, not to one secret
    generation. Single-profile callers keep using a pre-existing overlay from
    the earlier flat layout so upgrading does not strand it.
    """
    # A raw session-key task_id carries colons and other characters that are
    # unsafe in host path components (same class of bug as the docker -v mount
    # failure); route it through the shared sanitizer so all backends agree.
    name = f"overlay-{sanitize_task_id_for_path(task_id)}"
    root = _get_scratch_dir() / "hermes-overlays"
    overlay = root / _profile_artifact_id(owner_home) / image_authority_id / name
    legacy = root / name
    if adopt_legacy and not overlay.exists() and legacy.is_dir():
        return legacy
    overlay.mkdir(parents=True, exist_ok=True)
    return overlay




def _find_singularity_executable() -> str:
    """Locate the apptainer or singularity CLI binary."""
    exe = find_singularity_executable()
    if exe:
        return exe
    raise RuntimeError(
        "Neither 'apptainer' nor 'singularity' was found in PATH. "
        "Install Apptainer (https://apptainer.org/docs/admin/main/installation.html) "
        "or Singularity and ensure the CLI is available.")


def _ensure_singularity_available(
    *,
    owner_home: str | os.PathLike | None = None,
    source_home: str | os.PathLike | None = None,
) -> str:
    """Preflight check: resolve the executable and verify it responds."""
    exe = _find_singularity_executable()
    try:
        result = subprocess.run(
            [exe, "version"], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10,
            env=_singularity_subprocess_env(
                owner_home=owner_home,
                source_home=source_home,
            ),
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"Singularity backend selected but '{exe}' could not be executed."
        )
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


# Persistent overlays are not swept at process exit: another process may own them.
_sif_build_lock = threading.Lock()


def _get_or_build_sif(
    image: str,
    executable: str = "apptainer",
    *,
    owner_home: str | os.PathLike | None = None,
    source_home: str | os.PathLike | None = None,
    policy_generation: str = "",
) -> str:
    if image.endswith('.sif') and Path(image).exists():
        return image
    if not image.startswith('docker://'):
        return image

    explicit_owner = Path(owner_home).resolve() if owner_home is not None else None
    owner = explicit_owner or get_hermes_home().resolve()
    owner_id = _profile_artifact_id(owner)
    image_id = _image_identity(image)
    cache_dir = _get_apptainer_cache_dir()
    if not policy_generation:
        # Single-profile installs keep the pre-ownership shared cache entry
        # instead of re-pulling every image after an upgrade.
        legacy_name = image.replace('docker://', '').replace('/', '-').replace(':', '-')
        legacy_sif = cache_dir / f"{legacy_name}.sif"
        if legacy_sif.is_file():
            return str(legacy_sif)
    # The cache is keyed on durable owner + image identity only. A scope
    # generation is process-local (salted per interpreter), so keying on it
    # would force a rebuild after every restart without adding authority.
    profile_cache = cache_dir / "hermes-profile-sif" / owner_id
    profile_cache.mkdir(parents=True, exist_ok=True)
    immutable_reference = "@sha256:" in image.lower()
    cacheable = not policy_generation or immutable_reference
    sif_path = profile_cache / (
        f"{image_id}.sif"
        if cacheable
        else f"{image_id}-{uuid.uuid4().hex}.sif"
    )

    # Single-profile callers retain historical mutable-tag reuse. Under a
    # multiplex authority generation, only digest-pinned references are reused;
    # mutable tags build into a unique path so artifact existence cannot grant
    # a later profile action stale image authority.
    if cacheable and sif_path.exists():
        return str(sif_path)

    with _sif_build_lock:
        if cacheable and sif_path.exists():
            return str(sif_path)

        logger.info("Building SIF image (one-time setup)...")
        logger.info("  Source: %s", image)
        logger.info("  Target: %s", sif_path)

        tmp_dir = profile_cache / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        build_path = tmp_dir / f"{image_id}-{uuid.uuid4().hex}.building.sif"

        # Image construction may need private-registry auth, but only the exact
        # Apptainer/Singularity Docker credential contract is reintroduced.
        env = _singularity_subprocess_env(
            include_registry_auth=True,
            owner_home=explicit_owner,
            source_home=source_home,
        )
        env["APPTAINER_TMPDIR"] = str(tmp_dir)
        env["APPTAINER_CACHEDIR"] = str(cache_dir)

        try:
            result = subprocess.run(
                [executable, "build", str(build_path), image],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=600, env=env,
                stdin=subprocess.DEVNULL,
            )
            if result.returncode != 0:
                build_path.unlink(missing_ok=True)
                logger.warning("SIF build failed, falling back to docker:// URL")
                logger.warning("  Error: %s", result.stderr[:500])
                return image
            if not build_path.exists():
                raise RuntimeError("Apptainer reported success without publishing a SIF")
            os.replace(build_path, sif_path)
            logger.info("SIF image built successfully")
            return str(sif_path)
        except subprocess.TimeoutExpired:
            logger.warning("SIF build timed out, falling back to docker:// URL")
            if build_path.exists():
                build_path.unlink()
            return image
        except Exception as e:
            if build_path.exists():
                build_path.unlink()
            logger.warning("SIF build error: %s, falling back to docker:// URL", e)
            return image



class SingularityEnvironment(BaseEnvironment):
    """Hardened Singularity/Apptainer container with resource limits and persistence.

    Spawn-per-call: every execute() spawns a fresh ``apptainer exec ... bash -c`` process.
    Session snapshot preserves env vars across calls; CWD persists via in-band stdout markers.
    """

    # A timed-out `sudo -n true` probe only kills its own exec, never the instance.
    _passwordless_sudo_probe_supported = True
    _profile_scoped_passthrough = True

    def __init__(
        self,
        image: str,
        cwd: str = "~",
        timeout: int = 60,
        cpu: float = 0,
        memory: int = 0,
        disk: int = 0,
        persistent_filesystem: bool = False,
        task_id: str = "default",
    ):
        super().__init__(cwd=cwd, timeout=timeout)
        boundary = getattr(self, "_profile_env_boundary", None)
        from hermes_constants import get_process_hermes_home

        self._owner_home = (
            boundary.target_home
            if boundary is not None
            else get_hermes_home().resolve()
        )
        self._source_home = (
            boundary.source_home
            if boundary is not None
            else Path(get_process_hermes_home()).resolve()
        )
        self._owner_generation = (
            boundary.target_generation if boundary is not None else ""
        )
        self.executable = _ensure_singularity_available(
            owner_home=self._owner_home,
            source_home=self._source_home,
        )
        self.image = _get_or_build_sif(
            image,
            self.executable,
            owner_home=self._owner_home,
            source_home=self._source_home,
            policy_generation=self._owner_generation,
        )
        self._image_authority_id = _resolved_image_identity(
            image,
            self.image,
            require_immutable=boundary is not None,
        )
        self.instance_id = f"hermes_{uuid.uuid4().hex[:12]}"
        self._instance_started = False
        self._control_env: Optional[dict[str, str]] = None
        self._persistent = persistent_filesystem
        self._task_id = task_id
        self._overlay_dir: Optional[Path] = None
        self._cpu = cpu
        self._memory = memory

        if self._persistent:
            self._overlay_dir = _persistent_overlay_dir(
                self._owner_home,
                self._image_authority_id,
                task_id,
                adopt_legacy=boundary is None,
            )

        self._start_instance()
        self.init_session()


    def _start_instance(self):
        owner_home = self._owner_home
        source_home = self._source_home
        profile_boundary = self._profile_env_boundary
        cmd = [self.executable, "instance", "start"]
        cmd.extend(["--containall", "--no-home"])

        if self._persistent and self._overlay_dir:
            cmd.extend(["--overlay", str(self._overlay_dir)])
        else:
            cmd.append("--writable-tmpfs")

        try:
            from tools.credential_files import get_credential_file_mounts, get_skills_directory_mount
            # The credential-file owner still has a process-global config
            # cache. Until that separate owner becomes profile/generation keyed,
            # quarantine credential mounts under multiplex rather than attach a
            # potentially foreign host capability.
            credential_mounts = (
                []
                if profile_boundary is not None
                else get_credential_file_mounts()
            )
            for mount_entry in credential_mounts:
                cmd.extend(["--bind", f"{mount_entry['host_path']}:{mount_entry['container_path']}:ro"])
            for skills_mount in get_skills_directory_mount():
                cmd.extend(["--bind", f"{skills_mount['host_path']}:{skills_mount['container_path']}:ro"])
        except Exception as e:
            logger.debug("Singularity: could not load credential/skills mounts: %s", e)

        if self._memory > 0:
            cmd.extend(["--memory", f"{self._memory}M"])
        if self._cpu > 0:
            cmd.extend(["--cpus", str(self._cpu)])

        cmd.extend([str(self.image), self.instance_id])

        try:
            # The same environment authorizes this instance's later control
            # commands (cleanup's stop), captured while the owner is current.
            control_env = _singularity_subprocess_env(
                owner_home=owner_home,
                source_home=source_home,
            )
            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120,
                env=control_env, stdin=subprocess.DEVNULL,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Failed to start instance: {result.stderr}")
            self._control_env = dict(control_env)
            self._instance_started = True
            logger.info("Singularity instance %s started (persistent=%s)",
                        self.instance_id, self._persistent)
        except subprocess.TimeoutExpired:
            raise RuntimeError("Instance start timed out")


    def _run_bash(self, cmd_string: str, *, login: bool = False,
                  timeout: int = 120,
                  stdin_data: str | None = None) -> subprocess.Popen:
        """Spawn a bash process inside the Singularity instance."""
        if not self._instance_started:
            raise RuntimeError("Singularity instance not started")

        cmd = [self.executable, "exec",
               f"instance://{self.instance_id}"]
        if login:
            cmd.extend(["bash", "-l", "-c", cmd_string])
        else:
            cmd.extend(["bash", "-c", cmd_string])

        return _popen_bash(
            cmd,
            stdin_data,
            profile_home=self._owner_home,
            source_profile_home=self._source_home,
            enforce_profile_boundary=self._profile_env_boundary is not None,
        )


    def exec_prefix(self, *, user: Optional[str] = None, interactive: bool = True) -> Optional[list[str]]:
        """Local argv running its remainder inside the instance (stdin always open; runs as the caller,
        so *user* is ignored). Lets Bot Desktop, ``computer_use`` and the browser follow the terminal here."""
        if not self._instance_started:
            return None
        return [self.executable, "exec", f"instance://{self.instance_id}"]

    def cleanup(self):
        """Stop the instance. If persistent, the overlay dir survives.

        Stop authority is the control environment captured when this object
        started the instance, never the caller's current profile scope: teardown
        may run from another profile's turn or after this owner's credentials
        rotated, and neither may skip the stop. The handle is cleared only on a
        confirmed stop, so a failure leaves it for the next cleanup attempt.
        """
        owner_home = self._owner_home
        source_home = self._source_home
        if self._instance_started:
            try:
                control_env = getattr(self, "_control_env", None)
                if control_env is None:
                    control_env = _singularity_subprocess_env(
                        owner_home=owner_home,
                        source_home=source_home,
                    )
                result = subprocess.run(
                    [self.executable, "instance", "stop", self.instance_id],
                    capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30,
                    env=dict(control_env),
                    stdin=subprocess.DEVNULL,
                )
            except Exception as e:
                logger.warning("Failed to stop Singularity instance %s: %s", self.instance_id, e)
            else:
                if result.returncode == 0:
                    self._instance_started = False
                    logger.info("Singularity instance %s stopped", self.instance_id)
                else:
                    logger.warning(
                        "Failed to stop Singularity instance %s (exit %s): %s",
                        self.instance_id, result.returncode, (result.stderr or "").strip()[:200],
                    )
